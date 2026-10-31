"""Mailbox behavior uses an entirely in-memory IMAP double."""

import base64
import logging
import re
import traceback
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

import pytest

from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import MAX_SNIPPET_BYTES, MailClient, MailError, MailValidationError
from proton_mail_mcp.security import Credentials

PLAIN = b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 24 2 NIL NIL NIL)'
HTML = b'("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "7BIT" 90 1 NIL NIL NIL)'
ATTACHMENT = (
    b'("APPLICATION" "PDF" ("NAME" "invoice.pdf") NIL NIL "BASE64" 400 '
    b'NIL ("ATTACHMENT" ("FILENAME" "invoice.pdf")) NIL)'
)


@dataclass
class FakeMessage:
    headers: bytes
    structure: bytes
    sections: dict[str, bytes]
    internaldate: str | None = None


def message(
    identifier,
    *,
    references=None,
    reply=None,
    subject="Hello",
    structure=PLAIN,
    sections=None,
    date="Wed, 14 Oct 2026 10:00:00 +0000",
    internaldate=None,
    flags=None,
):
    # Message-ID, References, and In-Reply-To cannot use RFC 2047 encoded words.
    # Python 3.14 enforces their ASCII policy: international fixtures need UTF-8.
    international_ids = any(
        value and not value.isascii() for value in (identifier, references, reply)
    )
    headers = EmailMessage(policy=policy.SMTPUTF8 if international_ids else policy.SMTP)
    headers["Message-ID"] = identifier
    headers["From"] = "Sender <sender@example.com>"
    headers["To"] = "reader@example.com"
    headers["Subject"] = subject
    headers["Date"] = date
    if references:
        headers["References"] = references
    if reply:
        headers["In-Reply-To"] = reply
    item = FakeMessage(
        headers.as_bytes(), structure, sections or {"1": b"Hello plain text\r\n"}, internaldate
    )
    if flags is not None:
        # Mutable like the flags that STORE fakes assign; absent unless chosen.
        item.flags = set(flags)
    return item


def internaldate(item):
    return item.internaldate or parsedate_to_datetime(
        str(BytesParser(policy=policy.default).parsebytes(item.headers)["Date"])
    ).strftime("%d-%b-%Y %H:%M:%S %z")


