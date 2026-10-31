"""Certified Message-ID occurrence inventories, never an authorization cache.

The protocol contract is deliberately stronger than UIDVALIDITY alone. Within a
UIDVALIDITY, IMAP message contents are immutable; CONDSTORE assigns increasing
modification sequences to supported metadata changes. A stable STATUS/UID-set
snapshot brackets every refresh, including warm hits. New UIDs, CHANGEDSINCE
updates and expunges are reconciled, and the complete raw occurrence list is
retained, including duplicate fields hidden by ENVELOPE. A caller must still
fetch and verify each matching UID before authorizing anything.

Missing protocol evidence, concurrent drift, an unsafe file or an unreconciled
delta returns None: the caller must run its complete scan or refuse. A missing,
corrupt or obsolete inventory is rebuilt under the same command deadline.
Servers that change immutable content without changing UID/UIDVALIDITY, or lie
about CONDSTORE, violate the contract and cannot support certified reuse.
"""

from __future__ import annotations

import fcntl
import hashlib
import imaplib
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import mail
from .mail import CallDeadlineExceeded, MailError, _encode_folder, _ImapValues, _mailbox
from .rules_store import _unique_object

# Opaque provider IDs are now accepted. Rebuild inventories that cached the old
# RFC-only invalid verdict (or discarded controls around raw identity fields).
_VERSION = 3
_MAX_BYTES = 128 * 1024 * 1024
_STATUS_FIELDS = "(UIDVALIDITY UIDNEXT HIGHESTMODSEQ MESSAGES)"
_PREVIEW_BATCH_SIZE = 200
_PREVIEW_SNAPSHOT_SECONDS = 600
_PREVIEW_MAX_SNAPSHOTS = 128


class _Uncertified(Exception):
    """The inventory cannot prove current, complete identity coverage."""


def default_index_dir() -> Path:
    state = os.environ.get("XDG_STATE_HOME")
    root = Path(state) if state else Path.home() / ".local" / "state"
    return root / "proton-mail-mcp" / "identity-index"


def _scope(settings: Any, folder: str) -> str:
    # Distinguish accounts and endpoints without putting their text in paths.
    values = [settings.bridge_user, settings.imap_host, settings.imap_port, folder]
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode("ascii")).hexdigest()


def _private(descriptor: int, *, directory: bool = False) -> None:
    info = os.fstat(descriptor)
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise _Uncertified


@contextmanager
def _storage(path: Path, key: str, *, create: bool = True):
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock = None
    try:
        _private(directory, directory=True)
        flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
        lock = os.open(key + ".lock", flags, 0o600, dir_fd=directory)
        _private(lock)
        # Another refresh must not consume this request's bounded call budget.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield directory
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


def _number(value: Any, *, zero: bool = False) -> int:
    if not isinstance(value, bytes) or not value.isdigit():
        raise _Uncertified
    result = int(value)
    if result < (0 if zero else 1):
        raise _Uncertified
    return result


def _status(connection: Any, deadline: Any, folder: str) -> dict[str, int]:
    deadline.check(connection)
    status, data = connection.status(_mailbox(folder), _STATUS_FIELDS)
    if status != "OK" or not isinstance(data, (list, tuple)) or len(data) != 1:
        raise _Uncertified
    raw = data[0]
    if not isinstance(raw, bytes):
        raise _Uncertified
    parser = _ImapValues(raw)
    mailbox, fields = parser.value(), parser.value()
    if (
        not isinstance(mailbox, bytes)
        or mailbox != _encode_folder(folder).encode("ascii")
        or not isinstance(fields, list)
        or len(fields) != 8
        or raw[parser.position :].strip()
    ):
        raise _Uncertified
    result = {}
    for name, value in zip(fields[::2], fields[1::2], strict=True):
        if name not in (b"UIDVALIDITY", b"UIDNEXT", b"HIGHESTMODSEQ", b"MESSAGES"):
            raise _Uncertified
        key = name.decode("ascii")
        if key in result:
            raise _Uncertified
        result[key] = _number(value, zero=key == "MESSAGES")
    return result


