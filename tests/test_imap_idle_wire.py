"""Real imaplib parsing with an in-memory peer and explicitly fired timers."""

import imaplib
from collections import deque
from unittest.mock import Mock

import pytest
from test_mail import settings as settings

from proton_mail_mcp import imap_idle
from proton_mail_mcp.mail import MailError, _Deadline, _TimedConnection


class Timer:
    instances = []

    def __init__(self, interval, function, args=()):
        self.interval, self.function, self.args = interval, function, args
        self.cancelled = False
        self.started = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        assert self.started and not self.cancelled
        self.function(*self.args)


class Wire(imaplib.IMAP4):
    def open(self, host, port, timeout=None):
        self.sock = Mock()
        self.lines = deque([b"* OK test server\r\n"])
        self.sent = []
        self.before_continuation = []
        self.after_continuation = []
        self.before_completion = []
        self.waiting = None
        self.continued = False

    def send(self, data):
        self.sent.append(data)
        if data == b"DONE\r\n":
            assert self.continued
            self.continued = False
            self.lines.extend(self.before_completion)
            self.lines.append(self.tag + b" OK idle complete\r\n")
            return
        tag, command = data.rstrip().split(b" ", 1)
        if command == b"CAPABILITY":
            self.lines.extend([b"* CAPABILITY IMAP4rev1 IDLE\r\n", tag + b" OK caps\r\n"])
        else:
            assert command == b"IDLE", data
            self.tag = tag
            self.lines.extend(self.before_continuation)
            self.lines.append(b"+ idling\r\n")
            self.lines.extend(self.after_continuation)

    def readline(self):
        if not self.lines:
            assert self.waiting is not None, "The reader would block"
            self.waiting()
        line = self.lines.popleft()
        if line.startswith(b"+"):
            self.continued = True
        return line

    def shutdown(self):
        pass


@pytest.fixture
def wire(monkeypatch, settings):
    Timer.instances = []
    monkeypatch.setattr(imap_idle.threading, "Timer", Timer)
    raw = Wire()
    raw.state = "SELECTED"
    raw.untagged_responses.clear()
    deadline = _Deadline(settings.call_timeout, settings.timeout)
    return raw, _TimedConnection(raw, deadline), deadline


@pytest.mark.parametrize("before", [False, True])
def test_idle_waits_for_continuation_then_done_and_tagged_completion(wire, settings, before):
    raw, connection, deadline = wire
    if before:
        raw.before_continuation = [b"* 2 EXISTS\r\n"]
    else:
        raw.after_continuation = [b"* 2 EXISTS\r\n"]
    raw.before_completion = [b"* 1 EXPUNGE\r\n"]

    assert imap_idle._idle(connection, deadline, settings)

    assert raw.sent[-1] == b"DONE\r\n"
    assert raw.sent[-2].endswith(b" IDLE\r\n")
    assert connection.untagged_responses["EXPUNGE"] == [b"1"]
    assert not raw.tagged_commands  # The tagged completion was consumed.
    assert not raw.lines
    assert all(timer.cancelled for timer in Timer.instances)


def test_idle_renews_at_28_minutes_without_timeout_or_inventory_poll(wire, settings, monkeypatch):
    raw, connection, deadline = wire
    clock = [100.0]
    monkeypatch.setattr(imap_idle.time, "monotonic", lambda: clock[0])

    def renew():
        timer = next(
            timer
            for timer in reversed(Timer.instances)
            if timer.interval == 28 * 60 and not timer.cancelled
        )
        clock[0] += timer.interval
        assert not deadline.expired
        timer.fire()

    raw.waiting = renew
    for _ in range(2):
        assert not imap_idle._idle(connection, deadline, settings)
        # Even a timer callback already dispatched when cancel ran cannot kill
        # the next exchange. Deliberately call cancelled callbacks here.
        for timer in Timer.instances:
            if timer.interval in (settings.timeout, imap_idle.IDLE_SECONDS):
                timer.function(*timer.args)
        raw.sock.shutdown.assert_not_called()
    assert [command.split(b" ")[-1] for command in raw.sent[1:]] == [
        b"IDLE\r\n",
        b"DONE\r\n",
        b"IDLE\r\n",
        b"DONE\r\n",
    ]
    assert deadline.socket_timeout == settings.timeout


@pytest.mark.parametrize("response", [b"* BYE disconnect\r\n", b"malformed\r\n"])
def test_broken_idle_closes_stream_and_restores_short_budget(wire, settings, response):
    raw, connection, deadline = wire
    raw.after_continuation = [response]

    with pytest.raises((imaplib.IMAP4.abort, MailError)):
        imap_idle._idle(connection, deadline, settings)

    raw.sock.shutdown.assert_called_once()
    assert deadline.socket_timeout == settings.timeout
    assert all(timer.cancelled for timer in Timer.instances)
