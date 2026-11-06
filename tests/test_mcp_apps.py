"""MCP Apps capabilities, resources and view tool permissions."""

import asyncio
import json
from types import SimpleNamespace

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ClientCapabilities
from test_send_http import _HTTP
from test_server import FakeMail
from view_assertions import assert_self_contained_view, tool_calls, view_data

from proton_mail_mcp import mcp_apps
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.server import create_app, create_mcp

TOKEN = "mcp-apps-token-" + "x" * 32
APPS = {mcp_apps.EXTENSION_ID: {"mimeTypes": [mcp_apps.MIME_TYPE]}}
# Every tool that issues an approval token, redeems one or changes mail.
MODEL_ONLY = {
    "reindex",
    "preview_action",
    "create_folder",
    "preview_sieve_filter",
    "create_sieve_filter",
    "preview_add_rule",
    "add_rule",
    "unsubscribe",
    "preview_rules",
    "apply_rules",
    "delete_draft",
    "move",
    "set_read_state",
    "label",
    "undo_action",
}


class NoMail:
    """A backend without mail: any mailbox method a tool reaches is recorded and fails."""

    def __init__(self):
        self.touched = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise AttributeError(name)


def settings(**overrides):
    values = {"token": TOKEN, "bridge_user": "bridge@example.test", "confirm_mutations": False}
    return Settings(**{**values, **overrides})


def test_views_read_their_data_from_the_result_or_again_through_their_own_tool():
    bridge = mcp_apps.view_html("thread").split("var PMM = (function", 1)[1]
    assert f'var VIEW_KEY = "{mcp_apps.VIEW_META_KEY}";' in bridge
    view_data_code = bridge.split("function viewData", 1)[1].split("\n  }", 1)[0]
    assert "result._meta" in view_data_code
    # A host may pass the data on as structuredContent; the server never sends any.
    assert "structured(result)" in view_data_code
    # The re-read calls only the view's own tool, with the arguments of the host's tool-input.
    reread = bridge.split("function startReread", 1)[1].split("\n  }\n", 1)[0]
    assert "callTool(VIEW_TOOL, args," in reread
    assert "waitForInput(" in reread
    assert bridge.count("callTool(") == 2  # its definition and the re-read
    for name in mcp_apps.VIEWS:
        code = assert_self_contained_view(mcp_apps.view_html(name), allowed_iframes=True)
        assert f'var VIEW_TOOL = "{mcp_apps.VIEW_TOOLS[name]}";' in code
        assert f'var VIEW_NAME = "{name}";' in code
        view_script = code.split("var PMM = (function", 1)[1].split("})();", 1)[1]
        assert "PMM.load(result)" in view_script
        assert "PMM.retry()" in view_script
        assert "PMM.viewData(" not in view_script
        assert "structuredContent" not in view_script
    # Only the composer reads structuredContent: create_draft's own result.
    reply = mcp_apps.view_html("reply").split("var PMM = (function", 1)[1].split("})();", 1)[1]
    assert reply.count("PMM.structured(") == 1
    assert "PMM.structured(result)" in reply.split("function savedId", 1)[1].split("\n  }", 1)[0]


@pytest.mark.parametrize(
    "capabilities,expected",
    [
        ({"extensions": APPS}, True),
        ({"experimental": APPS}, True),
        ({"extensions": {mcp_apps.EXTENSION_ID: {"mimeTypes": ["text/html"]}}}, False),
        ({"extensions": {mcp_apps.EXTENSION_ID: {}}}, False),
        ({"extensions": {"other/extension": {}}}, False),
        ({}, False),
    ],
)
def test_client_declared_mcp_apps_support_is_detected(capabilities, expected):
    params = SimpleNamespace(capabilities=ClientCapabilities.model_validate(capabilities))
    ctx = SimpleNamespace(session=SimpleNamespace(client_params=params))
    assert mcp_apps.client_ui_support(ctx) is expected


