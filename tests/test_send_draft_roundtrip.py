"""APPEND bytes return unchanged through a local draft reader and send preview."""

from contextlib import contextmanager
from email import policy
from email.header import Header
from email.parser import BytesParser
from unittest.mock import Mock

import pytest
from test_mail import PLAIN, FakeImap, FakeMessage, unquote
from test_send import DRAFT_ID, RECIPIENT, SENDER, draft
from test_send import local_certificate as local_certificate
from test_send import rig as rig
from test_send_reader import response

from proton_mail_mcp.mail import MailClient, MailError, MailValidationError
from proton_mail_mcp.security import Credentials
from proton_mail_mcp.send import SendService


class AppendedDrafts(FakeImap):
    def __init__(self):
        super().__init__({"Drafts": {}})
        self.returned_bytes = []

    def append(self, folder, flags, internaldate, payload):
        result = super().append(folder, flags, internaldate, payload)
        self.folders[unquote(folder)]["7"] = FakeMessage(payload, PLAIN, {})
        return result

    def uid(self, command, *args):
        if command == "FETCH" and "BODY.PEEK[]" in args[1]:
            self.calls.append((command, *args))
            assert args[0] == "7"
            raw = self.folders[self.current]["7"].headers
            self.returned_bytes.append(raw)
            return "OK", response(raw)
        return super().uid(command, *args)


def roundtrip_client(rig, monkeypatch, username):
    fake = AppendedDrafts()
    client = MailClient(rig.settings, credentials_provider=Mock(side_effect=AssertionError))

    @contextmanager
    def session(deadline=None):
        yield fake, Credentials(username, "synthetic-password")

    monkeypatch.setattr(client, "_session", session)
    monkeypatch.setattr("proton_mail_mcp.mail.make_msgid", lambda domain: DRAFT_ID)
    return client, fake, SendService(client, rig.settings, tokens=rig.tokens, log=rig.log)


@pytest.mark.parametrize("username", [SENDER, f"Plain Owner <{SENDER}>"], ids=["bare", "named"])
@pytest.mark.parametrize("to", [RECIPIENT, f"Plain Recipient <{RECIPIENT}>"], ids=["bare", "named"])
@pytest.mark.parametrize("bracketed", [True, False], ids=["bracketed-id", "bare-id"])
def test_create_draft_preview_roundtrip(rig, monkeypatch, username, to, bracketed):
    client, fake, service = roundtrip_client(rig, monkeypatch, username)
    created = client.create_draft(to, subject="Synthetic draft", body="Plain body")
    draft_id = created if bracketed else created[1:-1]
    preview = service.preview_send(draft_id)
    assert preview["token"] and preview["draft_id"] == created
    assert preview["from"] == SENDER and preview["to"] == [RECIPIENT]
    assert len(fake.appended) == 1
    appended = fake.appended[0][3]
    assert fake.returned_bytes == [appended]
    message = BytesParser(policy=policy.SMTP).parsebytes(appended)
    assert message["From"] == SENDER
    assert str(message["To"]) == to
    assert message.get_content().strip() == "Plain body"
    assert client.read_draft(draft_id)["raw"] == appended
    assert len(rig.tokens._grants) == 1
    assert rig.smtp.calls == []


