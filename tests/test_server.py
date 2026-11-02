import asyncio
import hmac
import json
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from merged_tools import tool_call
from starlette.testclient import TestClient
from test_send import Host

from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import BridgeUnavailable, MailClient
from proton_mail_mcp.server import BearerAuth, create_app, create_mcp, main

TOKEN = "server-token-secret-" + "x" * 32
PASSWORD = "backend-password-secret-never-print"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MCP_HEADERS = {**AUTH, "Accept": "application/json, text/event-stream"}


class FakeMail:
    def __init__(self):
        self.calls = []
        self.failure = None

    def _record(self, name, kwargs):
        if self.failure:
            raise self.failure
        self.calls.append((name, kwargs))

    def search_mail(self, **kwargs):
        self._record("search_mail", kwargs)
        return {
            "messages": [
                {
                    "message_id": "<one@example.test>",
                    "uid": "42",
                    "folder": "INBOX",
                    "from": "from@example.test",
                    "to": "to@example.test",
                    "subject": "Example",
                    "date": "Wed, 14 Oct 2026 10:00:00 +0000",
                    "snippet": "Hello",
                }
            ],
            "next_cursor": None,
            "total": 1,
            "partial": False,
            "total_is_exact": True,
        }

    def count_mail(self, **kwargs):
        self._record("count_mail", kwargs)
        return {"folder": "INBOX", "count": 1}

    def reindex_preview(self, **kwargs):
        self._record("reindex_preview", kwargs)
        return {
            "indexed_count": 80,
            "all_mail_count": 100,
            "missing_count": 20,
            "eta_seconds": 5,
            "token": "synthetic-preview",
            "log_id": "a" * 32,
        }

    def reindex_execute(self, **kwargs):
        self._record("reindex_execute", kwargs)
        return {"state": "running", "run_id": "b" * 32, "log_id": "a" * 32}

    def reindex_status(self, **kwargs):
        self._record("reindex_status", kwargs)
        return {
            "state": "running",
            "run_id": "b" * 32,
            "percent_done": 50,
            "eta_seconds": 5,
            "last_error": None,
            "last_full_run": {"old_count": 80, "new_count": 50, "all_mail_count": 100},
            "indexed_count": 82,
            "indexed_max_uid": "105",
            "all_mail_count": 102,
            "all_mail_max_uid": "125",
            "all_mail_observed_at": "2026-10-28T10:00:00+00:00",
            "last_catchup_at": "2026-10-28T09:00:00+00:00",
            "last_expunge_reconciliation_at": "2026-10-28T10:00:00+00:00",
            "swap_outcome": None,
            "swap_reason": None,
        }

    def reindex_cancel(self, **kwargs):
        self._record("reindex_cancel", kwargs)
        return {"state": "cancelled", "run_id": kwargs["run_id"], "log_id": "a" * 32}

    def read_thread(self, **kwargs):
        self._record("read_thread", kwargs)
        return {
            "messages": [
                {
                    "message_id": "<one@example.test>",
                    "text": "Hello",
                    "attachments": [],
                    "truncated": False,
                }
            ],
            "partial": False,
        }

    def preview_rules(self, **kwargs):
        self._record("preview_rules", kwargs)
        return {"rules": [], "token": None}

    def preview_create_folder(self, **kwargs):
        self._record("preview_create_folder", kwargs)
        return {"exists": False, "token": "synthetic-preview"}

    def create_draft(self, **kwargs):
        self._record("create_draft", kwargs)
        return "<draft@example.test>"

    def preview_action(self, **kwargs):
        self._record("preview_action", kwargs)
        return {"count": 1, "samples": [], "blocked": [], "token": "synthetic-preview"}

    def list_actions(self, **kwargs):
        self._record("list_actions", kwargs)
        return {"actions": [], "truncated": False}

    def check_move_target(self, **kwargs):
        """A read-only precheck, not a backend change: routing tests ignore it."""

    def __getattr__(self, name):
        if name in {
            "move_to_trash",
            "archive",
            "move_to_folder",
            "delete_draft",
            "mark_read",
            "mark_unread",
            "add_label",
            "remove_label",
            "undo_action",
            "unsubscribe",
            "apply_rules",
            "create_folder",
            "preview_sieve_filter",
            "sieve_filter_ready",
            "create_sieve_filter",
            "preview_add_rule",
            "add_rule_ready",
            "add_rule",
            "list_rules",
        }:

            def action(**kwargs):
                self._record(name, kwargs)
                if name in {
                    "move_to_trash",
                    "archive",
                    "move_to_folder",
                    "mark_read",
                    "mark_unread",
                    "add_label",
                    "remove_label",
                    "apply_rules",
                }:
                    return {"log_ids": ["a" * 32], "partial": False}
                return {"log_id": "a" * 32, "partial": False}

            return action
        raise AttributeError(name)


