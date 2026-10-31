"""Draft lookup binds the requested UID and refuses incomplete mailbox responses."""

from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import MailClient, MailError, MailValidationError

ID = "<draft@example.test>"
RAW = (
    b"From: sender@example.test\r\nTo: recipient@example.test\r\n"
    b"Message-ID: <draft@example.test>\r\nSubject: Preview\r\n\r\nBody\r\n"
)


def response(raw=RAW, *, uid=b"7", flags=b"\\Draft", size=None):
    size = len(raw) if size is None else size
    return [
        (
            b"1 (UID "
            + uid
            + b" RFC822.SIZE "
            + str(size).encode()
            + b" FLAGS ("
            + flags
            + b") BODY[]<0> {"
            + str(len(raw)).encode()
            + b"}",
            raw,
        ),
        b")",
    ]


@pytest.fixture
def reader():
    settings = Settings(token="x" * 32, bridge_user="sender@example.test")
    client = MailClient(settings, credentials_provider=Mock(side_effect=AssertionError))
    connection = Mock()
    connection.select.return_value = ("OK", [b"1"])
    connection.untagged_responses = {"UIDVALIDITY": [b"9"]}
    connection.uid.side_effect = [("OK", [b"7"]), ("OK", response())]

    @contextmanager
    def session():
        yield connection, None

    client._session = session
    return client, connection


def test_draft_reader_uses_only_readonly_drafts_and_bounded_peek(reader):
    client, connection = reader
    assert client.read_draft(ID) == {
        "message_id": ID,
        "folder": "Drafts",
        "uid": "7",
        "uidvalidity": "9",
        "raw": RAW,
    }
    connection.select.assert_called_once_with('"Drafts"', readonly=True)
    assert connection.untagged_responses == {"UIDVALIDITY": [b"9"]}
    connection.response.assert_not_called()
    assert connection.uid.call_args_list[0].args == (
        "SEARCH",
        "CHARSET",
        "UTF-8",
        "HEADER",
        "Message-ID",
        b'"<draft@example.test>"',
    )
    assert connection.uid.call_args_list[1].args == (
        "FETCH",
        "7",
        "(UID RFC822.SIZE FLAGS BODY.PEEK[]<0.10485761>)",
    )
    client.credentials_provider.assert_not_called()


@pytest.mark.parametrize("uids", [b"", b"7 8", b"0 7", b"bad"])
def test_missing_ambiguous_or_invalid_draft_uid_refuses(reader, uids):
    client, connection = reader
    connection.uid.side_effect = [("OK", [uids])]
    with pytest.raises((MailError, MailValidationError)):
        client.read_draft(ID)
    assert connection.uid.call_count == 1


@pytest.mark.parametrize(
    "rows",
    [
        response(uid=b"8"),
        response(flags=b"\\Seen"),
        response(flags=b"\\Draft \\Deleted"),
        response(size=len(RAW) + 1),
        response(size=10485761),
        response(RAW.replace(b"<draft@", b"<other@")),
        response(b"Message-ID: <extra@example.test>\r\n" + RAW),
        response() + response(),
        [b"1 (UID 7 FLAGS (\\Draft))"],
        [b"1 (UID 7 BODY[]<0> NIL)"],
    ],
)
def test_incomplete_or_mismatched_content_refuses(reader, rows):
    client, connection = reader
    connection.uid.side_effect = [("OK", [b"7"]), ("OK", rows)]
    with pytest.raises((MailError, MailValidationError)):
        client.read_draft(ID)


def test_unsolicited_literal_and_flag_update_cannot_substitute_draft(reader):
    client, connection = reader
    rows = response(b"unrelated", uid=b"99") + [b"2 (UID 7 FLAGS (\\Seen))"] + response()
    connection.uid.side_effect = [("OK", [b"7"]), ("OK", rows)]
    assert client.read_draft(ID)["raw"] == RAW


@pytest.mark.parametrize("values", [[None], [], [b"invalid"]])
def test_missing_uidvalidity_refuses_before_search(reader, values):
    client, connection = reader
    connection.untagged_responses["UIDVALIDITY"] = values
    with pytest.raises(MailError):
        client.read_draft(ID)
    connection.uid.assert_not_called()
