"""The typed-OK send path for clients with no form: synthetic drafts, real MCP sessions."""

import asyncio
import base64
import io
import json
import logging
import secrets
import smtplib
import sys
from dataclasses import replace
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    ElicitationCapability,
    ElicitResult,
    ErrorData,
)
from test_send import (
    BODY,
    DRAFT_ID,
    OTHER_DRAFT_ID,
    PASSWORD,
    RECIPIENT,
    SENDER,
    SUBJECT,
    TOKEN,
    Host,
    draft,
    local_certificate,  # noqa: F401
    records,
    redeem,
    use_draft,
)
from test_send import rig as _send_rig
from test_send_http import _HTTP
from test_send_repeat import lost_reply
from test_send_token_approval import own_rig as _own_rig
from test_unsubscribe import IDENTIFIER
from test_unsubscribe_server import setup_tools

from proton_mail_mcp import send as send_module
from proton_mail_mcp import views
from proton_mail_mcp.confirmation import ConfirmationError, declares_no_form, form_available
from proton_mail_mcp.mail import MailError, MailValidationError
from proton_mail_mcp.server import create_app, create_mcp

rig = _send_rig
own_rig = _own_rig
FAILURES = (MailError, MailValidationError)
ALIAS = "alias@example.test"
OUTSIDE = "outside@example.test"
TYPED_OK_TEXT = ("typed_ok", "explicit OK", "cannot prove", "changed draft and a replay")
NO_FORM_LINE = "This app has no confirmation form, so type OK to send exactly this."
# What a declared capability cannot be read from. None of these may open the typed OK.
UNKNOWN_CLIENTS = [
    pytest.param(SimpleNamespace(session=SimpleNamespace(client_params=None)), id="no-initialize"),
    pytest.param(
        SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace(capabilities=None))),
        id="no-capabilities",
    ),
    pytest.param(
        SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace())),
        id="no-attribute",
    ),
    pytest.param(
        SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(
                    capabilities=SimpleNamespace(elicitation=SimpleNamespace())
                )
            )
        ),
        id="capability-without-modes",
    ),
]


def typed_ok(rig):
    preview = rig.service.preview_send(rig.client.identity["message_id"], form=False)
    assert preview["approval"] == "typed_ok"
    return preview


def send(rig, preview, host=None, draft_id=None):
    named = draft_id or preview["draft_id"]
    return asyncio.run(rig.service.send_draft(preview["token"], host or Host(support=False), named))


def assert_refused(rig, preview, result, **kwargs):
    sent = len(rig.smtp.sent)
    with pytest.raises(FAILURES):
        send(rig, preview, **kwargs)
    assert len(rig.smtp.sent) == sent
    assert records(rig)[-1]["result"] == result


async def call(http, name, **arguments):
    response = await http.rpc("tools/call", {"name": name, "arguments": arguments})
    return response["result"]


async def http_session(rig, app, capabilities):
    http = _HTTP(app, rig.settings.token)
    await http.initialize(capabilities)
    return http


def switched_off(rig):
    rig.settings = replace(rig.service.settings, send_typed_ok=False)
    rig.service.settings = rig.settings
    return rig


def typed_ok_warnings(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == send_module.__name__ and record.levelno == logging.WARNING
    ]


def mime_draft(build, **headers):
    """A draft another mail client saved; create_draft and forward_message never write these."""
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = SENDER
    message["To"] = RECIPIENT
    for name, value in headers.items():
        message[name.replace("_", "-")] = value
    message["Subject"] = SUBJECT
    message["Message-ID"] = DRAFT_ID
    message["Date"] = "Sun, 18 Oct 2026 12:00:00 +0000"
    build(message)
    return message.as_bytes(policy=policy.SMTP.clone(max_line_length=0))


def test_a_form_host_keeps_the_form_path_unchanged(rig):
    async def run():
        prompts = []

        async def answer(ctx, params):
            prompts.append(params.message)
            assert rig.smtp.calls == []
            return ElicitResult(action="accept", content={"confirm": True})

        server = create_mcp(rig.settings, rig.client)
        async with create_connected_server_and_client_session(
            server, elicitation_callback=answer
        ) as client:
            preview = await client.call_tool("preview_send", {"draft_id": DRAFT_ID})
            payload = preview.structuredContent
            assert payload["approval"] == "host_confirmation"
            assert not {"summary", "next_steps"} & set(payload)
            sent = await client.call_tool("send_draft", {"token": payload["token"]})
            assert sent.structuredContent["sent"] is True
            assert len(prompts) == 1 and payload["draft_hash"][:16] in prompts[0]

    asyncio.run(run())
    assert len(rig.smtp.sent) == 1
    assert [row["approval"] for row in records(rig) if row.get("budget_reserved")] == [
        "host_confirmation"
    ]


@pytest.mark.parametrize("decision", ["decline", "cancel"])
def test_a_person_refusing_the_form_stays_refused_and_keeps_the_form(rig, decision):
    async def run():
        async def answer(ctx, params):
            return ElicitResult(action=decision)

        server = create_mcp(rig.settings, rig.client)
        async with create_connected_server_and_client_session(
            server, elicitation_callback=answer
        ) as client:
            first = await client.call_tool("preview_send", {"draft_id": DRAFT_ID})
            refused = await client.call_tool(
                "send_draft", {"token": first.structuredContent["token"]}
            )
            assert refused.isError is True
            second = await client.call_tool("preview_send", {"draft_id": DRAFT_ID})
            assert second.structuredContent["approval"] == "host_confirmation"

    asyncio.run(run())
    assert rig.smtp.calls == []


def test_an_empty_elicitation_capability_means_a_form_as_the_specification_says(rig):
    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = await http_session(rig, app, {"elicitation": {}})
            http.on_elicit = lambda prompt: {"action": "accept", "content": {"confirm": True}}
            preview = (await call(http, "preview_send", draft_id=DRAFT_ID))["structuredContent"]
            assert preview["approval"] == "host_confirmation" and "summary" not in preview
            sent = await call(http, "send_draft", token=preview["token"])
            assert sent["structuredContent"]["sent"] is True
            assert len(http.elicitations) == 1

    asyncio.run(run())
    assert len(rig.smtp.sent) == 1


