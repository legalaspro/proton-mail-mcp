"""The default-off background All Mail sync, using fake-only, read-only IMAP peers."""

import imaplib
import logging
import shutil
import socket
import threading
import time
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import anyio
import pytest
from bridge_sim import BridgeSim, Msg, hdr, make_client
from gluon_search import GluonSearchImap
from starlette.testclient import TestClient
from test_folder_rules import mailbox as mailbox
from test_header_catchup_incremental import (
    RangeImap,
    assert_read_only,
    header_fetches,
    range_searches,
    seed_baseline,
)
from test_mail import client, message
from test_mail import settings as settings
from test_rules_matched_membership import forbid_full_all_mail_search, matched_case
from test_server import TOKEN, FakeMail, rpc

from proton_mail_mcp import background_sync, cleanup_lookup, mail
from proton_mail_mcp.background_sync import BackgroundSync
from proton_mail_mcp.cleanup_lookup import drop_expired_raw_snapshots
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.identity_index import _scope
from proton_mail_mcp.mail import MailError, MailValidationError
from proton_mail_mcp.server import create_app, create_mcp

BASE = {"PROTON_MCP_TOKEN": "x" * 32, "PROTON_BRIDGE_USER": "bridge@example.test"}
PRIVATE_SUBJECT = "private-subject-do-not-log"
PRIVATE_ERROR = "private-server-text test-password-secret"
ALLOWED = {"STARTTLS", "LOGIN", "CAPABILITY", "LIST", "STATUS", "EXAMINE", "SEARCH", "FETCH"}
ALLOWED.add("LOGOUT")


class TagImap(RangeImap):
    """imaplib's counter, with the greeting and STARTTLS CAPABILITY the fake omits."""

    @property
    def tagnum(self):
        return len(self.calls) + 2


def mail_items(*uids):
    return {str(uid): message(f"<{uid}@test.invalid>") for uid in uids}


def job(settings, uids=(1, 2), *, fake=None, seed=True, **changes):
    """A seeded index and a job whose last request ended long enough ago to tick."""
    settings = replace(settings, background_sync_enabled=True, **changes)
    fake = fake or TagImap({"All Mail": mail_items(*uids)}, uidnext=True)
    reader = client(settings, fake)
    if seed:
        seed_baseline(reader, [str(uid) for uid in uids])
    sync = BackgroundSync(reader, settings)
    sync._last_request = time.monotonic() - 60
    return fake, reader, sync


def names(fake):
    return [call[0] for call in fake.calls]


def outcome_of(sync):
    status = sync.status()
    return status["last_skip_reason"] or status["last_outcome"]


def job_threads():
    return [thread for thread in threading.enumerate() if thread.name.endswith("background-sync")]


def forbid_connection(reader):
    reader.imap_factory = Mock(side_effect=AssertionError("Gates must not connect"))
    reader.credentials_provider = Mock(side_effect=AssertionError("Gates must not read secrets"))


# 1. Off


def test_configuration_defaults_off_and_refuses_bad_values():
    defaults = Settings.from_env(BASE)
    assert defaults.background_sync_enabled is False
    assert defaults.background_sync_interval == 900
    assert defaults.background_sync_max_commands == 1500
    configured = Settings.from_env(
        {
            **BASE,
            "PROTON_MCP_BACKGROUND_SYNC_ENABLED": "1",
            "PROTON_MCP_BACKGROUND_SYNC_INTERVAL": "300",
            "PROTON_MCP_BACKGROUND_SYNC_MAX_COMMANDS": "5000",
        }
    )
    assert configured.background_sync_enabled is True
    assert configured.background_sync_interval == 300
    assert configured.background_sync_max_commands == 5000
    for name, value in (
        ("PROTON_MCP_BACKGROUND_SYNC_ENABLED", "true"),
        ("PROTON_MCP_BACKGROUND_SYNC_INTERVAL", "NaN"),
        ("PROTON_MCP_BACKGROUND_SYNC_INTERVAL", "299"),
        ("PROTON_MCP_BACKGROUND_SYNC_INTERVAL", "3601"),
        ("PROTON_MCP_BACKGROUND_SYNC_MAX_COMMANDS", "99"),
        ("PROTON_MCP_BACKGROUND_SYNC_MAX_COMMANDS", "5001"),
        ("PROTON_MCP_BACKGROUND_SYNC_MAX_COMMANDS", "1.5"),
    ):
        with pytest.raises(ConfigError):
            Settings.from_env({**BASE, name: value})


@pytest.mark.parametrize(
    "change",
    [
        {"background_sync_enabled": 1},
        {"background_sync_interval": True},
        {"background_sync_max_commands": True},
        {"background_sync_max_commands": 1500.0},
    ],
)
def test_invalid_typed_settings_refuse(change):
    with pytest.raises(ConfigError):
        replace(Settings.from_env(BASE), **change)


def test_off_has_no_thread_no_session_and_foreground_changes_nothing(settings):
    reader = Mock()
    sync = BackgroundSync(reader, settings)
    sync.start()
    assert sync._thread is None and not job_threads()
    with sync.foreground():
        assert sync._active == 0
    assert sync._last_request is None
    sync.stop()
    assert reader.mock_calls == []


def test_off_server_dispatch_and_status_are_unchanged():
    settings = Settings(token=TOKEN, bridge_user="bridge@example.test", confirm_mutations=False)
    fake = FakeMail()
    app = create_app(settings, fake)
    with TestClient(app, base_url="http://127.0.0.1") as http:
        status = rpc(http, "tools/call", {"name": "reindex_status", "arguments": {}})["result"]
        assert "background_sync" not in status["structuredContent"]
        assert not job_threads()
    assert fake.calls == [("reindex_status", {})]


def test_enabled_lifecycle_is_app_scoped_and_wraps_every_tool_call(monkeypatch):
    settings = Settings(
        token=TOKEN,
        bridge_user="bridge@example.test",
        confirm_mutations=False,
        background_sync_enabled=True,
    )
    job = Mock(wraps=BackgroundSync(FakeMail(), settings))
    entered = []
    real_foreground = job.foreground

    def foreground():
        entered.append(True)
        return real_foreground()

    job.foreground = foreground
    job.status.return_value = {"active": True, "last_outcome": "unchanged"}
    factory = Mock(return_value=job)
    monkeypatch.setattr("proton_mail_mcp.server.BackgroundSync", factory)
    app = create_app(settings, FakeMail())
    factory.assert_called_once()
    job.start.assert_not_called()
    with TestClient(app, base_url="http://127.0.0.1") as http:
        job.start.assert_called_once_with()
        result = rpc(http, "tools/call", {"name": "reindex_status", "arguments": {}})["result"]
        assert result["structuredContent"]["background_sync"]["last_outcome"] == "unchanged"
        rpc(http, "tools/call", {"name": "list_rules", "arguments": {}})
        rpc(http, "tools/call", {"name": "search_mail", "arguments": {}})
        assert len(entered) == 3
        job.stop.assert_not_called()
    job.stop.assert_called_once_with()


def test_cancelled_call_stays_counted_until_its_worker_really_ends():
    started, release = threading.Event(), threading.Event()

    class SlowMail(FakeMail):
        def search_mail(self, **kwargs):
            started.set()
            assert release.wait(5)
            return super().search_mail(**kwargs)

    settings = Settings(
        token=TOKEN,
        bridge_user="bridge@example.test",
        confirm_mutations=False,
        background_sync_enabled=True,
    )
    mcp = create_mcp(settings, SlowMail())
    sync = mcp._background_sync

    async def exercise():
        scope = anyio.CancelScope()

        async def call():
            with scope:
                await mcp._tool_manager.call_tool("search_mail", {})

        async with anyio.create_task_group() as group:
            group.start_soon(call)
            await anyio.to_thread.run_sync(started.wait, 5)
            scope.cancel()
            await anyio.sleep(0.05)
            assert sync._active == 1
            release.set()
        assert sync._active == 0
        assert scope.cancel_called

    anyio.run(exercise)


# 2-4. Ticks


def test_unchanged_tick_sends_only_status_after_setup(settings):
    fake, reader, sync = job(settings)
    sync._tick()
    assert names(fake) == ["STARTTLS", "LOGIN", "CAPABILITY", "LIST", "STATUS", "LOGOUT"]
    assert sync.status()["last_outcome"] == "unchanged"
    fake.calls.clear()

    sync._tick()

    # All Mail is looked up once per process: no LIST, EXAMINE, SEARCH or FETCH.
    assert names(fake) == ["STARTTLS", "LOGIN", "CAPABILITY", "STATUS", "LOGOUT"]
    assert sync.status()["last_outcome"] == "unchanged"


def test_growth_fetches_only_new_uids_and_certifies_the_epoch(settings):
    fake, reader, sync = job(settings)
    fake.folders["All Mail"].update(mail_items(3, 4))
    sync._tick()

    assert reader.search_index.coverage("All Mail") == ("1", 4, "5")
    assert sync.status()["last_outcome"] == "caught_up"
    assert sync.status()["rows_today"] == 2
    assert range_searches(fake) == [("SEARCH", "CHARSET", "UTF-8", "UID", "3:4")]
    assert [call[1] for call in header_fetches(fake)] == ["3,4"]
    assert not any(call[0] == "SEARCH" and "ALL" in call for call in fake.calls)
    fake.calls.clear()

    sync._tick()

    assert names(fake) == ["STARTTLS", "LOGIN", "CAPABILITY", "STATUS", "LOGOUT"]


def test_expunge_reconciles_through_the_existing_inventory_path(settings):
    fake, reader, sync = job(settings, (1, 2, 3))
    del fake.folders["All Mail"]["2"]
    sync._tick()

    assert reader.search_index.coverage("All Mail") == ("1", 2, "4")
    assert sync.status()["last_outcome"] == "caught_up"
    assert any(call[0] == "SEARCH" and "ALL" in call for call in fake.calls)
    assert not header_fetches(fake)


