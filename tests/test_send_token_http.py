"""Token approval and strict arguments over initialized in-memory HTTP sessions."""

import asyncio

import pytest
from test_send import DRAFT_ID, local_certificate, records  # noqa: F401
from test_send import rig as _send_rig
from test_send_http import _HTTP
from test_send_token_approval import own_rig as _own_rig

from proton_mail_mcp.server import create_app

rig = _send_rig
own_rig = _own_rig


@pytest.mark.parametrize("capabilities", [{}, {"elicitation": {"url": {}}}])
def test_http_token_approval_works_without_form_support(own_rig, capabilities):
    rig = own_rig

    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = _HTTP(app, rig.settings.token)
            await http.initialize(capabilities)
            listed = await http.rpc("tools/list")
            by_name = {tool["name"]: tool for tool in listed["result"]["tools"]}
            assert set(by_name["send_draft"]["inputSchema"]["properties"]) == {
                "token",
                "draft_id",
            }
            response = await http.rpc(
                "tools/call", {"name": "preview_send", "arguments": {"draft_id": DRAFT_ID}}
            )
            preview = response["result"]["structuredContent"]
            assert preview["approval"] == "token"
            request = {"name": "send_draft", "arguments": {"token": preview["token"]}}
            sent = await http.rpc("tools/call", request)
            assert sent["result"]["structuredContent"]["sent"] is True
            reused = await http.rpc("tools/call", request)
            assert reused["result"]["isError"] is True
            assert http.elicitations == []

    asyncio.run(run())
    assert len(rig.smtp.sent) == 1
    assert records(rig)[-1]["result"] == "token_invalid"


@pytest.mark.parametrize("extra", ["confirm", "confirmed", "confirmation", "ctx", "draft_id"])
def test_http_token_approval_rejects_extra_arguments(own_rig, extra):
    rig = own_rig

    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = _HTTP(app, rig.settings.token)
            await http.initialize({})
            response = await http.rpc(
                "tools/call", {"name": "preview_send", "arguments": {"draft_id": DRAFT_ID}}
            )
            preview = response["result"]["structuredContent"]
            assert preview["approval"] == "token"
            result = await http.rpc(
                "tools/call",
                {"name": "send_draft", "arguments": {"token": preview["token"], extra: True}},
            )
            assert result["result"]["isError"] is True
            assert rig.smtp.calls == [] and records(rig) == []
            sent = await http.rpc(
                "tools/call", {"name": "send_draft", "arguments": {"token": preview["token"]}}
            )
            assert sent["result"]["structuredContent"]["sent"] is True
            assert http.elicitations == []

    asyncio.run(run())
    assert len(rig.smtp.sent) == 1