def _snapshot(client: Any, connection: Any, deadline: Any, folder: str):
    before = _status(connection, deadline, folder)
    deadline.check(connection)
    # STATUS brackets this enumeration just as SELECT brackets complete scans.
    # ALL never names a nonexistent UID; an authoritative zero needs no SEARCH.
    values = client._search(connection, ["ALL"]) if before["MESSAGES"] else []
    if any(
        not isinstance(uid, str)
        or not re.fullmatch(r"[1-9][0-9]{0,9}", uid)
        or int(uid) > 4294967295
        for uid in values
    ) or len(values) != len(set(values)):
        raise _Uncertified
    uids = set(values)
    after = _status(connection, deadline, folder)
    if (
        before != after
        or len(uids) != after["MESSAGES"]
        or any(int(uid) >= after["UIDNEXT"] for uid in uids)
    ):
        raise _Uncertified
    return after, uids


def _changed(connection: Any, deadline: Any, modseq: int, maximum: int) -> set[str]:
    deadline.check(connection)
    # Recent imaplib versions unpack UID FETCH into exactly two arguments.
    # Keep the modifier in message_parts; its parentheses stay separate on wire.
    status, data = connection.uid("FETCH", "1:*", f"(UID MODSEQ) (CHANGEDSINCE {modseq})")
    if status != "OK" or not isinstance(data, (list, tuple)):
        raise _Uncertified
    changed = set()
    for raw in data:
        if raw is None:
            continue
        if not isinstance(raw, bytes):
            raise _Uncertified
        match = re.fullmatch(
            rb"\s*[1-9][0-9]*\s+\(UID ([1-9][0-9]*) MODSEQ \(([1-9][0-9]*)\)\)\s*", raw
        )
        # Only the exact UID n MODSEQ (m) order/shape is accepted inside FETCH.
        # Any other order/shape, including extensions or unsolicited flags,
        # safely falls back to the full path rather than silently hiding a UID.
        if match is None or not modseq < int(match[2]) <= maximum:
            raise _Uncertified
        uid = match[1].decode("ascii")
        if uid in changed:
            raise _Uncertified
        changed.add(uid)
    return changed


def _payload(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )


def _valid_row(row: Any, uid: str, next_uid: int) -> bool:
    return (
        isinstance(row, dict)
        and set(row) == {"uid", "message_id", "_raw_identity_fields", "identity_invalid"}
        and uid.isascii()
        and uid.isdigit()
        and 0 < int(uid) < next_uid
        and row["uid"] == uid
        and isinstance(row["message_id"], str)
        and type(row["identity_invalid"]) is bool
        and isinstance(row["_raw_identity_fields"], list)
        and all(isinstance(value, str) for value in row["_raw_identity_fields"])
    )


