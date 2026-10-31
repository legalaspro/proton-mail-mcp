"""One canonical (folder, UIDVALIDITY, UID) handle per message across mailbox views.

Bridge lists every message once in its single real folder (INBOX, Archive, Sent,
Drafts, Spam, Trash or Folders/...) and again in views such as All Mail, Starred
and Labels/..., each with its own UID space. The real-folder handle is canonical:
guarded moves, read-state changes and rules use it as their source, and their
tokens bind it. Search, threads and previews report it as folder, uidvalidity and
uid. A view handle under which a tool found the message is only an ``alias``.
Every tool takes real folders from the same LIST-vetted set, ``real_folders``.

Resolving a view handle reads no message content. Gluon serves ENVELOPE,
INTERNALDATE and RFC822.SIZE from its database, shares them between the mailbox
copies of one message, reports INTERNALDATE in UTC and evaluates ON on that UTC
calendar day. A view handle resolves only when exactly one real-folder copy and
exactly one All Mail copy share all three values, and every mailbox read still
reports the UIDVALIDITY, UIDNEXT and message count it was read with. All Mail
lists each message exactly once, so its single copy is the view's message and
the real-folder copy's; UIDNEXT never repeats, so unchanged counts prove that no
twin arrived and no copy left while the folders were scanned. Without All Mail
nothing resolves: a view-only twin of a real-folder message looks the same.
No copy, duplicates, a deadline or a mailbox error leave the canonical fields
null. So does incomplete metadata for the view handle or any candidate copy: a
candidate without all three values could be another copy, so the remaining
copies prove nothing.

A certified All Mail index answers the same question from its fingerprints. Its
single copy already rules out a twin, so a real folder needs a proof only if it
holds a copy of the wanted message: a folder never gives a handle unless its index
matches live STATUS, and a folder that fails gets a live scan for its messages
alone. A stale All Mail keeps the whole lookup live, since nothing else lists
every message once.
"""

from __future__ import annotations

import hashlib
import imaplib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .search_index import Unplaced

HANDLE_KEYS = ("folder", "uidvalidity", "uid")
_FETCH_BATCH_SIZE = 500
_DAYS_PER_SEARCH = 50


@dataclass
class IndexedViews:
    """What the index proved for each handle, and what is left for a live scan.

    ``locations`` is None both for a handle with no unique copy and for one in
    ``pending``, which a live scan fills in as it goes, so a scan that stops keeps
    what it placed. ``proofs`` holds the folders whose epochs matched live STATUS.
    """

    locations: list[dict[str, Any] | None]
    pending: dict[int, Unplaced]
    proofs: dict[str, tuple[str, int, str]]


def handle(folder: Any, uidvalidity: Any, uid: Any) -> dict[str, Any]:
    return {"folder": folder, "uidvalidity": uidvalidity, "uid": uid}


def describe(item: dict[str, Any], found: dict[str, Any], canonical: dict[str, Any] | None) -> None:
    """Report the canonical handle, or nulls; a different found handle is the alias."""
    item.update(canonical if canonical is not None else dict.fromkeys(HANDLE_KEYS))
    if canonical == found:
        item.pop("alias", None)
    else:
        item["alias"] = dict(found)


def mailbox_roles(connection: Any) -> tuple[dict[str, set[str]], set[str], str | None]:
    """Read LIST once: attributes, canonical real folders and the All Mail view."""
    from .cleanup_folders import _folder_attributes, real_folders

    attributes = _folder_attributes(connection)
    all_mail = next(
        (
            folder
            for folder, flags in attributes.items()
            if "\\all" in flags and "\\noselect" not in flags
        ),
        None,
    )
    return attributes, real_folders(attributes), all_mail


def listed_name(attributes: dict[str, set[str]], folder: str) -> str:
    """The LIST spelling of a requested mailbox; only ASCII INBOX ignores case."""
    if folder in attributes or not (folder.isascii() and folder.upper() == "INBOX"):
        return folder
    return next((name for name in attributes if name.isascii() and name.upper() == "INBOX"), folder)


def row_identity(row: dict[str, Any], real: set[str]) -> dict[str, Any]:
    """Canonical fields of a cleanup row: its own handle only in a real folder.

    ``real`` is the LIST-vetted set that search, threads and guarded moves use; a
    canonical-looking name with a view attribute such as ``\\Flagged`` is a view.
    A label or All Mail row keeps its handle as alias with null canonical fields
    until ``resolve_samples`` proves its real-folder copy. Its memberships share
    only a Message-ID with it, which never shows they are the same message.
    """
    found = handle(row.get("folder"), row.get("uidvalidity"), row.get("uid"))
    known = isinstance(found["folder"], str) and found["folder"] in real
    item: dict[str, Any] = {}
    describe(item, found, found if known else None)
    return item


