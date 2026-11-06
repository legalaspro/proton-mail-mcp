"""A cancelled MCP call cannot end its initialized, in-memory session."""

import threading

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import MailError
from proton_mail_mcp.server import create_mcp


@pytest.mark.parametrize("worker_fails", [False, True])
def test_cancelled_tool_late_completion_keeps_session_serving(worker_fails):
    started, release = threading.Event(), threading.Event()

    class FakeMail:
        def count_mail(self, **kwargs):
            return {"folder": "INBOX", "count": 2}

        def search_mail(self, **kwargs):
            started.set()
            assert release.wait(5), "Test did not release its fake mail worker"
            if worker_fails:
                raise MailError("Synthetic mailbox failure")
            return {"messages": [], "partial": False}

    settings = Settings(
        token="cancellation-test-token-" + "x" * 32,
        bridge_user="reader@example.test",
        confirm_mutations=False,
        enable_send=True,
    )
    mcp = create_mcp(settings, FakeMail())

    async def exercise():
        client_send, server_receive = anyio.create_memory_object_stream(16)
        server_send, client_receive = anyio.create_memory_object_stream(16)
        finished = anyio.Event()
        handle_request = mcp._mcp_server._handle_request

        async def observe_completion(message, *args, **kwargs):
            await handle_request(message, *args, **kwargs)
            if message.request_id == 2:
                finished.set()

        mcp._mcp_server._handle_request = observe_completion

        async def send(payload):
            await client_send.send(SessionMessage(JSONRPCMessage.model_validate(payload)))

        async def receive():
            with anyio.fail_after(5):
                response = await client_receive.receive()
            return response.message.model_dump(by_alias=True, exclude_none=True)

        async def serve():
            await mcp._mcp_server.run(
                server_receive,
                server_send,
                mcp._mcp_server.create_initialization_options(),
                raise_exceptions=True,
            )

        async with anyio.create_task_group() as group:
            group.start_soon(serve)
            try:
                await send(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "cancellation-test", "version": "1"},
                        },
                    }
                )
                assert (await receive())["id"] == 1
                await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                await send(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "search_mail", "arguments": {"from_": "sender"}},
                    }
                )
                assert await anyio.to_thread.run_sync(lambda: started.wait(5))
                await send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": 2, "reason": "Client deadline expired"},
                    }
                )
                cancelled = await receive()
                assert cancelled["id"] == 2
                assert cancelled["error"]["message"] == "Request cancelled"
                release.set()
                with anyio.fail_after(5):
                    await finished.wait()
                await send(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "count_mail", "arguments": {}},
                    }
                )
                # No second response to request 2 may precede this response.
                counted = await receive()
                assert counted["id"] == 3
                assert counted["result"]["structuredContent"] == {"folder": "INBOX", "count": 2}
                await send({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
                listed = await receive()
                assert listed["id"] == 4
                assert len(listed["result"]["tools"]) == 25
            finally:
                release.set()
                group.cancel_scope.cancel()

    anyio.run(exercise)
