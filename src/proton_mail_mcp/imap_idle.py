"""Opt-in, read-only mailbox watching on connections independent of MCP calls.

The small IDLE adapter deliberately uses imaplib's parser on every supported
Python version. A timer sends DONE instead of timing out a buffered socket read
(which makes older imaplib streams unusable). Only the worker reads the stream.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from .mail import MailError, _checked
from .search import _uidvalidity

_LOG = logging.getLogger(__name__)
IDLE_SECONDS = 28 * 60
_EVENTS = ("EXISTS", "EXPUNGE", "UIDVALIDITY")
_BATCH = 200


def _interrupt(connection: Any) -> None:
    """Wake a blocked reader without issuing a command or expunging mail."""
    sock = getattr(connection, "sock", None)
    if sock is not None:
        with suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)


def _budget(deadline: Any, settings: Any, *, idle: bool = False) -> None:
    # _session, its proxy and _DeadlineIMAP4 share this deadline object. Renew
    # that worker-local budget between phases; never extend a tool's deadline.
    seconds = IDLE_SECONDS + settings.call_timeout if idle else settings.call_timeout
    deadline.end = time.monotonic() + seconds + deadline.response_reserve
    deadline.socket_timeout = seconds if idle else settings.timeout


def _changed(connection: Any) -> bool:
    return any(connection.untagged_responses.get(kind) for kind in _EVENTS)


def _idle(connection: Any, deadline: Any, settings: Any) -> bool:
    """Run one RFC 2177 exchange, returning whether inventory may have changed."""
    if _changed(connection):
        return True  # Notifications received during the preceding FETCH/SEARCH.
    _budget(deadline, settings)
    raw = connection.connection
    try:
        tag = connection._new_tag()
        connection.send(tag + b" IDLE\r\n")
        while connection.tagged_commands[tag] is None:
            response = connection._get_response()
            connection._check_bye()
            if response is None:  # '+'; DONE is forbidden before this continuation.
                break
        else:
            _checked(connection._command_complete("IDLE", tag))
            raise MailError("Mailbox did not enter IDLE")
    except BaseException:
        _interrupt(raw)
        raise

    lock = threading.Lock()
    active = True
    done = False
    completion_timer = None
    completion_lock = threading.Lock()
    completion_active = True

    def completion_timeout() -> None:
        # Separate from the send lock: a blocked DONE must remain interruptible.
        with completion_lock:
            if completion_active:
                _interrupt(raw)

    def finish() -> None:
        nonlocal done, completion_timer
        with lock:
            if not active or done:
                return
            done = True
            # Bound a missing tagged completion, even when the renewal callback
            # sends DONE while the worker is blocked in a long IDLE read.
            completion_timer = threading.Timer(settings.timeout, completion_timeout)
            completion_timer.daemon = True
            completion_timer.start()
            try:
                raw.send(b"DONE\r\n")
            except Exception:
                _interrupt(raw)

    renewal = threading.Timer(IDLE_SECONDS, finish)
    renewal.daemon = True
    try:
        _budget(deadline, settings, idle=True)
        renewal.start()
        if _changed(connection):  # An update may precede the '+' continuation.
            finish()
        while connection.tagged_commands[tag] is None:
            connection._get_response()
            connection._check_bye()
            for kind in ("FETCH", "RECENT", "OK"):
                connection.untagged_responses.pop(kind, None)
            if _changed(connection):
                finish()
            if done:
                break
        _budget(deadline, settings)
        _checked(connection._command_complete("IDLE", tag))
        if not done:
            raise MailError("Mailbox ended IDLE unexpectedly")
        return _changed(connection)
    except BaseException:
        _interrupt(raw)  # A failed exchange cannot safely issue LOGOUT or be reused.
        raise
    finally:
        with lock:
            active = False
            renewal.cancel()
            with completion_lock:
                completion_active = False
            if completion_timer is not None:
                completion_timer.cancel()
        _budget(deadline, settings)


@dataclass
class _Inventory:
    validity: str | None = None
    uids: set[str] = field(default_factory=set)


class MailWatcher:
    """One read-only index maintenance worker per selected folder."""

    def __init__(self, client: Any, settings: Any):
        self.client = client
        self.settings = settings
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._connections: dict[str, Any] = {}
        self._fallback_logged = False
        self._all_mail: str | None = None
        self._roles_loaded = False
        self._statuses = dict.fromkeys(settings.imap_idle_folders, "disabled")

    def start(self) -> None:
        if not self.settings.imap_idle_enabled or self._threads:
            return
        self._stop.clear()
        for folder in self.settings.imap_idle_folders:
            self._status(folder, "starting")
            thread = threading.Thread(target=self._run, args=(folder,), daemon=True)
            self._threads.append(thread)
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            connections = list(self._connections.values())
        for connection in connections:
            _interrupt(connection)
        # Active greeting/login/IDLE readers wake immediately. Socket creation
        # remains bounded by its connect timeout; stop joins every worker.
        for thread in self._threads:
            thread.join()
        self._threads.clear()

    def _status(self, folder: str, status: str) -> None:
        with self._lock:
            self._statuses[folder] = status

    def _register(self, folder: str, connection: Any) -> None:
        with self._lock:
            self._connections[folder] = connection
        if self._stop.is_set():
            _interrupt(connection)

    def _sync(self, connection: Any, deadline: Any, folder: str, known: _Inventory) -> None:
        if self._stop.is_set():
            return
        _budget(deadline, self.settings)
        connection._search_index = self.client.search_index
        state = self.client._select(connection, folder)  # EXAMINE; never writable SELECT.
        validity = _uidvalidity(connection)
        for kind in _EVENTS:
            connection.untagged_responses.pop(kind, None)
        current = set(self.client._search(connection, ["ALL"]))
        if known.validity != validity:
            known.validity, known.uids = validity, current
            self._sync_account_index(connection, deadline, state, validity, current)
            return  # Startup/reset establishes a baseline without rebuilding history.

        removed = sorted(known.uids - current, key=int)
        for offset in range(0, len(removed), _BATCH):
            batch = removed[offset : offset + _BATCH]
            known.uids.difference_update(batch)
        added = sorted(current - known.uids, key=int)
        for offset in range(0, len(added), _BATCH):
            if self._stop.is_set():
                return
            batch = added[offset : offset + _BATCH]
            _budget(deadline, self.settings)
            self.client._metadata(
                connection,
                batch,
                deadline=deadline,
                bodystructure=False,
                internaldate=True,
                envelope=True,
                size=True,
            )
            # Missing FETCH results disappeared before headers could be read.
            # Preserve the inventory so repeated reconnects don't retry forever.
            known.uids.update(batch)
        self._sync_account_index(connection, deadline, state, validity, current)

    def _sync_account_index(
        self,
        connection: Any,
        deadline: Any,
        state: dict[str, Any],
        validity: str,
        current: set[str],
    ) -> None:
        """Maintain an existing All Mail index with bounded header catch-up."""
        from . import canonical
        from .mail import CallDeadlineExceeded
        from .search_sync import sync_all_mail, sync_selected_headers

        index = self.client.search_index
        if not self._roles_loaded:
            # Starting a watcher must not turn an empty cache into a historical
            # rebuild. Coverage metadata identifies an existing account index.
            if index.reporting_counts() is None:
                return
            self._all_mail = canonical.mailbox_roles(connection)[2]
            self._roles_loaded = True
        if self._all_mail is None:
            return
        _budget(deadline, self.settings)
        if state["folder"] == self._all_mail:
            result = sync_selected_headers(
                self.client,
                connection,
                deadline,
                self._all_mail,
                state,
                validity=validity,
                inventory=list(current),
                reserve=False,
            )
        else:
            result = sync_all_mail(self.client, connection, deadline, self._all_mail, reserve=False)
            restored = getattr(connection, "_selected_inventory", None)
            if restored is not state and restored == state:
                # EXAMINE's UIDVALIDITY describes the restored selection. It is
                # not an IDLE change. A changed epoch stays queued for resync.
                for kind in _EVENTS:
                    connection.untagged_responses.pop(kind, None)
        if result.stream_broken:
            raise CallDeadlineExceeded("Mailbox index update timed out")

    def _run(self, folder: str) -> None:
        known = _Inventory()
        backoff = 1.0
        while not self._stop.is_set():
            began = time.monotonic()
            try:
                deadline = self.client._deadline()
                with self.client._session(
                    deadline=deadline,
                    on_connect=lambda connection: self._register(folder, connection),
                ) as (connection, _):
                    if self._stop.is_set():
                        _interrupt(connection)
                        return
                    idle = "IDLE" in connection.capabilities
                    if not idle:
                        with self._lock:
                            if not self._fallback_logged:
                                _LOG.warning("IMAP IDLE unavailable; using periodic NOOP checks")
                                self._fallback_logged = True
                    self._sync(connection, deadline, folder, known)
                    while not self._stop.is_set():
                        self._status(folder, "idle" if idle else "polling")
                        if idle:
                            changed = _idle(connection, deadline, self.settings)
                        else:
                            if self._stop.wait(self.settings.imap_idle_poll_interval):
                                break
                            _budget(deadline, self.settings)
                            _checked(connection.noop())
                            changed = True
                        if changed:
                            self._sync(connection, deadline, folder, known)
            except Exception:
                # Protocol/credential exceptions can contain secrets or mail.
                # Expose only a fixed status, never exception text or folders.
                if not self._stop.is_set():
                    self._status(folder, "reconnecting")
            finally:
                with self._lock:
                    self._connections.pop(folder, None)
                if self._stop.is_set():
                    self._status(folder, "stopped")
            if not self._stop.is_set():
                if time.monotonic() - began >= 60:
                    backoff = 1.0
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 60.0)
        self._status(folder, "stopped")
