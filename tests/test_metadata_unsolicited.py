"""Batched metadata tolerates legal unsolicited FETCH updates from real imaplib."""

import imaplib
import re
import socket
import threading
import time
from datetime import datetime, timezone

import pytest
from test_mail import ATTACHMENT, PLAIN, FakeImap, client, message
from test_mail import settings as settings

from proton_mail_mcp.mail import MailClient, _internaldate
from proton_mail_mcp.security import Credentials
from proton_mail_mcp.threading import MAX_THREAD_BODY_BYTES, MAX_THREAD_HEADER_BYTES


class _MailboxPeer:
    def __init__(self, *, search_extra=b"", fetch_extra=b""):
        self.search_extra = search_extra
        self.fetch_extra = fetch_extra

    @staticmethod
    def record(sequence, uid, query):
        raw = message(f"<{uid}@example.test>").headers
        result = f"* {sequence} FETCH (UID {uid}".encode()
        if b"INTERNALDATE" in query:
            result += b' INTERNALDATE "17-Oct-2026 09:00:00 +0000"'
        if b"FLAGS" in query:
            result += b" FLAGS (\\Seen)"
        if b"BODYSTRUCTURE" in query:
            result += b" BODYSTRUCTURE " + PLAIN
        if b"RFC822.SIZE" in query:
            result += b" RFC822.SIZE " + str(len(raw) + 5).encode()
        if b"ENVELOPE" in query:
            result += b' ENVELOPE (NIL "Hello" NIL NIL NIL NIL NIL NIL NIL NIL)'
        if b"HEADER.FIELDS" in query:
            header_range = re.search(rb"\]<0\.(\d+)>", query)
            if header_range:
                raw = raw[: int(header_range.group(1))]
            result += (
                b" BODY[HEADER.FIELDS (MESSAGE-ID REFERENCES IN-REPLY-TO "
                b"FROM TO CC SUBJECT DATE)]"
                + (b"<0>" if header_range else b"")
                + b" {"
                + str(len(raw)).encode()
                + b"}\r\n"
                + raw
            )
        return result + b")\r\n"

    def response(self, tag, command):
        word = command.split(b" ", 1)[0].upper()
        done = tag + b" OK done\r\n"
        if word == b"CAPABILITY":
            return b"* CAPABILITY IMAP4rev1 STARTTLS\r\n" + done
        if word == b"LOGIN":
            return done
        if word == b"EXAMINE":
            return b"* 2 EXISTS\r\n* OK [UIDVALIDITY 7] ok\r\n* OK [UIDNEXT 100] next\r\n" + done
        if word == b"LIST":
            return b'* LIST (\\HasNoChildren) "/" "INBOX"\r\n' + done
        if word == b"LOGOUT":
            return b"* BYE done\r\n" + done
        if word == b"UID":
            subcommand = command.split(b" ", 2)[1].upper()
            if subcommand == b"SEARCH":
                return b"* SEARCH 42 99\r\n" + self.search_extra + done
            if subcommand == b"FETCH":
                match = re.fullmatch(rb"UID FETCH (\S+) (.*)", command, re.IGNORECASE)
                uids, query = match.groups()
                assert b"BODY[" not in query and b"RFC822 " not in query
                if query.startswith(b"(BODY.PEEK[1]"):
                    return b"* 1 FETCH (BODY[1] {5}\r\nHello)\r\n" + done
                return (
                    self.fetch_extra
                    + b"".join(
                        self.record(sequence, int(uid), query)
                        for sequence, uid in enumerate(uids.split(b","), start=1)
                    )
                    # Include same-UID updates after complete records too, so
                    # neither arrival order can overwrite fetched metadata.
                    + self.fetch_extra
                    + done
                )
        raise AssertionError("Unexpected command in read-only metadata test")

    def serve(self, sock):
        try:
            with sock, sock.makefile("rb") as reader:
                sock.sendall(b"* OK [CAPABILITY IMAP4rev1 STARTTLS] ready\r\n")
                while line := reader.readline():
                    tag, _, command = line.rstrip(b"\r\n").partition(b" ")
                    sock.sendall(self.response(tag, command))
                    if command == b"LOGOUT":
                        break
        except OSError:
            pass


