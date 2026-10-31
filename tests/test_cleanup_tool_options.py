"""The public MCP boundary defaults to compact cleanup results, strict limits and flags."""

import pytest
from merged_tools import tool_call
from test_server import client as client
from test_server import mail as mail
from test_server import rpc

ACTIONS = (
    "move_to_folder",
    "move_to_trash",
    "archive",
    "mark_read",
    "mark_unread",
    "add_label",
    "remove_label",
    "apply_rules",
)


@pytest.mark.parametrize("action", ACTIONS)
def test_cleanup_tool_defaults_slim_and_verbose_is_explicit(client, mail, monkeypatch, action):
    payload = {
        "approved_total": 1,
        "done": 1,
        "remaining": 0,
        "uncertain": 0,
        "run_done": 1,
        "run_id": "synthetic-run",
        "continuation_token": None,
        "stop_reason": None,
        "expires_at": "2026-10-01T00:00:00+00:00",
        "messages": [
            {
                "message_id": "<approved@example.invalid>",
                "status": "done",
                "subject": "Synthetic subject",
                "uid_before": "1",
            }
        ],
    }
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return payload

    monkeypatch.setattr(mail, action, execute)
    for verbose in (None, False, True):
        arguments = {"token": "synthetic-preview", "limit": 1}
        if verbose is not None:
            arguments["verbose"] = verbose
        tool, merged = tool_call(action, arguments)
        result = rpc(client, "tools/call", {"name": tool, "arguments": merged})["result"]
        assert not result.get("isError", False), result
        data = result["structuredContent"]
        assert ("messages" in data) is (verbose is True)
        assert data["message_ids"] == ["<approved@example.invalid>"]
        assert data["done"] == data["run_done"] == 1
        assert data["available"] is False
        assert calls[-1] == {"token": "synthetic-preview", "limit": 1}
    assert payload["messages"][0]["subject"] == "Synthetic subject"


INVALID_LIMITS = [False, True, 0, -1, 1001, 1.5, "30"]


@pytest.mark.parametrize(
    ("action", "limit"),
    [
        (action, limit)
        for action in ("preview_action", "move_to_folder", "apply_rules")
        for limit in INVALID_LIMITS
    ]
    # Execute limits stay 1..200; a preview limit above 200 is a bounded approval.
    + [("move_to_folder", 201), ("apply_rules", 201)],
)
def test_tool_limit_rejects_coercion_and_invalid_values(client, mail, action, limit):
    arguments = (
        {"action": "archive", "query": {}}
        if action == "preview_action"
        else {"token": "synthetic-preview"}
    )
    before = list(mail.calls)
    tool, merged = tool_call(action, {**arguments, "limit": limit})
    result = rpc(client, "tools/call", {"name": tool, "arguments": merged})["result"]
    assert result["isError"] is True
    assert mail.calls == before


@pytest.mark.parametrize("limit", [30, 200, 201, 1000])
def test_tool_preview_forwards_limit(client, mail, limit):
    # Above 200 the service checks PROTON_MCP_APPROVAL_MAX_MESSAGES; the schema
    # admits limits up to its 1000 ceiling.
    result = rpc(
        client,
        "tools/call",
        {"name": "preview_action", "arguments": {"action": "archive", "query": {}, "limit": limit}},
    )["result"]
    assert not result.get("isError", False)
    assert mail.calls[-1][1]["limit"] == limit


@pytest.mark.parametrize("mark_read", ["yes", "true", "1", 1, 0, None])
@pytest.mark.parametrize("tool", ["preview_rules", "preview_sieve_filter"])
def test_tool_mark_read_rejects_coercion(client, mail, tool, mark_read):
    arguments = (
        {"name": "n", "folder": "Folders/A", "conditions": [{"field": "to", "is": "x"}]}
        if tool == "preview_sieve_filter"
        else {"folder": "INBOX"}
    )
    before = list(mail.calls)
    result = rpc(
        client, "tools/call", {"name": tool, "arguments": {**arguments, "mark_read": mark_read}}
    )["result"]
    assert result["isError"] is True
    assert mail.calls == before
    result = rpc(
        client, "tools/call", {"name": tool, "arguments": {**arguments, "mark_read": True}}
    )["result"]
    assert not result.get("isError", False)
    defaults = {"match": "all"} if tool == "preview_sieve_filter" else {}
    assert mail.calls[-1] == (tool, {**arguments, **defaults, "mark_read": True})
