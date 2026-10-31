"""P-C search pack through the mail client: seen, unread_only, exclusions, queries.

Every UID SEARCH goes to tests/gluon_search.py, a port of gluon's SEARCH grammar
and matching, so each command must be one Bridge accepts and must select exactly
the intended messages. Peers are offline fakes or socketpairs; no Bridge,
network or credentials are involved.
"""

import base64
import imaplib
import json
import random
import re
import socket
import threading
from dataclasses import replace
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest
from bridge_sim import BridgeSim, Msg, hdr, make_client
from gluon_search import GluonSearchImap, Mail, evaluate, search
from test_mail import PLAIN, FakeMessage, client, message
from test_mail import settings as settings
from test_search_query import HOSTILE, SECRET, deep

from proton_mail_mcp import search_query
from proton_mail_mcp.mail import CallDeadlineExceeded, MailClient, MailValidationError
from proton_mail_mcp.search import _query_key
from proton_mail_mcp.security import Credentials

SEEN = ("\\Seen",)
TOOLS = ["search_mail", "count_mail"]


def letter(
    identifier,
    *,
    sender="Sender <sender@example.com>",
    to="reader@example.com",
    subject="Hello",
    body=b"Hello plain text\r\n",
    flags=(),
    day="14 Oct 2026",
):
    headers = EmailMessage(policy=policy.SMTP)
    headers["Message-ID"] = identifier
    headers["From"] = sender
    headers["To"] = to
    headers["Subject"] = subject
    headers["Date"] = f"Wed, {day} 10:00:00 +0000"
    item = FakeMessage(headers.as_bytes(), PLAIN, {"1": body})
    item.flags = set(flags)
    return item


def uids(result):
    return [item["uid"] for item in result["messages"]]


def fetches(fake):
    return [call for call in fake.calls if call[0] == "FETCH"]


# Parser tests cover every control character; one control exercises each API path.
@pytest.mark.parametrize("place", ["exclude_subject", "exclude_text", "query"])
@pytest.mark.parametrize("tool", TOOLS)
def test_controls_are_refused_before_connecting(settings, place, tool):
    value = "a\nb"
    if place == "query":
        arguments = {"query": {"or": [{"from": "x"}, {"not": {"subject": value}}]}}
    else:
        arguments = {place: value}
    fake = GluonSearchImap()
    with pytest.raises(MailValidationError, match="no control characters"):
        getattr(client(settings, fake), tool)(**arguments)
    assert fake.calls == []


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    "arguments,expected",
    [
        ({"query": [{"from": SECRET}]}, "nonempty object"),
        ({"query": {"cc": SECRET}}, "Unsupported query key"),
        ({"query": deep(6)}, "limited to 5 levels"),
        ({"query": {"or": [{"to": SECRET}] * 21}}, "list of 1 to 20 queries"),
        ({"exclude_subject": ""}, "exclude_subject must be nonempty text"),
        ({"exclude_text": 7}, "exclude_text must be nonempty text"),
        ({"exclude_text": "y" * 1001}, "exclude_text must be at most 1000"),
        ({"unread_only": "true"}, "unread_only must be a boolean"),
        ({"unread_only": 1}, "unread_only must be a boolean"),
    ],
)
def test_invalid_options_are_refused_before_connecting(settings, tool, arguments, expected):
    fake = GluonSearchImap()
    with pytest.raises(MailValidationError, match=expected) as raised:
        getattr(client(settings, fake), tool)(**arguments)
    assert SECRET not in str(raised.value)
    assert fake.calls == []


# --- search_mail ----------------------------------------------------------------


def test_seen_comes_from_flags_fetched_with_internaldate_in_one_batch(settings):
    fake = GluonSearchImap(
        {
            "INBOX": {
                "1": message("<1@x.test>"),
                "2": message("<2@x.test>", flags=SEEN),
                "3": message("<3@x.test>", flags=("\\SEEN", "\\Flagged")),
                "4": message("<4@x.test>", flags=("\\Answered", "$Seen", "Seen")),
            }
        }
    )
    result = client(settings, fake).search_mail(limit=10)
    assert {item["uid"]: item["seen"] for item in result["messages"]} == {
        "1": False,
        "2": True,
        "3": True,
        "4": False,
    }
    with_flags = [call for call in fetches(fake) if "FLAGS" in call[2]]
    assert len(with_flags) == 1 and with_flags[0][2] == "(UID INTERNALDATE FLAGS)"
    assert set(with_flags[0][1].split(",")) == {"1", "2", "3", "4"}
    # Page headers, structures and snippets are fetched as before.
    assert all("FLAGS" not in call[2] for call in fetches(fake) if call not in with_flags)
    assert len(fetches(fake)) == 1 + 1 + 4


