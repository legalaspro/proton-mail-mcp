"""forward_message writes one unsent draft; sending it takes only the guarded send path."""

import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock

import pytest
from attachment_fixtures import PNG, FakeImap, client, mail_with, part
from attachment_fixtures import settings as settings  # noqa: F401
from mcp.server.fastmcp.exceptions import ToolError
from test_attachment_drafts import SOURCE, files_of, parsed, sending_client
from test_send import DRAFT_ID, RECIPIENT, SENDER, Host, redeem
from test_send import local_certificate as local_certificate
from test_send import rig as rig
from test_send_token_approval import own_rig as own_rig

from proton_mail_mcp import attachments as attachments_module
from proton_mail_mcp.action_log import ActionLog
from proton_mail_mcp.mail import CallDeadlineExceeded, MailValidationError
from proton_mail_mcp.server import create_mcp
from proton_mail_mcp.views import forward_fields

PDF = b"%PDF-1.4 chosen \x00\xff bytes"


def source_mail(**options):
    return mail_with(
        SOURCE,
        part("chosen.pdf", "application/pdf", PDF),
        part("private.png", "image/png", PNG),
        part("also.txt", "text/plain", b"third"),
        subject=options.pop("subject", "Quarterly plan"),
        text=options.pop("text", b"Original text line\r\n"),
        **options,
    )


def forward(mail, **options):
    arguments = {"to": RECIPIENT, "attachments": [1], **options}
    return mail.forward_message(SOURCE, **arguments)


def test_a_forward_is_an_unsent_draft_with_exactly_the_chosen_attachments(rig, monkeypatch):
    mail, fake, _ = sending_client(rig, monkeypatch, source=source_mail())
    result = forward(mail, attachments=[1, 3], cc="copy@example.test", note="Please read.")
    assert result["draft_id"] == DRAFT_ID
    assert [row["filename"] for row in result["attachments"]] == ["chosen.pdf", "also.txt"]
    assert [row["filename"] for row in result["not_forwarded"]] == ["private.png"]
    assert "token" not in result and "preview_send" in result["next_steps"]
    [(folder, flags, _, raw)] = fake.appended
    assert (folder, flags) == ('"Drafts"', r"(\Draft)")
    assert files_of(raw) == [
        ("chosen.pdf", "application/pdf", PDF),
        ("also.txt", "text/plain", b"third"),
    ]
    assert b"private.png" not in raw
    message = parsed(raw)
    assert message.defects == []
    assert str(message["Subject"]) == "Fwd: Quarterly plan"
    assert str(message["To"]) == RECIPIENT and str(message["Cc"]) == "copy@example.test"
    assert message["In-Reply-To"] is None and message["References"] is None
    text = message.get_body(("plain",)).get_content().replace("\r\n", "\n")
    assert text.startswith("Please read.\n\n---------- Forwarded message ---------\n")
    assert "\nFrom: Sender <sender@example.com>\n" in text
    assert "\nSubject: Quarterly plan\n" in text and "\nOriginal text line\n" in text
    assert rig.smtp.calls == [], "forwarding itself never reaches SMTP"


def test_forwarding_no_attachment_must_be_chosen_and_then_copies_none(rig, monkeypatch):
    mail, fake, _ = sending_client(rig, monkeypatch, source=source_mail())
    result = forward(mail, attachments=[])
    assert result["attachments"] == [] and len(result["not_forwarded"]) == 3
    [(_, _, _, raw)] = fake.appended
    assert not parsed(raw).is_multipart()
    with pytest.raises(TypeError):
        mail.forward_message(SOURCE, to=RECIPIENT)


@pytest.mark.parametrize(
    "subject,expected",
    [
        ("Plan", "Fwd: Plan"),
        ("Fwd: Plan", "Fwd: Plan"),
        ("FW:Plan", "FW:Plan"),
        ("Re: Plan", "Fwd: Re: Plan"),
        ("Tabs\tand\r\nbreaks\x00", "Fwd: Tabs and breaks"),
        ("", "Fwd:"),
    ],
)
def test_forward_subjects_have_one_prefix_and_no_control_characters(subject, expected):
    fields = forward_fields({"subject": subject, "text": "x"}, "")
    assert fields[0] == expected


