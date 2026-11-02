"""Private append-only action and audit records, serialized across threads and processes."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import localfs
from .cleanup_lookup import display_row
from .mail import MailError, MailValidationError
from .run_journal import RUN_EVENTS, replay_runs, run_event

_PLAIN_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_ACTIONS = {
    "move_to_trash",
    "archive",
    "move_to_folder",
    "add_label",
    "remove_label",
    "mark_read",
    "mark_unread",
    "unsubscribe",
    "delete_draft",
    "create_folder",
    "create_sieve_filter",
    "add_rule",
}
_REINDEX_ACTIONS = {"reindex_preview", "reindex_execute", "reindex_cancel", "reindex_result"}
# Tools whose argument selects one exact action. Tokens, chunks and audit records
# keep naming the action; a lookup by the tool's name finds all of them.
MERGED_TOOLS = {
    "move": frozenset({"move_to_trash", "archive", "move_to_folder"}),
    "set_read_state": frozenset({"mark_read", "mark_unread"}),
    "label": frozenset({"add_label", "remove_label"}),
    "reindex": frozenset({"reindex_preview", "reindex_execute", "reindex_cancel"}),
}
_MERGED_ACTIONS = frozenset().union(*MERGED_TOOLS.values())
# Every tool that can change mail, drafts, folders, filters, rules, subscriptions
# or the local search index.
WRITE_TOOLS = frozenset(
    {
        *(_ACTIONS - _MERGED_ACTIONS),
        *MERGED_TOOLS,
        "apply_rules",
        "undo_action",
        "create_draft",
        "forward_message",
        "save_attachment",
        "send_draft",
    }
)
# The caller of the current MCP tool call or CLI run; set per call by the server.
_CLIENT: ContextVar[dict[str, Any] | None] = ContextVar("proton_mail_mcp_client", default=None)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
# Audit fields visible to list_actions; token IDs only filter, never display.
_AUDIT_FIELDS = ("tool", "outcome", "client", "action_hash", "counts")
# Shown only when the call recorded them: folder creation or local index run metadata.
_AUDIT_OPTIONAL_FIELDS = ("folder_created", "folder_creation_uncertain")
_REINDEX_AUDIT_FIELDS = ("run_id", "reindex_state")


class _LogBusy(MailError):
    """Another writer holds the journal lock."""


def replay_records(
    records: list[dict[str, Any]], *, include_runs: bool = False
) -> list[dict[str, Any]]:
    """Fold append-only execution updates; legacy and undo records stay intact."""
    result = []
    executions = {}
    for record in records:
        if record.get("action") in RUN_EVENTS:
            continue
        if record.get("action") not in {"execute_progress", "execute_result"}:
            # Do not modify the caller's records (including nested messages).
            record = dict(record)
            if isinstance(record.get("messages"), list):
                record["messages"] = [dict(row) for row in record["messages"]]
            result.append(record)
            if record.get("action") in _ACTIONS:
                if record.get("log_id") in executions:
                    raise MailError("Cleanup action log has duplicate IDs")
                executions[record["log_id"]] = record
            continue
        original = executions.get(record.get("action_log_id"))
        if original is None or original.get("status") != "in_progress":
            raise MailError("Cleanup action log has invalid execution progress")
        if record["action"] == "execute_progress":
            index, entry = record.get("index"), record.get("message")
            if (
                type(index) is not int
                or not 0 <= index < len(original["messages"])
                or not isinstance(entry, dict)
                or any(
                    entry.get(key) != original["messages"][index].get(key)
                    for key in (
                        "message_id",
                        "folder",
                        "source_folder",
                        "destination_folder",
                        "uid_before",
                        "uidvalidity_before",
                        "folders_before",
                        "memberships_before",
                        "message_id_digest",
                        "url_digest",
                        "method",
                        "size",
                        "internaldate",
                        "fingerprint",
                        "seen_before",
                        "destination_before",
                        "destination_uidvalidity",
                        "destination_uidnext",
                    )
                )
            ):
                raise MailError("Cleanup action log has invalid execution identity")
            original["messages"][index] = dict(entry)
        else:
            original["partial"] = record["partial"]
            original["status"] = record.get(
                "status", "partial" if record["partial"] else "completed"
            )
            if "error" in record:
                original["error"] = record["error"]
    if include_runs:
        return [*result, *replay_runs(records, list(executions.values()))]
    # Per-chunk recovery depends on that chunk's execution identity, not on
    # approval-wide metadata. Forward execution and summaries request both.
    return result


def header_digest(row: dict[str, Any]) -> str:
    """Persist header identity without putting sender or subject text in the log."""
    row = display_row(row)
    fields = {key: row.get(key) for key in ("from", "sender", "reply_to", "to", "subject", "date")}
    fields["sender_invalid"] = bool(row.get("sender_invalid"))
    payload = json.dumps(fields, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def default_log_path() -> Path:
    return localfs.state_home() / "actions.jsonl"


def new_record(action: str, params: dict[str, str]) -> dict[str, Any]:
    return {
        "log_id": secrets.token_hex(16),
        "timestamp": datetime.now(UTC).isoformat(),
        "action": action,
        "params": dict(params),
        "messages": [],
        "partial": False,
        "client": _CLIENT.get(),
    }


@contextmanager
def client_context(client: dict[str, Any] | None) -> Iterator[None]:
    """Attribute every record written during one tool call or CLI run to its caller."""
    reset = _CLIENT.set(client)
    try:
        yield
    finally:
        _CLIENT.reset(reset)


def token_digest(token: str) -> str:
    """Use the same full preview-token digest for records and lookups."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def token_id(token: Any) -> str | None:
    """Identify an issued preview token by a short digest prefix, never its value."""
    if isinstance(token, str) and _TOKEN.fullmatch(token):
        return hashlib.sha256(token.encode("ascii")).hexdigest()[:16]
    return None