@pytest.fixture
def mail():
    return FakeMail()


@pytest.fixture
def client(mail):
    # Stateless JSON routing tests; host confirmation has its own tests.
    settings = Settings(token=TOKEN, bridge_user="bridge@example.test", confirm_mutations=False)
    app = create_app(settings, mail_client=mail)
    with TestClient(app, base_url="http://127.0.0.1") as http:
        yield http


def flat(tool: dict) -> str:
    """A listed tool's description on one line, as a model reads it."""
    return " ".join(tool["description"].split())


def rpc(client, method, params=None, request_id=1):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    response = client.post("/mcp", json=body, headers=MCP_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/", "/unknown"])
@pytest.mark.parametrize("method", ["GET", "POST", "OPTIONS", "HEAD"])
def test_authentication_is_required_for_every_http_path_and_method(client, path, method):
    response = client.request(method, path)
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize(
    "authorization",
    ["", "Bearer wrong", f"Basic {TOKEN}", f"Bearer{TOKEN}", "Bearer", f"Bearer {TOKEN} x"],
)
def test_bad_authorization_is_rejected(client, authorization):
    response = client.get("/mcp", headers={"Authorization": authorization})
    assert response.status_code == 401
    assert TOKEN not in response.text


def test_duplicate_authorization_headers_are_rejected(client):
    response = client.get(
        "/mcp",
        headers=[
            ("Authorization", f"Bearer {TOKEN}"),
            ("Authorization", f"Bearer {TOKEN}"),
        ],
    )
    assert response.status_code == 401


def test_non_ascii_token_is_rejected_without_server_error(client):
    response = client.get("/mcp", headers=[(b"Authorization", b"Bearer caf\xe9")])
    assert response.status_code == 401


def test_tokens_are_compared_in_constant_time(client, monkeypatch):
    compare = Mock(wraps=hmac.compare_digest)
    monkeypatch.setattr(hmac, "compare_digest", compare)
    response = client.get("/unknown", headers=AUTH)
    assert response.status_code == 404
    compare.assert_called_once()
    left, right = compare.call_args.args
    assert isinstance(left, bytes)
    assert isinstance(right, bytes)
    assert left == right == TOKEN.encode()


@pytest.mark.parametrize("scope_type", ["websocket", "unsupported"])
@pytest.mark.parametrize("authenticated", [False, True])
def test_non_http_scopes_are_rejected_before_auth(monkeypatch, scope_type, authenticated):
    inner, receive, send = AsyncMock(), AsyncMock(), AsyncMock()
    compare = Mock(wraps=hmac.compare_digest)
    monkeypatch.setattr(hmac, "compare_digest", compare)
    scope = {
        "type": scope_type,
        "headers": [(b"authorization", f"Bearer {TOKEN}".encode())] if authenticated else [],
    }
    app = BearerAuth(inner, TOKEN)
    if scope_type == "websocket":
        asyncio.run(app(scope, receive, send))
        send.assert_awaited_once_with({"type": "websocket.close", "code": 1008})
    else:
        with pytest.raises(RuntimeError, match="^Unsupported ASGI scope$"):
            asyncio.run(app(scope, receive, send))
        send.assert_not_awaited()
    inner.assert_not_awaited()
    receive.assert_not_awaited()
    compare.assert_not_called()


def test_lifespan_scope_is_delegated_without_auth(monkeypatch):
    inner, receive, send = AsyncMock(), AsyncMock(), AsyncMock()
    compare = Mock(wraps=hmac.compare_digest)
    monkeypatch.setattr(hmac, "compare_digest", compare)
    scope = {"type": "lifespan"}
    asyncio.run(BearerAuth(inner, TOKEN)(scope, receive, send))
    inner.assert_awaited_once_with(scope, receive, send)
    compare.assert_not_called()


def test_initialize_and_exact_tool_surface(client):
    initialized = rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "offline-tests", "version": "1"},
        },
    )
    assert "result" in initialized
    listed = rpc(client, "tools/list", request_id=2)
    tools = listed["result"]["tools"]
    assert len(tools) == 30
    assert {tool["name"] for tool in tools} == {
        "show_thread",
        "compose_reply",
        "search_mail",
        "count_mail",
        "reindex",
        "reindex_status",
        "read_thread",
        "list_attachments",
        "read_attachment",
        "save_attachment",
        "forward_message",
        "list_from_addresses",
        "create_draft",
        "preview_action",
        "list_actions",
        "move",
        "delete_draft",
        "set_read_state",
        "label",
        "undo_action",
        "unsubscribe",
        "preview_rules",
        "apply_rules",
        "preview_create_folder",
        "create_folder",
        "preview_sieve_filter",
        "create_sieve_filter",
        "preview_add_rule",
        "add_rule",
        "list_rules",
    }
    search = next(tool for tool in tools if tool["name"] == "search_mail")
    assert set(search["inputSchema"]["properties"]) == {
        "from_",
        "to",
        "subject",
        "text",
        "since",
        "before",
        "folder",
        "limit",
        "cursor",
        "include_snippets",
        "unread_only",
        "exclude_subject",
        "exclude_text",
        "query",
    }
    count = next(tool for tool in tools if tool["name"] == "count_mail")
    assert set(count["inputSchema"]["properties"]) == set(search["inputSchema"]["properties"]) - {
        "limit",
        "cursor",
        "include_snippets",
    }
    assert "required" not in count["inputSchema"]
    assert count["inputSchema"]["additionalProperties"] is False
    draft = next(tool for tool in tools if tool["name"] == "create_draft")
    assert set(draft["inputSchema"]["required"]) == {"to", "subject", "body"}
    assert draft["inputSchema"]["properties"]["cc"]["default"] is None
    by_name = {tool["name"]: tool for tool in tools}
    for name in (
        "search_mail",
        "count_mail",
        "reindex_status",
        "read_thread",
        "preview_action",
        "list_actions",
        "preview_rules",
        "preview_create_folder",
        "preview_sieve_filter",
        "preview_add_rule",
        "list_rules",
    ):
        assert by_name[name]["annotations"]["readOnlyHint"] is True
    selectors = {"move": {"target"}, "set_read_state": {"state"}, "label": {"op"}}
    for name in ("move", "delete_draft", "set_read_state", "label", "unsubscribe", "apply_rules"):
        tool = by_name[name]
        expected = (
            {"token"} if name in {"unsubscribe", "delete_draft"} else {"token", "limit", "verbose"}
        )
        required = {"token"} | selectors.get(name, set())
        assert set(tool["inputSchema"]["properties"]) == expected | required
        assert set(tool["inputSchema"]["required"]) == required
        assert tool["annotations"]["readOnlyHint"] is False
        assert tool["annotations"]["destructiveHint"] is True
        assert "only call after the user explicitly approved the preview" in flat(tool)
    assert by_name["undo_action"]["annotations"]["destructiveHint"] is True
    preview = by_name["preview_create_folder"]
    assert set(preview["inputSchema"]["properties"]) == {"name"}
    assert preview["inputSchema"]["required"] == ["name"]
    create = by_name["create_folder"]
    assert set(create["inputSchema"]["properties"]) == {"token"}
    assert create["inputSchema"]["required"] == ["token"]
    assert create["inputSchema"]["additionalProperties"] is False
    assert create["annotations"]["readOnlyHint"] is False
    assert create["annotations"]["destructiveHint"] is False
    assert create["annotations"]["idempotentHint"] is True
    assert create["annotations"]["openWorldHint"] is False
    assert "only call after explicit approval of its preview" in flat(create)
    sieve = by_name["create_sieve_filter"]
    assert set(sieve["inputSchema"]["properties"]) == {"token"}
    assert sieve["inputSchema"]["required"] == ["token"]
    assert sieve["inputSchema"]["additionalProperties"] is False
    assert sieve["annotations"]["readOnlyHint"] is False
    assert sieve["annotations"]["destructiveHint"] is True
    assert sieve["annotations"]["openWorldHint"] is False
    assert "only call after explicit approval of its preview" in flat(sieve)
    saved = by_name["add_rule"]
    assert set(saved["inputSchema"]["properties"]) == {"token"}
    assert saved["inputSchema"]["required"] == ["token"]
    assert saved["inputSchema"]["additionalProperties"] is False
    assert saved["annotations"]["readOnlyHint"] is False
    assert saved["annotations"]["destructiveHint"] is True
    assert saved["annotations"]["openWorldHint"] is False
    assert "only call after explicit approval of its preview" in flat(saved)
    assert set(by_name["preview_add_rule"]["inputSchema"]["properties"]) == {
        "name",
        "folder",
        "conditions",
        "match",
        "mark_read",
        "include_matches",
    }
    assert by_name["list_rules"]["inputSchema"]["properties"] == {}
    assert by_name["reindex_status"]["inputSchema"]["properties"] == {}
    assert by_name["reindex_status"]["annotations"]["destructiveHint"] is False
    status_description = flat(by_name["reindex_status"])
    assert "last_full_run.all_mail_count is the count at that full rebuild" in status_description
    assert "all_mail_count is the live count" in status_description
    thread_description = flat(by_name["read_thread"])
    assert "every message's canonical fields" in thread_description
    assert "including direct real-folder reads" in thread_description
    assert 'partial_reason="thread expansion limited"' in thread_description
    tool = by_name["reindex"]
    assert set(tool["inputSchema"]["properties"]) == {"op", "token", "run_id"}
    assert tool["inputSchema"]["required"] == ["op"]
    assert tool["inputSchema"]["additionalProperties"] is False
    assert tool["annotations"]["readOnlyHint"] is False
    assert tool["annotations"]["destructiveHint"] is False
    assert tool["annotations"]["openWorldHint"] is False
    assert "only call after the user explicitly approved the preview" in flat(tool)
    for apps_enabled, enable_send, count in (
        (True, True, 33),
        (False, False, 28),
        (False, True, 30),
    ):
        settings = Settings(
            token=TOKEN,
            bridge_user="bridge@example.test",
            confirm_mutations=False,
            apps_enabled=apps_enabled,
            enable_send=enable_send,
            send_from_allowlist=("bridge@example.test",),
        )
        configured = asyncio.run(create_mcp(settings, FakeMail()).list_tools())
        expected = set(by_name)
        if not apps_enabled:
            expected -= {"show_thread", "compose_reply"}
        if enable_send:
            expected |= {"preview_send", "send_draft"}
            if apps_enabled:
                expected.add("review_send")
        assert len(configured) == count
        assert {tool.name for tool in configured} == expected


