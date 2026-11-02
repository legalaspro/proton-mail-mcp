"""Resumable, read-only startup repair of the serving header index.

Each short session owns its socket and at most one header batch. A foreground
request interrupts that socket without waiting. Headers are committed directly
to the optional cache, so a restart only fetches the remaining inventory.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, Iterator

from . import canonical
from .imap_idle import _interrupt
from .index_status import RebuildRate, progress_fields
from .mail import CallDeadlineExceeded, MailAuthenticationError, MailError, _Deadline
from .search import _coverage_status, _uidvalidity

_LOG = logging.getLogger(__name__)
_SESSION_SECONDS = 20.0
_RETRY_SECONDS = 1.0
_MAX_RETRY_SECONDS = 60.0
_MAX_HEADER_ATTEMPTS = 3
_MAX_FAILURES = 10
# Reasons that end recovery at once, whatever the mailbox does meanwhile.
_TERMINAL = {"authentication_failed", "no_all_mail", "index_unavailable", "invalid_inventory"}


class _Yielded(MailError):
    pass


class _Retry(MailError):
    """Only fixed internal reason codes may leave the worker."""


@dataclass
class _Work:
    folder: str
    validity: str
    members: list[str]
    pending: list[str]
    generation: int
    unchanged: bool = False
    attempts: dict[str, int] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)


class IndexAutoRebuild:
    def __init__(self, client: Any, settings: Any):
        self.client = client
        self.settings = settings
        self._on = settings.index_autorebuild_enabled
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._yielded = threading.Event()
        self._lock = threading.Lock()
        self._active = 0
        self._connection: Any = None
        self._thread: threading.Thread | None = None
        self._all_mail: str | None = None
        self._total = 0
        self._done = 0
        self._rate = RebuildRate()
        self._header_attempts: dict[tuple[str, str, int], dict[str, int]] = {}
        self._incomplete: dict[tuple[str, str, int], set[str]] = {}
        # Each folder's last EXISTS and how often any folder grew: Bridge's first sync
        # after a login keeps adding mail, and no inventory taken meanwhile holds still.
        self._exists: dict[str, int] = {}
        self._growth = 0
        self._syncing = False
        self.manual_run_id: str | None = None
        self._state: dict[str, Any] = {
            "state": "idle" if self._on else "disabled",
            "source": "auto",
            "indexed_count": 0,
            "target": None,
            "percent": None,
            "percent_done": 0.0,
            "eta_seconds": None,
            "last_error": None,
        }

    def start(self) -> None:
        """Launch without opening IMAP or waiting for any startup inventory."""
        if not self._on or self._thread is not None:
            return
        self.manual_run_id = self.client.reindex.status().get("run_id")
        if self.client.search_index.recovery_complete():
            # Cached certified coverage is enough to decide whether recovery is
            # needed. Startup must not read credentials or open a healthy inbox.
            self._update(state="idle", last_error=None, eta_seconds=None)
            return
        self._stop.clear()
        self._rate = RebuildRate()
        self._header_attempts.clear()
        self._incomplete.clear()
        self._exists.clear()
        self._syncing = False
        self._update(state="queued", last_error=None, eta_seconds=None)
        self._thread = threading.Thread(
            target=self._run, name="proton-mail-index-autorebuild", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            connection = self._connection
        if connection is not None:
            with suppress(Exception):
                _interrupt(connection)
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    @contextmanager
    def foreground(self) -> Iterator[None]:
        """Give foreground calls priority without waiting for the worker."""
        if not self._on:
            yield
            return
        with self._lock:
            self._active += 1
            connection = self._connection
            if connection is not None:
                self._yielded.set()
            if self._state["state"] == "running":
                self._state["state"] = "queued"
        if connection is not None:
            with suppress(Exception):
                _interrupt(connection)
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1
            self._wake.set()

    def status(self) -> dict[str, Any]:
        """Return progress from memory; this never issues IMAP or waits for I/O."""
        with self._lock:
            return dict(self._state)

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def _check(self) -> None:
        with self._lock:
            active = self._active
        if self._stop.is_set() or active:
            raise _Yielded

    def _register(self, connection: Any) -> None:
        with self._lock:
            self._connection = connection
            active = self._active
        if active or self._stop.is_set():
            self._yielded.set()
            _interrupt(connection)
            raise _Yielded

    @contextmanager
    def _session(self) -> Iterator[tuple[Any, _Deadline]]:
        self._check()
        self._yielded.clear()
        index = self.client.search_index
        guard = index.acquire_reindex_guard()
        try:
            self._check()
            # This guard excludes a manual run, including its final swap.
            self.manual_run_id = self.client.reindex.status().get("run_id")
            # While Bridge still syncs, a running session keeps saying so.
            self._update(state="running", last_error="bridge_syncing" if self._syncing else None)
            seconds = min(_SESSION_SECONDS, self.settings.call_timeout)
            deadline = _Deadline(seconds, min(seconds, self.settings.timeout))
            with self.client._session(deadline=deadline, on_connect=self._register) as (
                connection,
                _,
            ):
                try:
                    self._check()
                    yield connection, deadline
                    self._check()
                    if self._yielded.is_set():
                        raise _Yielded
                except Exception:
                    if self._yielded.is_set() or self._stop.is_set():
                        # An interrupted stream must not wait for LOGOUT.
                        deadline.end = time.monotonic()
                    raise
        finally:
            with self._lock:
                self._connection = None
            os.close(guard)

    def _observe(self, folder: str, exists: int) -> None:
        before = self._exists.get(folder)
        self._exists[folder] = exists
        if before is not None and exists > before:
            self._growth += 1

    @staticmethod
    def _valid_inventory(members: list[str], state: dict[str, Any]) -> None:
        if len(members) != len(set(members)) or len(members) != state["exists"]:
            raise _Retry("inventory_changed")
        if any(not uid.isascii() or not uid.isdigit() or int(uid) < 1 for uid in members):
            raise _Retry("invalid_inventory")

    def _discover(self) -> list[str]:
        index = self.client.search_index
        if not index.enabled:
            raise _Retry("index_unavailable")
        with self._session() as (connection, _):
            attributes, real, _ = canonical.mailbox_roles(connection)
            all_mail = [
                name
                for name, flags in attributes.items()
                if "\\all" in flags and "\\noselect" not in flags
            ]
            if len(all_mail) != 1:
                raise _Retry("no_all_mail")
            self._all_mail = all_mail[0]
            # Empty caches must persist their first inventory before any FETCH.
            # Initialization uses the same nonblocking private file lock as all
            # SearchIndex writers; it never resets an existing v2 database.
            if not index._initialized:
                index._initialize(create=True)
            folders = [self._all_mail, *sorted(real - {self._all_mail})]
            index.record_recovery_complete(False, folders=folders)
            index.set_all_mail(self._all_mail)
            return folders

    def _load(self, folder: str) -> _Work:
        index = self.client.search_index
        with self._session() as (connection, _):
            state = self.client._select(connection, folder)
            self._observe(folder, state["exists"])
            validity = _uidvalidity(connection)
            key = (folder, validity, index.generation)
            for ledger in (self._header_attempts, self._incomplete):
                for old in list(ledger):
                    if old[2] != key[2] or (old[0] == folder and old != key):
                        del ledger[old]
            if index.coverage(folder) == (validity, state["exists"], state["uidnext"]):
                self._header_attempts.pop(key, None)
                self._incomplete.pop(key, None)
                if folder == self._all_mail:
                    counts = index.reporting_status() or {}
                    progress = self._rate.observe(
                        counts.get("indexed_count", state["exists"]),
                        state["exists"],
                        time.monotonic(),
                    )
                    if progress["eta_seconds"] == 0:
                        progress["eta_seconds"] = None
                    self._update(**{**counts, **progress, "percent_done": progress["percent"]})
                _LOG.warning(
                    "Search index auto rebuild progress reason=unchanged count=%d", state["exists"]
                )
                return _Work(folder, validity, [], [], index.generation, unchanged=True)
            # Validate before reconciling below. Ordinary _search(ALL) eagerly
            # reconciles; doing both would scan the whole cache twice here.
            connection._search_selection = None
            members = self.client._search(connection, ["ALL"])
            self._valid_inventory(members, state)
            index.reconcile(folder, validity, members)
            known = index.known_uids(folder, validity)
            if known is None:
                raise _Retry("index_unavailable")
            # An inventory retry must not restart an unstorable UID's attempt
            # budget. Alternating successful FETCHes and transport failures can
            # otherwise keep the same bad header at the front forever.
            unknown = set(members) - known
            attempts = self._header_attempts.setdefault(key, {})
            for uid in list(attempts):
                if uid not in unknown:
                    del attempts[uid]
            skipped = self._incomplete.get(key, set())
            skipped.intersection_update(unknown)
            if not skipped:
                self._incomplete.pop(key, None)
            work = _Work(
                folder,
                validity,
                members,
                sorted(unknown - skipped, key=int, reverse=True),
                key[2],
                attempts=attempts,
                skipped=skipped,
            )
            self._total = len(members)
            self._done = len(members) - len(work.pending)
            self._progress(work)
            return work

    def _progress(self, work: _Work) -> None:
        # Public counts describe All Mail, rather than duplicated real handles.
        self._done = len(work.members) - len(work.pending) - len(work.skipped)
        fields: dict[str, Any] = {}
        if work.folder == self._all_mail:
            counts = self.client.search_index.reporting_status()
            if counts is not None:
                fields.update(counts)
            fields.update(self._rate.observe(self._done, self._total, time.monotonic()))
            if fields["eta_seconds"] == 0:
                fields["eta_seconds"] = None
            fields["percent_done"] = fields["percent"]
        else:
            # All Mail counts cannot estimate duplicated real-folder work.
            # Keep coverage counts but withhold an ETA until that phase ends.
            fields["eta_seconds"] = None
        self._update(**fields)
        _LOG.info(
            "Search index auto rebuild progress indexed=%d count=%d remaining=%d",
            self._done,
            len(work.members),
            len(work.pending),
        )

    def _batch(self, work: _Work) -> None:
        index = self.client.search_index
        batch = work.pending[: min(200, self.settings.reindex_batch_size)]
        with self._session() as (connection, deadline):
            self._observe(work.folder, self.client._select(connection, work.folder)["exists"])
            if _uidvalidity(connection) != work.validity or index.generation != work.generation:
                raise _Retry("epoch_changed")
            self.client._metadata(
                connection,
                batch,
                deadline=deadline,
                headers=True,
                bodystructure=False,
                internaldate=True,
                envelope=True,
                size=True,
            )
            count = index.known_count(work.folder, work.validity, uids=batch)
            # Normal batches need only a count restricted to these UIDs. Read
            # the folder's known set only to identify an incomplete batch.
            known = (
                set(batch)
                if count == len(batch)
                else index.known_uids(work.folder, work.validity)
                if count is not None
                else None
            )
            if known is None:
                raise _Retry("index_unavailable")
            # A successful FETCH can still leave headers unknown (for example,
            # SQLite cannot safely search text containing NUL). Bound attempts
            # per UID and continue warming the rest of this and later folders.
            missing = set(batch) - known
            for uid in missing:
                work.attempts[uid] = work.attempts.get(uid, 0) + 1
                if work.attempts[uid] >= _MAX_HEADER_ATTEMPTS:
                    work.skipped.add(uid)
            if work.skipped:
                self._incomplete[(work.folder, work.validity, work.generation)] = work.skipped
            work.pending = [
                uid for uid in work.pending if uid not in known and uid not in work.skipped
            ]
            index.record_catchup(work.folder, work.validity, generation=work.generation)
            self._progress(work)

    def _finish(self, work: _Work) -> bool:
        """Certify only a fresh inventory; a UID maximum never proves coverage."""
        if work.unchanged:
            return True
        index = self.client.search_index
        with self._session() as (connection, _):
            state = self.client._select(connection, work.folder)
            self._observe(work.folder, state["exists"])
            if _uidvalidity(connection) != work.validity or index.generation != work.generation:
                raise _Retry("epoch_changed")
            connection._search_selection = None
            members = self.client._search(connection, ["ALL"])
            self._valid_inventory(members, state)
            if _coverage_status(connection, work.folder) != (
                work.validity,
                state["exists"],
                state["uidnext"],
            ):
                raise _Retry("inventory_changed")
            index.reconcile(work.folder, work.validity, members)
            known = index.known_uids(work.folder, work.validity)
            if known is None:
                raise _Retry("index_unavailable")
            work.members = members
            work.pending = sorted(set(members) - known - work.skipped, key=int, reverse=True)
            self._total = len(members)
            self._progress(work)
            if work.pending:
                return False
            if work.skipped:
                # Missing header data must never receive a coverage certificate.
                return True
            if not index.record_coverage(
                work.folder,
                work.validity,
                state,
                members,
                generation=work.generation,
                account=work.folder == self._all_mail,
            ):
                raise _Retry("headers_incomplete")
            index.record_catchup(work.folder, work.validity, generation=work.generation)
            self._progress(work)
            return True

    def _wait(self, seconds: float, *, backoff: bool = False) -> None:
        if backoff:
            # Foreground calls may wake a yielded batch, but must not turn a
            # failed connection's backoff into a credential/login retry loop.
            self._stop.wait(seconds)
            return
        self._wake.wait(seconds)
        self._wake.clear()

    def _run(self) -> None:
        _LOG.warning("Search index auto rebuild start source=auto")
        folders: list[str] | None = None
        position = 0
        work: _Work | None = None
        retry = _RETRY_SECONDS
        failures: dict[tuple[str, str | None], int] = {}
        operation: tuple[str, str | None] = ("discover", None)
        growth = self._growth
        waits = 0
        while not self._stop.is_set():
            try:
                self._check()
                if folders is None:
                    operation = ("discover", None)
                    folders = self._discover()
                    failures.pop(operation, None)
                if work is None:
                    operation = ("load", folders[position])
                    work = self._load(folders[position])
                    failures.pop(operation, None)
                if work.pending:
                    operation = ("batch", work.folder)
                    self._batch(work)
                    failures.pop(operation, None)
                    self._wait(self.settings.reindex_batch_sleep)
                else:
                    operation = ("finish", work.folder)
                    finished = self._finish(work)
                    failures.pop(operation, None)
                    if not finished:
                        continue
                    # A certified folder held still: whatever synced has settled.
                    self._syncing = False
                    position += 1
                    work = None
                    if position == len(folders):
                        # All Mail may have changed while real handles warmed.
                        if len(folders) > 1 and folders[-1] != self._all_mail:
                            folders.append(self._all_mail)
                        else:
                            operation = ("complete", None)
                            counts = self.client.search_index.reporting_status() or {}
                            progress = progress_fields(
                                counts.get("indexed_count", self._done),
                                counts.get("all_mail_count", self._total),
                            )
                            if not self._incomplete:
                                self.client.search_index.record_recovery_complete(True)
                            self._update(
                                **{
                                    **counts,
                                    **progress,
                                    "state": "failed" if self._incomplete else "succeeded",
                                    "last_error": "headers_incomplete"
                                    if self._incomplete
                                    else None,
                                    "percent_done": progress["percent"],
                                }
                            )
                            _LOG.warning(
                                "Search index auto rebuild finish indexed=%d count=%d",
                                counts.get("indexed_count") or 0,
                                counts.get("all_mail_count") or 0,
                            )
                            return
                retry = _RETRY_SECONDS
            except Exception as error:
                with self._lock:
                    paused = self._active > 0
                if self._stop.is_set():
                    break
                if paused or self._yielded.is_set() or isinstance(error, _Yielded):
                    self._yielded.clear()
                    self._update(state="queued", last_error=None)
                    self._wait(0.1)
                    continue
                if isinstance(error, BlockingIOError) or (
                    isinstance(error, _Retry)
                    and error.args[0] == "index_unavailable"
                    and self.client.search_index.enabled
                ):
                    reason = "index_busy"
                    self._update(state="queued", last_error=reason)
                else:
                    reason = (
                        error.args[0]
                        if isinstance(error, _Retry)
                        else "authentication_failed"
                        if isinstance(error, MailAuthenticationError)
                        else "imap_timeout"
                        if isinstance(error, CallDeadlineExceeded)
                        else "rebuild_failed"
                    )
                    # A mailbox that grew since the last failure is still being
                    # downloaded, as in Bridge's first sync after a login. Such a
                    # failure keeps the backoff but not the retry budget, so a long
                    # sync is waited out instead of ending recovery for good.
                    self._syncing = reason not in _TERMINAL and self._growth > growth
                    growth = self._growth
                    if self._syncing:
                        reason = "bridge_syncing"
                    self._update(state="queued", last_error=reason)
                    if self._syncing:
                        # Its own backoff: a successful step between two such failures
                        # must not bring back one-second rereads of a large inventory.
                        waits += 1
                        retry = min(_MAX_RETRY_SECONDS, _RETRY_SECONDS * 2 ** (waits - 1))
                    else:
                        # Replaying an already certified earlier folder must not
                        # reset a persistent failure in a later folder or phase.
                        failures[operation] = failures.get(operation, 0) + 1
                        if reason in _TERMINAL or failures[operation] >= _MAX_FAILURES:
                            _LOG.warning("Search index auto rebuild stopped reason=%s", reason)
                            self._update(state="failed", last_error=reason)
                            return
                        retry = min(
                            _MAX_RETRY_SECONDS, _RETRY_SECONDS * 2 ** (failures[operation] - 1)
                        )
                _LOG.warning("Search index auto rebuild retry reason=%s", reason)
                # A changed epoch or partial FETCH needs a new authoritative
                # inventory. Already committed header batches remain reusable.
                folders, position, work = None, 0, None
                self._wait(retry, backoff=True)
                retry = min(_MAX_RETRY_SECONDS, retry * 2)
        self._update(state="idle", eta_seconds=None)
