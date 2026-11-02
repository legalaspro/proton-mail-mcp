"""The docs agree with the code, and each reader's entry point keeps its shape."""

import asyncio
import importlib.util
import re
from pathlib import Path

import pytest

from proton_mail_mcp.config import Settings
from proton_mail_mcp.server import create_mcp

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
PAGES = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "CHANGELOG.md",
    *sorted(DOCS.glob("*.md")),
]
LLMS_BASE = "https://raw.githubusercontent.com/legalaspro/proton-mail-mcp/main/"
CONFIGS = {
    "default": {},
    "send": {"PROTON_MCP_ENABLE_SEND": "1"},
    "no-views": {"PROTON_MCP_APPS_ENABLED": "0"},
    "send-no-views": {"PROTON_MCP_ENABLE_SEND": "1", "PROTON_MCP_APPS_ENABLED": "0"},
}
# Older Claude Code versions cut descriptions and instructions here, keeping what comes first.
CLIENT_CUT = 2048


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def generator():
    spec = importlib.util.spec_from_file_location(
        "generate_tool_docs", ROOT / "scripts" / "generate_tool_docs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def server(tmp_path, env):
    values = {
        "PROTON_MCP_TOKEN": "t" * 32,
        "PROTON_BRIDGE_USER": "user@example.test",
        "PROTON_MCP_ACTION_LOG": str(tmp_path / "actions.jsonl"),
        **env,
    }
    return create_mcp(Settings.from_env(values), mail_client=object())


def tools(mcp) -> dict:
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def outside_code(text: str) -> list[str]:
    """Lines outside fenced code, with inline code removed."""
    lines, fenced = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            lines.append(re.sub(r"`[^`\n]*`", "", line))
    return lines


def anchors(text: str) -> set[str]:
    """Heading anchors by GitHub's rule, including its numbering of repeated headings."""
    found: set[str] = set()
    counts: dict[str, int] = {}
    fenced = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        heading = None if fenced else re.match(r"^#{1,6} (.+?)\s*$", line)
        if heading:
            plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading.group(1))
            slug = re.sub(r"[^\w\- ]", "", plain.lower().replace("`", "")).replace(" ", "-")
            found.add(f"{slug}-{counts[slug]}" if slug in counts else slug)
            counts[slug] = counts.get(slug, 0) + 1
    return found


def test_the_cleanup_page_says_when_an_unsubscribe_send_needs_draft_id():
    page = " ".join(read(DOCS / "cleanup.md").split())
    assert "`send_draft(send_preview.token, send_preview.draft_id)`" in page
    assert "`send_draft(send_preview.token)`" not in page
    assert (
        "`draft_id` is required when `send_preview.approval` is `typed_ok`: a call without it "
        "refuses and uses up the token" in page
    )


def test_every_relative_link_and_anchor_resolves():
    known = {page.resolve(): anchors(read(page)) for page in PAGES}
    checked = 0
    for page in PAGES:
        for line in outside_code(read(page)):
            for target in re.findall(r"\[[^\]\n]*\]\(([^)\s]+)\)", line):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                checked += 1
                path, _, anchor = target.partition("#")
                destination = (page.parent / path).resolve() if path else page.resolve()
                assert destination.exists(), f"{page.name}: {target}"
                if anchor:
                    headings = known.get(destination, anchors(read(destination)))
                    assert anchor in headings, f"{page.name}: {target}"
    assert checked > 100


def test_the_tool_reference_is_generated_from_the_registered_tools():
    assert read(DOCS / "tools.md") == generator().render(), "run make docs"


@pytest.mark.parametrize("env", CONFIGS.values(), ids=CONFIGS)
def test_the_text_a_model_reads_fits_what_clients_keep(tmp_path, env):
    mcp = server(tmp_path, env)
    assert len(mcp.instructions) <= CLIENT_CUT
    for tool in tools(mcp).values():
        assert len(tool.description) <= CLIENT_CUT, tool.name


@pytest.mark.parametrize("env", CONFIGS.values(), ids=CONFIGS)
def test_every_argument_says_what_it_means(tmp_path, env):
    for tool in tools(server(tmp_path, env)).values():
        for name, field in tool.inputSchema.get("properties", {}).items():
            assert field.get("description"), f"{tool.name}.{name}"


@pytest.mark.parametrize("env", CONFIGS.values(), ids=CONFIGS)
def test_the_instructions_name_only_registered_tools(tmp_path, env):
    every = set(tools(server(tmp_path, CONFIGS["send"])))
    mcp = server(tmp_path, env)
    registered = set(tools(mcp))
    named = {word for word in re.findall(r"[a-z_]+", mcp.instructions) if word in every}
    assert named <= registered, named - registered
    assert {"preview_action", "move", "list_actions"} <= named


