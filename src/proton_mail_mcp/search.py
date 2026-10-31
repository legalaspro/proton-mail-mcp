"""Deadline-bounded, chronological mailbox search with resumable pages, and counts."""

from __future__ import annotations

import base64
import binascii
import hashlib
import imaplib
import json
import logging
import math
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .sender_matching import sender_matches

if TYPE_CHECKING:
    from .mail import MailClient

_METADATA_BATCH_SIZE = 500
_MAX_SNIPPET_MESSAGES = 20
_CURSOR_VERSION = 3
_MAX_CURSOR_CHARS = 2048
_MAX_CURSOR_RETRIES = 3
_ALL_MAIL_WINDOW_DAYS = 30
_MAX_SEARCH_CANDIDATES = 1000
_MAX_SEARCH_SECONDS = 20.0
_SEARCH_TIMEOUT = "Search timed out; narrow the date range or filters and retry"
_HEADERS = {"from": "From", "to": "To", "subject": "Subject"}
_LOG = logging.getLogger(__name__)


def _default_since() -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=_ALL_MAIL_WINDOW_DAYS)).isoformat()


def _query_key(values: list[Any]) -> str:
    payload = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _options(**values: Any) -> list[dict[str, Any]]:
    """Search options beyond the original filters, for the cursor key.

    Omitted when all are defaults, so cursors issued before these options
    existed stay valid for the same search.
    """
    chosen = {
        name: value for name, value in values.items() if value is not None and value is not False
    }
    return [chosen] if chosen else []


def _scope(since: str | None, before: str | None) -> list[Any]:
    from .mail import _imap_date

    criteria: list[Any] = []
    for name, value in (("SINCE", since), ("BEFORE", before)):
        if value is not None:
            criteria.extend((name, _imap_date(value)))
    return criteria


def _conditions(
    from_: str | None,
    to: str | None,
    subject: str | None,
    text: str | None,
    unread_only: Any,
    exclude_subject: str | None,
    exclude_text: str | None,
    query: Any,
) -> tuple[list[tuple], tuple | None]:
    """Validate the filters, exclusions and query; return conditions and the query tree."""
    from . import search_query
    from .mail import MailValidationError, _quote

    if not isinstance(unread_only, bool):
        raise MailValidationError("unread_only must be a boolean")
    for value in (from_, to, subject, text):
        if value is not None:
            _quote(value)
    tree = search_query.parse(query)
    nodes = search_query.conditions(
        from_=from_,
        to=to,
        subject=subject,
        text=text,
        exclude_subject=exclude_subject,
        exclude_text=exclude_text,
        query=tree,
    )
    return nodes, tree


def _leaf_sets(
    client: MailClient, connection: Any, scope: list[Any], leaves: set[tuple[str, str]]
) -> dict[tuple[str, str], set[str]]:
    """One UID SEARCH per condition value that could not join the main command.

    Only the date keys are repeated. INTERNALDATE never changes, so each set
    answers its condition for every message the main command matched, whatever
    its flags are by then.
    """
    from . import search_query

    found = {}
    for field, value in sorted(leaves):
        args, literal = search_query.leaf_criteria(field, value)
        found[field, value] = set(client._search(connection, [*scope, *args], literal=literal))
    return found


def _decode_cursor(cursor: str | None, query: str) -> dict[str, Any] | None:
    from .mail import MailValidationError

    if cursor is None:
        return None
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > _MAX_CURSOR_CHARS:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        version = value.get("v")
        keys = {"v", "q", "validity", "last", "max_uid"}
        if version in (2, _CURSOR_VERSION):
            keys.update(("emitted", "rejected"))
        if version == _CURSOR_VERSION:
            keys.add("retries")
        if type(version) is not int or version not in (1, 2, _CURSOR_VERSION):
            raise ValueError
        if set(value) != keys or value["q"] != query:
            raise ValueError
        last = value["last"]
        validity = value["validity"]
        max_uid = value["max_uid"]
        emitted = value.get("emitted", [])
        rejected = value.get("rejected", [])
        retries = value.get("retries", 0)
        if not (version >= 2 and validity is None):
            if not isinstance(validity, str) or not validity.isdigit():
                raise ValueError
        if not (version >= 2 and max_uid is None):
            if type(max_uid) is not int or max_uid < 0:
                raise ValueError
        if not (version >= 2 and last is None):
            if (
                not isinstance(last, list)
                or len(last) != 2
                or type(last[0]) not in (int, float)
                or not math.isfinite(last[0])
                or type(last[1]) is not int
                or last[1] < 1
                or max_uid is None
                or max_uid < last[1]
            ):
                raise ValueError
        if not isinstance(emitted, list) or not isinstance(rejected, list):
            raise ValueError
        if any(
            type(uid) is not int or uid < 1 or max_uid is None or uid > max_uid
            for uid in [*emitted, *rejected]
        ):
            raise ValueError
        if type(retries) is not int or not 0 <= retries < _MAX_CURSOR_RETRIES:
            raise ValueError
        return value
    except (ValueError, TypeError, UnicodeError, binascii.Error, OverflowError, RecursionError):
        raise MailValidationError("Invalid search cursor for these search criteria") from None


def _encode_cursor(
    query: str,
    validity: str | None,
    last: tuple[float, int] | None,
    max_uid: int | None,
    emitted: set[int],
    rejected: set[int],
    retries: int = 0,
) -> str | None:
    if len(emitted) + len(rejected) > _MAX_CURSOR_CHARS // 2:
        return None
    payload = {
        "v": _CURSOR_VERSION,
        "q": query,
        "validity": validity,
        "last": last,
        "max_uid": max_uid,
        "emitted": sorted(emitted),
        "rejected": sorted(rejected),
        "retries": retries,
    }
    cursor = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("ascii"))
        .decode("ascii")
        .rstrip("=")
    )
    return cursor if len(cursor) <= _MAX_CURSOR_CHARS else None


