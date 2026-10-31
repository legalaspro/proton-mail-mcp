"""Guarded sending uses synthetic drafts, host replies, and recording SMTP doubles only."""

import asyncio
import hashlib
import json
import ssl
import time
from dataclasses import replace
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mcp.server.elicitation import AcceptedElicitation, CancelledElicitation, DeclinedElicitation
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ClientCapabilities, ElicitationCapability
from test_security import local_certificate as local_certificate

from proton_mail_mcp.action_log import ActionLog
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.guarded import TokenStore
from proton_mail_mcp.mail import MailError, MailValidationError
from proton_mail_mcp.security import BridgeSecurityError, Credentials
from proton_mail_mcp.send import SendService
from proton_mail_mcp.server import create_mcp

SENDER = "sender@example.test"
RECIPIENT = "recipient@example.test"
DRAFT_ID = "<draft@example.test>"
OTHER_DRAFT_ID = "<other-draft@example.test>"
BODY = "Synthetic body that must never enter the audit log."
SUBJECT = "Synthetic subject that must be hashed in the audit log"
PASSWORD = "synthetic-bridge-password-not-for-audit"
TOKEN = "synthetic-server-token-" + "x" * 32


def draft(
    *,
    sender=SENDER,
    to=RECIPIENT,
    cc=None,
    bcc=None,
    subject=SUBJECT,
    body=BODY,
    identifier=DRAFT_ID,
    attachment=None,
):
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = sender
    message["To"] = to
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    message["Subject"] = subject
    message["Message-ID"] = identifier
    message["Date"] = "Sun, 18 Oct 2026 12:00:00 +0000"
    message.set_content(body)
    if attachment is not None:
        message.add_attachment(
            attachment, maintype="application", subtype="octet-stream", filename="report.bin"
        )
        message.set_boundary("synthetic-boundary")
    return message.as_bytes(policy=policy.SMTP.clone(max_line_length=0))


class DraftClient:
    def __init__(self, settings, raw):
        self.settings = settings
        self.raw = raw
        self.reads = []
        self.identity = {"message_id": DRAFT_ID, "folder": "Drafts", "uidvalidity": "1", "uid": "7"}
        self.credentials_provider = Mock(return_value=Credentials(SENDER, PASSWORD))

    def read_draft(self, draft_id):
        self.reads.append(draft_id)
        if self.raw is None:
            raise MailValidationError("Draft is missing")
        return {**self.identity, "raw": self.raw}


class Host:
    def __init__(self, action="accept", confirm=True, *, support=True, during=None):
        capability = ElicitationCapability(form={}) if support else None
        self.session = SimpleNamespace(
            client_params=SimpleNamespace(capabilities=ClientCapabilities(elicitation=capability))
        )
        self.action, self.confirm, self.during = action, confirm, during
        self.prompts = []

    async def elicit(self, *, message, schema):
        self.prompts.append(message)
        if self.during:
            self.during()
        if isinstance(self.action, BaseException):
            raise self.action
        if self.action == "decline":
            return DeclinedElicitation()
        if self.action == "cancel":
            return CancelledElicitation()
        return AcceptedElicitation(data=schema.model_validate({"confirm": self.confirm}))


class SMTPRecorder:
    def __init__(self, der):
        self.der = der
        self.calls = []
        self.sent = []
        self.failure = None
        self.does_esmtp = False
        self.sock = SimpleNamespace(getpeercert=lambda **kwargs: self.der)

    def factory(self, *args, **kwargs):
        self.calls.append(("connect", args, kwargs))
        if self.failure == "connect":
            raise OSError(PASSWORD)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def ehlo(self):
        self.calls.append(("ehlo",))
        return 250, b"synthetic"

    def starttls(self, *, context):
        self.calls.append(("starttls", context))
        if self.failure == "tls":
            raise ssl.SSLCertVerificationError(PASSWORD)
        return 220, b"synthetic"

    def login(self, username, password):
        self.calls.append(("login",))
        assert username == SENDER
        assert password == PASSWORD
        return 235, b"synthetic"

    def sendmail(self, sender, recipients, raw, *args, **kwargs):
        self.calls.append(("sendmail",))
        if self.failure == "send":
            raise OSError(PASSWORD)
        self.sent.append((sender, tuple(recipients), raw))
        return {}

    def mail(self, sender, options=()):
        self.calls.append(("mail", sender, tuple(options)))
        self.sender, self.recipients = sender, []
        return 250, b"synthetic"

    def rcpt(self, recipient, options=()):
        self.calls.append(("rcpt", recipient, tuple(options)))
        self.recipients.append(recipient)
        return 250, b"synthetic"

    def data(self, raw):
        self.calls.append(("data",))
        if self.failure == "send":
            raise OSError(PASSWORD)
        self.sent.append((self.sender, tuple(self.recipients), raw))
        return 250, b"synthetic"

    def quit(self):
        self.calls.append(("quit",))
        return 221, b"synthetic"

    def rset(self):
        self.calls.append(("rset",))
        return 250, b"synthetic"

    def close(self):
        self.calls.append(("close",))


