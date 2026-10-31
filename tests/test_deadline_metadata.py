"""Protocol batch parsing and deadline enforcement using offline socket peers."""

import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mock_imap_cleanup import CleanupIMAPPeer
from test_imap_deadline import mailbox_peer
from test_mail import PLAIN, FakeImap, client, message

from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.mail import CallDeadlineExceeded, MailClient, _Deadline, _DeadlineIMAP4
from proton_mail_mcp.security import Credentials


@pytest.fixture
def settings():
    return Settings(
        token="a-test-token-that-is-at-least-32-characters", bridge_user="reader@example.com"
    )


def test_batch_metadata_handles_uid_and_structure_after_header_literal(settings):
    raw = message("<literal@example.test>").headers

    class LiteralFirstImap(FakeImap):
        def uid(self, command, *args):
            assert command == "FETCH"
            assert args[0] == "42,99"
            return "OK", [
                (
                    b"8 (BODY[HEADER.FIELDS (MESSAGE-ID SUBJECT)] {"
                    + str(len(raw)).encode()
                    + b"}",
                    raw,
                ),
                b' UID 42 INTERNALDATE "17-Oct-2026 09:00:00 +0000" BODYSTRUCTURE ' + PLAIN + b")",
                (
                    b"2 (UID 99 BODY[HEADER.FIELDS (MESSAGE-ID SUBJECT)] {"
                    + str(len(raw)).encode()
                    + b"}",
                    raw,
                ),
                b" BODYSTRUCTURE " + PLAIN + b' INTERNALDATE "16-Oct-2026 09:00:00 +0000")',
            ]

    backend = client(settings, LiteralFirstImap())
    records = backend._metadata(LiteralFirstImap(), ["42", "99"], deadline=backend._deadline())
    assert set(records) == {"42", "99"}
    assert str(records["42"]["headers"]["Message-ID"]) == "<literal@example.test>"
    assert isinstance(records["42"]["structure"], list)
    assert records["42"]["internaldate"] > records["99"]["internaldate"]


def test_deadline_caps_every_socket_step_and_refuses_work_after_expiry(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: now[0])
    # Fifty seconds includes the five-second response reserve, leaving the
    # original 45-second I/O window for these socket-cap boundary assertions.
    deadline = _Deadline(50, 30)
    connection = Mock()
    assert deadline.check(connection) == 30
    connection.sock.settimeout.assert_called_with(30)
    now[0] += 40
    assert deadline.check(connection) == 5
    connection.sock.settimeout.assert_called_with(5)
    now[0] += 5
    with pytest.raises(CallDeadlineExceeded):
        deadline.check(connection)


def test_deadline_diagnostic_accounts_for_current_and_revisited_stages(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: now[0])
    deadline = _Deadline(20, 10)
    now[0] += 0.5
    deadline.stage = "source discovery"
    now[0] += 2
    deadline.stage = "membership check"
    now[0] += 3
    deadline.stage = "source discovery"
    now[0] += 0.25
    assert deadline.diagnostic() == (
        "stage=source discovery; stage_ms=(connection setup: 500, "
        "source discovery: 2250, membership check: 3000)"
    )
    # Reading diagnostics does not reset a running stage or change its budget.
    now[0] += 0.25
    assert "source discovery: 2500" in deadline.diagnostic()
    assert deadline.end == 120


def test_socket_timeout_before_mailbox_data_returns_partial_and_closes_without_logout(settings):
    fake = FakeImap()
    fake.shutdown = Mock()

    def slow(*args):
        raise TimeoutError("synthetic diagnostic")

    fake.uid = slow
    result = client(settings, fake).search_mail()
    assert result["timed_out"] is True and result["partial"] is True
    assert result["messages"] == [] and "timed out" in result["error"]
    fake.shutdown.assert_called_once_with()
    assert not any(call[0] == "LOGOUT" for call in fake.calls)


def test_caught_scan_socket_timeout_still_closes_session_with_partial_headers(settings):
    fake = FakeImap(
        {"INBOX": {str(uid): message(f"<{uid}@example.test>") for uid in range(1, 502)}}
    )
    fake.shutdown = Mock()
    original_uid = fake.uid
    metadata_fetches = 0

    def interrupted(command, *args):
        nonlocal metadata_fetches
        if command == "FETCH":
            metadata_fetches += 1
            if metadata_fetches == 2:
                raise TimeoutError("synthetic diagnostic")
        return original_uid(command, *args)

    fake.uid = interrupted
    result = client(settings, fake).search_mail(subject="Hello", limit=2)
    assert result["partial"] is True
    assert result["total"] == 500 and result["total_is_exact"] is False
    assert len(result["messages"]) == 2
    assert metadata_fetches == 2
    fake.shutdown.assert_called_once_with()
    assert not any(call[0] == "LOGOUT" for call in fake.calls)


def test_connect_timeout_is_limited_by_call_budget(settings):
    configuration = replace(settings, call_timeout=5)
    factory = Mock(return_value=FakeImap())
    backend = MailClient(configuration, lambda _: Credentials("test@example.test", "fake"), factory)
    assert backend.search_mail()["partial"] is False
    assert 0 < factory.call_args.kwargs["timeout"] <= 5


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), 301])
def test_invalid_call_timeout_is_rejected(settings, value):
    with pytest.raises(ConfigError, match="PROTON_MCP_CALL_TIMEOUT"):
        replace(settings, call_timeout=value)