def test_unread_only_searches_unseen_and_returns_only_unread_mail(settings):
    fake = GluonSearchImap(
        {
            "INBOX": {
                "1": letter("<1@x.test>", day="12 Oct 2026"),
                "2": letter("<2@x.test>", day="13 Oct 2026", flags=SEEN),
                "3": letter("<3@x.test>", day="14 Oct 2026"),
            }
        }
    )
    result = client(settings, fake).search_mail(
        unread_only=True, since="2026-10-01", include_snippets=False
    )
    assert uids(result) == ["3", "1"]
    assert [item["seen"] for item in result["messages"]] == [False, False]
    assert result["total"] == 2 and result["total_is_exact"] is True
    assert fake.searches == [b"SEARCH CHARSET UTF-8 SINCE 01-Oct-2026 UNSEEN"]


def test_unread_only_drops_messages_read_between_search_and_fetch(settings):
    class ReadDuringSearch(GluonSearchImap):
        def uid(self, command, *args):
            response = super().uid(command, *args)
            if command == "SEARCH":
                self.folders["INBOX"]["2"].flags = set(SEEN)
            return response

    fake = ReadDuringSearch({"INBOX": {"1": letter("<1@x.test>"), "2": letter("<2@x.test>")}})
    result = client(settings, fake).search_mail(unread_only=True, include_snippets=False)
    assert uids(result) == ["1"] and result["total"] == 1


@pytest.mark.parametrize("value", HOSTILE)
def test_hostile_values_select_only_messages_containing_them(settings, value):
    fake = GluonSearchImap(
        {
            "INBOX": {
                "1": letter("<hit@x.test>", subject="pre " + value + " post"),
                "2": letter("<miss@x.test>", subject="harmless"),
            }
        }
    )
    reader = client(settings, fake)
    assert uids(reader.search_mail(query={"subject": value}, include_snippets=False)) == ["1"]
    assert uids(reader.search_mail(exclude_subject=value, include_snippets=False)) == ["2"]
    assert uids(reader.search_mail(exclude_text=value, include_snippets=False)) == ["2"]
    assert fake.rejected == []


def test_exclude_subject_matches_decoded_subjects_locally(settings):
    fake = GluonSearchImap(
        {
            "INBOX": {
                "1": letter("<1@x.test>", subject="Café weekly digest"),
                "2": letter("<2@x.test>", subject="Invoice"),
            }
        }
    )
    assert b"=?utf-8?" in fake.folders["INBOX"]["1"].headers.lower()
    result = client(settings, fake).search_mail(
        exclude_subject="CAFÉ WEEKLY", include_snippets=False
    )
    assert uids(result) == ["2"]
    # Header conditions never become server keys in search_mail.
    assert fake.searches == [b"SEARCH CHARSET UTF-8 ALL"]


def test_exclude_text_is_a_not_text_key_of_the_same_search(settings):
    fake = GluonSearchImap(
        {
            "INBOX": {
                "1": letter("<1@x.test>", body=b"Friday: please unsubscribe here\r\n"),
                "2": letter("<2@x.test>", body=b"Lunch on Friday?\r\n"),
                "3": letter("<3@x.test>", body=b"Nothing\r\n"),
            }
        }
    )
    result = client(settings, fake).search_mail(
        text="friday", exclude_text="Unsubscribe", include_snippets=False
    )
    assert uids(result) == ["2"]
    assert fake.searches == [b'SEARCH CHARSET UTF-8 TEXT "friday" NOT TEXT "Unsubscribe"']


def test_query_header_conditions_match_locally_after_the_date_search(settings):
    fake = GluonSearchImap(
        {
            "INBOX": {
                "1": letter("<1@x.test>", sender="Alice <alice@example.com>", subject="Weekly"),
                "2": letter("<2@x.test>", sender="Bob <bob@example.com>", subject="Weekly"),
                "3": letter("<3@x.test>", sender="Carol <carol@example.com>", subject="Invoice"),
            }
        }
    )
    query = {"or": [{"from": "ALICE"}, {"not": {"subject": "weekly"}}]}
    result = client(settings, fake).search_mail(
        query=query, since="2026-10-01", include_snippets=False
    )
    assert uids(result) == ["3", "1"]
    assert fake.searches == [b"SEARCH CHARSET UTF-8 SINCE 01-Oct-2026"]