def resolve_samples(
    client: Any,
    connection: Any,
    deadline: Any,
    samples: list[dict[str, Any]],
    *,
    real: set[str],
    all_mail: str | None,
    started: float,
    budget: float,
) -> bool:
    """Resolve each sample's view alias in place, as search does; True if incomplete.

    The lookup reads at most All Mail and every real folder, as the preview's
    membership checks just did. It starts only if the preview used at most half of
    its 80% ``budget`` since ``started``, so a lookup of similar cost ends within it.
    A skipped or failed lookup leaves the canonical fields null and the alias.
    """
    from . import mail

    views = [sample for sample in samples if "alias" in sample]
    if not views:
        return False
    if mail.time.monotonic() - started > budget * 0.4:
        return True
    try:
        locations = resolve_views(
            client,
            connection,
            deadline,
            [sample["alias"] for sample in views],
            real=real,
            all_mail=all_mail,
        )
    except (mail.MailError, imaplib.IMAP4.error, OSError):
        return True
    for sample, location in zip(views, locations):
        describe(sample, dict(sample["alias"]), location)
    return False


def _envelope_digest(envelope: Any) -> str:
    # Gluon stores one envelope per message and returns it for every mailbox copy.
    return hashlib.sha256(repr(envelope).encode("ascii", "backslashreplace")).hexdigest()


def _fingerprints(
    client: Any, connection: Any, deadline: Any, uids: list[str]
) -> dict[str, tuple[tuple[str, float, int], str]]:
    """Fetch only database-backed metadata, complete for every requested UID.

    ``_metadata`` omits a UID lacking any value. That UID could be another copy
    of a wanted message, so skipping it could make the remaining copies look
    unique: fail the whole lookup instead.
    """
    from .mail import MailError

    result = {}
    for offset in range(0, len(uids), _FETCH_BATCH_SIZE):
        batch = uids[offset : offset + _FETCH_BATCH_SIZE]
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
        for uid in batch:
            record = metadata.get(uid)
            envelope = record["envelope"] if record is not None else None
            if not isinstance(envelope, list) or len(envelope) != 10:
                raise MailError("Mailbox returned incomplete metadata during canonical lookup")
            fingerprint = (_envelope_digest(envelope), record["internaldate"], record["size"])
            result[uid] = fingerprint, record["internaldate_day"]
    return result


def _scan(
    client: Any,
    connection: Any,
    deadline: Any,
    folder: str,
    days: set[str],
    copies: dict[tuple, list[dict[str, Any]]],
    *,
    selected: tuple[dict[str, Any], str] | None = None,
    known: dict[str, tuple[tuple[str, float, int], str]] | None = None,
) -> tuple[dict[str, Any], str]:
    """Record every handle in one folder whose fingerprint is wanted.

    ``known`` holds fingerprints already fetched under this same selection. Every
    other SEARCH candidate needs a complete fingerprint, wanted or not. Returns
    the SELECT state and UIDVALIDITY that the scan read.
    """
    from .mail import MailError, _imap_date
    from .search import _uidvalidity

    if selected is None:
        selected = client._select(connection, folder), _uidvalidity(connection)
    state, validity = selected
    if not state["exists"]:
        return selected
    ordered = sorted(days)
    uids: set[str] = set()
    for offset in range(0, len(ordered), _DAYS_PER_SEARCH):
        chunk = ordered[offset : offset + _DAYS_PER_SEARCH]
        criteria: list[Any] = ["OR"] * (len(chunk) - 1)
        for day in chunk:
            criteria.extend(("ON", _imap_date(day)))
        uids.update(client._search(connection, criteria))
    found = dict(known or {})
    found.update(_fingerprints(client, connection, deadline, sorted(uids - set(found), key=int)))
    if _uidvalidity(connection) != validity:
        raise MailError("Mailbox identity changed during canonical lookup")
    for uid, (fingerprint, _) in found.items():
        if fingerprint in copies:
            copies[fingerprint].append(handle(folder, validity, uid))
    return selected


def _unchanged(client: Any, connection: Any, states: dict[str, tuple[dict[str, Any], str]]) -> None:
    """Prove that no mailbox read gained or lost a message during the lookup.

    Each mailbox is opened again after the last scan. UIDNEXT never repeats under
    one UIDVALIDITY, so the same UIDNEXT and message count mean that nothing
    arrived or left since it was read; a mailbox without UIDNEXT proves nothing.
    """
    from .mail import MailError
    from .search import _uidvalidity

    for folder, (state, validity) in states.items():
        if (
            state["uidnext"] is None
            or client._select(connection, folder) != state
            or _uidvalidity(connection) != validity
        ):
            raise MailError("Mailbox changed during canonical lookup")


