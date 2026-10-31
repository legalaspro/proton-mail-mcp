"""Batched cleanup candidates with exact, unambiguous raw identity evidence."""

from __future__ import annotations

import hashlib
import json
import re
import time
from email import policy
from email.headerregistry import Address
from email.parser import HeaderParser
from functools import partial
from typing import Any

from . import mail
from .mail import CallDeadlineExceeded, MailError, MailValidationError, _message_id
from .search import _uidvalidity
from .sender_matching import sender_snapshot

_IDENTITY_BATCH_SIZE = 200
_IDENTITY_METADATA_BATCH_SIZE = 1000
_PREVIEW_RAW_SNAPSHOT_SECONDS = 600
_PREVIEW_RAW_KEEP_SECONDS = 24 * 3600
_PREVIEW_RAW_MAX_SNAPSHOTS = 128
_PREVIEW_RANGE_MAX_BYTES = 16 * 1024 * 1024


class PreviewExclusions:
    """Item refusals for a new preview only; absence keeps every caller strict."""

    def __init__(self, reasons: dict[str, str] | None = None):
        self.reasons: dict[str, str] = {} if reasons is None else reasons

    def add(self, identifier: str, reason: str) -> None:
        self.reasons.setdefault(identifier, reason)

    def response(self) -> dict:
        ids, size = [], 0
        counts: dict[str, int] = {}
        for identifier, reason in self.reasons.items():
            counts[reason] = counts.get(reason, 0) + 1
            encoded_size = len(json.dumps(identifier, ensure_ascii=True))
            if len(ids) < 20 and size + encoded_size <= 4096:
                ids.append(identifier)
                size += encoded_size
        return {
            "excluded_count": len(self.reasons),
            "excluded_ids": ids,
            "excluded_truncated": len(ids) < len(self.reasons),
            "excluded_reasons": counts,
        }


class _IncompleteIdentityInventory(MailValidationError):
    """A requested UID disappeared or lacked its envelope during the inventory."""


class _PreviewInventoryGrew(_IncompleteIdentityInventory):
    """A preview's snapshot grew under the same mailbox identity."""


def _text(value: Any) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else ""


