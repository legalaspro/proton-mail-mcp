"""Bounded, read-only updates of an already populated header index."""

from __future__ import annotations

import imaplib
import logging
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any

_SEARCH_RESERVE_SECONDS = 5.0
_LOG = logging.getLogger(__name__)


@dataclass
class HeaderSync:
    complete: bool = False
    inventory: list[str] | None = None
    fetched: set[str] = field(default_factory=set)
    timed_out: bool = False
    stream_broken: bool = False


def _reserve(client: Any) -> float:
    return min(_SEARCH_RESERVE_SECONDS, client.settings.call_timeout / 4)


def has_time(client: Any, deadline: Any) -> bool:
    """Keep the search reserve and at least a second for optional commands.

    optional_io and the shared deadline shorten each socket/watchdog timeout
    to its remaining optional budget; a full socket timeout need not be free.
    """
    return deadline.io_end - time.monotonic() > _reserve(client) + min(deadline.socket_timeout, 1.0)


@contextmanager
def optional_io(client: Any, deadline: Any):
    """Bound optional I/O without consuming the search's response budget."""
    end = deadline.end
    deadline.end -= _reserve(client)
    try:
        yield
    finally:
        deadline.end = end


def _enough_time(client: Any, deadline: Any, reserve: bool) -> bool:
    return has_time(client, deadline) if reserve else not deadline.expired


def _io(client: Any, deadline: Any, reserve: bool):
    return optional_io(client, deadline) if reserve else nullcontext()


def _fetch_headers(
    client: Any,
    connection: Any,
    deadline: Any,
    pending: list[str],
    result: HeaderSync,
    reserve: bool,
    batch_size: int,
) -> None:
    for start in range(0, len(pending), batch_size):
        if not _enough_time(client, deadline, reserve):
            result.timed_out = True
            break
        with _io(client, deadline, reserve):
            metadata = client._metadata(
                connection,
                pending[start : start + batch_size],
                deadline=deadline,
                headers=True,
                bodystructure=False,
                internaldate=True,
                envelope=True,
                size=True,
            )
        result.fetched.update(metadata)