@pytest.fixture
def rig(tmp_path, monkeypatch, local_certificate):
    cert, der = local_certificate
    settings = Settings(
        token=TOKEN,
        bridge_user=SENDER,
        enable_send=True,
        imap_cert_path=str(cert),
        send_from_allowlist=(SENDER,),
        action_log_path=str(tmp_path / "actions.jsonl"),
    )
    client = DraftClient(settings, draft())
    smtp = SMTPRecorder(der)
    monkeypatch.setattr("smtplib.SMTP", smtp.factory)
    monkeypatch.setattr("smtplib.SMTP_SSL", smtp.factory)
    now = [time.time()]
    tokens = TokenStore(settings.send_token_ttl, clock=lambda: now[0])
    log = ActionLog(settings.action_log_path)
    service = SendService(client, settings, tokens=tokens, log=log, clock=lambda: now[0])
    return SimpleNamespace(
        settings=settings,
        client=client,
        smtp=smtp,
        now=now,
        service=service,
        tokens=tokens,
        log=log,
    )


def redeem(rig, token, host=None):
    return asyncio.run(rig.service.send_draft(token, host or Host()))


def use_draft(rig, identifier):
    rig.client.identity["message_id"] = identifier
    rig.client.raw = draft(identifier=identifier)


def records(rig):
    # The send journal; per-call audit records in the same log have their own tests.
    with rig.log.transaction(readonly=True) as transaction:
        return [record for record in transaction.records() if record.get("action") != "audit"]


def test_accepted_host_sends_exact_previewed_bytes_once(rig):
    preview = rig.service.preview_send(DRAFT_ID)
    host = Host()
    redeem(rig, preview["token"], host)
    assert rig.smtp.sent == [(SENDER, (RECIPIENT,), rig.client.raw)]
    assert sum(call[0] == "connect" for call in rig.smtp.calls) == 1
    # Suppress smtplib's default getfqdn lookup and machine-hostname disclosure.
    assert (
        next(call[2] for call in rig.smtp.calls if call[0] == "connect")["local_hostname"]
        == "localhost"
    )
    assert len(rig.client.reads) >= 4 and set(rig.client.reads) == {DRAFT_ID}
    assert len(host.prompts) == 1
    assert SENDER in host.prompts[0] and RECIPIENT in host.prompts[0]
    assert SUBJECT in host.prompts[0]
    before = list(rig.smtp.calls)
    with pytest.raises((MailError, MailValidationError)):
        redeem(rig, preview["token"])
    assert rig.smtp.calls == before


@pytest.mark.parametrize(
    "action,confirm,support",
    [
        ("accept", True, False),
        ("decline", True, True),
        ("cancel", True, True),
        ("accept", False, True),
        ("accept", "true", True),
        ("accept", 1, True),
        (TimeoutError(PASSWORD), True, True),
        (RuntimeError(PASSWORD), True, True),
        (asyncio.CancelledError(), True, True),
    ],
)
def test_confirmation_refusals_burn_token_without_smtp(rig, action, confirm, support, caplog):
    token = rig.service.preview_send(DRAFT_ID)["token"]
    host = Host(action, confirm, support=support)
    expected = (
        asyncio.CancelledError
        if isinstance(action, asyncio.CancelledError)
        else (MailError, MailValidationError)
    )
    with pytest.raises(expected) as caught:
        redeem(rig, token, host)
    assert rig.smtp.calls == []
    assert PASSWORD not in str(caught.value) + caplog.text
    with pytest.raises((MailError, MailValidationError)):
        redeem(rig, token, Host())
    assert rig.smtp.calls == []
    assert len(records(rig)) >= 2


def test_host_without_form_capability_cannot_elicit_or_send(rig):
    token = rig.service.preview_send(DRAFT_ID)["token"]
    host = Host()
    host.session.client_params.capabilities.elicitation = ElicitationCapability(url={})
    with pytest.raises((MailError, MailValidationError)):
        redeem(rig, token, host)
    assert host.prompts == []
    assert rig.smtp.calls == []


