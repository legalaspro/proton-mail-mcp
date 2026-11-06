"""preview_action(action="create_folder") replaces preview_create_folder with the same approval.

The golden values in fixtures/preview_golden.json were made on main before the change, by
preview_create_folder and each preview_action kind, with a fixed expires_at in the form text.
"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from bridge_sim import BridgeSim, Msg, hdr
from mcp.server.fastmcp.exceptions import ToolError
from merged_tools import tool_call
from test_approval_binding import CLEANUP
from test_approval_binding import bridge as bridge  # noqa: F401
from test_audit_log import audits
from test_create_folder import FolderBridge
from test_send import Host

from proton_mail_mcp.confirmation import approval_text, describe_approval
from proton_mail_mcp.guarded import approval_hash
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.server import create_mcp

GOLDEN = json.loads(
    Path(__file__).with_name("fixtures").joinpath("preview_golden.json").read_text("utf-8")
)
FIXED = "2026-11-06T12:00:00+00:00"


def form(client, action, token, expires_at=FIXED):
    """The host form's text for a stored approval, with its expiry fixed."""
    summary = describe_approval(client.cleanup, action, token)
    return approval_text({**summary, "expires_at": expires_at})


def grants(client):
    return dict(client.cleanup.tokens._grants)


def audited(client):
    """Audit records grouped per call; none while the log does not exist yet."""
    path = Path(client.settings.action_log_path)
    return audits(path) if path.exists() else []


def call(mcp, tool, arguments, host=None):
    return asyncio.run(mcp._tool_manager.call_tool(tool, arguments, context=host))


@pytest.mark.parametrize("name", sorted(GOLDEN["folders"]))
def test_the_folder_token_is_the_one_preview_create_folder_issued(bridge, name):  # noqa: F811
    _, client = bridge(FolderBridge)
    preview = client.preview_action("create_folder", params={"folder": name})
    golden = GOLDEN["folders"][name]
    assert set(preview) == {"action", "name", "exists", "token", "expires_at", "action_hash"}
    assert preview["action"] == "create_folder" and preview["name"] == name
    assert preview["exists"] is False and preview["token"] and preview["expires_at"]
    assert preview["action_hash"] == golden["action_hash"]
    assert preview["action_hash"] == approval_hash("create_folder", {"name": name}, {}, [])
    grant = client.cleanup.tokens.describe(preview["token"], "create_folder")
    assert (grant.action, grant.params, grant.selector, grant.messages) == (
        "create_folder",
        {"name": name},
        {},
        [],
    )
    assert form(client, "create_folder", preview["token"]) == golden["form"]
    # The backend method the old tool called still issues exactly the same approval.
    again = client.preview_create_folder(name)
    assert {key: again[key] for key in ("action", "name", "exists", "action_hash")} == {
        key: preview[key] for key in ("action", "name", "exists", "action_hash")
    }


@pytest.mark.parametrize("key", sorted(GOLDEN["messages"]))
def test_message_previews_are_unchanged(bridge, key):  # noqa: F811
    action, params = key.split(" ", 1)
    params = json.loads(params)
    rows = {row[0]: row for row in CLEANUP}
    _, source, _, labels, flags = rows.get(action, (action, "INBOX", None, (), ()))
    if action == "delete_draft":
        source, flags = "Drafts", (r"\Draft",)
    sim, client = bridge(BridgeSim)
    folder = "INBOX" if source.startswith("Labels/") else source
    for index in range(2):
        message = hdr(f"<binding-{index}@example.test>", subject=f"Invoice {index}")
        sim.add(Msg(message, folder=folder, labels=labels, flags=flags))
    preview = client.preview_action(action, query={"folder": source}, params=params)
    masked = {
        name: "<masked>" if name in {"token", "expires_at", "log_id"} and value else value
        for name, value in preview.items()
    }
    golden = GOLDEN["messages"][key]
    assert json.loads(json.dumps(masked, default=str)) == golden["preview"]
    assert form(client, action, preview["token"], "fixed") == golden["form"]


def test_an_existing_folder_needs_no_token(bridge):  # noqa: F811
    sim, client = bridge(FolderBridge)
    sim.custom_folders.append("Folders/Existing")
    before = grants(client)
    preview = client.preview_action("create_folder", params={"folder": "Folders/Existing"})
    assert preview["exists"] is True and preview["token"] is None
    assert preview["expires_at"] is None and preview["action_hash"] is None
    assert grants(client) == before


NEW = {"folder": "Folders/New"}


