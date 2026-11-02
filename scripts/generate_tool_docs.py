#!/usr/bin/env python3
"""Write docs/tools.md from the registered tools, so it says what the model is told.

Run `make docs` after changing a tool, its arguments or the server instructions. A test fails
when the committed page differs from this output.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from proton_mail_mcp.config import Settings
from proton_mail_mcp.server import create_mcp

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "tools.md"

# Every registered tool appears in exactly one group: a test fails on a new tool left out.
GROUPS = (
    (
        "Read and look up",
        "Nothing changes and no approval is needed.",
        (
            "search_mail",
            "count_mail",
            "read_thread",
            "list_attachments",
            "read_attachment",
            "list_from_addresses",
            "list_rules",
            "reindex_status",
            "list_actions",
        ),
    ),
    (
        "Previews",
        "Read-only: each returns a single-use token to redeem after the user approves.",
        (
            "preview_action",
            "preview_rules",
            "preview_create_folder",
            "preview_add_rule",
            "preview_sieve_filter",
            "preview_send",
        ),
    ),
    (
        "Views",
        "Read-only, for showing mail to the user; every client also gets a text result.",
        ("show_thread", "compose_reply", "review_send"),
    ),
    (
        "Writes without a token",
        "Audited. None of them sends mail or changes existing mail, except an undo of a logged "
        "change.",
        ("create_draft", "forward_message", "save_attachment", "undo_action"),
    ),
    (
        "Redeem a token",
        "Audited. Only after the user approved the preview; see [how approvals work](safety.md).",
        (
            "move",
            "set_read_state",
            "label",
            "apply_rules",
            "delete_draft",
            "unsubscribe",
            "create_folder",
            "add_rule",
            "create_sieve_filter",
            "send_draft",
        ),
    ),
    (
        "Local search index",
        "Changes only the local index, never mail.",
        ("reindex",),
    ),
)
BASE = {"PROTON_MCP_TOKEN": "t" * 32, "PROTON_BRIDGE_USER": "user@example.test"}
SEND = {"PROTON_MCP_ENABLE_SEND": "1"}
NO_VIEWS = {"PROTON_MCP_APPS_ENABLED": "0"}
HEADER = """\
# Tool reference

Every MCP tool this server exposes, in the words the model reads. `make docs` generates this page
from the registered tools, and a test fails when it is out of date, so edit
[`server.py`](../src/proton_mail_mcp/server.py) and
[`tool_docs.py`](../src/proton_mail_mcp/tool_docs.py), not this page. How each feature behaves in
full is in the [documentation map](README.md).

The server exposes {base} tools by default, or {send} with sending enabled. With
`PROTON_MCP_APPS_ENABLED=0` it exposes {no_views}, or {send_no_views} with sending enabled, without
the [MCP Apps view](mcp-apps.md) tools.

New mail arriving during a tool call is out of scope for that call and is picked
up on the next request. Request mail again when you need newer mail.

**Beta: folder rules.** The rules engine is not fully reliable yet; preview first
and check the results. This applies to `preview_add_rule`, `add_rule`, `list_rules`,
`preview_rules` and `apply_rules`.

## Server instructions

Sent to every client when it connects. A client decides how to use them; many add them to the
model's system prompt. The sending paragraph appears only with sending enabled, in place of one
sentence that says sending is off, and the views paragraph only with views on. Likewise, a tool
description's paragraphs that start "With sending on" are sent only with sending enabled.

{instructions}

## Tools at a glance

"""


def _mcp(env: dict[str, str]):
    with tempfile.TemporaryDirectory() as folder:
        values = {**BASE, "PROTON_MCP_ACTION_LOG": f"{folder}/actions.jsonl", **env}
        return create_mcp(Settings.from_env(values), mail_client=object())


def _tools(env: dict[str, str]) -> dict:
    return {tool.name: tool for tool in asyncio.run(_mcp(env).list_tools())}


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|").replace("<", "&lt;")


def _type(schema: dict) -> str:
    if "anyOf" in schema:
        kinds = [_type(option) for option in schema["anyOf"] if option.get("type") != "null"]
        return " or ".join(kinds)
    if "enum" in schema:
        return " or ".join(f"`{value}`" for value in schema["enum"])
    kind = schema.get("type", "any")
    if kind == "array":
        return f"list of {_type(schema.get('items', {}))}"
    bounds = []
    if "minimum" in schema:
        bounds.append(f"at least {schema['minimum']}")
    if "maximum" in schema:
        bounds.append(f"at most {schema['maximum']}")
    return f"{kind}, {' and '.join(bounds)}" if bounds else kind


def _arguments(tool) -> list[str]:
    schema = tool.inputSchema
    properties = schema.get("properties", {})
    if not properties:
        return ["Takes no arguments."]
    required = schema.get("required", [])
    rows = ["| Argument | Type | Default | Meaning |", "| --- | --- | --- | --- |"]
    for name, field in properties.items():
        default = "required" if name in required else f"`{json.dumps(field.get('default'))}`"
        rows.append(
            f"| `{name}` | {_cell(_type(field))} | {_cell(default)} "
            f"| {_cell(field.get('description', ''))} |"
        )
    return rows


def _hints(tool) -> str:
    fields = tool.annotations.model_dump(exclude_none=True) if tool.annotations else {}
    return ", ".join(f"`{key}={json.dumps(value)}`" for key, value in sorted(fields.items()))


def _available(name: str, base: dict, send_no_views: dict) -> str:
    if name in base:
        return "Always" if name in send_no_views else "Unless `PROTON_MCP_APPS_ENABLED=0`"
    if name in send_no_views:
        return "With `PROTON_MCP_ENABLE_SEND=1`"
    return "With `PROTON_MCP_ENABLE_SEND=1`, unless `PROTON_MCP_APPS_ENABLED=0`"


def _summary(tool) -> str:
    # The first paragraph, so a first sentence that wraps is shown whole.
    return " ".join(tool.description.strip().split("\n\n", 1)[0].split())


def render() -> str:
    base = _tools({})
    send = _tools(SEND)
    no_views = _tools(NO_VIEWS)
    send_no_views = _tools({**SEND, **NO_VIEWS})
    named = [name for _, _, names in GROUPS for name in names]
    if sorted(named) != sorted(send):
        raise SystemExit(f"GROUPS does not match the tools: {sorted(set(named) ^ set(send))}")
    lines = [
        HEADER.format(
            base=len(base),
            send=len(send),
            no_views=len(no_views),
            send_no_views=len(send_no_views),
            instructions=_mcp(SEND).instructions,
        ).rstrip(),
        "",
        "| Tool | What it does | Available |",
        "| --- | --- | --- |",
    ]
    for _, _, names in GROUPS:
        for name in names:
            lines.append(
                f"| [`{name}`](#{name}) | {_cell(_summary(send[name]))} "
                f"| {_available(name, base, send_no_views)} |"
            )
    for title, intro, names in GROUPS:
        lines += ["", f"## {title}", "", intro]
        for name in names:
            tool = send[name]
            lines += ["", f"### `{name}`", "", tool.description.strip(), ""]
            lines += _arguments(tool)
            lines += ["", f"Available: {_available(name, base, send_no_views)}."]
            hints = _hints(tool)
            if hints:
                lines[-1] += f" Hints: {hints}."
    return "\n".join(lines) + "\n"


def main() -> int:
    page = render()
    if "--check" in sys.argv[1:]:
        if PAGE.read_text(encoding="utf-8") != page:
            print(f"{PAGE} is out of date; run make docs", file=sys.stderr)
            return 1
        return 0
    PAGE.write_text(page, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
