"""Exercise real initialized HTTP/SSE elicitation entirely through in-memory ASGI."""

import asyncio
import json

import pytest
from test_send import DRAFT_ID, PASSWORD, SUBJECT, local_certificate, records  # noqa: F401
from test_send import rig as _send_rig

from proton_mail_mcp.server import create_app

rig = _send_rig


class _HTTP:
    def __init__(self, app, token):
        self.app, self.token = app, token
        self.session = None
        self.request_id = 0
        self.elicitations = []
        self.on_elicit = None

    async def post(self, body):
        payload = json.dumps(body).encode()
        headers = [
            (b"host", b"127.0.0.1"),
            (b"authorization", f"Bearer {self.token}".encode()),
            (b"content-type", b"application/json"),
            (b"accept", b"application/json, text/event-stream"),
        ]
        if self.session:
            headers.extend(
                [
                    (b"mcp-session-id", self.session),
                    (b"mcp-protocol-version", b"2025-11-25"),
                ]
            )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "server": ("127.0.0.1", 80),
            "client": ("127.0.0.1", 12345),
        }
        received = False
        pending = b""
        messages = []
        status = None

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await asyncio.Future()

        async def send(event):
            nonlocal pending, status
            if event["type"] == "http.response.start":
                status = event["status"]
                response_headers = dict(event["headers"])
                self.session = response_headers.get(b"mcp-session-id", self.session)
            elif event["type"] == "http.response.body":
                pending += event.get("body", b"").replace(b"\r\n", b"\n")
                while b"\n\n" in pending:
                    frame, pending = pending.split(b"\n\n", 1)
                    for line in frame.splitlines():
                        if not line.startswith(b"data: "):
                            continue
                        message = json.loads(line[6:])
                        messages.append(message)
                        if message.get("method") == "elicitation/create":
                            self.elicitations.append(message["params"])
                            assert self.on_elicit is not None
                            reply = self.on_elicit(message["params"])
                            if hasattr(reply, "__await__"):
                                reply = await reply
                            reply_status, _ = await self.post(
                                {"jsonrpc": "2.0", "id": message["id"], "result": reply}
                            )
                            assert reply_status == 202

        async with asyncio.timeout(10):
            await self.app(scope, receive, send)
        if pending.strip():
            messages.append(json.loads(pending))
        return status, messages

    async def rpc(self, method, params=None):
        self.request_id += 1
        body = {"jsonrpc": "2.0", "id": self.request_id, "method": method}
        if params is not None:
            body["params"] = params
        status, messages = await self.post(body)
        assert status == 200
        return next(message for message in messages if message.get("id") == body["id"])

    async def initialize(self, capabilities):
        result = await self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": capabilities,
                "clientInfo": {"name": "offline-tests", "version": "1"},
            },
        )
        assert "result" in result and self.session
        status, _ = await self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert status == 202


def test_http_send_requires_initialized_host_elicitation(rig):
    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = _HTTP(app, rig.settings.token)
            await http.initialize({"elicitation": {"form": {}}})
            listed = await http.rpc("tools/list")
            assert len(listed["result"]["tools"]) == 33
            by_name = {tool["name"]: tool for tool in listed["result"]["tools"]}
            assert set(by_name["send_draft"]["inputSchema"]["properties"]) == {
                "token",
                "draft_id",
            }
            preview = await http.rpc(
                "tools/call", {"name": "preview_send", "arguments": {"draft_id": DRAFT_ID}}
            )
            payload = preview["result"]["structuredContent"]

            def accept(prompt):
                assert rig.smtp.calls == []
                assert prompt["mode"] == "form"
                assert prompt["requestedSchema"]["required"] == ["confirm"]
                assert prompt["requestedSchema"]["properties"]["confirm"]["type"] == "boolean"
                assert SUBJECT in prompt["message"]
                assert payload["draft_hash"][:16] in prompt["message"]
                return {"action": "accept", "content": {"confirm": True}}

            http.on_elicit = accept
            result = await http.rpc(
                "tools/call", {"name": "send_draft", "arguments": {"token": payload["token"]}}
            )
            assert not result["result"].get("isError")
            assert result["result"]["structuredContent"]["sent"] is True
            assert len(http.elicitations) == 1

    asyncio.run(run())
    assert len(rig.smtp.sent) == 1 and rig.smtp.sent[0][2] == rig.client.raw


@pytest.mark.parametrize("capabilities", [{}, {"elicitation": {"url": {}}}])
def test_http_host_without_form_support_refuses_a_form_token_and_audits(rig, capabilities):
    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            with_form = _HTTP(app, rig.settings.token)
            await with_form.initialize({"elicitation": {"form": {}}})
            preview = await with_form.rpc(
                "tools/call", {"name": "preview_send", "arguments": {"draft_id": DRAFT_ID}}
            )
            token = preview["result"]["structuredContent"]["token"]
            http = _HTTP(app, rig.settings.token)
            await http.initialize(capabilities)
            result = await http.rpc(
                "tools/call", {"name": "send_draft", "arguments": {"token": token}}
            )
            assert result["result"]["isError"] is True
            assert http.elicitations == []

    asyncio.run(run())
    assert rig.smtp.calls == []
    assert records(rig)[-1]["result"] == "confirmation_unavailable"


@pytest.mark.parametrize("extra", ["confirm", "confirmation", "accepted", "ctx"])
def test_http_caller_confirmation_arguments_are_refused_and_audited(rig, extra, caplog):
    async def run():
        app = create_app(rig.settings, rig.client)
        async with app.app.router.lifespan_context(app.app):
            http = _HTTP(app, rig.settings.token)
            await http.initialize({"elicitation": {"form": {}}})
            preview = await http.rpc(
                "tools/call", {"name": "preview_send", "arguments": {"draft_id": DRAFT_ID}}
            )
            token = preview["result"]["structuredContent"]["token"]
            result = await http.rpc(
                "tools/call",
                {"name": "send_draft", "arguments": {"token": token, extra: PASSWORD}},
            )
            assert result["result"]["isError"] is True
            assert http.elicitations == []
            visible = json.dumps(result) + caplog.text
            assert token not in visible and PASSWORD not in visible

    asyncio.run(run())
    assert rig.smtp.calls == []
    assert records(rig) == []
    with rig.log.transaction(readonly=True) as transaction:
        audit = [row for row in transaction.records() if row.get("tool") == "send_draft"]
    assert [row["outcome"] for row in audit] == ["started", "invalid_arguments"]