def _addresses(value: Any, *, required: bool = False) -> tuple[str | None, bool]:
    """Accept UTF-8 display names and ordinary mailboxes; ambiguous data fails closed."""
    if value is None and not required:
        return None, False
    if not isinstance(value, list) or not value:
        return "", True
    addresses = []
    for entry in value:
        if not isinstance(entry, list) or len(entry) != 4:
            return "", True
        name, route, mailbox, host = entry
        # A NIL host denotes group syntax, not a determinable sender. Source
        # routes, encoded words and malformed addr-specs are unknown. Gluon
        # already decodes MIME display names into UTF-8 in the DB envelope.
        if route is not None or not isinstance(mailbox, bytes) or not isinstance(host, bytes):
            return "", True
        if name is not None and not isinstance(name, bytes):
            return "", True
        try:
            display_name = "" if name is None else name.decode("utf-8", errors="strict")
        except UnicodeError:
            return "", True
        if "=?" in display_name or any(not char.isprintable() for char in display_name):
            return "", True
        parts = [mailbox, host]
        if any(
            not part.isascii() or b"=?" in part or any(char < 32 or char == 127 for char in part)
            for part in parts
        ):
            return "", True
        local, domain = mailbox.decode("ascii"), host.decode("ascii")
        if (
            not re.fullmatch(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+", local)
            or any(not part for part in local.split("."))
            or not re.fullmatch(r"[A-Za-z0-9.-]+", domain)
            or "." not in domain
            or any(not part for part in domain.rstrip(".").split("."))
        ):
            return "", True
        addresses.append(str(Address(display_name, local, domain)))
    return ", ".join(addresses), False


def envelope_row(item: dict, uid: str, folder: str, validity: str) -> dict:
    """Keep conservative envelope sender checks in the cleanup snapshot schema."""
    envelope = item.get("envelope")
    if not isinstance(envelope, list) or len(envelope) != 10:
        envelope = [None] * 10
    sender, invalid = _addresses(envelope[2], required=True)
    if isinstance(envelope[2], list) and len(envelope[2]) != 1:
        invalid = True
    # Gluon copies From when Sender/Reply-To is absent. NIL with a nonempty
    # From therefore means a present but unusable header, and must fail closed.
    explicit_sender, sender_invalid = _addresses(envelope[3], required=bool(envelope[2]))
    # RFC Sender is one mailbox. Multiple DB entries cannot identify one
    # responsible sender, including an absent Sender copied from multiple Froms.
    if isinstance(envelope[3], list) and len(envelope[3]) != 1:
        sender_invalid = True
    reply_to, reply_invalid = _addresses(envelope[4], required=bool(envelope[2]))
    recipient, _ = _addresses(envelope[5])
    row = {
        "message_id": _text(envelope[9]),
        "uid": uid,
        "folder": folder,
        "uidvalidity": validity,
        "from": sender or "",
        "sender": explicit_sender,
        "reply_to": reply_to,
        "to": recipient or "",
        "subject": _text(envelope[1]),
        "date": _text(envelope[0]),
        "size": item.get("size"),
        "internaldate": item.get("internaldate"),
        "internaldate_day": item.get("internaldate_day"),
    }
    # Most inventory rows never appear in a sample or log. Keep display-only
    # values raw until they are needed, including by a subject query.
    row["_envelope_display"] = True
    if invalid or sender_invalid or reply_invalid:
        row["sender_invalid"] = True
        row["sender_status"] = "unknown"
    try:
        identifier = envelope[9]
        if not isinstance(identifier, bytes):
            raise MailValidationError("Unavailable Message-ID")
        try:
            identifier.decode("utf-8", errors="strict")
        except UnicodeError:
            raise MailValidationError("Unavailable Message-ID") from None
        row["message_id"] = _message_id(row["message_id"])
    except MailValidationError:
        row["identity_invalid"] = True
    return row


def display_value(row: dict, key: str) -> str:
    """Decode only a requested display field, preserving existing digest formatting."""
    value = row.get(key, "")
    if not row.get("_envelope_display") or key not in ("subject", "date"):
        return value
    needs_decode = (
        "=?" in value
        if key == "subject"
        else not re.fullmatch(
            r"[A-Z][a-z]{2}, [0-9]{2} [A-Z][a-z]{2} [0-9]{4} "
            r"[0-9]{2}:[0-9]{2}:[0-9]{2} [+-][0-9]{4}",
            value,
        )
    )
    if needs_decode and value and not any(char in value for char in "\r\n\x00"):
        parsed = HeaderParser(policy=policy.default).parsestr(f"{key}: {value}\n\n")
        return str(parsed[key])
    return value


def display_row(row: dict) -> dict:
    return {**row, **{key: display_value(row, key) for key in ("subject", "date")}}


def message_fingerprint(row: dict) -> tuple[str, str, int | None]:
    """Stable message evidence, independent of its folder-local UID or flags."""
    return row["message_id"], display_value(row, "date"), row.get("size")


def fetch_rows(
    client: Any,
    connection: Any,
    deadline: Any,
    folder: str,
    uids: list[str],
    validity: str,
    *,
    complete: bool = True,
    stage: str = "source",
    raw_fields: bool = False,
    batch_size: int = _IDENTITY_BATCH_SIZE,
) -> list[dict]:
    seen = getattr(connection, "_cleanup_seen", False)
    rows = []
    for offset in range(0, len(uids), batch_size):
        batch = uids[offset : offset + batch_size]
        metadata = client._metadata(
            connection,
            batch,
            deadline=deadline,
            headers=True,
            identity_headers_only=True,
            bodystructure=False,
            internaldate=True,
            envelope=True,
            size=True,
            flags=seen,
        )
        if complete and set(metadata) != set(batch):
            raise _IncompleteIdentityInventory(
                "Message set changed during " + identity_context(stage, folder)
            )
        batch_rows = [
            envelope_row(metadata[uid], uid, folder, validity) for uid in batch if uid in metadata
        ]
        if _uidvalidity(connection) != validity:
            raise _IncompleteIdentityInventory(
                "Mailbox identity changed during " + identity_context(stage, folder)
            )
        for row in batch_rows:
            headers = metadata[row["uid"]].get("headers")
            # The raw fields use the same parser as read-only sender matching.
            # Matching never relaxes destructive-action validation, including
            # duplicate fields hidden by the envelope and unusable DB senders.
            senders, sender_invalid = sender_snapshot(headers)
            # Neither representation may hide policy-relevant sender evidence
            # from the other. A stale or contradictory DB envelope cannot be
            # replaced with apparently safe raw fields to authorize an action.
            sender_invalid |= any(row[key] != value for key, value in senders.items())
            row.update(senders)
            if sender_invalid:
                row["sender_invalid"] = True
                row["sender_status"] = "unknown"
            # Membership inventories need every occurrence, even an invalid
            # second field hidden by ENVELOPE. Never expose these in snapshots.
            if raw_fields:
                row["_raw_identity_fields"] = raw_identity_fields(headers)
            try:
                identifier = raw_message_id(headers)
            except MailValidationError:
                identifier = None
            # Every consumer must bind the canonical identity to exactly one
            # raw field on the fetched UID. This applies to ordinary cleanup,
            # rules, confirmation, per-UID mutation checks and logged undo.
            if identifier is None or identifier != row["message_id"]:
                row["identity_invalid"] = True
        if seen:
            for row in batch_rows:
                row["seen"] = any(
                    flag.casefold() == r"\seen" for flag in metadata[row["uid"]]["flags"]
                )
        rows.extend(batch_rows)
    return rows


def raw_identity_fields(headers: Any) -> list[str]:
    """Retain all raw occurrences, recovering UTF-8 without losing invalid bytes."""
    if headers is None:
        return []
    return [
        value.encode("utf-8", "surrogateescape").decode("utf-8", "surrogateescape").strip(" ")
        for name, value in headers.raw_items()
        if name.lower() == "message-id"
    ]


def raw_message_id(headers: Any) -> str:
    """Accept one undecoded ID token; duplicate fields and controls remain invalid."""
    values = raw_identity_fields(headers)
    if len(values) != 1:
        raise MailValidationError("Missing or ambiguous raw Message-ID")
    # BytesParser preserves raw octets through surrogateescape. Recover those
    # bytes strictly instead of accepting replacement characters or rejecting
    # valid non-ASCII identifiers that the canonical identity parser supports.
    try:
        identifier = values[0].encode("utf-8", "surrogateescape").decode("utf-8", "strict")
    except UnicodeError:
        raise MailValidationError("Raw Message-ID requires valid UTF-8 text") from None
    return _message_id(identifier)


def _identity_tokens(value: str) -> list[str]:
    # Delimiter-separated identities are invalid for authorization, but still
    # reveal requested IDs hidden alongside another raw/envelope identity.
    return re.findall(r"<[^<>\s]+>|[^<>\s]+", re.sub(r"[,;()]", " ", value))


def _identity_candidates(value: str) -> set[str]:
    """Find canonical occurrences, retaining malformed multi-ID evidence.

    This is discovery only: fetch_rows still requires exactly one valid raw
    field matching its envelope before any identity can authorize an action.
    """
    candidates = set()
    for part in _identity_tokens(value):
        try:
            candidates.add(_message_id(part))
        except (MailValidationError, UnicodeError):
            continue
    return candidates


def _raw_identity_candidates(values: list[str]) -> set[str]:
    # Case-only discrepancies remain unsafe evidence, never equivalent IDs.
    return {candidate.casefold() for value in values for candidate in _identity_candidates(value)}


def _names_requested_identity(values: list[str], folded: set[str]) -> bool:
    """The same answer as _raw_identity_candidates, validating only tokens that could match."""
    for value in values:
        for part in _identity_tokens(value):
            key = part.casefold()
            if key not in folded and f"<{key}>" not in folded:
                continue
            if _raw_identity_candidates([part]) & folded:
                return True
    return False


def _inventory_state(connection: Any, validity: str, stage: str, folder: str) -> dict:
    state = connection._selected_inventory
    responses = getattr(connection, "untagged_responses", {})
    counts = responses.get("EXISTS")
    # Unsolicited counts during SEARCH/FETCH also invalidate the SELECT snapshot,
    # even if a subsequent SELECT would happen to return its original count.
    changed = counts is not None and (
        not isinstance(counts, (list, tuple))
        or not counts
        or any(value != str(state["exists"]).encode("ascii") for value in counts)
    )
    next_values = responses.get("UIDNEXT")
    if next_values is not None:
        changed = (
            changed or state["uidnext"] is None or next_values != [state["uidnext"].encode("ascii")]
        )
    validities = responses.get("UIDVALIDITY")
    if validities is not None:
        changed = (
            changed
            or not isinstance(validities, (list, tuple))
            or not validities
            or any(value != validity.encode("ascii") for value in validities)
        )
    if changed or _uidvalidity(connection) != validity:
        raise _IncompleteIdentityInventory(
            "Message set changed during " + identity_context(stage, folder)
        )
    return state


def _verify_inventory(
    client: Any, connection: Any, state: dict, validity: str, stage: str, folder: str
) -> None:
    resumable = getattr(connection, "_rules_preview_state", None) is not None and (
        stage == "rules discovery" or (stage == "membership" and folder == "All Mail")
    )
    changed = None
    try:
        _inventory_state(connection, validity, stage, folder)
    except _IncompleteIdentityInventory as error:
        if not resumable:
            raise
        responses = getattr(connection, "untagged_responses", {})
        validities = responses.get("UIDVALIDITY")
        counts, next_values = responses.get("EXISTS"), responses.get("UIDNEXT")
        if (
            _uidvalidity(connection) != validity
            or (
                validities is not None
                and (
                    not isinstance(validities, (list, tuple))
                    or not validities
                    or any(value != validity.encode("ascii") for value in validities)
                )
            )
            or any(
                values is not None
                and (
                    not isinstance(values, (list, tuple))
                    or not values
                    or any(
                        not isinstance(value, bytes) or not re.fullmatch(rb"[0-9]{1,10}", value)
                        for value in values
                    )
                )
                for values in (counts, next_values)
            )
            or (counts is not None and any(int(value) < state["exists"] for value in counts))
            or (
                next_values is not None
                and (
                    len(next_values) != 1
                    or state["uidnext"] is None
                    or not int(state["uidnext"]) <= int(next_values[0]) <= 4294967295
                )
            )
        ):
            raise
        # Use the already planned re-SELECT to distinguish an arrival from a
        # UIDNEXT-only change. Never discard a transient UIDVALIDITY mismatch.
        changed = error
    # An empty inventory issued no SEARCH or FETCH after SELECT, so there is
    # no scan to bracket with another round trip.
    if state["exists"] == 0:
        if changed is not None:
            raise changed
        return
    current = client._select(connection, state["folder"])
    _inventory_state(connection, validity, stage, folder)
    if current != state or _uidvalidity(connection) != validity:
        error_type = (
            _PreviewInventoryGrew
            if resumable
            and current["exists"] > state["exists"]
            and (
                state["uidnext"] is None
                or (
                    current["uidnext"] is not None
                    and int(current["uidnext"]) >= int(state["uidnext"])
                )
            )
            else _IncompleteIdentityInventory
        )
        raise error_type("Message set changed during " + identity_context(stage, folder))
    if changed is not None:
        raise changed


def enumerate_uids(
    client: Any, connection: Any, deadline: Any, folder: str, validity: str, stage: str
) -> list[str]:
    """Enumerate existing UIDs only, with SELECT evidence bracketing the command."""
    state = _inventory_state(connection, validity, stage, folder)
    if state["exists"] == 0:
        return []
    deadline.check(connection)
    # ALL cannot name a nonexistent UID. The session's timed proxy bounds this
    # command by the remaining budget and closes an interrupted stream.
    status, data = connection.uid("SEARCH", "CHARSET", "UTF-8", "ALL")
    # Never infer emptiness from a server error, including "no such message":
    # enumeration is only reached with a positive, authoritative SELECT count.
    if status not in ("OK", b"OK"):
        if (
            status in ("NO", b"NO")
            and isinstance(data, (list, tuple))
            and any(
                isinstance(value, bytes) and b"no such message" in value.lower() for value in data
            )
        ):
            raise _IncompleteIdentityInventory(
                "Message set changed during " + identity_context(stage, folder)
            )
        # Preserve transport/server failures as retryable mailbox errors, rather
        # than letting undo mistake them for a definitive identity mismatch.
        raise MailError("Mailbox operation failed")
    if not isinstance(data, (list, tuple)) or len(data) != 1 or not isinstance(data[0], bytes):
        raise _IncompleteIdentityInventory("Mailbox returned an incomplete UID inventory")
    values = data[0].split()
    if any(
        not re.fullmatch(rb"[1-9][0-9]{0,9}", value) or int(value) > 4294967295 for value in values
    ):
        raise _IncompleteIdentityInventory("Mailbox returned invalid message identifiers")
    uids = [value.decode("ascii") for value in values]
    if len(uids) != len(set(uids)) or len(uids) != state["exists"]:
        raise _IncompleteIdentityInventory(
            "Message set changed during " + identity_context(stage, folder)
        )
    _verify_inventory(client, connection, state, validity, stage, folder)
    deadline.check(connection)
    return sorted(uids, key=int)


def identity_search(
    client: Any,
    connection: Any,
    identifiers: list[str],
    *,
    deadline: Any,
    folder: str,
    validity: str,
    stage: str,
    exclusions: PreviewExclusions | None = None,
) -> list[str]:
    """Discover canonical identities from DB envelopes without literal SEARCH.

    Dates cannot safely narrow membership: an old copy with the same ID still
    has to protect the candidate. Inventory every UID's cheap ENVELOPE instead,
    and fetch raw headers only for matching canonical identities. Rules require
    stronger raw-field completeness and take a separate path below.
    """
    cache = connection._cleanup_inventory
    key = (folder, validity, ("identity_envelopes",))
    if key not in cache:
        uids = enumerate_uids(client, connection, deadline, folder, validity, stage)
        rows = []
        for offset in range(0, len(uids), _IDENTITY_METADATA_BATCH_SIZE):
            batch = uids[offset : offset + _IDENTITY_METADATA_BATCH_SIZE]
            metadata = client._metadata(
                connection,
                batch,
                deadline=deadline,
                headers=False,
                bodystructure=False,
                internaldate=False,
                envelope=True,
            )
            if set(metadata) != set(batch) or _uidvalidity(connection) != validity:
                raise _IncompleteIdentityInventory(
                    "Message set changed during " + identity_context(stage, folder)
                )
            for uid in batch:
                envelope = metadata[uid].get("envelope")
                identifier = (
                    _text(envelope[9]) if isinstance(envelope, list) and len(envelope) == 10 else ""
                )
                rows.append({"uid": uid, "message_id": identifier, "folder": folder})
        cache[key] = rows
    return sorted(
        {
            row["uid"]
            for row in matching_rows(
                cache[key], identifiers, stage=stage, folder=folder, exclusions=exclusions
            )
        },
        key=int,
    )


# Identity inventories use larger PEEK batches with the same parser and deadlines.
fetch_index_rows = partial(fetch_rows, batch_size=_IDENTITY_METADATA_BATCH_SIZE)


def indexed_identity_uids(
    client: Any,
    connection: Any,
    deadline: Any,
    folder: str,
    validity: str,
    identifiers: list[str],
    *,
    stage: str,
    exclusions: PreviewExclusions | None = None,
) -> list[str] | None:
    """Use a certified occurrence inventory and current-call candidate evidence.

    The persistent index is a discovery aid, never authorization evidence.
    Its minimal rows cannot replace sender, fingerprint, flags or raw identity
    rechecks. The connection cache lasts only for this resolution and is reset
    at confirmation and after each mutation, like the full-scan inventories.
    """
    from .identity_index import certified_inventory

    cache = connection._cleanup_inventory
    key = (folder, validity, ("certified_identity_index",))
    if key not in cache:
        reserve = getattr(deadline, "response_reserve", 0)
        try:
            deadline.response_reserve = max(
                reserve, getattr(deadline, "rules_response_reserve", reserve)
            )
            cache[key] = certified_inventory(
                client,
                connection,
                deadline,
                folder,
                validity,
                fetch_rows=fetch_index_rows,
                allow_refresh=getattr(connection, "_rules_preview_state", None) is None,
                preview_state=getattr(connection, "_rules_preview_state", None),
            )
        finally:
            deadline.response_reserve = reserve
    inventory = cache[key]
    if inventory is None:
        return None
    wanted = set(identifiers)
    folded = {identifier.casefold() for identifier in identifiers}
    raw_identity = getattr(connection, "_cleanup_raw_identity", False)
    envelope_matches = {
        row["uid"]
        for row in (
            [row for row in inventory if row["message_id"] in wanted]
            if raw_identity
            else matching_rows(
                inventory, identifiers, stage=stage, folder=folder, exclusions=exclusions
            )
        )
    }
    uids = []
    for row in inventory:
        raw_candidates = _raw_identity_candidates(row["_raw_identity_fields"])
        raw_match = bool(raw_candidates & folded)
        if row["uid"] not in envelope_matches and not raw_match:
            continue
        # A requested identity hidden in a second field must refuse even when
        # the envelope identifies a different message. Do not let downstream
        # filtering discard that contradictory evidence.
        if row.get("identity_invalid"):
            if exclusions is not None:
                for identifier in identifiers:
                    if identifier.casefold() in raw_candidates:
                        exclusions.add(identifier, "invalid_identity")
                uids.append(row["uid"])
                continue
            if raw_identity or row["uid"] in envelope_matches:
                # Preserve per-row unresolved handling after the mandatory fresh
                # fetch. Rules also retain an unrelated envelope when a hidden raw
                # field matched; ordinary matching would discard it, so refuse below.
                uids.append(row["uid"])
                continue
            raise MailValidationError(
                "Missing or ambiguous raw Message-ID during " + identity_context(stage, folder)
            )
        if row["message_id"] in wanted:
            uids.append(row["uid"])
    return sorted(uids, key=int)


def _range_snapshot_seconds(client: Any) -> int:
    """How long the All Mail audit stays reusable after each write; every use revalidates it."""
    if client.settings.background_sync_active:
        return _PREVIEW_RAW_KEEP_SECONDS
    return _PREVIEW_RAW_SNAPSHOT_SECONDS


def drop_expired_raw_snapshots(client: Any) -> None:
    """Free expired audits even when no later preview would, with no mailbox I/O."""
    snapshots = getattr(client, "_rules_raw_identity_snapshots", None)
    if not snapshots:
        return
    now = time.monotonic()
    for key, entry in list(snapshots.items()):
        if entry["expires"] <= now and snapshots.get(key) is entry:
            snapshots.pop(key, None)


def _rules_audit_state(connection: Any) -> dict | None:
    """Share immutable discovery evidence without enabling preview behavior at apply."""
    preview = getattr(connection, "_rules_preview_state", None)
    return preview if preview is not None else getattr(connection, "_rules_apply_state", None)


def _preview_raw_inventory(
    client: Any,
    connection: Any,
    deadline: Any,
    folder: str,
    validity: str,
    stage: str,
    *,
    uid_ranges: bool = False,
) -> list[dict]:
    # Bulk discovery is resumable, so it must retain the normal reply reserve
    # even when the singleton's final approval may use that time.
    reserve = getattr(deadline, "response_reserve", 0)
    try:
        deadline.response_reserve = max(
            reserve, getattr(deadline, "rules_response_reserve", reserve)
        )
        audit = _preview_range_inventory if uid_ranges else _preview_raw_inventory_rows
        return audit(client, connection, deadline, folder, validity, stage)
    finally:
        deadline.response_reserve = reserve


def _preview_raw_inventory_rows(
    client: Any,
    connection: Any,
    deadline: Any,
    folder: str,
    validity: str,
    stage: str,
) -> list[dict]:
    """Reconcile immutable raw identities against each fresh complete UID list."""
    preview_state = _rules_audit_state(connection)
    # Batch size and ordering do not change the raw occurrence audit. Bind
    # reusable progress to its source, rules digest and read-state option.
    options = preview_state.get("options")
    options = options[:3] if options is not None else None
    inventories = preview_state.setdefault("inventories", {})
    saved = inventories.get(folder)
    valid_saved = _valid_preview_inventory(saved, options, folder)
    discarded = saved is not None and not valid_saved
    if valid_saved and saved["validity"] != validity:
        raise MailValidationError("Rules preview mailbox changed; preview again")
    if not valid_saved:
        # A checkpoint is only discovery evidence. Corruption must never hide
        # an occurrence from the fresh candidate and policy checks below.
        saved = None
    snapshots = None
    now = time.monotonic()
    if getattr(client, "settings", None) is not None:
        from .identity_index import _scope

        key = _scope(client.settings, folder)
        snapshots = getattr(client, "_rules_raw_identity_snapshots", None)
        if snapshots is None:
            snapshots = client._rules_raw_identity_snapshots = {}
        for scope, snapshot in list(snapshots.items()):
            if snapshot["expires"] <= now:
                snapshots.pop(scope, None)
        if saved is None and not discarded:
            cached = snapshots.get(key, {}).get("inventory")
            # A fresh preview may rebuild after a mailbox identity reset. An
            # active preview above must refuse because its source rows changed.
            if _valid_preview_inventory(cached, options, folder) and cached["validity"] == validity:
                saved = {**cached, "uids": list(cached["uids"]), "rows": list(cached["rows"])}
    selected = getattr(connection, "_selected_inventory", None)
    snapshot = (
        dict(_inventory_state(connection, validity, stage, folder))
        if selected is not None
        else None
    )
    # UIDVALIDITY binds immutable message contents to each UID. Arrivals and
    # expunges therefore need only a delta audit, even without CONDSTORE.
    # A completed UID enumeration is itself progress: repeating a slow SEARCH
    # before every partial audit can otherwise consume every call's budget.
    reuse_uids = (
        saved is not None
        and snapshot is not None
        and snapshot["uidnext"] is not None
        and saved.get("snapshot") == snapshot
    )
    uids = (
        list(saved["uids"])
        if reuse_uids
        else enumerate_uids(client, connection, deadline, folder, validity, stage)
    )
    if not reuse_uids and snapshot is not None and snapshot["uidnext"] is not None:
        preview_state["progress"] = preview_state.get("progress", 0) + 1
    if saved is None:
        saved = {"validity": validity, "uids": uids, "rows": []}
    else:
        live = set(uids)
        saved["rows"] = [row for row in saved["rows"] if row["uid"] in live]
        saved["uids"] = uids
    saved.update(snapshot=snapshot, options=options)
    inventories[folder] = saved

    def checkpoint():
        saved["checksum"] = _preview_inventory_checksum(saved)
        if snapshots is not None:
            while len(snapshots) >= _PREVIEW_RAW_MAX_SNAPSHOTS and key not in snapshots:
                snapshots.pop(next(iter(snapshots)))
            snapshots[key] = {
                "expires": now + _PREVIEW_RAW_SNAPSHOT_SECONDS,
                "inventory": {**saved, "uids": list(uids), "rows": list(saved["rows"])},
            }

    try:
        known = {row["uid"] for row in saved["rows"]}
        pending = [uid for uid in uids if uid not in known]
        batch_size = max(
            1,
            min(
                saved.get("batch_size", _IDENTITY_BATCH_SIZE),
                _IDENTITY_BATCH_SIZE // preview_state.get("batch_scale", 1),
            ),
        )
        for offset in range(0, len(pending), batch_size):
            deadline.check(connection)
            started = mail.time.monotonic()
            batch = pending[offset : offset + batch_size]
            try:
                rows = fetch_rows(
                    client,
                    connection,
                    deadline,
                    folder,
                    batch,
                    validity,
                    stage=stage,
                    raw_fields=True,
                )
            except CallDeadlineExceeded:
                # Retain a smaller retry size even when the next call omits its
                # cursor. No rows from an interrupted FETCH become evidence.
                if reuse_uids and len(saved["rows"]) == len(known):
                    saved["batch_size"] = max(1, len(batch) // 4)
                raise
            # This call has just fetched complete candidate evidence, including
            # flags when requested. Reuse it below rather than immediately issuing
            # a second FETCH for matching UIDs. Only identity fields survive in the
            # resumable snapshot; a later call still reads candidate fields afresh.
            fresh_key = (
                folder,
                validity,
                ("raw_identity_fresh", bool(getattr(connection, "_cleanup_seen", False))),
            )
            cache = getattr(connection, "_cleanup_inventory", None)
            if cache is not None:
                cache.setdefault(fresh_key, {}).update({row["uid"]: dict(row) for row in rows})
            # Save only discovery evidence before checking whether another batch
            # fits. Later calls still fetch sender, fingerprint and flags afresh.
            saved["rows"].extend(
                {key: row[key] for key in ("uid", "message_id", "_raw_identity_fields")}
                | {"identity_invalid": bool(row.get("identity_invalid"))}
                for row in rows
            )
            preview_state["progress"] = preview_state.get("progress", 0) + len(rows)
            # Rules batch sizing excludes these one-time audit reads.
            preview_state["audit_seconds"] = (
                preview_state.get("audit_seconds", 0) + mail.time.monotonic() - started
            )
            deadline.check(connection)
        saved["rows"].sort(key=lambda row: int(row["uid"]))
        return saved["rows"]
    finally:
        # Publish the bracketed enumeration and completed batches once per call,
        # even if the first FETCH times out. The cache also serves cursorless calls.
        checkpoint()


def _preview_range_inventory(
    client: Any, connection: Any, deadline: Any, folder: str, validity: str, stage: str
) -> list[dict]:
    """Audit raw occurrences in checkpointed UIDNEXT-derived batches."""
    from .identity_index import _scope
    from .search import _coverage_status

    preview = _rules_audit_state(connection)
    options = preview.get("options")
    options = options[:3] if options is not None else None
    scope = _scope(client.settings, folder)
    selected = dict(_inventory_state(connection, validity, stage, folder))
    snapshot = dict(selected)
    if snapshot["exists"] == 0 and snapshot["uidnext"] is None:
        snapshot["uidnext"] = "1"
    elif snapshot["uidnext"] is None:
        # Some peers omit the SELECT response code but expose the same small
        # allocation snapshot through STATUS. Never substitute SEARCH ALL.
        deadline.check(connection)
        status_validity, count, next_uid = _coverage_status(connection, folder)
        if status_validity != validity or count != snapshot["exists"]:
            raise _IncompleteIdentityInventory("Mailbox changed during rules preview")
        snapshot["uidnext"] = next_uid
    upper = int(snapshot["uidnext"])
    if not 1 <= upper <= 4294967295:
        raise _IncompleteIdentityInventory("Mailbox returned invalid UIDNEXT")
    inventories = preview.setdefault("inventories", {})
    saved = inventories.get(folder)
    valid_saved = _valid_preview_range_inventory(saved, options, folder, scope)
    discarded = saved is not None and not valid_saved
    if valid_saved and saved["validity"] != validity:
        raise MailValidationError("Rules preview mailbox changed; preview again")
    if not valid_saved:
        saved = None
    snapshots = getattr(client, "_rules_raw_identity_snapshots", None)
    if snapshots is None:
        snapshots = client._rules_raw_identity_snapshots = {}
    now = time.monotonic()
    for key, cached in list(snapshots.items()):
        if cached["expires"] <= now:
            snapshots.pop(key, None)
    if saved is None and not discarded:
        cached = snapshots.get(scope, {}).get("inventory")
        if (
            _valid_preview_range_inventory(cached, options, folder, scope)
            and cached["validity"] == validity
            # A kept audit from a higher UIDNEXT would refuse every fresh preview for 24 h.
            and not (
                client.settings.background_sync_active
                and snapshot["exists"]
                and int(cached["snapshot"]["uidnext"]) > upper
            )
        ):
            saved = {**cached, "rows": list(cached["rows"]), "live_uids": list(cached["live_uids"])}
    reused = saved is not None and saved["snapshot"] == snapshot
    if saved is None:
        saved = {
            "validity": validity,
            "scope": scope,
            "options": options,
            "snapshot": snapshot,
            "rows": [],
            "live_uids": [],
            "next_uid": 1,
            "batch_size": _IDENTITY_BATCH_SIZE,
        }
    elif saved["snapshot"] != snapshot:
        previous_upper = int(saved["snapshot"]["uidnext"])
        if snapshot["exists"] and previous_upper > upper:
            raise MailValidationError("Rules preview mailbox changed; preview again")
        active = client.settings.background_sync_active
        if (
            active
            and (saved["next_uid"] == previous_upper or "tail_only" in saved)
            and snapshot["exists"] > saved["snapshot"]["exists"]
        ):
            # A finished audit of a mailbox that grew needs only its new UIDs. The
            # count check after the walk catches an expunge hidden by those arrivals.
            saved.update(snapshot=snapshot, tail_only=True)
        elif snapshot["exists"] - saved["snapshot"]["exists"] == upper - previous_upper:
            # Every UID allocated since then is still here, so no row the walk passed
            # has left. Keep the place; the new UIDs come at the end of the range.
            saved["snapshot"] = snapshot
        else:
            # Re-enumerate live UIDs in bounded metadata batches. Immutable raw
            # rows survive arrivals/expunges; only newly discovered UIDs need headers.
            if saved["next_uid"] > 1:
                preview["all_mail_rewinds"] = preview.get("all_mail_rewinds", 0) + 1
            saved.pop("tail_only", None)
            saved.update(snapshot=snapshot, live_uids=[], next_uid=1)
        if active:
            # A kept audit must not pass an old outlier's narrow retries to a new walk.
            saved["batch_size"] = _IDENTITY_BATCH_SIZE
    if snapshot["exists"] == 0:
        saved.update(rows=[], live_uids=[], next_uid=upper)
    inventories[folder] = saved

    def checkpoint():
        encoded = json.dumps(
            {key: value for key, value in saved.items() if key != "checksum"},
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        saved["checksum"] = hashlib.sha256(encoded).hexdigest()
        if len(json.dumps(saved, ensure_ascii=True, separators=(",", ":")).encode("ascii")) > (
            _PREVIEW_RANGE_MAX_BYTES
        ):
            inventories.pop(folder, None)
            snapshots.pop(scope, None)
            raise MailValidationError("Rules preview identity inventory exceeds its size limit")
        while len(snapshots) >= _PREVIEW_RAW_MAX_SNAPSHOTS and scope not in snapshots:
            snapshots.pop(next(iter(snapshots)))
        snapshots[scope] = {
            "expires": now + _range_snapshot_seconds(client),
            "inventory": {
                **saved,
                "rows": list(saved["rows"]),
                "live_uids": list(saved["live_uids"]),
            },
        }

    def enumerate_again():
        # A tail that disagrees enumerates once, in place. Its rows stay, so this
        # reads no headers again, and a later mismatch still refuses.
        del saved["tail_only"]
        saved.update(live_uids=[], next_uid=1)
        checkpoint()

    def hidden_expunge():
        if "tail_only" not in saved or len(saved["live_uids"]) == snapshot["exists"]:
            return False
        enumerate_again()
        return True

    known = {row["uid"]: row for row in saved["rows"]}
    checkpoint()
    completed_batch = False
    tail = None
    try:
        while saved["next_uid"] < upper or hidden_expunge():
            deadline.check(connection)
            started = mail.time.monotonic()
            if tail is None and "tail_only" in saved:
                # An old expunge can cancel out a skipped row in the final count, so each
                # call lists what is left of its tail once and holds its FETCHes to that.
                tail = set(client._search(connection, ["UID", f"{saved['next_uid']}:{upper - 1}"]))
            size = max(
                1, min(saved["batch_size"], _IDENTITY_BATCH_SIZE // preview.get("batch_scale", 1))
            )
            end = min(upper, saved["next_uid"] + size)
            batch = [str(uid) for uid in range(saved["next_uid"], end)]
            try:
                if any(uid in known for uid in batch):
                    # A changed allocation can include expunged cached rows. Fetch only
                    # UID metadata to reconcile them without rereading their headers.
                    live = client._metadata(
                        connection,
                        batch,
                        deadline=deadline,
                        headers=False,
                        bodystructure=False,
                        internaldate=False,
                    )
                    missing = [uid for uid in batch if uid in live and uid not in known]
                else:
                    live, missing = None, batch
                rows = fetch_rows(
                    client,
                    connection,
                    deadline,
                    folder,
                    missing,
                    validity,
                    complete=False,
                    stage=stage,
                    raw_fields=True,
                )
            except CallDeadlineExceeded:
                # One cold outlier must not permanently fragment a normally fast
                # audit. Narrow retries only after an unchanged checkpoint is reused.
                if reused and not completed_batch:
                    saved["batch_size"] = max(1, len(batch) // 4)
                    checkpoint()
                raise
            if live is not None and {row["uid"] for row in rows} != set(missing):
                raise _IncompleteIdentityInventory("Mailbox returned an incomplete UID inventory")
            returned = live if live is not None else [row["uid"] for row in rows]
            if "tail_only" in saved and tail.intersection(batch) != set(returned):
                enumerate_again()
                continue
            fresh_key = (
                folder,
                validity,
                ("raw_identity_fresh", bool(getattr(connection, "_cleanup_seen", False))),
            )
            connection._cleanup_inventory.setdefault(fresh_key, {}).update(
                {row["uid"]: dict(row) for row in rows}
            )
            known.update(
                {
                    row["uid"]: {
                        key: row[key] for key in ("uid", "message_id", "_raw_identity_fields")
                    }
                    | {"identity_invalid": bool(row.get("identity_invalid"))}
                    for row in rows
                }
            )
            saved["live_uids"].extend(returned)
            saved.update(rows=list(known.values()), next_uid=end)
            preview["progress"] = preview.get("progress", 0) + 1
            preview["audit_seconds"] = (
                preview.get("audit_seconds", 0) + mail.time.monotonic() - started
            )
            # A fully returned batch survives even if that command used the rest
            # of the call window. No candidate is authorized by this saved evidence.
            completed_batch = True
            deadline.check(connection)
    finally:
        # Publish completed walk batches once per call, including on a deadline.
        if completed_batch:
            checkpoint()
    live = set(saved["live_uids"])
    if len(live) != snapshot["exists"]:
        saved.update(live_uids=[], next_uid=1)
        checkpoint()
        raise _IncompleteIdentityInventory("Mailbox returned an incomplete UID inventory")
    saved.pop("tail_only", None)
    saved["rows"] = sorted(
        (row for uid, row in known.items() if uid in live), key=lambda row: int(row["uid"])
    )
    checkpoint()
    if selected["uidnext"] is None and snapshot["exists"]:
        deadline.check(connection)
        if _coverage_status(connection, folder) != (validity, snapshot["exists"], str(upper)):
            raise _IncompleteIdentityInventory("Mailbox changed during rules preview")
    return saved["rows"]


def _valid_preview_range_inventory(saved: Any, options: Any, folder: str, scope: str) -> bool:
    from .identity_index import _valid_row

    try:
        if not isinstance(saved, dict) or set(saved) - {"tail_only"} != {
            "validity",
            "scope",
            "options",
            "snapshot",
            "rows",
            "live_uids",
            "next_uid",
            "batch_size",
            "checksum",
        }:
            return False
        snapshot, rows, live = saved["snapshot"], saved["rows"], saved["live_uids"]
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != {"folder", "exists", "uidnext"}
            or snapshot["folder"] != folder
            or type(snapshot["exists"]) is not int
            or snapshot["exists"] < 0
            or not isinstance(snapshot["uidnext"], str)
            or not re.fullmatch(r"[1-9][0-9]{0,9}", snapshot["uidnext"])
        ):
            return False
        upper = int(snapshot["uidnext"])
        return (
            upper <= 4294967295
            and saved["options"] == options
            and saved["scope"] == scope
            and isinstance(saved["validity"], str)
            and re.fullmatch(r"[1-9][0-9]*", saved["validity"]) is not None
            and saved["checksum"] == _preview_inventory_checksum(saved)
            and len(json.dumps(saved, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
            <= _PREVIEW_RANGE_MAX_BYTES
            and type(saved["next_uid"]) is int
            and 1 <= saved["next_uid"] <= upper
            and type(saved["batch_size"]) is int
            and 1 <= saved["batch_size"] <= _IDENTITY_BATCH_SIZE
            and isinstance(rows, list)
            and all(_valid_row(row, row["uid"], upper) for row in rows)
            and len({row["uid"] for row in rows}) == len(rows)
            and saved.get("tail_only", True) is True
            and isinstance(live, list)
            and len(live) == len(set(live))
            # Kept rows can outnumber EXISTS only until a tail walk's final count check.
            and (len(live) <= snapshot["exists"] or "tail_only" in saved)
            and set(live) <= {row["uid"] for row in rows}
            and all(int(uid) < saved["next_uid"] for uid in live)
        )
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        return False


def _preview_inventory_checksum(saved: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            {key: value for key, value in saved.items() if key != "checksum"},
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


def _valid_preview_inventory(saved: Any, options: Any, folder: str) -> bool:
    """Reject malformed or altered discovery progress before skipping any UID."""
    from .identity_index import _valid_row

    try:
        if not isinstance(saved, dict) or not (
            {"validity", "uids", "rows", "snapshot", "options", "checksum"}
            <= saved.keys()
            <= {"validity", "uids", "rows", "snapshot", "options", "checksum", "batch_size"}
        ):
            return False
        uids, rows, snapshot = saved["uids"], saved["rows"], saved["snapshot"]
        if snapshot is not None and (
            not isinstance(snapshot, dict)
            or set(snapshot) != {"folder", "exists", "uidnext"}
            or snapshot["folder"] != folder
            or type(snapshot["exists"]) is not int
            or snapshot["exists"] < 0
            or (
                snapshot["uidnext"] is not None
                and (
                    not isinstance(snapshot["uidnext"], str)
                    or not re.fullmatch(r"[1-9][0-9]{0,9}", snapshot["uidnext"])
                    or int(snapshot["uidnext"]) > 4294967295
                )
            )
        ):
            return False
        next_uid = int(snapshot["uidnext"]) if snapshot and snapshot["uidnext"] else 4294967296
        return (
            saved["options"] == options
            and isinstance(saved["validity"], str)
            and re.fullmatch(r"[1-9][0-9]*", saved["validity"]) is not None
            and saved["checksum"] == _preview_inventory_checksum(saved)
            and isinstance(uids, list)
            and all(
                isinstance(uid, str)
                and re.fullmatch(r"[1-9][0-9]{0,9}", uid)
                and int(uid) < next_uid
                for uid in uids
            )
            and len(set(uids)) == len(uids)
            and (snapshot is None or len(uids) == snapshot["exists"])
            and isinstance(rows, list)
            and type(saved.get("batch_size", 1)) is int
            and 1 <= saved.get("batch_size", 1) <= _IDENTITY_BATCH_SIZE
            and all(_valid_row(row, row["uid"], next_uid) for row in rows)
            and len({row["uid"] for row in rows}) == len(rows)
            and {row["uid"] for row in rows} <= set(uids)
        )
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        return False


def _candidate_rows(
    client: Any,
    connection: Any,
    deadline: Any,
    folder: str,
    uids: list[str],
    validity: str,
    *,
    stage: str,
    raw_fields: bool,
) -> list[dict]:
    fresh_key = (
        folder,
        validity,
        ("raw_identity_fresh", bool(getattr(connection, "_cleanup_seen", False))),
    )
    fresh = connection._cleanup_inventory.get(fresh_key, {})
    missing = [uid for uid in uids if uid not in fresh]
    fetched = (
        fetch_rows(
            client,
            connection,
            deadline,
            folder,
            missing,
            validity,
            stage=stage,
            raw_fields=raw_fields,
        )
        if missing
        else []
    )
    complete = {**fresh, **{row["uid"]: row for row in fetched}}
    return [dict(complete[uid]) for uid in uids]


def folder_inventory(
    client: Any,
    connection: Any,
    deadline: Any,
    folder: str,
    *,
    stage: str = "source",
    criteria: list[Any] | None = None,
    literal: bytes | None = None,
    identifiers: list[str] | None = None,
    exclusions: PreviewExclusions | None = None,
) -> list[dict]:
    """Cache only the requested candidate inventory for this connection and mailbox."""
    if stage == "source" and criteria is not None and identifiers is None:
        from .source_inventory import source_inventory

        rows = source_inventory(client, connection, deadline, folder, criteria, literal)
        if rows is not None:
            return rows
    client._select(connection, folder)
    validity = _uidvalidity(connection)
    state = _inventory_state(connection, validity, stage, folder)
    cache = getattr(connection, "_cleanup_inventory", None)
    if cache is None:
        cache = {}
        connection._cleanup_inventory = cache
    state_key = (folder, "selection_state")
    if state_key in cache and cache[state_key] != (state, validity):
        from .source_inventory import reconcile_source_selection

        if not reconcile_source_selection(
            client, connection, deadline, cache, state, validity, stage, folder
        ):
            raise _IncompleteIdentityInventory(
                "Message set changed during " + identity_context(stage, folder)
            )
    cache[state_key] = (state, validity)
    scope = (
        ("ids", *sorted(set(identifiers)))
        if identifiers is not None
        else ("query", *(criteria or ["ALL"]), literal)
    )
    key = (folder, validity, scope)
    if key not in cache:
        audit_state = _rules_audit_state(connection)
        if getattr(connection, "_rules_preview_state", None) is None and state["uidnext"] is None:
            # Execute needs an allocation witness from this selection to reuse
            # an audit. Peers without it retain the original full raw reread.
            audit_state = None
        if (
            audit_state is not None
            and state["exists"] == 0
            and identifiers is not None
            and getattr(connection, "_cleanup_raw_identity", False)
        ):
            # Empty selections need no membership FETCH, but still reconcile
            # expunges and reject a UIDVALIDITY reset of a resumed raw audit.
            _preview_raw_inventory(
                client,
                connection,
                deadline,
                folder,
                validity,
                stage,
                uid_ranges=folder == "All Mail" and stage == "membership",
            )
        # An authoritative empty SELECT needs no scan. In particular, a newer
        # STATUS snapshot must not start indexed discovery after this selection:
        # the final inventory check skips reselecting an untouched empty view.
        indexed_uids = (
            []
            if state["exists"] == 0
            else indexed_identity_uids(
                client,
                connection,
                deadline,
                folder,
                validity,
                identifiers,
                stage=stage,
                exclusions=exclusions,
            )
            if identifiers is not None
            else None
        )
        if indexed_uids is not None:
            cache[key] = _candidate_rows(
                client,
                connection,
                deadline,
                folder,
                indexed_uids,
                validity,
                stage=stage,
                raw_fields=exclusions is not None,
            )
        elif identifiers is not None and getattr(connection, "_cleanup_raw_identity", False):
            raw_key = (folder, validity, ("raw_identity_audit",))
            if raw_key not in cache:
                # ENVELOPE exposes only the first raw field. To preserve rules'
                # hidden-second-field refusal, audit raw IDs across the whole
                # mailbox in bounded PEEK batches, or fail closed on deadline.
                if audit_state is not None:
                    cache[raw_key] = _preview_raw_inventory(
                        client,
                        connection,
                        deadline,
                        folder,
                        validity,
                        stage,
                        uid_ranges=folder == "All Mail" and stage == "membership",
                    )
                else:
                    uids = enumerate_uids(client, connection, deadline, folder, validity, stage)
                    cache[raw_key] = fetch_rows(
                        client,
                        connection,
                        deadline,
                        folder,
                        uids,
                        validity,
                        stage=stage,
                        raw_fields=True,
                    )
            wanted = set(identifiers)
            folded = {identifier.casefold() for identifier in identifiers}
            matches = [
                row
                for row in cache[raw_key]
                if _names_requested_identity(row.get("_raw_identity_fields", []), folded)
                or row["message_id"] in wanted
            ]
            if audit_state is not None:
                cache[key] = _candidate_rows(
                    client,
                    connection,
                    deadline,
                    folder,
                    [row["uid"] for row in matches],
                    validity,
                    stage=stage,
                    raw_fields=True,
                )
            else:
                cache[key] = matches
        else:
            if identifiers is not None:
                uids = identity_search(
                    client,
                    connection,
                    identifiers,
                    deadline=deadline,
                    folder=folder,
                    validity=validity,
                    stage=stage,
                    exclusions=exclusions,
                )
            elif state["exists"] == 0:
                uids = []
            elif not criteria or criteria == ["ALL"]:
                uids = enumerate_uids(client, connection, deadline, folder, validity, stage)
            else:
                uids = sorted(
                    set(client._search(connection, criteria or ["ALL"], literal=literal)), key=int
                )
            cache[key] = fetch_rows(
                client,
                connection,
                deadline,
                folder,
                uids,
                validity,
                stage=stage,
                raw_fields=exclusions is not None,
            )
        _verify_inventory(client, connection, state, validity, stage, folder)
    deadline.check(connection)
    return [
        {
            name: value
            for name, value in row.items()
            if name != "_raw_identity_fields" or exclusions is not None
        }
        for row in cache[key]
    ]


def identity_context(stage: str, folder: str) -> str:
    """Bound and quote folder names without disclosing message identity or content."""
    name = folder[:64] + ("..." if len(folder) > 64 else "")
    return stage + " in " + json.dumps(name, ensure_ascii=True)


def matching_rows(
    rows: list[dict],
    identifiers: list[str],
    *,
    stage: str = "source",
    folder: str | None = None,
    raw_identity: bool = False,
    exclusions: PreviewExclusions | None = None,
) -> list[dict]:
    wanted = set(identifiers)
    if raw_identity:
        # Every invalid candidate must reach the ordinary unresolved path,
        # including a second raw field hidden by ENVELOPE's first value.
        return [
            dict(row) for row in rows if row.get("identity_invalid") or row["message_id"] in wanted
        ]
    matches = []
    for original in rows:
        identifier = original["message_id"]
        found = _identity_candidates(identifier) & wanted
        if exclusions is not None and original.get("identity_invalid"):
            raw_candidates = _raw_identity_candidates(original.get("_raw_identity_fields", []))
            found.update(
                candidate for candidate in wanted if candidate.casefold() in raw_candidates
            )
        if len(found) > 1:
            if exclusions is None:
                raise MailValidationError(
                    "Message-ID is ambiguous during "
                    + identity_context(stage, original["folder"] if folder is None else folder)
                )
            for candidate in sorted(found):
                exclusions.add(candidate, "ambiguous_identity")
        # Gluon shares one envelope between mailbox copies. An unrelated
        # unusable identity therefore cannot be a copy of a requested identity.
        # Retain matching invalid identities so source/membership checks block.
        for candidate in sorted(found):
            row = dict(original)
            row.pop("_raw_identity_fields", None)
            row["message_id"] = candidate
            matches.append(row)
    return matches
