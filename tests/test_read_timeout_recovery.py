"""Read deadlines and connection recovery use real imaplib over socketpairs."""

import socket
import threading
import time
from contextlib import contextmanager, suppress

import pytest

from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import CallDeadlineExceeded, MailClient, _DeadlineIMAP4
from proton_mail_mcp.security import Credentials


@contextmanager
def read_peer(monkeypatch, *, stage="SEARCH", trickle=False):
    """The first connection stalls; every later connection answers normally."""
    sockets = []
    workers = []
    commands = []
    stopped = threading.Event()

    def serve(remote, calls, first):
        def send(data):
            remote.sendall(data)

        def stall():
            if trickle:
                # Frequent bytes keep a socket read timeout from firing. The
                # per-command watchdog must still interrupt its buffered read.
                send(b"* SEARCH ")
                while not stopped.wait(0.02):
                    send(b"1 ")
            else:
                stopped.wait(3)

        try:
            if first and stage == "greeting":
                stall()
                return
            send(b"* OK [CAPABILITY IMAP4rev1 STARTTLS] ready\r\n")
            with remote.makefile("rb") as reader:
                while line := reader.readline():
                    tag, _, command = line.rstrip(b"\r\n").partition(b" ")
                    calls.append(command)
                    word = command.split(b" ", 1)[0]
                    operation = command.split(b" ")[1] if word == b"UID" else word
                    if first and operation.decode("ascii") == stage:
                        stall()
                        return
                    ok = tag + b" OK done\r\n"
                    if word == b"CAPABILITY":
                        send(b"* CAPABILITY IMAP4rev1 STARTTLS\r\n" + ok)
                    elif word in (b"STARTTLS", b"LOGIN"):
                        send(ok)
                    elif word == b"LIST":
                        send(b'* LIST (\\HasNoChildren) "/" "INBOX"\r\n' + ok)
                    elif word == b"EXAMINE":
                        send(
                            b"* 1 EXISTS\r\n* OK [UIDVALIDITY 7] stable\r\n"
                            b"* OK [UIDNEXT 2] next\r\n" + ok
                        )
                    elif operation == b"SEARCH":
                        send(b"* SEARCH\r\n" + ok)
                    elif word == b"LOGOUT":
                        send(b"* BYE done\r\n" + ok)
                        return
                    else:
                        send(tag + b" BAD unsupported test command\r\n")
        except OSError:
            pass

    def create_socket(connection, timeout):
        local, remote = socket.socketpair()
        local.settimeout(timeout)
        sockets.append((local, remote))
        calls = []
        commands.append(calls)
        worker = threading.Thread(
            target=serve, args=(remote, calls, len(sockets) == 1), daemon=True
        )
        workers.append(worker)
        worker.start()
        return local

    def starttls(connection, ssl_context=None):
        return connection._simple_command("STARTTLS")

    monkeypatch.setattr(_DeadlineIMAP4, "_create_socket", create_socket)
    monkeypatch.setattr(_DeadlineIMAP4, "starttls", starttls)
    settings = Settings(
        token="offline-read-recovery-token-" + "x" * 32,
        bridge_user="reader@example.test",
        timeout=0.12,
        call_timeout=5,
    )
    client = MailClient(settings, lambda _: Credentials("reader@example.test", "dummy"))
    try:
        yield client, commands, sockets
    finally:
        stopped.set()
        for pair in sockets:
            for sock in pair:
                with suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                sock.close()
        for worker in workers:
            worker.join(timeout=1)
            assert not worker.is_alive()


@pytest.mark.parametrize("operation", ["message-id", "sender"])
@pytest.mark.parametrize("trickle", [False, True], ids=["stall", "trickle"])
def test_timed_out_lookup_drops_connection_and_next_read_uses_fresh_session(
    monkeypatch, operation, trickle
):
    with read_peer(monkeypatch, trickle=trickle) as (client, commands, sockets):

        def read():
            if operation == "message-id":
                return client.read_thread("<seed@example.test>", include_thread=False)
            return client.search_mail(from_="sender@example.test", include_snippets=False)

        started = time.monotonic()
        result = read()
        assert time.monotonic() - started < 0.6
        assert result["partial"] is True
        if operation == "message-id":
            # Optional cold-index seed discovery has its own bound. A stalled
            # lookup leaves the requested content unknown, with an explicit
            # partial reason; transport recovery still discards its socket.
            assert "lookup incomplete" in result["partial_reason"]
            assert "timed_out" not in result and "error" not in result
        else:
            assert result["timed_out"] is True
            assert "timed out" in result["error"].lower()
        assert result["messages"] == []
        assert sockets[0][0].fileno() == -1
        assert not any(command == b"LOGOUT" for command in commands[0])
        searches = [command for command in commands[0] if command.startswith(b"UID SEARCH ")]
        assert len(searches) == 1
        if operation == "message-id":
            assert b'HEADER Message-ID "seed@example.test"' in searches[0]
        else:
            # With no fresh headers, the sender filter needs an IMAP inventory
            # before its exact local header checks can run.
            assert searches[0].endswith(b" ALL")

        recovered = read()
        assert recovered["partial"] is False
        assert recovered["messages"] == []
        assert not recovered.get("timed_out", False)
        assert len(sockets) == 2
        assert all(local.fileno() == -1 for local, _ in sockets)
        assert commands[1][-1] == b"LOGOUT"


@pytest.mark.parametrize("stage", ["greeting", "CAPABILITY", "STARTTLS", "LOGIN"])
def test_setup_timeout_keeps_deadline_type_and_next_session_can_connect(monkeypatch, stage):
    with read_peer(monkeypatch, stage=stage) as (client, commands, sockets):
        started = time.monotonic()
        with pytest.raises(CallDeadlineExceeded, match="timed out"):
            with client._session():
                pytest.fail("A timed-out setup must not expose its connection")
        assert time.monotonic() - started < 0.6
        assert sockets[0][0].fileno() == -1
        assert not any(command == b"LOGOUT" for command in commands[0])
        with client._session() as (connection, _):
            assert connection.capabilities == ("IMAP4REV1", "STARTTLS")
        assert len(sockets) == 2
        assert commands[1][-1] == b"LOGOUT"
