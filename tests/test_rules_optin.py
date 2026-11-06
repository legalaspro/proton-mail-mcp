"""PROTON_MCP_ENABLE_RULES: the folder rule and Sieve filter tools are listed only when it is 1."""

import asyncio
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient
from test_approval_binding import RULES, cleanup_case
from test_approval_binding import bridge as bridge  # noqa: F401
from test_audit_log import audits
from test_server import TOKEN, FakeMail, rpc
from test_server import mail as mail  # noqa: F401

from proton_mail_mcp import rules_timer
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.folder_rules import FolderRulesService
from proton_mail_mcp.rules_store import LocalRulesBackend, load_saved, store_path
from proton_mail_mcp.server import RULES_TOOLS, create_app, create_mcp, main
from proton_mail_mcp.sieve import filter_spec, render_script

BASE = {"PROTON_MCP_TOKEN": TOKEN, "PROTON_BRIDGE_USER": "bridge@example.test"}
OFF = (
    "Validation error: folder rules are off; the operator sets PROTON_MCP_ENABLE_RULES=1 and "
    "restarts the service"
)
# One valid call per rules tool, so a refusal is about the setting and nothing else.
CALLS = {
    "list_rules": {},
    "preview_rules": {"folder": "INBOX"},
    "apply_rules": {"token": "synthetic-preview"},
    "preview_add_rule": {
        "name": "Private rule name",
        "folder": "Folders/Reviewed",
        "conditions": [{"field": "subject", "contains": "Invoice"}],
    },
    "add_rule": {"token": "synthetic-preview"},
    "preview_sieve_filter": {
        "name": "Private filter name",
        "folder": "Folders/Reviewed",
        "conditions": [{"field": "subject", "contains": "Invoice"}],
    },
    "create_sieve_filter": {"token": "synthetic-preview"},
}


def test_the_setting_is_off_unless_exactly_1():
    assert Settings.from_env(BASE).enable_rules is False
    assert Settings.from_env({**BASE, "PROTON_MCP_ENABLE_RULES": "0"}).enable_rules is False
    assert Settings.from_env({**BASE, "PROTON_MCP_ENABLE_RULES": "1"}).enable_rules is True
    for value in ("", "true", "yes", "on", "2", " 1", "1 ", "01"):
        with pytest.raises(ConfigError, match="^PROTON_MCP_ENABLE_RULES must be 0 or 1$"):
            Settings.from_env({**BASE, "PROTON_MCP_ENABLE_RULES": value})
    for value in (1, 0, "1", None):
        with pytest.raises(ConfigError, match="PROTON_MCP_ENABLE_RULES must be boolean"):
            Settings(token=TOKEN, bridge_user="bridge@example.test", enable_rules=value)
    # Read once at startup: a running server's tool list cannot change.
    settings = Settings.from_env(BASE)
    with pytest.raises(FrozenInstanceError):
        settings.enable_rules = True


@pytest.mark.parametrize("value", ["yes", "2"])
def test_an_invalid_value_stops_startup_and_the_rules_timer(monkeypatch, capsys, value):
    run = Mock()
    monkeypatch.setattr("proton_mail_mcp.server.uvicorn.run", run)
    with pytest.raises(SystemExit) as stopped:
        main({**BASE, "PROTON_MCP_ENABLE_RULES": value})
    assert stopped.value.code == 2 and not run.called
    assert "PROTON_MCP_ENABLE_RULES must be 0 or 1" in capsys.readouterr().err
    # The timer builds the same settings, so the same value stops it before any mail is read.
    monkeypatch.setenv("PROTON_BRIDGE_USER", "synthetic-cli@example.test")
    monkeypatch.setenv("PROTON_MCP_ENABLE_RULES", value)
    monkeypatch.setattr("sys.argv", ["proton-mail-mcp-rules-run"])
    client = Mock(side_effect=AssertionError("no mailbox with an invalid setting"))
    monkeypatch.setattr(rules_timer, "MailClient", client)
    with pytest.raises(SystemExit) as stopped:
        rules_timer.main()
    assert stopped.value.code == 1 and not client.called


@pytest.mark.parametrize("value", [None, "0", "1"])
def test_the_rules_timer_runs_whatever_the_setting(monkeypatch, capsys, value):
    seen = []

    class Timer:
        def __init__(self, cleanup):
            pass

        def run(self, folder):
            return {"status": "initialized", "folder": folder}

    def client(settings):
        seen.append(settings.enable_rules)
        return Mock()

    monkeypatch.setenv("PROTON_BRIDGE_USER", "synthetic-cli@example.test")
    if value is None:
        monkeypatch.delenv("PROTON_MCP_ENABLE_RULES", raising=False)
    else:
        monkeypatch.setenv("PROTON_MCP_ENABLE_RULES", value)
    monkeypatch.setattr("sys.argv", ["proton-mail-mcp-rules-run"])
    monkeypatch.setattr(rules_timer, "MailClient", client)
    monkeypatch.setattr(rules_timer, "RulesTimer", Timer)
    with pytest.raises(SystemExit) as finished:
        rules_timer.main()
    assert finished.value.code == 0 and seen == [value == "1"]
    assert json.loads(capsys.readouterr().out)["status"] == "initialized"


@pytest.fixture
def off(mail, tmp_path):  # noqa: F811
    """The default server over HTTP, with an action log to check for audit records."""
    log = tmp_path / "actions.jsonl"
    settings = Settings(
        token=TOKEN, bridge_user="bridge@example.test", confirm_mutations=False, action_log_path=log
    )
    with TestClient(create_app(settings, mail_client=mail), base_url="http://127.0.0.1") as http:
        yield http, log


