"""Mutation tokens need a host-form user confirmation; offline doubles only."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from bridge_sim import mutations
from mcp.server.fastmcp.exceptions import ToolError
from merged_tools import tool_call
from sieve_fakes import FakeSieveBackend
from test_approval_binding import CLEANUP, RULES, cleanup_case, creates
from test_approval_binding import bridge as bridge  # noqa: F401
from test_create_folder import FolderBridge
from test_send import Host
from test_send_http import _HTTP
from test_unsubscribe import URL
from test_unsubscribe import setup as unsubscribe_mailbox

from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.folder_rules import FolderRulesService
from proton_mail_mcp.guarded import TokenStore
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.rules_store import load_saved, store_path
from proton_mail_mcp.server import RULES_TOOLS, create_app, create_mcp

BASE = {"PROTON_MCP_TOKEN": "confirmation-test-token-" + "x" * 32, "PROTON_BRIDGE_USER": "u"}
TOOLS = [action for action, *_ in CLEANUP] + ["apply_rules", "unsubscribe", "create_folder"]
TOOLS += ["create_sieve_filter", "add_rule"]
ROWS = [{"folder": "INBOX", "uidvalidity": "7", "uid": "1", "message_id": "<a@example.test>"}]


@pytest.fixture
def case(bridge, tmp_path, monkeypatch):  # noqa: F811
    """Build one MCP server, its real MailClient and one previewed token per tool."""

    def build(tool, **settings):
        if tool == "unsubscribe":
            client, _ = unsubscribe_mailbox(
                Settings(
                    token=BASE["PROTON_MCP_TOKEN"],
                    bridge_user="reader@example.test",
                    action_log_path=str(tmp_path / "unsubscribe-actions.jsonl"),
                )
            )
            requests = []
            monkeypatch.setattr(
                "proton_mail_mcp.unsubscribe_http.perform",
                lambda target, *, timeout: requests.append(target["url"]) or 200,
            )
            changed = lambda: bool(requests)  # noqa: E731
        elif tool == "create_folder":
            sim, client = bridge(FolderBridge)
            changed = lambda: bool(creates(sim))  # noqa: E731
        elif tool == "create_sieve_filter":
            sim, client = bridge()
            client.sieve_backend = FakeSieveBackend()
            changed = lambda: bool(client.sieve_backend.created)  # noqa: E731
        elif tool == "add_rule":
            sim, client = bridge()
            store = store_path(client.settings)
            changed = lambda: bool(load_saved(store)[0])  # noqa: E731
        else:
            _, source, _, labels, flags = next(
                (row for row in CLEANUP if row[0] == tool), CLEANUP[0]
            )
            sim, client, _ = cleanup_case(bridge, source, labels, flags)
            changed = lambda: bool(mutations(sim))  # noqa: E731
        # The rule and Sieve tools are registered only with PROTON_MCP_ENABLE_RULES=1.
        settings = {"enable_rules": tool in RULES_TOOLS, **settings}
        mcp = create_mcp(replace(client.settings, **settings), client)
        if tool == "apply_rules":
            path = tmp_path / "confirmation-rules.json"
            path.write_text(json.dumps(RULES))
            monkeypatch.setattr(FolderRulesService.__init__, "__defaults__", (path,))
            preview = client.preview_rules("INBOX")
        elif tool == "unsubscribe":
            preview = client.preview_action("unsubscribe", query={})
        elif tool == "create_folder":
            preview = client.preview_action("create_folder", params={"folder": "Folders/Confirmed"})
        elif tool == "create_sieve_filter":
            preview = client.preview_sieve_filter(
                "Invoices", "Folders/Reviewed", [{"field": "subject", "contains": "Invoice"}]
            )
        elif tool == "add_rule":
            preview = client.preview_add_rule(
                "Invoices", "Folders/Reviewed", [{"field": "subject", "contains": "Invoice"}]
            )
        else:
            _, source, params, *_ = next(row for row in CLEANUP if row[0] == tool)
            preview = client.preview_action(tool, query={"folder": source}, params=params)
        assert preview["token"] and preview["action_hash"], preview
        return SimpleNamespace(client=client, mcp=mcp, preview=preview, changed=changed)

    return build


def call(mcp, action, arguments, host=None):
    tool, arguments = tool_call(action, arguments)
    return asyncio.run(mcp._tool_manager.call_tool(tool, arguments, context=host))


def test_confirmation_defaults_on_and_parses_only_exact_flags():
    assert Settings.from_env(BASE).confirm_mutations is True
    assert (
        Settings.from_env({**BASE, "PROTON_MCP_CONFIRM_MUTATIONS": "0"}).confirm_mutations is False
    )
    assert Settings.from_env({**BASE, "PROTON_MCP_CONFIRM_TIMEOUT": "30"}).confirm_timeout == 30
    for value in ("", "true", "yes", "2", " 1", "0 "):
        with pytest.raises(ConfigError):
            Settings.from_env({**BASE, "PROTON_MCP_CONFIRM_MUTATIONS": value})
    for value in ("0", "-1", "301", "nan", "inf"):
        with pytest.raises(ConfigError):
            Settings.from_env({**BASE, "PROTON_MCP_CONFIRM_TIMEOUT": value})
    for value in (1, None, "1"):
        with pytest.raises(ConfigError):
            Settings(token=BASE["PROTON_MCP_TOKEN"], bridge_user="u", confirm_mutations=value)


@pytest.mark.parametrize("confirm,send,stateless", [(1, 0, False), (0, 1, False), (0, 0, True)])
def test_confirmation_uses_stateful_sessions_for_form_elicitation(confirm, send, stateless):
    settings = Settings(
        token=BASE["PROTON_MCP_TOKEN"],
        bridge_user="sender@example.test",
        confirm_mutations=bool(confirm),
        enable_send=bool(send),
    )
    mcp = create_mcp(settings, mail_client=SimpleNamespace())
    assert mcp.settings.stateless_http is stateless
    assert mcp.settings.json_response is stateless


def test_store_requires_confirmation_of_exactly_the_described_approval():
    tokens = TokenStore(confirmation_required=True)
    token, _ = tokens.issue("archive", {}, {"query": {}}, ROWS)
    for operation in (tokens.inspect, tokens.claim):
        with pytest.raises(MailValidationError, match="needs the user's confirmation"):
            operation(token, "archive")
    described = tokens.describe(token, "archive")
    assert described.confirmed is False and described.action_hash
    with pytest.raises(MailValidationError, match="changed during confirmation"):
        tokens.confirm(token, "archive", "0" * 64)
    tokens.confirm(token, "archive", described.action_hash)
    claimed = tokens.claim(token, "archive")
    assert claimed.confirmed is True
    continuation = tokens.continue_grant(claimed, ROWS)
    assert tokens.inspect(continuation, "archive").confirmed is True

    revoked, _ = tokens.issue("archive", {}, {"query": {}}, ROWS)
    tokens.revoke(revoked, "archive")
    with pytest.raises(MailValidationError, match="already used"):
        tokens.describe(revoked, "archive")
    plain = TokenStore()
    library, _ = plain.issue("archive", {}, {"query": {}}, ROWS)
    assert plain.confirmation_required is False
    assert plain.inspect(library, "archive").confirmed is True


@pytest.mark.parametrize("tool", TOOLS)
def test_host_without_form_support_refuses_and_retires_the_token(case, tool):
    built = case(tool)
    host = Host(support=False)
    with pytest.raises(ToolError, match="User confirmation was not accepted"):
        call(built.mcp, tool, {"token": built.preview["token"]}, host)
    assert host.prompts == [] and not built.changed()
    with pytest.raises(ToolError, match="already used"):
        call(built.mcp, tool, {"token": built.preview["token"]}, Host())
    assert not built.changed()


@pytest.mark.parametrize("tool", TOOLS)
def test_declined_form_refuses_and_retires_the_token(case, tool):
    built = case(tool)
    host = Host("decline")
    with pytest.raises(ToolError, match="User confirmation was not accepted"):
        call(built.mcp, tool, {"token": built.preview["token"]}, host)
    assert len(host.prompts) == 1 and not built.changed()
    with pytest.raises(ToolError, match="already used"):
        call(built.mcp, tool, {"token": built.preview["token"]}, Host())
    assert not built.changed()


@pytest.mark.parametrize("tool", TOOLS)
def test_accepted_form_redeems_exactly_the_previewed_change_once(case, tool):
    built = case(tool)
    host = Host()
    result = call(built.mcp, tool, {"token": built.preview["token"]}, host)
    assert built.changed() and len(host.prompts) == 1
    assert result.get("partial") is False, result
    prompt = host.prompts[0]
    assert built.preview["action_hash"][:16] in prompt
    assert json.loads(prompt.split("\n", 1)[1])["action_hash"] == built.preview["action_hash"]
    assert built.preview["token"] not in prompt
    with pytest.raises(ToolError, match="already used"):
        call(built.mcp, tool, {"token": built.preview["token"]}, Host())


@pytest.mark.parametrize("tool", TOOLS)
def test_mcp_issued_token_cannot_be_redeemed_around_the_tool(case, tool):
    built = case(tool)
    method = getattr(built.client, tool)
    with pytest.raises(MailValidationError, match="needs the user's confirmation"):
        method(built.preview["token"])
    assert not built.changed()
    call(built.mcp, tool, {"token": built.preview["token"]}, Host())
    assert built.changed()


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("extra", ["confirm", "confirmed", "ctx", "approval"])
def test_confirmation_cannot_be_supplied_in_tool_arguments(case, tool, extra):
    built = case(tool)
    host = Host()
    with pytest.raises(ToolError) as error:
        call(built.mcp, tool, {"token": built.preview["token"], extra: True}, host)
    assert built.preview["token"] not in str(error.value)
    assert host.prompts == [] and not built.changed()
    call(built.mcp, tool, {"token": built.preview["token"]}, Host())
    assert built.changed()


@pytest.mark.parametrize(
    "host",
    [
        Host("cancel"),
        Host(RuntimeError("synthetic host failure")),
        Host(confirm=False),
    ],
    ids=["cancel", "error", "unchecked"],
)
def test_cancelled_failed_or_unchecked_forms_retire_the_token(case, host):
    built = case("archive")
    with pytest.raises(ToolError, match="User confirmation was not accepted"):
        call(built.mcp, "archive", {"token": built.preview["token"]}, host)
    assert len(host.prompts) == 1 and not built.changed()
    with pytest.raises(ToolError, match="already used"):
        call(built.mcp, "archive", {"token": built.preview["token"]}, Host())


def test_unanswered_form_times_out_and_retires_the_token(case):
    built = case("archive", confirm_timeout=0.05)

    class SlowHost(Host):
        async def elicit(self, *, message, schema):
            self.prompts.append(message)
            await asyncio.sleep(5)

    host = SlowHost()
    with pytest.raises(ToolError, match="User confirmation was not accepted"):
        call(built.mcp, "archive", {"token": built.preview["token"]}, host)
    assert len(host.prompts) == 1 and not built.changed()
    with pytest.raises(ToolError, match="already used"):
        call(built.mcp, "archive", {"token": built.preview["token"]}, Host())


def test_continuation_inherits_the_users_confirmation(case):
    built = case("archive")
    first = Host()
    result = call(built.mcp, "archive", {"token": built.preview["token"], "limit": 1}, first)
    assert result["done"] == 1 and result["continuation_token"] and len(first.prompts) == 1
    second = Host(support=False)
    rest = call(built.mcp, "archive", {"token": result["continuation_token"]}, second)
    assert rest["done"] == 1 and rest["run_done"] == 2 and second.prompts == []


@pytest.mark.parametrize("tool", TOOLS)
def test_operator_opt_out_restores_token_only_redemption(case, tool):
    built = case(tool, confirm_mutations=False)
    host = Host(support=False)
    call(built.mcp, tool, {"token": built.preview["token"]}, host)
    assert built.changed() and host.prompts == []


def test_summaries_name_targets_without_unsubscribe_secrets(case):
    expected = {
        "move_to_folder": ['"Folders/Reviewed"', '"mark_read": false'],
        "add_label": ['"Labels/Review"'],
        "apply_rules": ['"target": "Folders/Reviewed"', '"contains": "Invoice"'],
        "unsubscribe": ['"host": "news.example.com"', '"method": "POST"'],
        "create_folder": ['"Folders/Confirmed"'],
        "mark_read": ['"folders": ["INBOX"]'],
        "create_sieve_filter": [
            'filter "Invoices", which files matching new mail into "Folders/Reviewed".',
            '"script_sha256": "',
            ':contains \\"Subject\\" \\"Invoice\\"',
            'fileinto \\"Reviewed\\";',
        ],
        "add_rule": [
            'Save the folder rule "Invoices": when rules run, matching INBOX mail moves to '
            '"Folders/Reviewed".',
            '"conditions": [{"contains": "Invoice", "field": "subject"}]',
            '"rule_sha256": "',
        ],
    }
    for tool, fragments in expected.items():
        built = case(tool)
        host = Host("decline")
        with pytest.raises(ToolError):
            call(built.mcp, tool, {"token": built.preview["token"]}, host)
        assert all(fragment in host.prompts[0] for fragment in fragments), host.prompts[0]
        assert URL not in host.prompts[0] and "synthetic-query-secret" not in host.prompts[0]


@pytest.mark.parametrize(
    "capabilities,accepted", [({"elicitation": {"form": {}}}, True), ({}, False)]
)
def test_http_session_confirms_cleanup_through_host_form(bridge, capabilities, accepted):  # noqa: F811
    sim, client, _ = cleanup_case(bridge, "INBOX", (), ())

    async def run():
        app = create_app(client.settings, client)
        async with app.app.router.lifespan_context(app.app):
            http = _HTTP(app, client.settings.token)
            await http.initialize(capabilities)
            preview = await http.rpc(
                "tools/call",
                {"name": "preview_action", "arguments": {"action": "archive", "query": {}}},
            )
            payload = preview["result"]["structuredContent"]

            def answer(prompt):
                assert prompt["mode"] == "form" and not mutations(sim)
                assert payload["action_hash"][:16] in prompt["message"]
                return {"action": "accept", "content": {"confirm": True}}

            http.on_elicit = answer
            result = await http.rpc(
                "tools/call",
                {"name": "move", "arguments": {"target": "Archive", "token": payload["token"]}},
            )
            return result["result"], http.elicitations

    result, elicitations = asyncio.run(run())
    if accepted:
        assert not result.get("isError") and result["structuredContent"]["done"] == 2
        assert len(elicitations) == 1 and mutations(sim)
    else:
        assert result["isError"] is True and elicitations == [] and not mutations(sim)


@pytest.mark.parametrize("tool", TOOLS)
def test_invalid_or_expired_tokens_are_refused_before_any_form(case, tool):
    built = case(tool)
    host = Host()
    with pytest.raises(ToolError, match="already used"):
        call(built.mcp, tool, {"token": "not-a-preview-token"}, host)
    tokens = built.client.cleanup.tokens
    now = [tokens.clock() + tokens.ttl]
    tokens.clock = lambda: now[0]
    with pytest.raises(ToolError, match="expired"):
        call(built.mcp, tool, {"token": built.preview["token"]}, host)
    assert host.prompts == [] and not built.changed()


def test_token_for_another_action_is_refused_before_any_form_and_kept(case):
    built = case("archive")
    host = Host()
    with pytest.raises(ToolError, match="does not match the requested action"):
        call(built.mcp, "move_to_trash", {"token": built.preview["token"]}, host)
    assert host.prompts == [] and not built.changed()
    call(built.mcp, "archive", {"token": built.preview["token"]}, host)
    assert built.changed() and len(host.prompts) == 1