@pytest.mark.parametrize(
    "capabilities",
    [{}, {"elicitation": None}, {"sampling": {}}],
    ids=["no-elicitation", "null-elicitation", "sampling-only"],
)
def test_a_host_with_no_form_gets_typed_ok_and_sends_without_any_form(rig, capabilities):
    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = await http_session(rig, app, capabilities)
            result = await call(http, "preview_send", draft_id=DRAFT_ID)
            preview = result["structuredContent"]
            assert preview["approval"] == "typed_ok"
            assert preview["token"] and preview["expires_at"]
            assert RECIPIENT in preview["summary"] and SUBJECT in preview["summary"]
            assert preview["summary"].splitlines()[-1] == NO_FORM_LINE
            assert preview["next_steps"].startswith(
                "This app has no confirmation form, so the user types OK to send."
            )
            assert "explicit OK" in preview["next_steps"]
            assert "in a code block" in preview["next_steps"]
            assert rig.smtp.calls == []
            sent = await call(
                http, "send_draft", token=preview["token"], draft_id=preview["draft_id"]
            )
            assert sent["structuredContent"]["sent"] is True
            assert http.elicitations == []

    asyncio.run(run())
    assert rig.smtp.sent == [(SENDER, (RECIPIENT,), rig.client.raw)]
    assert [row["approval"] for row in records(rig) if row.get("budget_reserved")] == ["typed_ok"]


@pytest.mark.parametrize(
    "capabilities",
    [{"elicitation": {"url": {}}}, {"elicitation": {"form": None, "url": {}}}],
    ids=["url-only", "null-form-with-url"],
)
def test_a_client_that_declares_elicitation_but_no_form_is_refused_and_never_falls_back(
    rig, capabilities
):
    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = await http_session(rig, app, capabilities)
            preview = (await call(http, "preview_send", draft_id=DRAFT_ID))["structuredContent"]
            assert preview["approval"] == "host_confirmation"
            assert not {"summary", "next_steps"} & set(preview)
            refused = await call(http, "send_draft", token=preview["token"], draft_id=DRAFT_ID)
            assert refused["isError"] is True
            assert "did not show a form" in refused["content"][0]["text"]
            assert http.elicitations == []
            review = await call(http, "review_send", draft_id=DRAFT_ID)
            assert json.loads(review["content"][0]["text"])["approval"] == "host_confirmation"

    asyncio.run(run())
    assert rig.smtp.calls == []
    assert records(rig)[-1]["result"] == "confirmation_unavailable"


def test_the_off_switch_restores_the_old_refusal(rig, caplog):
    switched_off(rig)

    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = await http_session(rig, app, {})
            preview = (await call(http, "preview_send", draft_id=DRAFT_ID))["structuredContent"]
            assert preview["approval"] == "host_confirmation"
            assert not {"summary", "next_steps"} & set(preview)
            review = await call(http, "review_send", draft_id=DRAFT_ID)
            assert json.loads(review["content"][0]["text"])["approval"] == "host_confirmation"
            refused = await call(http, "send_draft", token=preview["token"], draft_id=DRAFT_ID)
            assert refused["isError"] is True
            assert http.elicitations == []

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    assert rig.smtp.calls == []
    assert records(rig)[-1]["result"] == "confirmation_unavailable"
    assert typed_ok_warnings(caplog) == []


def test_a_typed_ok_token_from_before_the_off_switch_refuses(rig):
    preview = typed_ok(rig)
    switched_off(rig)
    assert_refused(rig, preview, "confirmation_unavailable")
    assert rig.smtp.calls == []


def test_the_off_switch_leaves_the_form_and_the_own_address_path_alone(own_rig):
    rig = switched_off(own_rig)
    rig.client.raw = draft(to=OUTSIDE)
    host = Host()
    preview = rig.service.preview_send(DRAFT_ID, form=True)
    assert preview["approval"] == "host_confirmation"
    assert send(rig, preview, host)["sent"] is True
    assert len(host.prompts) == 1
    rig.client.raw = draft(to=RECIPIENT, subject="A note to myself")
    own = rig.service.preview_send(DRAFT_ID, form=False)
    assert own["approval"] == "token"
    assert send(rig, own)["sent"] is True
    assert len(rig.smtp.sent) == 2


def test_each_typed_ok_step_warns_with_the_client_and_never_the_token(rig, caplog):
    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = await http_session(rig, app, {})
            preview = (await call(http, "preview_send", draft_id=DRAFT_ID))["structuredContent"]
            assert len(typed_ok_warnings(caplog)) == 1
            await call(http, "send_draft", token=preview["token"], draft_id=DRAFT_ID)
            return preview["token"]

    with caplog.at_level(logging.WARNING):
        token = asyncio.run(run())
    assert len(rig.smtp.sent) == 1
    first, second = typed_ok_warnings(caplog)
    assert "preview_send issued a token" in first
    assert "send_draft is sending with no form" in second
    for line in (first, second):
        assert 'client name "offline-tests", version "1"' in line
        assert "PROTON_MCP_SEND_TYPED_OK=0" in line
        assert token not in line


def test_a_refused_typed_ok_send_warns_only_for_its_preview(rig, caplog):
    with caplog.at_level(logging.WARNING):
        preview = typed_ok(rig)
        rig.client.raw = draft(subject="Changed subject")
        assert_refused(rig, preview, "draft_changed")
    assert len(typed_ok_warnings(caplog)) == 1


def test_a_form_client_never_warns_about_typed_ok(rig, caplog):
    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = await http_session(rig, app, {"elicitation": {"form": {}}})
            http.on_elicit = lambda prompt: {"action": "accept", "content": {"confirm": True}}
            preview = (await call(http, "preview_send", draft_id=DRAFT_ID))["structuredContent"]
            await call(http, "send_draft", token=preview["token"])

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    assert len(rig.smtp.sent) == 1
    assert typed_ok_warnings(caplog) == []