def test_ticks_are_read_only_and_leave_flags_unchanged(settings):
    fake, reader, sync = job(settings)
    fake.folders["All Mail"].update(mail_items(3))
    flags = {uid: getattr(item, "flags", None) for uid, item in fake.folders["All Mail"].items()}
    sync._tick()
    del fake.folders["All Mail"]["1"]
    sync._tick()

    assert set(names(fake)) <= ALLOWED
    assert sum(call[0] == "EXAMINE" for call in fake.calls) == 2
    assert_read_only(fake)
    assert header_fetches(fake)
    assert all("BODY.PEEK[" in call[2] for call in fake.calls if call[0] == "FETCH")
    assert {
        uid: getattr(item, "flags", None) for uid, item in fake.folders["All Mail"].items()
    } == {uid: flags[uid] for uid in ("2", "3")}


# 5. Gates


@pytest.mark.parametrize(
    "gate,outcome",
    [
        ("no_index", "no_index"),
        ("no_request", "no_recent_request"),
        ("stale_request", "no_recent_request"),
        ("active", "request_active"),
        ("grace", "request_active"),
        ("reindex", "reindex_running"),
        ("catchup_off", "catchup_disabled"),
        ("backoff", "backoff"),
    ],
)
def test_local_gates_skip_the_tick_without_any_imap_or_credentials(settings, gate, outcome):
    fake, reader, sync = job(
        settings,
        seed=gate != "no_index",
        search_index_catchup_max_messages=0 if gate == "catchup_off" else 2000,
    )
    forbid_connection(reader)
    if gate == "no_request":
        sync._last_request = None
    elif gate == "stale_request":
        sync._last_request = time.monotonic() - 24 * 3600 - 1
    elif gate == "grace":
        sync._last_request = time.monotonic() - 29
    elif gate == "reindex":
        reader.reindex._state["state"] = "running"
    elif gate == "backoff":
        sync._next_tick = time.monotonic() + 60
    if gate == "active":
        with sync.foreground():
            sync._tick()
    else:
        sync._tick()

    status = sync.status()
    assert status["last_skip_reason"] == outcome
    assert status["last_outcome"] is None and status["last_tick_at"] is None
    assert fake.calls == []
    reader.imap_factory.assert_not_called()
    reader.credentials_provider.assert_not_called()


def test_a_long_running_request_outlasting_the_grace_period_still_blocks_ticks(settings):
    fake, reader, sync = job(settings)
    forbid_connection(reader)
    with sync.foreground():
        sync._last_request = time.monotonic() - 3600
        sync._tick()
    assert outcome_of(sync) == "request_active"
    assert fake.calls == []


def test_a_request_older_than_the_grace_period_allows_a_tick(settings):
    fake, reader, sync = job(settings)
    sync._last_request = time.monotonic() - 31
    sync._tick()
    assert sync.status()["last_outcome"] == "unchanged"
    assert sync.status()["last_tick_at"] is not None


def test_idle_watcher_keeps_the_job_off_and_reports_why(settings):
    fake, reader, sync = job(settings, imap_idle_enabled=True, imap_idle_folders=("INBOX",))
    sync._last_request = None
    forbid_connection(reader)
    sync.start()
    assert sync._thread is None
    with sync.foreground():
        assert sync._active == 0
    assert sync._last_request is None
    assert sync.settings.background_sync_active is False
    status = sync.status()
    assert status["active"] is False and status["last_skip_reason"] == "imap_idle_enabled"


def test_first_tick_needs_a_request_since_start_and_stop_ends_the_thread(settings, monkeypatch):
    fake, reader, sync = job(settings)
    sync._last_request = None
    waits = []
    woke = threading.Event()

    class Stop(threading.Event):
        def wait(self, timeout=None):
            if waits:
                woke.set()
                return super().wait(5)
            waits.append(timeout)
            return False

    sync._stop = Stop()
    sync.start()
    assert woke.wait(5)
    sync.stop()

    assert not fake.calls and sync._thread is None
    assert outcome_of(sync) == "no_recent_request"
    assert 900 <= waits[0] <= 990


# 6. Preemption


class Socket:
    def __init__(self):
        self.shutdowns = []
        self.closed = threading.Event()

    def settimeout(self, seconds):
        pass

    def shutdown(self, how):
        self.shutdowns.append(how)
        self.closed.set()


class HeldImap(TagImap):
    """One header FETCH blocks like a socket read until the test releases it."""

    hold_batch = 2

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sock = Socket()
        self.holding = threading.Event()
        self.release = threading.Event()
        self.batches = 0

    def uid(self, command, *args):
        if command == "FETCH" and "HEADER.FIELDS" in args[1]:
            self.batches += 1
            if self.batches == self.hold_batch:
                self.holding.set()
                assert self.release.wait(5)
                raise imaplib.IMAP4.abort("simulated interrupted FETCH")
        return super().uid(command, *args)


def test_foreground_returns_at_once_and_interrupts_a_held_fetch(settings):
    fake = HeldImap({"All Mail": mail_items(*range(1, 603))}, uidnext=True)
    fake, reader, sync = job(settings, range(1, 3), fake=fake)
    before = reader.search_index.coverage_epoch("All Mail")
    worker = threading.Thread(target=sync._tick, daemon=True)
    worker.start()
    assert fake.holding.wait(5)

    # The fake only returns after this block, so any wait for the tick would hang.
    with sync.foreground():
        assert fake.sock.shutdowns == [socket.SHUT_RDWR]
        assert worker.is_alive() and not fake.release.is_set()
    assert worker.is_alive()
    fake.release.set()
    worker.join(5)
    assert not worker.is_alive()

    index = reader.search_index
    assert index.coverage_epoch("All Mail") == before
    assert index.known_count("All Mail", "1") == 502  # Completed batch kept.
    assert sync.status()["last_outcome"] == "yielded"
    assert sync._backoff == 0 and sync._next_tick == 0
    assert sync._stuck is None
    assert sync._all_mail == "All Mail"
    assert ("LOGOUT",) not in fake.calls  # An interrupted stream is never reused.


def test_yielding_before_any_new_header_does_not_mark_the_epoch_stuck(settings):
    fake = HeldImap({"All Mail": mail_items(1, 2, 3)}, uidnext=True)
    fake.hold_batch = 1
    fake, reader, sync = job(settings, range(1, 3), fake=fake)
    worker = threading.Thread(target=sync._tick, daemon=True)
    worker.start()
    assert fake.holding.wait(5)
    with sync.foreground():
        pass
    fake.release.set()
    worker.join(5)

    assert sync.status()["last_outcome"] == "yielded"
    assert reader.search_index.known_count("All Mail", "1") == 2
    assert sync._stuck is None
    # The next tick examines this epoch again and finishes the work.
    fake.release.clear()
    fake.hold_batch = 0
    sync._last_request = time.monotonic() - 60
    sync._tick()
    assert sync.status()["last_outcome"] == "caught_up"
    assert reader.search_index.coverage("All Mail") == ("1", 3, "4")


def test_a_failed_interrupt_neither_fails_the_call_nor_leaks_its_count(settings, monkeypatch):
    fake, reader, sync = job(settings)
    sync._connection = fake
    monkeypatch.setattr(background_sync, "_interrupt", Mock(side_effect=RuntimeError("synthetic")))
    with sync.foreground():
        assert sync._active == 1
    assert sync._active == 0


def test_request_started_while_connecting_interrupts_at_registration(settings):
    fake = HeldImap({"All Mail": mail_items(1, 2)}, uidnext=True)
    fake, reader, sync = job(settings, fake=fake)

    def request_arrives_during_setup(username, password):
        assert fake.sock.shutdowns == [socket.SHUT_RDWR]
        raise OSError("simulated interrupted login")

    fake.login = request_arrives_during_setup
    credentials = reader.credentials_provider

    def provider(settings):
        sync._active += 1  # Passed the gates; the request began before registration.
        return credentials(settings)

    reader.credentials_provider = provider
    sync._tick()

    assert sync.status()["last_outcome"] == "yielded"
    assert sync._backoff == 0 and sync._next_tick == 0
    assert "STATUS" not in names(fake)


def test_stop_interrupts_a_held_fetch_and_joins(settings):
    fake = HeldImap({"All Mail": mail_items(*range(1, 603))}, uidnext=True)
    fake, reader, sync = job(settings, range(1, 3), fake=fake)
    sync._thread = threading.Thread(target=sync._tick, daemon=True)
    sync._thread.start()
    assert fake.holding.wait(5)
    stopper = threading.Thread(target=sync.stop, daemon=True)
    stopper.start()
    assert fake.sock.closed.wait(5)
    fake.release.set()
    stopper.join(5)

    assert not stopper.is_alive()
    assert sync._thread is None
    assert fake.sock.shutdowns == [socket.SHUT_RDWR]
    assert sync.status()["last_outcome"] == "yielded"


# 7. Bounds


@pytest.mark.parametrize("path", ["tail", "inventory"])
def test_worst_paths_stay_within_sixteen_commands(settings, path):
    uids = range(1, 6) if path == "inventory" else range(1, 3)
    fake, reader, sync = job(settings, uids)
    start = max(uids) + 1
    fake.folders["All Mail"].update(mail_items(*range(start, start + 2000)))
    if path == "inventory":
        # The count no longer adds up: an old UID went, so ALL has to reconcile.
        del fake.folders["All Mail"]["1"]
    sync._tick()

    assert reader.search_index.coverage("All Mail") is not None
    assert len(header_fetches(fake)) == 4
    assert ("ALL" in [atom for call in fake.calls for atom in call]) is (path == "inventory")
    assert fake.tagnum <= 16
    assert sync.status()["commands_today"] == fake.tagnum
    assert sync._tick_commands() == 16