@pytest.mark.parametrize("accepted", [True, False])
def test_delete_draft_uses_the_stored_host_confirmation(tmp_path, accepted):
    settings = Settings(
        token=TOKEN,
        bridge_user="bridge@example.test",
        action_log_path=str(tmp_path / "actions.jsonl"),
    )
    backend = MailClient(settings)
    mcp = create_mcp(settings, backend)
    tokens = backend.cleanup.tokens
    token, _ = tokens.issue(
        "delete_draft",
        {"folder": "Folders/Unsent"},
        {"ids": ["<draft@example.test>"]},
        [{"message_id": "<draft@example.test>", "folder": "Folders/Unsent"}],
    )

    def execute(token):
        assert tokens.claim(token, "delete_draft").confirmed is True
        return {"log_id": "a" * 32, "count": 1, "partial": False, "token_consumed": True}

    backend.delete_draft = Mock(side_effect=execute)
    host = Host("accept" if accepted else "decline")

    def call():
        return asyncio.run(
            mcp._tool_manager.call_tool("delete_draft", {"token": token}, context=host)
        )

    if accepted:
        assert call()["count"] == 1
        backend.delete_draft.assert_called_once_with(token=token)
    else:
        with pytest.raises(ToolError, match="User confirmation was not accepted"):
            call()
        backend.delete_draft.assert_not_called()
    assert len(host.prompts) == 1
    sentence = 'Permanently delete 1 message(s) from Drafts ("Folders/Unsent"); cannot be undone.'
    assert sentence in host.prompts[0]