def test_create_draft_long_bare_recipient_does_not_fold_send_headers(rig, monkeypatch):
    client, fake, service = roundtrip_client(rig, monkeypatch, SENDER)
    recipient = "a" * 64 + "@example.test"
    created = client.create_draft(recipient, subject="Synthetic draft", body="Plain body")
    assert b"To: " + recipient.encode() + b"\r\n" in fake.appended[0][3]
    preview = service.preview_send(created)
    assert preview["token"] and preview["to"] == [recipient]
    assert fake.returned_bytes == [fake.appended[0][3]]
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "username",
    [
        f'"other@example.test" <{SENDER}>',
        f'"{SENDER}" <other@example.test>',
        f'"Other, Owner" <{SENDER}>',
        f'"Other <Owner>" <{SENDER}>',
        f"=?utf-8?q?Plain_Owner?= <{SENDER}>",
        f"Plain Ownér <{SENDER}>",
        "sеnder@example.test",
        f"Owners: {SENDER};",
        "",
        "not-an-address",
        f"{SENDER}, other@example.test",
        f"{SENDER},",
        f"{SENDER} (Other)",
        f"{SENDER}\r\nBcc: other@example.test",
    ],
)
def test_ambiguous_credential_from_refuses_before_append(rig, monkeypatch, username):
    client, fake, _ = roundtrip_client(rig, monkeypatch, username)
    with pytest.raises((MailError, MailValidationError)):
        client.create_draft(RECIPIENT, subject="Synthetic draft", body="Plain body")
    assert fake.appended == []
    assert not rig.tokens._grants and rig.smtp.calls == []


@pytest.mark.parametrize(
    "username,address",
    [
        ("other@example.test", "other@example.test"),
        ("Sender@example.test", "Sender@example.test"),
        ("sender@EXAMPLE.test", "sender@EXAMPLE.test"),
        ('"Plain Owner" <other@example.test>', "other@example.test"),
    ],
)
def test_create_draft_does_not_select_or_casefold_an_allowlisted_from(
    rig, monkeypatch, username, address
):
    client, fake, service = roundtrip_client(rig, monkeypatch, username)
    created = client.create_draft(RECIPIENT, subject="Synthetic draft", body="Plain body")
    message = BytesParser(policy=policy.SMTP).parsebytes(fake.appended[0][3])
    assert message["From"] == address
    with pytest.raises(MailValidationError, match="exact allowlisted"):
        service.preview_send(created)
    assert not rig.tokens._grants and rig.smtp.calls == []


@pytest.mark.parametrize("field", ["To", "Cc", "Bcc", "Reply-To"])
@pytest.mark.parametrize(
    "value",
    [
        f'"other@example.test" <{RECIPIENT}>',
        f'"Other, Recipient" <{RECIPIENT}>',
        f'"Other <Recipient>" <{RECIPIENT}>',
        f"=?utf-8?q?Plain_Recipient?= <{RECIPIENT}>",
        f"Plain Recipiént <{RECIPIENT}>",
        "recipiеnt@example.test",
        f"Recipients: {RECIPIENT};",
        "",
        "not-an-address",
        f"{RECIPIENT},",
        f"{RECIPIENT},,other@example.test",
        f"{RECIPIENT} trailing",
        f"{RECIPIENT} (Other)",
    ],
)
def test_address_headers_refuse_ambiguous_or_hidden_addresses(rig, field, value):
    raw = draft()
    if field == "To":
        raw = raw.replace(b"To: " + RECIPIENT.encode(), b"To: " + value.encode())
    else:
        raw = field.encode() + b": " + value.encode() + b"\r\n" + raw
    rig.client.raw = raw
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants and rig.smtp.calls == []


@pytest.mark.parametrize(
    "header",
    [
        b"From: sender@example.test, other@example.test\r\n",
        b"From: sender@example.test\r\nFrom: sender@example.test\r\n",
        b'From: "other@example.test" <sender@example.test>\r\n',
        b'From: "sender@example.test" <other@example.test>\r\n',
        b"From: Owners: sender@example.test;\r\n",
        b"From: \r\n",
        b"From: not-an-address\r\n",
        b"From: sender@example.test\r\nSender: other@example.test\r\n",
        b"From: sender@example.test\r\nSender: Sender@example.test\r\n",
        b"From: sender@example.test\r\nSender: sender@example.test, other@example.test\r\n",
    ],
)
def test_from_and_sender_guards_refuse_without_token(rig, header):
    rig.client.raw = draft().replace(b"From: sender@example.test\r\n", header)
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants and rig.smtp.calls == []