def _no_resumable_cursor(result: dict[str, Any]) -> dict[str, Any]:
    result.update(
        partial=True,
        next_cursor=None,
        partial_reason="no_resumable_cursor",
        hint='Narrow the query by dates, folder, from or to, or run reindex(op="preview"), '
        'then obtain explicit user approval before reindex(op="execute").',
    )
    return result


def _uidvalidity(connection: Any) -> str:
    from .mail import MailError

    # imaplib.response() consumes the selected mailbox's response. Rules check
    # it again during raw identity verification, so read imaplib's current
    # selection data without consuming it (SELECT resets this dictionary).
    responses = getattr(connection, "untagged_responses", None)
    if responses is not None:
        values = responses.get("UIDVALIDITY")
    else:
        _, values = connection.response("UIDVALIDITY")
    if isinstance(values, (list, tuple)) and values:
        raw = values[0]
        if isinstance(raw, bytes) and raw.isdigit():
            return raw.decode("ascii")
    raise MailError("Mailbox did not provide UIDVALIDITY for stable paging")


def _coverage_status(connection: Any, folder: str) -> tuple[str, int, str]:
    """Read only the small STATUS response, preserving the selected mailbox."""
    from .mail import MailError, _checked, _encode_folder, _ImapValues, _mailbox

    data = _checked(connection.status(_mailbox(folder), "(MESSAGES UIDNEXT UIDVALIDITY)"))
    if not isinstance(data, (list, tuple)) or len(data) != 1 or not isinstance(data[0], bytes):
        raise MailError("Mailbox returned invalid coverage status")
    raw = data[0]
    parser = _ImapValues(raw)
    mailbox, fields = parser.value(), parser.value()
    if (
        mailbox != _encode_folder(folder).encode("ascii")
        or not isinstance(fields, list)
        or len(fields) != 6
        or raw[parser.position :].strip()
    ):
        raise MailError("Mailbox returned invalid coverage status")
    values = {}
    for name, value in zip(fields[::2], fields[1::2], strict=True):
        if (
            name not in (b"MESSAGES", b"UIDNEXT", b"UIDVALIDITY")
            or name in values
            or not isinstance(value, bytes)
            or not value.isdigit()
            or (name != b"MESSAGES" and int(value) < 1)
        ):
            raise MailError("Mailbox returned invalid coverage status")
        values[name] = value
    return (
        values[b"UIDVALIDITY"].decode("ascii"),
        int(values[b"MESSAGES"]),
        values[b"UIDNEXT"].decode("ascii"),
    )


def _index_rebuilding(client: MailClient) -> bool:
    """Leave optional account maintenance to an active rebuild worker."""
    service = client._reindex_service
    if service is not None and service.running:
        return True
    recovery = getattr(client, "index_autorebuild", None)
    return recovery is not None and recovery.status().get("state") in {"queued", "running"}


def _certified_inventory(
    client: MailClient, folder: str, validity: str, state: dict[str, Any]
) -> list[str] | None:
    """A folder's proven UIDs at this SELECT's epoch, as All Mail reuses its own.

    An unchanged count and UIDNEXT under one UIDVALIDITY mean no message came or
    left since the certified inventory, so Bridge's ALL would return the same UIDs.
    Optional acceleration: a rebuild or an unavailable proof keeps that live read.
    """
    from .search_index import _ERRORS

    if _index_rebuilding(client):
        return None
    index = client.search_index
    try:
        if index.coverage(folder) != (validity, state["exists"], state["uidnext"]):
            return None
        known = index.known_uids(folder, validity)
    except _ERRORS:
        return None
    return list(known) if known is not None and len(known) == state["exists"] else None


@dataclass
class _MappingProgress:
    stream_broken: bool = False
    selection_restored: bool = True


def _folder_headers(
    client: MailClient,
    connection: Any,
    deadline: Any,
    folder: str,
    validity: str,
    state: dict[str, Any],
    uids: list[str],
    all_mail: str | None,
    *,
    progress: _MappingProgress,
) -> bool:
    """Reuse All Mail headers through this mailbox's own live UID inventory.

    ENVELOPE is database-backed in Bridge. It supplies membership evidence
    without decrypting thousands of headers. Message-ID maps search content
    only; canonical action identities still require the normal fingerprint proof.
    """
    from .mail import CallDeadlineExceeded, MailError
    from .search_sync import has_time, optional_io

    index = client.search_index
    if _index_rebuilding(client):
        return False
    generation = index.generation
    if index.covers(folder, validity, uids):
        return True
    if len(uids) != len(set(uids)) or len(uids) != state["exists"]:
        return False
    changed = False
    remaining = client.settings.search_index_catchup_max_messages

    class _BudgetStopped(Exception):
        pass

    @contextmanager
    def io():
        if not has_time(client, deadline):
            raise _BudgetStopped
        with optional_io(client, deadline):
            yield

    try:
        proof = index.coverage(all_mail) if all_mail is not None else None
        if proof is None or remaining == 0:
            return False
        with io():
            if _coverage_status(connection, all_mail) != proof:
                return False
        source_validity = proof[0]
        source_known = index.known_uids(all_mail, source_validity)
        if source_known is None:
            return False
        missing_source = sorted(
            source_known - index.metadata_uids(all_mail, source_validity), key=int
        )
        if missing_source:
            with io():
                changed = True
                progress.selection_restored = False
                source_state = client._select(connection, all_mail)
            if (_uidvalidity(connection), source_state["exists"], source_state["uidnext"]) != proof:
                return False
            for start in range(0, min(len(missing_source), remaining), _METADATA_BATCH_SIZE):
                batch = missing_source[start : start + min(_METADATA_BATCH_SIZE, remaining)]
                with io():
                    client._metadata(
                        connection,
                        batch,
                        deadline=deadline,
                        headers=False,
                        bodystructure=False,
                        internaldate=True,
                        envelope=True,
                        size=True,
                    )
                remaining -= len(batch)
            # Restoring the requested mailbox is necessary for the normal scan,
            # so it may use that scan's reserve after optional warming stops.
            restored = client._select(connection, folder)
            changed = False
            progress.selection_restored = True
            if restored != state or _uidvalidity(connection) != validity:
                return False  # An arrival is ordinary fallback, not a tool error.
            if not source_known <= index.metadata_uids(all_mail, source_validity):
                return False
        missing = sorted(set(uids) - index.metadata_uids(folder, validity), key=int)
        for start in range(0, min(len(missing), remaining), _METADATA_BATCH_SIZE):
            batch = missing[start : start + min(_METADATA_BATCH_SIZE, remaining)]
            with io():
                metadata = client._metadata(
                    connection,
                    batch,
                    deadline=deadline,
                    headers=False,
                    bodystructure=False,
                    internaldate=True,
                    envelope=True,
                    size=True,
                )
            index.observe(folder, validity, metadata, membership=True)
            remaining -= len(batch)
        # Do not infer uniqueness from a capped sample of either UID space.
        # Retained metadata lets later calls finish this bounded warm step.
        if generation != index.generation or not set(uids) <= index.metadata_uids(folder, validity):
            return False
        index.map_headers(
            all_mail,
            source_validity,
            folder,
            validity,
            index.cached_metadata(folder, validity, uids),
        )
        complete = index.covers(folder, validity, uids)
        if complete and len(uids) == len(set(uids)) == state["exists"]:
            index.record_coverage(
                folder, validity, state, uids, account=False, generation=generation
            )
        return complete
    except _BudgetStopped:
        return False
    except CallDeadlineExceeded:
        progress.stream_broken = True
        return False
    except (imaplib.IMAP4.abort, OSError):
        progress.stream_broken = True
        return False
    except (MailError, imaplib.IMAP4.error):
        return False
    finally:
        if changed and not progress.stream_broken:
            try:
                client._select(connection, folder)
                progress.selection_restored = True
            except CallDeadlineExceeded:
                progress.stream_broken = True
            except (imaplib.IMAP4.abort, OSError):
                progress.stream_broken = True
            except (MailError, imaplib.IMAP4.error):
                pass