def test_tailnet_hostname_and_https_origin_are_allowed_only_when_configured(mail):
    hostname = "mac.example.ts.net"
    app = create_app(
        Settings(
            token=TOKEN,
            bridge_user="bridge@example.test",
            public_host=hostname,
            confirm_mutations=False,
        ),
        mail_client=mail,
    )
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    with TestClient(app, base_url=f"https://{hostname}") as http:
        response = http.post(
            "/mcp",
            json=body,
            headers={
                **MCP_HEADERS,
                "Origin": f"https://{hostname}",
            },
        )
        assert response.status_code == 200
        response = http.post(
            "/mcp",
            json=body,
            headers={
                **MCP_HEADERS,
                "Host": "attacker.example.test",
            },
        )
        assert response.status_code == 421
        response = http.post(
            "/mcp",
            json=body,
            headers={
                **MCP_HEADERS,
                "Origin": "https://attacker.example.test",
            },
        )
        assert response.status_code == 403


@pytest.mark.parametrize("token", [None, "short"])
def test_main_refuses_missing_or_short_token_before_starting(monkeypatch, capsys, token):
    if token is not None:
        monkeypatch.setenv("PROTON_MCP_TOKEN", token)
    monkeypatch.setenv("PROTON_BRIDGE_USER", "bridge@example.test")
    run = Mock()
    monkeypatch.setattr("proton_mail_mcp.server.uvicorn.run", run)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2
    run.assert_not_called()
    assert TOKEN not in capsys.readouterr().err


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("search_mail", {"subject": "Example", "limit": 5}),
        (
            "search_mail",
            {
                "unread_only": True,
                "exclude_subject": "weekly",
                "exclude_text": "unsubscribe",
                "query": {"or": [{"from": "a@example.test"}, {"not": {"subject": "b"}}]},
            },
        ),
        ("count_mail", {"from_": "a@example.test", "unread_only": True, "since": "2026-10-01"}),
        ("count_mail", {"query": {"not": {"text": "draft"}}, "folder": "Archive"}),
        ("reindex_preview", {}),
        ("reindex_execute", {"token": "synthetic-preview"}),
        ("reindex_status", {}),
        ("reindex_cancel", {"run_id": "b" * 32}),
        ("read_thread", {"message_id": "<one@example.test>"}),
        ("preview_rules", {"folder": "INBOX"}),
        ("apply_rules", {"token": "synthetic-preview"}),
        ("preview_create_folder", {"name": "Folders/Reviewed"}),
        ("create_folder", {"token": "synthetic-preview"}),
        (
            "preview_sieve_filter",
            {
                "name": "GitHub",
                "folder": "Folders/GitHub",
                "conditions": [{"field": "from", "contains": "notifications@github.com"}],
                "match": "any",
                "mark_read": True,
            },
        ),
        ("create_sieve_filter", {"token": "synthetic-preview"}),
        (
            "preview_add_rule",
            {
                "name": "GitHub",
                "folder": "Folders/GitHub",
                "conditions": [{"field": "from", "is": "notifications@github.com"}],
                "match": "any",
                "mark_read": True,
                "include_matches": True,
            },
        ),
        ("add_rule", {"token": "synthetic-preview"}),
        ("list_rules", {}),
        ("unsubscribe", {"token": "synthetic-preview"}),
        ("delete_draft", {"token": "synthetic-preview"}),
        ("preview_action", {"action": "delete_draft", "ids": ["<draft@example.test>"]}),
        ("preview_action", {"action": "archive", "query": {"folder": "INBOX"}}),
        ("move_to_trash", {"token": "synthetic-preview"}),
        ("archive", {"token": "synthetic-preview"}),
        ("move_to_folder", {"token": "synthetic-preview"}),
        ("preview_rules", {"folder": "INBOX", "mark_read": True}),
        ("preview_rules", {"folder": "INBOX", "limit": 25, "order": "newest"}),
        ("preview_rules", {"folder": "INBOX", "limit": 1, "cursor": "synthetic-cursor"}),
        ("mark_read", {"token": "synthetic-preview"}),
        ("mark_unread", {"token": "synthetic-preview"}),
        ("add_label", {"token": "synthetic-preview"}),
        ("remove_label", {"token": "synthetic-preview"}),
        ("undo_action", {"log_id": "synthetic-log"}),
        ("list_actions", {"token": "synthetic-preview"}),
        (
            "create_draft",
            {
                "to": "to@example.test",
                "cc": "cc@example.test",
                "subject": "Re: Example",
                "body": "A reply",
                "in_reply_to": "<one@example.test>",
            },
        ),
    ],
)
def test_tools_call_injected_backend_without_network(client, mail, name, arguments):
    tool, merged = tool_call(name, arguments)
    result = rpc(client, "tools/call", {"name": tool, "arguments": merged})["result"]
    assert not result.get("isError", False), result
    called_name, actual = mail.calls[-1]
    assert called_name == name
    assert actual.items() >= arguments.items()