def test_an_html_only_original_is_forwarded_as_its_text(rig, monkeypatch):
    html = b'("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "7BIT" 40 1 NIL NIL NIL)'
    source = mail_with(SOURCE, subject="Plan")
    source.structure = html
    source.sections = {"1": b"<p>Rendered <b>words</b></p><script>x()</script>"}
    mail, fake, _ = sending_client(rig, monkeypatch, source=source)
    forward(mail, attachments=[])
    [(_, _, _, raw)] = fake.appended
    text = parsed(raw).get_body(("plain",)).get_content()
    assert "Rendered words" in text and "<script>" not in text and "<b>" not in text


def test_a_cut_original_is_marked_in_the_draft_and_the_result(rig, monkeypatch):
    long_text = b"x" * 70000
    mail, fake, _ = sending_client(rig, monkeypatch, source=source_mail(text=long_text))
    result = forward(mail, attachments=[])
    assert result["warnings"] == [
        "The original message is longer; only its beginning is in the draft."
    ]
    [(_, _, _, raw)] = fake.appended
    text = parsed(raw).get_body(("plain",)).get_content()
    assert "only its beginning is included" in text


def test_the_original_is_not_forwarded_from_a_read_that_timed_out(rig, monkeypatch):
    mail, fake, _ = sending_client(rig, monkeypatch, source=source_mail())
    real = mail.read_thread

    def timed_out(*args, **kwargs):
        return {**real(*args, **kwargs), "partial": True, "timed_out": True}

    monkeypatch.setattr(mail, "read_thread", timed_out)
    with pytest.raises(MailValidationError, match="lookup incomplete"):
        forward(mail)
    assert fake.appended == []


def test_the_original_is_not_forwarded_when_its_text_could_not_be_read(rig, monkeypatch):
    mail, fake, _ = sending_client(rig, monkeypatch, source=source_mail())
    real = fake.uid

    def failing(command, *args):
        if command == "FETCH" and "BODY.PEEK[1]" in args[1]:
            return "NO", [b"synthetic body failure"]
        return real(command, *args)

    monkeypatch.setattr(fake, "uid", failing)
    # The same read still lists the attachments: only a forward needs the text.
    assert mail.list_attachments(SOURCE)["attachments"]
    with pytest.raises(MailValidationError, match="lookup incomplete"):
        forward(mail)
    assert fake.appended == []


def test_a_forward_holds_at_most_as_many_files_as_any_draft(rig, monkeypatch):
    files = [part(f"f{index}.txt", "text/plain", b"x") for index in range(25)]
    mail, fake, _ = sending_client(rig, monkeypatch, source=mail_with(SOURCE, *files))
    with pytest.raises(MailValidationError, match="at most 20 attachments"):
        forward(mail, attachments=list(range(1, 22)))
    assert fake.appended == []
    assert fake.calls == [], "too many files refuse before reading any mail"
    assert len(forward(mail, attachments=list(range(1, 21)))["attachments"]) == 20


@pytest.mark.parametrize(
    "options,error",
    [
        ({"to": "not an address"}, "Recipients must be"),
        ({"to": ""}, "At least one recipient"),
        ({"cc": "not an address"}, "Recipients must be"),
        ({"attachments": [9]}, "No attachment with that index"),
        ({"attachments": [1, 1]}, "distinct positive"),
        ({"attachments": [0]}, "distinct positive"),
        ({"attachments": ["1"]}, "distinct positive"),
        ({"attachments": None}, "distinct positive"),
        ({"note": 7}, "note must be text"),
    ],
)
def test_bad_forward_arguments_write_no_draft(rig, monkeypatch, options, error):
    mail, fake, _ = sending_client(rig, monkeypatch, source=source_mail())
    with pytest.raises(MailValidationError, match=error):
        forward(mail, **options)
    assert fake.appended == []