def test_daily_command_cap_stops_ticks_and_utc_rollover_resets_it(settings, monkeypatch):
    fake, reader, sync = job(settings, background_sync_max_commands=100)
    day = ["2026-10-30"]
    monkeypatch.setattr(background_sync, "_today", lambda: day[0])
    sync._day = day[0]
    sync._commands = 85  # A tick needs 16 spare.
    sync._tick()
    assert outcome_of(sync) == "budget"
    assert fake.calls == []

    sync._commands = 84
    sync._tick()
    assert sync.status()["last_outcome"] == "unchanged"
    assert sync.status()["commands_today"] == 84 + fake.tagnum

    sync._commands = 100
    sync._tick()
    assert outcome_of(sync) == "budget"

    day[0] = "2026-10-31"
    assert sync.status()["commands_today"] == 0
    sync._tick()
    assert sync.status()["last_outcome"] == "unchanged"


def test_a_tick_crossing_utc_midnight_counts_toward_the_new_day(settings, monkeypatch):
    fake, reader, sync = job(settings)
    day = ["2026-10-30"]
    monkeypatch.setattr(background_sync, "_today", lambda: day[0])
    sync._day = day[0]
    sync._commands, sync._rows = 1000, 1000
    fake.folders["All Mail"].update(mail_items(3))
    uid = fake.uid

    def cross_midnight(command, *args):
        day[0] = "2026-10-31"
        return uid(command, *args)

    fake.uid = cross_midnight
    sync._tick()

    assert sync.status()["last_outcome"] == "caught_up"
    assert sync.status()["rows_today"] == 1
    assert sync.status()["commands_today"] == fake.tagnum


def test_row_cap_is_hard_so_the_last_tick_can_only_reach_it(settings):
    fake, reader, sync = job(settings)
    sync._rows = 3000
    fake.folders["All Mail"].update(mail_items(*range(3, 2003)))
    sync._tick()
    assert sync.status()["last_outcome"] == "caught_up"
    assert sync.status()["rows_today"] == 5000
    calls = list(fake.calls)

    fake.folders["All Mail"].update(mail_items(2003))
    sync._tick()

    assert outcome_of(sync) == "budget"
    assert fake.calls == calls


@pytest.mark.parametrize(
    "catchup,rows,runs",
    [
        (2000, 3000, True),
        (2000, 3001, False),
        (100, 4900, True),
        (100, 4901, False),
        (5000, 0, True),
        (5001, 0, False),  # A catch-up over the daily limit can never fit.
    ],
)
def test_a_tick_needs_room_for_a_full_catchup_in_the_daily_rows(settings, catchup, rows, runs):
    fake, reader, sync = job(settings, search_index_catchup_max_messages=catchup)
    sync._rows = rows

    sync._tick()

    assert outcome_of(sync) == ("unchanged" if runs else "budget")
    assert bool(fake.calls) is runs


@pytest.mark.parametrize(
    "limits,seconds,socket_seconds",
    [({}, 20.0, 10.0), ({"call_timeout": 5.0, "timeout": 3.0}, 5.0, 3.0)],
)
def test_tick_deadline_is_twenty_seconds_and_follows_shorter_limits(
    settings, limits, seconds, socket_seconds
):
    fake, reader, sync = job(settings, **limits)
    session = reader._session
    deadlines = []

    def capture(deadline=None, **options):
        deadlines.append((deadline, time.monotonic()))
        return session(deadline=deadline, **options)

    reader._session = capture
    sync._tick()
    deadline, started = deadlines[0]
    assert deadline.end - started == pytest.approx(seconds, abs=1)
    assert deadline.socket_timeout == socket_seconds


def test_a_fetch_past_the_deadline_ends_the_tick_with_backoff_and_no_logout(settings, monkeypatch):
    fake, reader, sync = job(settings)
    fake.folders["All Mail"].update(mail_items(3))
    clock = [time.monotonic()]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    sync._last_request = clock[0] - 60
    uid = fake.uid

    def slow(command, *args):
        if command == "FETCH":
            clock[0] += 21
        return uid(command, *args)

    fake.uid = slow
    sync._tick()

    assert sync.status()["last_outcome"] == "failed"
    assert sync._backoff == 1800
    assert ("LOGOUT",) not in fake.calls
    assert reader.search_index.coverage_epoch("All Mail") == ("1", 2, "3")

    # A timeout says nothing about the epoch: the next tick examines it again.
    assert sync._stuck is None
    fake.uid = uid
    fake.calls.clear()
    clock[0] += 1800
    sync._tick()
    assert sync.status()["last_outcome"] == "caught_up"
    assert names(fake).count("EXAMINE") == 1
    assert reader.search_index.coverage("All Mail") == ("1", 3, "4")


def test_rows_of_a_cut_inventory_read_still_count(settings):
    fake = HeldImap({"All Mail": mail_items(*range(1, 604))}, uidnext=True)
    fake, reader, sync = job(settings, range(1, 4), fake=fake)
    # An old UID went, so catch-up takes its ALL path, which raises past its result.
    del fake.folders["All Mail"]["1"]
    worker = threading.Thread(target=sync._tick, daemon=True)
    worker.start()
    assert fake.holding.wait(5)
    with sync.foreground():
        pass
    fake.release.set()
    worker.join(5)

    assert sync.status()["last_outcome"] == "yielded"
    assert any(call[0] == "SEARCH" and "ALL" in call for call in fake.calls)
    assert sync.status()["rows_today"] == 499  # 500 read, less the expunged UID.


@pytest.mark.parametrize("path", ["tail", "inventory"])
def test_a_tick_that_fetches_and_removes_rows_counts_what_it_fetched(settings, path):
    fake, reader, sync = job(settings, (1, 2) if path == "tail" else (1, 2, 3))
    if path == "tail":
        # An earlier attempt cached UID 3, which went since: certifying the tail removes it.
        reader.search_index.observe("All Mail", "1", {"3": {"headers": {}}})
    else:
        # Same count, higher UIDNEXT: ALL removes UID 1.
        del fake.folders["All Mail"]["1"]
    fake.folders["All Mail"].update(mail_items(4))
    known = reader.search_index.known_count("All Mail", "1")

    sync._tick()

    assert sync.status()["last_outcome"] == "caught_up"
    # One header in and one out: the net change would report no work at all.
    assert reader.search_index.known_count("All Mail", "1") == known
    assert [call[1] for call in header_fetches(fake)] == ["4"]
    assert sync.status()["rows_today"] == 1


def test_a_larger_catchup_cap_raises_the_headroom_a_tick_needs(settings):
    _, _, sync = job(settings, search_index_catchup_max_messages=5000)
    assert sync._tick_commands() == 22


# 8. Failures


def failing_session(reader):
    def factory(host, port, *, timeout):
        raise MailError(PRIVATE_ERROR)

    reader.imap_factory = factory


def test_failures_double_backoff_to_a_cap_and_reread_all_mail(settings, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="proton_mail_mcp")
    fake, reader, sync = job(settings)
    healthy = reader.imap_factory
    sync._tick()
    assert sync._all_mail == "All Mail"

    failing_session(reader)
    seen = []
    for _ in range(7):
        sync._next_tick = 0
        sync._tick()
        assert sync.status()["last_outcome"] == "failed"
        assert sync._next_tick > time.monotonic()
        seen.append(sync._backoff)
    assert seen == [1800, 3600, 7200, 14400, 21600, 21600, 21600]
    assert sync._all_mail is None

    # While backing off, the next wake stays local.
    reader.imap_factory = healthy
    fake.calls.clear()
    sync._tick()
    assert outcome_of(sync) == "backoff"
    assert fake.calls == []

    assert sync.status()["last_outcome"] == "failed"  # A skip does not hide it.

    sync._next_tick = 0
    sync._tick()
    assert sync.status()["last_outcome"] == "unchanged"
    assert sync.status()["last_skip_reason"] is None
    assert "LIST" in names(fake)  # Looked up again after the failed tick.

    assert PRIVATE_ERROR not in caplog.text
    assert "state=failed" in caplog.text and "state=healthy" in caplog.text
    warnings = [
        row.getMessage()
        for row in caplog.records
        if row.levelno >= logging.WARNING and row.name == background_sync.__name__
    ]
    assert warnings == ["Background sync state=failed", "Background sync state=healthy"]


def test_logs_and_status_hold_no_mail_or_folder_text(settings, caplog):
    caplog.set_level(logging.DEBUG, logger="proton_mail_mcp")
    fake, reader, sync = job(settings)
    fake.folders["All Mail"]["3"] = message("<private-id@test.invalid>", subject=PRIVATE_SUBJECT)
    sync._tick()
    del fake.folders["All Mail"]["1"]
    sync._tick()
    fake.uidvalidity = "2"
    sync._tick()
    failing_session(reader)
    sync._tick()

    text = caplog.text + str(sync.status())
    for private in (
        PRIVATE_SUBJECT,
        PRIVATE_ERROR,
        "private-id",
        "All Mail",
        "test-password-secret",
        "reader@example.com",
    ):
        assert private not in text