def test_search_mail_passes_new_options_only_when_chosen(client, mail):
    arguments = {"subject": "Example", "unread_only": False}
    result = rpc(client, "tools/call", {"name": "search_mail", "arguments": arguments})["result"]
    assert not result.get("isError", False), result
    # Earlier searches reach the backend with exactly the arguments they always had.
    assert set(mail.calls[-1][1]) == {
        "from_",
        "to",
        "subject",
        "text",
        "since",
        "before",
        "folder",
        "limit",
        "cursor",
        "include_snippets",
    }


@pytest.mark.parametrize("name", ["search_mail", "count_mail"])
@pytest.mark.parametrize("value", ["true", 1, None])
def test_unread_only_accepts_only_a_json_boolean(client, mail, name, value):
    result = rpc(client, "tools/call", {"name": name, "arguments": {"unread_only": value}})
    assert result["result"].get("isError") is True
    assert mail.calls == []


@pytest.mark.parametrize("extra", [{"limit": 5}, {"cursor": "x"}, {"include_snippets": False}])
def test_count_mail_refuses_paging_arguments(client, mail, extra):
    result = rpc(client, "tools/call", {"name": "count_mail", "arguments": extra})["result"]
    assert result.get("isError") is True and mail.calls == []


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("reindex", {}),
        ("reindex", {"token": "synthetic-preview"}),
        ("reindex", {"op": "resume"}),
        ("reindex", {"op": "execute"}),
        ("reindex", {"op": "execute", "token": "synthetic-preview", "confirm": True}),
        ("reindex", {"op": "execute", "token": "synthetic-preview", "run_id": "b" * 32}),
        ("reindex", {"op": "cancel"}),
        ("reindex", {"op": "cancel", "run_id": "b" * 32, "token": "synthetic-preview"}),
        ("reindex", {"op": "preview", "folder": "INBOX"}),
        ("reindex", {"op": "preview", "token": "synthetic-preview"}),
        ("reindex", {"op": "preview", "run_id": "b" * 32}),
        ("reindex_status", {"run_id": "b" * 32}),
    ],
)
def test_reindex_tools_accept_only_their_declared_arguments(client, mail, name, arguments):
    result = rpc(client, "tools/call", {"name": name, "arguments": arguments})["result"]
    assert result.get("isError") is True and mail.calls == []