def test_a_client_name_cannot_forge_a_log_line(rig, caplog):
    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = _HTTP(app, rig.settings.token)
            await http.rpc(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "tool\nForged: line", "version": "2\r\nForged: too"},
                },
            )
            await http.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            await call(http, "preview_send", draft_id=DRAFT_ID)

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    [line] = typed_ok_warnings(caplog)
    assert "\n" not in line and "\r" not in line
    assert '"tool\\nForged: line"' in line and '"2\\r\\nForged: too"' in line


@pytest.mark.parametrize(
    "made,kept",
    [
        ({"to": OUTSIDE}, OUTSIDE),
        ({"to": RECIPIENT, "attachment": b"file bytes"}, RECIPIENT),
        ({"to": RECIPIENT, "subject": "Fwd: a note"}, RECIPIENT),
        ({"to": OUTSIDE, "subject": "Fwd: a note", "attachment": b"file bytes"}, OUTSIDE),
    ],
    ids=["external", "self-with-attachment", "self-forward", "external-forward"],
)
def test_every_send_a_form_would_guard_takes_typed_ok(own_rig, made, kept):
    rig = own_rig
    rig.client.raw = draft(**made)
    preview = typed_ok(rig)
    assert kept in preview["summary"]
    assert send(rig, preview)["sent"] is True
    assert rig.smtp.sent == [(SENDER, (kept,), rig.client.raw)]


def test_the_own_address_path_is_unchanged_without_a_form(own_rig):
    rig = own_rig
    rig.client.raw = draft(to=RECIPIENT)

    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = await http_session(rig, app, {})
            result = await call(http, "preview_send", draft_id=DRAFT_ID)
            preview = result["structuredContent"]
            assert preview["approval"] == "token"
            assert not {"summary", "next_steps"} & set(preview)
            sent = await call(http, "send_draft", token=preview["token"])
            assert sent["structuredContent"]["sent"] is True
            assert http.elicitations == []

    asyncio.run(run())
    assert len(rig.smtp.sent) == 1


def test_a_form_token_on_a_client_without_one_still_refuses(rig):
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "host_confirmation"
    with pytest.raises(FAILURES):
        redeem(rig, preview["token"], Host(support=False))
    assert rig.smtp.calls == []
    assert records(rig)[-1]["result"] == "confirmation_unavailable"


def test_the_summary_is_plain_text_of_every_field(rig):
    rig.settings = replace(rig.settings, send_from_allowlist=(SENDER, ALIAS))
    rig.service.settings = rig.client.settings = rig.settings
    message = BytesParser(policy=policy.SMTP).parsebytes(
        draft(
            sender=ALIAS,
            to=f"{RECIPIENT}, {OUTSIDE}",
            cc="copy@example.test",
            bcc="hidden@example.test",
            subject="Quarterly plan",
            body="First line\n\nThird line",
            attachment=b"x" * 10,
        )
    )
    message.add_attachment(b"a,b", maintype="text", subtype="csv", filename="second.csv")
    rig.client.raw = message.as_bytes(policy=policy.SMTP.clone(max_line_length=0))
    preview = typed_ok(rig)
    assert preview["summary"] == "\n".join(
        [
            "Send this email? Sending cannot be undone.",
            f"From: {ALIAS} (an alias, not the account's own address)",
            f"To: {RECIPIENT}, {OUTSIDE}",
            "Cc: copy@example.test",
            "Bcc: hidden@example.test",
            "Subject: Quarterly plan",
            "Attachments (2):",
            '- "report.bin" (application/octet-stream, 10 bytes decoded)',
            '- "second.csv" (text/csv, 3 bytes decoded)',
            f"SHA-256 prefix: {preview['draft_hash'][:16]}",
            "Body:",
            "> First line",
            "> ",
            "> Third line",
            NO_FORM_LINE,
        ]
    )
    assert preview["attachments"][0]["size"] == 10


def test_the_summary_says_when_there_is_nothing_or_only_a_part_to_show(rig, monkeypatch):
    rig.client.raw = draft(body="y" * 1001)
    long = typed_ok(rig)["summary"].splitlines()
    assert "Body, first 1000 of 1001 characters only:" in long
    assert "> " + "y" * 1000 in long
    assert "Cc: none" in long and "Bcc: none" in long and "Attachments: none" in long
    rig.client.raw = draft(body="y" * 1000)
    assert "Body:" in typed_ok(rig)["summary"].splitlines()
    rig.client.raw = draft(body="")
    assert "Body: empty" in typed_ok(rig)["summary"].splitlines()
    monkeypatch.setattr(
        send_module,
        "_body_preview",
        lambda wire: {"type": None, "text": "", "truncated": False, "available": False},
    )
    assert "Body: could not be read for this summary" in typed_ok(rig)["summary"]


def test_the_summary_says_how_much_of_the_body_it_hides(rig):
    # Past both limits, so the total cannot come from the 20000-character preview.
    rig.client.raw = draft(body="y" * 50000)
    preview = typed_ok(rig)
    lines = preview["summary"].splitlines()
    assert "Body, first 1000 of 50000 characters only:" in lines
    assert "> " + "y" * 1000 in lines and "> " + "y" * 1001 not in lines
    review = rig.service.review_send(DRAFT_ID, form=False)["body_preview"]
    assert review["length"] == 50000 and len(review["text"]) == 20000


def test_the_summary_prints_the_whole_subject(rig):
    subject = "Start " + "s" * 300 + " end"
    rig.client.raw = draft(subject=subject)
    assert f"Subject: {subject}" in typed_ok(rig)["summary"].splitlines()


def test_the_summary_names_a_reply_to_only_when_there_is_one(rig):
    assert "Reply-To:" not in typed_ok(rig)["summary"]
    rig.client.raw = mime_draft(
        lambda message: message.set_content("Text"), Reply_To="elsewhere@example.test"
    )
    lines = typed_ok(rig)["summary"].splitlines()
    assert lines[5] == "Reply-To: elsewhere@example.test"
    snapshot = rig.service._read(DRAFT_ID)
    assert snapshot["reply_to"] == ["elsewhere@example.test"]
    assert "reply_to" not in send_module._binding(snapshot)