@pytest.mark.parametrize(
    "inner,decoded_once_more",
    [
        ("=?utf-8?B?Li4vLi4vZXZpbC5leGU=?=", "../../evil.exe"),
        ("=?utf-8?q?x=E2=80=AEfdp.exe?=", "x" + chr(0x202E) + "fdp.exe"),
        ("report.pdf=?utf-8?q?.exe?=", "report.pdf.exe"),
    ],
)
def test_a_doubly_encoded_name_stays_literal_in_a_forwards_header(
    rig, monkeypatch, inner, decoded_once_more
):
    # The sender's name decodes once on reading. What that leaves must not decode again
    # when it is written to the draft, or the cleaning below would never have seen it.
    sender_name = "=?utf-8?B?" + base64.b64encode(inner.encode()).decode() + "?="
    source = mail_with(SOURCE, part(sender_name, "application/pdf", PDF))
    mail, fake, service = sending_client(rig, monkeypatch, source=source)
    created = forward(mail)["draft_id"]
    [(_, _, _, raw)] = fake.appended
    [(name, _, _)] = files_of(raw)
    assert name == inner.replace("=?", "=_")
    assert "=?" not in name and decoded_once_more not in name
    [file] = service.preview_send(created)["attachments"]
    assert file["name"] == name


def test_forwarding_a_missing_message_refuses(rig, monkeypatch):
    mail, fake, _ = sending_client(rig, monkeypatch, source=source_mail())
    with pytest.raises(MailValidationError, match="Message not found"):
        mail.forward_message("<absent@example.test>", RECIPIENT, attachments=[])
    assert fake.appended == []


def test_the_upload_cap_applies_to_forwarded_attachments(rig, monkeypatch):
    monkeypatch.setattr(attachments_module, "MAX_UPLOAD_BYTES", 8)
    mail, fake, _ = sending_client(rig, monkeypatch, source=source_mail())
    with pytest.raises(MailValidationError, match="limit of one draft"):
        forward(mail)
    assert fake.appended == []


def test_a_timeout_while_fetching_attachments_writes_no_draft(rig, monkeypatch):
    mail, fake, _ = sending_client(rig, monkeypatch, source=source_mail())

    def expired(*args, **kwargs):
        raise CallDeadlineExceeded("Mailbox call deadline reached")

    monkeypatch.setattr(attachments_module, "_fetch_parts", expired)
    with pytest.raises(CallDeadlineExceeded):
        forward(mail)
    assert fake.appended == []


