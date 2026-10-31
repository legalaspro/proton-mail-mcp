"""Bound a source query to one UID snapshot while tolerating proven appends."""

from __future__ import annotations

import re
from typing import Any

from .mail import MailError, MailValidationError
from .search import _uidvalidity

SOURCE_INVENTORY_RETRIES = 2


class _ChangedSource(MailValidationError):
    pass


class _ChangedValidity(MailValidationError):
    pass


def _reason(stage: str, folder: str) -> str:
    from .cleanup_lookup import identity_context

    return "Message set changed during " + identity_context(stage, folder)


def _validity(connection: Any, validity: str, stage: str, folder: str) -> None:
    responses = getattr(connection, "untagged_responses", {})
    validities = responses.get("UIDVALIDITY", [validity.encode("ascii")])
    if (
        not isinstance(validities, (list, tuple))
        or not validities
        or any(value != validity.encode("ascii") for value in validities)
        or _uidvalidity(connection) != validity
    ):
        raise _ChangedValidity(_reason(stage, folder))


def _observe(
    connection: Any, state: dict, validity: str, stage: str, folder: str
) -> tuple[int, int]:
    """Read every unsolicited response before a reselect clears its history."""
    _validity(connection, validity, stage, folder)
    responses = getattr(connection, "untagged_responses", {})
    if responses.get("EXPUNGE"):
        raise _ChangedSource(_reason(stage, folder))
    latest = []
    for name, lower in (("EXISTS", state["exists"]), ("UIDNEXT", int(state["uidnext"]))):
        values = responses.get(name)
        if values is not None and (
            not isinstance(values, (list, tuple))
            or not values
            or any(
                not isinstance(value, bytes)
                or not re.fullmatch(rb"[0-9]+", value)
                or int(value) < lower
                for value in values
            )
        ):
            raise _ChangedSource(_reason(stage, folder))
        parsed = [int(value) for value in values] if values is not None else [lower]
        if parsed != sorted(parsed):
            raise _ChangedSource(_reason(stage, folder))
        latest.append(parsed[-1])
    return latest[0], latest[1]


def _low_uids(connection: Any, deadline: Any, upper: int, stage: str, folder: str) -> set[str]:
    """One indexed UID search, with an exact upper bound and strict parsing."""
    if upper < 1:
        return set()
    deadline.check(connection)
    status, data = connection.uid("SEARCH", "CHARSET", "UTF-8", "UID", f"1:{upper}")
    if status not in ("OK", b"OK"):
        raise MailError("Mailbox operation failed")
    if not isinstance(data, (list, tuple)) or len(data) != 1 or not isinstance(data[0], bytes):
        raise _ChangedSource(_reason(stage, folder))
    raw = data[0].split()
    if any(not re.fullmatch(rb"[1-9][0-9]{0,9}", uid) or int(uid) > 4294967295 for uid in raw):
        raise _ChangedSource(_reason(stage, folder))
    values = [uid.decode("ascii") for uid in raw]
    if len(values) != len(set(values)) or any(int(uid) > upper for uid in values):
        raise _ChangedSource(_reason(stage, folder))
    return set(values)


def _growth(state: dict, current: dict, stage: str, folder: str) -> None:
    if current["uidnext"] is None:
        raise _ChangedSource(_reason(stage, folder))
    count_delta = current["exists"] - state["exists"]
    next_delta = int(current["uidnext"]) - int(state["uidnext"])
    # An increased count requires enough new UID slots. A changing UIDNEXT
    # without any surviving append is not evidence of append-only growth.
    if count_delta < 0 or next_delta < count_delta or bool(count_delta) != bool(next_delta):
        raise _ChangedSource(_reason(stage, folder))


def _verify(
    client: Any,
    connection: Any,
    deadline: Any,
    snapshot: dict,
    validity: str,
    uids: set[str],
    stage: str,
    folder: str,
) -> dict:
    observed_count, observed_next = _observe(connection, snapshot, validity, stage, folder)
    current = client._select(connection, folder)
    _observe(connection, snapshot, validity, stage, folder)
    _growth(snapshot, current, stage, folder)
    if current["exists"] < observed_count or int(current["uidnext"]) < observed_next:
        raise _ChangedSource(_reason(stage, folder))
    if (
        snapshot["exists"]
        and _low_uids(connection, deadline, int(snapshot["uidnext"]) - 1, stage, folder) != uids
    ):
        raise _ChangedSource(_reason(stage, folder))
    _observe(connection, snapshot, validity, stage, folder)
    deadline.check(connection)
    return current


