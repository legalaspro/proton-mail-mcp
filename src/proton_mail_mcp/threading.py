"""Bounded, read-only thread expansion for Bridge's expensive header searches."""

from __future__ import annotations

import imaplib
import logging
import socket
import threading
from contextlib import contextmanager, suppress
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any

MAX_THREAD_DEPTH = 4
# Text bytes read per message: long newsletters and reports fit whole, still within the budget.
MAX_THREAD_BODY_BYTES = 262144
MAX_THREAD_HEADER_BYTES = 32768
MAX_THREAD_BODY_PARTS = 16
MAX_THREAD_SECONDS = 15.0
MAX_CANONICAL_SECONDS = 3.0
MAX_SEED_LOOKUP_SECONDS = 12.0
_METADATA_BATCH_SIZE = 100
_SEARCH_ID_BATCH_SIZE = 20
_SEED_UID_RANGE_SIZE = 100
# HEADER makes Bridge read every message in the mailbox. Above this size the search
# cannot be expected to fit one command's 10-second limit, and a timeout loses the stream.
_LIVE_HEADER_SEARCH_MAX_MESSAGES = 2000
# A live canonical scan reads about two database rows per All Mail message, about 0.2 ms each:
# above this size it cannot be expected to fit MAX_CANONICAL_SECONDS, so it is not started.
_LIVE_CANONICAL_SCAN_MAX_MESSAGES = 5000
# After the budget ends, a read whose stream was just closed may still unwind.
_CLOSE_GRACE_SECONDS = 0.25
_THREAD_TIMEOUT = "Thread read timed out; returning bounded message content collected so far"
_EXPANSION_LIMIT = "thread expansion limited"
_SEED_LOOKUP_LIMIT = "Search index has no verified message entry; bounded message lookup incomplete"
# Why a message's canonical fields stayed null.
_NO_SINGLE_COPY = "no_single_copy"
_LOOKUP_UNFINISHED = "lookup_unfinished"
_LOOKUP_SKIPPED = "lookup_skipped"
_LOGGER = logging.getLogger(__name__)


def _thread_folders(connection: Any) -> tuple[list[str], bool]:
    # Imported lazily: MailClient calls this module after its helpers are defined.
    from . import mail

    entries: list[tuple[str, set[str]]] = []
    for item in mail._checked(connection.list()):
        if item is None:
            continue
        if isinstance(item, tuple):
            item = item[0] + b"\r\n" + item[1]
        parser = mail._ImapValues(item)
        raw_flags, _, name = parser.value(), parser.value(), parser.value()
        flags = {mail._string(flag).lower() for flag in raw_flags or []}
        if "\\noselect" not in flags:
            entries.append((mail._decode_folder(name), flags))
    # Bridge label folders overlap. One All Mail mailbox covers every thread once.
    all_mail = [name for name, flags in entries if "\\all" in flags]
    if all_mail:
        return all_mail[:1], False
    entries.sort(key=lambda item: (item[0].upper() != "INBOX", "\\sent" not in item[1]))
    folders = list(dict.fromkeys(name for name, _ in entries))
    return folders[: mail.MAX_THREAD_FOLDERS], len(folders) > mail.MAX_THREAD_FOLDERS


def _criteria(identifiers: list[str]) -> list[Any]:
    from .mail import _message_id_search, _quote

    clauses = [
        ["HEADER", field, _quote(_message_id_search(value))]
        for value in identifiers
        for field in ("Message-ID", "References", "In-Reply-To")
    ]
    # SEARCH is a prefix grammar; this forms one OR tree in a single server scan.
    return ["OR"] * (len(clauses) - 1) + [word for clause in clauses for word in clause]


def _order(
    item: dict[str, Any], found: dict[str, Any], internaldate: float | None = None
) -> tuple[float, str, int]:
    try:
        timestamp = parsedate_to_datetime(item["date"])
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        stamp = timestamp.timestamp()
    except (ValueError, TypeError, OverflowError):
        stamp = internaldate if internaldate is not None else 0.0
    # Break ties by the mailbox handle the message was read from; canonical
    # fields can be null when no unique real-folder copy is known.
    return stamp, found["folder"], int(found["uid"])