def test_a_forward_is_sent_only_through_the_token_and_the_form(rig, monkeypatch):
    mail, _, service = sending_client(rig, monkeypatch, source=source_mail())
    draft_id = forward(mail, attachments=[1])["draft_id"]
    preview = service.preview_send(draft_id)
    assert preview["approval"] == "host_confirmation"
    assert [file["name"] for file in preview["attachments"]] == ["chosen.pdf"]
    assert preview["attachments"][0]["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert rig.smtp.calls == []
    host = Host()
    assert redeem(rig, preview["token"], host)["sent"] is True
    assert "chosen.pdf" in host.prompts[0]
    sender, recipients, delivered = rig.smtp.sent[0]
    assert (sender, recipients) == (SENDER, (RECIPIENT,))
    assert files_of(delivered) == [("chosen.pdf", "application/pdf", PDF)]


@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_declining_a_forward_sends_nothing(rig, monkeypatch, action):
    mail, _, service = sending_client(rig, monkeypatch, source=source_mail())
    preview = service.preview_send(forward(mail)["draft_id"])
    with pytest.raises(MailValidationError):
        redeem(rig, preview["token"], Host(action))
    assert rig.smtp.sent == []


def test_a_forward_shows_in_the_review_without_a_token(rig, monkeypatch):
    mail, _, service = sending_client(rig, monkeypatch, source=source_mail())
    review = service.review_send(forward(mail)["draft_id"])
    assert [file["name"] for file in review["attachments"]] == ["chosen.pdf"]
    assert "token" not in review and rig.tokens._grants == {}


def test_a_forward_or_attachment_to_your_own_address_still_needs_the_form(own_rig, monkeypatch):
    rig = own_rig
    mail, _, service = sending_client(rig, monkeypatch, source=source_mail())
    # A forward with no attachment.
    preview = service.preview_send(forward(mail, attachments=[])["draft_id"])
    assert preview["to"] == [RECIPIENT] and preview["approval"] == "host_confirmation"
    # A forward with one.
    preview = service.preview_send(forward(mail)["draft_id"])
    assert preview["approval"] == "host_confirmation"
    host = Host()
    assert redeem(rig, preview["token"], host)["sent"] is True
    assert len(host.prompts) == 1
    # Any draft with an attachment, whatever its subject.
    rig.smtp.sent.clear()
    mail.create_draft(
        RECIPIENT, subject="Note", body="B", attachments_from=SOURCE, attachment_indexes=[3]
    )
    assert service.preview_send(DRAFT_ID)["approval"] == "host_confirmation"
    # The own-address path itself is unchanged for a plain note.
    mail.create_draft(RECIPIENT, subject="Note", body="B")
    assert service.preview_send(DRAFT_ID)["approval"] == "token"


@pytest.mark.parametrize(
    "subject,approval",
    [
        ("Fwd: x", "host_confirmation"),
        ("FW: x", "host_confirmation"),
        ("fwd : x", "host_confirmation"),
        (" Fw:x", "host_confirmation"),
        ("Forward planning", "token"),
        ("Re: Fwd: x", "token"),
    ],
)
def test_the_own_address_path_refuses_forward_subjects(own_rig, monkeypatch, subject, approval):
    mail, _, service = sending_client(own_rig, monkeypatch)
    mail.create_draft(RECIPIENT, subject=subject, body="B")
    assert service.preview_send(DRAFT_ID)["approval"] == approval


def mcp_for(settings, fake, tmp_path):
    configured = replace(
        settings,
        token="forward-tests-token-" + "x" * 32,
        confirm_mutations=False,
        action_log_path=str(tmp_path / "log.jsonl"),
    )
    return create_mcp(configured, client(configured, fake)), configured


def call(mcp, name, arguments):
    return asyncio.run(mcp._tool_manager.call_tool(name, arguments, convert_result=True))


def test_the_tool_needs_a_choice_of_attachments_and_writes_no_draft_without_one(settings, tmp_path):
    fake = FakeImap({"INBOX": {"3": source_mail()}, "Drafts": {}})
    mcp, _ = mcp_for(settings, fake, tmp_path)
    with pytest.raises(ToolError):
        call(mcp, "forward_message", {"message_id": SOURCE, "to": RECIPIENT})
    for bad in ([0], ["x"], "all"):
        with pytest.raises(ToolError):
            call(
                mcp, "forward_message", {"message_id": SOURCE, "to": RECIPIENT, "attachments": bad}
            )
    assert fake.appended == []


def test_the_tool_is_an_audited_model_only_write_and_logs_no_content(settings, tmp_path):
    fake = FakeImap({"INBOX": {"3": source_mail()}, "Drafts": {}})
    mcp, configured = mcp_for(settings, fake, tmp_path)
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    forwarded = tools["forward_message"]
    assert forwarded.annotations.readOnlyHint is False
    assert forwarded.annotations.destructiveHint is False
    assert forwarded.meta == {"ui": {"visibility": ["model"]}}
    assert set(forwarded.inputSchema["required"]) == {"message_id", "to", "attachments"}
    call(
        mcp,
        "forward_message",
        {"message_id": SOURCE, "to": RECIPIENT, "attachments": [1], "note": "private note text"},
    )
    assert fake.appended
    with ActionLog(configured.action_log_path).transaction(readonly=True) as transaction:
        records = [row for row in transaction.records() if row.get("action") == "audit"]
    started, finished = records[-2:]
    assert started["tool"] == finished["tool"] == "forward_message"
    assert finished["outcome"] == "completed" and finished["attachment_count"] == 1
    assert finished["recipient_domains"] == ["example.test"]
    logged = json.dumps(records)
    assert "private note text" not in logged and "chosen.pdf" not in logged
    assert finished["message_id"]


def test_forwarding_sends_no_mail_from_the_tool_even_with_send_enabled(settings, tmp_path):
    fake = FakeImap({"INBOX": {"3": source_mail()}, "Drafts": {}})
    mcp, _ = mcp_for(replace(settings, enable_send=True), fake, tmp_path)
    smtp = Mock(side_effect=AssertionError("SMTP must not be used"))
    import proton_mail_mcp.send as send_module

    original = send_module.smtplib.SMTP
    send_module.smtplib.SMTP = smtp
    try:
        call(mcp, "forward_message", {"message_id": SOURCE, "to": RECIPIENT, "attachments": []})
    finally:
        send_module.smtplib.SMTP = original
    smtp.assert_not_called()
    assert len(fake.appended) == 1
    assert BytesParser(policy=policy.SMTP).parsebytes(fake.appended[0][3])["Subject"] == (
        "Fwd: Quarterly plan"
    )
