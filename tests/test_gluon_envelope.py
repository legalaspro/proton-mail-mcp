"""Envelope fixtures follow the archived Gluon source contracts, without a server."""

import pytest
from bridge_costs import _nstring, envelope_from_headers

from proton_mail_mcp.cleanup_lookup import envelope_row
from proton_mail_mcp.mail import _ImapValues


def parsed(*lines):
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
    return _ImapValues(envelope_from_headers(raw)).value()


def test_absent_sender_and_reply_to_copy_from():
    # imap/envelope.go:38-52; absent optional headers are never NIL with From.
    value = parsed("From: offers@updates.example.invalid")
    assert value[2] == [[None, None, b"offers", b"updates.example.invalid"]]
    assert value[3] == value[4] == value[2]


@pytest.mark.parametrize(
    "line,index",
    [
        ("Sender: <<<garbled", 3),
        ("Sender:", 3),
        ("Reply-To: <<<garbled", 4),
        ("Reply-To: =?x-unknown?Q?Bank?= <security@bank.example.com>", 4),
    ],
)
def test_present_unusable_optional_sender_is_nil(line, index):
    # imap/envelope.go:79-86; rfc5322/parser.go:53-56 and atom.go:177-180.
    value = parsed("From: offers@updates.example.invalid", line)
    assert value[2]
    assert value[index] is None
    assert value[7 - index] == value[2]


def test_groups_flatten_to_mailboxes():
    # rfc5322/address.go parseGroup returns its member list, without markers.
    value = parsed("From: Group: one@updates.example.invalid, two@updates.example.invalid;")
    assert value[2] == [
        [None, None, b"one", b"updates.example.invalid"],
        [None, None, b"two", b"updates.example.invalid"],
    ]
    assert parsed("From: undisclosed-recipients:;")[2:5] == [None, None, None]


@pytest.mark.parametrize(
    "encoded,name",
    [
        ("=?UTF-8?Q?B=C3=A4nk_Support?=", "Bänk Support"),
        ("=?UTF-8?Q?Newsletter?=", "Newsletter"),
    ],
)
def test_encoded_words_decode_to_utf8_names(encoded, name):
    # rfc5322/atom.go:173-186 stores the decoded name, not the encoded word.
    value = parsed(f"From: {encoded} <offers@updates.example.invalid>")
    assert value[2] == [[name.encode(), None, b"offers", b"updates.example.invalid"]]


@pytest.mark.parametrize("field,index", [("From", 2), ("Sender", 3), ("Reply-To", 4)])
def test_only_first_sender_header_is_visible(field, index):
    # rfc822/header.go:105-121 returns v[0], including for repeated headers.
    lines = [] if field == "From" else ["From: offers@updates.example.invalid"]
    value = parsed(
        *lines, f"{field}: first@updates.example.invalid", f"{field}: protected@bank.example.com"
    )
    assert value[index] == [[None, None, b"first", b"updates.example.invalid"]]


def test_only_first_message_id_is_visible():
    value = parsed("Message-ID: <first@example.invalid>", "Message-ID: <second@example.invalid>")
    assert value[9] == b"<first@example.invalid>"


@pytest.mark.parametrize("invalid_byte", [b"\x80", b"\xff"])
def test_invalid_utf8_message_id_uses_go_byte_escape(invalid_byte):
    identifier = b"<invalid" + invalid_byte + b"@example.invalid>"
    raw = b"From: offers@updates.example.invalid\r\nMessage-ID: " + identifier + b"\r\n\r\n"
    wire = envelope_from_headers(raw)
    assert ("\\x" + invalid_byte.hex()).encode("ascii") in wire
    value = _ImapValues(wire).value()
    assert value[9] == identifier
    row = envelope_row({"envelope": value}, "1", "INBOX", "100")
    assert row["identity_invalid"] is True


def test_quoted_local_at_sign_does_not_survive_address_split():
    # imap/params.go:141-146 uses strings.Split, requiring exactly two parts.
    value = parsed('From: "a@b"@updates.example.invalid')
    assert value[2] == [[None, None, None, None]]


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, b"NIL"),
        ("", b"NIL"),
        ('quote" slash\\', b'"quote\\" slash\\\\"'),
        ("Bänk", '"Bänk"'.encode()),
        ("\a\b\f\n\r\t\v", rb'"\a\b\f\n\r\t\v"'),
        ("\x00\x1f\x7f", rb'"\x00\x1f\x7f"'),
        ("\u00a0\u2028", rb'"\u00a0\u2028"'),
        ("\U000e0001", rb'"\U000e0001"'),
    ],
)
def test_envelope_strings_use_go_quote(value, expected):
    # imap/params.go:88-102 delegates all nonempty strings to strconv.Quote.
    assert _nstring(value) == expected