def unquote(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")


class FakeImap:
    capabilities = (b"IMAP4rev1",)

    def __init__(self, folders=None, *, folder_flags=None, uidnext=False):
        self.folders = folders if folders is not None else {"INBOX": {}}
        self.folder_flags = folder_flags or {}
        self.calls = []
        self.current = None
        self.appended = []
        self.literal = None
        self.search_literals = []
        self.uidvalidity = "1"
        # Opt in to imaplib's SELECT responses with UIDNEXT, the evidence that
        # canonical lookups need; other callers keep their UIDNEXT-less paths.
        self.reports_uidnext = uidnext
        self.next_uids = {}

    @property
    def untagged_responses(self):
        if not self.reports_uidnext:
            raise AttributeError("untagged_responses")
        responses = {"UIDVALIDITY": [str(self.uidvalidity).encode()]}
        if self.current is not None:
            # UIDNEXT never decreases, even after a test removes the newest UID.
            uids = [int(uid) for uid in self.folders.get(self.current, {})]
            following = max([self.next_uids.get(self.current, 1), *(uid + 1 for uid in uids)])
            self.next_uids[self.current] = following
            responses["UIDNEXT"] = [str(following).encode()]
        return responses

    def starttls(self, *, ssl_context):
        self.calls.append(("STARTTLS", ssl_context))
        return "OK", []

    def login(self, username, password):
        self.calls.append(("LOGIN", username, password))
        return "OK", []

    def capability(self):
        self.calls.append(("CAPABILITY",))
        atoms = [
            item.encode("ascii") if isinstance(item, str) else item for item in self.capabilities
        ]
        return "OK", [b" ".join(atoms)]

    def logout(self):
        self.calls.append(("LOGOUT",))
        return "BYE", []

    def select(self, folder, *, readonly):
        assert readonly is True
        self.current = unquote(folder)
        self.calls.append(("EXAMINE", folder, readonly))
        return "OK", [str(len(self.folders[self.current])).encode()]

    def status(self, folder, fields):
        assert fields == "(MESSAGES UIDNEXT UIDVALIDITY)"
        self.calls.append(("STATUS", folder, fields))
        name = unquote(folder)
        uids = self.folders[name]
        following = max([self.next_uids.get(name, 1), *(int(uid) + 1 for uid in uids)])
        self.next_uids[name] = following
        return "OK", [
            f"{folder} (MESSAGES {len(uids)} UIDNEXT {following} "
            f"UIDVALIDITY {self.uidvalidity})".encode()
        ]

    def list(self):
        self.calls.append(("LIST",))
        default_flags = r"\HasNoChildren"
        all_flags = r"\All"
        return "OK", [
            f'({flags}) "/" "{folder}"'.encode()
            for folder in self.folders
            for flags in [
                self.folder_flags.get(folder, all_flags if folder == "All Mail" else default_flags)
            ]
        ]

    def response(self, name):
        if name == "CAPABILITY":
            return name, [None]
        assert name == "UIDVALIDITY"
        return name, [str(self.uidvalidity).encode()]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            assert args[:2] == ("CHARSET", "UTF-8")
            criteria = list(args[2:])
            if self.literal is not None:
                payload = self.literal
                self.search_literals.append(payload)
                criteria.append(
                    '"' + payload.decode("utf-8").replace("\\", "\\\\").replace('"', '\\"') + '"'
                )
                self.literal = None
            result = []
            for uid, item in self.folders[self.current].items():
                headers = BytesParser(policy=policy.default).parsebytes(item.headers)
                remaining = list(criteria)
                haystacks = {}

                def criterion():
                    field = remaining.pop(0)
                    if isinstance(field, bytes):
                        field = field.decode("ascii")
                    if field == "OR":
                        left, right = criterion(), criterion()
                        return left or right
                    if field == "ALL":
                        return True
                    if field == "UID":
                        sequence = remaining.pop(0)
                        first, last = sequence.split(":")
                        maximum = max(map(int, self.folders[self.current]), default=0)
                        upper = maximum if last == "*" else int(last)
                        return min(int(first), upper) <= int(uid) <= max(int(first), upper)
                    header_search = field == "HEADER"
                    if field == "HEADER":
                        field = remaining.pop(0)
                    value = remaining.pop(0)
                    if field in ("SINCE", "BEFORE"):
                        return True
                    if field == "ON":
                        # RFC 3501 compares the INTERNALDATE calendar day.
                        return internaldate(item).split()[0] == value
                    needle = unquote(value)
                    cache_key = header_search, field.casefold()
                    if cache_key not in haystacks:
                        if header_search and field.casefold() == "message-id":
                            haystacks[cache_key] = " ".join(
                                value.encode("utf-8", "surrogateescape").decode(
                                    "utf-8", errors="replace"
                                )
                                for name, value in headers.raw_items()
                                if name.casefold() == "message-id"
                            )
                        elif field == "TEXT":
                            haystacks[cache_key] = " ".join(
                                part.decode(errors="replace") for part in item.sections.values()
                            )
                        else:
                            haystacks[cache_key] = " ".join(
                                str(value) for value in headers.get_all(field, [])
                            )
                    return needle.casefold() in haystacks[cache_key].casefold()

                matches = []
                while remaining:
                    matches.append(criterion())
                if all(matches):
                    result.append(uid.encode())
            return "OK", [b" ".join(result)]
        assert command == "FETCH"
        uid, query = args
        if "UID" in query.split() or query.startswith(("(UID ", "(UID)")):
            response = []
            for sequence, current_uid in enumerate(uid.split(","), start=1):
                item = self.folders[self.current].get(current_uid)
                if item is None:
                    continue
                metadata = f"{sequence} (UID {current_uid}".encode()
                if "RFC822.SIZE" in query:
                    size = len(item.headers) + sum(map(len, item.sections.values()))
                    metadata += b" RFC822.SIZE " + str(size).encode()
                if "INTERNALDATE" in query:
                    metadata += b' INTERNALDATE "' + internaldate(item).encode() + b'"'
                if "FLAGS" in query:
                    flags = sorted(getattr(item, "flags", ()))
                    metadata += b" FLAGS (" + " ".join(flags).encode() + b")"
                if "ENVELOPE" in query:
                    from bridge_costs import envelope_from_headers

                    metadata += b" ENVELOPE " + envelope_from_headers(item.headers)
                if "BODYSTRUCTURE" in query:
                    metadata += b" BODYSTRUCTURE " + item.structure
                if "BODY.PEEK[HEADER" in query:
                    raw_headers = item.headers
                    header_range = re.search(r"BODY\.PEEK\[HEADER\.FIELDS .*\]<0\.(\d+)>", query)
                    if header_range:
                        raw_headers = raw_headers[: int(header_range.group(1))]
                    metadata += b" BODY[HEADER.FIELDS (MESSAGE-ID REFERENCES IN-REPLY-TO "
                    metadata += b"FROM TO CC SUBJECT DATE)]"
                    if header_range:
                        metadata += b"<0>"
                    metadata += b" {" + str(len(raw_headers)).encode() + b"}"
                    response.extend([(metadata, raw_headers), b")"])
                else:
                    response.append(metadata + b")")
            return "OK", response
        item = self.folders[self.current][uid]
        if query == "(BODYSTRUCTURE)":
            return "OK", [b"1 (BODYSTRUCTURE " + item.structure + b")"]
        match = re.fullmatch(r"\(BODY\.PEEK\[([^]]+)\](?:<(\d+)\.(\d+)>)?\)", query)
        assert match is not None
        section, offset, length = match.groups()
        payload = item.headers if section == "HEADER" else item.sections[section]
        if offset is not None:
            payload = payload[int(offset) : int(offset) + int(length)]
        return "OK", [(b"1 (BODY[] {" + str(len(payload)).encode() + b"}", payload), b")"]

    def append(self, folder, flags, internaldate, payload):
        self.calls.append(("APPEND", folder, flags))
        self.appended.append((folder, flags, internaldate, payload))
        return "OK", [b"[APPENDUID 1 9]"]


@pytest.fixture
def settings():
    return Settings(
        token="a-test-token-that-is-at-least-32-characters", bridge_user="reader@example.com"
    )


def client(settings, fake):
    def factory(host, port, *, timeout):
        assert (host, port) == ("127.0.0.1", 1143)
        assert 0 < timeout <= settings.timeout
        return fake

    return MailClient(
        settings, lambda _: Credentials("reader@example.com", "test-password-secret"), factory
    )


def test_search_quotes_imap_syntax_and_unicode_without_expanding_criteria(settings):
    needle = 'café ") ALL (" \\ path'
    fake = FakeImap({"INBOX": {"7": message("<7@example.com>", subject=needle)}})
    found = client(settings, fake).search_mail(
        subject=needle, since="2026-01-02", before="2026-11-03"
    )
    assert found["messages"][0]["message_id"] == "<7@example.com>"
    assert found["messages"][0]["subject"] == needle
    search = next(call for call in fake.calls if call[0] == "SEARCH")
    assert search == (
        "SEARCH",
        "CHARSET",
        "UTF-8",
        "SINCE",
        "02-Jan-2026",
        "BEFORE",
        "03-Nov-2026",
    )
    assert fake.search_literals == []
    assert found["messages"][0]["snippet"] == "Hello plain text"
    assert all(call[2] is True for call in fake.calls if call[0] == "EXAMINE")
    assert all(
        "BODY.PEEK[" in call[2] or call[2] == "(UID INTERNALDATE)"
        for call in fake.calls
        if call[0] == "FETCH"
    )


@pytest.mark.parametrize(
    "argument,value",
    [
        ("from_", "a\r\nUID SEARCH ALL"),
        ("folder", "INBOX\nINBOX"),
        ("subject", "a\x00b"),
        ("since", "2026-10-14 ALL"),
        ("before", "2026-02-30"),
        ("limit", 0),
        ("limit", True),
    ],
)
def test_search_rejects_invalid_input_before_connecting(settings, argument, value):
    fake = FakeImap()
    with pytest.raises(ValueError):
        client(settings, fake).search_mail(**{argument: value})
    assert fake.calls == []


def test_search_caps_results_and_breaks_date_ties_by_uid(settings):
    fake = FakeImap({"INBOX": {str(uid): message(f"<{uid}@example.com>") for uid in range(1, 105)}})
    found = client(settings, fake).search_mail(limit=10000)
    assert len(found["messages"]) == 100
    assert found["messages"][0]["uid"] == "104"
    assert found["messages"][-1]["uid"] == "5"


def test_unicode_header_filter_is_local_and_text_uses_one_literal(settings):
    fake = FakeImap(
        {
            "INBOX": {
                "1": message("<1@example.com>", subject="Café", sections={"1": "Crème".encode()}),
                "2": message("<2@example.com>", subject="Café", sections={"1": b"Other"}),
                "3": message("<3@example.com>", subject="Other", sections={"1": "Crème".encode()}),
            }
        }
    )
    found = client(settings, fake).search_mail(subject="Café", text="Crème")
    assert [item["uid"] for item in found["messages"]] == ["1"]
    assert fake.search_literals == ["Crème".encode()]
    assert fake.literal is None


def test_search_snippets_use_partial_fetches_with_a_shared_byte_budget(settings):
    structure = (
        b"(" + PLAIN + b" " + PLAIN + b" " + PLAIN + b" " + ATTACHMENT + b' "MIXED" NIL NIL NIL)'
    )
    first = b"First narrative " + b"x" * 1000
    fake = FakeImap(
        {
            "INBOX": {
                "1": message(
                    "<1@example.com>",
                    structure=structure,
                    sections={
                        "1": first,
                        "2": b"Second narrative " + b"y" * 50000,
                        "3": b"Unneeded narrative",
                    },
                )
            }
        }
    )
    found = client(settings, fake).search_mail()
    assert found["messages"][0]["snippet"] == first.decode()[:200]
    body_fetches = [
        call
        for call in fake.calls
        if call[0] == "FETCH" and re.fullmatch(r"\(BODY\.PEEK\[\d+(?:\.\d+)*\]<.*>\)", call[2])
    ]
    assert body_fetches == [
        ("FETCH", "1", f"(BODY.PEEK[1]<0.{MAX_SNIPPET_BYTES}>)"),
        ("FETCH", "1", f"(BODY.PEEK[2]<0.{MAX_SNIPPET_BYTES - len(first)}>)"),
    ]


@pytest.mark.parametrize("encoding", ["BASE64", "QUOTED-PRINTABLE"])
def test_search_decodes_transfer_encoding_at_partial_fetch_boundary(settings, encoding):
    text = "Hello é " * 2000
    payload = (
        base64.encodebytes(text.encode()) if encoding == "BASE64" else (b"Hello =C3=A9 " * 2000)
    )
    if encoding == "BASE64":
        # Folding causes the partial payload to end inside a quartet.
        assert len(re.sub(rb"\s+", b"", payload[:MAX_SNIPPET_BYTES])) % 4 != 0
    structure = PLAIN.replace(b'"7BIT"', f'"{encoding}"'.encode())
    fake = FakeImap(
        {"INBOX": {"1": message("<1@example.com>", structure=structure, sections={"1": payload})}}
    )
    found = client(settings, fake).search_mail()
    assert found["messages"][0]["snippet"] == " ".join(text.split())[:200]
    assert ("FETCH", "1", f"(BODY.PEEK[1]<0.{MAX_SNIPPET_BYTES}>)") in fake.calls
    assert ("FETCH", "1", "(BODY.PEEK[1])") not in fake.calls


def test_search_partial_html_fallback_preserves_visible_snippet(settings):
    fake = FakeImap(
        {
            "INBOX": {
                "1": message(
                    "<1@example.com>",
                    structure=HTML,
                    sections={"1": b"<head>hidden</head><p>Hello &amp; world</p>" + b" " * 50000},
                )
            }
        }
    )
    assert client(settings, fake).search_mail()["messages"][0]["snippet"] == "Hello & world"
    assert ("FETCH", "1", f"(BODY.PEEK[1]<0.{MAX_SNIPPET_BYTES}>)") in fake.calls


def test_thread_connects_ancestors_and_siblings_across_folders_without_attachment_fetch(settings):
    multipart = b"(" + PLAIN + b" " + ATTACHMENT + b' "MIXED" ("BOUNDARY" "x") NIL NIL)'
    fake = FakeImap(
        {
            "INBOX": {
                "2": message(
                    "<child@example.com>", references="<root@example.com>", structure=multipart
                )
            },
            "Archive": {"4": message("<root@example.com>", date="Wed, 14 Oct 2026 09:00:00 +0000")},
            "Sent": {
                "8": message(
                    "<sibling@example.com>",
                    reply="<root@example.com>",
                    date="Wed, 14 Oct 2026 11:00:00 +0000",
                ),
                "9": message("<prefix-root@example.com>", subject="Unrelated"),
            },
        }
    )
    found = client(settings, fake).read_thread("<child@example.com>")
    assert [item["message_id"] for item in found["messages"]] == [
        "<root@example.com>",
        "<child@example.com>",
        "<sibling@example.com>",
    ]
    child = found["messages"][1]
    assert child["text"] == "Hello plain text"
    assert child["headers"]["References"] == "<root@example.com>"
    assert child["attachments"] == [
        {"filename": "invoice.pdf", "type": "application/pdf", "size": 400}
    ]
    assert not any(call[0] == "FETCH" and call[2] == "(BODY.PEEK[2])" for call in fake.calls)


def test_thread_deduplicates_message_ids_across_all_mail_and_labels(settings, monkeypatch):
    monkeypatch.setattr("proton_mail_mcp.mail.MAX_THREAD_MESSAGES", 2)
    root = message("<root@example.com>")
    child = message("<child@example.com>", reply="<root@example.com>")
    fake = FakeImap(
        {
            "All Mail": {"101": root, "102": child},
            "INBOX": {"1": root},
            "Labels/Project": {"51": root, "52": child},
            "Sent": {"2": child},
        },
        folder_flags={"All Mail": r"\All"},
        uidnext=True,
    )
    found = client(settings, fake).read_thread("<root@example.com>")
    assert {item["message_id"] for item in found["messages"]} == {
        "<root@example.com>",
        "<child@example.com>",
    }
    assert len(found["messages"]) == 2
    # Each message reports its real folder; the All Mail handle it was read from
    # is only the alias.
    assert [
        (item["message_id"], item["folder"], item["uidvalidity"], item["uid"], item["alias"])
        for item in found["messages"]
    ] == [
        (
            "<root@example.com>",
            "INBOX",
            "1",
            "1",
            {"folder": "All Mail", "uidvalidity": "1", "uid": "101"},
        ),
        (
            "<child@example.com>",
            "Sent",
            "1",
            "2",
            {"folder": "All Mail", "uidvalidity": "1", "uid": "102"},
        ),
    ]
    metadata = [call for call in fake.calls if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]]
    assert [call[1] for call in metadata] == ["101", "102"]
    assert all("BODYSTRUCTURE" not in call[2] for call in metadata)
    assert [
        call[1] for call in fake.calls if call[0] == "FETCH" and call[2] == "(BODYSTRUCTURE)"
    ] == ["101", "102"]
    examined = [call[1] for call in fake.calls if call[0] == "EXAMINE"]
    # Expansion still searches All Mail alone; the canonical lookup then reads
    # cheap envelopes in real folders, reopens each mailbox it read to prove
    # that none changed, and never opens a label.
    lookup = ['"All Mail"', '"INBOX"', '"Sent"']
    assert examined == ['"All Mail"', *lookup, *lookup]
    last_header_search = max(
        index for index, call in enumerate(fake.calls) if call[0] == "SEARCH" and "HEADER" in call
    )
    second_examine = [index for index, call in enumerate(fake.calls) if call[0] == "EXAMINE"][1]
    assert last_header_search < second_examine
    assert [
        call for call in fake.calls if call[0] == "FETCH" and call[2].startswith("(BODY.PEEK[1]")
    ] == [
        ("FETCH", "101", "(BODY.PEEK[1]<0.65536>)"),
        ("FETCH", "102", "(BODY.PEEK[1]<0.65536>)"),
    ]