def test_descriptions_call_only_tools_that_exist(tmp_path):
    registered = tools(server(tmp_path, CONFIGS["send"]))
    for tool in registered.values():
        for called in re.findall(r"\b([a-z]+_[a-z_]+)\(", tool.description):
            assert called in registered, f"{tool.name} names {called}"


def test_the_reference_says_which_settings_register_each_tool(tmp_path):
    every = tools(server(tmp_path, CONFIGS["send"]))
    default = tools(server(tmp_path, CONFIGS["default"]))
    no_views = tools(server(tmp_path, CONFIGS["send-no-views"]))
    page = read(DOCS / "tools.md")
    for name in every:
        expected = (
            "Always"
            if name in default and name in no_views
            else "Unless `PROTON_MCP_APPS_ENABLED=0`"
            if name in default
            else "With `PROTON_MCP_ENABLE_SEND=1`"
            if name in no_views
            else "With `PROTON_MCP_ENABLE_SEND=1`, unless `PROTON_MCP_APPS_ENABLED=0`"
        )
        assert f"| {expected} |" in page[page.index(f"| [`{name}`](#{name}) |") :].split("\n")[0]


def test_every_tool_has_a_row_where_results_are_explained(tmp_path):
    results = read(DOCS / "results.md")
    rows = results[results.index("## What each tool returns") : results.index("## Refused")]
    for name in tools(server(tmp_path, CONFIGS["send"])):
        assert f"`{name}`" in rows, name


def test_every_setting_in_the_code_has_a_row_and_every_row_a_setting():
    code = "\n".join(read(path) for path in (ROOT / "src").rglob("*.py"))
    settings = set(re.findall(r"\b(?:PROTON_MCP|PROTON_BRIDGE)_[A-Z0-9_]+\b", code))
    rows = set(
        re.findall(
            r"^\| `((?:PROTON_MCP|PROTON_BRIDGE)_[A-Z0-9_]+)` \|",
            read(DOCS / "configuration.md"),
            flags=re.M,
        )
    )
    assert settings == rows


def test_llms_txt_follows_the_format_and_covers_every_page():
    lines = read(ROOT / "llms.txt").splitlines()
    assert lines[0] == "# proton-mail-mcp"
    assert next(line for line in lines[1:] if line).startswith("> ")
    headings = [line for line in lines if line.startswith("#")]
    assert all(line.startswith("## ") for line in headings[1:])
    assert headings[-1] == "## Optional"
    section = lines.index(headings[1])
    listed = set()
    for line in lines[section:]:
        if not line or line.startswith("## "):
            continue
        entry = re.fullmatch(r"- \[[^\]]+\]\(([^)\s]+)\)(?:: .+)?", line)
        assert entry, line
        assert entry.group(1).startswith(LLMS_BASE), line
        path = ROOT / entry.group(1).removeprefix(LLMS_BASE)
        assert path.is_file(), line
        listed.add(path)
    assert listed == set(PAGES)


def test_every_page_is_on_the_map():
    documentation_map = read(DOCS / "README.md")
    for page in DOCS.glob("*.md"):
        if page.name != "README.md":
            assert f"]({page.name})" in documentation_map, page.name


def test_the_pypi_warning_stands_or_falls_on_every_page():
    readme = "Not on PyPI yet" in read(ROOT / "README.md")
    for page in (ROOT / "llms.txt", DOCS / "install.md", DOCS / "windows.md"):
        assert readme == ("Not on PyPI yet" in read(page)), page.name


def test_the_readme_stays_short_and_points_each_reader_on():
    readme = read(ROOT / "README.md")
    assert len(readme.splitlines()) <= 200
    for target in ("docs/README.md", "docs/getting-started.md", "AGENTS.md", "llms.txt"):
        assert f"]({target})" in readme, target


def test_agents_md_stays_small_and_its_commands_and_paths_exist():
    guide = read(ROOT / "AGENTS.md")
    # Claude Code advises under 200 lines; Codex stops reading at 32 KiB by default.
    assert len(guide.splitlines()) < 200
    assert len(guide.encode("utf-8")) < 32 * 1024
    targets = set(re.findall(r"^([a-z-]+):", read(ROOT / "Makefile"), flags=re.M))
    for target in re.findall(r"\bmake ([a-z-]+)", guide):
        assert target in targets, target
    for path in re.findall(r"`((?:src|tests|docs)/[\w./-]+)`", guide):
        assert (ROOT / path).exists(), path
    for name in re.findall(r"`([a-z_]+\.py)`", guide):
        assert list((ROOT / "src").rglob(name)), name
    assert "Do not implement, test, review or require handling for mid-call arrivals" in guide


def test_claude_code_loads_agents_md_through_its_import():
    assert read(ROOT / "CLAUDE.md").strip() == "@AGENTS.md"