def sync_growing_headers(
    client: Any,
    connection: Any,
    deadline: Any,
    folder: str,
    state: dict[str, Any],
    *,
    validity: str | None = None,
    reserve: bool = True,
    batch_size: int = 500,
) -> HeaderSync | None:
    """Resume a growing All Mail epoch; None retains the inventory fallback.

    UIDNEXT bounds the tail even with holes in the UID space. Cached batches
    survive interruption, while the saved epoch stays unchanged until every
    member has headers. Counts also guard against an expunge hidden by arrivals.
    """
    from .mail import CallDeadlineExceeded, MailError
    from .search import _coverage_status, _uidvalidity

    index = client.search_index
    generation = index.generation
    validity = validity or _uidvalidity(connection)
    previous = index.coverage_epoch(folder)
    current = (validity, state["exists"], state["uidnext"])
    if previous is None:
        _LOG.debug("Header catch-up epoch_missing->fallback")
        return None
    if previous[0] != validity:
        _LOG.warning("Header catch-up uidvalidity_changed->fallback")
        return None
    if state["uidnext"] is None:
        _LOG.warning("Header catch-up uidnext_missing->fallback")
        return None
    old_next, new_next = int(previous[2]), int(state["uidnext"])
    if state["exists"] < previous[1] or new_next < old_next:
        _LOG.warning(
            "Header catch-up shrank->fallback count=%d previous_count=%d "
            "uidnext=%d previous_uidnext=%d",
            state["exists"],
            previous[1],
            new_next,
            old_next,
        )
        return None
    if current == previous:
        if index.coverage(folder) == current:
            _LOG.info(
                "Header catch-up unchanged->noop count=%d uidnext=%d", state["exists"], new_next
            )
            return HeaderSync(complete=True)
        _LOG.warning(
            "Header catch-up epoch_unverified->fallback count=%d uidnext=%d",
            state["exists"],
            new_next,
        )
        return None
    cap = client.settings.search_index_catchup_max_messages
    if cap == 0:
        _LOG.debug(
            "Header catch-up disabled->fallback count=%d uidnext=%d", state["exists"], new_next
        )
        return None
    known = index.known_uids(folder, validity)
    if known is None:
        _LOG.warning(
            "Header catch-up known_missing->fallback count=%d uidnext=%d", state["exists"], new_next
        )
        return None
    if new_next == old_next:
        _LOG.warning(
            "Header catch-up uidnext_unchanged->fallback count=%d uidnext=%d",
            state["exists"],
            new_next,
        )
        return None
    baseline = {uid for uid in known if int(uid) < old_next}
    result = HeaderSync()
    try:
        if not _enough_time(client, deadline, reserve):
            result.timed_out = True
            _LOG.warning(
                "Header catch-up error reason=budget count=%d uidnext=%d", state["exists"], new_next
            )
            return result
        with _io(client, deadline, reserve):
            tail = client._search(connection, ["UID", f"{old_next}:{new_next - 1}"])
        inventory = sorted(baseline | set(tail), key=int)
        if len(tail) != len(set(tail)) or any(not old_next <= int(uid) < new_next for uid in tail):
            _LOG.warning("Header catch-up invalid_tail->fallback count=%d", state["exists"])
            return None
        membership_changed = len(baseline) != previous[1] or len(inventory) != state["exists"]
        if membership_changed:
            reconcile_ready = len(known) >= state["exists"] - cap
            _LOG.warning(
                "Header catch-up membership_changed->%s count=%d previous_count=%d new_uids=%d",
                "fallback" if reconcile_ready else "tail_progress",
                state["exists"],
                previous[1],
                len(tail),
            )
            if reconcile_ready:
                return None
        else:
            result.inventory = inventory
        missing = sorted(set(tail) - known, key=int)
        _LOG.info(
            "Header catch-up grew->incremental new_uids=%d missing=%d "
            "count=%d uid_start=%d uid_end=%d",
            len(tail),
            len(missing),
            state["exists"],
            old_next,
            new_next - 1,
        )
        _fetch_headers(client, connection, deadline, missing[:cap], result, reserve, batch_size)
        reason = "membership_changed" if membership_changed else "backlog"
        if not membership_changed and not result.timed_out and len(missing) <= cap:
            if not _enough_time(client, deadline, reserve):
                result.timed_out = True
            else:
                with _io(client, deadline, reserve):
                    unchanged = _coverage_status(connection, folder) == current
                if unchanged:
                    result.complete = index.record_coverage(
                        folder,
                        validity,
                        state,
                        inventory,
                        generation=generation,
                        catchup=True,
                        tail_range=(old_next, new_next),
                    )
                    reason = "headers_unverified"
                else:
                    reason = "epoch_changed"
        if not result.complete:
            _LOG.warning(
                "Header catch-up error reason=%s fetched=%d remaining=%d count=%d uidnext=%d",
                "budget" if result.timed_out else reason,
                len(result.fetched),
                len(missing) - len(result.fetched),
                state["exists"],
                new_next,
            )
    except CallDeadlineExceeded:
        result.timed_out = result.stream_broken = True
        _LOG.warning(
            "Header catch-up error reason=timeout count=%d uidnext=%d", state["exists"], new_next
        )
    except (imaplib.IMAP4.abort, OSError):
        result.stream_broken = True
        _LOG.warning(
            "Header catch-up error reason=connection count=%d uidnext=%d", state["exists"], new_next
        )
    except (MailError, imaplib.IMAP4.error):
        _LOG.warning(
            "Header catch-up error reason=imap count=%d uidnext=%d", state["exists"], new_next
        )
    return result