def _load(directory: int, key: str):
    try:
        # A corrupt path may name a FIFO. Nonblocking open reaches the type
        # check without waiting for a writer outside the call deadline.
        descriptor = os.open(
            key + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        _private(stream.fileno())
        if os.fstat(stream.fileno()).st_size > _MAX_BYTES:
            return None
        try:
            sealed = json.loads(stream.read(_MAX_BYTES + 1), object_pairs_hook=_unique_object)
            value = sealed["inventory"]
            if (
                set(sealed) != {"inventory", "sha256"}
                or sealed["sha256"] != hashlib.sha256(_payload(value)).hexdigest()
                or set(value) != {"version", "scope", "state", "rows"}
                or value["version"] != _VERSION
                or type(value["version"]) is not int
                or value["scope"] != key
                or set(value["state"]) != {"UIDVALIDITY", "UIDNEXT", "HIGHESTMODSEQ", "MESSAGES"}
                or any(type(number) is not int for number in value["state"].values())
                or any(
                    number < (0 if name == "MESSAGES" else 1)
                    for name, number in value["state"].items()
                )
                or not isinstance(value["rows"], dict)
                or len(value["rows"]) != value["state"]["MESSAGES"]
                or not all(
                    _valid_row(row, uid, value["state"]["UIDNEXT"])
                    for uid, row in value["rows"].items()
                )
            ):
                return None
            return value
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            return None


def _write(directory: int, key: str, state: dict, rows: dict) -> None:
    value = {"version": _VERSION, "scope": key, "state": state, "rows": rows}
    sealed = {"inventory": value, "sha256": hashlib.sha256(_payload(value)).hexdigest()}
    temporary = key + "." + secrets.token_hex(12) + ".tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_payload(sealed))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, key + ".json", src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _reconcile(connection, deadline, initial, uids, saved):
    """Keep only unchanged, live rows from a complete or interrupted snapshot."""
    if saved is None:
        return {}, uids
    previous = saved["state"]
    known = set(saved["rows"])
    previous_uids = set(saved.get("uids", known))
    if (
        initial["UIDVALIDITY"] != previous["UIDVALIDITY"]
        or initial["UIDNEXT"] < previous["UIDNEXT"]
        or initial["HIGHESTMODSEQ"] < previous["HIGHESTMODSEQ"]
        or (
            initial["HIGHESTMODSEQ"] == previous["HIGHESTMODSEQ"]
            and (uids != previous_uids or initial["UIDNEXT"] != previous["UIDNEXT"])
        )
        or any(int(uid) < previous["UIDNEXT"] for uid in uids - previous_uids)
    ):
        return {}, uids
    changed = (
        _changed(connection, deadline, previous["HIGHESTMODSEQ"], initial["HIGHESTMODSEQ"])
        if initial["HIGHESTMODSEQ"] != previous["HIGHESTMODSEQ"]
        else set()
    )
    if not changed <= uids:
        raise _Uncertified
    # An interrupted checkpoint can contain holes. Missing rows and changed
    # completed rows both need fresh raw fields before this snapshot is usable.
    rows = {uid: row for uid, row in saved["rows"].items() if uid in uids and uid not in changed}
    return rows, uids - set(rows)


def _refresh(
    client,
    connection,
    deadline,
    folder,
    validity,
    fetch_rows,
    directory,
    key,
    *,
    allow_refresh=True,
):
    saved = _load(directory, key) if not allow_refresh else None
    if not allow_refresh and saved is None:
        return None
    initial, uids = _snapshot(client, connection, deadline, folder)
    if str(initial["UIDVALIDITY"]) != validity:
        raise _Uncertified
    if allow_refresh:
        saved = _load(directory, key)
    if not allow_refresh:
        # A preview must not spend its budget catching up an unrelated mailbox.
        # Reuse only the exact certified snapshot, including its complete UID set.
        if saved is None or initial != saved["state"] or uids != set(saved["rows"]):
            return None
        deadline.check(connection)
        return list(saved["rows"].values())
    if not uids:
        # The stable snapshot proves there are no raw fields to discover,
        # including after the last UID was expunged. No FETCH can add evidence.
        deadline.check(connection)
        _write(directory, key, initial, {})
        deadline.check(connection)
        return []
    if saved is not None and initial == saved["state"] and uids == set(saved["rows"]):
        # The bracketed snapshot proves an unchanged complete prior audit.
        deadline.check(connection)
        return list(saved["rows"].values())
    rows, pending = _reconcile(connection, deadline, initial, uids, saved)
    fetched = fetch_rows(
        client, connection, deadline, folder, sorted(pending, key=int), validity, raw_fields=True
    )
    if len(fetched) != len(pending) or {row["uid"] for row in fetched} != pending:
        raise _Uncertified
    for row in fetched:
        minimal = {name: row[name] for name in ("uid", "message_id", "_raw_identity_fields")}
        minimal["identity_invalid"] = bool(row.get("identity_invalid"))
        if not _valid_row(minimal, row["uid"], initial["UIDNEXT"]):
            raise _Uncertified
        rows[row["uid"]] = minimal
    final, final_uids = _snapshot(client, connection, deadline, folder)
    if final != initial or final_uids != uids or set(rows) != final_uids:
        raise _Uncertified
    deadline.check(connection)
    _write(directory, key, final, rows)
    deadline.check(connection)
    return list(rows.values())


def _preview_inventory(
    client, connection, deadline, folder, validity, fetch_rows, path, key, state
):
    """Certify bounded raw batches without creating or updating index files."""
    now = mail.time.monotonic()
    snapshots = getattr(client, "_rules_identity_snapshots", None)
    if snapshots is None:
        snapshots = client._rules_identity_snapshots = {}
    for scope, snapshot in list(snapshots.items()):
        if snapshot["expires"] <= now:
            snapshots.pop(scope, None)
    checkpoints = state.setdefault("certified_inventories", {})
    saved = checkpoints.get(key)
    if saved is None:
        saved = snapshots.get(key, {}).get("inventory")
    if saved is None:
        # Missing or unsafe storage cannot prevent a fresh in-memory audit.
        # No lock, directory or inventory file is created by a rules preview.
        try:
            with _storage(path, key, create=False) as directory:
                saved = _load(directory, key)
        except (_Uncertified, OSError):
            pass
    initial, uids = _snapshot(client, connection, deadline, folder)
    if str(initial["UIDVALIDITY"]) != validity:
        raise _Uncertified
    rows, pending = _reconcile(connection, deadline, initial, uids, saved)
    checkpoint = {"state": initial, "uids": uids, "rows": rows}
    checkpoints[key] = checkpoint
    pending = sorted(pending, key=int)
    batch_size = max(1, _PREVIEW_BATCH_SIZE // state.get("batch_scale", 1))
    for offset in range(0, len(pending), batch_size):
        deadline.check(connection)
        batch = pending[offset : offset + batch_size]
        started = mail.time.monotonic()
        fetched = fetch_rows(client, connection, deadline, folder, batch, validity, raw_fields=True)
        if len(fetched) != len(batch) or {row["uid"] for row in fetched} != set(batch):
            raise _Uncertified
        completed = {}
        for row in fetched:
            minimal = {name: row[name] for name in ("uid", "message_id", "_raw_identity_fields")}
            minimal["identity_invalid"] = bool(row.get("identity_invalid"))
            if not _valid_row(minimal, row["uid"], initial["UIDNEXT"]):
                raise _Uncertified
            completed[row["uid"]] = minimal
        # Publish only an entire successfully validated FETCH batch. Checking
        # the budget afterwards preserves completed work across a deadline.
        rows.update(completed)
        cache = getattr(connection, "_cleanup_inventory", None)
        if cache is not None:
            # Full current-call audit rows can also satisfy the candidate read;
            # only the minimal identity rows above survive in checkpoints.
            fresh_key = (
                folder,
                validity,
                ("raw_identity_fresh", bool(getattr(connection, "_cleanup_seen", False))),
            )
            cache.setdefault(fresh_key, {}).update({row["uid"]: dict(row) for row in fetched})
        state["progress"] = state.get("progress", 0) + len(batch)
        # Rules batch sizing excludes these one-time audit reads.
        state["audit_seconds"] = state.get("audit_seconds", 0) + mail.time.monotonic() - started
        deadline.check(connection)
    if (
        pending
        or saved is None
        or initial != saved["state"]
        or uids != set(saved.get("uids", saved["rows"]))
    ):
        final, final_uids = _snapshot(client, connection, deadline, folder)
        if final != initial or final_uids != uids or set(rows) != final_uids:
            raise _Uncertified
    deadline.check(connection)
    while len(snapshots) >= _PREVIEW_MAX_SNAPSHOTS and key not in snapshots:
        snapshots.pop(next(iter(snapshots)))
    snapshots[key] = {
        "expires": now + _PREVIEW_SNAPSHOT_SECONDS,
        "inventory": {"state": initial, "rows": dict(rows)},
    }
    return list(rows.values())


def certified_inventory(
    client: Any,
    connection: Any,
    deadline: Any,
    folder: str,
    validity: str,
    *,
    fetch_rows: Any,
    allow_refresh: bool = True,
    preview_state: dict | None = None,
) -> list[dict] | None:
    """Return certified identity-only rows, or request the caller's full scan.

    Unsupported servers never open the state directory. Rows contain neither
    bodies nor sender/subject metadata; matching candidates need full fields
    freshly fetched in this call. No partial refresh is persisted or returned. With
    allow_refresh=False and no preview_state, only an exact warm snapshot can
    be returned; missing or stale inventories trigger no data fetches or writes.
    With preview_state, completed raw batches resume in memory, under fresh
    STATUS/UID-set and CHANGEDSINCE guards, and index files remain read-only.
    """
    settings = client.settings
    capabilities = getattr(connection, "capabilities", ())
    if not settings.identity_index_enabled or not any(
        value in ("CONDSTORE", b"CONDSTORE") for value in capabilities
    ):
        return None
    path = Path(settings.identity_index_dir) if settings.identity_index_dir else default_index_dir()
    key = _scope(settings, folder)
    try:
        if preview_state is not None:
            return _preview_inventory(
                client,
                connection,
                deadline,
                folder,
                validity,
                fetch_rows,
                path,
                key,
                preview_state,
            )
        with _storage(path, key, create=allow_refresh) as directory:
            return _refresh(
                client,
                connection,
                deadline,
                folder,
                validity,
                fetch_rows,
                directory,
                key,
                allow_refresh=allow_refresh,
            )
    except CallDeadlineExceeded:
        raise
    except (
        _Uncertified,
        OSError,
        MailError,
        imaplib.IMAP4.error,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        deadline.check(connection)
        return None