def test_thread_does_not_collapse_messages_without_message_ids(settings):
    first = message("<first@example.com>", reply="<root@example.com>")
    second = message("<second@example.com>", reply="<root@example.com>")
    first.headers = first.headers.replace(b"Message-ID: <first@example.com>\r\n", b"")
    second.headers = second.headers.replace(b"Message-ID: <second@example.com>\r\n", b"")
    fake = FakeImap({"INBOX": {"1": first, "2": second}})
    found = client(settings, fake).read_thread("<root@example.com>")
    assert [item["uid"] for item in found["messages"]] == ["1", "2"]


@pytest.mark.parametrize("field", ["References", "In-Reply-To"])
@pytest.mark.parametrize("control", [b"\x00", b"\x7f"])
def test_thread_skips_malformed_references_and_still_connects_valid_links(
    settings, monkeypatch, field, control
):
    monkeypatch.setattr("proton_mail_mcp.mail.MAX_THREAD_IDS", 3)
    child = message("<child@example.com>")
    malformed = field.encode() + b": <bad" + control + b"@example.com> <root@example.com>"
    child.headers = child.headers.replace(b"\r\n\r\n", b"\r\n" + malformed + b"\r\n\r\n", 1)
    fake = FakeImap(
        {
            "INBOX": {
                "1": message("<root@example.com>"),
                "2": child,
                "3": message("<sibling@example.com>", reply="<root@example.com>"),
            }
        }
    )
    found = client(settings, fake).read_thread("<child@example.com>")
    assert {item["message_id"] for item in found["messages"]} == {
        "<root@example.com>",
        "<child@example.com>",
        "<sibling@example.com>",
    }
    assert all(
        control not in argument
        for call in fake.calls
        if call[0] == "SEARCH"
        for argument in call[1:]
        if isinstance(argument, bytes)
    )