def _plain_and_html(message):
    message.set_content("Shown text")
    message.add_alternative("<p>Hidden text</p>", subtype="html")


def _plain_and_second_text(message):
    message.set_content("Shown text")
    message.add_attachment("Hidden text", disposition="inline")


def _empty_plain_and_html(message):
    message.set_content("")
    message.add_alternative("<p>Hidden text</p>", subtype="html")


def _calendar_only(message):
    message.set_content("Hidden text", subtype="calendar")


@pytest.mark.parametrize(
    "build,body",
    [
        (_plain_and_html, ["Body:", "> Shown text"]),
        (_plain_and_second_text, ["Body:", "> Shown text"]),
        (_empty_plain_and_html, ["Body: empty"]),
        (_calendar_only, ["Body: empty"]),
    ],
    ids=["plain-and-html", "second-inline-text", "empty-plain-and-html", "calendar-only"],
)
def test_the_summary_counts_the_text_parts_it_does_not_show(rig, build, body):
    rig.client.raw = mime_draft(build)
    preview = typed_ok(rig)
    lines = preview["summary"].splitlines()
    shown = lines.index("Other text parts, not shown: 1")
    assert lines[shown + 2 : -1] == body
    assert "Hidden text" not in preview["summary"]
    assert send(rig, preview)["sent"] is True


@pytest.mark.parametrize(
    "file,listed",
    [
        ({"filename": "list.txt"}, '- "list.txt" (text/plain, '),
        ({"filename": "list.txt", "disposition": "inline"}, '- "list.txt" (text/plain, '),
        ({}, '- "(unnamed)" (text/plain, '),
    ],
    ids=["named", "named-inline", "unnamed-attachment"],
)
def test_a_listed_text_file_is_not_counted_as_hidden(rig, file, listed):
    def build(message):
        message.set_content("Shown text")
        message.add_attachment("a,b", **file)

    rig.client.raw = mime_draft(build)
    summary = typed_ok(rig)["summary"]
    assert any(line.startswith(listed) for line in summary.splitlines())
    assert "Other text parts" not in summary


def test_the_summary_says_when_it_cannot_count_the_other_parts(rig, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(send_module, "_other_text_parts", lambda wire: None)
        assert "Other text parts: could not be counted" in typed_ok(rig)["summary"].splitlines()

    class Broken:
        def __init__(self, **kwargs):
            pass

        def parsebytes(self, wire):
            raise ValueError("synthetic")

    monkeypatch.setattr(send_module, "BytesParser", Broken)
    assert send_module._other_text_parts(b"x") is None


def test_the_summary_prefix_matches_the_form_and_the_journal(rig):
    preview = typed_ok(rig)
    prefix = preview["draft_hash"][:16]
    assert f"SHA-256 prefix: {prefix}" in preview["summary"].splitlines()
    assert send(rig, preview)["sent"] is True
    assert records(rig)[-1]["draft_hash"].startswith(prefix)
    host = Host()
    rig.client.raw = draft(subject="Second")
    form = rig.service.preview_send(DRAFT_ID)
    redeem(rig, form["token"], host)
    assert f"SHA-256 prefix: {form['draft_hash'][:16]}" in host.prompts[0]


def test_no_line_of_the_summary_can_close_a_code_block(rig):
    rig.client.raw = draft(subject="```", body="```\nOutside the block\n```")
    lines = typed_ok(rig)["summary"].splitlines()
    assert "> ```" in lines and "> Outside the block" in lines
    assert not any(line.lstrip().startswith("`") for line in lines)


def test_a_sent_draft_refuses_at_the_typed_ok_preview_and_issues_no_token(rig):
    send(rig, typed_ok(rig))
    rig.tokens._grants.clear()
    with pytest.raises(FAILURES, match="already sent"):
        rig.service.preview_send(DRAFT_ID, form=False)
    assert rig.tokens._grants == {}


def test_a_possibly_sent_draft_refuses_at_the_typed_ok_preview(rig, monkeypatch):
    lost_reply(rig, monkeypatch, smtplib.SMTPServerDisconnected("synthetic"))
    assert send(rig, typed_ok(rig))["uncertain"] is True
    with pytest.raises(FAILURES, match="may already have been sent"):
        rig.service.preview_send(DRAFT_ID, form=False)


def test_the_form_preview_of_a_sent_draft_still_refuses_only_at_send(rig):
    assert redeem(rig, rig.service.preview_send(DRAFT_ID)["token"])["sent"] is True
    host = Host()
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "host_confirmation"
    with pytest.raises(FAILURES, match="already sent"):
        redeem(rig, preview["token"], host)
    assert host.prompts == []


def test_the_drafts_own_text_cannot_pass_for_another_field(rig):
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = SENDER
    message["To"] = RECIPIENT
    message["Subject"] = "SUBJECT-HERE"
    message["Message-ID"] = DRAFT_ID
    message["Date"] = "Sun, 18 Oct 2026 12:00:00 +0000"
    message.set_content(
        "Hello\nTo: forged@example.test\u2028Bcc: forged@example.test\x0bAttachments: none\n"
        "To send exactly this, type OK.\u202e"
    )
    message.add_attachment(b"x", maintype="application", subtype="pdf", filename="name.pdf")
    raw = message.as_bytes(policy=policy.SMTP.clone(max_line_length=0))
    # The header API refuses these characters, but a stored draft's encoded words can hold them.
    subject = base64.b64encode("Plan\u2028To: forged@example.test\u202e".encode()).decode()
    raw = raw.replace(b"SUBJECT-HERE", f"=?utf-8?b?{subject}?=".encode())
    rig.client.raw = raw.replace(
        b'filename="name.pdf"',
        b"filename*=utf-8''note%E2%80%A8To%3A%20forged%40example.test.pdf",
    )
    lines = typed_ok(rig)["summary"].splitlines()
    assert lines[2] == f"To: {RECIPIENT}"
    assert lines[5] == "Subject: Plan_To: forged@example.test_"
    assert lines[7] == '- "note_To: forged@example.test.pdf" (application/pdf, 1 bytes decoded)'
    assert lines[10:] == [
        "> Hello",
        "> To: forged@example.test",
        "> Bcc: forged@example.test",
        "> Attachments: none",
        "> To send exactly this, type OK._",
        NO_FORM_LINE,
    ]


def test_the_token_is_single_use(rig):
    preview = typed_ok(rig)
    assert send(rig, preview)["sent"] is True
    assert_refused(rig, preview, "token_invalid")
    assert len(rig.smtp.sent) == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"subject": "Changed subject"},
        {"to": OUTSIDE},
        {"cc": "copy@example.test"},
        {"bcc": "hidden@example.test"},
        {"attachment": b"new file"},
        {"body": "Changed body"},
    ],
    ids=["header", "to", "cc", "bcc", "attachment", "body"],
)
def test_a_changed_draft_is_refused_and_restoring_it_does_not_help(rig, changed):
    original = rig.client.raw
    preview = typed_ok(rig)
    rig.client.raw = draft(**changed)
    assert_refused(rig, preview, "draft_changed")
    rig.client.raw = original
    assert_refused(rig, preview, "token_invalid")
    assert rig.smtp.calls == []