@pytest.mark.parametrize("name", sorted(CALLS))
def test_a_rules_tool_with_rules_off_refuses_before_anything_runs(off, mail, name):  # noqa: F811
    http, log = off
    listed = {tool["name"] for tool in rpc(http, "tools/list")["result"]["tools"]}
    assert len(listed) == 22 and not listed & RULES_TOOLS
    result = rpc(http, "tools/call", {"name": name, "arguments": CALLS[name]})["result"]
    assert result["isError"] is True
    assert [block["text"] for block in result["content"]] == [OFF]
    # No backend call, no audit record, and no argument echoed back.
    assert mail.calls == [] and not log.exists()
    assert "Private" not in json.dumps(result) and "synthetic-preview" not in json.dumps(result)


def test_with_rules_on_the_same_calls_reach_the_backend(mail, tmp_path):  # noqa: F811
    settings = Settings(
        token=TOKEN,
        bridge_user="bridge@example.test",
        confirm_mutations=False,
        enable_rules=True,
        action_log_path=tmp_path / "actions.jsonl",
    )
    with TestClient(create_app(settings, mail_client=mail), base_url="http://127.0.0.1") as http:
        for name, arguments in CALLS.items():
            result = rpc(http, "tools/call", {"name": name, "arguments": arguments})["result"]
            assert not result.get("isError", False), (name, result)
            assert OFF not in json.dumps(result)
    called = {name for name, _ in mail.calls}
    assert RULES_TOOLS <= called


def test_unknown_names_keep_the_sdks_refusal(off):
    http, _ = off
    result = rpc(http, "tools/call", {"name": "rules_run", "arguments": {}})["result"]
    assert result["isError"] is True and "Unknown tool" in json.dumps(result)


def test_the_tool_list_is_the_same_on_every_call():
    settings = Settings(token=TOKEN, bridge_user="bridge@example.test")
    mcp = create_mcp(settings, FakeMail())
    first = [tool.name for tool in asyncio.run(mcp.list_tools())]
    assert first == [tool.name for tool in asyncio.run(mcp.list_tools())]
    assert len(first) == 22 and not set(first) & RULES_TOOLS


def call(mcp, tool, arguments):
    return asyncio.run(mcp._tool_manager.call_tool(tool, arguments))


def test_rule_records_stay_findable_and_undoable_with_rules_off(bridge, tmp_path, monkeypatch):  # noqa: F811
    sim, client, items = cleanup_case(bridge, "INBOX", (), ())
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(RULES))
    monkeypatch.setattr(FolderRulesService.__init__, "__defaults__", (path,))
    settings = replace(client.settings, confirm_mutations=False)
    # A run and a saved rule while rules were on.
    on = create_mcp(replace(settings, enable_rules=True), client)
    preview = call(on, "preview_rules", {"folder": "INBOX"})
    applied = call(on, "apply_rules", {"token": preview["token"]})
    assert applied["done"] == 2 and {item.folder for item in items} == {"Folders/Reviewed"}
    rule = call(on, "preview_add_rule", CALLS["preview_add_rule"])
    assert call(on, "add_rule", {"token": rule["token"]})["added"] is True
    # Then a restart with rules off.
    off = create_mcp(settings, client)
    assert not {tool.name for tool in asyncio.run(off.list_tools())} & RULES_TOOLS
    found = call(off, "list_actions", {"action": "apply_rules"})
    assert [entry["tool"] for entry in found["audit"]] == ["apply_rules"]
    assert {run["log_ids"][0] for run in found["runs"]} == set(applied["log_ids"])
    saved = call(off, "list_actions", {"action": "add_rule"})
    assert [entry["tool"] for entry in saved["audit"]] == ["add_rule"]
    for log_id in applied["log_ids"]:
        assert call(off, "undo_action", {"log_id": log_id})["count"] >= 1
    assert {item.folder for item in items} == {"INBOX"}
    # The saved rule is kept, and the undo is audited under its own name.
    assert [rule.name for rule in load_saved(store_path(client.settings))[0]] == [
        CALLS["preview_add_rule"]["name"]
    ]
    tools = [records[-1]["tool"] for records in audits(Path(client.settings.action_log_path))]
    assert tools.count("undo_action") == len(applied["log_ids"])


def save_rule(path, name="Private rule name"):
    spec = filter_spec(name, "Folders/Reviewed", [{"field": "subject", "contains": "Invoice"}])
    LocalRulesBackend(path).create_filter(name, render_script(spec), timeout=5)


@pytest.mark.parametrize(
    "rules,enabled,notice",
    [("saved", "0", True), ("saved", "1", False), ("none", "0", False), ("broken", "0", False)],
)
def test_startup_notice_when_saved_rules_exist_but_the_tools_are_off(
    monkeypatch, capsys, tmp_path, rules, enabled, notice
):
    store = tmp_path / "state" / "saved-rules.json"
    if rules == "saved":
        save_rule(store)
    elif rules == "broken":
        store.parent.mkdir()
        store.write_text("{")
    run = Mock()
    monkeypatch.setattr("proton_mail_mcp.server.create_app", Mock())
    monkeypatch.setattr("proton_mail_mcp.server.uvicorn.run", run)
    main({**BASE, "PROTON_MCP_SAVED_RULES": str(store), "PROTON_MCP_ENABLE_RULES": enabled})
    run.assert_called_once()
    err = capsys.readouterr().err
    assert ("PROTON_MCP_ENABLE_RULES=1" in err) is notice
    # The notice never shows a rule, its folder or its conditions.
    assert "Private rule name" not in err and "Invoice" not in err and "Reviewed" not in err