def test_text_beside_header_conditions_under_or_needs_one_more_text_search(settings):
    fake = GluonSearchImap(
        {
            "INBOX": {
                "1": letter("<1@x.test>", subject="Invoice 7"),
                "2": letter("<2@x.test>", body=b"Your receipt\r\n"),
                "3": letter("<3@x.test>"),
            }
        }
    )
    query = {"or": [{"subject": "invoice"}, {"text": "receipt"}]}
    result = client(settings, fake).search_mail(
        query=query, since="2026-10-01", include_snippets=False
    )
    assert sorted(uids(result)) == ["1", "2"]
    assert fake.searches == [
        b"SEARCH CHARSET UTF-8 SINCE 01-Oct-2026",
        b'SEARCH CHARSET UTF-8 SINCE 01-Oct-2026 TEXT "receipt"',
    ]


def test_second_non_ascii_text_value_is_searched_on_its_own(settings):
    fake = GluonSearchImap(
        {
            "INBOX": {
                "1": letter("<1@x.test>", body="Crème brûlée\r\n".encode()),
                "2": letter("<2@x.test>", body="Crème caramel\r\n".encode()),
                "3": letter("<3@x.test>", body=b"Plain\r\n"),
            }
        }
    )
    result = client(settings, fake).search_mail(
        text="Crème", exclude_text="Brûlée", include_snippets=False
    )
    assert uids(result) == ["2"]
    creme, brulee = "Crème".encode(), "Brûlée".encode()
    assert fake.searches == [
        b"SEARCH CHARSET UTF-8 TEXT {%d}\r\n%s" % (len(creme), creme),
        b"SEARCH CHARSET UTF-8 TEXT {%d}\r\n%s" % (len(brulee), brulee),
    ]
    assert fake.search_literals == [creme, brulee]


def cursor_payload(cursor):
    return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))


def test_cursor_binds_new_options_and_earlier_cursors_keep_paging(settings):
    mailbox = {
        str(uid): letter(f"<{uid}@x.test>", day=f"{10 + uid} Oct 2026", flags=SEEN * (uid % 2))
        for uid in range(1, 7)
    }
    fake = GluonSearchImap({"INBOX": mailbox})
    reader = client(settings, fake)
    plain = reader.search_mail(limit=2, include_snippets=False)
    # Without new options the key is the one cursors had before them.
    assert cursor_payload(plain["next_cursor"])["q"] == _query_key(["INBOX", *[None] * 6])
    second = reader.search_mail(limit=2, cursor=plain["next_cursor"], include_snippets=False)
    assert uids(second) == ["4", "3"]
    unread = reader.search_mail(limit=1, unread_only=True, include_snippets=False)
    assert uids(unread) == ["6"]
    following = reader.search_mail(
        limit=1, unread_only=True, cursor=unread["next_cursor"], include_snippets=False
    )
    assert uids(following) == ["4"]
    for options in ({}, {"exclude_subject": "x"}, {"query": {"from": "sender"}}):
        with pytest.raises(MailValidationError, match="Invalid search cursor"):
            reader.search_mail(cursor=unread["next_cursor"], **options)
    first = reader.search_mail(limit=1, query={"subject": "Hello", "from": "sender"})
    again = reader.search_mail(
        limit=1, query={"from": "sender", "subject": "Hello"}, cursor=first["next_cursor"]
    )
    assert uids(again) == ["5"]
    for query in ({"from": "sender"}, {"not": {"subject": "Hello"}}):
        with pytest.raises(MailValidationError, match="Invalid search cursor"):
            reader.search_mail(query=query, cursor=first["next_cursor"])


def test_searches_without_new_options_keep_their_command_and_fields(settings):
    fake = GluonSearchImap(
        {
            "INBOX": {
                "1": letter("<1@x.test>", body=b"plain body\r\n"),
                "2": letter("<2@x.test>", body=b"other\r\n"),
            }
        }
    )
    result = client(settings, fake).search_mail(
        subject="Hello", since="2026-10-01", before="2026-11-01", text="plain"
    )
    assert uids(result) == ["1"]
    assert fake.searches == [
        b'SEARCH CHARSET UTF-8 SINCE 01-Oct-2026 BEFORE 01-Nov-2026 TEXT "plain"'
    ]
    assert set(result["messages"][0]) == {
        "message_id",
        "folder",
        "uidvalidity",
        "uid",
        "from",
        "to",
        "subject",
        "date",
        "seen",
        "snippet",
        "snippet_omitted",
    }
    assert set(result) == {
        "messages",
        "next_cursor",
        "total",
        "partial",
        "total_is_exact",
    }


