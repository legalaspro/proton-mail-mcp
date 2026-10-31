"""Review-send view: preview_send's exact preview, no token, sending stays gated."""

import asyncio
import json
from dataclasses import replace

import pytest
from test_send import BODY, DRAFT_ID, SENDER, SUBJECT, draft, local_certificate, records  # noqa: F401
from test_send import rig as _send_rig
from test_send_http import _HTTP
from view_assertions import assert_self_contained_view, tool_calls, view_data
from view_browser import HOST_CAPABILITIES, browser, run_view, tool_result

from proton_mail_mcp import mcp_apps, views
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.server import create_app, create_mcp

rig = _send_rig
REDEEMABLE = {"token", "expires_at"}


def test_review_matches_preview_send_exactly_without_issuing_a_token(rig):
    review = rig.service.review_send(DRAFT_ID)
    assert rig.tokens._grants == {}
    assert REDEEMABLE.isdisjoint(review)
    preview = rig.service.preview_send(DRAFT_ID)
    assert len(rig.tokens._grants) == 1
    assert {key: value for key, value in preview.items() if key not in REDEEMABLE} == {
        key: value for key, value in review.items() if key != "body_preview"
    }
    assert list(review)[: len(preview) - 2] == [key for key in preview if key not in REDEEMABLE]
    assert review["approval"] == "host_confirmation"
    assert review["body_preview"] == {
        "type": "text/plain",
        "text": BODY,
        "length": len(BODY),
        "truncated": False,
        "available": True,
    }
    # Reviewing reads and checks the draft like a preview, but journals nothing.
    assert records(rig) == []
    assert rig.smtp.calls == []


def test_review_shows_html_bodies_as_text_and_shortens_long_ones(rig):
    rig.client.raw = draft(body="<p>Hello <b>there</b></p><script>x()</script>")
    rig.client.raw = rig.client.raw.replace(b"text/plain", b"text/html")
    assert rig.service.review_send(DRAFT_ID)["body_preview"]["text"] == "Hello there"
    rig.client.raw = draft(body="x" * 25000)
    shown = rig.service.review_send(DRAFT_ID)["body_preview"]
    assert shown["truncated"] is True and len(shown["text"]) == 20000


@pytest.mark.parametrize(
    "change,message",
    [
        ("disabled", "Guarded send is disabled"),
        ("sender", "Draft From is not an exact allowlisted address"),
        ("missing", "Draft read failed"),
        ("budget", "Rolling daily send budget is exhausted"),
    ],
)
def test_review_refuses_whatever_preview_send_refuses(rig, change, message):
    service = rig.service
    if change == "disabled":
        service.settings = replace(rig.settings, enable_send=False)
    elif change == "sender":
        rig.client.raw = draft(sender="other@example.test")
    elif change == "missing":
        rig.client.raw = None
    else:
        service.settings = replace(rig.settings, send_daily_cap=1)
        service._remaining = lambda records: 0
    with pytest.raises(Exception, match=message) as refused:
        service.review_send(DRAFT_ID)
    with pytest.raises(type(refused.value), match=message):
        service.preview_send(DRAFT_ID)
    assert rig.smtp.calls == []


def test_review_view_never_carries_redeemable_fields():
    preview = {"draft_id": "<d@example.test>", "approval": "host_confirmation", "draft_hash": "a"}
    text, view = views.review_view({**preview, "token": "secret-token", "expires_at": 1.0})
    assert text["draft_id"] == "<d@example.test>"
    assert REDEEMABLE.isdisjoint(text) and REDEEMABLE.isdisjoint(view["preview"])
    assert "secret-token" not in json.dumps([text, view])
    assert view["quotable_draft_id"] == "<d@example.test>"
    assert "preview_send(draft_id)" in text["next_steps"]
    assert "confirms the exact draft in the host's form" in text["next_steps"]
    _, unquotable = views.review_view({**preview, "draft_id": "<a b@example.test>"})
    assert unquotable["quotable_draft_id"] is None
    token_text, _ = views.review_view({**preview, "approval": "token"})
    assert "without a separate form" in token_text["next_steps"]


