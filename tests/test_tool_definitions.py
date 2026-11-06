"""What each tool tells a model, or a registry such as Glama, before anything is called.

readOnlyHint also decides which calls the audit records, so the table pins it exactly.
"""

import asyncio
import re

import pytest

from proton_mail_mcp.config import Settings
from proton_mail_mcp.server import RULES_TOOLS, create_mcp

# readOnlyHint, destructiveHint, idempotentHint, openWorldHint
READ = (True, False, True, False)
# A preview changes no mail, but each call issues a new token.
PREVIEW = (True, False, False, False)
WRITE = (False, False, False, False)
REDEEM = (False, True, False, False)
HINTS = {
    "search_mail": READ,
    "count_mail": READ,
    "read_thread": READ,
    "list_attachments": READ,
    "read_attachment": READ,
    "list_from_addresses": READ,
    "list_rules": READ,
    "reindex_status": READ,
    "list_actions": READ,
    "show_thread": READ,
    "compose_reply": READ,
    "review_send": READ,
    "preview_action": PREVIEW,
    "preview_rules": PREVIEW,
    "preview_add_rule": PREVIEW,
    "preview_sieve_filter": PREVIEW,
    "preview_send": PREVIEW,
    "create_draft": WRITE,
    "forward_message": WRITE,
    "save_attachment": WRITE,
    "reindex": WRITE,
    "undo_action": REDEEM,
    "move": REDEEM,
    "set_read_state": REDEEM,
    "label": REDEEM,
    "apply_rules": REDEEM,
    "delete_draft": REDEEM,
    "add_rule": REDEEM,
    "create_sieve_filter": REDEEM,
    # An existing folder is a success that leaves the token unused.
    "create_folder": (False, False, True, False),
    # These reach the world outside the mailbox: the senders' servers, or recipients.
    "unsubscribe": (False, True, False, True),
    "send_draft": (False, True, False, True),
}


def every_tool(tmp_path, enable_send=True, enable_rules=True) -> dict:
    settings = Settings(
        token="t" * 32,
        bridge_user="user@example.test",
        enable_send=enable_send,
        enable_rules=enable_rules,
        send_from_allowlist=("user@example.test",),
        action_log_path=tmp_path / "actions.jsonl",
    )
    return {tool.name: tool for tool in asyncio.run(create_mcp(settings, object()).list_tools())}


def words(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def test_every_tool_declares_all_four_hints_as_the_table_says(tmp_path):
    tools = every_tool(tmp_path)
    assert set(tools) == set(HINTS)
    for name, tool in tools.items():
        hints = tool.annotations
        declared = (
            hints.readOnlyHint,
            hints.destructiveHint,
            hints.idempotentHint,
            hints.openWorldHint,
        )
        assert declared == HINTS[name], name


def test_every_tool_has_a_title_that_says_more_than_its_name(tmp_path):
    for name, tool in every_tool(tmp_path).items():
        assert tool.title and tool.title == tool.annotations.title, name
        assert words(tool.title) != words(name), name


def test_each_description_opens_with_a_short_purpose_and_says_what_comes_back(tmp_path):
    for name, tool in every_tool(tmp_path).items():
        first = tool.description.split("\n\n", 1)[0]
        assert len(first) <= 300, name
        assert "return" in tool.description.lower(), name


def test_with_sending_off_no_description_names_a_send_tool(tmp_path):
    sending = every_tool(tmp_path)
    tools = every_tool(tmp_path, enable_send=False)
    for name, tool in tools.items():
        named = set(re.findall(r"[a-z_]+", tool.description))
        assert not named & {"preview_send", "send_draft", "review_send"}, name
        assert "With sending on" not in tool.description, name
        # Only the paragraphs about sending go: the rest is word for word the same.
        kept = [
            text
            for text in sending[name].description.split("\n\n")
            if not text.startswith("With sending on")
        ]
        assert tool.description == "\n\n".join(kept), name


def test_with_rules_off_no_text_names_a_rules_tool(tmp_path):
    rules = every_tool(tmp_path)
    tools = every_tool(tmp_path, enable_rules=False)
    assert set(rules) - set(tools) == RULES_TOOLS
    for name, tool in tools.items():
        arguments = [
            field.get("description", "") for field in tool.inputSchema["properties"].values()
        ]
        for text in (tool.description, *arguments):
            assert not set(re.findall(r"[a-z_]+", text)) & RULES_TOOLS, name
        # The flag only decides which tools are listed: the rest are the same, word for word.
        assert tool.description == rules[name].description, name
        assert tool.inputSchema == rules[name].inputSchema, name
        assert tool.annotations == rules[name].annotations, name


@pytest.mark.parametrize("enable_send", [True, False])
def test_the_instructions_say_rules_are_off_and_name_no_rules_tool(tmp_path, enable_send):
    def instructions(enable_rules):
        settings = Settings(
            token="t" * 32,
            bridge_user="user@example.test",
            enable_send=enable_send,
            enable_rules=enable_rules,
            send_from_allowlist=("user@example.test",),
            action_log_path=tmp_path / "actions.jsonl",
        )
        return create_mcp(settings, object()).instructions

    off, on = instructions(False), instructions(True)
    assert not set(re.findall(r"[a-z_]+", off)) & RULES_TOOLS
    assert "Folder rules and Sieve filters are off unless the operator sets " in off
    assert "PROTON_MCP_ENABLE_RULES=1" in off and "PROTON_MCP_ENABLE_RULES" not in on
    assert {"preview_rules", "apply_rules", "preview_add_rule", "add_rule"} <= set(
        re.findall(r"[a-z_]+", on)
    )
    assert "Tools cannot enable or start the optional rules timer." in on
    # Both name the one entry point for folder previews, and the retired name nowhere.
    for text in (off, on):
        assert "unsubscribe or create_folder" in text and "preview_create_folder" not in text