def test_slow_change_ticks_back_off_and_a_fast_one_resets(settings, monkeypatch):
    fake, reader, sync = job(settings)
    clock = [time.monotonic()]
    monkeypatch.setattr(background_sync.time, "monotonic", lambda: clock[0])
    sync._last_request = clock[0] - 60
    select = fake.select
    delay = [6.0]

    def timed_select(folder, *, readonly):
        clock[0] += delay[0]
        return select(folder, readonly=readonly)

    fake.select = timed_select
    fake.folders["All Mail"].update(mail_items(3))
    sync._tick()
    assert sync.status()["last_outcome"] == "caught_up"
    assert sync._backoff == 1800 and sync._next_tick == clock[0] + 1800

    sync._tick()
    assert outcome_of(sync) == "backoff"

    clock[0] += 1800
    sync._tick()
    assert sync.status()["last_outcome"] == "unchanged"
    assert sync._backoff == 1800  # A cheap STATUS says nothing about EXAMINE's cost.

    fake.folders["All Mail"].update(mail_items(4))
    sync._tick()
    assert sync._backoff == 3600

    clock[0] += 3600
    delay[0] = 0.0
    fake.folders["All Mail"].update(mail_items(5))
    sync._tick()
    assert sync.status()["last_outcome"] == "caught_up"
    assert sync._backoff == 0


def test_stuck_epoch_stops_repeat_examines_until_the_epoch_changes(settings):
    fake, reader, sync = job(settings)
    # A header with NUL is never marked known, so this epoch cannot be certified.
    fake.folders["All Mail"]["3"] = message("<3@test.invalid>")
    fake.folders["All Mail"]["3"].headers = fake.folders["All Mail"]["3"].headers.replace(
        b"Hello", b"Hel\x00lo"
    )
    sync._tick()
    assert sync.status()["last_outcome"] == "incomplete"
    assert names(fake).count("EXAMINE") == 1
    fake.calls.clear()

    sync._tick()
    assert sync.status()["last_outcome"] == "stuck"
    assert names(fake) == ["STARTTLS", "LOGIN", "CAPABILITY", "STATUS", "LOGOUT"]
    assert sync._backoff == 0

    fake.calls.clear()
    fake.folders["All Mail"].update(mail_items(4))
    sync._tick()
    assert names(fake).count("EXAMINE") == 1


def test_incomplete_tick_with_progress_is_not_stuck(settings, monkeypatch):
    fake, reader, sync = job(settings)
    fake.folders["All Mail"].update(mail_items(3, 4))
    # A later STATUS moved: everything fetched is kept, but not certified yet.
    status = fake.status
    calls = []

    def moved(folder, fields):
        calls.append(folder)
        if len(calls) == 2:
            fake.folders["All Mail"].update(mail_items(5))
        return status(folder, fields)

    fake.status = moved
    sync._tick()
    assert sync.status()["last_outcome"] == "incomplete"
    assert reader.search_index.known_count("All Mail", "1") == 4
    assert sync._stuck is None


def test_uidvalidity_change_sends_no_examine_and_wipes_nothing(settings):
    fake, reader, sync = job(settings)
    before = reader.search_index.known_count("All Mail", "1")
    fake.uidvalidity = "2"
    sync._tick()

    assert sync.status()["last_outcome"] == "uidvalidity_changed"
    assert "EXAMINE" not in names(fake)
    assert reader.search_index.known_count("All Mail", "1") == before
    assert reader.search_index.coverage_epoch("All Mail") == ("1", 2, "3")
    assert sync._backoff == 0


def test_index_without_an_all_mail_epoch_sends_no_examine(settings):
    fake, reader, sync = job(settings, seed=False)
    # Another folder's proof: an index exists, but nothing says what All Mail held.
    fake.folders["Archive"] = mail_items(1)
    reader.search_index.observe("Archive", "1", {"1": {"headers": {}}})
    assert reader.search_index.record_coverage("Archive", "1", {"exists": 1, "uidnext": "2"}, ["1"])
    assert reader.search_index.reporting_counts() is not None
    sync._tick()

    assert sync.status()["last_outcome"] == "no_epoch"
    assert "EXAMINE" not in names(fake)
    assert "STATUS" in names(fake)


def test_missing_all_mail_role_is_reported_and_read_again(settings):
    fake, reader, sync = job(settings)
    fake.folders["Archive"] = fake.folders.pop("All Mail")
    sync._tick()
    assert sync.status()["last_outcome"] == "no_all_mail"
    assert names(fake).count("LIST") == 1
    fake.folders["All Mail"] = fake.folders.pop("Archive")
    sync._tick()
    assert sync.status()["last_outcome"] == "unchanged"
    assert names(fake).count("LIST") == 2


def test_failed_status_read_counts_as_failure_and_keeps_the_proof(settings):
    fake, reader, sync = job(settings)
    proof = reader.search_index.coverage("All Mail")
    fake.status = Mock(return_value=("NO", [PRIVATE_ERROR.encode()]))
    sync._tick()
    assert sync.status()["last_outcome"] == "failed"
    assert reader.search_index.coverage("All Mail") == proof


# 9. Keep-alive


def finished_audit(mailbox, *, count=2):
    case = matched_case(mailbox, count=count)
    client_ = case.cleanup.client
    client_.settings = replace(client_.settings, background_sync_enabled=True)
    result = case.rules.preview_rules(limit=1)
    snapshots = client_._rules_raw_identity_snapshots
    entry = next(
        cached
        for cached in snapshots.values()
        if cached["inventory"]["snapshot"]["folder"] == "All Mail"
    )
    return case, result, snapshots, entry


def test_ticks_and_wakes_never_touch_saved_audits_or_rules_cursors(mailbox):
    case, result, snapshots, entry = finished_audit(mailbox)
    reader = case.cleanup.client
    seed_baseline(reader, sorted(case.fake.folders["All Mail"], key=int), uidnext="1001")
    sync = BackgroundSync(reader, reader.settings)
    sync._last_request = time.monotonic() - 60
    inventory, expires = deepcopy(entry["inventory"]), entry["expires"]
    states = deepcopy(case.cleanup._rules_preview_states)
    assert states and result["next_cursor"] in states

    drop_expired_raw_snapshots(reader)
    sync._tick()

    assert sync.status()["last_outcome"] == "unchanged"
    assert any(cached is entry for cached in snapshots.values())
    assert set(entry) == {"expires", "inventory"} and entry["expires"] == expires
    assert entry["inventory"] == inventory
    assert entry["inventory"]["checksum"] == cleanup_lookup._preview_inventory_checksum(inventory)
    assert case.cleanup._rules_preview_states == states


@pytest.mark.parametrize(
    "changes,seconds",
    [
        ({"background_sync_enabled": True}, 24 * 3600),
        ({"background_sync_enabled": False}, 600),
        ({"background_sync_enabled": True, "imap_idle_enabled": True}, 600),
    ],
)
def test_only_the_all_mail_audit_stays_for_24_hours_while_the_job_is_active(
    mailbox, monkeypatch, changes, seconds
):
    case = matched_case(mailbox, count=1)
    reader = case.cleanup.client
    reader.settings = replace(reader.settings, **changes)
    clock = [time.monotonic()]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    written = clock[0]
    assert case.rules.preview_rules(limit=1)["token"]

    snapshots = reader._rules_raw_identity_snapshots
    all_mail = _scope(reader.settings, "All Mail")
    # Physical-copy checks keep their 10 minutes in every mode.
    physical = set(snapshots) - {all_mail}
    assert physical
    assert snapshots[all_mail]["expires"] == written + seconds
    assert {snapshots[key]["expires"] for key in physical} == {written + 600}

    clock[0] = written + 599
    drop_expired_raw_snapshots(reader)
    assert set(snapshots) == physical | {all_mail}

    clock[0] = written + 600
    drop_expired_raw_snapshots(reader)
    assert set(snapshots) == ({all_mail} if seconds > 600 else set())

    clock[0] = written + seconds
    drop_expired_raw_snapshots(reader)
    assert not snapshots


def test_a_narrowed_physical_audit_batch_does_not_survive_past_ten_minutes(mailbox, monkeypatch):
    monkeypatch.setattr(cleanup_lookup, "_IDENTITY_BATCH_SIZE", 8)
    case = matched_case(mailbox, count=1)
    reader = case.cleanup.client
    reader.settings = replace(reader.settings, background_sync_enabled=True)
    for uid in range(1, 11):
        case.fake.folders["Trash"][str(uid)] = message(f"<trash-{uid}@example.test>")
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    metadata = reader._metadata
    sizes = []
    cutting = [True]

    def fetch(connection, uids, **options):
        if case.fake.current == "Trash" and options.get("identity_headers_only"):
            sizes.append(len(uids))
            if cutting[0]:
                clock[0] += 21
                options["deadline"].check(connection)
        return metadata(connection, uids, **options)

    monkeypatch.setattr(reader, "_metadata", fetch)
    # The first cut saves the UID list, and the second reuses it and narrows.
    assert case.rules.preview_rules(limit=1)["partial"]
    assert case.rules.preview_rules(limit=1)["partial"]
    trash = reader._rules_raw_identity_snapshots[_scope(reader.settings, "Trash")]
    assert trash["inventory"]["batch_size"] == 2
    assert sizes == [8, 8]
    sizes.clear()
    cutting[0] = False

    clock[0] += 601
    assert case.rules.preview_rules(limit=1)["token"]

    assert sizes == [8, 2]  # Full batches again, for 10 UIDs.


def read_audit_reads(case, monkeypatch):
    reader = case.cleanup.client
    metadata = reader._metadata
    audited = []

    def record(connection, uids, **options):
        if case.fake.current == "All Mail" and options.get("identity_headers_only"):
            audited.append(tuple(uids))
        return metadata(connection, uids, **options)

    monkeypatch.setattr(reader, "_metadata", record)
    return audited


def test_unchanged_recheck_an_hour_later_reads_no_audit_rows(mailbox, monkeypatch):
    case, result, snapshots, entry = finished_audit(mailbox, count=1)
    clock = [time.monotonic()]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    clock[0] += 3600
    audited = read_audit_reads(case, monkeypatch)

    again = case.rules.preview_rules(limit=1)

    assert again["token"] and again["count"] == 1
    # The candidate's own fresh read; the mailbox-wide audit is not repeated.
    assert audited == [("1",)]
    assert_read_only(case.fake)