@pytest.mark.parametrize(
    "arguments,message",
    [
        ({"params": NEW, "query": {"folder": "INBOX"}}, "no query, ids or limit"),
        ({"params": NEW, "query": {}}, "no query, ids or limit"),
        ({"params": NEW, "ids": ["<a@example.test>"]}, "no query, ids or limit"),
        ({"params": NEW, "ids": []}, "no query, ids or limit"),
        ({"params": NEW, "limit": 1}, "no query, ids or limit"),
        ({"query": {"folder": "INBOX"}}, "no query, ids or limit"),
        ({}, "needs params"),
        ({"params": {}}, "needs params"),
        ({"params": None}, "needs params"),
        ({"params": ["Folders/New"]}, "needs params"),
        ({"params": {"name": "Folders/New"}}, "needs params"),
        ({"params": {"folder": "Folders/New", "mark_read": False}}, "needs params"),
        ({"params": {"folder": "Folders/New", "create_folder_if_missing": True}}, "needs params"),
        ({"params": {"folder": "INBOX"}}, "under Folders/"),
        ({"params": {"folder": "Labels/New"}}, "under Folders/"),
        ({"params": {"folder": "Folders/"}}, "under Folders/"),
        ({"params": {"folder": "Folders/a//b"}}, "under Folders/"),
        ({"params": {"folder": "Folders/../x"}}, "under Folders/"),
        ({"params": {"folder": "Folders/" + "x" * 493}}, "under Folders/"),
        ({"params": {"folder": 7}}, "under Folders/"),
    ],
)
def test_anything_but_one_folder_name_refuses_and_issues_nothing(bridge, arguments, message):  # noqa: F811
    sim, client = bridge(FolderBridge)
    before = grants(client)
    with pytest.raises(MailValidationError, match=message):
        client.preview_action("create_folder", **arguments)
    assert grants(client) == before
    # Refused before any connection: nothing reached Bridge.
    assert not sim.commands


@pytest.fixture
def served(bridge):  # noqa: F811
    """An MCP server over a folder-capable Bridge double, with the host form on."""
    sim, client = bridge(FolderBridge)
    return sim, client, create_mcp(client.settings, client)


def test_the_mcp_preview_redeems_with_create_folder_after_the_form(served):
    sim, client, mcp = served
    preview = call(
        mcp, "preview_action", {"action": "create_folder", "params": {"folder": "Folders/Mcp"}}
    )
    assert preview["action_hash"] == approval_hash("create_folder", {"name": "Folders/Mcp"}, {}, [])
    # A preview is read-only: it writes no audit record.
    assert audited(client) == []
    host = Host()
    result = call(mcp, "create_folder", {"token": preview["token"]}, host)
    assert result["created"] is True and result["token_consumed"] is True
    assert "Folders/Mcp" in sim.custom_folders and len(host.prompts) == 1
    assert 'Create the folder "Folders/Mcp".' in host.prompts[0]
    [records] = audited(client)
    finished = records[-1]
    assert finished["tool"] == "create_folder" and finished["outcome"] == "completed"
    assert finished["counts"] == {"approved": 1, "done": 1, "uncertain": 0, "remaining": 0}
    assert finished["exists"] is False and finished["log_ids"] == [result["log_id"]]
    assert finished["action_hash"] == preview["action_hash"]
    with pytest.raises(ToolError, match="already used"):
        call(mcp, "create_folder", {"token": preview["token"]}, Host())


def test_the_mcp_preview_refuses_a_message_selector_beside_a_folder(served):
    sim, client, mcp = served
    before = grants(client)
    for extra in ({"query": {}}, {"ids": ["<a@example.test>"]}, {"limit": 5}):
        arguments = {"action": "create_folder", "params": {"folder": "Folders/X"}, **extra}
        with pytest.raises(ToolError, match="no query, ids or limit"):
            call(mcp, "preview_action", arguments)
    assert grants(client) == before and not sim.commands
    assert audited(client) == []


def test_the_retired_name_refuses_and_says_what_to_call(served):
    sim, client, mcp = served
    with pytest.raises(ToolError) as refused:
        call(mcp, "preview_create_folder", {"name": "Folders/Old"})
    assert str(refused.value) == (
        "Validation error: preview_create_folder was replaced by "
        'preview_action(action="create_folder", params={"folder": ...})'
    )
    # Nothing ran: no Bridge command, no token, no audit record, and no argument echoed.
    assert not sim.commands and grants(client) == {} and "Folders/Old" not in str(refused.value)
    assert audited(client) == []
    assert "preview_create_folder" not in {tool.name for tool in asyncio.run(mcp.list_tools())}


def test_old_calls_map_to_the_new_one():
    assert tool_call("preview_create_folder", {"name": "Folders/A"}) == (
        "preview_action",
        {"action": "create_folder", "params": {"folder": "Folders/A"}},
    )


@pytest.mark.parametrize("confirm", [True, False], ids=["form", "no-form"])
def test_a_folder_token_from_preview_action_never_redeems_elsewhere(bridge, confirm):  # noqa: F811
    sim, client = bridge(FolderBridge)
    mcp = create_mcp(replace(client.settings, confirm_mutations=confirm), client)
    preview = client.preview_action("create_folder", params={"folder": "Folders/Pinned"})
    token = preview["token"]
    for tool, arguments in (
        ("move", {"target": "Folders/Pinned"}),
        ("set_read_state", {"state": "read"}),
        ("label", {"op": "add"}),
        ("delete_draft", {}),
        ("unsubscribe", {}),
    ):
        host = Host()
        with pytest.raises(ToolError, match="does not match"):
            call(mcp, tool, {**arguments, "token": token}, host)
        assert host.prompts == []
    assert "Folders/Pinned" not in sim.custom_folders
    result = call(mcp, "create_folder", {"token": token}, Host())
    assert result["created"] is True and "Folders/Pinned" in sim.custom_folders