@pytest.fixture
def real_reader(settings):
    peers = []

    def make_reader(peer):
        class PairIMAP(imaplib.IMAP4):
            def _create_socket(self, timeout):
                client_sock, server_sock = socket.socketpair()
                client_sock.settimeout(timeout)
                worker = threading.Thread(target=peer.serve, args=(server_sock,), daemon=True)
                peers.append((server_sock, worker))
                worker.start()
                return client_sock

            def starttls(self, *, ssl_context):
                # TLS is outside this parser test; all protocol reads still use
                # the real stdlib implementation and an AF_UNIX socket pair.
                return "OK", []

        return MailClient(
            settings,
            lambda _: Credentials("reader@example.test", "dummy"),
            PairIMAP,
        )

    yield make_reader
    for sock, worker in peers:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        worker.join(timeout=1)
        assert not worker.is_alive()


@pytest.mark.parametrize("during", ["search", "fetch"])
@pytest.mark.parametrize("flags", [b"FLAGS (\\Seen)", b"UID 7 FLAGS (\\Seen)", b"UID 42 FLAGS ()"])
@pytest.mark.parametrize("operation", ["search", "thread"])
def test_unsolicited_fetch_updates_do_not_break_metadata(real_reader, during, flags, operation):
    update = b"* 7 FETCH (" + flags + b")\r\n"
    peer = _MailboxPeer(**{during + "_extra": update})
    reader = real_reader(peer)
    if operation == "search":
        result = reader.search_mail(include_snippets=False)
        assert {item["uid"] for item in result["messages"]} == {"42", "99"}
        assert result["total"] == 2 and result["total_is_exact"] is True
        # A same-UID flag update counts like the reply: the latest FLAGS win.
        assert {item["uid"]: item["seen"] for item in result["messages"]} == {
            "42": not (flags == b"UID 42 FLAGS ()" and during == "fetch"),
            "99": True,
        }
    else:
        result = reader.read_thread("<42@example.test>", include_thread=False)
        assert [item["alias"]["uid"] for item in result["messages"]] == ["42"]
        assert result["messages"][0]["text"] == "Hello"
    assert result["partial"] is False


def test_large_thread_wire_fetches_only_bounded_peek_text_and_headers(real_reader):
    body_size = MAX_THREAD_BODY_BYTES + 100
    structure = (
        b"("
        + PLAIN.replace(b"24 2", str(body_size).encode() + b" 2")
        + b" "
        + ATTACHMENT.replace(b"400", b"100000000")
        + b' "MIXED" NIL NIL NIL)'
    )

    class LargeMessagePeer(_MailboxPeer):
        def __init__(self):
            super().__init__()
            self.commands = []

        @staticmethod
        def record(sequence, uid, query):
            record = _MailboxPeer.record(sequence, uid, query).replace(PLAIN, structure)
            return re.sub(rb"RFC822\.SIZE \d+", b"RFC822.SIZE 100100000", record)

        def response(self, tag, command):
            self.commands.append(command)
            if b"BODY.PEEK[1]" in command:
                assert command == f"UID FETCH 42 (BODY.PEEK[1]<0.{MAX_THREAD_BODY_BYTES}>)".encode()
                payload = b"x" * MAX_THREAD_BODY_BYTES
                return (
                    b"* 1 FETCH (UID 42 BODY[1]<0> {"
                    + str(len(payload)).encode()
                    + b"}\r\n"
                    + payload
                    + b")\r\n"
                    + tag
                    + b" OK done\r\n"
                )
            return super().response(tag, command)

    peer = LargeMessagePeer()
    result = real_reader(peer).read_thread("<42@example.test>", include_thread=False)
    item = result["messages"][0]
    assert result["partial"] is False and item["truncated"] is True
    assert item["size_bytes"] == 100100000
    assert item["body_size_bytes"] == body_size
    assert item["body_fetched_bytes"] == MAX_THREAD_BODY_BYTES
    assert item["attachments"][0]["size"] == 100000000
    fetches = [command for command in peer.commands if command.startswith(b"UID FETCH ")]
    assert len(fetches) == 3
    assert b"BODYSTRUCTURE" not in fetches[0]
    assert f"]<0.{MAX_THREAD_HEADER_BYTES}>".encode() in fetches[0]
    assert fetches[1] == b"UID FETCH 42 (BODYSTRUCTURE)"
    assert fetches[2] == f"UID FETCH 42 (BODY.PEEK[1]<0.{MAX_THREAD_BODY_BYTES}>)".encode()
    assert not any(command.startswith((b"SELECT ", b"UID STORE ")) for command in peer.commands)