def test_with_the_job_off_the_audit_lapses_after_ten_minutes(mailbox, monkeypatch):
    case = matched_case(mailbox, count=1)
    clock = [time.monotonic()]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    assert case.rules.preview_rules(limit=1)["token"]
    clock[0] += 601
    audited = read_audit_reads(case, monkeypatch)

    assert case.rules.preview_rules(limit=1)["token"]

    assert len(audited) > 1


def test_a_new_write_starts_a_new_24_hour_window(mailbox, monkeypatch):
    case = matched_case(mailbox, count=1)
    reader = case.cleanup.client
    reader.settings = replace(reader.settings, background_sync_enabled=True)
    clock = [time.monotonic()]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    case.rules.preview_rules(limit=1)
    clock[0] += 3600
    case.rules.preview_rules(limit=1)
    entry = reader._rules_raw_identity_snapshots[_scope(reader.settings, "All Mail")]
    assert entry["expires"] == clock[0] + 24 * 3600


def test_every_wake_frees_expired_audits_even_while_ticks_back_off(settings, monkeypatch):
    fake, reader, sync = job(settings)
    sync._next_tick = time.monotonic() + 3600
    dropped = Mock()
    monkeypatch.setattr(background_sync, "drop_expired_raw_snapshots", dropped)
    wakes = iter([False, False, True])
    sync._stop = Mock(wait=lambda seconds: next(wakes))
    sync._run()
    assert dropped.call_count == 2
    assert fake.calls == []
    assert outcome_of(sync) == "backoff"


def test_a_fresh_write_replacing_an_expired_entry_is_not_dropped(mailbox):
    case, result, snapshots, entry = finished_audit(mailbox)
    key = next(key for key, value in snapshots.items() if value is entry)
    entry["expires"] = time.monotonic() - 1
    fresh = {"expires": time.monotonic() + 600, "inventory": {}}

    class Racing(dict):
        def items(self):
            items = list(super().items())
            self[key] = fresh  # A preview's checkpoint lands during the sweep.
            return items

    case.cleanup.client._rules_raw_identity_snapshots = racing = Racing(snapshots)
    drop_expired_raw_snapshots(case.cleanup.client)
    assert racing[key] is fresh


def higher_uidnext(saved):
    saved["snapshot"] = {**saved["snapshot"], "uidnext": "5000"}
    saved["checksum"] = cleanup_lookup._preview_inventory_checksum(saved)


@pytest.mark.parametrize("enabled", [True, False])
def test_kept_audit_from_a_higher_uidnext_cannot_refuse_every_fresh_preview(mailbox, enabled):
    case, result, snapshots, entry = finished_audit(mailbox, count=1)
    reader = case.cleanup.client
    reader.settings = replace(reader.settings, background_sync_enabled=enabled)
    higher_uidnext(entry["inventory"])

    if enabled:
        assert case.rules.preview_rules(limit=1)["token"]
        rebuilt = next(
            cached["inventory"]
            for cached in snapshots.values()
            if cached["inventory"]["snapshot"]["folder"] == "All Mail"
        )
        assert rebuilt["snapshot"]["uidnext"] == "1001"
    else:
        # With the job off, this refusal lapses with the 10-minute entry, as before.
        with pytest.raises(MailValidationError, match="mailbox changed"):
            case.rules.preview_rules(limit=1)


def test_a_live_cursor_with_a_higher_uidnext_still_refuses(mailbox, monkeypatch):
    case = matched_case(mailbox, count=1)
    reader = case.cleanup.client
    reader.settings = replace(reader.settings, background_sync_enabled=True)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    metadata = reader._metadata
    batches = []

    def fetch(connection, uids, **options):
        if case.fake.current == "All Mail" and options.get("identity_headers_only"):
            batches.append(tuple(uids))
            if len(batches) == 2:
                clock[0] += 21
                options["deadline"].check(connection)
        return metadata(connection, uids, **options)

    monkeypatch.setattr(reader, "_metadata", fetch)
    first = case.rules.preview_rules(limit=1)
    assert first["next_cursor"]
    higher_uidnext(
        case.cleanup._rules_preview_states[first["next_cursor"]]["inventories"]["All Mail"]
    )

    with pytest.raises(MailValidationError, match="mailbox changed"):
        case.rules.preview_rules(limit=1, cursor=first["next_cursor"])


def slow_preview_chain(mailbox, monkeypatch, *, ticks):
    """Run a deadline-limited chain, optionally with real ticks between its calls."""
    case = matched_case(mailbox, count=4)
    reader = case.cleanup.client
    reader.settings = replace(reader.settings, background_sync_enabled=True)
    for uid in range(5, 25):
        case.fake.folders["All Mail"][str(uid)] = message(f"<unrelated-{uid}@example.test>")
    uids = sorted(case.fake.folders["All Mail"], key=int)
    seed_baseline(reader, uids, uidnext="1001")
    sync = BackgroundSync(reader, reader.settings)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cleanup_lookup, "_IDENTITY_BATCH_SIZE", 8)
    metadata = reader._metadata

    def slow(connection, uids, **options):
        if case.fake.current == "All Mail" and options.get("identity_headers_only"):
            clock[0] += min(9.0, options["deadline"].io_end - clock[0])
            options["deadline"].check(connection)
        return metadata(connection, uids, **options)

    monkeypatch.setattr(reader, "_metadata", slow)
    results, progress, outcomes = [], [], []
    cursor = None
    for _ in range(30):
        result = case.rules.preview_rules(limit=1, cursor=cursor)
        cursor = result["next_cursor"]
        if cursor is not None:
            progress.append(case.cleanup._rules_preview_states[cursor]["progress"])
        for key in ("token", "expires_at", "action_hash", "next_cursor"):
            result[key] = bool(result[key])
        results.append(result)
        if ticks:
            sync._last_request = clock[0] - 60
            sync._tick()
            outcomes.append(sync.status()["last_outcome"])
        if cursor is None:
            break
    return results, progress, outcomes, case


def test_preview_chain_with_ticks_matches_one_without(mailbox, monkeypatch):
    plain = slow_preview_chain(mailbox, monkeypatch, ticks=False)
    # Both chains start from the same seeded index, not from the first chain's reads.
    shutil.rmtree(plain[3].cleanup.client.search_index.path.parent)
    ticked = slow_preview_chain(mailbox, monkeypatch, ticks=True)

    assert len(plain[0]) > 1
    assert ticked[0] == plain[0]
    assert ticked[1] == plain[1]
    assert set(ticked[2]) == {"unchanged"}
    assert_read_only(ticked[3].fake)


# 11. Mail arriving during a request


def test_mail_arriving_during_a_request_waits_for_the_next_request(mailbox, monkeypatch):
    case = matched_case(mailbox, count=1)
    reader = case.cleanup.client
    reader.settings = replace(reader.settings, background_sync_enabled=True)
    sync = BackgroundSync(reader, reader.settings)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    metadata = reader._metadata
    fetched = []
    arrived = []

    def arrival(connection, uids, **options):
        if case.fake.current == "All Mail" and options.get("identity_headers_only"):
            fetched.extend(uids)
            if not arrived:
                # Mid-request: a tick is gated off, and this request's walk is
                # bounded by its own SELECT, so it reads no UID at or past that bound.
                arrived.append(True)
                case.fake.folders["All Mail"]["5000"] = message("<arrival@example.test>")
                sync._tick()
                assert outcome_of(sync) == "request_active"
                clock[0] += 21
                options["deadline"].check(connection)
        return metadata(connection, uids, **options)

    monkeypatch.setattr(reader, "_metadata", arrival)
    with sync.foreground():
        first = case.rules.preview_rules(limit=1)
    assert first["partial"] and first["token"] is None and first["next_cursor"]
    assert arrived and "5000" not in fetched

    with sync.foreground():
        second = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
    assert second["token"] and second["count"] == 1
    assert "5000" in fetched
    assert_read_only(case.fake)


# Real imaplib over a socketpair: real tag counting and a really blocked read.


@pytest.fixture
def wire(tmp_path):
    sim = BridgeSim()
    sim.uidvalidity["All Mail"] = 1
    sim.add_many(Msg(hdr(f"<old-{number}@test.invalid>")) for number in range(1, 3))
    reader = make_client(sim, tmp_path)
    reader.settings = replace(reader.settings, background_sync_enabled=True)
    seed_baseline(reader, ["1", "2"])
    sync = BackgroundSync(reader, reader.settings)
    sync._last_request = time.monotonic() - 60
    yield sim, reader, sync
    sync.stop()
    sim.close()
    assert not sim.errors and not sim.violations


def test_real_commands_on_the_worst_tail_path_are_counted_and_read_only(wire):
    sim, reader, sync = wire
    sim.add_many(Msg(hdr(f"<new-{number}@test.invalid>")) for number in range(2000))

    sync._tick()

    status = sync.status()
    assert status["last_outcome"] == "caught_up" and status["rows_today"] == 2000
    assert reader.search_index.coverage("All Mail") == ("1", 2002, "2003")
    # The peer saw exactly what imaplib's counter reports, setup and LOGOUT included.
    assert status["commands_today"] == len(sim.commands) <= 16
    words = [command[3].split() for command in sim.commands]
    assert {" ".join(word[:2]) if word[0] == "UID" else word[0] for word in words} == {
        "CAPABILITY",
        "STARTTLS",
        "LOGIN",
        "LIST",
        "STATUS",
        "EXAMINE",
        "UID SEARCH",
        "UID FETCH",
        "LOGOUT",
    }
    fetches = [command for command in sim.commands if command[3].startswith("UID FETCH")]
    assert len(fetches) == 4
    assert all(selected == "All Mail" and readonly for _, selected, readonly, _ in fetches)
    assert all("BODY.PEEK[" in shown and "BODY[" not in shown for *_, shown in fetches)