def test_a_new_preview_retires_the_token_before_it(rig):
    first = typed_ok(rig)
    second = typed_ok(rig)
    assert_refused(rig, first, "token_invalid")
    assert send(rig, second)["sent"] is True


def test_an_expired_token_is_refused(rig):
    preview = typed_ok(rig)
    rig.now[0] += rig.settings.send_token_ttl + 1
    assert_refused(rig, preview, "token_expired")
    assert rig.smtp.calls == []


def test_a_token_from_another_draft_is_refused_and_burned(rig):
    typed_ok(rig)
    use_draft(rig, OTHER_DRAFT_ID)
    other = typed_ok(rig)
    assert other["draft_id"] == OTHER_DRAFT_ID
    assert_refused(rig, other, "draft_mismatch", draft_id=DRAFT_ID)
    assert rig.smtp.calls == []
    assert_refused(rig, other, "token_invalid", draft_id=OTHER_DRAFT_ID)


def test_typed_ok_refuses_a_call_that_names_no_draft(rig):
    preview = typed_ok(rig)
    with pytest.raises(MailValidationError, match="needs draft_id"):
        asyncio.run(rig.service.send_draft(preview["token"], Host(support=False)))
    assert rig.smtp.calls == []
    assert records(rig)[-1]["result"] == "draft_mismatch"


def test_a_draft_id_may_omit_its_brackets(rig):
    preview = typed_ok(rig)
    assert send(rig, preview, draft_id=DRAFT_ID.strip("<>"))["sent"] is True


def test_an_unusable_draft_id_refuses_without_being_echoed(rig):
    preview = typed_ok(rig)
    with pytest.raises(FAILURES) as caught:
        send(rig, preview, draft_id=PASSWORD)
    assert PASSWORD not in str(caught.value) and rig.smtp.calls == []


def test_draft_id_only_ever_refuses_on_the_other_paths_too(rig):
    preview = rig.service.preview_send(DRAFT_ID)
    host = Host()
    with pytest.raises(FAILURES):
        send(rig, preview, host, draft_id=OTHER_DRAFT_ID)
    assert host.prompts == [] and rig.smtp.calls == []
    assert records(rig)[-1]["result"] == "draft_mismatch"
    named = rig.service.preview_send(DRAFT_ID)
    assert send(rig, named, host, draft_id=DRAFT_ID)["sent"] is True
    assert len(host.prompts) == 1


def test_a_client_with_a_form_redeeming_a_typed_ok_token_gets_the_form(rig):
    preview = typed_ok(rig)
    declining = Host("decline")
    with pytest.raises(FAILURES):
        send(rig, preview, declining)
    assert len(declining.prompts) == 1 and rig.smtp.calls == []
    assert records(rig)[-1]["result"] == "confirmation_declined"
    accepting = Host()
    assert send(rig, typed_ok(rig), accepting)["sent"] is True
    assert len(accepting.prompts) == 1
    # The journal names the path it really took, not the one the preview reported.
    assert [row["approval"] for row in records(rig) if row.get("budget_reserved")] == [
        "host_confirmation"
    ]


SAYS_NO_FORM = [
    pytest.param(METHOD_NOT_FOUND, "Method not found", id="no-such-method"),
    pytest.param(INVALID_REQUEST, "Elicitation not supported", id="sdk-not-supported"),
]


@pytest.mark.parametrize("code,message", SAYS_NO_FORM)
def test_a_host_that_declares_a_form_and_says_it_has_none_never_falls_back(rig, code, message):
    async def run():
        asked = []

        async def answer(ctx, params):
            asked.append(params)
            return ErrorData(code=code, message=message)

        server = create_mcp(rig.settings, rig.client)
        async with create_connected_server_and_client_session(
            server, elicitation_callback=answer
        ) as client:
            for attempt in (1, 2):
                preview = (
                    await client.call_tool("preview_send", {"draft_id": DRAFT_ID})
                ).structuredContent
                assert preview["approval"] == "host_confirmation"
                assert "summary" not in preview
                refused = await client.call_tool(
                    "send_draft", {"token": preview["token"], "draft_id": preview["draft_id"]}
                )
                assert refused.isError is True
                assert "did not show a form" in refused.content[0].text
                assert records(rig)[-1]["result"] == "confirmation_unavailable"
                assert len(asked) == attempt

    asyncio.run(run())
    assert rig.smtp.calls == []