def test_stalled_thread_body_returns_known_sizes_before_caller_timeout(real_reader, monkeypatch):
    class StalledBodyPeer(_MailboxPeer):
        def response(self, tag, command):
            if b"BODY.PEEK[1]" in command:
                # Keep the socket open without replying; the real command
                # watchdog must interrupt the read, without a server sleep.
                return b""
            return super().response(tag, command)

    monkeypatch.setattr("proton_mail_mcp.threading.MAX_THREAD_SECONDS", 0.3)
    started = time.monotonic()
    result = real_reader(StalledBodyPeer()).read_thread("<42@example.test>", include_thread=False)
    assert time.monotonic() - started < 1
    assert result["partial"] is True
    assert "timed out" in result["partial_reason"]
    item = result["messages"][0]
    assert item["truncated"] is True and item["text"] == ""
    assert item["size_bytes"] > 0 and item["body_size_bytes"] == 24
    assert item["body_fetched_bytes"] == 0


@pytest.mark.parametrize("stall_canonical", [False, True])
def test_expansion_socket_cut_uses_fresh_bounded_canonical_session(
    real_reader, monkeypatch, stall_canonical
):
    from proton_mail_mcp import canonical
    from proton_mail_mcp.mail import CallDeadlineExceeded

    class StalledExpansionPeer(_MailboxPeer):
        def __init__(self):
            super().__init__()
            self.expanding = threading.Event()
            self.selection = threading.local()

        @staticmethod
        def record(sequence, uid, query):
            record = _MailboxPeer.record(sequence, uid, query)
            record = re.sub(rb"RFC822\.SIZE \d+", b"RFC822.SIZE 300", record)
            if b"ENVELOPE" in query:
                record = record[:-3] + (
                    b' ENVELOPE (NIL "subject" NIL NIL NIL NIL NIL NIL NIL '
                    b'"<42@example.test>"))\r\n'
                )
            return record

        def response(self, tag, command):
            if command.startswith(b"LIST "):
                return (
                    b'* LIST (\\All) "/" "All Mail"\r\n'
                    b'* LIST () "/" "INBOX"\r\n' + tag + b" OK done\r\n"
                )
            if command.startswith(b"EXAMINE "):
                self.selection.folder = command.split(b" ", 1)[1]
                return super().response(tag, command).replace(b"* 2 EXISTS", b"* 1 EXISTS")
            if command.startswith(b"UID SEARCH "):
                if b" OR " in command:
                    self.expanding.set()
                    # Leave the command pending until the real socket watchdog
                    # cuts the expansion stream at its reserved boundary.
                    return b""
                uid = b"42" if self.selection.folder == b'"All Mail"' else b"7"
                return b"* SEARCH " + uid + b"\r\n" + tag + b" OK done\r\n"
            return super().response(tag, command)

    peer = StalledExpansionPeer()
    reader = real_reader(peer)
    original_factory = reader.imap_factory
    connections = []

    def factory(*args, **kwargs):
        connection = original_factory(*args, **kwargs)
        connections.append(connection)
        return connection

    reader.imap_factory = factory
    original_resolve = canonical.resolve_views
    resolving, canonical_closed = threading.Event(), threading.Event()
    started = time.monotonic()

    def resolve(client, connection, deadline, handles, **kwargs):
        assert peer.expanding.is_set()
        assert len(connections) == 2 and connection.connection is connections[1]
        assert connection.sock.fileno() >= 0
        assert 0 < deadline.end - time.monotonic() <= 0.5
        assert deadline.end <= started + 2.0
        resolving.set()
        if stall_canonical:
            # Bypass command guards so only read_thread's watchdog can release
            # this read. It must close the newly registered session's socket.
            connection.sock.settimeout(None)
            assert connection.sock.recv(1) == b""
            canonical_closed.set()
            raise CallDeadlineExceeded("Canonical lookup stalled")
        return original_resolve(client, connection, deadline, handles, **kwargs)

    monkeypatch.setattr("proton_mail_mcp.threading.MAX_THREAD_SECONDS", 2.0)
    monkeypatch.setattr("proton_mail_mcp.threading.MAX_CANONICAL_SECONDS", 0.5)
    monkeypatch.setattr(canonical, "resolve_views", resolve)
    result = reader.read_thread("<42@example.test>")

    assert time.monotonic() - started < 2.75
    assert resolving.is_set()
    assert result["partial"] is True
    assert result["partial_reason"].startswith("thread expansion limited")
    assert result["timed_out"] is True and "Thread read timed out" in result["error"]
    [item] = result["messages"]
    assert item["text"] == "Hello" and item["truncated"] is False
    assert item["alias"] == {"folder": "All Mail", "uidvalidity": "7", "uid": "42"}
    if stall_canonical:
        assert canonical_closed.is_set()
        assert "Canonical lookup reached its time limit" in result["partial_reason"]
        assert (item["folder"], item["uidvalidity"], item["uid"]) == (None, None, None)
    else:
        assert (item["folder"], item["uidvalidity"], item["uid"]) == ("INBOX", "7", "7")