@pytest.fixture
def bridge(tmp_path):
    sim = BridgeSim(strict_search=True)
    yield sim, make_client(sim, tmp_path)
    sim.close()
    assert not sim.errors and not sim.violations and not sim.search_rejections


def seed_views(sim):
    for index in range(3):
        sim.add(Msg(hdr(f"<archived-{index}@example.invalid>", subject="Old"), "Archive"))
    target = sim.add(Msg(hdr("<target@example.invalid>", subject="Release notes")))
    sim.add(Msg(hdr("<read@example.invalid>", subject="Release notes"), flags={"\\Seen"}))
    sim.add(Msg(hdr("<digest@example.invalid>", subject="Weekly digest")))
    return target


def test_view_searches_keep_canonical_handles_with_unread_only_and_exclusions(bridge):
    sim, reader = bridge
    target = seed_views(sim)
    result = reader.search_mail(
        folder="All Mail",
        since="2026-10-01",
        unread_only=True,
        exclude_subject="weekly",
        include_snippets=False,
    )
    [hit] = [item for item in result["messages"] if item["subject"] == "Release notes"]
    assert hit["seen"] is False and len(result["messages"]) == 4
    assert (hit["folder"], hit["uid"]) == ("INBOX", str(sim.uid_of("INBOX", target.pid)))
    assert hit["alias"]["folder"] == "All Mail"
    assert hit["alias"]["uid"] == str(sim.uid_of("All Mail", target.pid))
    assert {item["folder"] for item in result["messages"]} == {"INBOX", "Archive"}
    assert result["partial"] is False
    searches = [command for *_, command in sim.commands if command.startswith("UID SEARCH")]
    assert searches[0] == "UID SEARCH CHARSET UTF-8 SINCE 01-Oct-2026 UNSEEN"


class WirePeer:
    """Records each command's octets, literals included; answers from gluon_search."""

    def __init__(self, mailbox):
        self.mailbox = mailbox
        self.commands = []
        self.rejected = []

    def record(self, sequence, uid, query):
        item = self.mailbox[uid]
        out = f"* {sequence} FETCH (UID {uid}".encode()
        if b"INTERNALDATE" in query:
            out += b' INTERNALDATE "14-Oct-2026 10:00:00 +0000"'
        if b"FLAGS" in query:
            out += b" FLAGS (" + " ".join(sorted(item.flags)).encode() + b")"
        if b"ENVELOPE" in query:
            from bridge_costs import envelope_from_headers

            out += b" ENVELOPE " + envelope_from_headers(item.headers)
        if b"RFC822.SIZE" in query:
            out += (
                b" RFC822.SIZE "
                + str(len(item.headers) + sum(map(len, item.sections.values()))).encode()
            )
        if b"HEADER.FIELDS" in query:
            out += (
                b" BODY[HEADER.FIELDS (MESSAGE-ID REFERENCES IN-REPLY-TO FROM SENDER REPLY-TO "
                b"TO CC SUBJECT DATE)] {" + str(len(item.headers)).encode() + b"}\r\n"
            )
            out += item.headers
        return out + b")\r\n"

    def response(self, tag, command):
        word = command.split(b" ", 1)[0].upper()
        done = tag + b" OK done\r\n"
        if word == b"CAPABILITY":
            return b"* CAPABILITY IMAP4rev1 STARTTLS\r\n" + done
        if word == b"LOGIN":
            return done
        if word == b"LIST":
            return b'* LIST (\\HasNoChildren) "/" "INBOX"\r\n' + done
        if word == b"EXAMINE":
            return f"* {len(self.mailbox)} EXISTS\r\n* OK [UIDVALIDITY 7] ok\r\n".encode() + done
        if word == b"LOGOUT":
            return b"* BYE done\r\n" + done
        if command.upper().startswith(b"UID SEARCH "):
            try:
                found = search(command[4:], self.mailbox)
            except ValueError as error:
                self.rejected.append((command, str(error)))
                return tag + b" BAD invalid SEARCH\r\n"
            return b"* SEARCH " + " ".join(found).encode() + b"\r\n" + done
        match = re.fullmatch(rb"UID FETCH (\S+) (.*)", command, re.IGNORECASE)
        if match:
            requested, query = match.groups()
            records = b"".join(
                self.record(sequence, uid.decode(), query)
                for sequence, uid in enumerate(requested.split(b","), start=1)
            )
            return records + done
        self.rejected.append((command, "unexpected"))
        return tag + b" BAD unexpected\r\n"

    def serve(self, sock):
        try:
            with sock, sock.makefile("rb") as reader:
                sock.sendall(b"* OK [CAPABILITY IMAP4rev1 STARTTLS] ready\r\n")
                while line := reader.readline():
                    while match := re.search(rb"\{(\d+)\}\r\n$", line):
                        sock.sendall(b"+ go ahead\r\n")
                        line += reader.read(int(match.group(1)))
                        line += reader.readline()
                    tag, _, command = line[:-2].partition(b" ")
                    self.commands.append(command)
                    sock.sendall(self.response(tag, command))
                    if command.upper() == b"LOGOUT":
                        break
        except OSError:
            pass


