"""Hard deadlines against real imaplib and local socketpair peers only."""

import imaplib
import socket
import threading
import time
from contextlib import contextmanager, suppress

import pytest
from test_mail import message

from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import MailClient, _DeadlineIMAP4
from proton_mail_mcp.search_index import SearchIndex
from proton_mail_mcp.security import Credentials


@contextmanager
def mailbox_peer(
    monkeypatch,
    *,
    mode="stall",
    stage="FETCH",
    default_factory=False,
    simulate_tls=True,
    seed=False,
    call_timeout=0.4,
):
    """Simulate TLS's protocol boundary; no network or TLS server is involved."""
    local, remote = socket.socketpair()
    stopped = threading.Event()
    commands = []

    def send(chunk, delay=0):
        if stopped.wait(delay):
            return False
        remote.sendall(chunk)
        return True

    def slow_response(tag, command):
        if mode == "stall":
            # Receive data late in the command, then stall. A timeout applied
            # only once per command would allow another full timeout here.
            send(b"* 1 FETCH (FLAGS ())\r\n", 0.34)
            stopped.wait(3)
        elif mode == "multiline":
            for uid in range(1, 21):
                if not send(
                    f"* {uid} FETCH (UID {uid} INTERNALDATE "
                    '"17-Oct-2026 09:00:00 +0000")\r\n'.encode(),
                    0.07,
                ):
                    return
            send(tag + b" OK done\r\n")
        elif mode == "literal":
            send(b"* 1 FETCH (UID 1 BODY[HEADER] {20}\r\n")
            for _ in range(20):
                if not send(b"x", 0.07):
                    return
            send(b")\r\n" + tag + b" OK done\r\n")
        elif mode == "line":
            prefix = b"* OK " if command == b"greeting" else b"* SEARCH "
            send(prefix)
            # Keep trickling for at least twice the call budget.
            for _ in range(max(20, int(2 * call_timeout / 0.07))):
                if not send(b"1 ", 0.07):
                    return
            send(b"\r\n" + (tag + b" OK done\r\n" if tag else b""))

    def serve():
        try:
            if stage == "greeting":
                slow_response(b"", b"greeting")
                return
            send(b"* OK [CAPABILITY IMAP4rev1 STARTTLS] test ready\r\n")
            with remote.makefile("rb") as reader:
                while line := reader.readline():
                    tag, _, command = line.rstrip(b"\r\n").partition(b" ")
                    commands.append(command)
                    word = command.split(b" ", 1)[0]
                    operation = command.split(b" ")[1] if word == b"UID" else word
                    if (
                        operation.decode() == stage
                        or (
                            stage == "seed_lookup"
                            and operation == b"SEARCH"
                            and b" UID " not in command
                        )
                        or (stage == "SEARCH ON" and operation == b"SEARCH" and b" ON " in command)
                    ):
                        slow_response(tag, operation)
                        return
                    ok = tag + b" OK done\r\n"
                    if word == b"CAPABILITY":
                        send(b"* CAPABILITY IMAP4rev1 STARTTLS\r\n" + ok)
                    elif word == b"STARTTLS" and stage == "TLS":
                        send(ok, 0.3)
                        stopped.wait(3)
                        return
                    elif word in (b"STARTTLS", b"LOGIN"):
                        send(ok)
                    elif word == b"EXAMINE":
                        send(
                            (
                                b"* 20431 EXISTS\r\n* OK [UIDNEXT 20432] next\r\n"
                                if seed
                                else b"* 20 EXISTS\r\n"
                            )
                            + b"* OK [UIDVALIDITY 7] stable\r\n"
                            + ok
                        )
                    elif word == b"LIST":
                        if stage == "SEARCH ON":
                            send(b'* LIST (\\All) "/" "All Mail"\r\n* LIST () "/" "INBOX"\r\n' + ok)
                        else:
                            send(b'* LIST (\\All) "/" "INBOX"\r\n' + ok)
                    elif operation == b"SEARCH":
                        uids = (
                            b"20431" if seed else b" ".join(str(i).encode() for i in range(1, 21))
                        )
                        send(b"* SEARCH " + uids)
                        send(b"\r\n" + ok)
                    elif operation == b"FETCH" and seed:
                        item = message("<seed@example.test>")
                        if b"HEADER.FIELDS" in command:
                            send(
                                b'* 1 FETCH (UID 20431 INTERNALDATE "14-Oct-2026 10:00:00 +0000" '
                                b"RFC822.SIZE 512 "
                                b"BODY[HEADER.FIELDS (MESSAGE-ID FROM TO SUBJECT DATE)]"
                                b"<0> {"
                                + str(len(item.headers)).encode()
                                + b"}\r\n"
                                + item.headers
                                + b")\r\n"
                                + ok
                            )
                        elif b"BODYSTRUCTURE" in command:
                            send(b"* 1 FETCH (BODYSTRUCTURE " + item.structure + b")\r\n" + ok)
                        elif b"ENVELOPE" in command:
                            send(
                                b'* 1 FETCH (UID 20431 INTERNALDATE "14-Oct-2026 10:00:00 +0000" '
                                b"RFC822.SIZE 512 ENVELOPE "
                                b"(NIL NIL NIL NIL NIL NIL NIL NIL NIL NIL))\r\n" + ok
                            )
                        else:
                            body = item.sections["1"]
                            send(
                                b"* 1 FETCH (BODY[1]<0> {"
                                + str(len(body)).encode()
                                + b"}\r\n"
                                + body
                                + b")\r\n"
                                + ok
                            )
                    elif word == b"LOGOUT":
                        send(b"* BYE done\r\n" + ok)
                        return
                    else:
                        send(tag + b" BAD unsupported test command\r\n")
        except OSError:
            pass

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()

    def create_socket(self, timeout):
        local.settimeout(timeout)
        return local

    def starttls(self, ssl_context=None):
        return self._simple_command("STARTTLS")

    class PairImap(imaplib.IMAP4):
        _create_socket = create_socket

    PairImap.starttls = starttls
    factory = PairImap
    if default_factory:
        monkeypatch.setattr(_DeadlineIMAP4, "_create_socket", create_socket)
        if simulate_tls:
            monkeypatch.setattr(_DeadlineIMAP4, "starttls", starttls)
        factory = None
    settings = Settings(
        token="local-test-token-" + "x" * 32,
        bridge_user="reader@example.test",
        timeout=1,
        call_timeout=call_timeout,
    )
    backend = MailClient(settings, lambda _: Credentials("reader@example.test", "dummy"), factory)
    try:
        yield backend, commands, local
    finally:
        stopped.set()
        for sock in (local, remote):
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        worker.join(timeout=1)
        assert not worker.is_alive()


