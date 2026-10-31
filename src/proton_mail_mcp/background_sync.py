"""Opt-in, read-only upkeep of an existing All Mail header index between requests.

One daemon thread compares STATUS with the saved epoch and reuses the request-path
catch-up only when it moved. It never builds an empty cache, waits for a request
after every start, and gives way to any tool call by shutting down its own socket.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from typing import Any, Iterator

from . import canonical
from .cleanup_lookup import drop_expired_raw_snapshots
from .imap_idle import _interrupt
from .mail import CallDeadlineExceeded, _Deadline
from .search import _coverage_status
from .search_sync import sync_selected_headers

_LOG = logging.getLogger(__name__)
_TICK_SECONDS = 20.0
_SOCKET_SECONDS = 10.0
_GRACE_SECONDS = 30.0
_ACTIVE_SECONDS = 24 * 3600
_SLOW_SECONDS = 5.0
_MAX_BACKOFF_SECONDS = 6 * 3600
_MAX_ROWS_PER_DAY = 5000
# Setup (5), LIST, STATUS, EXAMINE, tail SEARCH, ALL or a second STATUS, LOGOUT
# and one spare; FETCHes follow the catch-up cap at sync_selected_headers' batch.
_FIXED_COMMANDS = 12
_HEADER_BATCH = 500
_HEALTHY = frozenset({"unchanged", "caught_up"})
_PROBLEMS = frozenset(
    {"failed", "incomplete", "stuck", "uidvalidity_changed", "no_all_mail", "no_epoch", "budget"}
)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class BackgroundSync:
    """One optional worker; requests never wait for it and it never outlasts them."""

    def __init__(self, client: Any, settings: Any):
        self.client = client
        self.settings = settings
        self._on = settings.background_sync_active
        self._stop = threading.Event()
        self._yielded = threading.Event()
        # Requests take this on the event loop, so it is never held across I/O.
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._connection: Any = None
        self._active = 0
        self._last_request: float | None = None
        self._all_mail: str | None = None
        self._stuck: tuple[str, int, str] | None = None
        self._backoff = 0.0
        self._next_tick = 0.0
        self._problem: str | None = None
        self._day = _today()
        self._commands = 0
        self._rows = 0
        self._outcome: str | None = None
        self._last_tick_at: str | None = None
        self._skip = "imap_idle_enabled" if settings.imap_idle_enabled else None

    def start(self) -> None:
        if not self._on or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="proton-mail-background-sync", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._yielded.set()
        self._stop.set()
        with self._lock:
            connection = self._connection
        if connection is not None:
            _interrupt(connection)
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    @contextmanager
    def foreground(self) -> Iterator[None]:
        """Mark one tool call: no tick starts, and a running tick yields at once.

        Runs on the event loop, so it only takes a short lock and shuts down the
        tick's socket without waiting for that tick to finish.
        """
        if not self._on:
            yield
            return
        with self._lock:
            self._active += 1
            self._last_request = time.monotonic()
            connection = self._connection
        if connection is not None:
            self._yielded.set()
            with suppress(Exception):  # Best effort: yielding must never fail the call.
                _interrupt(connection)
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1
                self._last_request = time.monotonic()

    def status(self) -> dict[str, Any]:
        """Fixed outcome codes and counters from local memory only."""
        with self._lock:
            self._roll_day()
            return {
                "active": self._on,
                "last_outcome": self._outcome,
                "last_tick_at": self._last_tick_at,
                "last_skip_reason": self._skip,
                "commands_today": self._commands,
                "rows_today": self._rows,
                "max_commands_per_day": self.settings.background_sync_max_commands,
                "max_rows_per_day": _MAX_ROWS_PER_DAY,
            }

    def _roll_day(self) -> None:
        today = _today()
        if today != self._day:
            self._day, self._commands, self._rows = today, 0, 0

    def _register(self, connection: Any) -> None:
        with self._lock:
            self._connection = connection
            preempted = self._active > 0
        if preempted or self._stop.is_set():
            self._yielded.set()
            _interrupt(connection)

    def _tick_commands(self) -> int:
        catchup = self.settings.search_index_catchup_max_messages
        return _FIXED_COMMANDS + math.ceil(catchup / _HEADER_BATCH)

    def _skip_reason(self, now: float) -> str | None:
        """Local gates: any reason means this wake sends no IMAP command at all."""
        with self._lock:
            self._roll_day()
            active, last = self._active, self._last_request
            commands, rows = self._commands, self._rows
        if active or (last is not None and now - last < _GRACE_SECONDS):
            return "request_active"
        # Boots, crash loops and unused servers spend nothing, and cannot prompt
        # a locked credential store.
        if last is None or now - last > _ACTIVE_SECONDS:
            return "no_recent_request"
        if now < self._next_tick:
            return "backoff"
        if self.settings.search_index_catchup_max_messages == 0:
            return "catchup_disabled"
        if self.client.reindex.running:
            return "reindex_running"
        if (
            commands + self._tick_commands() > self.settings.background_sync_max_commands
            # One tick can fetch a full catch-up, so that has to fit for the limit to hold.
            or rows + self.settings.search_index_catchup_max_messages > _MAX_ROWS_PER_DAY
        ):
            return "budget"
        # Startup recovery handles an empty cache; periodic sync needs an inventory.
        if self.client.search_index.reporting_counts() is None:
            return "no_index"
        return None

    def _poll(self, connection: Any, deadline: Any) -> str:
        client, index = self.client, self.client.search_index
        if self._all_mail is None:
            self._all_mail = canonical.mailbox_roles(connection)[2]
            if self._all_mail is None:
                return "no_all_mail"
        all_mail = self._all_mail
        status = _coverage_status(connection, all_mail)
        if status == index.coverage(all_mail):
            return "unchanged"
        epoch = index.coverage_epoch(all_mail)
        if epoch is None:
            return "no_epoch"
        # EXAMINE would wipe this folder's rows; that reset belongs to a request.
        if epoch[0] != status[0]:
            return "uidvalidity_changed"
        if status == self._stuck:
            return "stuck"
        before = index.known_count(all_mail, status[0])
        result = None
        try:
            state = client._select(connection, all_mail)  # EXAMINE; never writable SELECT.
            result = sync_selected_headers(
                client,
                connection,
                deadline,
                all_mail,
                state,
                reserve=False,
                batch_size=_HEADER_BATCH,
            )
        finally:
            after = index.known_count(all_mail, status[0])
            grew = None not in (before, after) and after > before
            # A cut inventory read raises past its result, leaving only its net growth.
            rows = len(result.fetched) if result is not None else after - before if grew else 0
            with self._lock:
                self._roll_day()
                self._rows += rows
        if result.complete:
            self._stuck = None
            return "caught_up"
        # A cut or timed-out tick says nothing about whether this epoch can certify.
        transient = result.timed_out or result.stream_broken or self._yielded.is_set()
        if not transient and not grew and None not in (before, after):
            self._stuck = status
        if result.stream_broken:
            # As in the watcher: _session must not send LOGOUT on this stream.
            raise CallDeadlineExceeded("Mailbox index update timed out")
        return "incomplete"

    def _record(self, outcome: str, *, tick: bool = False) -> None:
        # Fixed codes only: protocol and credential errors can contain secrets or mail.
        with self._lock:
            if tick:
                self._outcome, self._skip = outcome, None
                self._last_tick_at = datetime.now(timezone.utc).isoformat()
            else:
                self._skip = outcome
        if outcome not in _HEALTHY and outcome not in _PROBLEMS:
            _LOG.debug("Background sync outcome=%s", outcome)
            return
        problem = outcome if outcome in _PROBLEMS else None
        if problem != self._problem:
            _LOG.warning("Background sync state=%s", problem or "healthy")
            self._problem = problem
        else:
            _LOG.info("Background sync outcome=%s", outcome)

    def _tick(self) -> None:
        started = time.monotonic()
        skip = self._skip_reason(started)
        if skip is not None:
            self._record(skip)
            return
        self._yielded.clear()
        settings = self.settings
        deadline = _Deadline(
            min(_TICK_SECONDS, settings.call_timeout), min(_SOCKET_SECONDS, settings.timeout)
        )
        session = self.client._session(deadline=deadline, on_connect=self._register)
        try:
            with session as (connection, _):
                outcome = self._poll(connection, deadline)
        except Exception:
            outcome = "failed"
        with self._lock:
            self._roll_day()
            # imaplib's tag counter covers setup and LOGOUT, with no mail.py hook.
            self._commands += getattr(self._connection, "tagnum", 0)
            self._connection = None
        if outcome in ("failed", "incomplete") and self._yielded.is_set():
            outcome = "yielded"
        now = time.monotonic()
        if outcome == "failed":
            self._all_mail = None
        changed = outcome in ("caught_up", "incomplete")
        if outcome == "failed" or (changed and now - started > _SLOW_SECONDS):
            self._backoff = min(
                max(self._backoff, settings.background_sync_interval) * 2, _MAX_BACKOFF_SECONDS
            )
            self._next_tick = now + self._backoff
        elif changed:
            self._backoff = 0.0
        self._record(outcome, tick=True)

    def _run(self) -> None:
        interval = self.settings.background_sync_interval
        # Waits start after each tick, so missed ticks are dropped: a suspended
        # host wakes to one tick, not a burst.
        while not self._stop.wait(interval + random.uniform(0, interval * 0.1)):
            try:
                drop_expired_raw_snapshots(self.client)
                self._tick()
            except Exception:
                self._record("failed", tick=True)