def test_metadata_merges_requested_items_and_drops_incomplete_uids(settings):
    raw = message("<42@example.test>").headers

    class SplitMetadataImap(FakeImap):
        def uid(self, command, *args):
            assert command == "FETCH"
            return "OK", [
                b"1 (UID 42 FLAGS (\\Seen))",
                b'2 (UID 99 INTERNALDATE "17-Oct-2026 09:00:00 +0000")',
                b'1 (UID 42 INTERNALDATE "17-Oct-2026 09:00:00 +0000")',
                b"1 (UID 42 BODYSTRUCTURE " + PLAIN + b")",
                (
                    b"1 (BODY[HEADER.FIELDS (MESSAGE-ID)] {" + str(len(raw)).encode() + b"}",
                    raw,
                ),
                b" UID 42)",
                b"1 (UID 42 FLAGS ())",
            ]

    reader = client(settings, SplitMetadataImap())
    result = reader._metadata(SplitMetadataImap(), ["42", "99"], deadline=reader._deadline())
    assert set(result) == {"42"}
    assert str(result["42"]["headers"]["Message-ID"]) == "<42@example.test>"
    assert isinstance(result["42"]["structure"], list)
    assert result["42"]["internaldate"] > 0


@pytest.mark.parametrize(
    "date",
    [
        "not a date",
        "17-Oct-2026 09:00:00",
        "32-Oct-2026 09:00:00 +0000",
        "17-Xxx-2026 09:00:00 +0000",
    ],
)
def test_invalid_internaldate_uses_partial_result_instead_of_failing(settings, date):
    fake = FakeImap(
        {
            "INBOX": {
                "42": message("<valid@example.test>"),
                "99": message("<invalid@example.test>", internaldate=date),
            }
        }
    )
    reader = client(settings, fake)
    result = reader.search_mail(include_snippets=False)
    assert [item["uid"] for item in result["messages"]] == ["42"]
    assert result["partial"] is True and result["total_is_exact"] is False
    assert result["next_cursor"]

    # Permanently invalid dates are skipped within this cursor, so the next
    # page exhausts the snapshot without returning 42 or fetching 99 again.
    calls = len(fake.calls)
    exhausted = reader.search_mail(include_snippets=False, cursor=result["next_cursor"])
    assert exhausted["messages"] == []
    assert exhausted["next_cursor"] is None and exhausted["partial"] is False
    assert not any(call[0] == "FETCH" for call in fake.calls[calls:])


def test_internaldate_parsing_respects_offsets_and_space_padded_days():
    expected = datetime(2026, 10, 7, 10, tzinfo=timezone.utc).timestamp()
    assert _internaldate(b" 7-Oct-2026 12:00:00 +0200") == expected
    assert _internaldate(b"07-Oct-2026 03:00:00 -0700") == expected