@pytest.mark.parametrize("mode", ["stall", "multiline", "literal", "line"])
def test_deadline_interrupts_stalls_and_trickles_inside_a_command(monkeypatch, mode):
    stage = "SEARCH" if mode == "line" else "FETCH"
    with mailbox_peer(monkeypatch, mode=mode, stage=stage) as (backend, commands, _):
        started = time.monotonic()
        result = backend.search_mail(include_snippets=False)
        assert result["partial"] is True and result["timed_out"] is True
        assert time.monotonic() - started < 0.65
        assert any(command.startswith(b"EXAMINE ") for command in commands)
        assert not any(command.startswith(b"LOGOUT") for command in commands)


@pytest.mark.parametrize("stage", ["greeting", "CAPABILITY"])
def test_default_factory_bounds_constructor_reads_and_closes_socket(monkeypatch, stage):
    with mailbox_peer(monkeypatch, mode="line", stage=stage, default_factory=True) as (
        backend,
        _,
        local,
    ):
        started = time.monotonic()
        result = backend.search_mail()
        assert result["timed_out"] is True and "timed out" in result["error"]
        assert time.monotonic() - started < 0.65
        assert local.fileno() == -1


def test_thread_index_seed_survives_stalled_search_with_real_watchdog(monkeypatch):
    # A longer budget leaves the seed's round trips ample time on a loaded host.
    with mailbox_peer(
        monkeypatch, mode="line", stage="SEARCH", default_factory=True, seed=True, call_timeout=2
    ) as (backend, commands, local):
        backend.search_index = SearchIndex(backend.settings, rebuild=True)
        backend.search_index.observe(
            "INBOX", "7", {"20431": {"headers": {"Message-ID": "<seed@example.test>"}}}
        )
        started = time.monotonic()
        result = backend.read_thread("<seed@example.test>")
        assert time.monotonic() - started < 2
        assert result["partial"] is True and result["timed_out"] is True
        [item] = result["messages"]
        assert item["message_id"] == "<seed@example.test>"
        assert item["text"] == "Hello plain text"
        headers = [command for command in commands if b"HEADER.FIELDS" in command]
        assert len(headers) == 1 and headers[0].startswith(b"UID FETCH 20431 ")
        searches = [command for command in commands if command.startswith(b"UID SEARCH ")]
        assert len(searches) == 1 and b" OR " in searches[0]
        assert commands.index(headers[0]) < commands.index(searches[0])
        assert local.fileno() == -1
        assert not any(command.startswith(b"LOGOUT") for command in commands)


def test_thread_seed_miss_uses_one_message_id_lookup_with_real_transport(monkeypatch):
    with mailbox_peer(
        monkeypatch, stage="none", default_factory=True, seed=True, call_timeout=2
    ) as (backend, commands, local):
        started = time.monotonic()
        result = backend.read_thread("<seed@example.test>", include_thread=False)
        assert time.monotonic() - started < 2
        # LIST marks this mailbox as an All Mail view; direct reads skip lookup.
        assert result["partial"] is False and "timed_out" not in result
        [item] = result["messages"]
        assert item["message_id"] == "<seed@example.test>"
        assert item["text"] == "Hello plain text"
        assert (item["folder"], item["uidvalidity"], item["uid"]) == (None, None, None)
        assert item["alias"] == {"folder": "INBOX", "uidvalidity": "7", "uid": "20431"}
        searches = [command for command in commands if command.startswith(b"UID SEARCH ")]
        assert len(searches) == 1
        assert searches[0].endswith(b' HEADER Message-ID "seed@example.test"')
        assert b" UID " not in searches[0]
        assert local.fileno() == -1 and commands[-1] == b"LOGOUT"