@pytest.mark.parametrize("code,message", SAYS_NO_FORM)
def test_a_new_session_per_call_stays_on_the_form_too(rig, code, message):
    async def run():
        async def answer(ctx, params):
            return ErrorData(code=code, message=message)

        server = create_mcp(rig.settings, rig.client)
        for _ in range(2):
            async with create_connected_server_and_client_session(
                server, elicitation_callback=answer
            ) as client:
                preview = (
                    await client.call_tool("preview_send", {"draft_id": DRAFT_ID})
                ).structuredContent
                assert preview["approval"] == "host_confirmation"
            async with create_connected_server_and_client_session(
                server, elicitation_callback=answer
            ) as client:
                refused = await client.call_tool("send_draft", {"token": preview["token"]})
                assert refused.isError is True

    asyncio.run(run())
    assert rig.smtp.calls == []
    assert [row["result"] for row in records(rig)] == ["confirmation_unavailable"] * 2


@pytest.mark.parametrize(
    "reply,result",
    [
        (ErrorData(code=INTERNAL_ERROR, message="Host trouble"), "confirmation_error"),
        (ErrorData(code=INVALID_PARAMS, message="Host trouble"), "confirmation_error"),
        # The same code from a host that shows forms: too long, or one already open.
        (ErrorData(code=INVALID_REQUEST, message="Form too long"), "confirmation_error"),
        (ErrorData(code=INVALID_REQUEST, message="Form already open"), "confirmation_error"),
        # The wrong code with the right words is no statement about the form either.
        (ErrorData(code=INTERNAL_ERROR, message="Elicitation not supported"), "confirmation_error"),
        (ElicitResult(action="accept", content={"confirm": "yes"}), "confirmation_error"),
        (ElicitResult(action="accept", content={"confirm": False}), "confirmation_declined"),
        (ElicitResult(action="accept"), "confirmation_error"),
    ],
    ids=[
        "internal",
        "invalid-params",
        "invalid-request-too-long",
        "invalid-request-overlap",
        "internal-saying-not-supported",
        "malformed-accept",
        "accept-confirm-false",
        "accept-no-content",
    ],
)
def test_any_other_host_failure_keeps_the_form(rig, reply, result):
    async def run():
        async def answer(ctx, params):
            return reply

        server = create_mcp(rig.settings, rig.client)
        async with create_connected_server_and_client_session(
            server, elicitation_callback=answer
        ) as client:
            first = await client.call_tool("preview_send", {"draft_id": DRAFT_ID})
            refused = await client.call_tool(
                "send_draft", {"token": first.structuredContent["token"]}
            )
            assert refused.isError is True
            assert records(rig)[-1]["result"] == result
            second = await client.call_tool("preview_send", {"draft_id": DRAFT_ID})
            assert second.structuredContent["approval"] == "host_confirmation"

    asyncio.run(run())
    assert rig.smtp.calls == []


def test_an_unanswered_form_keeps_the_form(rig):
    settings = replace(rig.settings, send_confirm_timeout=0.05)

    async def run():
        async def answer(ctx, params):
            await asyncio.sleep(0.5)
            return ElicitResult(action="decline")

        server = create_mcp(settings, rig.client)
        async with create_connected_server_and_client_session(
            server, elicitation_callback=answer
        ) as client:
            first = await client.call_tool("preview_send", {"draft_id": DRAFT_ID})
            refused = await client.call_tool(
                "send_draft", {"token": first.structuredContent["token"]}
            )
            assert refused.isError is True
            assert "got no answer in time" in refused.content[0].text
            assert "no other kind of confirmation applies" in refused.content[0].text
            assert records(rig)[-1]["result"] == "confirmation_timeout"
            second = await client.call_tool("preview_send", {"draft_id": DRAFT_ID})
            assert second.structuredContent["approval"] == "host_confirmation"

    asyncio.run(run())
    assert rig.smtp.calls == []


def test_a_typed_ok_token_never_falls_back_when_a_form_times_out(rig):
    host = Host(TimeoutError())
    assert_refused(rig, typed_ok(rig), "confirmation_timeout", host=host)
    assert len(host.prompts) == 1


@pytest.mark.parametrize(
    "result,words",
    [
        ("confirmation_timeout", "got no answer in time"),
        ("confirmation_unavailable", "did not show a form"),
        ("confirmation_error", "form failed"),
    ],
)
def test_a_form_that_gives_no_answer_says_why_and_that_nothing_else_applies(result, words):
    text = str(ConfirmationError(result))
    assert words in text and "no other kind of confirmation applies" in text
    assert text.startswith("User confirmation was not accepted")


@pytest.mark.parametrize("result", ["confirmation_declined", "confirmation_cancelled"])
def test_a_persons_refusal_keeps_its_short_message(result):
    assert str(ConfirmationError(result)) == (
        "User confirmation was not accepted; request a fresh preview"
    )


def test_only_a_declared_form_counts_as_a_form():
    assert form_available(Host()) is True
    assert form_available(Host(support=False)) is False
    assert form_available(None) is False
    assert form_available(SimpleNamespace(session=SimpleNamespace(client_params=None))) is False


def test_only_a_capability_read_as_absent_declares_no_form():
    assert declares_no_form(Host(support=False)) is True
    assert declares_no_form(Host()) is False
    assert declares_no_form(None) is False
    for mode in ({"url": {}}, {"form": {}, "url": {}}, {}):
        host = Host()
        host.session.client_params.capabilities.elicitation = ElicitationCapability(**mode)
        assert declares_no_form(host) is False, mode


@pytest.mark.parametrize("client", UNKNOWN_CLIENTS)
def test_an_unknown_client_state_declares_nothing_and_refuses(rig, client):
    assert declares_no_form(client) is False
    assert_refused(rig, typed_ok(rig), "confirmation_unavailable", host=client)
    assert rig.smtp.calls == []


@pytest.mark.parametrize("client", UNKNOWN_CLIENTS)
def test_the_preview_tools_keep_the_form_for_an_unknown_client_state(rig, client):
    tools = create_mcp(rig.settings, rig.client)._tool_manager

    def run(name):
        return asyncio.run(tools.call_tool(name, {"draft_id": DRAFT_ID}, context=client))

    assert run("preview_send")["approval"] == "host_confirmation"
    assert json.loads(run("review_send").content[0].text)["approval"] == "host_confirmation"


