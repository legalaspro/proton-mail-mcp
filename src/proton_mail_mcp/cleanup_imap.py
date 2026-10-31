"""Guarded moves, labels, read-state changes and undo over Bridge IMAP.

Moves require UID MOVE. Moves and read-state changes send one UID STORE and/or
one UID MOVE per source folder for a verified chunk of up to 200 UIDs, then
verify each UID's outcome. Label removal uses scoped expunge: Bridge
represents labels as virtual mailboxes, so deleting exactly the selected label
UID removes that membership while preserving the message in All Mail. UIDPLUS
is mandatory and unscoped EXPUNGE is never issued. Drafts-only permanent
deletion is implemented separately in delete_draft.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from typing import Any, Callable

from . import mail
from .action_log import ActionLog, LogTransaction, header_digest, new_record
from .cleanup_folders import _move_folders
from .cleanup_lookup import (
    _IncompleteIdentityInventory,
    envelope_row,
    fetch_rows,
    folder_inventory,
    identity_context,
    matching_rows,
    message_fingerprint,
)
from .cleanup_undo import remaining_undo
from .mail import (
    MailError,
    MailValidationError,
    _checked,
    _mailbox,
    _message_id,
)
from .rules_source import require_read_state_source
from .search import _uidvalidity

MAX_BATCH = 200
_SEEN_ACTIONS = {"mark_read", "mark_unread"}
_ACTIONS = {
    "move_to_trash",
    "archive",
    "move_to_folder",
    "add_label",
    "remove_label",
} | _SEEN_ACTIONS
_MOVE_ACTIONS = {"move_to_trash", "archive", "move_to_folder"}
# One UID STORE and/or UID MOVE per source folder and chunk; labels stay per message.
_BATCHED_ACTIONS = _MOVE_ACTIONS | _SEEN_ACTIONS
_COPYUID = re.compile(rb"\bCOPYUID\s+([1-9]\d*)\s+([0-9:,]+)\s+([0-9:,]+)(?=[\s\]]|$)")
_UNDO_BUDGET_ERROR = (
    "Undo time budget reached; call undo_action again to restore the remaining messages"
)


class _UndoVerificationError(MailValidationError):
    """An undo identity stopped matching before its first mutation."""


def _handle_identity(row: dict[str, Any]) -> tuple[str, str, str, str]:
    """Bind a logical copy to its current, strictly folder-local IMAP handle."""
    return tuple(str(row[key]) for key in ("message_id", "folder", "uidvalidity", "uid"))


def _capabilities(connection: Any) -> set[str]:
    return {
        item.decode("ascii").upper() if isinstance(item, bytes) else item.upper()
        for item in getattr(connection, "capabilities", ())
    }


def _copyuid(connection: Any, response: Any, uid: str) -> tuple[str | None, str | None]:
    pieces = [item for item in response if isinstance(item, bytes)]
    # Servers may place COPYUID in tagged OK or an untagged OK response. imaplib
    # extracts the latter into response(), which performs no network I/O.
    _, extra = connection.response("COPYUID")
    if isinstance(extra, (tuple, list)):
        pieces.extend(b"COPYUID " + item for item in extra if isinstance(item, bytes))
    for piece in pieces:
        match = re.search(rb"\bCOPYUID\s+([1-9]\d*)\s+([0-9:,]+)\s+([0-9:,]+)(?=[\s\]]|$)", piece)
        if not match:
            continue
        # Each command changes one UID. A singleton may legally be expressed
        # as n:n, but a multi-UID mapping must never be guessed or expanded.
        source = re.fullmatch(rb"([1-9]\d*)(?::\1)?", match[2])
        destination = re.fullmatch(rb"([1-9]\d*)(?::\1)?", match[3])
        if source and destination and source[1].decode() == uid:
            return match[1].decode(), destination[1].decode()
    return None, None


def copy_identity(row: dict) -> tuple:
    """Evidence that pins a moved copy to its new UID; identical copies share it."""
    return (
        row["message_id"],
        message_fingerprint(row),
        header_digest(row),
        row.get("internaldate"),
    )


def command_group_counts(action: str, rows: list[dict]) -> list[int]:
    """Batched command groups needed by each prefix of rows (element n-1: n rows).

    One group per source folder; a move sends identical copies in successive
    groups, so that every moved UID can be pinned even without COPYUID.
    """
    counts, waves, copies = [], {}, Counter()
    for row in rows:
        if action in _MOVE_ACTIONS:
            key = (row["folder"], copy_identity(row))
            copies[key] += 1
            waves[row["folder"]] = max(waves.get(row["folder"], 0), copies[key])
        else:
            waves[row["folder"]] = 1
        counts.append(sum(waves.values()))
    return counts


def _uid_set(uids: list[str]) -> str:
    """Name exactly these UIDs as compact ascending ranges, never with "*"."""
    if not uids or any(not re.fullmatch(r"[1-9][0-9]*", uid) for uid in uids):
        raise MailValidationError("Cleanup UID sets require positive numeric UIDs")
    values = sorted({int(uid) for uid in uids})
    ranges = []
    start = previous = values[0]
    for value in [*values[1:], None]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f"{start}:{previous}")
        start = previous = value
    return ",".join(ranges)


def _uid_values(text: bytes, limit: int) -> list[str] | None:
    """Expand one COPYUID uid-set in its stated order; None for junk, "*" or excess."""
    values: list[str] = []
    for part in text.split(b","):
        match = re.fullmatch(rb"([1-9][0-9]{0,9})(?::([1-9][0-9]{0,9}))?", part)
        if match is None:
            return None
        first, last = int(match[1]), int(match[2] or match[1])
        low, high = min(first, last), max(first, last)
        if high > 4294967295 or len(values) + high - low + 1 > limit:
            return None
        values.extend(str(value) for value in range(low, high + 1))
    return values


def _copyuid_map(connection: Any, response: Any, uids: list[str]) -> dict[str, tuple[str, str]]:
    """Map moved source UIDs to (UIDVALIDITY, destination UID) from COPYUID.

    RFC 4315 lists destination UIDs in the order of the source UIDs. Only a code
    whose equal-length sets name a subset of this command's UIDs counts; any
    conflicting, duplicated or malformed evidence leaves those UIDs unmapped.
    """
    pieces = [item for item in response if isinstance(item, bytes)]
    _, extra = connection.response("COPYUID")
    if isinstance(extra, (tuple, list)):
        pieces.extend(b"COPYUID " + item for item in extra if isinstance(item, bytes))
    wanted = set(uids)
    found: dict[str, set[tuple[str, str]]] = {}
    for piece in pieces:
        for match in _COPYUID.finditer(piece):
            sources = _uid_values(match[2], len(uids))
            destinations = _uid_values(match[3], len(uids))
            if (
                sources is None
                or destinations is None
                or len(sources) != len(destinations)
                or len(set(sources)) != len(sources)
                or len(set(destinations)) != len(destinations)
                or not set(sources) <= wanted
            ):
                continue
            for source, destination in zip(sources, destinations):
                found.setdefault(source, set()).add((match[1].decode(), destination))
    mapping = {source: next(iter(values)) for source, values in found.items() if len(values) == 1}
    # Two source messages can never share one destination copy.
    shared = Counter(mapping.values())
    return {source: value for source, value in mapping.items() if shared[value] == 1}


class CleanupMutator:
    def __init__(self, client: Any, settings: Any, policy: Any):
        self.client, self.policy = client, policy
        self.log = ActionLog(settings.action_log_path)

    def validate_target(
        self, action: str, params: dict[str, Any], connection: Any, *, missing_ok: bool = False
    ) -> str:
        """Return the existing target; missing_ok admits an approved folder to create."""
        if action not in _ACTIONS:
            raise MailValidationError("Unsupported cleanup action")
        if not isinstance(params, dict):
            raise MailValidationError("Cleanup params must be an object")
        if action in _SEEN_ACTIONS:
            if params:
                raise MailValidationError("This cleanup action accepts no params")
            return ""
        if action in {"move_to_trash", "archive"}:
            if params:
                raise MailValidationError("This cleanup action accepts no params")
            target = "Trash" if action == "move_to_trash" else "Archive"
        else:
            field = "folder" if action == "move_to_folder" else "label"
            allowed = (
                {field, "mark_read", "create_folder_if_missing"}
                if action == "move_to_folder"
                else {field}
            )
            if (
                field not in params
                or set(params) - allowed
                or not isinstance(params[field], str)
                or ("mark_read" in params and type(params["mark_read"]) is not bool)
                # Bound only when the approval itself includes creating the folder.
                or (
                    "create_folder_if_missing" in params
                    and params["create_folder_if_missing"] is not True
                )
            ):
                raise MailValidationError("Cleanup params do not match the action")
            target = params[field]
            prefix = "Folders/" if field == "folder" else "Labels/"
            if field == "label" and not target.startswith(prefix):
                target = prefix + target
            if not target.startswith(prefix) or not target[len(prefix) :]:
                raise MailValidationError("Cleanup target must be an existing folder or label")
            if any(part in {"", ".", ".."} for part in target.split("/")):
                raise MailValidationError("Cleanup target contains an invalid path component")
        _mailbox(target)
        if target not in self.client._folders(connection):
            if missing_ok and params.get("create_folder_if_missing") is True:
                return target
            raise MailValidationError("Cleanup target folder or label does not exist")
        return target

    def _fetch(self, connection: Any, deadline: Any, folder: str, uids: list[str]) -> list[dict]:
        self.client._select(connection, folder)
        validity = _uidvalidity(connection)
        result = fetch_rows(
            self.client, connection, deadline, folder, uids, validity, complete=False
        )
        for row in result:
            row["folders"] = [folder]
        return result

    def _locate(self, connection: Any, deadline: Any, folder: str, message_id: str) -> list[dict]:
        matches = matching_rows(
            folder_inventory(
                self.client, connection, deadline, folder, stage="undo", identifiers=[message_id]
            ),
            [message_id],
            stage="undo",
            folder=folder,
        )
        if len(matches) > MAX_BATCH:
            raise MailValidationError(
                "Message-ID lookup exceeds the 200 message safety cap during "
                + identity_context("undo", folder)
            )
        return matches

    def _logged_uid(
        self,
        connection: Any,
        deadline: Any,
        folder: str,
        message_id: str,
        uid: Any,
        validity: Any,
    ) -> tuple[dict | None, str | None]:
        if not all(
            isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value)
            for value in (uid, validity)
        ):
            return None, "invalid logged UID identity"
        # A failed read cannot establish an identity mismatch. Let preparation
        # abort before claiming this undo attempt so a transient failure is retryable.
        self.client._select(connection, folder)
        if _uidvalidity(connection) != validity:
            return None, "UIDVALIDITY changed"
        rows = fetch_rows(
            self.client, connection, deadline, folder, [uid], validity, complete=False
        )
        if not rows:
            return None, "not found"
        row = rows[0]
        row["folders"] = [folder]
        if row.get("identity_invalid"):
            raise MailValidationError(
                "Protected or ambiguous message identity during "
                + identity_context("undo", folder)
                + "; no messages changed"
            )
        if row["message_id"] != message_id:
            return None, "Message-ID changed"
        return row, None

    def _undo_lookup(
        self,
        connection: Any,
        deadline: Any,
        folder: str,
        previous: dict,
        *,
        folders: list[str] | None = None,
    ) -> tuple[dict | None, str | None]:
        uid, validity = previous.get("uid_after"), previous.get("uidvalidity_after")
        retry = previous.get("undo_retry_handle")
        if retry and retry["source_folder"] == folder:
            uid, validity = retry["uid_before"], retry["uidvalidity_before"]
        if uid is not None or validity is not None:
            # Even incomplete/malformed logged handles must never fall back
            # to SEARCH: a different UID cannot satisfy an approved handle.
            row, reason = self._logged_uid(
                connection, deadline, folder, previous["message_id"], uid, validity
            )
            if row is not None:
                self._check_protected([row], stage="undo")
            if row is not None and "fingerprint" in previous:
                if list(message_fingerprint(row)) != previous["fingerprint"]:
                    return None, "message fingerprint changed"
            return row, reason
        if "destination_before" in previous and previous.get("status") != "uncertain":
            return None, "destination UID mapping unavailable; manual handling required"
        row, reason = self._unconfirmed_undo(connection, deadline, folder, previous, folders)
        if row is not None and "destination_before" in previous:
            return None, "destination UID mapping unavailable; manual handling required"
        boundary = previous.get("destination_uidnext")
        if (
            row is not None
            and boundary is not None
            and (
                row["uidvalidity"] != previous.get("destination_uidvalidity")
                or int(row["uid"]) < int(boundary)
            )
        ):
            return None, "destination identity predates the move; manual handling required"
        return row, reason + "; manual handling required" if reason else None

    def _unconfirmed_undo(
        self,
        connection: Any,
        deadline: Any,
        folder: str,
        previous: dict,
        folders: list[str] | None,
    ) -> tuple[dict | None, str | None]:
        """Count physical identities account-wide for MOVE; keep label lookups scoped."""
        digest = previous.get("header_digest")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return None, "no logged UID identity or header digest"
        if (
            type(previous.get("size")) is not int
            or previous["size"] < 0
            or type(previous.get("internaldate")) not in (int, float)
        ):
            return None, "no logged message size or internal date"
        identity = {key: previous[key] for key in ("size", "internaldate")}
        matches = []
        for name in [folder] if folders is None else folders:
            context = identity_context("undo", name)
            try:
                rows = self._locate(connection, deadline, name, previous["message_id"])
            except _IncompleteIdentityInventory:
                return None, "incomplete message fetch during " + context
            if any(row.get("identity_invalid") for row in rows):
                return None, "ambiguous identity during " + context
            if folders is not None:
                rows = [
                    row
                    for row in rows
                    if header_digest(row) == digest
                    and all(row.get(key) == value for key, value in identity.items())
                ]
                # Read views too: duplicate identities there are ambiguous, but
                # one membership is not another physical copy. Unknown folders count.
                if name.startswith("Labels/") or name in {"All Mail", "Starred", "Scheduled"}:
                    if len(rows) > 1:
                        return None, "ambiguous virtual view during " + context
                    continue
            matches.extend(rows)
        if len(matches) != 1:
            if folders is None:
                return None, "not found" if not matches else "ambiguous"
            return None, f"found {len(matches)} matching physical copies"
        if folders is not None and (folder != "Trash" or matches[0]["folder"] != "Trash"):
            return None, "matching physical copy is outside the required Trash destination"
        if header_digest(matches[0]) != digest:
            return None, "header digest changed"
        if any(matches[0].get(key) != value for key, value in identity.items()):
            return None, "message size or internal date changed"
        matches[0]["undo_header_digest"] = digest
        matches[0]["undo_identity"] = identity
        return matches[0], None

    def _undo_memberships(
        self, connection: Any, deadline: Any, previous: dict, row: dict, folders: list[str]
    ) -> tuple[list[dict], str | None, str | None]:
        # Each retained (Message-ID, folder) copy is bound to its logged handle.
        # A fresh Message-ID search could include an unrelated colliding message.
        copies = [row]
        for membership in previous.get("memberships_before", []):
            folder = membership["folder"]
            if membership.get("message_id", previous["message_id"]) != previous["message_id"]:
                return [], "logged membership identity changed", folder
            if (
                folder == row["folder"]
                or folder not in folders
                or (
                    folder == previous["source_folder"]
                    and membership.get("uid") != previous.get("uid_before")
                )
            ):
                continue
            copy, reason = self._logged_uid(
                connection,
                deadline,
                folder,
                previous["message_id"],
                membership.get("uid"),
                membership.get("uidvalidity"),
            )
            if reason == "not found":
                continue  # MOVE or label removal may have removed this copy.
            if reason:
                return [], reason, folder
            copies.append(copy)
        return copies, None, None

    def _check_protected(self, rows: list[dict], *, stage: str = "preflight") -> None:
        for row in rows:
            if self.policy.check(row) or row.get("identity_invalid"):
                raise MailValidationError(
                    "Protected sender or ambiguous identity during "
                    + identity_context(stage, row["folder"])
                    + "; no messages changed"
                )

    def _verify(self, connection: Any, deadline: Any, rows: list[dict]) -> None:
        for folder in dict.fromkeys(row["folder"] for row in rows):
            requested = [row for row in rows if row["folder"] == folder]
            fetched = self._fetch(connection, deadline, folder, [row["uid"] for row in requested])
            if sorted(map(_handle_identity, requested)) != sorted(map(_handle_identity, fetched)):
                raise MailValidationError(
                    "Message set changed since preview during "
                    + identity_context("preflight", folder)
                    + "; no messages changed"
                )
            self._check_protected(fetched)
            originals = {row["uid"]: row for row in requested}
            if any(
                message_fingerprint(row) != message_fingerprint(originals[row["uid"]])
                or (
                    "seen" in originals[row["uid"]]
                    and row.get("seen") != originals[row["uid"]]["seen"]
                )
                for row in fetched
            ):
                raise MailValidationError(
                    "Message fingerprint changed since preview during "
                    + identity_context("preflight", folder)
                    + "; no messages changed"
                )

    def _preflight(
        self, connection: Any, deadline: Any, action: str, target: str, rows: list[dict]
    ) -> None:
        if not rows or len(rows) > MAX_BATCH:
            raise MailValidationError("Cleanup batches must contain 1 to 200 messages")
        if len({_handle_identity(row) for row in rows}) != len(rows):
            raise MailValidationError("Cleanup batch contains duplicate messages")
        if getattr(connection, "_cleanup_seen", False) and any(
            type(row.get("seen")) is not bool for row in rows
        ):
            raise MailValidationError("Seen changes require a complete prior flag snapshot")
        if getattr(connection, "_cleanup_seen", False):
            for folder in dict.fromkeys(row["folder"] for row in rows):
                require_read_state_source(folder, connection)
        capabilities = _capabilities(connection)
        if action in _MOVE_ACTIONS and "MOVE" not in capabilities:
            raise MailValidationError("Server lacks MOVE capability; refusing unsafe move fallback")
        if action == "remove_label" and "UIDPLUS" not in capabilities:
            raise MailValidationError("Label removal requires UIDPLUS for scoped UID EXPUNGE")
        move_folders = (
            _move_folders(connection) if action in _MOVE_ACTIONS | _SEEN_ACTIONS else set()
        )
        surviving: dict[str, list[dict]] = {}
        all_mail: list[dict] = []
        if action == "remove_label":
            all_mail = matching_rows(
                folder_inventory(
                    self.client,
                    connection,
                    deadline,
                    "All Mail",
                    stage="preflight",
                    identifiers=[row["message_id"] for row in rows],
                ),
                [row["message_id"] for row in rows],
                stage="preflight",
                folder="All Mail",
            )
            for copy in all_mail:
                surviving.setdefault(copy["message_id"], []).append(copy)
        for row in rows:
            _message_id(row["message_id"])
            if action in _SEEN_ACTIONS and row["folder"] not in move_folders:
                raise MailValidationError("Seen previews must use the message's real folder")
            if action in _MOVE_ACTIONS:
                if row["folder"] not in move_folders:
                    raise MailValidationError("Move previews must use the message's real folder")
                if row["folder"] == target:
                    raise MailValidationError("Message is already in the cleanup target")
            if action == "add_label" and target in row.get("folders", []):
                raise MailValidationError("Message already has the requested label")
            if action == "remove_label":
                if row["folder"] != target or not target.startswith("Labels/"):
                    raise MailValidationError("Label removal must select exactly its label mailbox")
                matches = surviving.get(row["message_id"], [])
                if len(matches) != 1:
                    raise MailValidationError("Label removal requires one surviving All Mail copy")
                self._check_protected(matches)
        # Reuse the complete inventory to identify recovery handles, then
        # refresh only those handles before relying on their continued survival.
        verification_started = mail.time.monotonic()
        self._verify(connection, deadline, [*rows, *all_mail])
        # The first per-UID check repeats SELECT/FETCH after the chunk is frozen.
        # Its reserve uses actual bounded I/O, excluding earlier discovery.
        deadline.cleanup_claim_verification_seconds = mail.time.monotonic() - verification_started
        # Check every source's writable selection before the first mutation;
        # UIDVALIDITY may change specifically when switching from EXAMINE.
        for folder in dict.fromkeys(row["folder"] for row in rows):
            _checked(connection.select(_mailbox(folder), readonly=False))
            expected = {row["uidvalidity"] for row in rows if row["folder"] == folder}
            if expected != {_uidvalidity(connection)}:
                raise MailValidationError("Mailbox UIDVALIDITY changed before mutation")

    @staticmethod
    def _row_record(row: dict, destination: str) -> dict:
        return {
            "message_id": row["message_id"],
            "folder": row["folder"],
            "source_folder": row["folder"],
            "folders_before": list(row.get("folders", [row["folder"]])),
            "memberships_before": [
                {"message_id": row["message_id"], **membership}
                for membership in row.get("memberships", [])
            ],
            "destination_folder": destination,
            "uid_before": row["uid"],
            "uidvalidity_before": row["uidvalidity"],
            "uid_after": None,
            "uidvalidity_after": None,
            "header_digest": header_digest(row),
            "fingerprint": list(message_fingerprint(row)),
            "size": row.get("size"),
            "internaldate": row.get("internaldate"),
            "personal_provider_bypasses": [],
            "status": "not_attempted",
            **({"seen_before": row["seen"]} if "seen" in row else {}),
        }

    def _perform(
        self,
        connection: Any,
        deadline: Any,
        action: str,
        target: str,
        row: dict,
        record: dict,
        *,
        before_mutation: Callable[[], None] | None = None,
        stage: str = "execute",
        journal_intent: Callable[[], None] | None = None,
    ) -> None:
        deadline.check(connection)
        if stage != "undo" and (action in _SEEN_ACTIONS or row.get("mark_read")):
            require_read_state_source(row["folder"], connection)
        _checked(connection.select(_mailbox(row["folder"]), readonly=False))
        if _uidvalidity(connection) != row["uidvalidity"]:
            raise _UndoVerificationError(
                "UIDVALIDITY changed before mutation during "
                + identity_context(stage, row["folder"])
            )
        # Recheck the exact UID after writable SELECT, before any mutation.
        # I/O failures propagate to _run's interruption handler, not "missing".
        fetched = fetch_rows(
            self.client,
            connection,
            deadline,
            row["folder"],
            [row["uid"]],
            row["uidvalidity"],
            complete=False,
        )
        if not fetched or fetched[0]["message_id"] != row["message_id"]:
            raise _UndoVerificationError(
                "Message changed before mutation during " + identity_context(stage, row["folder"])
            )
        current = fetched[0]
        self._check_protected([current], stage=stage)
        digest = header_digest(current)
        if row.get("undo_header_digest") and digest != row["undo_header_digest"]:
            raise _UndoVerificationError(
                "header digest changed before mutation during "
                + identity_context(stage, row["folder"])
            )
        if any(current.get(key) != value for key, value in row.get("undo_identity", {}).items()):
            raise _UndoVerificationError(
                "message size or internal date changed before mutation during "
                + identity_context(stage, row["folder"])
            )
        if message_fingerprint(current) != message_fingerprint(row):
            raise _UndoVerificationError(
                "Message fingerprint changed before mutation during "
                + identity_context(stage, row["folder"])
            )
        if stage != "undo" and "seen" in row and current.get("seen") != row["seen"]:
            raise _UndoVerificationError(
                "Message Seen state changed before mutation during "
                + identity_context(stage, row["folder"])
            )
        record["header_digest"] = digest
        _, bypasses = self.policy.evaluate(current)
        if stage != "undo" and (
            row.get("personal_provider_allowlist_digest") != self.policy.allowlist_digest()
            or row.get("personal_provider_bypasses") != bypasses
        ):
            raise _UndoVerificationError(
                "Personal-provider allowlist or bypass changed before mutation during "
                + identity_context(stage, row["folder"])
                + "; request a new preview"
            )
        deadline.check(connection)
        if before_mutation is not None:
            before_mutation()
        # Attach evidence from the last sender check to every attempted mutation,
        # including uncertain outcomes. _run appends it under the existing lock.
        record["personal_provider_bypasses"] = bypasses
        record["status"] = "uncertain"
        changes_seen = action in _SEEN_ACTIONS or row.get("mark_read") or "restore_seen" in row
        if changes_seen:
            record["stage"] = "seen"
        if journal_intent is not None:
            journal_intent()
        deadline.check(connection)
        # Mutations invalidate the call-local read inventory for later undo lookups.
        connection._cleanup_inventory = {}
        if changes_seen:
            seen = row.get("restore_seen", action != "mark_unread")
            _checked(
                connection.uid(
                    "STORE", row["uid"], "+FLAGS.SILENT" if seen else "-FLAGS.SILENT", r"(\Seen)"
                )
            )
            if action in _SEEN_ACTIONS or action == "restore_seen":
                record["uidvalidity_after"], record["uid_after"] = row["uidvalidity"], row["uid"]
                record["status"] = "done"
                return
            deadline.check(connection)
            record["stage"] = "move"
            if journal_intent is not None:
                journal_intent()
            deadline.check(connection)
        if action in _MOVE_ACTIONS or action == "restore_folder":
            record["stage"] = "move"
            data = _checked(connection.uid("MOVE", row["uid"], _mailbox(target)))
            self.client.search_index.remove(row["folder"], row["uidvalidity"], [row["uid"]])
            # The folder MOVE is confirmed, but label restoration is still
            # incomplete even if parsing its recovery handle fails below.
            restore_labels = action == "restore_folder" and row.get("restore_labels")
            record["status"] = "moved" if restore_labels else "done"
            record["uidvalidity_after"], record["uid_after"] = _copyuid(
                connection, data, row["uid"]
            )
            supplied_mapping = record["uid_after"] is not None
            used = connection._cleanup_moved_handles | {
                (target, item["uidvalidity"], item["uid"])
                for item in record.get("destination_before", [])
            }
            boundary = record.get("destination_uidnext")
            before_boundary = boundary is not None and (
                record["uidvalidity_after"] != record["destination_uidvalidity"]
                or record["uid_after"] is None
                or int(record["uid_after"]) < int(boundary)
            )
            if (
                before_boundary
                or (target, record["uidvalidity_after"], record["uid_after"]) in used
            ):
                # Stale COPYUID cannot authorize a pre-existing copy or reuse
                # an earlier message's destination handle.
                record["uidvalidity_after"] = record["uid_after"] = None
            if record["uid_after"] is None and (
                "destination_before" in record or (boundary is not None and supplied_mapping)
            ):
                # Reject and repair a stale supplied mapping using the append
                # boundary. An absent mapping on ordinary trash/archive keeps
                # its existing account-wide digest-only undo verification.
                self._pin_destination(connection, deadline, target, row, record)
            if record["uid_after"] is not None:
                connection._cleanup_moved_handles.add(
                    (target, record["uidvalidity_after"], record["uid_after"])
                )
            if restore_labels:
                record["restored_labels"] = []
                restored, reason = self._undo_lookup(connection, deadline, target, record)
                if reason:
                    raise MailError("Restored message could not be located for label restoration")
                for label in row["restore_labels"]:
                    entry = self._row_record(restored, label)
                    record["restored_labels"].append(entry)
                    self._perform(
                        connection, deadline, "add_label", label, restored, entry, stage=stage
                    )
                record["status"] = "done"
        elif action == "add_label":
            record["stage"] = "copy"
            data = _checked(connection.uid("COPY", row["uid"], _mailbox(target)))
            record["status"] = "done"
            record["uidvalidity_after"], record["uid_after"] = _copyuid(
                connection, data, row["uid"]
            )
        elif action == "remove_label":
            # This cleanup path expunges only virtual label memberships. A
            # Labels/ mailbox is a virtual membership, not a real mail folder.
            if not row["folder"].startswith("Labels/") or "UIDPLUS" not in _capabilities(
                connection
            ):
                raise MailValidationError("Scoped label removal prerequisites changed")
            record["stage"] = "label_flag"
            _checked(connection.uid("STORE", row["uid"], "+FLAGS.SILENT", r"(\Deleted)"))
            record["status"] = "flagged"
            deadline.check(connection)
            record["stage"] = "label_expunge"
            record["status"] = "uncertain"
            _checked(connection.uid("EXPUNGE", row["uid"]))
            self.client.search_index.remove(row["folder"], row["uidvalidity"], [row["uid"]])
            record["status"] = "done"
        elif action == "restore_label_flag":
            if not row["folder"].startswith("Labels/"):
                raise MailValidationError("Flag restoration is restricted to label mailboxes")
            record["stage"] = "restore_label_flag"
            _checked(connection.uid("STORE", row["uid"], "-FLAGS.SILENT", r"(\Deleted)"))
            record["status"] = "done"
        else:
            raise MailValidationError("Unsupported cleanup action")

    def _pin_destination(self, connection, deadline, target, row, record):
        """Pin only the one new copy produced by this acknowledged MOVE."""
        boundary = record.get("destination_uidnext")
        if boundary is not None:
            state = self.client._select(connection, target)
            validity = _uidvalidity(connection)
            if validity != record["destination_uidvalidity"] or state["uidnext"] is None:
                return
            first, after = int(boundary), int(state["uidnext"])
            # A bounded append interval cannot name a pre-existing destination
            # UID. Large concurrent arrivals leave recovery unpinned rather
            # than turning this per-message fallback into a mailbox inventory.
            if not 0 < after - first <= MAX_BATCH:
                return
            inventory = fetch_rows(
                self.client,
                connection,
                deadline,
                target,
                [str(uid) for uid in range(first, after)],
                validity,
                complete=False,
                stage="destination",
            )
        else:
            inventory = folder_inventory(
                self.client,
                connection,
                deadline,
                target,
                stage="destination",
                identifiers=[row["message_id"]],
            )
            validity = _uidvalidity(connection)
        matches = matching_rows(
            inventory,
            [row["message_id"]],
            stage="destination",
            folder=target,
        )
        if validity != record["destination_uidvalidity"]:
            return
        excluded = {
            (target, item["uidvalidity"], item["uid"])
            for item in record.get("destination_before", [])
        } | connection._cleanup_moved_handles
        matches = [
            item
            for item in matches
            if (target, item["uidvalidity"], item["uid"]) not in excluded
            and not item.get("identity_invalid")
            and message_fingerprint(item) == message_fingerprint(row)
            and header_digest(item) == record["header_digest"]
            and item.get("internaldate") == record.get("internaldate")
        ]
        if len(matches) == 1:
            record["uidvalidity_after"], record["uid_after"] = validity, matches[0]["uid"]

    def _run(
        self,
        transaction: LogTransaction,
        connection: Any,
        deadline: Any,
        record: dict,
        operations: list[tuple[str, str, dict]],
        *,
        before_mutation: Callable[[], None] | None = None,
    ) -> dict:
        def authorize_once():
            nonlocal before_mutation
            if before_mutation is not None:
                before_mutation()
                before_mutation = None

        stage = "undo" if record["action"] == "undo_action" else "execute"
        journaled = record.get("status") == "in_progress"
        connection._cleanup_moved_handles = set()

        def progress():
            transaction.append(
                {
                    "action": "execute_progress",
                    "action_log_id": record["log_id"],
                    "index": index,
                    "message": entry,
                }
            )

        # The slowest restore so far in this call plans the next one.
        restore_seconds = 0.0
        try:
            for index, (action, target, row) in enumerate(operations):
                entry = record["messages"][index]
                if stage == "undo" and index and not self._undo_headroom(deadline, restore_seconds):
                    # A batched chunk can hold 200 messages, while undo restores
                    # each by its own handle. After the first, stop cleanly before
                    # a restore that cannot finish: untouched entries stay retryable.
                    record["partial"] = True
                    record["error"] = _UNDO_BUDGET_ERROR
                    break
                restore_started = mail.time.monotonic()
                try:
                    self._perform(
                        connection,
                        deadline,
                        action,
                        target,
                        row,
                        entry,
                        before_mutation=authorize_once,
                        stage=stage,
                        **({"journal_intent": progress} if journaled else {}),
                    )
                except _UndoVerificationError as error:
                    if record["action"] != "undo_action" or entry["status"] != "not_attempted":
                        raise
                    record["partial"] = True
                    record["missing"].append(
                        {
                            "message_id": row["message_id"],
                            "folder": row["folder"],
                            "reason": str(error),
                        }
                    )
                finally:
                    if journaled:
                        progress()
                restore_seconds = max(restore_seconds, mail.time.monotonic() - restore_started)
        except Exception:
            # Parsing and other unexpected failures must still mark and return
            # the journaled chunk, including its undo ID and exact outcomes.
            record["partial"] = True
            record["error"] = (
                "Cleanup interrupted during "
                + identity_context(stage + " " + entry.get("stage", "verification"), row["folder"])
                + "; inspect logged outcomes before another action"
            )
        finally:
            if journaled:
                record["partial"] = record["partial"] or any(
                    entry["status"] != "done" for entry in record["messages"]
                )
                # Preserve the recovery ID even when a later append fails. The
                # pre-command intent remains durable and can be inspected/undone.
                try:
                    transaction.append(
                        {
                            "action": "execute_result",
                            "action_log_id": record["log_id"],
                            "partial": record["partial"],
                            **({"error": record["error"]} if "error" in record else {}),
                        }
                    )
                except Exception:
                    record["partial"] = True
                    record["error"] = (
                        "Cleanup outcome could not be logged; recover with list_actions"
                    )
            else:
                transaction.append(record)
        completed = sum(item["status"] == "done" for item in record["messages"])
        return {
            "log_id": record["log_id"],
            "count": completed,
            "partial": record["partial"],
            "messages": record["messages"],
            "missing": record.get("missing", []),
            **(
                {
                    "unresolved": [
                        {
                            **{key: value for key, value in row.items() if key != "outcome_log_id"},
                            "log_id": row["outcome_log_id"],
                        }
                        for row in record["unresolved"]
                    ]
                }
                if record.get("unresolved")
                else {}
            ),
            **({"error": record["error"]} if "error" in record else {}),
        }

    def _run_batched(
        self,
        transaction: LogTransaction,
        connection: Any,
        deadline: Any,
        record: dict,
        action: str,
        target: str,
        rows: list[dict],
        *,
        before_mutation: Callable[[], None] | None = None,
    ) -> dict:
        """Change each source folder's verified rows with one UID STORE and/or UID MOVE.

        Every UID is checked again after the writable SELECT, and only UIDs that
        pass are named in the command, each with a durable intent first. Each
        UID's outcome is then verified (Seen by FETCH; MOVE by source absence
        and a pinned destination copy) before it counts as done. A UID that
        changed is left untouched and reported; an unverified outcome stays
        uncertain. Nothing outside the frozen, journaled prefix is ever named.
        """
        journaled = record.get("status") == "in_progress"
        connection._cleanup_moved_handles = set()
        entries = record["messages"]
        progress = {"stage": "verification", "folder": rows[0]["folder"], "authorized": False}
        written = [copy.deepcopy(entry) for entry in entries]

        def journal(indices: list[int]) -> None:
            # One durable line per changed row, in order; each line is complete.
            if journaled:
                for index in indices:
                    if entries[index] != written[index]:
                        transaction.append(
                            {
                                "action": "execute_progress",
                                "action_log_id": record["log_id"],
                                "index": index,
                                "message": entries[index],
                            }
                        )
                        written[index] = copy.deepcopy(entries[index])

        try:
            groups: dict[str, list[int]] = {}
            for index, row in enumerate(rows):
                groups.setdefault(row["folder"], []).append(index)
            batches = []
            for folder, indices in groups.items():
                if action not in _MOVE_ACTIONS:
                    batches.append((folder, indices))
                    continue
                # Identical copies go to separate commands, so that every moved
                # UID can be pinned to exactly one new copy even without COPYUID
                # (command_group_counts plans the same groups).
                waves: list[list[int]] = []
                copies = Counter()
                for index in indices:
                    identity = copy_identity(rows[index])
                    if copies[identity] == len(waves):
                        waves.append([])
                    waves[copies[identity]].append(index)
                    copies[identity] += 1
                batches.extend((folder, wave) for wave in waves)
            for folder, indices in batches:
                progress.update(stage="verification", folder=folder)
                verified, bypasses, changed = self._final_batch_check(
                    connection, deadline, action, folder, rows, entries, indices
                )
                # Durable evidence for rows left untouched (still not_attempted).
                journal(changed)
                if not verified:
                    continue
                deadline.check(connection)
                if not progress["authorized"]:
                    if before_mutation is not None:
                        before_mutation()
                    progress["authorized"] = True
                try:
                    self._mutate_batch(
                        connection,
                        deadline,
                        action,
                        target,
                        folder,
                        rows,
                        entries,
                        verified,
                        bypasses,
                        journal,
                        progress,
                    )
                except Exception:
                    # A refused command or a lost reply leaves its rows uncertain;
                    # each is reported as mismatched with that reason.
                    for index in verified:
                        if entries[index]["status"] == "uncertain":
                            entries[index].setdefault(
                                "verification",
                                "Batched command outcome could not be verified during "
                                + identity_context("execute", folder),
                            )
                    raise
                finally:
                    # Each touched row's final known state, including uncertain.
                    journal(verified)
            if any("verification" in entry for entry in entries):
                record["error"] = (
                    "Final per-UID verification found changed or unverified messages; "
                    "they are reported as mismatched and not counted as done"
                )
        except Exception:
            # Parsing and other unexpected failures must still mark and return
            # the journaled chunk, including its undo ID and exact outcomes.
            record["partial"] = True
            record["error"] = (
                "Cleanup interrupted during "
                + identity_context("execute " + progress["stage"], progress["folder"])
                + "; inspect logged outcomes before another action"
            )
        finally:
            if journaled:
                record["partial"] = record["partial"] or any(
                    entry["status"] != "done" for entry in entries
                )
                try:
                    transaction.append(
                        {
                            "action": "execute_result",
                            "action_log_id": record["log_id"],
                            "partial": record["partial"],
                            **({"error": record["error"]} if "error" in record else {}),
                        }
                    )
                except Exception:
                    record["partial"] = True
                    record["error"] = (
                        "Cleanup outcome could not be logged; recover with list_actions"
                    )
            else:
                transaction.append(record)
        return {
            "log_id": record["log_id"],
            "count": sum(entry["status"] == "done" for entry in entries),
            "partial": record["partial"],
            "messages": entries,
            "missing": record.get("missing", []),
            "mismatched": [
                {
                    **{key: entry[key] for key in ("message_id", "folder")},
                    "uid": entry["uid_before"],
                    "status": entry["status"],
                    "reason": entry["verification"],
                }
                for entry in entries
                if "verification" in entry
            ],
            **({"error": record["error"]} if "error" in record else {}),
        }

    def _final_batch_check(self, connection, deadline, action, folder, rows, entries, indices):
        """Writable SELECT, then one FETCH re-verifies each UID right before mutation."""
        deadline.check(connection)
        if action in _SEEN_ACTIONS or rows[indices[0]].get("mark_read"):
            require_read_state_source(folder, connection)
        _checked(connection.select(_mailbox(folder), readonly=False))
        validity = _uidvalidity(connection)
        current = {}
        if all(rows[index]["uidvalidity"] == validity for index in indices):
            # I/O failures propagate to the interruption handler, not "changed".
            current = {
                row["uid"]: row
                for row in fetch_rows(
                    self.client,
                    connection,
                    deadline,
                    folder,
                    [rows[index]["uid"] for index in indices],
                    validity,
                    complete=False,
                )
            }
        allowlist = self.policy.allowlist_digest()
        verified, bypasses, changed = [], {}, []
        for index in indices:
            row, now = rows[index], current.get(rows[index]["uid"])
            reason = None
            if row["uidvalidity"] != validity:
                reason = "UIDVALIDITY changed before mutation"
            elif now is None or now["message_id"] != row["message_id"]:
                reason = "Message changed before mutation"
            elif self.policy.check(now) or now.get("identity_invalid"):
                reason = "Protected sender or ambiguous identity"
            elif message_fingerprint(now) != message_fingerprint(row):
                reason = "Message fingerprint changed before mutation"
            elif "seen" in row and now.get("seen") != row["seen"]:
                reason = "Message Seen state changed before mutation"
            else:
                _, found = self.policy.evaluate(now)
                if (
                    row.get("personal_provider_allowlist_digest") != allowlist
                    or row.get("personal_provider_bypasses") != found
                ):
                    reason = (
                        "Personal-provider allowlist or bypass changed before mutation; "
                        "request a new preview"
                    )
                else:
                    entries[index]["header_digest"] = header_digest(now)
                    bypasses[index] = found
                    verified.append(index)
            if reason is not None:
                entries[index]["verification"] = (
                    reason + " during " + identity_context("execute", folder)
                )
                changed.append(index)
        return verified, bypasses, changed

    def _mutate_batch(
        self,
        connection,
        deadline,
        action,
        target,
        folder,
        rows,
        entries,
        verified,
        bypasses,
        journal,
        progress,
    ):
        changes_seen = action in _SEEN_ACTIONS or bool(rows[verified[0]].get("mark_read"))
        for index in verified:
            # Attach the last sender check's evidence; then make the intent durable.
            entries[index]["personal_provider_bypasses"] = bypasses[index]
            entries[index]["status"] = "uncertain"
            entries[index]["stage"] = "seen" if changes_seen else "move"
            journal([index])
        deadline.check(connection)
        # Mutations invalidate the call-local read inventory for later undo lookups.
        connection._cleanup_inventory = {}
        if changes_seen:
            seen = action != "mark_unread"
            progress["stage"] = "seen"
            _checked(
                connection.uid(
                    "STORE",
                    _uid_set([rows[index]["uid"] for index in verified]),
                    "+FLAGS.SILENT" if seen else "-FLAGS.SILENT",
                    r"(\Seen)",
                )
            )
            progress["stage"] = "seen verification"
            verified = self._verify_seen(
                connection, deadline, folder, rows, entries, verified, seen
            )
            if action in _SEEN_ACTIONS:
                for index in verified:
                    entries[index]["uidvalidity_after"] = rows[index]["uidvalidity"]
                    entries[index]["uid_after"] = rows[index]["uid"]
                    entries[index]["status"] = "done"
                return
            for index in verified:
                entries[index]["stage"] = "move"
                journal([index])
            deadline.check(connection)
        if not verified:
            return
        progress["stage"] = "move"
        data = _checked(
            connection.uid(
                "MOVE", _uid_set([rows[index]["uid"] for index in verified]), _mailbox(target)
            )
        )
        progress["stage"] = "move verification"
        self.client.search_index.remove(
            folder, rows[verified[0]]["uidvalidity"], [rows[index]["uid"] for index in verified]
        )
        self._verify_moves(connection, deadline, folder, target, rows, entries, verified, data)

    def _light_rows(self, connection, deadline, folder, uids, validity, *, flags=False):
        """Envelope metadata only: no message literal is read to verify an outcome."""
        metadata = self.client._metadata(
            connection,
            uids,
            deadline=deadline,
            headers=False,
            bodystructure=False,
            internaldate=True,
            envelope=True,
            size=True,
            flags=flags,
        )
        if _uidvalidity(connection) != validity:
            raise _IncompleteIdentityInventory(
                "Mailbox identity changed during " + identity_context("verification", folder)
            )
        result = []
        for uid in uids:
            if uid in metadata:
                row = envelope_row(metadata[uid], uid, folder, validity)
                if flags:
                    row["seen"] = any(
                        flag.casefold() == r"\seen" for flag in metadata[uid]["flags"]
                    )
                result.append(row)
        return result

    def _verify_seen(self, connection, deadline, folder, rows, entries, indices, seen):
        """Every UID must now carry the requested Seen state; others stay uncertain."""
        current = {
            row["uid"]: row
            for row in self._light_rows(
                connection,
                deadline,
                folder,
                [rows[index]["uid"] for index in indices],
                rows[indices[0]]["uidvalidity"],
                flags=True,
            )
        }
        verified = []
        for index in indices:
            row, now = rows[index], current.get(rows[index]["uid"])
            if (
                now is None
                or now["message_id"] != row["message_id"]
                or message_fingerprint(now) != message_fingerprint(row)
                or now["seen"] is not seen
            ):
                entries[index]["verification"] = (
                    "Seen state could not be verified after STORE during "
                    + identity_context("execute", folder)
                )
                continue
            verified.append(index)
        return verified

    def _verify_moves(self, connection, deadline, folder, target, rows, entries, indices, response):
        """Count a UID as moved only once it left its source and its copy is pinned.

        UIDs are never reused within one UIDVALIDITY, so after the acknowledged
        MOVE one read of the source shows which UIDs left it: a UID still
        present was not moved, even if COPYUID names it. Leaving is not enough:
        another client may have moved the message elsewhere first, and the
        server then omits it from COPYUID. A UID is done only once its new copy
        in the destination is pinned, by a COPYUID handle naming a new, unused
        destination UID whose envelope matches, or else by its one new
        identity match. Every UID stays uncertain until then; when evidence is
        missing or a read fails, each affected UID is reported with a reason.
        """
        uids = [rows[index]["uid"] for index in indices]
        supplied = _copyuid_map(connection, response, uids)
        validity = rows[indices[0]]["uidvalidity"]
        try:
            # Fetch the UID alone. _metadata drops a record that lacks a requested
            # item, so asking for more could make a UID named here look moved.
            present = self.client._metadata(
                connection,
                uids,
                deadline=deadline,
                headers=False,
                bodystructure=False,
                internaldate=False,
            )
            if _uidvalidity(connection) != validity:
                raise _IncompleteIdentityInventory(
                    "Mailbox identity changed during "
                    + identity_context("move verification", folder)
                )
        except Exception:
            # COPYUID alone settles nothing: without this read no UID is done.
            for index in indices:
                entries[index]["verification"] = (
                    "Source folder could not be verified after MOVE during "
                    + identity_context("execute", folder)
                )
            raise
        left = []
        for index in indices:
            entries[index].update(status="uncertain", uidvalidity_after=None, uid_after=None)
            if rows[index]["uid"] in present:
                # Not moved, whatever COPYUID claimed: the outcome is unverified.
                supplied.pop(rows[index]["uid"], None)
                entries[index]["verification"] = (
                    "Message remained in its source folder after MOVE during "
                    + identity_context("execute", folder)
                )
            else:
                left.append(index)
        if not left:
            return
        try:
            self._confirm_destinations(
                connection, deadline, target, rows, entries, left, supplied, len(indices)
            )
        finally:
            # A pinned copy is exact evidence, even if a later read failed. A UID
            # without one may now be anywhere: it stays uncertain, never done.
            for index in left:
                if entries[index]["uid_after"] is not None:
                    entries[index]["status"] = "done"
                else:
                    entries[index]["verification"] = (
                        "Message left its source folder, but its copy could not be verified "
                        "in the destination after MOVE during "
                        + identity_context("execute", target)
                    )

    def _confirm_destinations(
        self, connection, deadline, target, rows, entries, moved, supplied, sent
    ):
        self.client._select(connection, target)
        target_validity = _uidvalidity(connection)
        proposed = {}
        for index in moved:
            entry = entries[index]
            used = connection._cleanup_moved_handles | {
                (target, item["uidvalidity"], item["uid"])
                for item in entry.get("destination_before", [])
            }
            if rows[index]["uid"] not in supplied:
                continue
            validity_after, uid_after = supplied[rows[index]["uid"]]
            boundary = entry.get("destination_uidnext")
            if (
                validity_after != target_validity
                or validity_after != entry.get("destination_uidvalidity", target_validity)
                or (boundary is not None and int(uid_after) < int(boundary))
                or (target, validity_after, uid_after) in used
            ):
                # Stale COPYUID cannot authorize a pre-existing copy or reuse
                # an earlier message's destination handle.
                continue
            proposed[index] = uid_after
        copies = {}
        if proposed:
            copies = {
                row["uid"]: row
                for row in self._light_rows(
                    connection,
                    deadline,
                    target,
                    sorted(set(proposed.values()), key=int),
                    target_validity,
                )
            }
        for index, uid_after in proposed.items():
            candidate = copies.get(uid_after)
            if (
                candidate is not None
                and not candidate.get("identity_invalid")
                and candidate["message_id"] == rows[index]["message_id"]
                and message_fingerprint(candidate) == message_fingerprint(rows[index])
                and header_digest(candidate) == entries[index]["header_digest"]
            ):
                entries[index]["uidvalidity_after"] = target_validity
                entries[index]["uid_after"] = uid_after
                connection._cleanup_moved_handles.add((target, target_validity, uid_after))
        # Repair a missing or rejected mapping, as the single-UID path does:
        # without a pinned copy a UID that left its source is not done.
        unpinned = [index for index in moved if entries[index]["uid_after"] is None]
        self._pin_destinations(connection, deadline, target, rows, entries, unpinned, sent)

    def _pin_destinations(self, connection, deadline, target, rows, entries, indices, sent):
        """Pin each row to one new destination copy; one read per command.

        With a destination append boundary, a single FETCH of the new UIDs
        (bounded by this command's size plus the single-UID path's 200 UID
        allowance for arrivals) serves every row. Without one, a single
        identity inventory of the rows' Message-IDs does. Copies that existed
        before the move never qualify; a row without exactly one match stays
        unpinned.
        """
        bounded = [index for index in indices if entries[index].get("destination_uidnext")]
        unbounded = [index for index in indices if index not in bounded]
        if unbounded:
            inventory = folder_inventory(
                self.client,
                connection,
                deadline,
                target,
                stage="destination",
                identifiers=list(dict.fromkeys(rows[index]["message_id"] for index in unbounded)),
            )
            self._pin_matches(
                connection, target, _uidvalidity(connection), inventory, rows, entries, unbounded
            )
        if not bounded:
            return
        state = self.client._select(connection, target)
        validity = _uidvalidity(connection)
        first = min(int(entries[index]["destination_uidnext"]) for index in bounded)
        if (
            state["uidnext"] is None
            or any(entries[index]["destination_uidvalidity"] != validity for index in bounded)
            or not 0 < int(state["uidnext"]) - first <= MAX_BATCH + sent - 1
        ):
            # Large concurrent arrivals leave these rows unverified rather than
            # turning outcome verification into a mailbox inventory.
            return
        inventory = fetch_rows(
            self.client,
            connection,
            deadline,
            target,
            [str(uid) for uid in range(first, int(state["uidnext"]))],
            validity,
            complete=False,
            stage="destination",
        )
        self._pin_matches(connection, target, validity, inventory, rows, entries, bounded)

    @staticmethod
    def _pin_matches(connection, target, validity, inventory, rows, entries, indices):
        """Pin each row to its one matching copy that no other handle accounts for."""
        excluded = set(connection._cleanup_moved_handles)
        for index in indices:
            excluded.update(
                (target, item["uidvalidity"], item["uid"])
                for item in entries[index].get("destination_before", [])
            )
        for index in indices:
            row, entry = rows[index], entries[index]
            # Without a recorded destination identity, the preflight inventory
            # found no copy of this Message-ID there: any match arrived since.
            if entry.get("destination_uidvalidity", validity) != validity:
                continue
            matches = [
                item
                for item in matching_rows(
                    inventory, [row["message_id"]], stage="destination", folder=target
                )
                if (target, item["uidvalidity"], item["uid"]) not in excluded
                and not item.get("identity_invalid")
                and message_fingerprint(item) == message_fingerprint(row)
                and header_digest(item) == entry["header_digest"]
                and item.get("internaldate") == entry.get("internaldate")
            ]
            if len(matches) == 1:
                entry["uidvalidity_after"], entry["uid_after"] = validity, matches[0]["uid"]
                excluded.add((target, validity, matches[0]["uid"]))
                connection._cleanup_moved_handles.add((target, validity, matches[0]["uid"]))

    @staticmethod
    def _undo_headroom(deadline: Any, observed: float = 0.0) -> bool:
        """Room for one more per-message restore and the journal.

        A restore is allowed the per-message seconds cleanup plans, or the
        slowest restore observed in this call when that took longer.
        """
        from . import guarded

        required = (
            max(guarded.EXECUTE_MUTATION_SECONDS_PER_MESSAGE, observed)
            + guarded.EXECUTE_LOG_RESERVE_SECONDS
        )
        return deadline.io_end - mail.time.monotonic() >= required

    def apply(
        self,
        connection: Any,
        deadline: Any,
        action: str,
        params: dict[str, Any],
        snapshots: list[dict],
        *,
        before_mutation: Callable[[], None] | None = None,
        before_chunk: Callable[[], None] | None = None,
        token_digest: str | None = None,
    ) -> dict:
        with self.log.transaction() as transaction:
            # Refuse forward changes if either the chunk or run audit is damaged.
            transaction.records(include_runs=True)
            target = self.validate_target(action, params, connection)
            connection._cleanup_seen = action in _SEEN_ACTIONS or params.get("mark_read", False)
            self._preflight(connection, deadline, action, target, snapshots)
            # Finish read-only recovery preparation before selecting the chunk.
            # Its cost can make the originally sized chunk exceed the budget.
            destination_uidnext = None
            if action in _MOVE_ACTIONS:
                identifiers = [row["message_id"] for row in snapshots]
                source_counts = {}
                certified = all("label_states" in row for row in snapshots)
                if certified:
                    state = self.client._select(connection, target)
                    destination_uidnext = state["uidnext"]
                    if destination_uidnext is None and getattr(
                        connection, "_cleanup_bounded_recovery", False
                    ):
                        raise MailValidationError(
                            "Destination recovery evidence changed after preview; preview again"
                        )
                # UIDNEXT excludes every destination UID that existed before
                # this chunk, including same-identity arrivals after preview.
                # Without it retain the complete recovery inventory below.
                # Certified and custom-folder moves pin destination recovery
                # handles; duplicate source counts cannot change that choice.
                if action != "move_to_folder" and not certified:
                    for folder in dict.fromkeys(row["folder"] for row in snapshots):
                        for item in folder_inventory(
                            self.client, connection, deadline, folder, identifiers=identifiers
                        ):
                            identity = (folder, item["message_id"])
                            source_counts[identity] = source_counts.get(identity, 0) + 1
                if destination_uidnext is None:
                    inventory = folder_inventory(
                        self.client,
                        connection,
                        deadline,
                        target,
                        stage="preflight",
                        identifiers=identifiers,
                    )
                else:
                    inventory = [
                        member
                        for row in snapshots
                        for member in row.get("memberships", [])
                        if member["folder"] == target
                    ]
                validity = _uidvalidity(connection)
            deadline.check(connection)
            verified_handles = tuple(map(_handle_identity, snapshots))
            if before_chunk is not None:
                before_chunk()
            if (
                not snapshots
                or tuple(map(_handle_identity, snapshots)) != verified_handles[: len(snapshots)]
            ):
                raise MailValidationError("Prepared cleanup chunk is not a verified prefix")
            # The journal and operations use the same frozen prefix. The final
            # authorization callback may check headroom but must not resize it.
            snapshots = list(snapshots)
            record = new_record(action, params)
            destination = "All Mail" if action == "remove_label" else target
            record["messages"] = [
                self._row_record(row, row["folder"] if action in _SEEN_ACTIONS else destination)
                for row in snapshots
            ]
            if action in _MOVE_ACTIONS:
                for row, entry in zip(snapshots, record["messages"], strict=True):
                    existing = [
                        item for item in inventory if item["message_id"] == row["message_id"]
                    ]
                    if (
                        # Rules can split one duplicate set across destinations;
                        # custom-folder recovery always needs a pinned handle.
                        action == "move_to_folder"
                        or identifiers.count(row["message_id"]) > 1
                        or source_counts.get((row["folder"], row["message_id"]), 0) > 1
                        or existing
                        or params.get("mark_read")
                    ):
                        entry["destination_before"] = [
                            {key: item[key] for key in ("uid", "uidvalidity")} for item in existing
                        ]
                        entry["destination_uidvalidity"] = validity
                    if destination_uidnext is not None:
                        # The append boundary rejects stale COPYUID for every
                        # certified move without changing the existing digest-
                        # only recovery contract when no mapping was supplied.
                        entry["destination_uidvalidity"] = validity
                        entry["destination_uidnext"] = destination_uidnext
            if action == "remove_label":
                for row, entry in zip(snapshots, record["messages"], strict=True):
                    recovery = next(
                        (
                            item
                            for item in row.get("memberships", [])
                            if item["folder"] == "All Mail"
                        ),
                        None,
                    )
                    if recovery:
                        entry["uid_after"] = recovery["uid"]
                        entry["uidvalidity_after"] = recovery["uidvalidity"]
            operations = [
                (
                    action,
                    target,
                    {**row, **({"mark_read": True} if params.get("mark_read") else {})},
                )
                for row in snapshots
            ]
            record["status"] = "in_progress"
            record["token_digest"] = token_digest
            run_context = getattr(self, "run_context", None)
            if run_context is not None:
                record.update(
                    {key: run_context[key] for key in ("run_id", "chunk_index", "approved_total")}
                )
            transaction.append(record)

            def authorize_run():
                if before_mutation is not None:
                    before_mutation()
                if run_context is not None:
                    self.log.ensure_run(transaction, run_context)

            if action in _BATCHED_ACTIONS:
                result = self._run_batched(
                    transaction,
                    connection,
                    deadline,
                    record,
                    action,
                    target,
                    [row for _, _, row in operations],
                    before_mutation=authorize_run,
                )
            else:
                result = self._run(
                    transaction,
                    connection,
                    deadline,
                    record,
                    operations,
                    before_mutation=authorize_run,
                )
            if run_context is not None and run_context.get("_call_started"):
                try:
                    self.log.update_run(transaction, run_context["run_id"])
                except Exception:
                    result["partial"] = True
                    result["error"] = "Cleanup run checkpoint could not be logged; use list_actions"
            return result

    def undo_action(self, log_id: str) -> dict:
        if not isinstance(log_id, str) or not re.fullmatch(r"[0-9a-f]{32}", log_id):
            raise MailValidationError("Invalid cleanup log ID")
        deadline = self.client._deadline(execute=True)
        with self.log.transaction() as transaction:
            records = transaction.records()
            originals = [record for record in records if record.get("log_id") == log_id]
            if len(originals) != 1 or originals[0].get("action") not in _ACTIONS:
                raise MailValidationError("Cleanup log ID was not found or cannot be undone")
            original = originals[0]
            if not isinstance(original.get("messages"), list) or not (
                1 <= len(original["messages"]) <= MAX_BATCH
            ):
                raise MailValidationError("Undo batch exceeds the 200 message safety cap")
            original, unresolved = remaining_undo(original, records)
            with self.client._session(deadline=deadline) as (connection, _):
                record, operations, examined = self._prepare_undo(connection, deadline, original)
                record["undo_version"] = 1
                record["unresolved"] = unresolved
                record["partial"] = record["partial"] or bool(unresolved)
                deferred = len(original["messages"]) - len(examined)
                if deferred:
                    # Unexamined messages stay retryable: this attempt never claims them.
                    record["partial"] = True
                    record["deferred"] = deferred
                    record["error"] = _UNDO_BUDGET_ERROR
                deadline.check(connection)
                # One durable reservation per attempt, while the exclusive log
                # lock spans preparation, mutations and the outcome append.
                # An unmatched claim blocks retries after a crash or log failure.
                claim = new_record("undo_claim", {"log_id": log_id})
                claim["undo_of"] = log_id
                claim["outcome_log_id"] = record["log_id"]
                claim["original_indices"] = examined
                transaction.append(claim)
                result = self._run(transaction, connection, deadline, record, operations)
                if deferred:
                    result["deferred"] = deferred
                return result

    def _prepare_undo(
        self, connection: Any, deadline: Any, original: dict
    ) -> tuple[dict, list, list[int]]:
        """Look up each message to restore; return the record, operations and examined indices.

        A batched chunk can hold 200 messages, each looked up by its own logged
        handles. Preparation keeps at least half of the remaining I/O window for
        restoring what it prepared: once that half is spent, the messages after
        the current one stay unexamined. The attempt claims only examined
        messages, so the rest remain retryable by the next undo call.
        """
        connection._cleanup_inventory = {}
        connection._cleanup_seen = any("seen_before" in row for row in original["messages"])
        prepare_started = mail.time.monotonic()
        prepare_end = prepare_started + max(0.0, deadline.io_end - prepare_started) / 2
        examined: list[int] = []
        folders = self.client._folders(connection)
        record = new_record("undo_action", {"log_id": original["log_id"]})
        record["undo_of"] = original["log_id"]
        record["missing"] = []
        operations = []
        protected_rows = []
        action = original["action"]
        move_folders = _move_folders(connection) if action in _MOVE_ACTIONS else set()
        capabilities = _capabilities(connection)
        if action in _MOVE_ACTIONS and "MOVE" not in capabilities:
            raise MailValidationError(
                "Undo requires MOVE capability; refusing unsafe move fallback"
            )
        if action == "add_label" and "UIDPLUS" not in capabilities:
            raise MailValidationError("Undo label addition requires UIDPLUS")
        for previous in original["messages"]:
            if examined and mail.time.monotonic() >= prepare_end:
                break
            examined.append(previous["original_index"])
            if previous["status"] == "not_attempted":
                continue
            if (action in _SEEN_ACTIONS or original.get("params", {}).get("mark_read")) and type(
                previous.get("seen_before")
            ) is not bool:
                raise MailValidationError("Invalid logged Seen state")
            identifier = _message_id(previous["message_id"])
            source, destination = previous["source_folder"], previous["destination_folder"]
            if previous.get("folder", source) != source:
                raise MailValidationError("Logged message folder does not match its source")
            if action in _MOVE_ACTIONS and source not in move_folders:
                record["missing"].append(
                    {
                        "message_id": identifier,
                        "folder": source,
                        "reason": "original source is not an allowed real folder",
                    }
                )
                continue
            lookup = destination
            target = source
            inverse = "restore_folder" if action in _MOVE_ACTIONS else "remove_label"
            if action in _SEEN_ACTIONS:
                lookup, target, inverse = source, source, "restore_seen"
            if action == "remove_label":
                if source not in folders:
                    record["missing"].append(
                        {"message_id": identifier, "folder": source, "reason": "folder missing"}
                    )
                    continue
                if not source.startswith("Labels/"):
                    raise MailValidationError("Undo label target must remain a Labels/ mailbox")
                if destination != "All Mail":
                    record["missing"].append(
                        {
                            "message_id": identifier,
                            "folder": destination,
                            "reason": "invalid All Mail recovery folder",
                        }
                    )
                    continue
                inverse = "add_label"
            if target not in folders or lookup not in folders:
                record["missing"].append(
                    {"message_id": identifier, "folder": lookup, "reason": "folder missing"}
                )
                continue
            # A Seen STORE may have succeeded before its MOVE was attempted.
            # The original source handle can restore flags without searching for
            # an unrelated same-ID destination copy.
            source_only = action in _SEEN_ACTIONS or (
                "seen_before" in previous and previous["status"] == "uncertain"
            )
            row, reason = None, None
            if source_only:
                source_entry = dict(
                    previous,
                    uid_after=previous.get("uid_before"),
                    uidvalidity_after=previous.get("uidvalidity_before"),
                )
                row, reason = self._undo_lookup(connection, deadline, source, source_entry)
                if row is not None:
                    lookup, target, inverse = source, source, "restore_seen"
            if not source_only or (reason == "not found" and action in _MOVE_ACTIONS):
                row, reason = self._undo_lookup(
                    connection,
                    deadline,
                    lookup,
                    previous,
                    folders=folders if action in _MOVE_ACTIONS else None,
                )
            if reason:
                record["missing"].append(
                    {"message_id": identifier, "folder": lookup, "reason": reason}
                )
                continue
            recovery = row
            if "seen_before" in previous:
                if type(previous["seen_before"]) is not bool:
                    raise MailValidationError("Invalid logged Seen state")
                row["restore_seen"] = previous["seen_before"]
            if action == "remove_label":
                # A timed-out STORE/EXPUNGE may have left the original label
                # UID marked Deleted. Never clear a different same-ID label UID.
                existing, reason = self._logged_uid(
                    connection,
                    deadline,
                    source,
                    identifier,
                    previous.get("uid_before"),
                    previous.get("uidvalidity_before"),
                )
                if reason and reason != "not found":
                    record["missing"].append(
                        {"message_id": identifier, "folder": source, "reason": reason}
                    )
                    continue
                if existing:
                    row, lookup, inverse = existing, source, "restore_label_flag"
            retry = previous.get("undo_retry_handle")
            if retry:
                if (row["folder"], row["uid"], row["uidvalidity"]) != (
                    retry["source_folder"],
                    retry["uid_before"],
                    retry["uidvalidity_before"],
                ) or header_digest(row) != retry["header_digest"]:
                    record["missing"].append(
                        {
                            "message_id": identifier,
                            "folder": lookup,
                            "reason": "retry identity changed",
                        }
                    )
                    continue
                row["undo_header_digest"] = retry["header_digest"]
            copies, reason, failed_folder = self._undo_memberships(
                connection, deadline, previous, row, folders
            )
            if reason:
                record["missing"].append(
                    {"message_id": identifier, "folder": failed_folder, "reason": reason}
                )
                continue
            protected_rows.extend(copies)
            protected_rows.append(recovery)
            memberships = [
                {key: copy[key] for key in ("message_id", "folder", "uidvalidity", "uid")}
                for copy in copies
            ]
            row["memberships"] = memberships
            row["folders"] = sorted(item["folder"] for item in memberships)
            if action in _MOVE_ACTIONS and inverse != "restore_seen":
                labels = [
                    name
                    for name in previous.get("folders_before", [])
                    if name.startswith("Labels/")
                ]
                if any(label not in folders for label in labels):
                    record["missing"].append(
                        {
                            "message_id": identifier,
                            "folder": lookup,
                            "reason": "original label missing",
                        }
                    )
                    continue
                if source in row["folders"]:
                    record["missing"].append(
                        {
                            "message_id": identifier,
                            "folder": source,
                            "reason": "source already contains message",
                        }
                    )
                    continue
                row["restore_labels"] = [label for label in labels if label not in row["folders"]]
            if inverse == "remove_label":
                if not lookup.startswith("Labels/"):
                    raise MailValidationError("Undo label removal must select its Labels/ mailbox")
                if not any(copy["folder"] == "All Mail" for copy in copies):
                    record["missing"].append(
                        {
                            "message_id": identifier,
                            "folder": "All Mail",
                            "reason": "logged All Mail copy missing",
                        }
                    )
                    continue
                target = lookup
            protected_rows.append(row)
            entry = self._row_record(row, target)
            if row.get("restore_labels"):
                inventory = folder_inventory(
                    self.client, connection, deadline, target, stage="undo"
                )
                entry["destination_before"] = [
                    {key: item[key] for key in ("uid", "uidvalidity")}
                    for item in inventory
                    if item["message_id"] == identifier
                ]
                entry["destination_uidvalidity"] = _uidvalidity(connection)
            entry["original_index"] = previous["original_index"]
            record["messages"].append(entry)
            operations.append((inverse, target, row))
        self._check_protected(protected_rows, stage="undo")
        # Each selected row was verified by UID above; _perform repeats that
        # verification after writable SELECT and reports late mismatches missing.
        for missing in record["missing"]:
            if " during undo in " not in missing["reason"]:
                missing["reason"] += " during " + identity_context("undo", missing["folder"])
        record["partial"] = bool(record["missing"])
        if record["missing"] and connection._cleanup_seen:
            # Read-state restoration is one exact bound set: an identity drift
            # anywhere refuses all STOREs before the claim. The attempt claims
            # every remaining message, examined or not, so none is retried.
            record["messages"] = []
            operations = []
            examined = [row["original_index"] for row in original["messages"]]
        return record, operations, examined