def test_unknown_client_support_is_null():
    class Stateless:
        @property
        def session(self):
            raise ValueError("no initialized session")

    assert mcp_apps.client_ui_support(Stateless()) is None
    ctx = SimpleNamespace(session=SimpleNamespace(client_params=None))
    assert mcp_apps.client_ui_support(ctx) is None


@pytest.mark.parametrize("capabilities", [{"extensions": APPS}, {"elicitation": {"form": {}}}])
def test_initialized_http_session_reads_thread_view_with_and_without_extension(capabilities):
    backend = FakeMail()

    async def run():
        app = create_app(settings(confirm_mutations=True), backend)
        async with app.app.router.lifespan_context(app.app):
            http = _HTTP(app, TOKEN)
            await http.initialize(capabilities)
            listed = await http.rpc("resources/list")
            resources = listed["result"]["resources"]
            assert {item["uri"] for item in resources} == {
                mcp_apps.THREAD_URI,
                mcp_apps.REPLY_URI,
            }
            [resource] = [item for item in resources if item["uri"] == mcp_apps.THREAD_URI]
            assert resource["mimeType"] == mcp_apps.MIME_TYPE
            assert resource["_meta"] == mcp_apps.RESOURCE_META
            read = await http.rpc("resources/read", {"uri": mcp_apps.THREAD_URI})
            [content] = read["result"]["contents"]
            assert content["mimeType"] == mcp_apps.MIME_TYPE
            assert content["_meta"] == mcp_apps.RESOURCE_META
            assert content["text"] == mcp_apps.view_html("thread")
            called = await http.rpc(
                "tools/call",
                {"name": "show_thread", "arguments": {"message_id": "<one@example.test>"}},
            )
            return called["result"]

    result = asyncio.run(run())
    assert result["isError"] is False
    [item] = json.loads(result["content"][0]["text"])["messages"]
    assert item["message_id"] == "<one@example.test>"
    assert item["text"] == "Hello"
    assert view_data(result)["view"] == "thread"
    assert view_data(result)["messages"][0]["body"] == {"kind": "text", "text": "Hello"}
    assert backend.calls == [
        (
            "read_thread",
            {"message_id": "<one@example.test>", "include_thread": True, "prefer_html": True},
        )
    ]


@pytest.mark.parametrize("enable_rules", [True, False])
def test_views_may_call_only_create_draft_and_the_read_only_view_tools(enable_rules):
    send = settings(
        enable_send=True, enable_rules=enable_rules, send_from_allowlist=("bridge@example.test",)
    )
    by_name = {tool.name: tool for tool in asyncio.run(create_mcp(send, NoMail()).list_tools())}
    restricted = {
        name
        for name, tool in by_name.items()
        if tool.meta is not None and tool.meta["ui"]["visibility"] == ["model"]
    }
    linked = {
        "show_thread": mcp_apps.THREAD_URI,
        "compose_reply": mcp_apps.REPLY_URI,
        "review_send": mcp_apps.REVIEW_SEND_URI,
    }
    assert set(mcp_apps.VIEW_TOOLS.values()) == set(linked)
    assert mcp_apps.VIEW_CALLABLE == {"create_draft", *linked}
    assert restricted == set(by_name) - mcp_apps.VIEW_CALLABLE
    # Every tool that issues or redeems a token, or changes mail, is model-only.
    rules = {"preview_rules", "apply_rules", "preview_add_rule", "add_rule"}
    rules |= {"preview_sieve_filter", "create_sieve_filter"}
    expected = MODEL_ONLY if enable_rules else MODEL_ONLY - rules
    assert expected | {"preview_send", "send_draft"} <= restricted
    # So is every other tool that reads mail: a view reads only through its own view tool.
    assert {"read_thread", "search_mail", "count_mail", "list_actions"} <= restricted
    for name in restricted:
        assert by_name[name].meta == {"ui": {"visibility": ["model"]}}
    # A view tool's view may call it again for data its host left out. Each reads only and
    # issues no token.
    for name, uri in linked.items():
        assert by_name[name].meta == {
            "ui": {"resourceUri": uri, "visibility": ["model", "app"]},
            "ui/resourceUri": uri,
        }
        assert by_name[name].annotations.readOnlyHint is True
    # The reply composer saves unsent drafts through the existing tool.
    assert by_name["create_draft"].meta is None


