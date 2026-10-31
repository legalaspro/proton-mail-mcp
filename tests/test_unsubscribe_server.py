"""Exercise the complete unsubscribe draft handoff through the registered MCP tools."""

import asyncio
import json
from dataclasses import replace
from email import policy
from email.parser import BytesParser

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from test_send import DRAFT_ID, RECIPIENT, SENDER, Host
from test_send import local_certificate as local_certificate
from test_send import rig as rig
from test_send_draft_roundtrip import roundtrip_client
from test_unsubscribe import IDENTIFIER, newsletter

from proton_mail_mcp.server import create_mcp

SUBJECT = "Remove + café"
BODY = "Remove me\r\nList subscription"
URL = f"mailto:{RECIPIENT}?subject=Remove%20+%20caf%C3%A9&body=Remove%20me%0D%0AList%20subscription"


def setup_tools(rig, monkeypatch):
    rig.settings = replace(rig.settings, send_self_no_form=True)
    client, fake, _ = roundtrip_client(rig, monkeypatch, SENDER)
    client.credentials_provider = rig.client.credentials_provider
    fake.folders["INBOX"] = {"1": newsletter(url=URL, one_click=False)}
    mcp = create_mcp(rig.settings, client)

    def call(name, arguments, host=None):
        return asyncio.run(mcp._tool_manager.call_tool(name, arguments, context=host or Host()))

    return client, fake, call


def test_unsubscribe_returns_separate_bound_send_preview(rig, monkeypatch):
    client, fake, call = setup_tools(rig, monkeypatch)
    preview = call("preview_action", {"action": "unsubscribe", "ids": [IDENTIFIER]})
    assert not fake.appended and not rig.smtp.calls
    host = Host()
    result = call("unsubscribe", {"token": preview["token"]}, host)

    assert result["count"] == 0 and result["pending_send"] == 1
    assert len(fake.appended) == 1 and not rig.smtp.calls
    handoff = result["drafts"][0]
    send_preview = handoff["send_preview"]
    assert handoff["draft_id"] == send_preview["draft_id"] == DRAFT_ID
    recovered = call("list_actions", {"token": preview["token"]})
    assert recovered["actions"][0]["draft_ids"] == [DRAFT_ID]
    assert recovered["actions"][0]["status"] == result["status"] == "pending_send"
    assert recovered["actions"][0]["partial"] is result["partial"] is False
    assert recovered["audit"][0]["outcome"] == "pending_send"
    for private in (RECIPIENT, SUBJECT, BODY, URL, preview["token"], send_preview["token"]):
        assert private not in json.dumps(recovered)
    assert send_preview["method"] == "MAILTO"
    assert send_preview["to"] == [RECIPIENT]
    assert send_preview["subject"] == SUBJECT and send_preview["body"] == BODY
    assert send_preview["approval"] == "host_confirmation"
    confirmation = json.loads(host.prompts[0].split("\n", 1)[1])
    assert confirmation["drafts"][0]["body"] == BODY

    # Neither the unsubscribe approval nor an arbitrary token authorizes SMTP.
    for token in (preview["token"], "invalid-send-token"):
        with pytest.raises(ToolError):
            call("send_draft", {"token": token})
    assert not rig.smtp.calls
    sent = call("send_draft", {"token": send_preview["token"]})
    assert sent["sent"] is True and len(rig.smtp.sent) == 1
    sender, recipients, wire = rig.smtp.sent[0]
    assert sender == SENDER and recipients == (RECIPIENT,)
    message = BytesParser(policy=policy.SMTP).parsebytes(wire)
    assert str(message["Subject"]) == SUBJECT
    assert message.get_content() == BODY + "\r\n"
    with pytest.raises(ToolError):
        call("send_draft", {"token": send_preview["token"]})
    assert len(rig.smtp.sent) == 1
    journal = client.cleanup.backend.log.path.read_text()
    for secret in (RECIPIENT, SUBJECT, BODY, URL, preview["token"], send_preview["token"]):
        assert secret not in journal


def test_handoff_keeps_saved_draft_when_send_preview_is_refused(rig, monkeypatch):
    client, fake, call = setup_tools(rig, monkeypatch)
    preview = call("preview_action", {"action": "unsubscribe", "ids": [IDENTIFIER]})
    original = client.read_draft

    def changed(draft_id):
        row = original(draft_id)
        row["raw"] = row["raw"].replace(RECIPIENT.encode(), b"changed@example.test")
        return row

    monkeypatch.setattr(client, "read_draft", changed)
    result = call("unsubscribe", {"token": preview["token"]})
    handoff = result["drafts"][0]
    assert handoff["draft_id"] == DRAFT_ID
    assert "send_preview" not in handoff
    assert "does not match" in handoff["send_preview_error"]
    assert len(fake.appended) == 1 and not rig.smtp.calls