def audit_record(
    tool: str, outcome: str, *, audit_id: str, token: Any = None, **fields: Any
) -> dict[str, Any]:
    """One uniform write-call record: who, what, when, outcome; no content or secrets."""
    return {
        "action": "audit",
        "audit_id": audit_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "client": _CLIENT.get(),
        "tool": tool,
        "outcome": outcome,
        "token_id": token_id(token),
        **fields,
    }


class LogTransaction:
    def __init__(self, descriptor: int | None):
        self.descriptor = descriptor
        self._tail: bytes | None = None
        self._failed = False

    def records(self, *, include_runs: bool = False) -> list[dict[str, Any]]:
        if self.descriptor is None:
            return []
        self._tail = None
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        # A duplicated descriptor preserves the locked original and does not
        # change its O_APPEND semantics when the read offset reaches EOF.
        with os.fdopen(os.dup(self.descriptor), "rb") as stream:
            try:
                result = []
                tail = b""
                for line in stream:
                    if not line.endswith(b"\n"):
                        # Only an unterminated final append can be discarded.
                        tail = line
                        break
                    if b"\0" in line:
                        fragment, digest = line[:-1].rsplit(b"\0", 1)
                        if digest != hashlib.sha256(fragment).hexdigest().encode("ascii"):
                            raise ValueError("Invalid interrupted-append seal")
                        continue
                    result.append(json.loads(line))
            except (ValueError, UnicodeError):
                raise MailError("Cleanup action log is invalid; refusing changes") from None
        if any(not isinstance(record, dict) for record in result):
            raise MailError("Cleanup action log is invalid; refusing changes")
        result = replay_records(result, include_runs=include_runs)
        self._tail = tail
        return result

    def append(self, record: dict[str, Any]) -> None:
        if self.descriptor is None or self._failed:
            raise MailError("Cleanup action log is unavailable")
        payload = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
        if self._tail is None:
            self.records()
        try:
            if self._tail:
                # Preserve the torn bytes, sealing just this validated tail.
                # The checksum distinguishes a sealed fragment from corruption
                # in complete records. A torn seal is itself still a final tail.
                seal = b"\0" + hashlib.sha256(self._tail).hexdigest().encode("ascii") + b"\n"
                self._write(seal)
                self._tail = b""
            self._write(payload)
        except BaseException:
            # Do not let finally blocks append outcomes after a failed intent.
            self._failed = True
            raise

    def _write(self, payload: bytes) -> None:
        # One complete frame per write under the exclusive lock. Never complete
        # a short write: its bytes are an uncommitted tail, even if valid JSON.
        if os.write(self.descriptor, payload) != len(payload):
            raise MailError("Cleanup action log could not be appended")
        os.fsync(self.descriptor)