def test_views_call_only_the_tools_views_may_call():
    called = {
        name: tool_calls(assert_self_contained_view(mcp_apps.view_html(name), allowed_iframes=True))
        for name in mcp_apps.VIEWS
    }
    assert called == {
        "thread": {"show_thread"},
        "reply": {"compose_reply", "create_draft"},
        "review_send": {"review_send"},
    }
    assert set().union(*called.values()) == mcp_apps.VIEW_CALLABLE


def test_requests_to_see_or_answer_mail_are_routed_to_the_views_first():
    send = settings(enable_send=True, send_from_allowlist=("bridge@example.test",))
    mcp = create_mcp(send, NoMail())
    by_name = {tool.name: tool.description for tool in asyncio.run(mcp.list_tools())}
    # Purpose and routing open the description, so clients that cut descriptions keep them.
    first = {name: text.split("\n\n", 1)[0].replace("\n", " ") for name, text in by_name.items()}
    assert first["show_thread"] == (
        "Show an email or thread to the user in a read-only view: the default for any request "
        "to show, open, read, look at or display mail."
    )
    assert first["compose_reply"] == (
        "Open a reply or new-draft composer: the default for any request to reply, draft a "
        "reply, write back or compose."
    )
    assert first["read_thread"] == (
        "Read one message and its thread as plain text, for analysis or summaries; nothing is "
        "marked read. If show_thread is listed and the user wants to see or open mail, call "
        "show_thread instead."
    )
    assert first["create_draft"] == (
        "Save a new unsent draft in Drafts and return its Message-ID; this never sends. If "
        "compose_reply is listed and the user wants to reply or compose, call compose_reply "
        "first: this is the no-view path."
    )
    assert "show_thread, not read_thread" in mcp.instructions
    assert "compose_reply, not create_draft" in mcp.instructions
    plain = create_mcp(settings(apps_enabled=False), NoMail())
    assert "show_thread" not in plain.instructions


def test_disabling_apps_restores_the_plain_tool_surface():
    async def run(config):
        async with create_connected_server_and_client_session(create_mcp(config, NoMail())) as s:
            return (await s.list_tools()).tools, (await s.list_resources()).resources

    enabled_tools, enabled_resources = anyio.run(run, settings())
    disabled_tools, disabled_resources = anyio.run(run, settings(apps_enabled=False))
    assert disabled_resources == []
    assert {tool.name for tool in enabled_tools} - {tool.name for tool in disabled_tools} == {
        "show_thread",
        "compose_reply",
    }
    assert all(tool.meta is None for tool in disabled_tools)


@pytest.mark.parametrize(
    "value,expected", [(None, True), ("1", True), ("0", False)], ids=["default", "on", "off"]
)
def test_apps_switch_is_parsed_exactly(value, expected):
    env = {"PROTON_MCP_TOKEN": TOKEN, "PROTON_BRIDGE_USER": "bridge@example.test"}
    if value is not None:
        env["PROTON_MCP_APPS_ENABLED"] = value
    assert Settings.from_env(env).apps_enabled is expected


@pytest.mark.parametrize("value", ["", "true", "yes", " 1", "2"])
def test_apps_switch_refuses_other_values(value):
    env = {
        "PROTON_MCP_TOKEN": TOKEN,
        "PROTON_BRIDGE_USER": "bridge@example.test",
        "PROTON_MCP_APPS_ENABLED": value,
    }
    with pytest.raises(ConfigError, match="PROTON_MCP_APPS_ENABLED"):
        Settings.from_env(env)
    with pytest.raises(ConfigError, match="PROTON_MCP_APPS_ENABLED"):
        settings(apps_enabled=1)
