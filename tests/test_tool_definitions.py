"""What each tool tells a model, or a registry such as Glama, before anything is called.

readOnlyHint also decides which calls the audit records, so the table pins it exactly.
"""

import asyncio
import re

from proton_mail_mcp.config import Settings
from proton_mail_mcp.server import create_mcp

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
    "preview_create_folder": PREVIEW,
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


def every_tool(tmp_path, enable_send=True) -> dict:
    settings = Settings(
        token="t" * 32,
        bridge_user="user@example.test",
        enable_send=enable_send,
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