@pytest.mark.parametrize("identifier", ["<bad\x00@example.com>", "<bad\x7f@example.com>"])
def test_thread_rejects_malformed_root_id_before_connecting(settings, identifier):
    fake = FakeImap()
    with pytest.raises(MailValidationError):
        client(settings, fake).read_thread(identifier)
    assert fake.calls == []


def test_thread_filters_substring_message_id_matches(settings):
    fake = FakeImap(
        {"INBOX": {"1": message("<x@example.com>", references="<prefix-x@example.com>")}}
    )
    assert (
        client(settings, fake).read_thread("<prefix-x@example.com>")["messages"][0]["message_id"]
        == "<x@example.com>"
    )
    assert client(settings, fake).read_thread("<fix-x@example.com>")["messages"] == []


def test_thread_prefers_plain_alternative(settings):
    structure = b"(" + HTML + b" " + PLAIN + b' "ALTERNATIVE" NIL NIL NIL)'
    fake = FakeImap(
        {
            "INBOX": {
                "1": message(
                    "<1@example.com>", structure=structure, sections={"2": b"Preferred plain body"}
                )
            }
        }
    )
    found = client(settings, fake).read_thread("<1@example.com>")
    assert found["messages"][0]["text"] == "Preferred plain body"
    assert found["messages"][0]["attachments"] == []