def test_matching_sender_remains_exact(rig):
    rig.client.raw = b"Sender: sender@example.test\r\n" + draft()
    assert rig.service.preview_send(DRAFT_ID)["token"]
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "draft_id",
    ["", "draft>", "<draft", "<draft><other>", "draft id", "draft\nother", "draft\n", "\tdraft"],
)
def test_invalid_draft_id_refuses_before_read(rig, draft_id):
    with pytest.raises(MailValidationError):
        rig.service.preview_send(draft_id)
    assert rig.client.reads == []
    assert not rig.tokens._grants and rig.smtp.calls == []


class BridgeRewrittenDrafts(AppendedDrafts):
    """Store the bridge's rendering of APPENDed drafts, not the submitted bytes."""

    def __init__(self, rewritten_from, *, cte=None):
        super().__init__()
        self.rewritten_from = rewritten_from
        self.cte = cte

    def append(self, folder, flags, internaldate, payload):
        result = super().append(folder, flags, internaldate, payload)
        if self.cte:
            message = BytesParser(policy=policy.SMTP).parsebytes(payload)
            message.set_content(message.get_content(), cte=self.cte)
            payload = message.as_bytes(policy=policy.SMTP)
        headers, separator, body = payload.partition(b"\r\n\r\n")
        rewritten = []
        for line in headers.split(b"\r\n"):
            if line.startswith(b"From:"):
                line = b"From: " + self.rewritten_from.encode("ascii")
            elif line.startswith(b"Message-ID:"):
                line = b"Message-Id:" + line[len(b"Message-ID:") :]
            elif line.startswith(b"Date:"):
                line = b"Date: Mon, 19 Oct 2026 10:00:00 +0000"
            rewritten.append(line)
        rewritten.append(b"References: <c3ludGhldGlj==@protonmail.internalid>")
        stored = b"\r\n".join(rewritten) + separator + body
        self.folders[unquote(folder)]["7"] = FakeMessage(stored, PLAIN, {})
        return result


def rewritten_roundtrip_client(rig, monkeypatch, rewritten_from, *, cte=None):
    fake = BridgeRewrittenDrafts(rewritten_from, cte=cte)
    client = MailClient(rig.settings, credentials_provider=Mock(side_effect=AssertionError))

    @contextmanager
    def session(deadline=None):
        yield fake, Credentials(SENDER, "synthetic-password")

    monkeypatch.setattr(client, "_session", session)
    monkeypatch.setattr("proton_mail_mcp.mail.make_msgid", lambda domain: DRAFT_ID)
    return client, fake, SendService(client, rig.settings, tokens=rig.tokens, log=rig.log)


@pytest.mark.parametrize(
    "rewritten_from",
    [
        f"Plain Owner <{SENDER}>",
        f'"Plain Owner" <{SENDER}>',
        f"=?utf-8?q?Pl=C3=A4in_Owner?= <{SENDER}>",
        f"=?UTF-8?B?UGzDpGluIE93bmVy?= <{SENDER}>",
    ],
    ids=["plain-name", "quoted-name", "utf8-q-name", "utf8-b-name"],
)
@pytest.mark.parametrize("bracketed", [True, False], ids=["bracketed-id", "bare-id"])
def test_create_draft_bridge_rewritten_preview_roundtrip(
    rig, monkeypatch, rewritten_from, bracketed
):
    client, fake, service = rewritten_roundtrip_client(rig, monkeypatch, rewritten_from)
    created = client.create_draft(RECIPIENT, subject="Synthetic draft", body="Plain body")
    draft_id = created if bracketed else created[1:-1]
    stored = fake.folders["Drafts"]["7"].headers
    appended = fake.appended[0][3]
    assert b"From: " + SENDER.encode() + b"\r\n" in appended
    assert b"From: " + rewritten_from.encode() + b"\r\n" in stored
    assert b"Message-Id: " + created.encode() + b"\r\n" in stored
    assert b"Date: Mon, 19 Oct 2026 10:00:00 +0000\r\n" in stored
    assert b"References: <c3ludGhldGlj==@protonmail.internalid>\r\n" in stored
    assert b"In-Reply-To:" not in stored
    assert stored != appended
    assert client.read_draft(draft_id)["raw"] == stored
    preview = service.preview_send(draft_id)
    assert preview["token"] and preview["draft_id"] == created
    assert preview["from"] == SENDER and preview["to"] == [RECIPIENT]
    assert service._read(draft_id)["wire"] == stored
    assert len(rig.tokens._grants) == 1
    assert rig.smtp.calls == []