def test_full_send_still_needs_a_fresh_token_and_host_confirmation(rig):
    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = _HTTP(app, rig.settings.token)
            capabilities = {
                "elicitation": {"form": {}},
                "extensions": {mcp_apps.EXTENSION_ID: {"mimeTypes": [mcp_apps.MIME_TYPE]}},
            }
            await http.initialize(capabilities)
            listed = await http.rpc("tools/list")
            by_name = {tool["name"]: tool for tool in listed["result"]["tools"]}
            # The review renders for the model's call; views cannot read drafts with it.
            assert by_name["review_send"]["_meta"]["ui"] == {
                "resourceUri": mcp_apps.REVIEW_SEND_URI,
                "visibility": ["model"],
            }
            for name in ("preview_send", "send_draft"):
                assert by_name[name]["_meta"] == {"ui": {"visibility": ["model"]}}
            resources = await http.rpc("resources/list")
            assert mcp_apps.REVIEW_SEND_URI in {
                item["uri"] for item in resources["result"]["resources"]
            }

            reviewed = await http.rpc(
                "tools/call", {"name": "review_send", "arguments": {"draft_id": DRAFT_ID}}
            )
            result = reviewed["result"]
            assert result["isError"] is False
            shown = view_data(result)["preview"]
            assert REDEEMABLE.isdisjoint(shown)
            assert REDEEMABLE.isdisjoint(json.loads(result["content"][0]["text"]))
            assert shown["subject"] == SUBJECT and shown["from"] == SENDER

            # Nothing in the review redeems: send_draft refuses each value it holds.
            for candidate in (shown["draft_hash"], shown["action_hash"], DRAFT_ID):
                refused = await http.rpc(
                    "tools/call", {"name": "send_draft", "arguments": {"token": candidate}}
                )
                assert refused["result"]["isError"] is True
            assert http.elicitations == []

            preview = await http.rpc(
                "tools/call", {"name": "preview_send", "arguments": {"draft_id": DRAFT_ID}}
            )
            payload = preview["result"]["structuredContent"]
            assert payload["draft_hash"] == shown["draft_hash"]
            assert payload["action_hash"] == shown["action_hash"]

            def accept(prompt):
                assert rig.smtp.calls == []
                assert shown["draft_hash"][:16] in prompt["message"]
                return {"action": "accept", "content": {"confirm": True}}

            http.on_elicit = accept
            sent = await http.rpc(
                "tools/call", {"name": "send_draft", "arguments": {"token": payload["token"]}}
            )
            assert sent["result"]["structuredContent"]["sent"] is True
            assert len(http.elicitations) == 1

    asyncio.run(run())
    assert len(rig.smtp.sent) == 1 and rig.smtp.sent[0][2] == rig.client.raw


def test_declined_confirmation_after_review_sends_nothing(rig):
    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = _HTTP(app, rig.settings.token)
            await http.initialize({"elicitation": {"form": {}}})
            await http.rpc(
                "tools/call", {"name": "review_send", "arguments": {"draft_id": DRAFT_ID}}
            )
            preview = await http.rpc(
                "tools/call", {"name": "preview_send", "arguments": {"draft_id": DRAFT_ID}}
            )
            http.on_elicit = lambda prompt: {"action": "decline"}
            token = preview["result"]["structuredContent"]["token"]
            declined = await http.rpc(
                "tools/call", {"name": "send_draft", "arguments": {"token": token}}
            )
            assert declined["result"]["isError"] is True

    asyncio.run(run())
    assert rig.smtp.calls == []
    assert records(rig)[-1]["result"] == "confirmation_declined"


def test_review_tool_exists_only_with_send_and_views_enabled(rig):
    def names(settings):
        return {tool.name for tool in asyncio.run(create_mcp(settings, rig.client).list_tools())}

    assert "review_send" in names(rig.settings)
    assert "review_send" not in names(replace(rig.settings, enable_send=False))
    assert "review_send" not in names(replace(rig.settings, apps_enabled=False))
    tool = asyncio.run(create_mcp(rig.settings, rig.client).list_tools())
    review = next(item for item in tool if item.name == "review_send")
    assert review.annotations.readOnlyHint is True
    assert set(review.inputSchema["properties"]) == {"draft_id"}
    assert review.inputSchema["additionalProperties"] is False


def test_review_errors_are_reported_without_backend_details(rig, monkeypatch):
    mcp = create_mcp(rig.settings, rig.client)

    def explode(draft_id):
        raise RuntimeError(f"password={rig.settings.token}")

    async def call(arguments):
        return await mcp.call_tool("review_send", arguments)

    monkeypatch.setattr(
        "proton_mail_mcp.send.SendService.review_send", lambda self, d, form: explode(d)
    )
    with pytest.raises(Exception) as failed:
        asyncio.run(call({"draft_id": DRAFT_ID}))
    assert rig.settings.token not in str(failed.value)
    assert "Draft preview failed" in str(failed.value)

    def invalid(self, draft_id, form):
        raise MailValidationError("Draft is missing or ambiguous")

    monkeypatch.setattr("proton_mail_mcp.send.SendService.review_send", invalid)
    with pytest.raises(Exception, match="Validation error: Draft is missing or ambiguous"):
        asyncio.run(call({"draft_id": DRAFT_ID}))