@pytest.mark.parametrize("mode", ["stall", "line"])
def test_thread_seed_miss_short_cap_interrupts_lookup_with_real_watchdog(monkeypatch, mode):
    monkeypatch.setattr("proton_mail_mcp.threading.MAX_SEED_LOOKUP_SECONDS", 0.5)
    with mailbox_peer(
        monkeypatch,
        mode=mode,
        stage="seed_lookup",
        default_factory=True,
        seed=True,
        call_timeout=2,
    ) as (backend, commands, local):
        started = time.monotonic()
        result = backend.read_thread("<seed@example.test>", include_thread=False)

        # The dedicated seed budget ends stalls and trickling responses before
        # the full thread deadline, without claiming a content-read timeout.
        assert time.monotonic() - started < 0.7
        assert result["partial"] is True and result["messages"] == []
        assert "lookup incomplete" in result["partial_reason"]
        assert "timed_out" not in result and "error" not in result
        searches = [command for command in commands if command.startswith(b"UID SEARCH ")]
        assert len(searches) == 1
        assert searches[0].endswith(b' HEADER Message-ID "seed@example.test"')
        assert local.fileno() == -1
        assert not any(command.startswith(b"LOGOUT") for command in commands)


def test_thread_canonical_cap_interrupts_search_with_real_watchdog(monkeypatch):
    monkeypatch.setattr("proton_mail_mcp.threading.MAX_CANONICAL_SECONDS", 0.5)
    with mailbox_peer(
        monkeypatch, stage="SEARCH ON", default_factory=True, seed=True, call_timeout=2
    ) as (backend, commands, local):
        started = time.monotonic()
        result = backend.read_thread("<seed@example.test>")
        # The socket watchdog must unwind before the cap plus outer close grace.
        assert time.monotonic() - started < 0.7
        assert result["partial"] is True
        assert "canonical" in result["partial_reason"].lower()
        assert "time limit" in result["partial_reason"].lower()
        assert "timed_out" not in result and "error" not in result
        [item] = result["messages"]
        assert item["message_id"] == "<seed@example.test>"
        assert item["text"] == "Hello plain text" and item["truncated"] is False
        assert (item["folder"], item["uidvalidity"], item["uid"]) == (None, None, None)
        assert item["alias"] == {"folder": "All Mail", "uidvalidity": "7", "uid": "20431"}
        assert any(
            command.startswith(b"UID SEARCH ") and b" ON " in command for command in commands
        )
        assert local.fileno() == -1
        assert not any(command.startswith(b"LOGOUT") for command in commands)


def test_deadline_interrupts_tls_handshake_after_delayed_starttls_reply(monkeypatch):
    with mailbox_peer(monkeypatch, stage="TLS", default_factory=True, simulate_tls=False) as (
        backend,
        commands,
        _,
    ):
        started = time.monotonic()
        result = backend.search_mail()
        assert result["timed_out"] is True and result["partial"] is True
        assert time.monotonic() - started < 0.65
        assert any(command == b"STARTTLS" for command in commands)
        assert not any(command.startswith((b"LOGIN", b"LOGOUT")) for command in commands)


@pytest.mark.parametrize("stage", ["STARTTLS", "LOGIN", "EXAMINE"])
@pytest.mark.parametrize("operation", ["search", "thread"])
def test_stalled_setup_returns_timeout_before_mailbox_data(monkeypatch, stage, operation):
    with mailbox_peer(monkeypatch, stage=stage) as (backend, commands, _):
        started = time.monotonic()
        if operation == "search":
            result = backend.search_mail()
        else:
            result = backend.read_thread("<seed@example.test>")
        assert result["timed_out"] is True and result["partial"] is True
        assert result["messages"] == [] and "timed out" in result["error"]
        assert time.monotonic() - started < 0.65
        assert not any(command.startswith(b"LOGOUT") for command in commands)


@pytest.mark.parametrize("operation", ["search", "thread"])
def test_connect_timeout_returns_partial_before_mailbox_data(operation):
    settings = Settings(token="local-test-token-" + "x" * 32, bridge_user="reader@example.test")

    def unavailable(*args, **kwargs):
        raise TimeoutError("dummy peer never connected")

    backend = MailClient(
        settings, lambda _: Credentials("reader@example.test", "dummy"), unavailable
    )
    if operation == "search":
        result = backend.search_mail()
    else:
        result = backend.read_thread("<seed@example.test>")
    assert result["timed_out"] is True and result["partial"] is True
    assert result["messages"] == [] and "timed out" in result["error"]