@pytest.mark.parametrize("stage", ["before_confirmation", "during_confirmation"])
@pytest.mark.parametrize(
    "change", ["body", "subject", "attachment", "gone", "uid", "identity", "from", "recipients"]
)
def test_draft_is_rechecked_on_both_sides_of_confirmation(rig, stage, change):
    token = rig.service.preview_send(DRAFT_ID)["token"]

    def mutate():
        if change == "gone":
            rig.client.raw = None
        elif change == "uid":
            rig.client.identity["uid"] = "8"
        elif change == "identity":
            rig.client.identity["message_id"] = "<other@example.test>"
        elif change == "body":
            rig.client.raw = draft(body="Changed body")
        elif change == "subject":
            rig.client.raw = draft(subject="Changed subject")
        elif change == "from":
            rig.client.raw = draft(sender="other@example.test")
        elif change == "recipients":
            rig.client.raw = draft(to=", ".join(f"to{i}@example.test" for i in range(11)))
        else:
            rig.client.raw = draft(attachment=b"new attachment")

    host = Host(during=mutate if stage == "during_confirmation" else None)
    if stage == "before_confirmation":
        mutate()
    with pytest.raises((MailError, MailValidationError)):
        redeem(rig, token, host)
    assert rig.smtp.calls == []
    assert len(host.prompts) == (stage == "during_confirmation")


def test_expired_token_never_prompts_or_connects(rig):
    token = rig.service.preview_send(DRAFT_ID)["token"]
    rig.now[0] += rig.settings.send_token_ttl + 1
    host = Host()
    with pytest.raises((MailError, MailValidationError)):
        redeem(rig, token, host)
    assert host.prompts == []
    assert rig.smtp.calls == []


def test_token_expiring_during_confirmation_cannot_send(rig):
    token = rig.service.preview_send(DRAFT_ID)["token"]
    host = Host(during=lambda: rig.now.__setitem__(0, rig.now[0] + 1000))
    with pytest.raises((MailError, MailValidationError)):
        redeem(rig, token, host)
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "sender",
    [
        "other@example.test",
        "Sender@example.test",
        "sender@EXAMPLE.test",
        "sеnder@example.test",
        "sender@exаmple.test",
        "sender@example.test <other@example.test>",
        "sender@example.test, other@example.test",
        "<sender@example.test>",
        "sender@example.test (Other)",
        "(Other) sender@example.test",
        "sender(comment)@example.test",
        '"sender"@example.test',
    ],
)
def test_from_requires_exact_bare_ascii_allowlisted_address(rig, sender):
    rig.client.raw = draft().replace(
        b"From: " + SENDER.encode("ascii"), b"From: " + sender.encode("utf-8")
    )
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "header",
    [
        b"From: other@example.test\r\n",
        b"Sender: other@example.test\r\n",
        b"Sender: other@example.test\r\nSender: sender@example.test\r\n",
        b"To: extra@example.test\r\n",
        b"Subject: hidden subject\r\n",
    ],
)
def test_ambiguous_sender_and_duplicate_headers_refused(rig, header):
    rig.client.raw = header + draft()
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert rig.smtp.calls == []


@pytest.mark.parametrize("field", [b"From", b"To", b"Cc", b"Bcc", b"Subject"])
@pytest.mark.parametrize("injection", [b"\r\n injected", b"\rmalformed", b"\nmalformed"])
def test_address_and_subject_controls_refuse_without_smtp(rig, field, injection):
    raw = draft(cc="carbon@example.test", bcc="blind@example.test")
    lines = raw.split(b"\r\n")
    rig.client.raw = b"\r\n".join(
        line + injection if line.startswith(field + b":") else line for line in lines
    )
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert rig.smtp.calls == []


def test_recipient_cap_includes_to_cc_and_bcc(rig):
    rig.client.raw = draft(
        to=", ".join(f"to{i}@example.test" for i in range(8)),
        cc="carbon@example.test, another@example.test",
        bcc="blind@example.test",
    )
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert rig.smtp.calls == []


def test_tokens_store_digests_without_plaintext(rig):
    token = rig.service.preview_send(DRAFT_ID)["token"]
    assert list(rig.tokens._grants) == [hashlib.sha256(token.encode("ascii")).digest()]
    assert token not in repr(rig.tokens._grants)