def test_reindex_already_running_preserves_run_id(client, mail):
    run_id = "b" * 32

    def already_running(**kwargs):
        mail._record("reindex_execute", kwargs)
        return {"error": "already running", "state": "running", "run_id": run_id}

    mail.reindex_execute = already_running
    result = rpc(
        client,
        "tools/call",
        {"name": "reindex", "arguments": {"op": "execute", "token": "synthetic-preview"}},
    )["result"]
    assert not result.get("isError", False), result
    assert result["structuredContent"] == {
        "error": "already running",
        "state": "running",
        "run_id": run_id,
    }


def test_reindex_status_preserves_live_counts_full_run_and_sync_fields(client):
    result = rpc(client, "tools/call", {"name": "reindex_status", "arguments": {}})["result"]
    assert not result.get("isError", False), result
    status = result["structuredContent"]
    assert status == {
        "state": "running",
        "run_id": "b" * 32,
        "percent_done": 50,
        "eta_seconds": 5,
        "last_error": None,
        "last_full_run": {"old_count": 80, "new_count": 50, "all_mail_count": 100},
        "indexed_count": 82,
        "indexed_max_uid": "105",
        "all_mail_count": 102,
        "all_mail_max_uid": "125",
        "all_mail_observed_at": "2026-10-28T10:00:00+00:00",
        "last_catchup_at": "2026-10-28T09:00:00+00:00",
        "last_expunge_reconciliation_at": "2026-10-28T10:00:00+00:00",
        "swap_outcome": None,
        "swap_reason": None,
    }