class ActionLog:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_log_path()

    def ensure_run(self, transaction: LogTransaction, context: dict[str, Any]) -> None:
        """Persist approval before a mutation, under the chunk's existing lock."""
        if context.get("_call_started"):
            return
        runs = transaction.records(include_runs=True)
        previous = next(
            (
                row
                for row in runs
                if row.get("action") == "execute_run" and row["run_id"] == context["run_id"]
            ),
            None,
        )
        if previous is None:
            transaction.append(
                run_event(
                    "run_start",
                    context["run_id"],
                    run_action=context["action"],
                    token_digest=context["token_digest"],
                    approved_total=context["approved_total"],
                    log_ids=[
                        row["log_id"] for row in runs if row.get("run_id") == context["run_id"]
                    ],
                    done=0,
                    uncertain=0,
                    remaining=context["approved_total"],
                    status="in_progress",
                    partial=True,
                    client=_CLIENT.get(),
                )
            )
        else:
            if (
                any(previous[key] != context[key] for key in ("token_digest", "approved_total"))
                or previous["run_action"] != context["action"]
            ):
                raise MailError("Cleanup run approval changed; refusing changes")
            transaction.append(run_event("run_resume", context["run_id"], client=_CLIENT.get()))
        context["_call_started"] = True

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.transaction(readonly=True) as transaction:
            records = transaction.records(include_runs=True)
        return next(
            (
                row
                for row in records
                if row.get("action") == "execute_run" and row["run_id"] == run_id
            ),
            None,
        )

    def update_run(self, transaction: LogTransaction, run_id: str) -> dict[str, Any]:
        """Journal a checkpoint without closing the current confirmation call."""
        return self._record_run_state(transaction, run_id, "run_progress")

    def _record_run_state(
        self, transaction: LogTransaction, run_id: str, event: str, **fields: Any
    ) -> dict[str, Any]:
        records = transaction.records(include_runs=True)
        run = next(
            (
                row
                for row in records
                if row.get("action") == "execute_run" and row["run_id"] == run_id
            ),
            None,
        )
        if run is None:
            raise MailError("Cleanup run journal has missing start")
        state = {key: run[key] for key in ("done", "uncertain", "remaining", "log_ids")}
        state["remaining_unchanged"] = run["remaining"]
        completed = run["done"] == run["approved_total"] and not any(
            row.get("partial")
            for row in records
            if row.get("action") != "execute_run" and row.get("run_id") == run_id
        )
        state["status"] = (
            ("completed" if completed else "partial") if event == "run_end" else "in_progress"
        )
        state["partial"] = not completed or event != "run_end"
        transaction.append(run_event(event, run_id, **state, **fields, client=_CLIENT.get()))
        return run

    def finish_run(
        self, run_id: str, *, stop_reason: str | None = None, blocked: int = 0
    ) -> dict[str, Any]:
        with self.transaction() as transaction:
            self._record_run_state(
                transaction, run_id, "run_end", stop_reason=stop_reason, blocked=blocked
            )
            return next(
                row
                for row in transaction.records(include_runs=True)
                if row.get("action") == "execute_run" and row["run_id"] == run_id
            )

    @contextmanager
    def transaction(self, *, readonly: bool = False) -> Iterator[LogTransaction]:
        descriptor = None
        try:
            if not readonly:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            refusal = localfs.parent_refusal(self.path)
            if refusal:
                raise MailError(f"Cleanup action log {refusal}")
            flags = (
                os.O_RDONLY | localfs.O_NONBLOCK
                if readonly
                else os.O_RDWR | os.O_APPEND | os.O_CREAT
            )
            try:
                descriptor = localfs.open(
                    self.path, flags | localfs.O_NOFOLLOW | localfs.O_CLOEXEC, 0o600
                )
            except FileNotFoundError:
                if not readonly:
                    raise
                yield LogTransaction(None)
                return
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or not localfs.owned(info) or info.st_nlink != 1:
                raise MailError("Cleanup action log must be a private regular file")
            refusal = localfs.acl_refusal(descriptor)
            if refusal:
                raise MailError(f"Cleanup action log {refusal}")
            if not readonly:
                localfs.fchmod(descriptor, 0o600)
            try:
                # Refuse a concurrent operation rather than waiting outside its
                # mailbox deadline. A lock spans validation, changes, and append.
                localfs.lock(descriptor, exclusive=not readonly)
            except BlockingIOError:
                raise _LogBusy("Cleanup action log is busy; retry the operation") from None
            yield LogTransaction(descriptor)
        except OSError:
            raise MailError("Cleanup action log is unavailable") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def audit(self, record: dict[str, Any], *, wait: float = 5.0) -> None:
        """Append one audit record, briefly waiting out another writer's lock."""
        deadline = time.monotonic() + wait
        while True:
            try:
                with self.transaction() as transaction:
                    transaction.append(record)
                return
            except _LogBusy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def list_actions(
        self,
        token: str | None = None,
        since: str | None = None,
        before: str | None = None,
        limit: int = 100,
        action: str | None = None,
        client: str | None = None,
    ) -> dict[str, Any]:
        """Find recovery IDs, action previews and audited writes without reading mail.

        action filters by tool (what) and client by MCP client name (who). A plain
        YYYY-MM-DD means that whole day in the server's local timezone, in either
        bound: since starts when it starts and before ends when it ends.
        """

        def instant(value, *, day_end=False):
            try:
                if isinstance(value, str) and _PLAIN_DATE.fullmatch(value):
                    day = date.fromisoformat(value) + timedelta(days=int(day_end))
                    return datetime(day.year, day.month, day.day).astimezone()
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is not None:
                    return parsed
            except (TypeError, ValueError, OverflowError, OSError):
                pass
            raise MailValidationError(
                "Action lookup requires YYYY-MM-DD dates or timezone-aware ISO timestamps"
            )

        if type(limit) is not int or not 1 <= limit <= 200:
            raise MailValidationError("Action lookup limit must be between 1 and 200")
        start = instant(since) if since is not None else None
        end = instant(before, day_end=True) if before is not None else None
        if start is not None and end is not None and start >= end:
            raise MailValidationError("Action lookup since must precede before")
        if action is not None and action not in {*WRITE_TOOLS, *_MERGED_ACTIONS, "preview_action"}:
            raise MailValidationError("Action lookup action must name a write or preview tool")
        wanted = {action, *MERGED_TOOLS.get(action, ())}
        if client is not None and (
            not isinstance(client, str)
            or not 1 <= len(client) <= 100
            or any(ord(char) < 32 or ord(char) == 127 for char in client)
        ):
            raise MailValidationError("Action lookup client must be a short client name")

        def who(record):
            caller = record.get("client")
            return caller.get("name") if isinstance(caller, dict) else None

        digest = None
        if token is not None:
            if not isinstance(token, str) or not token.isascii() or not 1 <= len(token) <= 256:
                raise MailValidationError("Invalid action lookup token")
            digest = token_digest(token)
        with self.transaction(readonly=True) as transaction:
            records = transaction.records(include_runs=True)
        runs = {row["run_id"]: row for row in records if row.get("action") == "execute_run"}
        matched_runs = {
            row["run_id"]
            for row in records
            if row.get("run_id") in runs
            and digest is not None
            and hmac.compare_digest(row.get("token_digest") or "", digest)
        }
        matched_reindex_runs = {
            row["run_id"]
            for row in records
            if row.get("action") in _REINDEX_ACTIONS
            and row.get("run_id")
            and digest is not None
            and hmac.compare_digest(row.get("token_digest") or "", digest)
        }
        found = []
        matched_chunk_runs = set()
        for record in reversed(records):
            preview_action = (
                record["params"].get("action") if isinstance(record.get("params"), dict) else None
            )
            preview = record.get("action") == "preview_action" and preview_action in {
                "unsubscribe",
                "move_to_trash",
                "archive",
                "move_to_folder",
                "delete_draft",
            }
            reindex = record.get("action") in _REINDEX_ACTIONS
            if record.get("action") not in _ACTIONS and not preview and not reindex:
                continue
            # A move approval's folder creation belongs to its run, but is no chunk.
            chunk = "run_id" in record
            run_id = record.get("run_id") if chunk else record.get("parent_run_id")
            if (
                digest is not None
                and not hmac.compare_digest(record.get("token_digest") or "", digest)
                and run_id not in matched_runs
                and not (reindex and run_id in matched_reindex_runs)
            ):
                continue
            timestamp = instant(record["timestamp"])
            if (start is not None and timestamp < start) or (end is not None and timestamp >= end):
                continue
            run = runs.get(run_id)
            # Rules chunks are folder moves; their run names the approved tool.
            if action is not None and wanted.isdisjoint(
                {
                    record["action"],
                    preview_action if preview else None,
                    "reindex_execute" if record["action"] == "reindex_result" else None,
                    run and run["run_action"],
                }
            ):
                continue
            if client is not None and who(record) != client:
                continue
            if reindex:
                # Index records cannot undo mail and expose only safe metadata;
                # never return token digests, approval params, or backend errors.
                found.append(
                    {
                        key: record[key]
                        for key in (
                            "log_id",
                            "timestamp",
                            "action",
                            "run_id",
                            "state",
                            "indexed_count",
                            "all_mail_count",
                            "missing_count",
                            "eta_seconds",
                            "old_count",
                            "new_count",
                            "percent_done",
                            "action_log_id",
                            "swap_outcome",
                            "swap_reason",
                            "cancel_requested",
                        )
                        if key in record
                    }
                    | {"undoable": False}
                )
                continue
            if preview:
                found.append(
                    {
                        key: record[key]
                        for key in (
                            "log_id",
                            "timestamp",
                            "action",
                            "status",
                            "count",
                            "blocked_count",
                            "excluded_count",
                            "partial",
                            "total_is_exact",
                            "action_hash",
                        )
                    }
                    | {
                        "preview_action": preview_action,
                        "done": 0,
                        "uncertain": 0,
                        "not_attempted": 0,
                    }
                )
                continue
            messages = record["messages"]
            done = sum(row["status"] == "done" for row in messages)
            untouched = sum(row["status"] == "not_attempted" for row in messages)
            pending_send = sum(row["status"] == "pending_send" for row in messages)
            status = record.get("status", "partial" if record["partial"] else "completed")
            if run is not None:
                matched_chunk_runs.add(run["run_id"])
            # Keep an unfinished chunk's existing recovery status. A completed
            # chunk must not make an incomplete approval look successful.
            if chunk and run is not None and run["partial"] and status == "completed":
                status = "partial"
            found.append(
                {
                    "log_id": record["log_id"],
                    "timestamp": record["timestamp"],
                    "action": record["action"],
                    "status": status,
                    "count": len(messages),
                    "done": done,
                    "not_attempted": untouched,
                    "uncertain": len(messages) - done - untouched - pending_send,
                    **(
                        {
                            "pending_send": pending_send,
                            "draft_ids": [
                                row["draft_id"]
                                for row in messages
                                if row["status"] == "pending_send"
                                and isinstance(row.get("draft_id"), str)
                            ],
                        }
                        if pending_send
                        else {}
                    ),
                    "partial": status not in {"completed", "pending_send"},
                }
            )
        result = {"actions": found[:limit], "truncated": len(found) > limit}
        if runs:
            visible_runs = []
            for run in reversed(list(runs.values())):
                if digest is not None and run["run_id"] not in matched_runs:
                    continue
                if (action is not None and run["run_action"] not in wanted) or (
                    client is not None and who(run) != client
                ):
                    continue
                timestamp = instant(run["timestamp"])
                # Preserve whole-approval context for a matching chunk even
                # when its run start falls just outside the requested window.
                if run["run_id"] not in matched_chunk_runs and (
                    (start is not None and timestamp < start)
                    or (end is not None and timestamp >= end)
                ):
                    continue
                visible_runs.append(
                    {
                        key: run[key]
                        for key in (
                            "run_id",
                            "timestamp",
                            "approved_total",
                            "status",
                            "done",
                            "uncertain",
                            "remaining",
                            "remaining_unchanged",
                            "blocked",
                            "partial",
                            "log_ids",
                        )
                    }
                    | {"action": run["run_action"], "stop_reason": run.get("stop_reason")}
                )
            result["runs"] = visible_runs[:limit]
            result["runs_truncated"] = len(visible_runs) > limit
        # Fold each call's started record and its outcome; a call without an
        # outcome is still running or was interrupted and reads as "started".
        audits: dict[str, dict[str, Any]] = {}
        for record in records:
            if record.get("action") == "audit" and isinstance(record.get("audit_id"), str):
                entry = audits.setdefault(record["audit_id"], {"timestamp": record["timestamp"]})
                entry.update({key: record.get(key) for key in (*_AUDIT_FIELDS, "token_id")})
                optional = _AUDIT_OPTIONAL_FIELDS
                if record.get("tool") in _REINDEX_ACTIONS:
                    optional += _REINDEX_AUDIT_FIELDS
                entry.update({key: record[key] for key in optional if key in record})
        if audits:
            visible_audits = []
            for audit_id, entry in reversed(list(audits.items())):
                timestamp = instant(entry["timestamp"])
                if (
                    (start is not None and timestamp < start)
                    or (end is not None and timestamp >= end)
                    or (
                        digest is not None
                        and not hmac.compare_digest(entry["token_id"] or "", digest[:16])
                    )
                    or (action is not None and entry["tool"] not in wanted)
                    or (client is not None and who(entry) != client)
                ):
                    continue
                visible_audits.append(
                    {"audit_id": audit_id, "timestamp": entry["timestamp"]}
                    | {key: entry[key] for key in _AUDIT_FIELDS}
                    | {
                        key: entry[key]
                        for key in (*_AUDIT_OPTIONAL_FIELDS, *_REINDEX_AUDIT_FIELDS)
                        if key in entry
                    }
                )
            result["audit"] = visible_audits[:limit]
            result["audit_truncated"] = len(visible_audits) > limit
        return result