def test_foreground_wakes_a_really_blocked_read_without_waiting(wire):
    sim, reader, sync = wire
    sim.add(Msg(hdr("<new@test.invalid>")))
    stalled = threading.Event()

    def stall_fetch(sid, selected, op, shown):
        if op == "FETCH":
            stalled.set()
            return "stall"

    sim.hooks.append(stall_fetch)
    worker = threading.Thread(target=sync._tick, daemon=True)
    worker.start()
    assert stalled.wait(5)

    with sync.foreground():
        pass
    worker.join(4)  # Under the peer's 5 s stall and the 5 s socket timeout.

    assert not worker.is_alive()
    assert sync.status()["last_outcome"] == "yielded"
    assert sync._backoff == 0
    assert not any(command[3] == "LOGOUT" for command in sim.commands)
    assert reader.search_index.coverage_epoch("All Mail") == ("1", 2, "3")


# 10. Tail-only All Mail audits


def record_reads(case, monkeypatch):
    """Split All Mail reads: UID-only enumeration, and raw header or candidate FETCHes."""
    reader = case.cleanup.client
    metadata = reader._metadata
    reads = SimpleNamespace(uid_only=[], raw=[])

    def record(connection, uids, **options):
        if case.fake.current == "All Mail":
            if not options.get("headers", True):
                reads.uid_only.append(tuple(uids))
            elif options.get("identity_headers_only"):
                reads.raw.append(tuple(uids))
        return metadata(connection, uids, **options)

    monkeypatch.setattr(reader, "_metadata", record)
    return reads


def tail_case(mailbox, monkeypatch, *, enabled=True, batch_size=None):
    """A finished All Mail audit of UIDs 1 and 1000 by the first preview of one match."""
    if batch_size is not None:
        monkeypatch.setattr(cleanup_lookup, "_IDENTITY_BATCH_SIZE", batch_size)
    case = matched_case(mailbox, count=1)
    reader = case.cleanup.client
    reader.settings = replace(reader.settings, background_sync_enabled=enabled)
    searches = forbid_full_all_mail_search(case, monkeypatch)
    first = case.rules.preview_rules(limit=1)
    assert first["token"] and first["count"] == 1
    case.searches = searches
    return case


def arrive(case, *uids):
    for uid in uids:
        case.fake.folders["All Mail"][str(uid)] = message(f"<arrival-{uid}@example.test>")


def saved_audit(case):
    snapshots = case.cleanup.client._rules_raw_identity_snapshots
    return next(
        cached["inventory"]
        for cached in snapshots.values()
        if cached["inventory"]["snapshot"]["folder"] == "All Mail"
    )


def assert_exact_audit(case):
    saved = saved_audit(case)
    live = set(case.fake.folders["All Mail"])
    assert "tail_only" not in saved
    assert set(saved["live_uids"]) == live
    assert {row["uid"] for row in saved["rows"]} == live
    assert saved["next_uid"] == int(saved["snapshot"]["uidnext"])
    assert saved["snapshot"]["exists"] == len(live)


def flat(reads):
    return [uid for batch in reads for uid in batch]


def test_growth_reads_only_new_uids_with_no_uid_enumeration(mailbox, monkeypatch):
    case = tail_case(mailbox, monkeypatch)
    reads = record_reads(case, monkeypatch)
    arrive(case, 1001, 1002)

    result = case.rules.preview_rules(limit=1)

    assert result["token"] and result["count"] == 1
    assert reads.uid_only == []
    # The two new UIDs, then the candidate's own fresh read.
    assert reads.raw == [("1001", "1002"), ("1",)]
    assert case.searches == [("UID", "1001:1002")]
    assert_exact_audit(case)
    assert_read_only(case.fake)


def test_switch_off_keeps_the_walk_at_the_first_new_uid(mailbox, monkeypatch):
    case = tail_case(mailbox, monkeypatch, enabled=False)
    reads = record_reads(case, monkeypatch)
    arrive(case, 1001, 1002)

    result = case.rules.preview_rules(limit=1)

    assert result["token"] and result["count"] == 1
    assert reads.uid_only == []
    # The old audit remains complete while the ordinary walk reads its new range.
    assert reads.raw == [("1001", "1002"), ("1",)]
    assert case.searches == []
    assert_exact_audit(case)
    assert_read_only(case.fake)


def scale_rows(count):
    return [
        {
            "uid": str(uid),
            "message_id": f"<{uid}@test.invalid>",
            "_raw_identity_fields": [f"<{uid}@test.invalid>"],
            "identity_invalid": False,
        }
        for uid in range(1, count + 1)
    ]


class ScaleDeadline:
    def check(self, connection=None):
        return 1.0


def scale_case(settings, monkeypatch, count):
    """A cached audit, built directly because 47.5k fake messages would add nothing."""
    settings = replace(settings, background_sync_enabled=True)
    options = ("inbox", "digest", False)
    scope = _scope(settings, "All Mail")
    saved = {
        "validity": "1",
        "scope": scope,
        "options": options,
        "snapshot": {"folder": "All Mail", "exists": count, "uidnext": str(count + 1)},
        "rows": scale_rows(count),
        "live_uids": [str(uid) for uid in range(1, count + 1)],
        "next_uid": count + 1,
        "batch_size": 200,
    }
    saved["checksum"] = cleanup_lookup._preview_inventory_checksum(saved)

    def unexpected_metadata(*args, **kwargs):
        pytest.fail("A tail-only audit needs no UID-only enumeration")

    def tail_search(connection, criteria):
        assert criteria == ["UID", f"{count + 1}:{count + 3}"]
        return [str(uid) for uid in range(count + 1, count + 4)]

    owner = SimpleNamespace(
        settings=settings,
        _metadata=unexpected_metadata,
        _search=tail_search,
        _rules_raw_identity_snapshots={
            scope: {"expires": time.monotonic() + 600, "inventory": saved}
        },
    )
    fetched = []

    def fetch(client, connection, deadline, folder, uids, validity, **kwargs):
        fetched.append(list(uids))
        return [
            {
                "uid": uid,
                "message_id": f"<{uid}@test.invalid>",
                "_raw_identity_fields": [f"<{uid}@test.invalid>"],
            }
            for uid in uids
        ]

    monkeypatch.setattr(cleanup_lookup, "fetch_rows", fetch)
    peer = SimpleNamespace(
        _rules_preview_state={"options": (*options, 200, "oldest")},
        _selected_inventory={"folder": "All Mail", "exists": count + 3, "uidnext": str(count + 4)},
        untagged_responses={"UIDVALIDITY": [b"1"]},
        _cleanup_inventory={},
    )
    return owner, peer, fetched


def test_47500_message_audit_fetches_only_the_three_new_uids(settings, monkeypatch):
    owner, peer, fetched = scale_case(settings, monkeypatch, 47500)

    rows = cleanup_lookup._preview_range_inventory(
        owner, peer, ScaleDeadline(), "All Mail", "1", "membership"
    )

    assert fetched == [["47501", "47502", "47503"]]
    assert len(rows) == 47503
    assert [row["uid"] for row in rows[-3:]] == ["47501", "47502", "47503"]
    assert peer._rules_preview_state["progress"] == 1
    saved = peer._rules_preview_state["inventories"]["All Mail"]
    assert "tail_only" not in saved
    assert saved["next_uid"] == 47504 and len(saved["live_uids"]) == 47503
    assert cleanup_lookup._valid_preview_range_inventory(
        saved, ("inbox", "digest", False), "All Mail", saved["scope"]
    )


def test_hidden_expunge_finishes_in_the_same_call_with_no_error(mailbox, monkeypatch):
    case = tail_case(mailbox, monkeypatch)
    reads = record_reads(case, monkeypatch)
    # The count grew by one, hiding that UID 1000 went: only the walk can tell.
    del case.fake.folders["All Mail"]["1000"]
    arrive(case, 1001, 1002)

    result = case.rules.preview_rules(limit=1)

    assert result["token"] and result["count"] == 1
    assert result["next_cursor"] is None and not result["partial"]
    assert reads.uid_only  # One UID-only re-enumeration, in this same call.
    assert flat(reads.raw).count("1001") == 1  # Rows already read are never read again.
    assert case.searches == [("UID", "1001:1002")]
    assert_exact_audit(case)
    assert "1000" not in {row["uid"] for row in saved_audit(case)["rows"]}
    assert_read_only(case.fake)


def test_same_count_expunge_plus_arrival_takes_the_full_walk_directly(mailbox, monkeypatch):
    case = tail_case(mailbox, monkeypatch)
    reads = record_reads(case, monkeypatch)
    del case.fake.folders["All Mail"]["1000"]
    arrive(case, 1001)

    result = case.rules.preview_rules(limit=1)

    assert result["token"] and result["count"] == 1
    assert reads.uid_only[0][0] == "1"
    assert_exact_audit(case)


@pytest.mark.parametrize("change", ["expunge", "uidnext_only"])
def test_a_mailbox_that_did_not_grow_takes_the_full_walk(mailbox, monkeypatch, change):
    case = tail_case(mailbox, monkeypatch)
    reads = record_reads(case, monkeypatch)
    if change == "expunge":
        del case.fake.folders["All Mail"]["1000"]
    else:
        # An arrival expunged again before this call: same count, higher UIDNEXT.
        arrive(case, 1001)
        case.fake.status('"All Mail"', "(MESSAGES UIDNEXT UIDVALIDITY)")
        del case.fake.folders["All Mail"]["1001"]

    result = case.rules.preview_rules(limit=1)

    assert result["token"] and result["count"] == 1
    assert reads.uid_only[0][0] == "1"
    assert_exact_audit(case)