def test_create_draft_accepts_omitted_cc(client, mail):
    result = rpc(
        client,
        "tools/call",
        {
            "name": "create_draft",
            "arguments": {"to": "to@example.test", "subject": "Example", "body": "Hello"},
        },
    )["result"]
    assert not result.get("isError", False), result
    assert mail.calls[-1][0] == "create_draft"
    assert mail.calls[-1][1]["cc"] is None


@pytest.mark.parametrize("extra", ["params", "ids", "action", "folder"])
def test_cleanup_rejects_extra_confirmation_arguments_without_leaking_token(client, mail, extra):
    result = rpc(
        client,
        "tools/call",
        {
            "name": "move",
            "arguments": {"target": "Archive", "token": TOKEN, extra: PASSWORD},
        },
    )["result"]
    assert result.get("isError") is True
    assert mail.calls == []
    assert TOKEN not in json.dumps(result) and PASSWORD not in json.dumps(result)


@pytest.mark.parametrize("token", [[PASSWORD], {"secret": PASSWORD}])
def test_cleanup_schema_validation_does_not_echo_secret_inputs(client, mail, token):
    result = rpc(
        client,
        "tools/call",
        {
            "name": "move",
            "arguments": {"target": "Archive", "token": token},
        },
    )["result"]
    assert result.get("isError") is True
    assert mail.calls == []
    assert PASSWORD not in json.dumps(result)


@pytest.mark.parametrize("extra", ["name", "folder", "params", "ids", "action"])
def test_create_folder_rejects_changed_payload_beside_token(client, mail, extra):
    result = rpc(
        client,
        "tools/call",
        {"name": "create_folder", "arguments": {"token": TOKEN, extra: PASSWORD}},
    )["result"]
    assert result.get("isError") is True and not mail.calls
    assert TOKEN not in json.dumps(result) and PASSWORD not in json.dumps(result)


@pytest.mark.parametrize("name", ["rules_timer", "rules_run", "enable_rules_timer"])
def test_rules_timer_is_not_available_as_a_tool(client, mail, name):
    result = rpc(client, "tools/call", {"name": name, "arguments": {}})["result"]
    assert result.get("isError") is True and not mail.calls


@pytest.mark.parametrize("failure", [RuntimeError, ValueError])
@pytest.mark.parametrize(
    "name,arguments",
    [
        ("search_mail", {"subject": "Example"}),
        ("count_mail", {"subject": "Example"}),
        ("preview_action", {"action": "archive", "query": {}}),
        ("move", {"target": "Archive", "token": "synthetic-preview"}),
        ("undo_action", {"log_id": "synthetic-log"}),
        ("list_actions", {"token": "synthetic-preview"}),
        ("preview_create_folder", {"name": "Folders/Reviewed"}),
        ("create_folder", {"token": "synthetic-preview"}),
        (
            "preview_sieve_filter",
            {"name": "n", "folder": "Folders/A", "conditions": [{"field": "to", "is": "x"}]},
        ),
        ("create_sieve_filter", {"token": "synthetic-preview"}),
        (
            "preview_add_rule",
            {"name": "n", "folder": "Folders/A", "conditions": [{"field": "to", "is": "x"}]},
        ),
        ("add_rule", {"token": "synthetic-preview"}),
        ("list_rules", {}),
    ],
)
def test_mail_backend_error_does_not_leak_secrets(client, mail, caplog, failure, name, arguments):
    mail.failure = failure(f"Failure with {TOKEN} and {PASSWORD}")
    result = rpc(
        client,
        "tools/call",
        {
            "name": name,
            "arguments": arguments,
        },
    )
    assert result["result"].get("isError") is True
    visible = json.dumps(result) + caplog.text
    assert "Mailbox operation failed; check Bridge configuration and availability" in visible
    assert "Validation error:" not in visible
    assert TOKEN not in visible
    assert PASSWORD not in visible


@pytest.mark.parametrize("name", ["search_mail", "count_mail"])
def test_bridge_not_running_is_said_with_its_address(client, mail, name):
    mail.failure = BridgeUnavailable("127.0.0.1", 1143)
    result = rpc(client, "tools/call", {"name": name, "arguments": {"subject": "Example"}})
    assert result["result"].get("isError") is True
    visible = json.dumps(result)
    assert "Proton Mail Bridge is not running: nothing answers on 127.0.0.1:1143." in visible
    assert "Mailbox operation failed" not in visible