def sync_selected_headers(
    client: Any,
    connection: Any,
    deadline: Any,
    folder: str,
    state: dict[str, Any],
    *,
    validity: str | None = None,
    inventory: list[str] | None = None,
    reserve: bool = True,
    batch_size: int = 500,
) -> HeaderSync:
    """Catch up a selected mailbox and certify its full live inventory.

    Only callers which identified LIST's All Mail role should install this
    account-wide proof. The index is optional: a missing cache, unsafe header,
    failed write or a backlog over the configured bound leaves it incomplete.
    Existing headers are never fetched again. ALL reconciles expunges even
    when the count stayed unchanged, and INTERNALDATE warms indexed ordering.
    A timed-out command marks its stream unusable; callers must reconnect.
    """
    from .mail import CallDeadlineExceeded
    from .search import _uidvalidity

    index = client.search_index
    validity = validity or _uidvalidity(connection)
    if inventory is None:
        incremental = sync_growing_headers(
            client,
            connection,
            deadline,
            folder,
            state,
            validity=validity,
            reserve=reserve,
            batch_size=batch_size,
        )
        if incremental is not None:
            return incremental
    generation = index.generation
    result = HeaderSync(inventory=inventory)
    proof = index.coverage(folder)
    if proof is not None and proof == (validity, state["exists"], state["uidnext"]):
        result.complete = index.coverage(folder) == proof
        return result

    known = index.known_uids(folder, validity)
    cap = client.settings.search_index_catchup_max_messages
    if known is None or len(known) < state["exists"] - cap:
        if cap and index.coverage_epoch(folder) is not None:
            _LOG.warning(
                "Header catch-up inventory_skipped reason=%s count=%d cap=%d",
                "known_missing" if known is None else "backlog",
                state["exists"],
                cap,
            )
        return result
    if not _enough_time(client, deadline, reserve):
        result.timed_out = True
        _LOG.warning("Header catch-up error reason=budget count=%d", state["exists"])
        return result
    try:
        if inventory is None:
            with _io(client, deadline, reserve):
                inventory = client._search(connection, ["ALL"])
            result.inventory = inventory
        else:
            # Supplied inventories must also remove vanished cache entries.
            index.reconcile(folder, validity, inventory)
        missing = sorted(set(inventory) - known, key=int)
        _LOG.warning(
            "Header catch-up inventory->%s missing=%d count=%d",
            "reconcile" if len(missing) <= cap else "backlog",
            len(missing),
            state["exists"],
        )
        if len(missing) <= cap:
            _fetch_headers(client, connection, deadline, missing, result, reserve, batch_size)
    except CallDeadlineExceeded:
        result.timed_out = True
        result.stream_broken = True
        _LOG.warning("Header catch-up error reason=timeout count=%d", state["exists"])
        return result
    if not result.timed_out:
        result.complete = index.record_coverage(
            folder, validity, state, inventory, generation=generation
        )
        if result.complete:
            index.record_catchup(folder, validity, generation=generation)
        else:
            _LOG.warning(
                "Header catch-up error reason=headers_unverified count=%d", state["exists"]
            )
    else:
        _LOG.warning("Header catch-up error reason=budget count=%d", state["exists"])
    return result


def sync_all_mail(
    client: Any,
    connection: Any,
    deadline: Any,
    all_mail: str,
    *,
    reserve: bool = True,
) -> HeaderSync:
    """Temporarily EXAMINE All Mail, catching up before restoring selection.

    An interrupted command deliberately leaves selection unrestored because
    no further commands may run on that connection.
    """
    from .mail import CallDeadlineExceeded, MailError
    from .search import _coverage_status, _uidvalidity

    previous = getattr(connection, "_selected_inventory", None)
    selected = previous is not None and previous["folder"] == all_mail
    result = HeaderSync()
    changed = False

    def io():
        return optional_io(client, deadline) if reserve else nullcontext()

    try:
        if reserve and not has_time(client, deadline):
            result.timed_out = True
            _LOG.warning("Header catch-up error reason=budget")
            return result
        proof = client.search_index.coverage(all_mail)
        # The common unchanged epoch needs neither a UID inventory nor a SELECT.
        current = None
        if proof is not None or selected:
            with io():
                current = _coverage_status(connection, all_mail)
            if proof is not None and current == proof:
                result.complete = client.search_index.coverage(all_mail) == proof
                _LOG.info(
                    "Header catch-up unchanged->noop count=%d uidnext=%s", current[1], current[2]
                )
                return result
        if selected and current == (
            _uidvalidity(connection),
            previous["exists"],
            previous["uidnext"],
        ):
            state = previous
        else:
            # The original SELECT can predate a page FETCH and its arrivals.
            # Refresh that selection before using its epoch as coverage proof.
            with io():
                changed = True
                state = client._select(connection, all_mail)
        result = sync_selected_headers(
            client, connection, deadline, all_mail, state, reserve=reserve
        )
        return result
    except CallDeadlineExceeded:
        result.timed_out = True
        result.stream_broken = True
        _LOG.warning("Header catch-up error reason=timeout")
        return result
    except (MailError, imaplib.IMAP4.error, OSError):
        _LOG.warning("Header catch-up error reason=imap")
        raise
    finally:
        if changed and previous is not None and not selected and not result.stream_broken:
            try:
                with io():
                    client._select(connection, previous["folder"])
            except CallDeadlineExceeded:
                result.complete = False
                result.timed_out = True
                result.stream_broken = True
                _LOG.warning("Header catch-up error reason=restore_timeout")