def test_review_view_holds_no_token_and_sends_only_by_asking_the_assistant():
    html = mcp_apps.view_html("review_send")
    code = assert_self_contained_view(html)
    view_script = code.split("var PMM = (function", 1)[1].split("})();", 1)[1]
    # review_send is model-only, so the view calls no tool; Refresh asks the assistant.
    assert tool_calls(code) == set() and "callTool" not in view_script
    assert ".token" not in view_script and "expires_at" not in view_script
    assert view_script.count("PMM.sendMessage(") == 3
    # Chat messages quote a plain draft ID and the hex fingerprint, nothing else.
    assert "data.quotable_draft_id" in view_script and "HEX.test(digest)" in view_script
    assert 'PMM.can("message")' in view_script


def review(draft_id="<review-1@example.test>", approval="host_confirmation"):
    """The tool result a host delivers to the review view."""
    preview = {
        "draft_id": draft_id,
        "from": "me@example.test",
        "to": ["alice@example.test"],
        "cc": [],
        "bcc": [],
        "subject": "Quarterly plan",
        "attachments": [],
        "draft_hash": "ab" * 32,
        "caps": {"daily_sends": 20, "remaining_daily_sends": 19, "window_hours": 24},
        "approval": approval,
        "action_hash": "cd" * 32,
        "body_preview": {"type": "text/plain", "text": "Hi", "truncated": False, "available": True},
    }
    return tool_result(mcp_apps.view_result(*views.review_view(preview)))


SEND = """
const doc = t.doc;
await t.waitFor(() => doc.getElementById("hash").textContent, "the review");
t.check("steps", doc.getElementById("gate").textContent);
doc.getElementById("send").click();
await t.waitFor(() => t.requests("ui/message").length === 1, "the chat message");
t.check("message", t.requests("ui/message")[0].params.content[0].text);
t.check("tool_calls", t.requests("tools/call").length);
"""


@browser
@pytest.mark.parametrize(
    "approval,step,ask",
    [
        ("host_confirmation", "confirmation form for send_draft", "so I can confirm"),
        ("token", "Own-address token approval", "so I can confirm"),
        ("typed_ok", "cannot prove that you typed it", "only after I type OK"),
    ],
)
def test_the_view_says_how_each_approval_path_sends(tmp_path, approval, step, ask):
    checks = run_view(tmp_path, "review_send", review(approval=approval), SEND)["checks"]
    assert step in checks["steps"]
    assert ask in checks["message"] and checks["tool_calls"] == 0


REFRESH = """
const doc = t.doc;
await t.waitFor(() => doc.getElementById("hash").textContent, "the review");
const refresh = doc.getElementById("refresh");
t.check("shown", t.visible(refresh) ? refresh.textContent : null);
if (t.visible(refresh)) {
  refresh.click();
  await t.waitFor(() => t.requests("ui/message").length === 1, "the chat message");
  await t.sleep(50);
  t.check("after", refresh.textContent);
}
t.check("messages", t.requests("ui/message").map((message) => message.params.content[0].text));
t.check("tool_calls", t.requests("tools/call").length);
"""


@browser
@pytest.mark.parametrize("case", ["asks", "cannot_message", "unquotable_id"])
def test_refresh_asks_the_assistant_because_views_cannot_read_drafts(tmp_path, case):
    capabilities = dict(HOST_CAPABILITIES)
    if case == "cannot_message":
        del capabilities["message"]
    result = review("<a b@example.test>") if case == "unquotable_id" else review()
    report = run_view(tmp_path, "review_send", result, REFRESH, capabilities=capabilities)
    checks = report["checks"]
    assert checks["tool_calls"] == 0
    if case == "asks":
        assert checks["shown"] == "Refresh…"
        assert checks["messages"] == [
            "Refresh the send review of my draft <review-1@example.test> with review_send."
        ]
        assert checks["after"] == "Asked the assistant"
    else:
        # Without chat messages or a plain draft ID the view offers no Refresh.
        assert checks["shown"] is None and checks["messages"] == []