@pytest.mark.parametrize("cte", ["base64", "quoted-printable"])
def test_create_draft_bridge_rerendered_mime_keeps_stored_wire(rig, monkeypatch, cte):
    client, fake, service = rewritten_roundtrip_client(
        rig, monkeypatch, f"Plain Owner <{SENDER}>", cte=cte
    )
    created = client.create_draft(RECIPIENT, subject="Synthetic draft", body="Plain body")
    stored = fake.folders["Drafts"]["7"].headers
    message = BytesParser(policy=policy.SMTP).parsebytes(stored)
    assert message["Content-Transfer-Encoding"] == cte
    assert message.get_content().strip() == "Plain body"
    preview = service.preview_send(created)
    assert preview["token"] and preview["from"] == SENDER
    assert preview["to"] == [RECIPIENT]
    assert service._read(created)["wire"] == stored
    assert rig.smtp.calls == []


def test_create_draft_bridge_folded_encoded_from_keeps_stored_wire(rig, monkeypatch):
    name = Header("Plain " + "Réwritten Owner " * 12, "utf-8", header_name="From")
    rewritten_from = name.encode(linesep="\r\n") + f" <{SENDER}>"
    assert "\r\n " in rewritten_from
    client, fake, service = rewritten_roundtrip_client(rig, monkeypatch, rewritten_from)
    created = client.create_draft(RECIPIENT, subject="Synthetic draft", body="Plain body")
    stored = fake.folders["Drafts"]["7"].headers
    assert b"From: " + rewritten_from.encode() + b"\r\n" in stored
    preview = service.preview_send(created)
    assert preview["token"] and preview["from"] == SENDER
    assert preview["to"] == [RECIPIENT]
    assert service._read(created)["wire"] == stored
    assert rig.smtp.calls == []


@pytest.mark.parametrize("cte", [None, "base64", "quoted-printable"])
def test_create_draft_bridge_metadata_with_bare_from(rig, monkeypatch, cte):
    client, fake, service = rewritten_roundtrip_client(rig, monkeypatch, SENDER, cte=cte)
    created = client.create_draft(RECIPIENT, subject="Synthetic draft", body="Plain body")
    stored = fake.folders["Drafts"]["7"].headers
    assert b"Message-Id: " + created.encode() + b"\r\n" in stored
    assert b"References: <c3ludGhldGlj==@protonmail.internalid>\r\n" in stored
    assert BytesParser(policy=policy.SMTP).parsebytes(stored).get_content().strip() == "Plain body"
    preview = service.preview_send(created)
    assert preview["token"] and preview["from"] == SENDER
    assert preview["to"] == [RECIPIENT]
    assert service._read(created)["wire"] == stored
    assert rig.smtp.calls == []


def test_create_draft_bridge_long_bare_to_keeps_stored_wire(rig, monkeypatch):
    client, fake, service = rewritten_roundtrip_client(rig, monkeypatch, SENDER)
    recipient = "a" * 64 + "@example.test"
    created = client.create_draft(recipient, subject="Synthetic draft", body="Plain body")
    stored = fake.folders["Drafts"]["7"].headers
    assert b"To: " + recipient.encode() + b"\r\n" in stored
    preview = service.preview_send(created)
    assert preview["token"] and preview["from"] == SENDER
    assert preview["to"] == [recipient]
    assert service._read(created)["wire"] == stored
    assert rig.smtp.calls == []
