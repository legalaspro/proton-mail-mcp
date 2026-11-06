"""The tool list each configuration exposes, pinned so that no change to it goes unnoticed.

tests/fixtures/tool_surface.json records, for each of the eight combinations of views, sending
and folder rules, every tool's annotations, argument names, required arguments and _meta.
docs/tools.md already pins the descriptions. After changing the surface on purpose, run
`.venv/bin/python tests/test_tool_surface.py` and commit the new snapshot.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from proton_mail_mcp.config import Settings
from proton_mail_mcp.server import create_mcp

SNAPSHOT = Path(__file__).with_name("fixtures") / "tool_surface.json"
BASE = {"PROTON_MCP_TOKEN": "t" * 32, "PROTON_BRIDGE_USER": "user@example.test"}
PROFILES = {
    "default": {},
    "send": {"PROTON_MCP_ENABLE_SEND": "1"},
    "no-views": {"PROTON_MCP_APPS_ENABLED": "0"},
    "send-no-views": {"PROTON_MCP_ENABLE_SEND": "1", "PROTON_MCP_APPS_ENABLED": "0"},
    "rules": {"PROTON_MCP_ENABLE_RULES": "1"},
    "send-rules": {"PROTON_MCP_ENABLE_SEND": "1", "PROTON_MCP_ENABLE_RULES": "1"},
    "rules-no-views": {"PROTON_MCP_ENABLE_RULES": "1", "PROTON_MCP_APPS_ENABLED": "0"},
    "send-rules-no-views": {
        "PROTON_MCP_ENABLE_SEND": "1",
        "PROTON_MCP_ENABLE_RULES": "1",
        "PROTON_MCP_APPS_ENABLED": "0",
    },
}
# Set in 1.31.0, when folder rules became opt-in and preview_create_folder folded into
# preview_action (CHANGELOG.md): the lists most users get stay at 25 tools or fewer.
MAX_TOOLS = 25
CAP = (
    "{profile} lists {count} tools, over the cap of {cap} set in 1.31.0 (CHANGELOG.md: the default "
    "tool list drops from 30 tools to 22). Put a new tool behind a setting, as "
    "PROTON_MCP_ENABLE_RULES does, or fold it into an existing tool."
)


def surface(env: dict[str, str]) -> dict:
    with tempfile.TemporaryDirectory() as folder:
        values = {**BASE, "PROTON_MCP_ACTION_LOG": f"{folder}/actions.jsonl", **env}
        tools = asyncio.run(
            create_mcp(Settings.from_env(values), mail_client=object()).list_tools()
        )
    return {
        tool.name: {
            "annotations": tool.annotations.model_dump(exclude_none=True),
            "arguments": sorted(tool.inputSchema.get("properties", {})),
            "required": sorted(tool.inputSchema.get("required", [])),
            "extra_arguments": tool.inputSchema.get("additionalProperties"),
            "meta": tool.meta,
        }
        for tool in sorted(tools, key=lambda tool: tool.name)
    }


def snapshot() -> dict:
    return {profile: surface(env) for profile, env in PROFILES.items()}


def test_every_profile_lists_exactly_the_pinned_tools():
    pinned = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    current = snapshot()
    assert sorted(pinned) == sorted(current)
    for profile, tools in current.items():
        assert sorted(tools) == sorted(pinned[profile]), profile
        for name, shape in tools.items():
            assert shape == pinned[profile][name], (
                f"{profile}: {name} changed; if on purpose, run "
                "`.venv/bin/python tests/test_tool_surface.py` and commit the snapshot"
            )


def test_the_tool_counts_per_profile():
    counts = {profile: len(tools) for profile, tools in snapshot().items()}
    assert counts == {
        "default": 22,
        "send": 25,
        "no-views": 20,
        "send-no-views": 22,
        "rules": 29,
        "send-rules": 32,
        "rules-no-views": 27,
        "send-rules-no-views": 29,
    }


def test_the_default_and_sending_lists_stay_within_the_cap():
    for profile in ("default", "send", "no-views", "send-no-views"):
        count = len(surface(PROFILES[profile]))
        assert count <= MAX_TOOLS, CAP.format(profile=profile, count=count, cap=MAX_TOOLS)


def test_every_tool_refuses_unknown_arguments_in_every_profile():
    for profile, tools in snapshot().items():
        for name, shape in tools.items():
            assert shape["extra_arguments"] is False, (profile, name)


if __name__ == "__main__":
    SNAPSHOT.write_text(json.dumps(snapshot(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {SNAPSHOT}", file=sys.stderr)