@pytest.fixture
def wire(settings):
    workers = []

    def build(peer):
        class PairIMAP(imaplib.IMAP4):
            def _create_socket(self, timeout):
                local, remote = socket.socketpair()
                local.settimeout(timeout)
                worker = threading.Thread(target=peer.serve, args=(remote,), daemon=True)
                workers.append((remote, worker))
                worker.start()
                return local

            def starttls(self, *, ssl_context):
                # TLS is outside these protocol tests; every read is real imaplib.
                return "OK", []

        return MailClient(settings, lambda _: Credentials("reader@example.test", "dummy"), PairIMAP)

    yield build
    for remote, worker in workers:
        try:
            remote.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        worker.join(timeout=1)
        assert not worker.is_alive()


def test_search_mail_reports_seen_and_renders_queries_over_real_imaplib(wire):
    peer = WirePeer(
        {
            "1": letter("<1@x.test>", subject="Invoice", flags=SEEN),
            "2": letter("<2@x.test>", subject="Weekly", body=b"See the agenda\r\n"),
            "3": letter("<3@x.test>", subject="Other"),
        }
    )
    query = {"or": [{"subject": "invoice"}, {"text": "agenda", "to": "reader"}]}
    result = wire(peer).search_mail(query=query, include_snippets=False)
    assert {item["uid"]: item["seen"] for item in result["messages"]} == {"1": True, "2": False}
    searches = [command for command in peer.commands if command.startswith(b"UID SEARCH")]
    assert searches == [b"UID SEARCH CHARSET UTF-8 ALL", b'UID SEARCH CHARSET UTF-8 TEXT "agenda"']
    assert peer.rejected == []


# --- count_mail -------------------------------------------------------------------


def test_count_mail_sends_one_uid_search_and_fetches_nothing(settings):
    fake = GluonSearchImap(
        {
            "INBOX": {
                "1": letter("<1@x.test>", sender="alice@example.com", to="team@example.com"),
                "2": letter("<2@x.test>", sender="alice@example.com", body=b"draft\r\n"),
                "3": letter("<3@x.test>", sender="alice@example.com", subject="Weekly"),
                "4": letter("<4@x.test>", sender="alice@example.com", flags=SEEN),
                "5": letter("<5@x.test>", sender="bob@example.com"),
                "6": letter("<6@x.test>", sender="alice@example.com", day="30 Sep 2026"),
            }
        }
    )
    result = client(settings, fake).count_mail(
        from_="alice",
        since="2026-10-01",
        unread_only=True,
        exclude_subject="weekly",
        query={"or": [{"to": "team"}, {"not": {"text": "draft"}}]},
    )
    assert result == {"folder": "INBOX", "count": 1}
    assert fake.searches == [
        b'SEARCH CHARSET UTF-8 SINCE 01-Oct-2026 UNSEEN FROM "alice" NOT SUBJECT "weekly" '
        b'OR TO "team" NOT TEXT "draft"'
    ]
    commands = [call[0] for call in fake.calls]
    assert [name for name in commands if name in {"LIST", "EXAMINE", "SEARCH", "FETCH"}] == [
        "LIST",
        "EXAMINE",
        "SEARCH",
    ]


