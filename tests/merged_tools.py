"""The MCP call that redeems each action through its merged tool."""

# Tokens, action-log records and audit records keep the action names on the left.
_CALLS = {
    "move_to_trash": ("move", {"target": "Trash"}),
    "archive": ("move", {"target": "Archive"}),
    "move_to_folder": ("move", {"target": "Folders/Reviewed"}),
    "mark_read": ("set_read_state", {"state": "read"}),
    "mark_unread": ("set_read_state", {"state": "unread"}),
    "add_label": ("label", {"op": "add"}),
    "remove_label": ("label", {"op": "remove"}),
    "reindex_preview": ("reindex", {"op": "preview"}),
    "reindex_execute": ("reindex", {"op": "execute"}),
    "reindex_cancel": ("reindex", {"op": "cancel"}),
}


def tool_call(action, arguments):
    """The tool and arguments for one action; any other tool name passes through."""
    tool, selector = _CALLS.get(action, (action, {}))
    return tool, {**selector, **arguments}