def test_default_tool_list_has_no_send_tools(rig):
    settings = replace(rig.settings, enable_send=False)
    mcp = create_mcp(settings, rig.client)
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {"preview_send", "send_draft"}.isdisjoint(names)
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "extra",
    [
        {"confirm": True},
        {"confirmed": True},
        {"confirmation": "accept"},
        {"ctx": {"action": "accept", "confirm": True}},
        {"token": 123},
        {"token": ["synthetic"]},
    ],
)
def test_caller_cannot_supply_confirmation_or_draft_override(rig, extra, monkeypatch):
    monkeypatch.setattr("proton_mail_mcp.server.SendService", lambda *args: rig.service)
    mcp = create_mcp(rig.settings, rig.client)
    tool = mcp._tool_manager.get_tool("send_draft")
    assert set(tool.parameters["properties"]) == {"token", "draft_id"}
    token = rig.service.preview_send(DRAFT_ID)["token"]
    host = Host()
    with pytest.raises((ToolError, ValueError)):
        asyncio.run(
            mcp._tool_manager.call_tool("send_draft", {"token": token, **extra}, context=host)
        )
    assert host.prompts == []
    assert records(rig) == []
    assert rig.smtp.calls == []
    assert rig.tokens.inspect(token, "send_draft").selector == {"draft_id": DRAFT_ID}


@pytest.mark.parametrize(
    "host",
    [
        "192.0.2.1",
        "bridge.example.test",
        "localhost",
        "127.0.0.2",
        "0.0.0.0",
        "::",
        "::ffff:127.0.0.1",
    ],
)
def test_nonliteral_loopback_smtp_hosts_refuse_before_connect(rig, host):
    with pytest.raises((ConfigError, MailError, MailValidationError)):
        settings = replace(rig.settings, smtp_host=host)
        service = SendService(rig.client, settings)
        token = service.preview_send(DRAFT_ID)["token"]
        asyncio.run(service.send_draft(token, Host()))
    assert rig.smtp.calls == []


@pytest.mark.parametrize("cert", [None, "missing", "invalid"])
def test_missing_or_invalid_pin_refuses_before_smtp(rig, tmp_path, cert):
    path = None
    if cert is not None:
        path = str(tmp_path / "bad.pem")
        if cert == "invalid":
            (tmp_path / "bad.pem").write_text("not a PEM certificate")
    with pytest.raises((ConfigError, BridgeSecurityError, MailError, MailValidationError)):
        settings = replace(rig.settings, imap_cert_path=path)
        service = SendService(rig.client, settings)
        token = service.preview_send(DRAFT_ID)["token"]
        asyncio.run(service.send_draft(token, Host()))
    assert rig.smtp.calls == []


@pytest.mark.parametrize("failure", ["tls", "peer_pin"])
def test_peer_certificate_failure_never_authenticates_or_sends(rig, failure):
    if failure == "tls":
        rig.smtp.failure = "tls"
    else:
        rig.smtp.der = b"wrong certificate"
    token = rig.service.preview_send(DRAFT_ID)["token"]
    result = redeem(rig, token)
    assert result["result"] == "smtp_error"
    assert not any(call[0] in {"login", "sendmail"} for call in rig.smtp.calls)
    assert rig.smtp.sent == []


def test_transport_verifies_certificates_by_default(rig):
    token = rig.service.preview_send(DRAFT_ID)["token"]
    redeem(rig, token)
    context = next(call[1] for call in rig.smtp.calls if call[0] == "starttls")
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert rig.settings.smtp_allow_unverified is False


def test_audit_never_contains_content_addresses_secrets_or_confirmation(rig, caplog):
    preview = rig.service.preview_send(DRAFT_ID)
    redeem(rig, preview["token"])
    entries = records(rig)
    assert entries
    visible = json.dumps(entries) + caplog.text
    for forbidden in (BODY, SUBJECT, SENDER, RECIPIENT, PASSWORD, TOKEN, preview["token"]):
        assert forbidden not in visible
    for entry in entries:
        assert entry["action"].startswith("send")
        assert entry["timestamp"]
        assert entry["draft_hash"] == preview["draft_hash"]
        assert entry["subject_hash"] == hashlib.sha256(SUBJECT.encode()).hexdigest()
        assert entry["recipient_domains"] == ["example.test"]
        assert entry["recipient_count"] == 1
        assert entry["message_id"] == DRAFT_ID
        assert entry["result"] in {"reserved", "sent"}
    assert DRAFT_ID in visible
    assert hashlib.sha256(SUBJECT.encode()).hexdigest() in visible