def reconcile_source_selection(
    client: Any,
    connection: Any,
    deadline: Any,
    cache: dict,
    state: dict,
    validity: str,
    stage: str,
    folder: str,
) -> bool:
    """Prove that this call's earlier source snapshot still exists on a later visit.

    Other folders and their full identity/membership inventories remain strict.
    The caller still performs its ordinary complete membership inventory after
    this check, including any newly appended rows.
    """
    saved = cache.get((folder, "source_snapshot"))
    if saved is None:
        return False
    snapshot, original_validity, uids = saved
    if validity != original_validity:
        raise _ChangedValidity(_reason(stage, folder))
    _growth(snapshot, state, stage, folder)
    _verify(client, connection, deadline, snapshot, validity, uids, stage, folder)
    return True


def source_inventory(
    client: Any,
    connection: Any,
    deadline: Any,
    folder: str,
    criteria: list[Any],
    literal: bytes | None,
) -> list[dict] | None:
    """Read a source query with two bounded retries for a concurrent move-out."""
    from .cleanup_lookup import _IncompleteIdentityInventory, fetch_rows

    cache = getattr(connection, "_cleanup_inventory", None)
    if cache is None:
        cache = {}
        connection._cleanup_inventory = cache
    stage = "source"
    key = (folder, "source_query", tuple(criteria), literal)
    snapshot_key = (folder, "source_snapshot")
    original_validity = None
    for attempt in range(SOURCE_INVENTORY_RETRIES + 1):
        deadline.check(connection)
        state = client._select(connection, folder)
        validity = _uidvalidity(connection)
        if original_validity is not None and validity != original_validity:
            raise _ChangedValidity(_reason(stage, folder))
        original_validity = validity
        # Without UIDNEXT there is no trustworthy append boundary. Keep the
        # ordinary strict path for peers that do not provide this evidence.
        if state["uidnext"] is None:
            return None
        try:
            _observe(connection, state, validity, stage, folder)
            if snapshot_key in cache:
                snapshot, previous_validity, uids = cache[snapshot_key]
                if validity != previous_validity:
                    raise _ChangedValidity(_reason(stage, folder))
                _growth(snapshot, state, stage, folder)
            else:
                snapshot = state
                uids = (
                    _low_uids(connection, deadline, int(state["uidnext"]) - 1, stage, folder)
                    if state["exists"]
                    else set()
                )
                if len(uids) != state["exists"]:
                    raise _ChangedSource(_reason(stage, folder))
            if key in cache:
                rows = cache[key]
            else:
                candidates = (
                    client._search(connection, criteria, literal=literal)
                    if criteria and snapshot["exists"]
                    else sorted(uids, key=int)
                )
                # The selector can never add arrivals at or above the original
                # UIDNEXT, even if they otherwise match all query fields.
                candidates = sorted(set(candidates) & uids, key=int)
                rows = fetch_rows(
                    client, connection, deadline, folder, candidates, validity, stage=stage
                )
            current = _verify(client, connection, deadline, snapshot, validity, uids, stage, folder)
            cache[snapshot_key] = (snapshot, validity, uids)
            cache[key] = rows
            cache[(folder, "selection_state")] = (current, validity)
            return [dict(row) for row in rows]
        except (_ChangedSource, _IncompleteIdentityInventory):
            # A validity change must never be retried as if this were the same
            # mailbox, including an unsolicited transient change before SELECT.
            _validity(connection, validity, stage, folder)
            for saved_key in list(cache):
                if saved_key[0] == folder:
                    del cache[saved_key]
            if attempt == SOURCE_INVENTORY_RETRIES:
                raise _IncompleteIdentityInventory(
                    _reason(stage, folder) + "; mailbox is changing; retry"
                ) from None
    raise AssertionError("Unreachable source inventory retry state")