@pytest.mark.parametrize(
    "name,arguments,expected",
    [
        ("search_mail", {"limit": 0}, "Limit must be a positive integer"),
        ("search_mail", {"since": TOKEN}, "Dates must use YYYY-MM-DD"),
        ("search_mail", {"before": "2026-02-30"}, "Dates must use a valid YYYY-MM-DD date"),
        (
            "search_mail",
            {"folder": "INBOX\n" + PASSWORD},
            "Mail search and header values must contain no control characters",
        ),
        (
            "search_mail",
            {"subject": "Subject\n" + TOKEN},
            "Mail search and header values must contain no control characters",
        ),
        (
            "search_mail",
            {"query": {"or": [{"from": "a"}, {"subject": "b\r\n" + TOKEN}]}},
            "Mail search and header values must contain no control characters",
        ),
        (
            "search_mail",
            {"exclude_text": "x\n" + TOKEN},
            "Mail search and header values must contain no control characters",
        ),
        ("search_mail", {"query": {"cc": TOKEN}}, "Unsupported query key"),
        ("search_mail", {"exclude_subject": ""}, "exclude_subject must be nonempty text"),
        ("count_mail", {"since": TOKEN}, "Dates must use YYYY-MM-DD"),
        (
            "count_mail",
            {"folder": "INBOX\n" + PASSWORD},
            "Mail search and header values must contain no control characters",
        ),
        ("count_mail", {"query": {"not": [TOKEN]}}, "Each query must be a nonempty object"),
        (
            "count_mail",
            {"query": {"or": [{"text": TOKEN[:3]}] * 21}},
            "Query or needs a list of 1 to 20 queries",
        ),
        (
            "read_thread",
            {"message_id": PASSWORD + "\r\nInjected: value"},
            "Message-ID must be one bounded, control-free token",
        ),
        (
            "create_draft",
            {"to": PASSWORD, "subject": "Example", "body": "Hello"},
            "Recipients must be valid email addresses",
        ),
        (
            "create_draft",
            {"to": "", "subject": "Example", "body": "Hello"},
            "At least one recipient is required",
        ),
        (
            "create_draft",
            {"to": "(" * 500 + "a@b" + ")" * 500, "subject": "Example", "body": "Hello"},
            "Recipients must be valid email addresses",
        ),
        (
            "create_draft",
            {"to": "@b.test", "subject": "Example", "body": "Hello"},
            "Recipients must be valid email addresses",
        ),
        (
            "create_draft",
            {
                "to": "to@example.test",
                "subject": "Example",
                "body": "Hello",
                "in_reply_to": TOKEN + " trailing",
            },
            "Message-ID must be one bounded, control-free token",
        ),
    ],
)
def test_invalid_mail_input_reports_safe_validation_error_without_session(
    monkeypatch, caplog, name, arguments, expected
):
    settings = Settings(
        token=TOKEN,
        bridge_user="bridge@example.test",
        confirm_mutations=False,
        index_autorebuild_enabled=False,
    )
    backend = MailClient(settings)
    session = Mock(side_effect=AssertionError("Validation must precede a Bridge session"))
    monkeypatch.setattr(backend, "_session", session)
    app = create_app(settings, mail_client=backend)
    with TestClient(app, base_url="http://127.0.0.1") as http:
        result = rpc(http, "tools/call", {"name": name, "arguments": arguments})["result"]
    assert result.get("isError") is True
    visible = json.dumps(result) + caplog.text
    assert "Validation error: " + expected in visible
    assert "Bridge configuration and availability" not in visible
    assert TOKEN not in visible
    assert PASSWORD not in visible
    session.assert_not_called()


@pytest.mark.parametrize(
    "header,status",
    [
        ("Host", 421),
        ("Origin", 403),
        ("Content-Type", 400),
    ],
)
@pytest.mark.parametrize("value", [TOKEN, PASSWORD], ids=["token", "password"])
def test_rejected_http_headers_do_not_log_secrets(client, caplog, header, status, value):
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
        },
        headers={**MCP_HEADERS, header: value},
    )
    assert response.status_code == status
    assert TOKEN not in response.text + caplog.text
    assert PASSWORD not in response.text + caplog.text
    assert "HTTP transport rejected a request" in caplog.text
