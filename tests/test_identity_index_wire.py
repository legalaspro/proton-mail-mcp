"""CHANGEDSINCE uses the actual stdlib UID API and a local socket pair."""

import imaplib
import inspect
import re
import socket
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import replace

import pytest
from test_identity_index import Deadline
from test_identity_index import inventory as inventory
from test_identity_index_integration import (
    assert_no_mutations_or_header_search,
    indexed_preview,
)
from test_identity_index_integration import (
    indexed_mailbox as indexed_mailbox,
)

from proton_mail_mcp import identity_index, mail


@contextmanager
def changed_peer(responses):
    """Override only socket creation; all commands and parsing use imaplib."""
    local, remote = socket.socketpair()
    commands, errors = [], []

    def serve():
        try:
            remote.sendall(b"* PREAUTH ready\r\n")
            with remote.makefile("rb") as reader:
                while line := reader.readline():
                    commands.append(line)
                    tag, _, command = line.rstrip(b"\r\n").partition(b" ")
                    done = tag + b" OK done\r\n"
                    if command == b"CAPABILITY":
                        answer = b"* CAPABILITY IMAP4rev1 CONDSTORE\r\n"
                    elif command == b"EXAMINE INBOX":
                        answer = b"* 1 EXISTS\r\n* OK [UIDVALIDITY 41] stable\r\n"
                    elif command == b'STATUS "INBOX" (UIDVALIDITY UIDNEXT HIGHESTMODSEQ MESSAGES)':
                        answer = (
                            b'* STATUS "INBOX" '
                            b"(UIDVALIDITY 41 UIDNEXT 2 HIGHESTMODSEQ 2 MESSAGES 1)\r\n"
                        )
                    elif command == b"UID FETCH 1:* (UID MODSEQ) (CHANGEDSINCE 1)":
                        answer = b"".join(b"* " + row + b"\r\n" for row in responses)
                    else:
                        raise AssertionError("Unexpected local peer command")
                    remote.sendall(answer + done)
        except OSError:
            pass
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()

    class PairIMAP(imaplib.IMAP4):
        def _create_socket(self, timeout):
            local.settimeout(timeout)
            return local

    connection = None
    try:
        connection = PairIMAP(timeout=1)
        assert type(connection).uid is imaplib.IMAP4.uid
        assert connection.select("INBOX", readonly=True)[0] == "OK"
        yield connection, commands
    finally:
        if connection is not None:
            connection.shutdown()
        for sock in (local, remote):
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert not errors


def test_changed_since_real_stdlib_uid_wire_and_response():
    with changed_peer([b"1 FETCH (UID 1 MODSEQ (2))"]) as (connection, commands):
        assert identity_index._changed(connection, Deadline(), 1, 2) == {"1"}
        assert re.fullmatch(
            rb"[A-Z]+[0-9]+ UID FETCH 1:\* \(UID MODSEQ\) \(CHANGEDSINCE 1\)\r\n",
            commands[-1],
        )
        print("CHANGEDSINCE_WIRE", commands[-1].split(b" ", 1)[1].decode().rstrip())


def test_real_stdlib_old_fetch_call_form_matches_its_argument_validation():
    strict_unpacking = re.search(
        r"message_set,\s*message_parts\s*=\s*args", inspect.getsource(imaplib.IMAP4.uid)
    )
    with changed_peer([b"1 FETCH (UID 1 MODSEQ (2))"]) as (connection, commands):
        previous = list(commands)
        if strict_unpacking:
            with pytest.raises(ValueError, match="too many values to unpack"):
                connection.uid("FETCH", "1:*", "(UID MODSEQ)", "(CHANGEDSINCE 1)")
            assert commands == previous
        else:
            assert connection.uid("FETCH", "1:*", "(UID MODSEQ)", "(CHANGEDSINCE 1)") == (
                "OK",
                [b"1 (UID 1 MODSEQ (2))"],
            )
        print("STDLIB_FETCH_TWO_ARGUMENT_UNPACKING", bool(strict_unpacking))


@pytest.mark.parametrize(
    "responses",
    [
        [b"1 FETCH (MODSEQ (2) UID 1)"],
        [b"1 FETCH (UID 1 FLAGS () MODSEQ (2))"],
        [b"1 FETCH (UID 1 MODSEQ (2) FLAGS ())"],
        [b"1 FETCH (UID 1)"],
        [b"1 FETCH (MODSEQ (2))"],
        [b"1 FETCH (UID 1 MODSEQ 2)"],
        [b"1 FETCH (UID 1 MODSEQ (2 3))"],
        [b"1 FETCH (UID 1 MODSEQ (1))"],
        [b"1 FETCH (UID 1 MODSEQ (3))"],
        [b"1 FETCH (UID 0 MODSEQ (2))"],
        [b"1 FETCH (UID 1 MODSEQ (2))", b"2 FETCH (UID 1 MODSEQ (2))"],
    ],
)
def test_changed_since_uncertified_wire_response_requests_full_path(inventory, responses):
    assert inventory.run()
    path = next(inventory.path.glob("*.json"))
    original = path.read_bytes()

    def no_partial_fetch(*args, **kwargs):
        pytest.fail("An uncertified delta must not be used to fetch a partial inventory")

    with changed_peer(responses) as (connection, commands):
        assert (
            identity_index.certified_inventory(
                inventory.client,
                connection,
                inventory.deadline,
                "INBOX",
                "41",
                fetch_rows=no_partial_fetch,
            )
            is None
        )
        assert b"UID FETCH 1:* (UID MODSEQ) (CHANGEDSINCE 1)\r\n" in commands[-1]
    assert path.read_bytes() == original


def test_changed_since_stall_preserves_deadline_and_closes_stream(indexed_mailbox, monkeypatch):
    sim, client, service, _, selected = indexed_mailbox(count=1, filler=2, latency=0)
    assert indexed_preview(client, service)["token"]
    selected[0].flags.add(r"\Seen")
    monkeypatch.setattr(mail, "time", time)
    client.settings = replace(client.settings, call_timeout=0.8, timeout=5)

    def stall_delta(sid, folder, op, command):
        if op == "FETCH" and "CHANGEDSINCE" in command:
            return "stall"
        return None

    sim.hooks.append(stall_delta)
    started = time.monotonic()
    result = indexed_preview(client, service)
    assert time.monotonic() - started < client.settings.call_timeout
    assert result["token"] is None and result["partial"]
    assert "deadline" in result["reason"].lower()
    assert any("CHANGEDSINCE" in command for *_, command in sim.commands)
    assert sim.peers[-1][0].fileno() == -1
    assert_no_mutations_or_header_search(sim)