def test_preview_includes_attachment_digests_all_recipients_and_caps(rig):
    content = b"synthetic attachment\x00payload"
    rig.client.raw = draft(cc="carbon@example.test", bcc="blind@example.test", attachment=content)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["from"] == SENDER
    assert preview["to"] == [RECIPIENT]
    assert preview["cc"] == ["carbon@example.test"]
    assert preview["bcc"] == ["blind@example.test"]
    assert preview["subject"] == SUBJECT
    assert preview["attachments"] == [
        {
            "name": "report.bin",
            "type": "application/octet-stream",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    ]
    assert len(preview["draft_hash"]) == 64
    assert preview["caps"] == {
        "messages_per_call": 1,
        "recipients_per_message": 10,
        "daily_sends": 20,
        "remaining_daily_sends": 20,
        "window_hours": 24,
    }
    assert rig.smtp.calls == []
    host = Host()
    redeem(rig, preview["token"], host)
    assert preview["draft_hash"][:16] in host.prompts[0]
    assert "report.bin" in host.prompts[0]
    sender, recipients, raw = rig.smtp.sent[0]
    assert sender == SENDER
    assert recipients == (RECIPIENT, "carbon@example.test", "blind@example.test")
    delivered = BytesParser(policy=policy.default).parsebytes(raw)
    assert delivered.get("Bcc") is None
    assert next(delivered.iter_attachments()).get_payload(decode=True) == content


def test_attachment_bytes_and_bcc_participate_in_draft_hash(rig):
    hashes = []
    for content, bcc in [
        (b"first", "one@example.test"),
        (b"other", "one@example.test"),
        (b"other", "two@example.test"),
    ]:
        rig.client.raw = draft(attachment=content, bcc=bcc)
        hashes.append(rig.service.preview_send(DRAFT_ID)["draft_hash"])
    assert len(set(hashes)) == 3
    assert rig.smtp.calls == []


def test_daily_budget_persists_across_restart_and_expires_after_rolling_day(rig):
    settings = replace(rig.settings, send_daily_cap=1)
    service = SendService(rig.client, settings, log=rig.log, clock=lambda: rig.now[0])
    first = service.preview_send(DRAFT_ID)["token"]
    asyncio.run(service.send_draft(first, Host()))
    assert len(rig.smtp.sent) == 1
    rig.smtp.calls.clear()
    restarted = SendService(
        rig.client, settings, log=ActionLog(settings.action_log_path), clock=lambda: rig.now[0]
    )
    with pytest.raises((MailError, MailValidationError), match="budget"):
        restarted.preview_send(DRAFT_ID)
    assert rig.smtp.calls == []
    rig.now[0] += 86400 + 1
    preview = restarted.preview_send(DRAFT_ID)
    assert preview["caps"]["remaining_daily_sends"] == 1
    assert rig.smtp.calls == []


def test_preissued_token_cannot_exceed_daily_total(rig):
    settings = replace(rig.settings, send_daily_cap=1)
    service = SendService(rig.client, settings, log=rig.log)
    first = service.preview_send(DRAFT_ID)["token"]
    use_draft(rig, OTHER_DRAFT_ID)
    second = service.preview_send(OTHER_DRAFT_ID)["token"]
    use_draft(rig, DRAFT_ID)
    asyncio.run(service.send_draft(first, Host()))
    use_draft(rig, OTHER_DRAFT_ID)
    rig.smtp.calls.clear()
    host = Host()
    with pytest.raises((MailError, MailValidationError), match="budget"):
        asyncio.run(service.send_draft(second, host))
    assert host.prompts == []
    assert rig.smtp.calls == []


def test_confirmation_decline_does_not_spend_daily_budget(rig):
    token = rig.service.preview_send(DRAFT_ID)["token"]
    with pytest.raises((MailError, MailValidationError)):
        redeem(rig, token, Host("decline"))
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["caps"]["remaining_daily_sends"] == rig.settings.send_daily_cap
    assert rig.smtp.calls == []


def test_smtp_error_reservation_survives_restart_and_never_leaks_errors(rig, caplog):
    settings = replace(rig.settings, send_daily_cap=1)
    service = SendService(rig.client, settings, log=rig.log)
    rig.smtp.failure = "send"
    token = service.preview_send(DRAFT_ID)["token"]
    result = asyncio.run(service.send_draft(token, Host()))
    assert result["result"] == "smtp_error"
    assert result["token_consumed"] is True
    assert PASSWORD not in json.dumps(result) + json.dumps(records(rig)) + caplog.text
    rig.smtp.calls.clear()
    restarted = SendService(rig.client, settings, log=ActionLog(settings.action_log_path))
    with pytest.raises((MailError, MailValidationError), match="budget"):
        restarted.preview_send(DRAFT_ID)
    assert rig.smtp.calls == []


def test_intent_is_durable_before_smtp_connection(rig, monkeypatch):
    original = rig.smtp.factory

    def connect(*args, **kwargs):
        # A separate reader cannot acquire the active lock, so inspect the
        # fixture journal bytes directly to establish ordering before transport.
        lines = rig.log.path.read_text().splitlines()
        assert any(json.loads(line).get("budget_reserved") is True for line in lines)
        return original(*args, **kwargs)

    monkeypatch.setattr("smtplib.SMTP", connect)
    token = rig.service.preview_send(DRAFT_ID)["token"]
    assert redeem(rig, token)["sent"] is True


def test_unavailable_journal_prevents_smtp(rig, monkeypatch):
    token = rig.service.preview_send(DRAFT_ID)["token"]

    def unavailable(*args, **kwargs):
        raise MailError("Synthetic unavailable journal")

    monkeypatch.setattr(rig.log, "transaction", unavailable)
    with pytest.raises((MailError, MailValidationError)):
        redeem(rig, token)
    assert rig.smtp.calls == []


def test_daily_budget_is_rechecked_after_confirmation(rig):
    token = rig.service.preview_send(DRAFT_ID)["token"]

    def exhaust():
        with rig.log.transaction() as transaction:
            for _ in range(rig.settings.send_daily_cap):
                record = rig.service._record(None, "reserved")
                record["budget_reserved"] = True
                transaction.append(record)

    with pytest.raises((MailError, MailValidationError), match="budget"):
        redeem(rig, token, Host(during=exhaust))
    assert rig.smtp.calls == []


@pytest.mark.parametrize("tls", [False, True])
def test_both_transports_only_connect_to_literal_loopback(rig, tls):
    rig.service.settings = replace(rig.settings, smtp_host="::1", smtp_tls=tls)
    token = rig.service.preview_send(DRAFT_ID)["token"]
    assert redeem(rig, token)["sent"] is True
    connection = rig.smtp.calls[0]
    assert connection[0] == "connect" and connection[1] == ("::1", 1025)
    assert sum(call[0] == "starttls" for call in rig.smtp.calls) == (not tls)
    if tls:
        assert connection[2]["context"].verify_mode == ssl.CERT_REQUIRED


def test_unverified_smtp_needs_explicit_configuration_opt_out(rig):
    rig.service.settings = replace(rig.settings, imap_cert_path=None, smtp_allow_unverified=True)
    token = rig.service.preview_send(DRAFT_ID)["token"]
    assert redeem(rig, token)["sent"] is True
    context = next(call[1] for call in rig.smtp.calls if call[0] == "starttls")
    assert context.verify_mode == ssl.CERT_NONE


@pytest.mark.parametrize("stage", ["read", "credentials"])
def test_backend_diagnostics_are_redacted_and_audit_retains_preview_identity(
    rig, monkeypatch, stage
):
    preview = rig.service.preview_send(DRAFT_ID)
    failure = Mock(side_effect=MailValidationError(PASSWORD))
    monkeypatch.setattr(
        rig.client, "read_draft" if stage == "read" else "credentials_provider", failure
    )
    with pytest.raises((MailError, MailValidationError)) as caught:
        redeem(rig, preview["token"])
    assert PASSWORD not in str(caught.value) + json.dumps(records(rig))
    assert records(rig)[-1]["draft_hash"] == preview["draft_hash"]
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(action="unknown", data=SimpleNamespace(confirm=True)),
        SimpleNamespace(action="accept", data=SimpleNamespace()),
        SimpleNamespace(action="accept", data=None),
        SimpleNamespace(action="accept", data=SimpleNamespace(confirm="true")),
        SimpleNamespace(action="accept", data=SimpleNamespace(confirm=1)),
    ],
)
def test_only_explicit_accept_with_boolean_confirm_can_send(rig, response):
    token = rig.service.preview_send(DRAFT_ID)["token"]
    host = Host()

    async def invalid_response(**kwargs):
        return response

    host.elicit = invalid_response
    with pytest.raises((MailError, MailValidationError)):
        redeem(rig, token, host)
    assert rig.smtp.calls == []