def test_thread_preserves_separate_plain_narrative_parts(settings):
    structure = b"(" + PLAIN + b" " + PLAIN + b' "MIXED" NIL NIL NIL)'
    fake = FakeImap(
        {
            "INBOX": {
                "1": message(
                    "<1@example.com>",
                    structure=structure,
                    sections={"1": b"First narrative", "2": b"Second narrative"},
                )
            }
        }
    )
    assert (
        client(settings, fake).read_thread("<1@example.com>")["messages"][0]["text"]
        == "First narrative\n\nSecond narrative"
    )


@pytest.mark.parametrize(
    "parameters",
    [
        b'"FILENAME*" "utf-8\'\'caf%C3%A9.pdf"',
        b'"FILENAME*0*" "utf-8\'\'caf%C3%A9" "FILENAME*1*" ".pdf"',
    ],
)
def test_attachment_rfc2231_filename_decodes_without_fetching_attachment(settings, parameters):
    attachment = ATTACHMENT.replace(b'"FILENAME" "invoice.pdf"', parameters)
    structure = b"(" + PLAIN + b" " + attachment + b' "MIXED" NIL NIL NIL)'
    fake = FakeImap({"INBOX": {"1": message("<1@example.com>", structure=structure)}})
    assert (
        client(settings, fake).read_thread("<1@example.com>")["messages"][0]["attachments"][0][
            "filename"
        ]
        == "café.pdf"
    )