def test_call_timeout_environment_configuration(settings):
    configuration = Settings.from_env(
        {
            "PROTON_MCP_TOKEN": settings.token,
            "PROTON_BRIDGE_USER": settings.bridge_user,
            "PROTON_MCP_CALL_TIMEOUT": "42.5",
        }
    )
    assert configuration.call_timeout == 42.5


class RecordedWatchdog:
    def __init__(self, delay, callback):
        self.callback = callback
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def watchdogs(monkeypatch):
    created = []

    def factory(delay, callback):
        watchdog = RecordedWatchdog(delay, callback)
        created.append(watchdog)
        return watchdog

    monkeypatch.setattr("proton_mail_mcp.mail.threading.Timer", factory)
    return created


def test_nested_guards_reuse_watchdog_and_late_callback_cannot_close_socket(watchdogs):
    deadline = _Deadline(45, 30)
    connection = SimpleNamespace(sock=Mock())
    with deadline.guard(connection):
        for _ in range(100):
            with deadline.guard(connection):
                with deadline.guard(connection):
                    pass
        assert len(watchdogs) == 1
        assert watchdogs[0].started and not watchdogs[0].cancelled
    assert watchdogs[0].cancelled
    watchdogs[0].callback()
    connection.sock.shutdown.assert_not_called()
    with deadline.guard(connection):
        assert len(watchdogs) == 2
    assert all(watchdog.cancelled for watchdog in watchdogs)


def test_completed_command_disables_watchdog_before_final_deadline_check(watchdogs, monkeypatch):
    deadline = _Deadline(45, 30)
    connection = SimpleNamespace(sock=Mock())
    check = deadline.check

    def race_at_completion(current=None):
        if current is None:
            # Fire between successful command completion and timer cleanup,
            # while the watchdog still exists and has not been cancelled.
            assert len(watchdogs) == 1 and not watchdogs[0].cancelled
            watchdogs[0].callback()
        return check(current)

    monkeypatch.setattr(deadline, "check", race_at_completion)
    with deadline.guard(connection):
        pass
    connection.sock.shutdown.assert_not_called()
    assert watchdogs[0].cancelled and not deadline.expired


def test_nested_guard_for_different_socket_restores_outer_watchdog(watchdogs):
    deadline = _Deadline(45, 30)
    first = SimpleNamespace(sock=Mock())
    second = SimpleNamespace(sock=Mock())
    with deadline.guard(first):
        with deadline.guard(second):
            assert len(watchdogs) == 2
        with deadline.guard(first):
            assert len(watchdogs) == 2
    assert all(watchdog.cancelled for watchdog in watchdogs)
    for watchdog in watchdogs:
        watchdog.callback()
    first.sock.shutdown.assert_not_called()
    second.sock.shutdown.assert_not_called()


def test_nested_read_timeout_maps_to_deadline_and_cancels_watchdog(watchdogs):
    deadline = _Deadline(45, 30)
    connection = SimpleNamespace(sock=Mock())
    with pytest.raises(CallDeadlineExceeded, match="Mailbox call timed out"):
        with deadline.guard(connection):
            with deadline.guard(connection):
                raise TimeoutError("Synthetic server detail must not escape")
    assert deadline.expired
    assert len(watchdogs) == 1 and watchdogs[0].cancelled
    watchdogs[0].callback()
    connection.sock.shutdown.assert_not_called()
    with pytest.raises(CallDeadlineExceeded):
        with deadline.guard(connection):
            pytest.fail("An expired stream must not be reused")
    assert len(watchdogs) == 1


def test_production_imap_bulk_headers_reuse_watchdogs_and_leave_no_timer_threads(
    settings, monkeypatch
):
    peer = CleanupIMAPPeer()
    peer.folders["INBOX"] = {
        str(uid): message(f"<bulk-{uid}@example.invalid>").headers for uid in range(1, 101)
    }
    socket_factory = peer.factory()
    monkeypatch.setattr(_DeadlineIMAP4, "_create_socket", socket_factory._create_socket)
    monkeypatch.setattr(_DeadlineIMAP4, "starttls", socket_factory.starttls)
    timers = []
    timer_factory = threading.Timer

    def tracked_timer(*args, **kwargs):
        timer = timer_factory(*args, **kwargs)
        timers.append(timer)
        return timer

    monkeypatch.setattr("proton_mail_mcp.mail.threading.Timer", tracked_timer)
    backend = MailClient(settings, lambda _: Credentials("reader@example.com", "dummy"))
    try:
        result = backend.search_mail(limit=3, include_snippets=False, subject="Hello")
        assert len(result["messages"]) == 3 and result["total"] == 100
        assert result["partial"] is False
        # A watchdog per header line/literal would create over 300 threads.
        assert 1 < len(timers) < 30
    finally:
        peer.close()
        for timer in timers:
            timer.join(timeout=1)
        assert all(not timer.is_alive() for timer in timers)


@pytest.mark.parametrize("mode", ["stall", "multiline", "literal", "line"])
def test_production_imap_nested_guards_still_interrupt_trickling_reads(monkeypatch, mode):
    stage = "SEARCH" if mode == "line" else "FETCH"
    with mailbox_peer(monkeypatch, mode=mode, stage=stage, default_factory=True) as (
        backend,
        commands,
        local,
    ):
        result = backend.search_mail(include_snippets=False)
        assert result["partial"] is True and result["timed_out"] is True
        assert local.fileno() == -1
        assert not any(command.startswith(b"LOGOUT") for command in commands)