def _drop_unresolved(result: dict[str, Any]) -> int:
    """Keep actionable rows and mark omitted canonical handles as partial."""
    from .canonical import HANDLE_KEYS

    messages = result["messages"]
    result["messages"] = [
        item for item in messages if all(item.get(key) is not None for key in HANDLE_KEYS)
    ]
    dropped = len(messages) - len(result["messages"])
    if dropped:
        reason = result.get("partial_reason")
        keep = reason not in (None, "date_window", "candidate_cap")
        result.update(partial=True, partial_reason=reason if keep else "unresolved_handles")
    return dropped


def search_mail(
    client: MailClient,
    from_: str | None = None,
    to: str | None = None,
    subject: str | None = None,
    text: str | None = None,
    since: str | None = None,
    before: str | None = None,
    folder: str = "INBOX",
    limit: int = 20,
    cursor: str | None = None,
    include_snippets: bool = True,
    unread_only: bool = False,
    exclude_subject: str | None = None,
    exclude_text: str | None = None,
    query: dict[str, Any] | None = None,
    *,
    _deadline: Any = None,
    _skip_optional: bool = False,
) -> dict[str, Any]:
    """Use cheap server date searches and decode header text locally.

    Bridge's HEADER searches decrypt and scan the entire mailbox. Header filters
    are therefore matched against the full local decoded-header index before
    fetching dates, flags and any uncached headers. TEXT still requires a server
    scan and is limited by the call deadline.
    A complete decoded-header index removes All Mail's default date window,
    after checking a complete live UID inventory or its unchanged certified epoch.
    Otherwise unbounded All Mail uses the past 30 UTC calendar days and reports
    applied_since, partial=true and a partial_reason. The index
    stores no bodies: TEXT still uses Bridge's full-text search, without the
    default date window when the header index proves mailbox coverage.
    Messages report their canonical real-folder handle; a view's handle is an alias.
    The folder is matched by its LIST name; only INBOX ignores ASCII case.

    Every message reports seen from the FLAGS fetched with its INTERNALDATE.
    unread_only adds UNSEEN, a flag-only key, and drops messages read since.
    Exclusions and query conditions on from, to and subject are matched locally
    like the filters. Text conditions are TEXT keys of the same UID SEARCH, with
    NOT and OR, unless ``search_query.plan`` leaves one to a UID SEARCH per value.
    After index pruning, header filters fetch at most 1000 uncached candidates,
    with a 20-second maximum call budget. Larger header scans return explicit
    partial totals, with a bounded continuation cursor whenever resumable.
    Incomplete scans exclude rows already returned; exhausted continuation
    returns an explicit no_resumable_cursor reason and a recovery hint.
    """
    from . import canonical, search_query, search_sync
    from .mail import (
        MAX_SEARCH_RESULTS,
        MAX_SNIPPET_BYTES,
        CallDeadlineExceeded,
        MailError,
        MailValidationError,
        _imap_date,
        _mailbox,
    )

    requested_folder = folder
    _mailbox(folder)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise MailValidationError("Limit must be a positive integer")
    if not isinstance(include_snippets, bool):
        raise MailValidationError("include_snippets must be a boolean")
    limit = min(limit, MAX_SEARCH_RESULTS)
    scope = _scope(since, before)
    conditions, tree = _conditions(
        from_, to, subject, text, unread_only, exclude_subject, exclude_text, query
    )
    searched, literal, local = search_query.plan(conditions, {"text"})
    has_text = any("text" in search_query.fields(node) for node in conditions)
    headers_needed = any(search_query.fields(node) - {"text"} for node in local)
    unbounded = since is None and before is None
    window_since = _default_since() if unbounded else None
    options = _options(
        unread_only=unread_only,
        exclude_subject=exclude_subject,
        exclude_text=exclude_text,
        query=tree,
    )
    search_key = _query_key([folder, from_, to, subject, text, since, before, *options])
    window_key = _query_key([folder, from_, to, subject, text, window_since, before, *options])
    window_cursor = False
    try:
        position = _decode_cursor(cursor, search_key)
    except MailValidationError:
        if not unbounded:
            raise
        # Validate malformed/changed cursors before opening the connection. LIST
        # determines which of these two query keys applies to this mailbox.
        position = _decode_cursor(cursor, window_key)
        window_cursor = True
    result: dict[str, Any] = {
        "messages": [],
        "next_cursor": None,
        "total": 0,
        "partial": False,
        "total_is_exact": False,
    }

    class _CatchupStopped(CallDeadlineExceeded):
        """The optional stream was interrupted; retry within the same call budget."""

    deadline = _deadline or client._deadline()
    catchup_timed_out = False
    index_limited = False
    progress_scope: tuple[str, str, int] | None = None
    mapping_progress = _MappingProgress()
    validity = position["validity"] if position is not None else None
    max_uid = position["max_uid"] if position is not None else None
    last = tuple(position["last"]) if position is not None and position["last"] else None
    emitted = set(position.get("emitted", [])) if position is not None else set()
    rejected = set(position.get("rejected", [])) if position is not None else set()
    retries = position.get("retries", 0) if position is not None else 0
    selection_limit = max(1, limit - len(emitted)) if retries else limit
    emitted_keys: dict[int, tuple[float, int]] = {}
    page_keys: dict[int, tuple[float, int]] = {}
    enumeration_complete = False
    metadata_incomplete = False
    lookup_failed = False
    unresolved_handles = 0
    scan_limited = False
    header_uids: set[str] = set()
    has_more = False
    unstarted = position is not None and position["last"] is None and max_uid is None

    def report_index_status(response: dict[str, Any]) -> dict[str, Any]:
        """Keep recovery progress independent of the public completeness reason."""
        from .index_status import attach_index_status

        indexed_count = target = None
        if index_limited and progress_scope is not None:
            progress_folder, progress_validity, target = progress_scope
            # SELECT supplied the live total. Reporting must not wait for a writer.
            indexed_count = (
                client.search_index.reporting_count(progress_folder, progress_validity) or 0
            )
        return attach_index_status(
            client,
            response,
            depends_on_index=index_limited,
            incomplete=index_limited,
            indexed_count=indexed_count,
            target=target,
        )

    def continuation(response: dict[str, Any]) -> dict[str, Any]:
        if response["partial"] or has_more:
            shown = {int(item.get("alias", item)["uid"]) for item in response["messages"]}
            checkpoint = last
            if enumeration_complete:
                # Advance through the displayed prefix, stopping just before
                # the first missing metadata or unresolved canonical handle.
                for uid, key in page_keys.items():
                    if uid not in shown:
                        break
                    checkpoint = key
                if not has_more and shown == set(page_keys):
                    # A completed default window (or snippet-only failure) has
                    # no remaining position within this fixed search scope.
                    return _no_resumable_cursor(response)
            delivered = emitted | shown
            # Cached header nonmatches are already excluded by candidates().
            # Retain other rejections, including malformed dates, until overflow.
            remaining_rejected = set()
            if rejected:
                remaining_rejected = {
                    int(uid)
                    for uid in client.search_index.candidates(
                        folder, validity, [str(uid) for uid in rejected], local
                    )
                }
            uncached_progress = response.get("timed_out") and bool(
                client.search_index.cached_headers(folder, validity, list(header_uids))
            )
            failed = bool(
                (response.get("timed_out") and not uncached_progress)
                or lookup_failed
                or unresolved_handles
                or metadata_incomplete
                or (not enumeration_complete and not scan_limited and not uncached_progress)
            )
            retry_count = retries + 1 if failed else 0
            if failed and checkpoint != last:
                retry_count = 1
            if retry_count >= _MAX_CURSOR_RETRIES:
                if (
                    enumeration_complete
                    and unresolved_handles
                    and not lookup_failed
                    and not metadata_incomplete
                    and not response.get("timed_out")
                ):
                    # A permanently unresolved handle must not hide later rows.
                    checkpoint = next(reversed(page_keys.values()))
                    retry_count = 0
                    response["partial_reason"] = "unresolved_handles_skipped"
                else:
                    return _no_resumable_cursor(response)
            if checkpoint is not None and (checkpoint != last or not scan_limited):
                keys = dict(emitted_keys)
                keys.update(page_keys)
                delivered = {uid for uid in delivered if uid not in keys or keys[uid] < checkpoint}
            snapshot_max = max_uid
            if snapshot_max is None and delivered:
                snapshot_max = max(delivered)
            response["next_cursor"] = _encode_cursor(
                search_key,
                validity,
                checkpoint,
                snapshot_max,
                delivered,
                remaining_rejected,
                retry_count,
            )
            if (
                response["next_cursor"] is None
                and enumeration_complete
                and not metadata_incomplete
                and remaining_rejected
            ):
                response["next_cursor"] = _encode_cursor(
                    search_key, validity, checkpoint, snapshot_max, delivered, set(), retry_count
                )
            if response["next_cursor"] is None:
                return _no_resumable_cursor(response)
        return response

    if _deadline is None:
        deadline.end = min(deadline.end, time.monotonic() + _MAX_SEARCH_SECONDS)
        deadline.socket_timeout = min(deadline.socket_timeout, 10.0)
        deadline.response_reserve = min(deadline.response_reserve, 2.0)
    try:
        with client._session(deadline=deadline) as (connection, _):
            attributes, real, all_mail = canonical.mailbox_roles(connection)
            client.search_index.set_all_mail(all_mail)
            # Classify, select and report the mailbox as LIST names it: "inbox"
            # is the real folder INBOX, not a view needing canonical lookup.
            folder = canonical.listed_name(attributes, folder)
            state = client._select(connection, folder)
            validity = _uidvalidity(connection)
            progress_scope = (folder, validity, state["exists"])
            is_all_mail = "\\all" in attributes.get(folder, set())
            indexed_complete = False
            inventory = None
            catchup_stream_broken = False
            coverage_generation = client.search_index.generation
            if (
                not _skip_optional
                and not is_all_mail
                and headers_needed
                and all_mail is not None
                and not _index_rebuilding(client)
            ):
                from .search_sync import sync_all_mail

                if client.search_index.has_mailbox(all_mail):
                    try:
                        synced = sync_all_mail(client, connection, deadline, all_mail)
                    except (MailError, imaplib.IMAP4.error, OSError):
                        pass  # Optional acceleration; preserve the ordinary scan.
                    else:
                        catchup_timed_out = synced.timed_out
                        if synced.stream_broken:
                            with suppress(Exception):
                                connection.shutdown()
                            raise _CatchupStopped
                        if _uidvalidity(connection) != validity:
                            raise MailValidationError("Mailbox identity changed during search")
            if is_all_mail:
                known = (
                    client.search_index.known_uids(folder, validity)
                    if unbounded and cursor is None
                    else None
                )
                # Only inventory a cache close enough to catch up in this call.
                # Counts alone cannot certify coverage after expunges/arrivals.
                cap = client.settings.search_index_catchup_max_messages
                proof = client.search_index.coverage(folder)
                selected_epoch = (validity, state["exists"], state["uidnext"])
                indexed_complete = proof is not None and proof == selected_epoch
                incremental = None
                if unbounded and cursor is None:
                    incremental = search_sync.sync_growing_headers(
                        client,
                        connection,
                        deadline,
                        folder,
                        state,
                        validity=validity,
                        batch_size=_METADATA_BATCH_SIZE,
                    )
                if incremental is not None:
                    inventory = incremental.inventory
                    indexed_complete = incremental.complete
                    catchup_timed_out = incremental.timed_out
                    catchup_stream_broken = incremental.stream_broken
                    if indexed_complete and inventory is None and known is not None:
                        inventory = list(known)
                elif (
                    unbounded
                    and cursor is None
                    and proof is not None
                    and proof == selected_epoch
                    and known is not None
                    and len(known) == state["exists"]
                ):
                    # A persisted complete inventory plus an unchanged live
                    # epoch is sufficient. Avoid ALL and header catch-up on
                    # every from-only search, including the first after restart.
                    inventory = list(known)
                    indexed_complete = True
                elif (
                    unbounded
                    and cursor is None
                    and known is not None
                    and len(known) >= state["exists"] - cap
                    and not search_sync.has_time(client, deadline)
                ):
                    catchup_timed_out = True
                    _LOG.warning("Header catch-up error reason=budget count=%d", state["exists"])
                elif (
                    unbounded
                    and cursor is None
                    and known is not None
                    and len(known) >= state["exists"] - cap
                ):
                    try:
                        with search_sync.optional_io(client, deadline):
                            inventory = client._search(connection, ["ALL"])
                        missing = sorted(set(inventory) - known, key=int)
                        log = _LOG.info if len(missing) <= cap else _LOG.warning
                        log(
                            "Header catch-up inventory->%s missing=%d count=%d",
                            "reconcile" if len(missing) <= cap else "fallback",
                            len(missing),
                            state["exists"],
                        )
                        if missing and len(missing) <= cap:
                            for start in range(0, len(missing), _METADATA_BATCH_SIZE):
                                if not search_sync.has_time(client, deadline):
                                    catchup_timed_out = True
                                    _LOG.warning(
                                        "Header catch-up error reason=budget count=%d",
                                        state["exists"],
                                    )
                                    break
                                with search_sync.optional_io(client, deadline):
                                    client._metadata(
                                        connection,
                                        missing[start : start + _METADATA_BATCH_SIZE],
                                        deadline=deadline,
                                        headers=True,
                                        bodystructure=False,
                                        internaldate=True,
                                        envelope=True,
                                        size=True,
                                    )
                    except CallDeadlineExceeded:
                        catchup_timed_out = True
                        catchup_stream_broken = True
                        _LOG.warning(
                            "Header catch-up error reason=timeout count=%d", state["exists"]
                        )
                    except (MailError, imaplib.IMAP4.error, OSError):
                        _LOG.warning("Header catch-up error reason=imap count=%d", state["exists"])
                        raise
                    # Writes can fail or leave unsafe headers unknown.
                    known = client.search_index.known_uids(folder, validity)
                    indexed_complete = (
                        not catchup_timed_out
                        and known is not None
                        and inventory is not None
                        and len(inventory) == len(set(inventory)) == state["exists"]
                        and set(inventory) <= known
                    )
                    if inventory is not None:
                        certified = client.search_index.record_coverage(
                            folder,
                            validity,
                            state,
                            inventory,
                            known=known,
                            generation=coverage_generation,
                        )
                        if certified and not catchup_timed_out:
                            client.search_index.record_catchup(
                                folder, validity, generation=coverage_generation
                            )
                elif (
                    unbounded
                    and cursor is None
                    and client.search_index.enabled
                    and client.search_index.coverage_epoch(folder) is not None
                ):
                    if known is not None and cap > 0:
                        _LOG.warning(
                            "Header catch-up backlog_exceeds_cap->fallback "
                            "known=%d count=%d cap=%d",
                            len(known),
                            state["exists"],
                            cap,
                        )
                    elif known is None and client.search_index.reason not in (None, "missing"):
                        _LOG.warning(
                            "Header catch-up known_headers_unavailable->fallback count=%d",
                            state["exists"],
                        )
            # Keep an issued cursor's scope stable if the cache warms or becomes
            # unavailable between pages; its max_uid still bounds the snapshot.
            default_window = (
                unbounded
                and is_all_mail
                and (window_cursor or ((cursor is None or unstarted) and not indexed_complete))
            )
            if default_window:
                index_limited = not indexed_complete
                since = window_since
                scope[0:0] = ["SINCE", _imap_date(since)]
                search_key = window_key
                result.update(partial=True, partial_reason="date_window", applied_since=since)
            if not (unstarted and default_window):
                position = _decode_cursor(cursor, search_key)
            if position is not None and position["validity"] not in (None, validity):
                raise MailValidationError("Search cursor expired because UIDVALIDITY changed")
            if max_uid is None and state["uidnext"] is not None:
                max_uid = int(state["uidnext"]) - 1
            if catchup_stream_broken:
                # Never issue another command on the interrupted stream. The
                # reserved budget can serve the normal window on a new session.
                with suppress(Exception):
                    connection.shutdown()
                raise _CatchupStopped
            criteria = [*scope, *(["UNSEEN"] if unread_only else []), *searched]
            if not is_all_mail and not criteria:
                inventory = _certified_inventory(client, folder, validity, state)
                indexed_complete = inventory is not None
            searched_uids = (
                inventory
                if inventory is not None and not criteria
                else client._search(connection, criteria or ["ALL"], literal=literal)
            )
            if is_all_mail and not criteria and inventory is None:
                # Cursor pages skip catch-up, but their ordinary ALL search can
                # refresh the same proof used by every other folder.
                client.search_index.record_coverage(
                    folder, validity, state, searched_uids, generation=coverage_generation
                )
            elif not is_all_mail and not criteria and inventory is None:
                # All Mail coverage says nothing about this folder's distinct
                # UID space. Certify its headers against its own live inventory.
                live_count = len(set(searched_uids))
                complete_inventory = len(searched_uids) == live_count == state["exists"]
                indexed_complete = complete_inventory and client.search_index.covers(
                    folder, validity, searched_uids
                )
            if not _skip_optional and not is_all_mail and headers_needed and not indexed_complete:
                _folder_headers(
                    client,
                    connection,
                    deadline,
                    folder,
                    validity,
                    state,
                    searched_uids,
                    all_mail,
                    progress=mapping_progress,
                )
                if mapping_progress.stream_broken:
                    with suppress(Exception):
                        connection.shutdown()
                    raise _CatchupStopped
                if not mapping_progress.selection_restored:
                    client._select(connection, folder)
                if _uidvalidity(connection) != validity:
                    raise MailValidationError("Mailbox identity changed during search")
            uids = sorted(set(searched_uids), key=int, reverse=True)
            if max_uid is None:
                max_uid = max(map(int, uids), default=0)
            found_text = _leaf_sets(
                client,
                connection,
                scope,
                {leaf for node in local for leaf in search_query.leaves(node) if leaf[0] == "text"},
            )

            def leaf_test(uid: str, item: dict[str, Any]) -> Any:
                def test(field: str, value: str) -> bool:
                    if field == "text":
                        return uid in found_text[field, value]
                    headers = item.get("headers", cached_headers.get(uid, {}))
                    if field == "from":
                        return sender_matches(
                            headers.get("From"), value, decoded="headers" not in item
                        )
                    header = str(headers.get(_HEADERS[field], ""))
                    return value.casefold() in header.casefold()

                return test

            uids = [uid for uid in uids if int(uid) <= max_uid]
            excluded = emitted | rejected
            if emitted and any(int(uid) not in excluded for uid in uids):
                emitted_metadata = client.search_index.cached_metadata(
                    folder, validity, [str(uid) for uid in emitted]
                )
                missing_dates = [str(uid) for uid in emitted if str(uid) not in emitted_metadata]
                if missing_dates:
                    # A disabled/lost index must not keep already traversed
                    # UIDs forever. Only their immutable dates are needed to
                    # trim the bounded suffix when the checkpoint advances.
                    emitted_metadata.update(
                        client._metadata(
                            connection,
                            missing_dates,
                            deadline=deadline,
                            headers=False,
                            bodystructure=False,
                            internaldate=True,
                        )
                    )
                emitted_keys.update(
                    (int(uid), (item["internaldate"], int(uid)))
                    for uid, item in emitted_metadata.items()
                )
            # Delivered rows matched immutable content on an earlier attempt.
            # SEARCH still supplies current scope/UNSEEN membership for totals.
            emitted_count = sum(int(uid) in emitted for uid in uids)
            uids = [uid for uid in uids if int(uid) not in excluded]
            # Match every known header before the expensive-work cap. Unknown
            # headers and server TEXT predicates survive for exact live checks.
            uids = client.search_index.candidates(folder, validity, uids, local)
            indexed_page = (
                client.search_index.ordered_page(
                    folder,
                    validity,
                    uids,
                    local,
                    selection_limit,
                    last=last,
                )
                if not has_text and not unread_only
                else None
            )
            if indexed_page is not None:
                uids = [uid for uid, _ in indexed_page[1][:selection_limit]]
            cached_headers = (
                client.search_index.cached_headers(folder, validity, uids)
                if headers_needed or default_window
                else {}
            )
            uncached = [uid for uid in uids if uid not in cached_headers]
            candidate_count = len(uncached)
            scan_limited = headers_needed and candidate_count > _MAX_SEARCH_CANDIDATES
            # Known decoded headers can satisfy local filters without another
            # literal fetch. A plain window opportunistically retains a bounded
            # set of headers for timeout recovery; the rest only need dates/flags.
            header_uids = (
                set(uncached[:_MAX_SEARCH_CANDIDATES])
                if headers_needed or default_window
                else set()
            )
            if scan_limited:
                index_limited = True
                # UIDs do not imply chronology. A continuation retries this
                # boundary, excluding delivered rows, until enumeration finishes.
                uids = [uid for uid in uids if uid in cached_headers or uid in header_uids]
                result.update(partial=True, partial_reason="candidate_cap")
            matches: dict[str, dict[str, Any]] = {}
            complete = False
            can_fetch = True
            metadata_missing = False
            invalid_dates: set[str] = set()
            try:
                for start in range(0, len(uids), _METADATA_BATCH_SIZE):
                    batch = uids[start : start + _METADATA_BATCH_SIZE]
                    for fetch_headers in (True, False):
                        group = [uid for uid in batch if (uid in header_uids) == fetch_headers]
                        if not group:
                            continue
                        metadata = client._metadata(
                            connection,
                            group,
                            deadline=deadline,
                            headers=fetch_headers,
                            bodystructure=False,
                            internaldate=True,
                            # Database-backed in Gluon, like INTERNALDATE: seen
                            # needs no further command, per message or per page.
                            flags=True,
                            invalid_dates=invalid_dates,
                        )
                        # Explicitly malformed dates cannot supply sort keys.
                        # Skip them for this cursor while retrying absent metadata.
                        rejected.update(map(int, invalid_dates))
                        if set(group) != set(metadata):
                            metadata_missing = True
                            result.update(partial=True, partial_reason="metadata_missing")
                        for uid, item in metadata.items():
                            item["seen"] = any(
                                flag.casefold() == "\\seen" for flag in item["flags"]
                            )
                            if unread_only and item["seen"]:
                                continue  # Read after UNSEEN matched it.
                            test = leaf_test(uid, item)
                            if all(search_query.matches(node, test) for node in local):
                                matches[uid] = item
                                if (
                                    scan_limited
                                    and uid in header_uids
                                    and last is not None
                                    and (item["internaldate"], int(uid)) >= last
                                ):
                                    # A capped fallback can revisit an earlier
                                    # complete page after index availability
                                    # changes. Retire those traversed matches
                                    # too, so they cannot fill every retry cap.
                                    emitted.add(int(uid))
                                    emitted_keys[int(uid)] = (item["internaldate"], int(uid))
                            else:
                                rejected.add(int(uid))
                complete = not metadata_missing and not scan_limited
            except CallDeadlineExceeded:
                # A socket timeout may occur before the overall deadline. The
                # IMAP stream is then unusable: retain cached data and stop I/O.
                can_fetch = False
                index_limited |= headers_needed and fetch_headers
                result.update(
                    partial=True, partial_reason="timed_out", timed_out=True, error=_SEARCH_TIMEOUT
                )
            metadata_incomplete = metadata_missing
            result["total"] = emitted_count + (
                indexed_page[0] - len(invalid_dates) if indexed_page is not None else len(matches)
            )
            result["total_is_exact"] = complete and not default_window
            ordered = (
                [uid for uid, _ in indexed_page[1]]
                if indexed_page is not None
                else sorted(
                    matches, key=lambda uid: (matches[uid]["internaldate"], int(uid)), reverse=True
                )
            )
            if last is not None:
                ordered = [
                    uid
                    for uid in ordered
                    if indexed_page is not None or (matches[uid]["internaldate"], int(uid)) < last
                ]
            page = [uid for uid in ordered[:selection_limit] if uid in matches]
            enumeration_complete = complete
            page_keys = {int(uid): (matches[uid]["internaldate"], int(uid)) for uid in page}
            # Headers already fetched for local filters can be returned even if
            # the deadline prevents this page's BODYSTRUCTURE/snippet fetch.
            page_metadata = {
                uid: matches[uid] for uid in page if matches[uid].get("headers") is not None
            }
            try:
                if page and can_fetch and (include_snippets or len(page_metadata) != len(page)):
                    invalid_dates = set()
                    fetched_page = client._metadata(
                        connection,
                        page,
                        deadline=deadline,
                        headers=True,
                        bodystructure=include_snippets,
                        internaldate=True,
                        envelope=True,
                        size=True,
                        invalid_dates=invalid_dates,
                    )
                    rejected.update(map(int, invalid_dates))
                    missing_page = set(page) - set(fetched_page)
                    if missing_page:
                        # Messages can disappear between SEARCH and FETCH. Do
                        # not describe a shortened page as complete or exact.
                        result.update(partial=True, partial_reason="metadata_missing")
                        result["total_is_exact"] = False
                        result["total"] -= len(missing_page)
                        complete = False
                        metadata_incomplete = True
                    page_metadata = fetched_page
            except CallDeadlineExceeded:
                can_fetch = False
                result.update(
                    partial=True, partial_reason="timed_out", timed_out=True, error=_SEARCH_TIMEOUT
                )
            if not is_all_mail and not criteria and not indexed_complete:
                # Headers this search just cached can finish its inventory's proof.
                client.search_index.record_coverage(
                    folder,
                    validity,
                    state,
                    searched_uids,
                    account=False,
                    generation=coverage_generation,
                )
            found = []
            for uid in page:
                if uid not in page_metadata:
                    continue
                item = client._summary(page_metadata[uid]["headers"], uid, folder, validity)
                item["seen"] = matches[uid]["seen"]
                if include_snippets:
                    item["snippet"] = ""
                    item["snippet_omitted"] = True
                # A view such as All Mail or a label reports its own handle as
                # alias only, until the message's real folder is resolved.
                location = canonical.handle(folder, validity, uid)
                canonical.describe(item, location, location if folder in real else None)
                result["messages"].append(item)
                found.append((uid, item, location))
            has_more = len(ordered) > selection_limit
            if include_snippets:
                for uid, item, _ in found[:_MAX_SNIPPET_MESSAGES]:
                    metadata = page_metadata[uid]
                    if metadata.get("structure") is None:
                        continue
                    try:
                        body, _ = client._body(
                            connection,
                            uid,
                            max_bytes=MAX_SNIPPET_BYTES,
                            structure=metadata["structure"],
                            deadline=deadline,
                        )
                    except CallDeadlineExceeded:
                        raise
                    except (imaplib.IMAP4.error, OSError):
                        # A broken stream leaves requested message data incomplete
                        # and cannot safely serve later snippets or handle lookups.
                        result.update(partial=True, partial_reason="metadata_missing")
                        can_fetch = False
                        lookup_failed = folder not in real
                        break
                    except (MailError, ValueError, TypeError, IndexError):
                        # Snippets are optional; malformed MIME in one message
                        # must not discard this page or prevent other snippets.
                        continue
                    item["snippet"] = " ".join(body.split())[:200]
                    item["snippet_omitted"] = False
            if found and folder not in real and can_fetch:
                try:
                    locations = canonical.resolve_views(
                        client,
                        connection,
                        deadline,
                        [location for _, _, location in found],
                        real=real,
                        all_mail=all_mail,
                    )
                except CallDeadlineExceeded:
                    result.update(
                        partial=True,
                        timed_out=True,
                        error=_SEARCH_TIMEOUT,
                        partial_reason="canonical_unresolved",
                    )
                    can_fetch = False
                except (MailError, imaplib.IMAP4.error, OSError) as exc:
                    # The page is complete; only canonical handles are unknown,
                    # including after incomplete metadata in any scanned folder.
                    lookup_failed = True
                    result.update(partial=True, partial_reason="canonical_unresolved")
                    can_fetch = isinstance(exc, MailError)
                else:
                    for (_, item, location), resolved in zip(found, locations):
                        canonical.describe(item, location, resolved)
                    unresolved_handles = sum(resolved is None for resolved in locations)
            if (
                can_fetch
                and _deadline is None
                and not default_window
                and not catchup_timed_out
                and all_mail is not None
                and client.search_index.has_mailbox(all_mail)
                and not _index_rebuilding(client)
            ):
                from .search_index import _ERRORS
                from .search_sync import sync_all_mail

                # This folder's page is already complete. Spend only the
                # optional reserve on keeping the account index current.
                try:
                    synced = sync_all_mail(client, connection, deadline, all_mail)
                    if synced.stream_broken:
                        can_fetch = False
                        deadline.end = min(deadline.end, time.monotonic())
                        with suppress(Exception):
                            connection.shutdown()
                except CallDeadlineExceeded:
                    can_fetch = False
                    deadline.end = min(deadline.end, time.monotonic())
                    with suppress(Exception):
                        connection.shutdown()
                except (MailError, imaplib.IMAP4.error, OSError, *_ERRORS):
                    pass
            _drop_unresolved(result)
            return report_index_status(continuation(result))
    except _CatchupStopped:
        try:
            fallback = search_mail(
                client,
                from_=from_,
                to=to,
                subject=subject,
                text=text,
                since=window_since if is_all_mail else since,
                before=before,
                folder=requested_folder,
                limit=limit,
                cursor=cursor,
                include_snippets=include_snippets,
                unread_only=unread_only,
                exclude_subject=exclude_subject,
                exclude_text=exclude_text,
                query=query,
                _deadline=deadline,
                _skip_optional=True,
            )
        except (CallDeadlineExceeded, MailError):
            # Connection setup can still fail. Retain explicit partial/window
            # evidence even when the reserved fallback cannot reach Bridge.
            result.update(
                partial=True, partial_reason="timed_out", timed_out=True, error=_SEARCH_TIMEOUT
            )
            index_limited = True
            return report_index_status(continuation(result))
        if is_all_mail:
            index_limited = True
            fallback.update(
                partial=True,
                partial_reason=fallback.get("partial_reason", "date_window"),
                total_is_exact=False,
                applied_since=window_since,
            )
        if fallback["next_cursor"] is None and fallback["partial"]:
            _no_resumable_cursor(fallback)
        return report_index_status(fallback)
    except CallDeadlineExceeded:
        result.update(
            partial=True, partial_reason="timed_out", timed_out=True, error=_SEARCH_TIMEOUT
        )
        _drop_unresolved(result)
        return report_index_status(continuation(result))