def test_html_fallback_strips_markup_and_hidden_content(settings):
    fake = FakeImap(
        {
            "INBOX": {
                "1": message(
                    "<1@example.com>",
                    structure=HTML,
                    sections={
                        "1": (
                            b"<head>hidden</head><p>Hello &amp; world</p>"
                            b"<script>secret()</script><p>Next</p>"
                        )
                    },
                )
            }
        }
    )
    body = client(settings, fake).read_thread("<1@example.com>")["messages"][0]["text"]
    assert "Hello & world" in body and "Next" in body
    assert "hidden" not in body and "secret" not in body and "<" not in body


@pytest.mark.parametrize(
    "encoding,payload", [("BASE64", b"SGVsbG8gw6k="), ("QUOTED-PRINTABLE", b"Hello =C3=A9")]
)
def test_text_transfer_encoding_is_decoded(settings, encoding, payload):
    structure = PLAIN.replace(b'"7BIT"', f'"{encoding}"'.encode())
    fake = FakeImap(
        {"INBOX": {"1": message("<1@example.com>", structure=structure, sections={"1": payload})}}
    )
    assert client(settings, fake).read_thread("<1@example.com>")["messages"][0]["text"] == "Hello é"


def test_create_draft_appends_only_new_draft_and_sets_reply_headers(settings):
    fake = FakeImap(
        {
            "INBOX": {"1": message("<parent@example.com>", references="<ancestor@example.com>")},
            "Drafts": {},
        }
    )
    draft_id = client(settings, fake).create_draft(
        ["other@example.com"],
        "cc@example.com",
        subject="Re: Hello",
        body="Draft body",
        in_reply_to="<parent@example.com>",
    )
    folder, flags, internaldate, raw = fake.appended[0]
    assert (folder, flags, internaldate) == ('"Drafts"', r"(\Draft)", None)
    draft = BytesParser(policy=policy.default).parsebytes(raw)
    assert draft["Message-ID"] == draft_id
    assert draft["From"] == "reader@example.com"
    assert draft["To"] == "other@example.com" and draft["Cc"] == "cc@example.com"
    assert draft["References"] == "<ancestor@example.com> <parent@example.com>"
    assert draft["In-Reply-To"] == "<parent@example.com>"
    assert draft.get_content().strip() == "Draft body"
    assert {call[0] for call in fake.calls} <= {
        "STARTTLS",
        "LOGIN",
        "CAPABILITY",
        "LIST",
        "EXAMINE",
        "SEARCH",
        "FETCH",
        "APPEND",
        "LOGOUT",
    }