def test_count_mail_reads_exists_for_an_empty_folder_or_no_conditions(settings):
    fake = GluonSearchImap(
        {"INBOX": {}, "Archive": {"1": letter("<1@x.test>"), "2": letter("<2@x.test>")}}
    )
    reader = client(settings, fake)
    assert reader.count_mail(from_="x", unread_only=True) == {"folder": "INBOX", "count": 0}
    assert reader.count_mail(folder="Archive") == {"folder": "Archive", "count": 2}
    assert not any(call[0] in {"SEARCH", "FETCH"} for call in fake.calls)
    assert reader.count_mail(folder="Archive", since="2026-10-15")["count"] == 0
    assert fake.searches == [b"SEARCH CHARSET UTF-8 SINCE 15-Oct-2026"]


@pytest.mark.parametrize("value", HOSTILE)
def test_count_mail_hostile_values_count_only_messages_containing_them(settings, value):
    fake = GluonSearchImap(
        {
            "INBOX": {
                "1": letter("<hit@x.test>", subject="pre " + value + " post"),
                "2": letter("<miss@x.test>", subject="harmless"),
            }
        }
    )
    reader = client(settings, fake)
    assert reader.count_mail(subject=value) == {"folder": "INBOX", "count": 1}
    assert reader.count_mail(query={"not": {"subject": value}})["count"] == 1
    # TEXT covers the raw message, its headers included.
    assert reader.count_mail(exclude_text=value)["count"] == 1
    assert fake.rejected == [] and fetches(fake) == []


def test_count_mail_compares_raw_headers_unlike_search_mail(settings):
    fake = GluonSearchImap(
        {
            "INBOX": {
                "1": letter("<1@x.test>", subject="Café weekly digest"),
                "2": letter("<2@x.test>", subject="Invoice"),
            }
        }
    )
    reader = client(settings, fake)
    # Bridge compares the raw header "=?utf-8?q?Caf=C3=A9?= weekly digest".
    assert reader.count_mail(subject="Café")["count"] == 0
    assert reader.count_mail(subject="WEEKLY digest")["count"] == 1
    assert reader.count_mail(exclude_subject="CAFÉ WEEKLY")["count"] == 2
    # search_mail matches the decoded header.
    assert reader.search_mail(subject="Café", include_snippets=False)["total"] == 1
    excluded = reader.search_mail(exclude_subject="CAFÉ WEEKLY", include_snippets=False)
    assert uids(excluded) == ["2"]


def test_count_mail_counts_further_non_ascii_values_with_searches_alone(settings):
    fake = GluonSearchImap(
        {
            "INBOX": {
                "1": letter("<1@x.test>", body="Crème\r\n".encode()),
                "2": letter("<2@x.test>", body="Brûlée\r\n".encode()),
                "3": letter("<3@x.test>", body="Crème brûlée\r\n".encode(), flags=SEEN),
                "4": letter("<4@x.test>"),
            }
        }
    )
    query = {"or": [{"text": "Crème"}, {"text": "Brûlée"}]}
    result = client(settings, fake).count_mail(query=query, unread_only=True)
    assert result == {"folder": "INBOX", "count": 2}
    creme, brulee = "Crème".encode(), "Brûlée".encode()
    assert fake.searches == [
        b"SEARCH CHARSET UTF-8 UNSEEN",
        b"SEARCH CHARSET UTF-8 TEXT {%d}\r\n%s" % (len(brulee), brulee),
        b"SEARCH CHARSET UTF-8 TEXT {%d}\r\n%s" % (len(creme), creme),
    ]
    assert fetches(fake) == [] and fake.rejected == []


def test_count_mail_uses_list_names_and_no_all_mail_window(settings, monkeypatch):
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-10-20")
    mailbox = {"1": letter("<1@x.test>"), "2": letter("<2@x.test>", flags=SEEN)}
    fake = GluonSearchImap({"INBOX": dict(mailbox), "All Mail": dict(mailbox)})
    reader = client(settings, fake)
    assert reader.count_mail(folder="inbox", unread_only=True) == {"folder": "INBOX", "count": 1}
    assert reader.count_mail(folder="All Mail", subject="hello")["count"] == 2
    assert fake.searches == [
        b"SEARCH CHARSET UTF-8 UNSEEN",
        b'SEARCH CHARSET UTF-8 SUBJECT "hello"',
    ]
    # search_mail keeps its default window for the same All Mail view.
    reader.search_mail(folder="All Mail", subject="hello", include_snippets=False)
    assert fake.searches[-1] == b"SEARCH CHARSET UTF-8 SINCE 20-Oct-2026"