def count_mail(
    client: MailClient,
    from_: str | None = None,
    to: str | None = None,
    subject: str | None = None,
    text: str | None = None,
    since: str | None = None,
    before: str | None = None,
    folder: str = "INBOX",
    unread_only: bool = False,
    exclude_subject: str | None = None,
    exclude_text: str | None = None,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Count matching messages with UID SEARCH alone; no message is fetched.

    Unlike search_mail, every condition is a server key: FROM, TO and SUBJECT
    compare Bridge's raw header text, so an RFC 2047 encoded header may not match
    a non-ASCII value that search_mail finds after decoding. Header and text keys
    make Gluon read every message in the folder (date keys do not narrow that),
    within the call deadline; date and unread_only keys use its database only.
    No All Mail default window applies: SEARCH needs no per-message fetch. A
    condition left out of the command by ``search_query.plan`` (a further
    non-ASCII value) is counted from one more UID SEARCH per value. An empty
    folder needs no SEARCH, and with no conditions EXAMINE's EXISTS is the count.
    Setup and all searches share a 20-second maximum budget, with at most ten
    seconds per command; configured shorter limits apply. Timeouts return a
    partial result with count=0, timed_out=true and a safe error.
    """
    from . import canonical, search_query
    from .mail import CallDeadlineExceeded, _mailbox

    _mailbox(folder)
    scope = _scope(since, before)
    conditions, _ = _conditions(
        from_, to, subject, text, unread_only, exclude_subject, exclude_text, query
    )
    searched, literal, rest = search_query.plan(conditions, set(search_query.FIELDS))
    criteria = [*scope, *(["UNSEEN"] if unread_only else []), *searched]
    result = {"folder": folder, "count": 0}
    deadline = client._deadline()
    deadline.end = min(deadline.end, time.monotonic() + _MAX_SEARCH_SECONDS)
    deadline.socket_timeout = min(deadline.socket_timeout, 10.0)
    deadline.response_reserve = min(deadline.response_reserve, 2.0)
    try:
        with client._session(deadline=deadline) as (connection, _):
            attributes = canonical.mailbox_roles(connection)[0]
            folder = canonical.listed_name(attributes, folder)
            result["folder"] = folder
            state = client._select(connection, folder)
            if not state["exists"]:
                return result
            if not criteria and not rest:
                result["count"] = state["exists"]
                return result
            uids = set(client._search(connection, criteria or ["ALL"], literal=literal))
            found = _leaf_sets(
                client,
                connection,
                scope,
                {leaf for node in rest for leaf in search_query.leaves(node)},
            )

            def counted(uid: str) -> bool:
                def test(field: str, value: str) -> bool:
                    return uid in found[field, value]

                return all(search_query.matches(node, test) for node in rest)

            count = sum(map(counted, uids))
            deadline.check()
            result["count"] = count
            return result
    except CallDeadlineExceeded:
        result.update(
            count=0,
            partial=True,
            partial_reason="timed_out",
            timed_out=True,
            error="Count timed out; narrow the date range or filters and retry",
        )
        return result