def test_new_draft_appends_without_opening_existing_mailbox(settings):
    fake = FakeImap()
    client(settings, fake).create_draft("other@example.com", subject="New draft", body="Text")
    assert [call[0] for call in fake.calls] == [
        "STARTTLS",
        "LOGIN",
        "CAPABILITY",
        "APPEND",
        "LOGOUT",
    ]
    draft = BytesParser(policy=policy.default).parsebytes(fake.appended[0][3])
    assert "References" not in draft and "In-Reply-To" not in draft
    assert "Cc" not in draft


@pytest.mark.parametrize("field", ["to", "cc"])
@pytest.mark.parametrize("suffix", [",", ", "])
def test_draft_rejects_trailing_comma_recipient_list_before_connecting(settings, field, suffix):
    fake = FakeImap()
    arguments = {"to": "other@example.com", "subject": "Hello", "body": "Text"}
    arguments[field] = "first@example.com, second@example.com" + suffix
    with pytest.raises(MailValidationError, match="Recipients must be valid email addresses"):
        client(settings, fake).create_draft(**arguments)
    assert fake.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("to", "victim@example.com\r\nBcc: hidden@example.com"),
        ("to", ""),
        ("to", []),
        ("cc", "not-an-address"),
        ("subject", "Hi\nBcc: hidden@example.com"),
        ("in_reply_to", "<parent@example.com>\r\nX-Other: yes"),
    ],
)
def test_draft_rejects_header_injection_and_invalid_addresses(settings, field, value):
    fake = FakeImap()
    arguments = {"to": "other@example.com", "cc": None, "subject": "Hello", "body": "Text"}
    arguments[field] = value
    with pytest.raises(ValueError):
        client(settings, fake).create_draft(**arguments)
    assert fake.calls == []