def test_count_mail_counts_view_messages_once(bridge):
    sim, reader = bridge
    seed_views(sim)
    counted = reader.count_mail(folder="All Mail", unread_only=True, exclude_subject="weekly")
    assert counted == {"folder": "All Mail", "count": 4}
    assert reader.count_mail(folder="INBOX", unread_only=True) == {"folder": "INBOX", "count": 2}
    assert not any(" FETCH " in command for *_, command in sim.commands)


def test_count_mail_deadline_during_search_returns_timeout(settings, monkeypatch):
    fake = GluonSearchImap({"INBOX": {"1": letter("<1@x.test>")}})
    reader = client(settings, fake)

    def stalled(*args, **kwargs):
        raise CallDeadlineExceeded("Mailbox call timed out")

    monkeypatch.setattr(reader, "_search", stalled)
    result = reader.count_mail(text="anything")
    assert result == {
        "folder": "INBOX",
        "count": 0,
        "partial": True,
        "partial_reason": "timed_out",
        "timed_out": True,
        "error": "Count timed out; narrow the date range or filters and retry",
    }


@pytest.mark.parametrize("general,socket_timeout", [(120, 60), (7, 3)])
def test_count_mail_bounds_budget_before_connection_setup(
    settings, monkeypatch, general, socket_timeout
):
    reader = client(
        replace(settings, call_timeout=general, timeout=socket_timeout), GluonSearchImap()
    )
    monkeypatch.setattr("proton_mail_mcp.search.time.monotonic", lambda: 100.0)
    captured = []

    def stopped_session(*, deadline):
        captured.append(deadline)
        raise CallDeadlineExceeded("private connection failure")

    monkeypatch.setattr(reader, "_session", stopped_session)
    result = reader.count_mail(from_="sender")

    assert len(captured) == 1
    deadline = captured[0]
    assert deadline.end == 100.0 + min(general, 20.0)
    assert deadline.socket_timeout == min(socket_timeout, 10.0)
    assert deadline.response_reserve <= 2.0
    assert result["timed_out"] and result["partial"] and result["count"] == 0
    assert "private" not in result["error"]


@pytest.mark.parametrize("stage", ["starttls", "login", "search", "second_search", "late_search"])
def test_count_mail_timeout_discards_connection_and_next_call_succeeds(
    settings, monkeypatch, stage
):
    fake = GluonSearchImap({"INBOX": {"1": letter("<1@x.test>", sender="Café <sender@x.test>")}})
    healthy = GluonSearchImap({"INBOX": {"1": letter("<2@x.test>")}})
    reader = client(settings, fake)
    connections = iter([fake, healthy])
    reader.imap_factory = lambda *args, **kwargs: next(connections)
    monkeypatch.setattr(fake, "shutdown", lambda: fake.calls.append(("SHUTDOWN",)), raising=False)
    clock = [100.0]
    monkeypatch.setattr("proton_mail_mcp.search.time.monotonic", lambda: clock[0])

    def stalled(*args, **kwargs):
        raise TimeoutError("private count peer failure")

    options = {"from_": "sender"}
    if stage in {"starttls", "login"}:
        monkeypatch.setattr(fake, stage, stalled)
    else:
        original_uid = fake.uid
        searches = 0

        def timed_uid(command, *args):
            nonlocal searches
            response = original_uid(command, *args)
            if command == "SEARCH":
                searches += 1
                target = 2 if stage == "second_search" else 1
                if searches == target:
                    if stage == "late_search":
                        clock[0] += 19.0
                    else:
                        stalled()
            return response

        monkeypatch.setattr(fake, "uid", timed_uid)
        if stage == "second_search":
            options = {"from_": "Café", "to": "Ünï"}

    result = reader.count_mail(**options)

    assert result["timed_out"] is True and result["partial"] is True
    assert result["count"] == 0 and "Count timed out" in result["error"]
    assert "private" not in result["error"]
    assert ("SHUTDOWN",) in fake.calls
    assert not any(call[0] == "LOGOUT" for call in fake.calls)
    assert reader.count_mail(from_="sender") == {"folder": "INBOX", "count": 1}
    assert ("LOGOUT",) in healthy.calls