def read_thread(
    client: Any,
    message_id: str,
    *,
    include_thread: bool = True,
    prefer_html: bool = False,
    include_bcc: bool = False,
    include_flags: bool = False,
) -> dict[str, Any]:
    from . import canonical, mail
    from .search import _coverage_status, _uidvalidity

    message_id = mail._message_id(message_id)
    if not isinstance(include_thread, bool):
        raise mail.MailValidationError("include_thread must be a boolean")
    if not isinstance(prefer_html, bool):
        raise mail.MailValidationError("prefer_html must be a boolean")
    if not isinstance(include_bcc, bool):
        raise mail.MailValidationError("include_bcc must be a boolean")
    if not isinstance(include_flags, bool):
        raise mail.MailValidationError("include_flags must be a boolean")
    deadline = client._deadline()
    # A single expensive Bridge MIME/header operation must end before the
    # caller's usual 60-second timeout, even with a longer general call budget.
    deadline.end = min(deadline.end, mail.time.monotonic() + MAX_THREAD_SECONDS)
    seed_socket_timeout = min(deadline.socket_timeout, MAX_SEED_LOOKUP_SECONDS)
    deadline.socket_timeout = min(deadline.socket_timeout, 10.0)
    content_socket_timeout = deadline.socket_timeout
    deadline.response_reserve = min(deadline.response_reserve, MAX_THREAD_SECONDS * 0.1)
    thread_end = deadline.end
    response_reserve = deadline.response_reserve
    expansion_end = None
    partial = False
    partial_reason = None
    timed_out = False
    canonical_limited = False
    seed_lookup_limited = False
    seed_inventories: dict[str, tuple[str, int]] = {}
    messages: dict[str | tuple[str, str], dict[str, Any]] = {}
    records: dict[str | tuple[str, str], tuple[str, str, dict[str, Any]]] = {}
    # The handle each message was read from, which may be an All Mail or label view.
    found: dict[str | tuple[str, str], dict[str, Any]] = {}
    validities: dict[str, str] = {}
    real: set[str] = set()
    completed_bodies: set[str | tuple[str, str]] = set()
    requested_identity: str | tuple[str, str] | None = None
    # Messages a finished canonical lookup found no single real-folder copy for.
    unplaced: set[str | tuple[str, str]] = set()
    selected_folder: str | None = None
    identifiers = {message_id}
    metadata_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    session: dict[str, Any] = {}
    # Wake the caller when canonical lookup changes the budget or I/O finishes.
    budget_changed_or_done = threading.Event()
    worker_done = threading.Event()
    abandoned = threading.Event()
    timings = dict.fromkeys(("seed", "expand", "bodies", "canonical"), 0.0)
    current_phase = None
    failed_phase = None
    phase_started = mail.time.monotonic()

    def start_phase(name: str | None) -> None:
        nonlocal current_phase, phase_started, expansion_end, failed_phase
        now = mail.time.monotonic()
        if current_phase is not None:
            timings[current_phase] += now - phase_started
        current_phase, phase_started = name, now
        if name == "expand" and include_thread and requested_identity is not None:
            if expansion_end is None:
                # Seed lookup and its body keep the full thread budget. Reserve
                # only what remains when relative-message expansion starts.
                expansion_end = thread_end - min(MAX_CANONICAL_SECONDS, (thread_end - now) / 2)
                deadline.end = expansion_end
                budget_changed_or_done.set()
        if name is None:
            failed_phase = None

    @contextmanager
    def phase(name: str):
        nonlocal failed_phase
        previous = current_phase
        start_phase(name)
        try:
            yield
        except mail.CallDeadlineExceeded:
            failed_phase = name
            raise
        finally:
            start_phase(previous)

    class ExpansionLimited(Exception):
        """Stop between commands, leaving the stream usable for canonical lookup."""

    def check(connection: Any) -> None:
        if abandoned.is_set():
            raise mail.CallDeadlineExceeded("Thread read ended")
        if expansion_end is not None and deadline.expired:
            raise ExpansionLimited
        deadline.check(connection)

    def limit_status() -> tuple[bool, str, bool]:
        if seed_lookup_limited:
            # A missing optional hint must not spend the full thread budget on
            # a cold-cache mailbox scan or turn that limited lookup into an
            # apparent timeout of the requested message's content.
            return True, _SEED_LOOKUP_LIMIT, False
        if canonical_limited:
            # The read is as it was: null canonical fields say all there is to say.
            return partial, partial_reason, timed_out
        if (
            expansion_end is not None
            and (failed_phase or current_phase) in {"expand", "bodies"}
            and mail.time.monotonic() >= expansion_end - response_reserve
        ):
            return True, _EXPANSION_LIMIT, True
        return True, _THREAD_TIMEOUT, True

    def select(connection: Any, folder: str) -> None:
        nonlocal selected_folder
        if selected_folder != folder:
            check(connection)
            client._select(connection, folder)
            validities[folder] = _uidvalidity(connection)
            selected_folder = folder

    def fetch_body_content(connection: Any, identity: str | tuple[str, str]) -> None:
        check(connection)
        folder, uid, metadata = records[identity]
        item = messages[identity]
        select(connection, folder)
        details: dict[str, Any] = {}
        try:
            body, attachments = client._body(
                connection,
                uid,
                max_bytes=MAX_THREAD_BODY_BYTES,
                structure=metadata.get("structure"),
                deadline=deadline,
                max_parts=MAX_THREAD_BODY_PARTS,
                details=details,
                # Only views ask for markup; the default stays plain-text first.
                **({"prefer_html": True} if prefer_html else {}),
            )
        except Exception:
            # Keep known sizes and transfer progress if the stream timed out.
            if "text" in details and details.get("body_type") != "text/html":
                details["text"] = mail._collapse_filler(details["text"])
            item.update(details, truncated=True)
            raise
        if details.get("body_type") != "text/html":
            # Only markup for a view keeps its characters as they are.
            body = mail._collapse_filler(body)
        item.update(
            details,
            text=body[:MAX_THREAD_BODY_BYTES],
            attachments=attachments,
            truncated=details["truncated"]
            or metadata.get("headers_truncated", False)
            or len(body) > MAX_THREAD_BODY_BYTES,
        )
        completed_bodies.add(identity)

    def fetch_body(connection: Any, identity: str | tuple[str, str]) -> None:
        with phase("bodies"):
            fetch_body_content(connection, identity)

    def ingest(
        connection: Any, folder: str, matches: list[str], frontier: list[str], *, seed: bool = False
    ) -> list[str]:
        nonlocal partial, requested_identity
        candidates = sorted(set(matches), key=int)
        cached = [uid for uid in candidates if (folder, uid) in metadata_cache]
        fresh = [uid for uid in candidates if (folder, uid) not in metadata_cache]
        available = max(0, mail.MAX_THREAD_CANDIDATES - len(metadata_cache))
        if len(fresh) > available:
            partial = True
            fresh = fresh[:available]
        next_frontier = []
        # A previously nonmatching header may connect to a later BFS frontier.
        # Re-filter cached candidates while charging only new fetches to the cap.
        batches = [
            (uids[offset : offset + _METADATA_BATCH_SIZE], fetch)
            for uids, fetch in ((cached, False), (fresh, True))
            for offset in range(0, len(uids), _METADATA_BATCH_SIZE)
        ]
        for batch, fetch in batches:
            check(connection)
            if fetch:
                metadata_by_uid = client._metadata(
                    connection,
                    batch,
                    deadline=deadline,
                    bodystructure=False,
                    size=True,
                    header_max_bytes=MAX_THREAD_HEADER_BYTES,
                    # Only the draft composer asks for Bcc; the read_thread tool never does.
                    **({"bcc": True} if include_bcc else {}),
                    # The composer asks whether the message it replies to is a saved draft.
                    **({"flags": True} if include_flags else {}),
                )
                for uid in batch:
                    metadata_cache[folder, uid] = metadata_by_uid.get(uid)
            for uid in batch:
                metadata = metadata_cache[folder, uid]
                if metadata is None:
                    partial = True
                    continue
                if metadata.get("headers_truncated"):
                    partial = True
                headers = metadata["headers"]
                try:
                    own_ids = [mail._message_id(mail._raw_header_value(headers))]
                except mail.MailValidationError:
                    own_ids = []
                links = mail._ids(str(headers.get("References", ""))) + mail._ids(
                    str(headers.get("In-Reply-To", ""))
                )
                if not set(frontier).intersection(own_ids + ([] if seed else links)):
                    continue
                identity: str | tuple[str, str] = own_ids[0] if own_ids else (folder, uid)
                if identity not in messages:
                    if len(messages) >= mail.MAX_THREAD_MESSAGES:
                        partial = True
                        continue
                    item = client._summary(headers, uid, folder, validities[folder])
                    item.update(
                        headers={name: str(value) for name, value in headers.items()},
                        text="",
                        attachments=[],
                        truncated=True,
                        size_bytes=metadata["size"],
                        headers_fetched_bytes=metadata["headers_fetched_bytes"],
                        headers_truncated=metadata["headers_truncated"],
                        body_size_bytes=None,
                        body_fetched_bytes=0,
                        body_limit_bytes=MAX_THREAD_BODY_BYTES,
                        body_parts_fetched=0,
                        body_parts_limit=MAX_THREAD_BODY_PARTS,
                    )
                    if include_flags:
                        item["flags"] = metadata["flags"]
                    # Direct reads expose only the alias, even in a real folder.
                    found[identity] = canonical.handle(folder, validities[folder], uid)
                    canonical.describe(
                        item,
                        found[identity],
                        found[identity] if include_thread and folder in real else None,
                    )
                    # Publish the message last: a reply at the deadline may copy it.
                    records[identity] = folder, uid, metadata
                    messages[identity] = item
                if message_id in own_ids and requested_identity is None:
                    requested_identity = identity
                if include_thread:
                    for identifier in own_ids + links:
                        if identifier not in identifiers:
                            if len(identifiers) >= mail.MAX_THREAD_IDS:
                                partial = True
                                continue
                            identifiers.add(identifier)
                            next_frontier.append(identifier)
            if requested_identity is not None and requested_identity not in completed_bodies:
                fetch_body(connection, requested_identity)
        return next_frontier

    def collect() -> None:
        nonlocal partial, partial_reason, timed_out, canonical_limited, real
        all_mail = None

        def views() -> list[str | tuple[str, str]]:
            return [identity for identity in messages if messages[identity]["folder"] is None]

        def prepare_lookup() -> None:
            start_phase(None)
            deadline.end = min(thread_end, mail.time.monotonic() + MAX_CANONICAL_SECONDS)
            deadline.response_reserve = min(response_reserve, MAX_CANONICAL_SECONDS * 0.1)
            budget_changed_or_done.set()

        def resolve(connection: Any, *, indexed_only: bool = False) -> None:
            nonlocal canonical_limited, selected_folder
            identities = views()
            handles = [found[identity] for identity in identities]
            indexed = locations = None
            finished = False
            connection._search_index = client.search_index
            start_phase("canonical")
            # The first pass comes before expansion, so a read stopped there is still incomplete.
            canonical_limited = not indexed_only
            try:
                indexed = canonical.resolve_indexed_views(
                    client, connection, deadline, handles, real=real, all_mail=all_mail
                )
                connection._search_index = None
                too_large = indexed is None and (
                    seed_inventories.get(all_mail, ("", 0))[1] > _LIVE_CANONICAL_SCAN_MAX_MESSAGES
                )
                if not indexed_only and not too_large and (indexed is None or indexed.pending):
                    locations = canonical.resolve_views(
                        client,
                        connection,
                        deadline,
                        handles,
                        real=real,
                        all_mail=all_mail,
                        indexed=indexed,
                    )
                    finished = all_mail is not None
                elif indexed is not None and not indexed.pending:
                    finished = True
            except (mail.MailError, imaplib.IMAP4.error, OSError) as error:
                # Optional: canonical fields stay null, and the read is not incomplete for that.
                _LOGGER.debug("read_thread canonical lookup ended: %s", type(error).__name__)
            finally:
                if locations is None and indexed is not None:
                    # A scan that stopped keeps what the index proved and what it placed.
                    locations = indexed.locations
                if locations is not None:
                    for identity, location in zip(identities, locations):
                        canonical.describe(messages[identity], found[identity], location)
                        if finished and location is None:
                            unplaced.add(identity)
                # The index path may have selected a mailbox while its writes were on.
                connection._search_index = connection._search_selection = None
                # Indexed proof refreshes can select a real folder.
                selected_folder = getattr(connection, "_selected_inventory", {}).get("folder")
                canonical_limited = False
                start_phase(None)

        def catch_up_all_mail(connection: Any) -> None:
            """Bring All Mail's proof to this epoch, for local expansion and canonical lookup.

            New mail makes the proof stale, and no other part of a read renews it. Optional
            work under the search reserve, costing about what arrived; the bodies already
            read are safe. A changed UIDVALIDITY is a reset that belongs to a search.
            """
            nonlocal failed_phase, selected_folder
            from .search import _index_rebuilding
            from .search_sync import sync_all_mail

            index = client.search_index
            epoch = index.coverage_epoch(all_mail) if all_mail is not None else None
            if (
                epoch is None
                or selected_folder != all_mail
                or epoch[0] != validities[all_mail]
                or _index_rebuilding(client)
            ):
                return
            connection._search_index = index
            connection._search_selection = (all_mail, epoch[0])
            try:
                synced = sync_all_mail(client, connection, deadline, all_mail)
            except (mail.MailError, imaplib.IMAP4.error, OSError):
                return  # Optional: expansion and the lookup have their own paths.
            finally:
                connection._search_index = None
                connection._search_selection = None
                selected_folder = connection._selected_inventory["folder"]
            if synced.stream_broken:
                # No further command may use this stream; a new session finishes the lookup.
                failed_phase = "expand"
                raise mail.CallDeadlineExceeded("Thread read ended")

        start_phase("seed")
        try:

            def collect_messages(connection: Any, folders: list[str]) -> None:
                nonlocal partial, partial_reason, seed_lookup_limited
                frontier = [message_id]
                # Retain headers before potentially expensive MIME structure/body I/O,
                # then fetch the requested body before searching any reply links.
                # This also handles Unicode IDs through imaplib's continuation literal.
                # Confirm every folder's index hint before live-searching any of
                # them. An empty folder needs neither, whatever its UIDNEXT.
                unscanned = []
                for folder in folders:
                    select(connection, folder)
                    seed_inventories[folder] = (
                        validities[folder],
                        connection._selected_inventory["exists"],
                    )
                    if not connection._selected_inventory["exists"]:
                        continue
                    unscanned.append(folder)
                    indexed_uid = client.search_index.seed_uid(
                        folder, validities[folder], message_id
                    )
                    check(connection)
                    if indexed_uid is not None:
                        previous_partial = partial
                        frontier.extend(
                            ingest(connection, folder, [indexed_uid], [message_id], seed=True)
                        )
                        if requested_identity is None:
                            # Stale optional hints are misses, not incomplete mail.
                            partial = previous_partial
                    if requested_identity is not None:
                        break
                fallback_end = min(thread_end, mail.time.monotonic() + MAX_SEED_LOOKUP_SECONDS)
                for folder in unscanned:
                    if requested_identity is not None:
                        break
                    # Folder reselection and any missing UIDNEXT evidence are
                    # part of discovery too, so they share the search's cap.
                    seed_lookup_limited = True
                    deadline.end = fallback_end
                    deadline.socket_timeout = seed_socket_timeout
                    deadline.response_reserve = min(response_reserve, MAX_SEED_LOOKUP_SECONDS * 0.1)
                    budget_changed_or_done.set()
                    select(connection, folder)
                    uidnext = connection._selected_inventory["uidnext"]
                    if uidnext is None:
                        validity, _, uidnext = _coverage_status(connection, folder)
                        if validity != validities[folder]:
                            raise mail.MailError("Mailbox changed during thread read")
                    value = mail._message_id_search(message_id)
                    upper = int(uidnext) - 1
                    # Hundreds of small HEADER searches can consume the call
                    # before reaching an old message after cache loss. A single
                    # Message-ID lookup has one budget shared by all folders;
                    # raw headers still verify every returned hint. Small UID
                    # spaces retain their existing one-range lookup.
                    check(connection)
                    matches = []
                    if upper > 0:
                        criteria = ["HEADER", "Message-ID"]
                        if upper <= _SEED_UID_RANGE_SIZE:
                            criteria = ["UID", f"1:{upper}", *criteria]
                        if value.isascii():
                            matches = client._search(connection, [*criteria, mail._quote(value)])
                        else:
                            matches = client._search(
                                connection, criteria, literal=value.encode("utf-8")
                            )
                    check(connection)
                    # Bodies retain the remaining full call budget. Only
                    # fallback discovery is limited by the shared deadline.
                    seed_lookup_limited = False
                    deadline.end = thread_end
                    deadline.socket_timeout = content_socket_timeout
                    deadline.response_reserve = response_reserve
                    budget_changed_or_done.set()
                    frontier.extend(ingest(connection, folder, matches, [message_id], seed=True))
                if include_thread and views():
                    catch_up_all_mail(connection)
                    resolve(connection, indexed_only=True)
                start_phase("expand")
                depth = 0
                while include_thread and frontier and depth < MAX_THREAD_DEPTH:
                    check(connection)
                    next_frontier: list[str] = []
                    for folder in folders:
                        select(connection, folder)
                        inventory = connection._selected_inventory
                        matches = client.search_index.thread_uids(
                            folder, validities[folder], inventory, frontier
                        )
                        if matches is None and inventory["exists"] > (
                            _LIVE_HEADER_SEARCH_MAX_MESSAGES
                        ):
                            # A search this size would only time out. Cached links still
                            # find relatives, and ingest confirms each one by FETCH.
                            matches = client.search_index.thread_hints(
                                folder, validities[folder], frontier
                            )
                            if matches is not None:
                                partial, partial_reason = True, _EXPANSION_LIMIT
                        if matches is None:
                            # Keep OR commands bounded and ASCII-only. imaplib supports
                            # one literal per command, so Unicode IDs need one HEADER
                            # search per field rather than a whole-mailbox fallback.
                            ascii_ids = [
                                identifier for identifier in frontier if identifier.isascii()
                            ]
                            matches = []
                            for offset in range(0, len(ascii_ids), _SEARCH_ID_BATCH_SIZE):
                                check(connection)
                                matches.extend(
                                    client._search(
                                        connection,
                                        _criteria(
                                            ascii_ids[offset : offset + _SEARCH_ID_BATCH_SIZE]
                                        ),
                                    )
                                )
                            for identifier in frontier:
                                if not identifier.isascii():
                                    for field in ("Message-ID", "References", "In-Reply-To"):
                                        check(connection)
                                        matches.extend(
                                            client._header_search(
                                                connection,
                                                field,
                                                mail._message_id_search(identifier),
                                            )
                                        )
                        next_frontier.extend(ingest(connection, folder, matches, frontier))
                    frontier = next_frontier
                    depth += 1
                if include_thread and frontier and depth >= MAX_THREAD_DEPTH:
                    partial = True
                start_phase("bodies")
                for identity in messages:
                    if identity not in completed_bodies:
                        fetch_body(connection, identity)

            with client._session(deadline=deadline) as (connection, _):
                session["connection"] = connection
                # Disable index maintenance during content reads. The seed hint
                # is verified by FETCH; canonical lookup can reuse indexed proofs.
                connection._search_index = None
                folders, partial = _thread_folders(connection)
                _, real, all_mail = canonical.mailbox_roles(connection)

                try:
                    collect_messages(connection, folders)
                except ExpansionLimited:
                    partial = True
                    partial_reason = _EXPANSION_LIMIT
                if abandoned.is_set():
                    raise mail.CallDeadlineExceeded("Thread read ended")
                # Only a between-command stop can reuse this IMAP stream.
                start_phase(None)
                deadline.end = thread_end
                deadline.response_reserve = response_reserve
                budget_changed_or_done.set()
                # Bodies come first. A failed lookup leaves view handles unresolved
                # and the result partial. Use search_mail to get canonical
                # handles for direct reads, which retain only the alias.
                if views() and include_thread:
                    prepare_lookup()
                    resolve(connection)
        except mail.CallDeadlineExceeded:
            failed_during_content = (failed_phase or current_phase) in {"expand", "bodies"}
            partial, partial_reason, timed_out = limit_status()
            if (
                failed_during_content
                and include_thread
                and not abandoned.is_set()
                and mail.time.monotonic() < thread_end
                and views()
            ):
                # A command watchdog may have cut the stream and shortened the
                # deadline. Let the broken session unwind before spending the
                # canonical reserve on a fresh connection, including its setup.
                prepare_lookup()
                try:
                    with client._session(
                        deadline=deadline,
                        on_connect=lambda connection: session.update(connection=connection),
                    ) as (connection, _):
                        session["connection"] = connection
                        if not abandoned.is_set():
                            resolve(connection)
                except mail.CallDeadlineExceeded:
                    partial, partial_reason, timed_out = limit_status()
                except mail.MailError:
                    partial = True
        except mail.MailError:
            if requested_identity is None:
                raise
            # Any failure after the seed was found ends I/O. Its already-fetched
            # body and every successfully collected relative remain useful.
            partial = True

    failures: list[BaseException] = []

    def run() -> None:
        try:
            collect()
        except BaseException as error:  # Raised again by the caller below.
            failures.append(error)
        finally:
            start_phase(None)
            worker_done.set()
            budget_changed_or_done.set()

    # Command watchdogs end I/O at the deadline's cutoff, but only for socket
    # reads they can interrupt. Bound the whole read too: a step that has not
    # returned when the budget ends cannot hold the reply.
    worker = threading.Thread(target=run, name="read_thread", daemon=True)
    worker.start()
    while not worker_done.is_set():
        budget_changed_or_done.clear()
        if worker_done.is_set():
            break
        remaining = deadline.end - mail.time.monotonic()
        if remaining <= 0:
            break
        budget_changed_or_done.wait(remaining)
    if not worker_done.is_set():
        # Refuse further I/O and wake a blocked read, as a command watchdog does.
        status = limit_status()
        abandoned.set()
        deadline.end = min(deadline.end, mail.time.monotonic())
        sock = getattr(session.get("connection"), "sock", None)
        if sock is not None:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        worker.join(_CLOSE_GRACE_SECONDS)
        # The limit stands even if the read returns within the grace, as from
        # a cut logout that cleanup suppresses. Copy what has been read; a read
        # still running may change it. The canonical cap only limits handles.
        collected = {identity: dict(item) for identity, item in list(messages.items())}
    elif failures:
        raise failures[0]
    else:
        collected, status = messages, (partial, partial_reason, timed_out)
    for identity, item in collected.items():
        if item["folder"] is None:
            if not include_thread:
                item["canonical_reason"] = _LOOKUP_SKIPPED
            elif identity in unplaced:
                item["canonical_reason"] = _NO_SINGLE_COPY
            else:
                item["canonical_reason"] = _LOOKUP_UNFINISHED
    incomplete, reason, expired = status
    ordered = sorted(
        collected,
        key=lambda identity: _order(
            collected[identity], found[identity], records[identity][2].get("internaldate")
        ),
    )
    result = {"messages": [collected[identity] for identity in ordered], "partial": incomplete}
    if reason is not None:
        result["partial_reason"] = reason
    if expired:
        result.update(timed_out=True, error=_THREAD_TIMEOUT)
    elapsed = dict(timings)
    active_phase, active_started = current_phase, phase_started
    if active_phase is not None:
        elapsed[active_phase] += mail.time.monotonic() - active_started
    _LOGGER.debug(
        "read_thread timing seed=%.3fs expand=%.3fs bodies=%.3fs canonical=%.3fs",
        *(elapsed[name] for name in ("seed", "expand", "bodies", "canonical")),
    )
    if reason == _SEED_LOOKUP_LIMIT:
        from .index_status import attach_index_status

        # Only a limited seed lookup can depend on missing index headers. A
        # successful live fallback, body truncation or expansion limit does not.
        counts = [
            client.search_index.reporting_count(folder, validity)
            for folder, (validity, _) in list(seed_inventories.items())
        ]
        target = sum(total for _, total in list(seed_inventories.values()))
        indexed_count = sum(count or 0 for count in counts)
        headers_missing = any(count is None for count in counts) or indexed_count < target
        return attach_index_status(
            client,
            result,
            depends_on_index=True,
            incomplete=headers_missing,
            indexed_count=indexed_count,
            target=target,
        )
    return result