def cut_second_tail_batch(case, monkeypatch, *, after_fetch=False, on_cut=None):
    """Advance the clock past the deadline on the second FETCH of a tail walk."""
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    reader = case.cleanup.client
    metadata = reader._metadata
    batches = []

    def fetch(connection, uids, **options):
        tail = (
            case.fake.current == "All Mail"
            and options.get("identity_headers_only")
            and int(uids[0]) > 1000
        )
        if not tail:
            return metadata(connection, uids, **options)
        batches.append(tuple(uids))
        if len(batches) == 2 and not after_fetch:
            clock[0] += 21
            if on_cut:
                on_cut()
            options["deadline"].check(connection)
        rows = metadata(connection, uids, **options)
        if len(batches) == 2 and after_fetch:
            clock[0] += 21
        return rows

    monkeypatch.setattr(reader, "_metadata", fetch)
    return batches


@pytest.mark.parametrize("with_cursor", [True, False])
def test_deadline_cut_mid_tail_resumes_where_it_stopped(mailbox, monkeypatch, with_cursor):
    case = tail_case(mailbox, monkeypatch, batch_size=2)
    arrive(case, 1001, 1002, 1003, 1004, 1005)
    batches = cut_second_tail_batch(case, monkeypatch)
    reads = record_reads(case, monkeypatch)

    first = case.rules.preview_rules(limit=1)

    assert first["partial"] and first["token"] is None and first["next_cursor"]
    saved = saved_audit(case)
    assert saved["tail_only"] is True and saved["next_uid"] == 1003
    assert batches == [("1001", "1002"), ("1003", "1004")]

    second = case.rules.preview_rules(limit=1, cursor=first["next_cursor"] if with_cursor else None)

    assert second["token"] and second["count"] == 1
    assert reads.uid_only == []
    # The completed batch is not read again, and each call lists only what is left.
    assert batches == [("1001", "1002"), ("1003", "1004"), ("1003", "1004"), ("1005",)]
    assert case.searches == [("UID", "1001:1005"), ("UID", "1003:1005")]
    assert_exact_audit(case)


@pytest.mark.parametrize("after_fetch", [False, True])
def test_hidden_expunge_found_after_a_cut_still_finishes_without_error(
    mailbox, monkeypatch, after_fetch
):
    # With after_fetch the cut follows the last tail batch, leaving more kept rows
    # than EXISTS. That checkpoint must stay valid or the whole audit would be lost.
    # The first case stops before the last batch, so the count disagrees only later.
    case = tail_case(mailbox, monkeypatch, batch_size=2)
    del case.fake.folders["All Mail"]["1000"]
    arrive(case, 1001, 1002, 1003, 1004)
    batches = cut_second_tail_batch(case, monkeypatch, after_fetch=after_fetch)
    reads = record_reads(case, monkeypatch)

    first = case.rules.preview_rules(limit=1)

    assert first["partial"] and first["next_cursor"]
    saved = saved_audit(case)
    assert saved["tail_only"] is True
    if after_fetch:
        assert len(saved["live_uids"]) > saved["snapshot"]["exists"]
    reads.raw.clear()

    second = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])

    assert second["token"] and second["count"] == 1
    assert reads.uid_only  # The hidden expunge was found by the final count.
    assert "1" not in flat(reads.raw[:-1])  # Old rows survive the re-enumeration.
    assert_exact_audit(case)
    assert len(batches) >= 2


def test_a_second_count_mismatch_after_reenumeration_still_refuses(mailbox, monkeypatch):
    case = tail_case(mailbox, monkeypatch)
    reader = case.cleanup.client
    metadata = reader._metadata
    arrive(case, 1001)

    def hide_old_row(connection, uids, **options):
        rows = metadata(connection, uids, **options)
        if case.fake.current == "All Mail":
            rows.pop("1000", None)
            rows.pop("1001", None)  # Tail count is short, and so is the full walk.
        return rows

    monkeypatch.setattr(reader, "_metadata", hide_old_row)
    with pytest.raises(MailValidationError, match="incomplete UID inventory"):
        case.rules.preview_rules(limit=1)
    assert "tail_only" not in saved_audit(case)
    assert not case.cleanup.tokens._grants or all(
        grant.action == "apply_rules" for grant in case.cleanup.tokens._grants.values()
    )


def test_a_tail_row_omitted_once_is_healed_by_the_same_call(mailbox, monkeypatch):
    case = tail_case(mailbox, monkeypatch)
    reader = case.cleanup.client
    metadata = reader._metadata
    arrive(case, 1001, 1002)
    omitted = []

    def omit_once(connection, uids, **options):
        rows = metadata(connection, uids, **options)
        if case.fake.current == "All Mail" and "1002" in rows and not omitted:
            omitted.append(True)
            rows.pop("1002")
        return rows

    monkeypatch.setattr(reader, "_metadata", omit_once)
    result = case.rules.preview_rules(limit=1)

    assert omitted and result["token"] and result["count"] == 1
    assert_exact_audit(case)


@pytest.mark.parametrize("persistent", [True, False])
def test_a_deletion_cannot_cancel_out_a_tail_row_the_fetch_omitted(
    mailbox, monkeypatch, persistent
):
    case = tail_case(mailbox, monkeypatch)
    reader = case.cleanup.client
    metadata = reader._metadata
    # Live: 1, 1001 and 1002. The kept audit still counts 1000, so with 1002 left
    # out of the FETCH the final count alone agrees.
    del case.fake.folders["All Mail"]["1000"]
    arrive(case, 1001, 1002)
    omissions = []

    def omit(connection, uids, **options):
        rows = metadata(connection, uids, **options)
        if case.fake.current == "All Mail" and "1002" in rows and (persistent or not omissions):
            omissions.append(tuple(uids))
            rows.pop("1002")
        return rows

    monkeypatch.setattr(reader, "_metadata", omit)
    issue = Mock(side_effect=case.cleanup.tokens.issue)
    monkeypatch.setattr(case.cleanup.tokens, "issue", issue)

    if persistent:
        with pytest.raises(MailValidationError, match="incomplete UID inventory"):
            case.rules.preview_rules(limit=1)
        issue.assert_not_called()
        assert "tail_only" not in saved_audit(case)
    else:
        result = case.rules.preview_rules(limit=1)
        assert result["token"] and result["count"] == 1
        assert_exact_audit(case)
    assert omissions


def test_a_tail_row_the_search_omitted_is_still_read(mailbox, monkeypatch):
    # Batches FETCH the whole range, not the list, so a short SEARCH cannot hide a row.
    case = tail_case(mailbox, monkeypatch)
    reader = case.cleanup.client
    search = reader._search
    del case.fake.folders["All Mail"]["1000"]
    arrive(case, 1001, 1002)

    def short_search(connection, criteria, literal=None):
        return [uid for uid in search(connection, criteria, literal) if uid != "1002"]

    monkeypatch.setattr(reader, "_search", short_search)
    result = case.rules.preview_rules(limit=1)

    assert result["token"] and result["count"] == 1
    assert_exact_audit(case)


def test_a_tail_search_past_the_deadline_keeps_the_audit_and_the_batch_size(mailbox, monkeypatch):
    case = tail_case(mailbox, monkeypatch)
    reader = case.cleanup.client
    search = reader._search
    arrive(case, 1001)
    cuts = [True, True]

    def slow_search(connection, criteria, literal=None):
        if cuts:
            cuts.pop()
            raise mail.CallDeadlineExceeded("Synthetic deadline during the tail SEARCH")
        return search(connection, criteria, literal)

    monkeypatch.setattr(reader, "_search", slow_search)
    first = case.rules.preview_rules(limit=1)
    assert first["partial"] and first["next_cursor"]
    assert saved_audit(case)["tail_only"] is True

    # Now an unchanged checkpoint is reused, which is when a cut FETCH would narrow.
    second = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
    assert second["partial"] and second["next_cursor"]
    assert saved_audit(case)["batch_size"] == 200

    third = case.rules.preview_rules(limit=1, cursor=second["next_cursor"])
    assert third["token"] and third["count"] == 1
    assert_exact_audit(case)


def test_arrival_during_a_tail_walk_waits_for_the_next_call(mailbox, monkeypatch):
    case = tail_case(mailbox, monkeypatch, batch_size=2)
    arrive(case, 1001, 1002, 1003, 1004)
    batches = cut_second_tail_batch(case, monkeypatch, on_cut=lambda: arrive(case, 5000))
    reads = record_reads(case, monkeypatch)
    reader = case.cleanup.client
    metadata = reader._metadata
    markers = []

    def watch(connection, uids, **options):
        if case.fake.current == "All Mail":
            saved = connection._rules_preview_state["inventories"].get("All Mail", {})
            markers.append("tail_only" in saved)
        return metadata(connection, uids, **options)

    monkeypatch.setattr(reader, "_metadata", watch)
    first = case.rules.preview_rules(limit=1)

    assert first["partial"] and first["next_cursor"]
    assert "5000" not in flat(reads.raw) and "5000" not in flat(batches)
    assert saved_audit(case)["snapshot"]["uidnext"] == "1005"
    assert markers and all(markers)

    second = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])

    assert second["token"] and second["count"] == 1
    assert "5000" in flat(reads.raw)
    # The unfinished tail carries on: this arrival costs no UID-only enumeration.
    assert reads.uid_only == []
    assert_exact_audit(case)