def test_count_mail_sends_a_non_ascii_value_as_one_literal_over_real_imaplib(wire):
    peer = WirePeer(
        {
            "1": letter("<1@x.test>", body="Crème brûlée\r\n".encode()),
            "2": letter("<2@x.test>", body=b"Plain\r\n"),
            "3": letter("<3@x.test>", body="crème\r\n".encode(), flags=SEEN),
        }
    )
    result = wire(peer).count_mail(text="Crème", unread_only=True, exclude_subject='a")(b')
    assert result == {"folder": "INBOX", "count": 1}
    searches = [command for command in peer.commands if command.startswith(b"UID SEARCH")]
    assert searches == [
        b'UID SEARCH CHARSET UTF-8 UNSEEN NOT SUBJECT "a\\")(b" TEXT {6}\r\n' + "Crème".encode()
    ]
    assert not any(command.startswith(b"UID FETCH") for command in peer.commands)
    assert peer.rejected == []


# Every rendered query selects what it means.

VALUES = {
    "from": ["alice", "bob@", "example", "Zoë"],
    "to": ["reader", "team", "Ünï"],
    "subject": ["invoice", "weekly", "Café", "re:"],
    "text": ["receipt", "unsubscribe", "Crème", "hello"],
}


def corpus():
    people = [
        ("Alice <alice@example.com>", "reader@example.com"),
        ("Bob <bob@example.org>", "team@example.com"),
        ("Zoë Zed <zoe@example.net>", "Ünïcode <uni@example.com>"),
        ("news@example.invalid", "reader@example.com, team@example.com"),
    ]
    subjects = ["Invoice 42", "Weekly digest", "Café weekly", "Re: hello"]
    bodies = [b"Your receipt\r\n", b"Click to unsubscribe\r\n", "Crème brûlée\r\n".encode(), b"hi"]
    mailbox = {}
    for index in range(12):
        sender, to = people[index % 4]
        mailbox[str(index + 1)] = letter(
            f"<m{index}@example.test>",
            sender=sender,
            to=to,
            subject=subjects[(index // 4 + index) % 4],
            body=bodies[(index * 3) % 4],
            flags=SEEN if index % 3 == 0 else (),
        )
    return mailbox


def random_query(rng, level=1, budget=None):
    budget = budget if budget is not None else [rng.randint(1, 8)]
    if level >= 5 or budget[0] <= 1 or rng.random() < 0.3:
        budget[0] -= 1
        field = rng.choice(sorted(VALUES))
        return {field: rng.choice(VALUES[field])}
    kind = rng.choice(["and", "or", "not", "object"])
    if kind == "not":
        return {"not": random_query(rng, level + 1, budget)}
    if kind == "object":
        budget[0] -= 2
        return {field: rng.choice(VALUES[field]) for field in rng.sample(sorted(VALUES), 2)}
    return {kind: [random_query(rng, level + 1, budget) for _ in range(rng.randint(1, 3))]}


def gluon_matches(tree, uid, item):
    mail = Mail.from_fake(uid, 1, item)

    def test(field, value):
        return evaluate((search_query.FIELDS[field], value), mail)

    return search_query.matches(tree, test)


def local_matches(tree, uid, item):
    headers = BytesParser(policy=policy.default).parsebytes(item.headers)
    mail = Mail.from_fake(uid, 1, item)

    def test(field, value):
        if field == "text":
            return evaluate(("TEXT", value), mail)
        return value.casefold() in str(headers.get(field.title(), "")).casefold()

    return search_query.matches(tree, test)


def test_random_not_or_queries_select_exactly_their_messages(settings):
    """count_mail follows gluon's matching for every key; search_mail matches
    header conditions on decoded headers and text on the server."""
    rng = random.Random(20261025)
    mailbox = corpus()
    fake = GluonSearchImap({"INBOX": mailbox})
    reader = client(settings, fake)
    differs = 0
    for _ in range(150):
        query = random_query(rng)
        tree = search_query.parse(query)
        unread_only = rng.random() < 0.3
        eligible = {
            uid: item
            for uid, item in mailbox.items()
            if not (unread_only and "\\Seen" in item.flags)
        }
        server = {uid for uid, item in eligible.items() if gluon_matches(tree, uid, item)}
        local = {uid for uid, item in eligible.items() if local_matches(tree, uid, item)}
        differs += server != local
        fake.calls.clear()
        counted = reader.count_mail(query=query, unread_only=unread_only)
        assert counted == {"folder": "INBOX", "count": len(server)}, query
        assert fetches(fake) == []
        found = reader.search_mail(
            query=query, unread_only=unread_only, limit=100, include_snippets=False
        )
        assert set(uids(found)) == local and found["total"] == len(local), query
        assert all(item["seen"] is False for item in found["messages"] if unread_only)
    assert fake.rejected == []
    # Encoded non-ASCII headers make the two matchings differ for some queries.
    assert differs
