"""Opt-in, local-Bridge-only rules runner. Never registered as an MCP tool."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
import tempfile
from contextlib import contextmanager, suppress
from pathlib import Path

from . import localfs
from .action_log import client_context, new_record
from .cleanup_folders import _move_folders, _real_folder_name
from .config import Settings
from .folder_rules import RULES_PATH, FolderRulesService, _unique_object, engine_rules
from .guarded import EXECUTE_CHUNK_SIZE, MAX_BATCH
from .mail import MailClient, MailError, MailValidationError
from .rules_source import require_rules_source
from .rules_store import store_path
from .search import _uidvalidity
from .security import _local_address


def _private_file(path, flags):
    refusal = localfs.parent_refusal(path)
    if refusal:
        raise MailError(f"Rules state {refusal}")
    descriptor = localfs.open(
        path, flags | localfs.O_NOFOLLOW | localfs.O_CLOEXEC | localfs.O_NONBLOCK, 0o600
    )
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or not localfs.owned(info) or info.st_nlink != 1:
        os.close(descriptor)
        raise MailError("Rules state must be a private regular file")
    refusal = localfs.acl_refusal(descriptor)
    if refusal:
        os.close(descriptor)
        raise MailError(f"Rules state {refusal}")
    return descriptor


class RulesTimer:
    def __init__(self, cleanup, *, rules_path=RULES_PATH, state_path=None):
        self.cleanup = cleanup
        self.rules_path = Path(rules_path)
        # The same saved custom rules that add_rule writes and manual rules apply.
        self.saved_rules_path = store_path(cleanup.settings)
        self.state_path = (
            Path(state_path)
            if state_path is not None
            else cleanup.backend.log.path.with_name("rules-state.json")
        )
        self.lock_path = self.state_path.with_suffix(".lock")
        paths = (self.state_path, self.lock_path, cleanup.backend.log.path, self.saved_rules_path)
        if len({path.resolve() for path in paths}) != len(paths):
            raise MailValidationError(
                "Rules state, lock, action journal and saved rules need distinct paths"
            )

    @contextmanager
    def _lock(self):
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = _private_file(self.lock_path, os.O_RDWR | os.O_CREAT)
        try:
            localfs.fchmod(descriptor, 0o600)
            try:
                localfs.lock(descriptor)
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            os.close(descriptor)

    def _load_state(self):
        try:
            descriptor = _private_file(self.state_path, os.O_RDONLY)
        except FileNotFoundError:
            return {"version": 1, "mailboxes": {}}
        try:
            with os.fdopen(descriptor, "rb") as stream:
                raw = stream.read(65537)
            state = json.loads(raw, object_pairs_hook=_unique_object)
            if (
                len(raw) > 65536
                or not isinstance(state, dict)
                or set(state) != {"version", "mailboxes"}
                or type(state["version"]) is not int
                or state["version"] != 1
                or not isinstance(state["mailboxes"], dict)
            ):
                raise ValueError
            for folder, mark in state["mailboxes"].items():
                if (
                    not _real_folder_name(folder)
                    or not isinstance(mark, dict)
                    or set(mark) != {"uidvalidity", "uid", "pending"}
                    or not isinstance(mark["uidvalidity"], str)
                    or not mark["uidvalidity"].isascii()
                    or not mark["uidvalidity"].isdigit()
                    or int(mark["uidvalidity"]) < 1
                    or type(mark["uid"]) is not int
                    or not 0 <= mark["uid"] <= 4294967295
                    or type(mark["pending"]) is not bool
                ):
                    raise ValueError
            return state
        except (ValueError, TypeError, RecursionError):
            raise MailError("Rules state is invalid; refusing changes") from None

    def _save_state(self, state):
        descriptor, name = tempfile.mkstemp(prefix=".rules-state-", dir=self.state_path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                json.dump(state, stream, ensure_ascii=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            localfs.replace(name, self.state_path)
            directory = localfs.open(self.state_path.parent, os.O_RDONLY | localfs.O_DIRECTORY)
            try:
                localfs.fsync_directory(directory)
            finally:
                os.close(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(name)

    def _event(self, folder, status, **details):
        record = new_record("rules_timer", {"folder": folder})
        record.update(status=status, **details)
        with self.cleanup.backend.log.transaction() as transaction:
            transaction.append(record)

    def run(self, folder="INBOX", cap=None):
        cap = self.cleanup.settings.rules_max_moves if cap is None else cap
        if type(cap) is not int or not 1 <= cap <= MAX_BATCH:
            raise MailValidationError("Rules move cap must be between 1 and 200")
        require_rules_source(folder)
        # One high-water mark for the one case-insensitive INBOX identity.
        folder = "INBOX"
        with self._lock() as acquired:
            if not acquired:
                return {"status": "busy", "count": 0, "log_ids": []}
            try:
                if _local_address(self.cleanup.settings.imap_host) is None:
                    raise MailValidationError("Rules timer requires a loopback Bridge endpoint")
                with self.cleanup._exclusive():
                    return self._run(folder, cap)
            except Exception:
                with suppress(Exception):
                    self._event(folder, "failed")
                raise

    def _run(self, folder, cap):
        rules, digest = engine_rules(self.rules_path, self.saved_rules_path)
        state = self._load_state()
        mark = state["mailboxes"].get(folder)
        if mark and mark["pending"]:
            raise MailError("Rules run needs journal inspection before clearing its pending mark")
        cleanup = self.cleanup
        # A durable run record also proves the journal writable before any move.
        self._event(folder, "started", rules_digest=digest)
        result = {"status": "completed", "count": 0, "skipped": 0, "log_ids": []}
        deadline = cleanup.client._deadline(execute=True)
        with cleanup.client._session(deadline=deadline) as (connection, _):
            source_folder = require_rules_source(folder, connection)
            real_folders = _move_folders(connection)
            if source_folder not in real_folders or any(
                rule.folder not in real_folders for rule in rules
            ):
                raise MailValidationError("Rules require existing real source and target folders")
            cleanup.client._select(connection, source_folder)
            validity = _uidvalidity(connection)
            uids = sorted({int(uid) for uid in cleanup.client._search(connection, ["ALL"])})
            if any(not 1 <= uid <= 4294967295 for uid in uids):
                raise MailError("Mailbox returned invalid message identifiers")

            def checkpoint(uid):
                cleanup.client._select(connection, source_folder)
                if _uidvalidity(connection) != validity:
                    raise MailError("Mailbox UIDVALIDITY changed during rules run")
                state["mailboxes"][folder] = {"uidvalidity": validity, "uid": uid, "pending": False}
                self._save_state(state)

            if mark is not None and mark["uidvalidity"] != validity:
                result["status"] = "reset"
                self._event(folder, "reset", uidvalidity=validity)
                checkpoint(max(uids, default=0))
                return result
            if mark is None:
                # An explicit first opt-in drains the existing backlog across
                # bounded runs. Epoch resets still baseline conservatively.
                self._event(folder, "initialized", uidvalidity=validity)
                checkpoint(0)
                mark = state["mailboxes"][folder]
                result["status"] = "initialized"

            candidates = [uid for uid in uids if uid > mark["uid"]][:MAX_BATCH]
            if not candidates:
                return result
            planner = FolderRulesService(cleanup, self.rules_path, saved_path=self.saved_rules_path)

            def plan(remaining):
                # Unattended runs only move: a saved rule's mark_read never applies.
                groups, _ = planner._plan(
                    connection,
                    deadline,
                    rules,
                    source_folder,
                    source_uids={str(uid) for uid in remaining},
                    rule_mark_read=False,
                )
                cleanup.client._select(connection, source_folder)
                if _uidvalidity(connection) != validity:
                    raise MailError("Mailbox UIDVALIDITY changed during rules run")
                return {
                    int(row["uid"]): (rule.folder, row)
                    for rule, group in zip(rules, groups)
                    for row in group
                }

            moves = plan(candidates)
            last = mark["uid"]
            offset = 0
            while offset < len(candidates):
                uid = candidates[offset]
                if uid not in moves:
                    checkpoint(uid)
                    last = uid
                    offset += 1
                    result["skipped"] += 1
                    continue
                if result["count"] >= cap:
                    break
                target, _ = moves[uid]
                chunk = []
                while offset + len(chunk) < len(candidates):
                    next_uid = candidates[offset + len(chunk)]
                    entry = moves.get(next_uid)
                    if (
                        entry is None
                        or entry[0] != target
                        or len(chunk) >= min(EXECUTE_CHUNK_SIZE, cap - result["count"])
                    ):
                        break
                    chunk.append(entry[1])

                def pending():
                    # Called after the guarded writable SELECT/identity check.
                    # Do not change the selected mailbox before its MOVE.
                    state["mailboxes"][folder] = {
                        "uidvalidity": validity,
                        "uid": last,
                        "pending": True,
                    }
                    self._save_state(state)

                outcome = cleanup.backend.apply(
                    connection,
                    deadline,
                    "move_to_folder",
                    {"folder": target},
                    chunk,
                    before_mutation=pending,
                )
                result["log_ids"].append(outcome["log_id"])
                result["count"] += outcome["count"]
                if outcome["partial"]:
                    result["status"] = "partial"
                    break
                last = int(chunk[-1]["uid"])
                checkpoint(last)
                offset += len(chunk)
                if offset < len(candidates) and result["count"] < cap:
                    # As in manual rules, refresh membership protections after
                    # each chunk before authorizing any remaining source UID.
                    moves = plan(candidates[offset:])
        self._event(folder, result["status"], count=result["count"], log_ids=result["log_ids"])
        return result


def main():
    parser = argparse.ArgumentParser(description="Run one reviewed rules batch (opt-in)")
    parser.add_argument(
        "--folder",
        default="INBOX",
        help=(
            "Source mailbox: INBOX only, case-insensitive (default: INBOX); "
            "other names and conflicting special-use flags are refused"
        ),
    )
    args = parser.parse_args()
    try:
        require_rules_source(args.folder)
        # This CLI never starts a server or accepts an MCP token. Reuse the existing
        # Bridge/config validation without requiring an unused bearer secret.
        values = dict(os.environ)
        values["PROTON_MCP_TOKEN"] = secrets.token_urlsafe(32)
        settings = Settings.from_env(values)
        # Journal records name this operator-run CLI, not an MCP client.
        caller = {"name": "proton-mail-mcp-rules-run", "version": None, "transport": "cli"}
        with client_context(caller):
            result = RulesTimer(MailClient(settings).cleanup).run(folder=args.folder)
        print(json.dumps(result))
        raise SystemExit(1 if result["status"] == "partial" else 0)
    except Exception:
        print(
            "Rules run failed; no further moves; inspect the action journal and state",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