def test_wrong_certificate_pin_is_rejected_before_login(settings, monkeypatch):
    fake = FakeImap()

    def reject(*_):
        raise RuntimeError("certificate mismatch")

    monkeypatch.setattr("proton_mail_mcp.mail.verify_certificate", reject)
    with pytest.raises(MailError):
        client(settings, fake).search_mail()
    assert [call[0] for call in fake.calls] == ["STARTTLS", "LOGOUT"]


def test_starttls_failure_never_authenticates(settings):
    fake = FakeImap()

    def refused(*, ssl_context):
        fake.calls.append(("STARTTLS",))
        return "NO", [b"test-password-secret"]

    fake.starttls = refused
    with pytest.raises(MailError, match="Mailbox operation failed"):
        client(settings, fake).search_mail()
    assert [call[0] for call in fake.calls] == ["STARTTLS", "LOGOUT"]


def test_unavailable_folder_returns_sanitized_error_without_body_fetch(settings):
    fake = FakeImap()

    def unavailable(folder, *, readonly):
        fake.calls.append(("EXAMINE", folder, readonly))
        return "NO", [b"test-password-secret"]

    fake.select = unavailable
    with pytest.raises(MailError, match="Mailbox operation failed") as caught:
        client(settings, fake).search_mail(folder="Unavailable")
    assert "test-password-secret" not in str(caught.value)
    assert [call[0] for call in fake.calls] == [
        "STARTTLS",
        "LOGIN",
        "CAPABILITY",
        "LIST",
        "EXAMINE",
        "LOGOUT",
    ]


def test_server_errors_and_logs_never_contain_password_or_token(settings, caplog):
    fake = FakeImap()

    def bad_login(username, password):
        raise RuntimeError(f"rejected {username}: {password} {settings.token}")

    fake.login = bad_login
    with caplog.at_level(logging.DEBUG), pytest.raises(MailError) as caught:
        client(settings, fake).search_mail()
    output = caplog.text + str(caught.value) + "".join(traceback.format_exception(caught.value))
    assert "test-password-secret" not in output and settings.token not in output
    assert fake.calls[-1] == ("LOGOUT",)


def test_logout_failure_does_not_expose_server_details(settings, caplog):
    fake = FakeImap()

    def bad_logout():
        raise RuntimeError("test-password-secret")

    fake.logout = bad_logout
    assert client(settings, fake).search_mail()["messages"] == []
    assert "test-password-secret" not in caplog.text