def resolve_views(
    client: Any,
    connection: Any,
    deadline: Any,
    handles: list[dict[str, Any]],
    *,
    real: set[str],
    all_mail: str | None,
    indexed: IndexedViews | None = None,
) -> list[dict[str, Any] | None]:
    """Return the unique canonical handle for each view handle, in order, or None.

    A handle resolves only if its fingerprint has exactly one All Mail copy and
    exactly one real-folder copy, and no mailbox read changed before the result.
    Without All Mail none resolves, and no mailbox is read. Errors propagate so
    callers can report partial results; nothing is mapped from incomplete scans,
    and incomplete metadata or a changed mailbox is such an error.

    A certified All Mail narrows this to ``_scan_unproven``. A caller that already
    ran ``resolve_indexed_views`` passes its result as ``indexed``.
    """
    from .mail import MAX_THREAD_FOLDERS, MailError
    from .search import _uidvalidity

    if indexed is None:
        indexed = resolve_indexed_views(
            client, connection, deadline, handles, real=real, all_mail=all_mail
        )
    if indexed is not None:
        return _scan_unproven(client, connection, deadline, indexed, real=real)

    views = [tuple(value[key] for key in HANDLE_KEYS) for value in handles]
    mapping: dict[tuple, dict[str, Any] | None] = dict.fromkeys(views)
    # Only All Mail lists every message once. Without it, a view-only message
    # and an identical real-folder message look like one message in two places.
    if not views or not real or all_mail is None:
        return [None] * len(views)
    if len(real) > MAX_THREAD_FOLDERS:
        raise MailError("Mailbox has too many folders for canonical lookup")
    groups: dict[str, list[tuple]] = {}
    for view in mapping:
        groups.setdefault(view[0], []).append(view)
    prints: dict[tuple, tuple[str, float, int]] = {}
    days: set[str] = set()
    # Every mailbox read, with the SELECT state and UIDVALIDITY it was read under.
    states: dict[str, tuple[dict[str, Any], str]] = {}
    all_copies: dict[tuple, list[dict[str, Any]]] | None = None
    # All Mail is visited last so its uniqueness scan reuses that selection.
    for folder in sorted(groups, key=lambda name: name == all_mail):
        selected = client._select(connection, folder), _uidvalidity(connection)
        members = [view for view in groups[folder] if view[1] == selected[1]]
        found = _fingerprints(
            client, connection, deadline, sorted({view[2] for view in members}, key=int)
        )
        if members:
            # The view's message must still be there when the lookup ends.
            states[folder] = selected
        for view in members:
            prints[view], day = found[view[2]]
            days.add(day)
        if folder == all_mail and prints:
            all_copies = {fingerprint: [] for fingerprint in prints.values()}
            states[folder] = _scan(
                client,
                connection,
                deadline,
                folder,
                days,
                all_copies,
                selected=selected,
                known=found,
            )
    if not prints:
        return [None] * len(views)
    if all_copies is None:
        all_copies = {fingerprint: [] for fingerprint in prints.values()}
        states[all_mail] = _scan(client, connection, deadline, all_mail, days, all_copies)
    real_copies: dict[tuple, list[dict[str, Any]]] = {
        fingerprint: [] for fingerprint in prints.values()
    }
    for folder in sorted(real, key=lambda name: (name.upper() != "INBOX", name)):
        states[folder] = _scan(client, connection, deadline, folder, days, real_copies)
    for view, fingerprint in prints.items():
        if len(real_copies[fingerprint]) == 1 and len(all_copies[fingerprint]) == 1:
            mapping[view] = real_copies[fingerprint][0]
    # A twin delivered after the All Mail scan could be the real-folder copy
    # found; a view message removed meanwhile could leave its twin unique.
    if any(location is not None for location in mapping.values()):
        _unchanged(client, connection, states)
    return [None if mapping[view] is None else dict(mapping[view]) for view in views]


def _scan_unproven(
    client: Any,
    connection: Any,
    deadline: Any,
    indexed: IndexedViews,
    *,
    real: set[str],
) -> list[dict[str, Any] | None]:
    """Finish an indexed lookup, scanning only real folders that fail their proof.

    A proven folder is skipped unless a row with no fingerprint could be the
    message. All Mail's single copy makes one live copy the message, so the scan
    stops once each fingerprint has one, trying folders with a cached row first.
    Mail arriving during the request waits for the next one, so nothing is reread.
    """
    from .search import _coverage_status

    if not indexed.pending:
        return indexed.locations
    index = client.search_index
    fingerprints = {item.fingerprint for item in indexed.pending.values()}
    copies: dict[tuple, list[dict[str, Any]]] = {fingerprint: [] for fingerprint in fingerprints}
    stamps = {fingerprint[1] for fingerprint in fingerprints}
    # Gluon reports INTERNALDATE in UTC, the day that ON compares.
    days = {datetime.fromtimestamp(stamp, UTC).date().isoformat() for stamp in stamps}
    holders = {folder for item in indexed.pending.values() for folder in item.holders}
    for folder in sorted(
        real, key=lambda name: (name not in holders, name.upper() != "INBOX", name)
    ):
        if all(copies.values()):
            break
        proof = indexed.proofs.get(folder)
        if proof is None:
            proof = index.coverage(folder)
            if proof is not None:
                deadline.check(connection)
                if _coverage_status(connection, folder) != proof:
                    proof = None
        if proof is None or index.may_hold_unfingerprinted(folder, proof[0], stamps):
            _scan(client, connection, deadline, folder, days, copies)
            for position, item in indexed.pending.items():
                found = copies[item.fingerprint]
                indexed.locations[position] = dict(found[0]) if len(found) == 1 else None
    return indexed.locations


