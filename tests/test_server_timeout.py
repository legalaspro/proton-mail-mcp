"""An injected IMAP timeout leaves the initialized MCP session serving calls."""

from datetime import timedelta

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from test_mail import FakeImap, message

from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import MailClient
from proton_mail_mcp.security import Credentials
from proton_mail_mcp.server import create_mcp


@pytest.mark.parametrize("inject_timeout", [False, True], ids=["default", "forced-timeout"])
def test_imap_timeout_keeps_initialized_mcp_session_serving(inject_timeout):
    settings = Settings(
        token="offline-session-timeout-token-" + "x" * 32,
        bridge_user="reader@example.test",
        confirm_mutations=False,
        enable_send=True,
    )
    connections = []
    injected = False

    class TrackedImap(FakeImap):
        def uid(self, command, *args):
            nonlocal injected
            if inject_timeout and not injected:
                injected = True
                raise TimeoutError("Synthetic test-only IMAP timeout")
            return super().uid(command, *args)

        def shutdown(self):
            self.calls.append(("SHUTDOWN",))

    def factory(host, port, *, timeout):
        connection = TrackedImap({"INBOX": {"7": message("<7@example.test>")}})
        connections.append(connection)
        return connection

    backend = MailClient(
        settings,
        lambda _: Credentials("reader@example.test", "dummy"),
        factory,
    )

    def assert_search_succeeded(result):
        assert result.isError is False
        content = result.structuredContent
        assert content["partial"] is False
        assert not content.get("timed_out", False)
        assert [item["message_id"] for item in content["messages"]] == ["<7@example.test>"]

    async def exercise():
        with anyio.fail_after(5):
            async with create_connected_server_and_client_session(
                create_mcp(settings, backend),
                read_timeout_seconds=timedelta(seconds=2),
                raise_exceptions=True,
            ) as session:
                first = await session.call_tool("search_mail", {"include_snippets": False})
                assert len(connections) == 1
                if inject_timeout:
                    assert first.isError is False
                    assert first.structuredContent["timed_out"] is True
                    assert first.structuredContent["partial"] is True
                    assert first.structuredContent["messages"] == []
                    assert "timed out" in first.structuredContent["error"].lower()
                    assert connections[0].calls[-1] == ("SHUTDOWN",)
                    assert not any(call[0] == "LOGOUT" for call in connections[0].calls)
                else:
                    assert_search_succeeded(first)
                    assert connections[0].calls[-1] == ("LOGOUT",)
                first_calls = list(connections[0].calls)

                status = await session.call_tool("reindex_status", {})
                assert status.isError is False
                assert status.structuredContent["state"] == "incomplete"
                assert len(connections) == 1

                recovered = await session.call_tool("search_mail", {"include_snippets": False})
                assert_search_succeeded(recovered)
                assert len(connections) == 2
                assert connections[1] is not connections[0]
                assert connections[0].calls == first_calls
                assert connections[1].calls[-1] == ("LOGOUT",)
                tools = (await session.list_tools()).tools
                assert len(tools) == 25
                assert all(
                    tool.annotations.readOnlyHint is True
                    for tool in tools
                    if tool.name in {"search_mail", "reindex_status"}
                )

    anyio.run(exercise)
    assert injected is inject_timeout
    assert {call[0] for connection in connections for call in connection.calls} <= {
        "STARTTLS",
        "LOGIN",
        "CAPABILITY",
        "LIST",
        "EXAMINE",
        "SEARCH",
        "FETCH",
        "LOGOUT",
        "SHUTDOWN",
    }