def test_expunge_during_an_unfinished_tail_takes_an_unmarked_full_walk(mailbox, monkeypatch):
    case = tail_case(mailbox, monkeypatch, batch_size=2)
    arrive(case, 1001, 1002, 1003, 1004)
    cut_second_tail_batch(case, monkeypatch)
    reads = record_reads(case, monkeypatch)
    first = case.rules.preview_rules(limit=1)
    assert first["next_cursor"] and saved_audit(case)["tail_only"] is True
    reader = case.cleanup.client
    metadata = reader._metadata
    markers = []

    def watch(connection, uids, **options):
        if case.fake.current == "All Mail":
            saved = connection._rules_preview_state["inventories"]["All Mail"]
            markers.append("tail_only" in saved)
        return metadata(connection, uids, **options)

    monkeypatch.setattr(reader, "_metadata", watch)
    del case.fake.folders["All Mail"]["1000"]
    del case.fake.folders["All Mail"]["1004"]

    second = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])

    assert second["token"] and second["count"] == 1
    assert reads.uid_only[0][0] == "1"
    assert markers and not any(markers)
    assert_exact_audit(case)


class ExistsImap(GluonSearchImap):
    """Gluon flushes EXISTS after each selected-state command; FakeImap never does."""

    queued_exists = None

    @property
    def untagged_responses(self):
        responses = super().untagged_responses
        if self.queued_exists is not None:
            responses["EXISTS"] = [str(self.queued_exists).encode()]
        return responses

    def select(self, folder, *, readonly):
        self.queued_exists = None
        return super().select(folder, readonly=readonly)


def test_arrival_flushed_during_a_hidden_expunge_walk_does_not_lose_the_cursor(
    mailbox, monkeypatch
):
    # Re-entering the whole audit would re-check EXISTS mid-walk and raise, as #65
    # removed. The in-place walk reaches its deadline with the cursor kept instead.
    case = tail_case(mailbox, monkeypatch)
    case.fake.__class__ = ExistsImap
    del case.fake.folders["All Mail"]["1000"]
    arrive(case, 1001, 1002)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    reader = case.cleanup.client
    metadata = reader._metadata

    def fetch(connection, uids, **options):
        rows = metadata(connection, uids, **options)
        if case.fake.current == "All Mail":
            if uids[0] == "1001":
                case.fake.queued_exists = 4
            elif not options.get("headers", True):
                # The first UID-only batch of the walk uses up this call.
                clock[0] += 21
        return rows

    monkeypatch.setattr(reader, "_metadata", fetch)
    first = case.rules.preview_rules(limit=1)

    assert first["partial"] and first["next_cursor"] and first["token"] is None
    assert "tail_only" not in saved_audit(case)

    monkeypatch.setattr(reader, "_metadata", metadata)
    second = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])

    assert second["token"] and second["count"] == 1
    assert_exact_audit(case)


@pytest.mark.parametrize("enabled", [True, False])
def test_a_new_walk_starts_at_full_batch_size_only_while_the_job_is_active(
    mailbox, monkeypatch, enabled
):
    case = tail_case(mailbox, monkeypatch, enabled=enabled)
    saved = saved_audit(case)
    saved["batch_size"] = 3
    saved["checksum"] = cleanup_lookup._preview_inventory_checksum(saved)
    del case.fake.folders["All Mail"]["1000"]

    assert case.rules.preview_rules(limit=1)["token"]

    assert saved_audit(case)["batch_size"] == (200 if enabled else 3)


def test_arrival_in_the_call_finishing_a_tail_keeps_the_cursor_and_reads_it_next_call(
    mailbox, monkeypatch
):
    # The audit it finished still counts, and the arriving UID waits for the
    # next call on this cursor rather than discarding the completed audit.
    case = tail_case(mailbox, monkeypatch)
    reader = case.cleanup.client
    reads = record_reads(case, monkeypatch)
    metadata = reader._metadata
    arrive(case, 1001)

    def arrival(connection, uids, **options):
        rows = metadata(connection, uids, **options)
        if uids == ["1001"]:
            arrive(case, 1002)
        return rows

    monkeypatch.setattr(reader, "_metadata", arrival)
    first = case.rules.preview_rules(limit=1)
    assert first["partial"] and not first["total_is_exact"]
    assert first["next_cursor"] and first["token"] is None
    assert first["count"] == first["batch_count"] == 0
    assert first["remaining"] == 1
    assert "changed" in first["reason"] and "continue preview_rules" in first["reason"]
    assert "1002" not in flat(reads.raw)
    reads.raw.clear()

    result = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])

    assert result["token"] and result["count"] == 1
    assert reads.uid_only == []
    assert reads.raw == [("1002",), ("1",)]
    assert_exact_audit(case)
    assert_read_only(case.fake)


def test_uidvalidity_change_still_refuses_a_cursor_and_rebuilds_a_fresh_preview(
    mailbox, monkeypatch
):
    case = tail_case(mailbox, monkeypatch, batch_size=2)
    arrive(case, 1001, 1002, 1003, 1004)
    cut_second_tail_batch(case, monkeypatch)
    first = case.rules.preview_rules(limit=1)
    assert first["next_cursor"]
    reads = record_reads(case, monkeypatch)
    case.fake.uidvalidity = "2"

    with pytest.raises(MailValidationError, match="mailbox changed"):
        case.rules.preview_rules(limit=1, cursor=first["next_cursor"])

    # A new preview never treats rows of the old epoch as an audit to extend.
    result = case.rules.preview_rules(limit=1)
    assert result["token"] and result["count"] == 1
    assert reads.uid_only == []
    assert reads.raw[0][0] == "1" and len(reads.raw) > 3
    assert saved_audit(case)["validity"] == "2"


def test_arrival_during_a_partial_audit_keeps_the_walk_position(mailbox, monkeypatch):
    case = matched_case(mailbox, count=1)
    reader = case.cleanup.client
    reader.settings = replace(reader.settings, background_sync_enabled=True)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    metadata = reader._metadata
    batches = []

    def fetch(connection, uids, **options):
        if case.fake.current == "All Mail" and options.get("identity_headers_only"):
            batches.append(tuple(uids))
            if len(batches) == 2:
                clock[0] += 21
                options["deadline"].check(connection)
        return metadata(connection, uids, **options)

    monkeypatch.setattr(reader, "_metadata", fetch)
    first = case.rules.preview_rules(limit=1)
    assert first["partial"] and first["next_cursor"]
    assert saved_audit(case)["next_uid"] == 201
    assert "all_mail_audit=201/1001 rewinds=0" in first["reason"]
    state = case.cleanup._rules_preview_states[first["next_cursor"]]
    reads = record_reads(case, monkeypatch)
    arrive(case, 1001)

    result = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])

    assert result["token"] and result["count"] == 1
    assert reads.uid_only == []
    assert reads.raw[0][0] == "201"
    assert "1001" in flat(reads.raw)
    assert state.get("all_mail_rewinds", 0) == 0
    assert_exact_audit(case)
    assert_read_only(case.fake)


def test_tail_rows_are_really_audited_for_a_hidden_duplicate_identity(mailbox, monkeypatch):
    case = tail_case(mailbox, monkeypatch)
    peer = message("<arrival@example.test>")
    peer.headers = peer.headers[:-2] + b"Message-ID: <rule-1@example.test>\r\n\r\n"
    case.fake.folders["All Mail"]["1001"] = peer

    def unexpected_issue(*args, **kwargs):
        pytest.fail("A hidden identity in a new row must not be approved")

    monkeypatch.setattr(case.cleanup.tokens, "issue", unexpected_issue)
    with pytest.raises(MailValidationError, match="unresolved|ambiguous|Message-ID"):
        case.rules.preview_rules(limit=1)
    assert_read_only(case.fake)


def test_tail_rows_are_really_audited_for_a_protected_sender(mailbox, monkeypatch):
    case = tail_case(mailbox, monkeypatch)
    protected = deepcopy(case.fake.folders["All Mail"]["1"])
    protected.headers = protected.headers.replace(
        b"notifications@github.com", b"relative@gmail.com"
    )
    case.fake.folders["All Mail"]["1001"] = protected

    result = case.rules.preview_rules(limit=1)

    assert result["count"] == 0 and result["token"] is None
    assert result["rules"][0]["blocked"]
    assert_read_only(case.fake)


def test_tail_marker_is_validated_and_bound_to_the_checksum(settings, monkeypatch):
    owner, peer, _ = scale_case(settings, monkeypatch, 3)
    saved = next(iter(owner._rules_raw_identity_snapshots.values()))["inventory"]
    scope, options = saved["scope"], ("inbox", "digest", False)

    def valid(inventory):
        inventory = {**inventory}
        inventory["checksum"] = cleanup_lookup._preview_inventory_checksum(inventory)
        return cleanup_lookup._valid_preview_range_inventory(inventory, options, "All Mail", scope)

    assert valid(saved)
    assert valid({**saved, "tail_only": True})
    assert not valid({**saved, "tail_only": False})
    assert not valid({**saved, "tail_only": 1})
    assert not valid({**saved, "tail_only": "yes"})
    # Unchecksummed, the same marker is altered progress and refuses.
    assert not cleanup_lookup._valid_preview_range_inventory(
        {**saved, "tail_only": True}, options, "All Mail", scope
    )
    # More kept rows than EXISTS need the marker: a plain walk can never get there.
    over = {**saved, "snapshot": {**saved["snapshot"], "exists": 2}}
    assert not valid(over)
    assert valid({**over, "tail_only": True})


def test_keep_alive_and_tail_only_make_a_later_recheck_read_only_new_mail(mailbox, monkeypatch):
    case = tail_case(mailbox, monkeypatch)
    reader = case.cleanup.client
    clock = [time.monotonic()]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    clock[0] += 2 * 3600
    drop_expired_raw_snapshots(reader)
    reads = record_reads(case, monkeypatch)
    arrive(case, 1001)

    result = case.rules.preview_rules(limit=1)

    assert result["token"] and result["count"] == 1
    assert reads.uid_only == []
    assert reads.raw == [("1001",), ("1",)]