def _refresh_indexed_folder(
    client: Any,
    connection: Any,
    deadline: Any,
    folder: str,
    current: tuple[str, int, str],
) -> tuple[str, int, str] | None:
    """Re-certify already cached real-folder headers with a cheap UID inventory.

    Expunges and arrivals whose headers were already observed need no FETCH.
    Missing headers retain the live resolver instead of starting a second
    catch-up operation during canonical lookup. The normal search reserve and
    generation guard also apply to this optional inventory.
    """
    from .mail import CallDeadlineExceeded
    from .search import _uidvalidity
    from .search_sync import has_time, optional_io

    index = client.search_index
    if getattr(getattr(client, "_reindex_service", None), "running", False):
        return None
    generation = index.generation
    known = index.known_uids(folder, current[0])
    if known is None or len(known) < current[1] or not has_time(client, deadline):
        return None
    try:
        with optional_io(client, deadline):
            state = client._select(connection, folder)
        validity = _uidvalidity(connection)
        if validity != current[0] or not has_time(client, deadline):
            return None
        with optional_io(client, deadline):
            inventory = client._search(connection, ["ALL"])
    except CallDeadlineExceeded:
        # optional_io restores the response budget, but the interrupted stream
        # must remain unusable even when a caller catches this lookup failure.
        deadline.end = min(deadline.end, time.monotonic())
        raise
    if not index.record_coverage(
        folder,
        validity,
        state,
        inventory,
        generation=generation,
        account=False,
    ):
        return None
    return index.coverage(folder)


def resolve_indexed_views(
    client: Any,
    connection: Any,
    deadline: Any,
    handles: list[dict[str, Any]],
    *,
    real: set[str],
    all_mail: str | None,
) -> IndexedViews | None:
    """Use certified fingerprints without rescanning days, for the folders that matter.

    All Mail must match its proof, or the result is None and the lookup stays live.
    Real folders are checked only if they hold a cached copy of a wanted fingerprint;
    both live STATUS passes must match each proof used. A changed holder can cheaply
    re-certify existing headers with a bounded UID inventory. A handle no proven
    folder holds stays in ``pending``; duplicate fingerprints keep their existing
    unresolved result. Message-ID never participates in this proof.
    """
    from .mail import MAX_THREAD_FOLDERS, CallDeadlineExceeded, MailError
    from .search import _coverage_status

    # A bounded read can opt its own session out of synchronous cache work.
    index = getattr(connection, "_search_index", getattr(client, "search_index", None))
    if (
        index is None
        or not real
        or all_mail is None
        or not handles
        or len(real) > MAX_THREAD_FOLDERS
        or any(item.get("folder") != all_mail for item in handles)
    ):
        return None
    proofs = {all_mail: index.coverage(all_mail)}
    if proofs[all_mail] is None:
        return None
    try:
        deadline.check(connection)
        if _coverage_status(connection, all_mail) != proofs[all_mail]:
            return None
        located = index.indexed_locations(handles, real, all_mail, proofs)
        if located is None:
            return None
        holders = {
            folder for item in located if isinstance(item, Unplaced) for folder in item.holders
        }
        for folder in sorted(holders):
            deadline.check(connection)
            current = _coverage_status(connection, folder)
            proof = index.coverage(folder)
            if proof != current:
                proof = _refresh_indexed_folder(client, connection, deadline, folder, current)
            if proof is not None:
                proofs[folder] = proof
        if holders:
            located = index.indexed_locations(handles, real, all_mail, proofs)
            if located is None:
                return None
        for folder in sorted(proofs):
            deadline.check(connection)
            if (
                _coverage_status(connection, folder) != proofs[folder]
                or index.coverage(folder) != proofs[folder]
            ):
                return None
    except CallDeadlineExceeded:
        raise
    except (MailError, imaplib.IMAP4.error, OSError):
        return None
    return IndexedViews(
        [None if isinstance(item, Unplaced) else item for item in located],
        {position: item for position, item in enumerate(located) if isinstance(item, Unplaced)},
        proofs,
    )