def test_review_send_reports_the_same_path_and_stays_token_free(rig):
    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = await http_session(rig, app, {})
            preview = (await call(http, "preview_send", draft_id=DRAFT_ID))["structuredContent"]
            result = await call(http, "review_send", draft_id=DRAFT_ID)
            text = json.loads(result["content"][0]["text"])
            assert text["approval"] == "typed_ok"
            assert text["action_hash"] == preview["action_hash"]
            assert not {"token", "expires_at", "summary"} & set(text)
            assert preview["token"] not in json.dumps(result)
            assert "typed OK" in text["next_steps"]

    asyncio.run(run())
    assert rig.smtp.calls == []


def test_the_review_text_names_each_approval_path():
    preview = {"draft_id": DRAFT_ID, "draft_hash": "a"}
    steps = {
        approval: views.review_view({**preview, "approval": approval})[0]["next_steps"]
        for approval in ("host_confirmation", "token", "typed_ok")
    }
    assert "typed OK" in steps["typed_ok"] and "draft_id" in steps["typed_ok"]
    assert "host's form" in steps["host_confirmation"]
    assert "without a separate form" in steps["token"]


def test_a_mailto_unsubscribe_preview_takes_the_same_path(rig, monkeypatch):
    rig.settings = replace(rig.settings, confirm_mutations=False)
    _, _, tools = setup_tools(rig, monkeypatch)
    no_form = Host(support=False)
    preview = tools("preview_action", {"action": "unsubscribe", "ids": [IDENTIFIER]}, no_form)
    result = tools("unsubscribe", {"token": preview["token"]}, no_form)
    send_preview = result["drafts"][0]["send_preview"]
    assert send_preview["approval"] == "typed_ok"
    assert send_preview["method"] == "MAILTO" and RECIPIENT in send_preview["summary"]
    sent = tools(
        "send_draft",
        {"token": send_preview["token"], "draft_id": send_preview["draft_id"]},
        no_form,
    )
    assert sent["sent"] is True and no_form.prompts == []
    assert len(rig.smtp.sent) == 1


@pytest.mark.parametrize("typed_ok_on", [True, False], ids=["on", "off"])
def test_a_mailto_unsubscribe_preview_follows_the_switch(rig, monkeypatch, typed_ok_on):
    rig.settings = replace(rig.settings, confirm_mutations=False, send_typed_ok=typed_ok_on)
    _, _, tools = setup_tools(rig, monkeypatch)
    no_form = Host(support=False)
    preview = tools("preview_action", {"action": "unsubscribe", "ids": [IDENTIFIER]}, no_form)
    result = tools("unsubscribe", {"token": preview["token"]}, no_form)
    expected = "typed_ok" if typed_ok_on else "host_confirmation"
    assert result["drafts"][0]["send_preview"]["approval"] == expected


def test_a_mailto_unsubscribe_preview_keeps_the_form_for_an_unknown_client_state(rig, monkeypatch):
    rig.settings = replace(rig.settings, confirm_mutations=False)
    _, _, tools = setup_tools(rig, monkeypatch)
    unknown = SimpleNamespace(session=SimpleNamespace(client_params=None))
    preview = tools("preview_action", {"action": "unsubscribe", "ids": [IDENTIFIER]}, unknown)
    result = tools("unsubscribe", {"token": preview["token"]}, unknown)
    assert result["drafts"][0]["send_preview"]["approval"] == "host_confirmation"


def test_descriptions_and_docs_say_what_the_token_guards(rig):
    listed = create_mcp(rig.settings, rig.client)._tool_manager.list_tools()
    tools = {tool.name: tool for tool in listed}
    for name in ("preview_send", "send_draft"):
        text = " ".join(tools[name].description.split())
        assert all(words in text for words in TYPED_OK_TEXT), name
        assert "this app has no confirmation form" in text and "in a code block" in text, name
    root = Path(__file__).resolve().parents[1]
    readme = " ".join((root / "README.md").read_text(encoding="utf-8").split())
    assert "cannot prove that a human typed the OK" in readme
    assert "only guards against a changed draft and a replay" in readme
    assert "always gets the form and never falls back" in readme
    assert "`PROTON_MCP_SEND_TYPED_OK=0` turns this path off" in readme
    assert "whether it keeps one session or opens a new one per call" in readme
    assert "| `PROTON_MCP_SEND_TYPED_OK` | `1` |" in readme
    assert "must never widen this boundary further" in readme
    assert "There is no setting for this path" not in readme
    assert "must never widen this boundary." not in readme
    apps = " ".join((root / "docs" / "mcp-apps.md").read_text(encoding="utf-8").split())
    assert "keeps the existing gate exactly" not in apps
    assert "PROTON_MCP_SEND_TYPED_OK=0" in apps


def test_no_token_or_secret_reaches_the_summary_the_logs_or_a_refusal(rig, monkeypatch, caplog):
    monkeypatch.setenv("PROTON_BRIDGE_PASSWORD", PASSWORD)
    monkeypatch.setenv("PROTON_MCP_TOKEN", TOKEN)
    shown = []

    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = await http_session(rig, app, {})
            preview = (await call(http, "preview_send", draft_id=DRAFT_ID))["structuredContent"]
            used = preview["token"]
            for arguments in (
                {"token": used},
                {"token": used, "draft_id": DRAFT_ID},
                {"token": used, "draft_id": OTHER_DRAFT_ID},
            ):
                refused = await http.rpc(
                    "tools/call", {"name": "send_draft", "arguments": arguments}
                )
                shown.append(refused)
            fresh = (await call(http, "preview_send", draft_id=DRAFT_ID))["structuredContent"]
            sent = await call(http, "send_draft", token=fresh["token"], draft_id=DRAFT_ID)
            assert sent["structuredContent"]["sent"] is True
            shown.append(sent)
            return used, fresh

    # INFO, below the WARNING the server runs at. No setting reaches DEBUG, where
    # sse_starlette logs every raw response, with any token, for every tool.
    with caplog.at_level(logging.INFO):
        used, fresh = asyncio.run(run())
    assert len(rig.smtp.sent) == 1
    for token in (used, fresh["token"]):
        assert token not in caplog.text
        assert token not in fresh["summary"]
        assert token not in json.dumps(shown)
    with open(rig.settings.action_log_path, encoding="utf-8") as stream:
        journal = stream.read()
    assert used not in journal and fresh["token"] not in journal
    # The journal keeps its promise of no content, summaries included.
    assert BODY not in journal and SUBJECT not in journal and RECIPIENT not in journal
    output = json.dumps([fresh, shown]) + caplog.text + journal
    assert PASSWORD not in output and TOKEN not in output


def _longest_piece(token, text):
    for size in range(len(token), 0, -1):
        if any(token[start : start + size] in text for start in range(len(token) - size + 1)):
            return size
    return 0


@pytest.mark.parametrize(
    "message",
    [
        lambda token: {
            "id": 900,
            "method": "tools/call",
            "params": {"name": "send_draft", "arguments": token},
        },
        lambda token: {
            "id": 900,
            "method": "tools/call",
            "params": {"name": 123, "arguments": {"token": token}},
        },
        lambda token: {
            "id": 900,
            "method": "tools/call",
            "params": {"arguments": {"token": token}},
        },
        lambda token: {
            "id": 900,
            "method": "tools/call",
            "params": {"name": "send_draft", "arguments": json.dumps({"token": token})},
        },
        lambda token: {"method": "notifications/made-up", "params": {"token": token}},
        lambda token: {"method": "notifications/cancelled", "params": {"requestId": {"x": token}}},
    ],
    ids=[
        "arguments-the-bare-token",
        "bad-name",
        "no-name",
        "arguments-as-json-text",
        "unknown-notification",
        "bad-notification",
    ],
)
def test_a_malformed_message_leaves_no_token_text_in_the_log(rig, caplog, message):
    tokens = []

    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = await http_session(rig, app, {})
            preview = (await call(http, "preview_send", draft_id=DRAFT_ID))["structuredContent"]
            tokens.append(preview["token"])
            await http.post({"jsonrpc": "2.0", **message(preview["token"])})

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    assert "Failed to validate" in caplog.text
    assert _longest_piece(tokens[0], caplog.text) < 8


@pytest.fixture
def own_logger(rig):
    """Like uvicorn's loggers: neither the root's filters nor its handlers see these records."""
    create_mcp(rig.settings, rig.client)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("proton_mail_mcp.tests.own_handler")
    logger.propagate = False
    logger.addHandler(handler)
    yield logger, stream
    logger.removeHandler(handler)


def test_token_text_leaves_a_logger_that_never_reaches_the_root(own_logger):
    logger, stream = own_logger
    token = secrets.token_urlsafe(32)
    logger.warning(f"inline {token}")
    logger.warning("argument %s", token)
    logger.warning("mapping %(value)s", {"value": token})
    logger.warning(ValueError(token))
    try:
        raise ValueError(token)
    except ValueError:
        logger.exception("failed")
    text = stream.getvalue()
    assert token not in text
    assert text.count("<redacted>") == 5
    assert "inline <redacted>" in text and "argument <redacted>" in text
    assert "ValueError: <redacted>" in text


def test_an_echoed_value_leaves_the_log_whatever_its_shape(own_logger):
    logger, stream = own_logger
    logger.warning(
        "1 error\n  Bad [type=dict_type, input_value='a\nshort-secret, x', input_type=str]\nNext"
    )
    logger.warning("cut off input_value='short-secret")
    logger.warning("Failed to validate. Message was: method='m' params={'k': 'a\nshort-secret'}")
    text = stream.getvalue()
    assert "short-secret" not in text
    assert "input_value=<redacted>, input_type=str]\nNext" in text
    assert "Failed to validate. Message was: <redacted>" in text


def test_log_text_that_cannot_be_a_token_stays_as_it_is(own_logger):
    logger, stream = own_logger
    log_id = "0123456789abcdef" * 2
    logger.warning("log_id=%s count=%d", log_id, 7)
    names = (
        "smtplib.SMTPServerDisconnected: set PROTON_MCP_SEND_TYPED_OK=0",
        "PROTON_MCP_SEARCH_INDEX_CATCHUP_MAX_MESSAGES",
        "in a_function_name_of_more_than_forty_characters_long",
    )
    logger.warning(" ".join(names))
    assert stream.getvalue() == f"log_id={log_id} count=7\n{' '.join(names)}\n"


def test_a_record_with_nothing_to_remove_keeps_its_arguments_and_exception(rig):
    create_mcp(rig.settings, rig.client)
    try:
        raise ValueError("plain failure")
    except ValueError:
        info = sys.exc_info()
    record = logging.getLogRecordFactory()("n", logging.ERROR, "f", 1, "%s %d", ("a", 1), info)
    assert record.args == ("a", 1) and record.exc_info == info


def test_a_log_call_with_a_bad_format_cannot_print_its_arguments(own_logger, capsys):
    logger, stream = own_logger
    token = secrets.token_urlsafe(32)
    logger.warning("%d", token)
    assert "could not be formatted" in stream.getvalue()
    assert token not in stream.getvalue() + capsys.readouterr().err


def test_the_root_logger_filters_a_record_that_missed_the_factory(rig, caplog):
    create_mcp(rig.settings, rig.client)
    token = secrets.token_urlsafe(32)
    record = logging.LogRecord("root", logging.WARNING, __file__, 1, "raw %s", (token,), None)
    with caplog.at_level(logging.WARNING):
        logging.getLogger().handle(record)
    assert "raw <redacted>" in caplog.text and token not in caplog.text


def test_a_handler_that_prints_exc_info_itself_gets_no_token(rig):
    create_mcp(rig.settings, rig.client)
    token = secrets.token_urlsafe(32)
    try:
        raise ValueError(token)
    except ValueError:
        info = sys.exc_info()
    record = logging.getLogRecordFactory()("n", logging.ERROR, "f", 1, "failed", (), info)
    assert record.exc_info is None
    assert token not in record.exc_text and "ValueError: <redacted>" in record.exc_text
