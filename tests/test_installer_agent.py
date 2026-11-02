"""Agent-driven install: client configs, --json, hand-offs to a person, and the friction a new
user met on macOS (the plan before questions, a running Bridge app, uv's cache, stale files)."""

import base64
import codecs
import io
import json
import os
import re
import stat
import subprocess
import sys
import tomllib
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

from proton_mail_mcp import credential_store
from proton_mail_mcp.credential_store import BRIDGE_SERVICE, TOKEN_SERVICE
from proton_mail_mcp.installer import cli, clients
from proton_mail_mcp.installer import plan as planner
from proton_mail_mcp.installer.detect import simulated
from proton_mail_mcp.installer.execute import Executor, InstallError
from proton_mail_mcp.installer.plan import Options, PlanError, install_plan, paths_for
from proton_mail_mcp.installer.probe import simulated_probe
from proton_mail_mcp.installer.steps import (
    MARKER,
    Command,
    ConfigureClient,
    NeedsUser,
    Note,
    SelfCheck,
)

ROOT = Path(__file__).resolve().parents[1]
USER = "you@example.test"
# Stands for the bearer token: each test that reaches it checks every output for it.
SENTINEL = "SENTINEL-token-never-printed-0123456789abcdefABCDEF"
CACHED = "/Users/you/.cache/uv/archive-v0/AbC123/bin/python"
MANAGED = "/Users/you/.local/share/uv/python/cpython-3.12-macos-aarch64-none/bin/python3.12"
posix = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes and paths")


@pytest.fixture(autouse=True)
def no_pseudo_terminal(monkeypatch):
    """Bridge's CLI goes through the scripted runner, not a real pseudo-terminal."""
    monkeypatch.setattr(cli.bridge_cli, "available", lambda: False)


@pytest.fixture(autouse=True)
def normal_user(monkeypatch):
    # Hosted CI containers run pytest as root, which the installer refuses.
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(os, "geteuid", lambda: 1000)


def plan(family="debian", probe=None, **overrides):
    platform = simulated(family)
    probe = probe or simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    values = {
        "bridge_user": USER,
        "source": "/src/checkout",
        "python": "/usr/bin/python3",
        "setup": True,
        **overrides,
    }
    return install_plan(platform, Options(**values), probe, paths)


def needs(steps, name=None):
    return [step for step in steps if isinstance(step, NeedsUser) and name in (None, step.id)]


def said(steps):
    return " ".join(step.text for step in steps if isinstance(step, (Note, NeedsUser)))


@pytest.fixture
def stored(monkeypatch):
    """The credential store holds SENTINEL as the token; each lookup is recorded."""
    calls = []

    def lookup(store, service, account, locations=None):
        calls.append((store, service, account, locations))
        return SENTINEL

    monkeypatch.setattr(credential_store, "lookup", lookup)
    return calls


# Where each client keeps its servers -----------------------------------------------


@pytest.mark.parametrize(
    ("client", "family", "env", "expected"),
    [
        (
            "claude-desktop",
            "macos",
            {},
            "/Users/you/Library/Application Support/Claude/claude_desktop_config.json",
        ),
        (
            "claude-desktop",
            "windows",
            {"APPDATA": "D:\\Roaming"},
            "D:\\Roaming\\Claude\\claude_desktop_config.json",
        ),
        (
            "claude-desktop",
            "windows",
            {},
            "C:\\Users\\you\\AppData\\Roaming\\Claude\\claude_desktop_config.json",
        ),
        ("claude-desktop", "debian", {}, "/home/you/.config/Claude/claude_desktop_config.json"),
        (
            "claude-desktop",
            "arch",
            {"XDG_CONFIG_HOME": "/xdg"},
            "/xdg/Claude/claude_desktop_config.json",
        ),
        ("claude-code", "macos", {}, "/Users/you/.claude.json"),
        ("claude-code", "debian", {"CLAUDE_CONFIG_DIR": "/cfg"}, "/cfg/.claude.json"),
        ("claude-code", "windows", {}, "C:\\Users\\you\\.claude.json"),
        ("cursor", "macos", {}, "/Users/you/.cursor/mcp.json"),
        ("cursor", "fedora", {}, "/home/you/.cursor/mcp.json"),
        ("cursor", "windows", {}, "C:\\Users\\you\\.cursor\\mcp.json"),
        ("chatgpt", "macos", {}, "/Users/you/.codex/config.toml"),
        ("codex", "debian", {"CODEX_HOME": "/codex"}, "/codex/config.toml"),
        ("codex", "windows", {}, "C:\\Users\\you\\.codex\\config.toml"),
        ("gemini", "macos", {}, "/Users/you/.gemini/settings.json"),
        ("gemini", "windows", {}, "C:\\Users\\you\\.gemini\\settings.json"),
        ("grok-bot", "debian", {}, ""),
    ],
)
def test_each_client_config_is_where_that_client_reads_it(client, family, env, expected):
    home = {"macos": "/Users/you", "windows": "C:\\Users\\you"}.get(family, "/home/you")
    assert clients.config_path(client, family, env, home) == expected


LAUNCHER = "/Users/you/Library/Application Support/proton-mail-mcp/venv/bin/proton-mail-mcp"
ENV_FILE = "/Users/you/Library/Application Support/proton-mail-mcp/proton-mail-mcp.env"
COMMAND = clients.connect_command(LAUNCHER, ENV_FILE)


def docs_entry(client):
    """The proton-mail entry of the first config block on the client's own page."""
    page = (ROOT / "docs" / "clients" / f"{client}.md").read_text(encoding="utf-8")
    if clients.CLIENTS[client].form == "toml":
        block = page[page.index("```toml\n") + 8 :]
        return tomllib.loads(block[: block.index("```")])["mcp_servers"]["proton-mail"]
    block = page[page.index("```json\n") + 8 :]
    return json.loads(block[: block.index("```")])["mcpServers"]["proton-mail"]


@pytest.mark.parametrize("client", list(clients.CLIENTS))
def test_the_written_entry_is_the_one_the_client_page_documents(client):
    documented = docs_entry(client)
    if clients.CLIENTS[client].http:
        assert documented == clients.entry(client, 8765, clients.TOKEN_PLACEHOLDER)
    else:
        assert documented == clients.entry(client, 8765, "", COMMAND)


def test_every_client_but_chatgpt_starts_connect_and_holds_no_token():
    for name in clients.CLIENTS:
        entry = clients.entry(name, 8799, SENTINEL, COMMAND)
        if name == "chatgpt":
            assert entry == {
                "url": "http://127.0.0.1:8799/mcp",
                "http_headers": {"Authorization": f"Bearer {SENTINEL}"},
            }
            continue
        assert SENTINEL not in json.dumps(entry)
        assert entry["command"] == LAUNCHER
        assert entry["args"] == ["connect", "--env-file", ENV_FILE]
    assert clients.entry("claude-code", 1, "", COMMAND)["type"] == "stdio"


# Merging into the file ----------------------------------------------------------------


def test_merging_keeps_every_other_key_and_server_in_place_and_is_idempotent():
    text = json.dumps(
        {"theme": "dark", "mcpServers": {"other": {"command": "x"}}, "last": [1, 2]}, indent=2
    )
    merged = clients.merged(text, {"url": "u"})
    data = json.loads(merged)
    assert list(data) == ["theme", "mcpServers", "last"]
    assert data["mcpServers"] == {"other": {"command": "x"}, "proton-mail": {"url": "u"}}
    assert data["theme"] == "dark" and data["last"] == [1, 2]
    assert clients.merged(merged, {"url": "u"}) is None
    replaced = json.loads(clients.merged(merged, {"url": "v"}))
    assert list(replaced["mcpServers"]) == ["other", "proton-mail"]
    assert replaced["mcpServers"]["proton-mail"] == {"url": "v"}


def test_an_entry_with_keys_of_its_own_besides_counts_as_the_same():
    wanted = {"command": "c", "args": ["connect"]}
    assert clients.same({**wanted, "env": {}}, wanted)
    assert clients.same({**wanted, "tool_timeout_sec": 150}, wanted)
    assert not clients.same({"command": "c", "args": []}, wanted)
    assert not clients.same({"command": "npx", "args": ["connect"], "env": {"A": "1"}}, wanted)
    assert not clients.same(None, wanted)
    kept = '[mcp_servers.proton-mail]\ncommand = "c"\nargs = ["connect"]\ntool_timeout_sec = 150\n'
    assert clients.merged(kept, wanted, "toml") is None


@pytest.mark.parametrize("text", [None, "", " \n", "{}"])
def test_a_missing_or_empty_file_gets_only_the_entry(text):
    assert json.loads(clients.merged(text, {"url": "u"})) == {
        "mcpServers": {"proton-mail": {"url": "u"}}
    }


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('{"mcpServers": {"a": 1}', "not valid JSON"),
        ("[1, 2]", "JSON object"),
        ('{"mcpServers": ["a"]}', "mcpServers"),
    ],
)
def test_a_file_that_cannot_be_merged_is_refused_without_quoting_it(text, reason):
    with pytest.raises(clients.ConfigError) as caught:
        clients.merged(text, {})
    assert reason in str(caught.value) and text not in str(caught.value)


CODEX = """\
# My Codex settings
model = "o3"

[mcp_servers.github]
url = "https://example.test/mcp"

[mcp_servers.proton-mail]
command = "npx"
args = ["-y", "mcp-remote"]

[mcp_servers.proton-mail.env]
AUTH_HEADER = "Bearer old"

[profiles.fast]
model = "o4-mini"
"""


def test_codexs_toml_gets_one_table_replaced_and_everything_else_kept():
    server = clients.entry("chatgpt", 8765, 'to"k\\en')
    text = clients.merged(CODEX, server, "toml")
    data = tomllib.loads(text)
    assert data["mcp_servers"]["proton-mail"] == server
    assert data["mcp_servers"]["github"] == {"url": "https://example.test/mcp"}
    assert data["model"] == "o3" and data["profiles"] == {"fast": {"model": "o4-mini"}}
    assert "# My Codex settings" in text and "Bearer old" not in text
    assert clients.merged(text, server, "toml") is None
    assert tomllib.loads(clients.merged(None, server, "toml")) == {
        "mcp_servers": {"proton-mail": server}
    }


def test_a_toml_merge_keeps_the_comments_before_the_next_table_and_the_place_of_the_entry():
    text = (
        '# top\n[mcp_servers.proton-mail]\n# about the old entry\nurl = "old"\n\n'
        '# notes about the other server: keep me\n[mcp_servers.other]\ncommand = "x"\n'
        '[mcp_servers.proton-mail.http_headers]\nAuthorization = "Bearer old"\n'
        "# trailing note: keep me too\n"
    )
    server = clients.entry("codex", 8765, "", COMMAND)
    new = clients.merged(text, server, "toml")
    assert tomllib.loads(new)["mcp_servers"] == {"proton-mail": server, "other": {"command": "x"}}
    assert "# notes about the other server: keep me\n[mcp_servers.other]" in new
    assert new.endswith("# trailing note: keep me too\n") and new.startswith("# top\n")
    assert new.index("[mcp_servers.proton-mail]") < new.index("[mcp_servers.other]")
    assert "about the old entry" not in new and "Bearer old" not in new


def test_a_merge_keeps_the_files_line_endings():
    toml = CODEX.replace("\n", "\r\n")
    new = clients.merged(toml, clients.entry("codex", 8765, "", COMMAND), "toml")
    assert "\n" not in new.replace("\r\n", "") and "# My Codex settings\r\n" in new
    new = clients.merged('{\r\n  "k": 1\r\n}\r\n', clients.entry("cursor", 8765, "", COMMAND))
    assert "\n" not in new.replace("\r\n", "") and json.loads(new)["k"] == 1


@pytest.mark.parametrize("newline", ["\r\n", "\n"])
@pytest.mark.parametrize(
    "text",
    [
        '# My Codex settings\n[model]\nname = "o3"\n',
        "# only a comment\n",
        '# My Codex settings\nmodel = "o3"',
        "\n",
        CODEX,
        'model = "o3"\n[mcp_servers.proton-mail]\nurl = "old"',
    ],
    ids=["no-entry", "comment-only", "no-final-newline", "blank", "entry", "entry-last"],
)
def test_a_toml_merge_ends_each_line_once_as_the_file_does(text, newline):
    text = text.replace("\n", newline)
    server = clients.entry("chatgpt", 8765, "tok")
    new = clients.merged(text, server, "toml")
    # No "\r\r\n" from an appended table, and no "\n" alone in a CRLF file or "\r" in an LF one.
    rest = new.replace(newline, "")
    assert new.endswith(newline) and "\r" not in rest and "\n" not in rest
    data = tomllib.loads(text) if text.strip() else {}
    servers = {**data.get("mcp_servers", {}), "proton-mail": server}
    assert tomllib.loads(new) == {**data, "mcp_servers": servers}
    assert clients.merged(new, server, "toml") is None
    if "proton-mail" not in text:
        # Appended after the file, which is kept as it was, and a blank line.
        kept = text.rstrip()
        assert new.startswith(kept + newline * 2 if kept else "[mcp_servers.proton-mail]")


def test_a_json_number_that_cannot_be_written_back_is_refused():
    with pytest.raises(clients.ConfigError, match="too large"):
        clients.merged('{"limit": 1e400}', clients.entry("cursor", 8765, "", COMMAND))


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("model = ", "not valid TOML"),
        ("mcp_servers = 3\n", "not a table"),
        ('[mcp_servers]\nproton-mail = { url = "u" }\n', "cannot edit safely"),
        ('[mcp_servers]\nproton-mail.url = "u"\n', "cannot edit safely"),
    ],
)
def test_a_toml_file_that_cannot_be_edited_safely_is_refused(text, reason):
    with pytest.raises(clients.ConfigError, match=reason):
        clients.merged(text, clients.entry("codex", 8765, "", COMMAND), "toml")


@pytest.mark.parametrize(
    "text",
    [
        'mcp_servers = { other = { command = "x" } }\n',
        '[notes]\ntext = """\n[mcp_servers.proton-mail]\n"""\n',
    ],
)
def test_a_toml_refusal_without_the_entry_says_there_is_none(text):
    server = clients.entry("codex", 8765, "", COMMAND)
    with pytest.raises(clients.ConfigError) as caught:
        clients.merged(text.replace("\n", "\r\n"), server, "toml")
    reason = str(caught.value)
    assert reason.startswith("has no proton-mail entry") and "cannot add one safely" in reason


def test_the_native_commands_add_at_user_scope_in_the_order_each_cli_parses():
    remove, add, listing = clients.native_commands("claude-code", "/bin/claude", COMMAND)
    assert remove == ("/bin/claude", "mcp", "remove", "--scope", "user", "proton-mail")
    assert add == ("/bin/claude", "mcp", "add", "--scope", "user", "proton-mail", "--", *COMMAND)
    assert listing == ("/bin/claude", "mcp", "list")
    # Codex's and Gemini CLI's add replace an entry they have, so nothing is removed first.
    remove, add, listing = clients.native_commands("codex", "/bin/codex", COMMAND)
    assert remove == () and add == ("/bin/codex", "mcp", "add", "proton-mail", "--", *COMMAND)
    # Gemini CLI 0.66.0 counts only what comes before `--` as its <commandOrUrl>, so the
    # program goes there and connect's arguments after it.
    remove, add, listing = clients.native_commands("gemini", "/bin/gemini", COMMAND)
    assert remove == () and listing == ("/bin/gemini", "mcp", "list")
    assert add == (
        "/bin/gemini",
        *("mcp", "add", "--scope", "user", "proton-mail", LAUNCHER),
        *("--", "connect", "--env-file", ENV_FILE),
    )


def test_cursors_link_carries_the_entry_and_no_token():
    server = clients.entry("cursor", 8765, "", COMMAND)
    link = clients.cursor_link(server)
    assert link.startswith(
        "cursor://anysphere.cursor-deeplink/mcp/install?name=proton-mail&config="
    )
    encoded = urllib.parse.unquote(link.split("config=")[1])
    assert json.loads(base64.b64decode(encoded)) == server
    vscode = clients.vscode_link(server)
    assert json.loads(urllib.parse.unquote(vscode.split("?", 1)[1]))["args"][0] == "connect"


# Writing it --------------------------------------------------------------------------


def configure(path, client="cursor", dry_run=False, native="", runner=None):
    out = io.StringIO()
    executor = Executor(dry_run=dry_run, out=out, runner=runner)
    http = clients.CLIENTS[client].http
    step = ConfigureClient(
        client,
        str(path),
        8765,
        f"Add to {client}",
        command=() if http else COMMAND,
        native=native,
        store="keychain" if http else "",
        service=TOKEN_SERVICE if http else "",
        account=USER if http else "",
    )
    executor.run([step])
    return out.getvalue(), executor


@posix
def test_a_new_stdio_config_holds_no_token_and_reads_none(tmp_path, stored):
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    output, executor = configure(path, "claude-desktop")
    entry = json.loads(path.read_text())["mcpServers"]["proton-mail"]
    assert entry == {"command": LAUNCHER, "args": ["connect", "--env-file", ENV_FILE]}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "the file holds no token" in output and stored == []
    assert executor.records == [
        {"n": 1, "kind": "ConfigureClient", "summary": "Add to claude-desktop", "status": "done"}
    ]


@posix
def test_chatgpts_entry_holds_the_token_owner_only_and_nothing_prints_it(tmp_path, stored):
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text(CODEX)
    path.chmod(0o644)
    output, _executor = configure(path, "chatgpt")
    entry = tomllib.loads(path.read_text())["mcp_servers"]["proton-mail"]
    assert entry["http_headers"] == {"Authorization": f"Bearer {SENTINEL}"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    [backup] = path.parent.glob("config.toml.*.bak")
    assert backup.read_text() == CODEX and stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert SENTINEL not in output and "never printed" in output
    assert "replaced the existing proton-mail entry" in output
    assert stored == [("keychain", TOKEN_SERVICE, USER, {})]


@posix
def test_an_existing_config_is_backed_up_by_date_then_merged_and_a_rerun_changes_nothing(
    tmp_path, stored
):
    path = tmp_path / "mcp.json"
    before = json.dumps({"mcpServers": {"github": {"url": "https://example.test/mcp"}}, "k": 1})
    path.write_text(before)
    path.chmod(0o644)
    output, _executor = configure(path)
    [backup] = tmp_path.glob("mcp.json.*.bak")
    assert re.fullmatch(r"mcp\.json\.\d{8}-\d{6}\.bak", backup.name)
    assert backup.read_text() == before and stat.S_IMODE(backup.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data["k"] == 1 and data["mcpServers"]["github"] == {"url": "https://example.test/mcp"}
    assert data["mcpServers"]["proton-mail"] == clients.entry("cursor", 8765, "", COMMAND)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert f"kept the previous file as {backup}" in output
    assert "replaced the existing" not in output
    written = path.read_bytes()
    output, executor = configure(path)
    assert "unchanged" in output and path.read_bytes() == written
    assert list(tmp_path.glob("mcp.json.*.bak")) == [backup]
    assert executor.records[0]["status"] == "unchanged"


@posix
def test_two_changes_in_one_second_keep_two_backups(tmp_path, stored):
    path = tmp_path / "mcp.json"
    path.write_text('{"mcpServers": {"proton-mail": {"url": "a"}}}')
    configure(path)
    path.write_text('{"mcpServers": {"proton-mail": {"url": "b"}}}')
    configure(path)
    assert len(list(tmp_path.glob("mcp.json.*.bak"))) == 2


def test_a_dry_run_shows_what_changes_without_any_value_in_the_file(tmp_path, stored):
    path = tmp_path / "config.toml"
    path.write_text(CODEX)
    output, executor = configure(path, "chatgpt", dry_run=True)
    assert stored == [] and path.read_text() == CODEX
    assert "- proton-mail: the entry there now (its values are not shown)" in output
    assert clients.TOKEN_PLACEHOLDER in output and "(kept as they are: github)" in output
    assert "Bearer old" not in output and "https://example.test/mcp" not in output
    # The entry there starts a command: replacing it is said as a warning.
    assert executor.records[0]["status"] == "warning" and len(executor.warnings) == 1
    output, _executor = configure(tmp_path / "new.json", dry_run=True)
    assert "(creates the file)" in output and not (tmp_path / "new.json").exists()


@pytest.mark.parametrize("text", ['{"mcpServers": {', "[]", '{"mcpServers": 3}'])
def test_a_config_that_cannot_be_merged_stays_exactly_as_it_was(tmp_path, stored, text):
    path = tmp_path / "mcp.json"
    path.write_text(text)
    with pytest.raises(InstallError) as caught:
        configure(path)
    assert path.read_text() == text and list(tmp_path.glob("*.bak")) == []
    assert "it was not changed" in str(caught.value)


@posix
def test_a_symbolic_link_is_never_written_through(tmp_path, stored):
    real = tmp_path / "dotfiles.json"
    real.write_text("{}")
    link = tmp_path / "mcp.json"
    link.symlink_to(real)
    with pytest.raises(InstallError, match="symbolic link"):
        configure(link)
    assert real.read_text() == "{}" and stored == []


def test_no_stored_token_writes_nothing_and_says_to_run_setup(tmp_path, monkeypatch):
    def lookup(*_args, **_kwargs):
        raise credential_store.CredentialStoreError("Credential is unavailable")

    monkeypatch.setattr(credential_store, "lookup", lookup)
    with pytest.raises(InstallError, match="run setup first"):
        configure(tmp_path / "config.toml", "chatgpt")
    assert not (tmp_path / "config.toml").exists()


class Scripted:
    """A client's own command line: records each call and answers as told."""

    def __init__(self, listed="proton-mail: ... - ✓ Connected", add_status=0):
        self.calls = []
        self.listed = listed
        self.add_status = add_status

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        status = self.add_status if argv[2] == "add" else 0
        out = self.listed if argv[2] == "list" else "done"
        return subprocess.CompletedProcess(argv, status, out, "")


@posix
def test_a_native_client_removes_then_adds_then_lists_with_its_own_command(tmp_path, stored):
    path = tmp_path / ".claude.json"
    before = json.dumps(
        {
            "mcpServers": {"proton-mail": {"type": "http", "url": "u"}},
            "projects": {"/work/app": {"mcpServers": {"proton-mail": {"command": "x"}}}},
        }
    )
    path.write_text(before)
    runner = Scripted()
    output, executor = configure(path, "claude-code", native="/bin/claude", runner=runner)
    remove, add, listing = clients.native_commands("claude-code", "/bin/claude", COMMAND)
    assert runner.calls == [list(remove), list(add), list(listing)]
    assert "ok: proton-mail: ... - ✓ Connected" in output
    [backup] = tmp_path.glob(".claude.json.*.bak")
    assert backup.read_text() == before and path.read_text() == before
    assert "the project /work/app has its own proton-mail entry" in output
    assert executor.warnings and stored == []


@posix
def test_a_native_client_that_has_the_entry_already_is_left_alone(tmp_path, stored):
    path = tmp_path / ".claude.json"
    entry = clients.entry("claude-code", 8765, "", COMMAND)
    path.write_text(json.dumps({"mcpServers": {"proton-mail": entry}}))
    runner = Scripted()
    output, executor = configure(path, "claude-code", native="/bin/claude", runner=runner)
    assert runner.calls == [] and executor.records[0]["status"] == "unchanged"


def test_a_failing_native_add_stops_and_a_missing_listing_warns(tmp_path, stored):
    with pytest.raises(InstallError, match="docs/clients/gemini.md"):
        configure(
            tmp_path / "settings.json",
            "gemini",
            native="/bin/gemini",
            runner=Scripted(add_status=1),
        )
    output, executor = configure(
        tmp_path / "settings.json", "gemini", native="/bin/gemini", runner=Scripted(listed="")
    )
    assert "does not list proton-mail" in output and executor.warnings


class Removing(Scripted):
    """Claude Code's own remove takes the entry out of its file, as the real one does, and the
    client changes something else there meanwhile."""

    def __init__(self, path, **kwargs):
        super().__init__(**kwargs)
        self.path = path

    def __call__(self, argv, **kwargs):
        if argv[2] == "remove":
            data = json.loads(self.path.read_text())
            del data["mcpServers"]["proton-mail"]
            data["numStartups"] = 7
            self.path.write_text(json.dumps(data))
        return super().__call__(argv, **kwargs)


@posix
def test_a_failed_add_puts_back_the_entry_claude_codes_remove_took_out(tmp_path, stored):
    path = tmp_path / ".claude.json"
    old = {"type": "stdio", "command": "/old/proton-mail-mcp", "args": ["connect"]}
    path.write_text(json.dumps({"mcpServers": {"proton-mail": old, "github": {"url": "u"}}}))
    runner = Removing(path, add_status=1)
    with pytest.raises(InstallError, match="exited with status 1"):
        configure(path, "claude-code", native="/bin/claude", runner=runner)
    assert [call[2] for call in runner.calls] == ["remove", "add"]
    data = json.loads(path.read_text())
    assert data["mcpServers"] == {"proton-mail": old, "github": {"url": "u"}}
    assert data["numStartups"] == 7


@posix
def test_an_interrupted_add_puts_the_entry_back_too(tmp_path, stored):
    path = tmp_path / ".claude.json"
    old = {"type": "stdio", "command": "/old/proton-mail-mcp", "args": ["connect"]}
    path.write_text(json.dumps({"mcpServers": {"proton-mail": old}}))

    class Interrupted(Removing):
        def __call__(self, argv, **kwargs):
            if argv[2] == "add":
                raise KeyboardInterrupt
            return super().__call__(argv, **kwargs)

    with pytest.raises(KeyboardInterrupt):
        configure(path, "claude-code", native="/bin/claude", runner=Interrupted(path))
    assert json.loads(path.read_text())["mcpServers"] == {"proton-mail": old}


def test_claude_code_removes_nothing_when_there_is_no_entry(tmp_path, stored):
    path = tmp_path / ".claude.json"
    path.write_text('{"mcpServers": {}}')
    runner = Scripted()
    configure(path, "claude-code", native="/bin/claude", runner=runner)
    assert [call[2] for call in runner.calls] == ["add", "list"]


def test_a_failing_gemini_add_never_touches_the_entry_there(tmp_path, stored):
    path = tmp_path / "settings.json"
    before = json.dumps({"mcpServers": {"proton-mail": {"command": "old"}}})
    path.write_text(before)
    runner = Scripted(add_status=1)
    with pytest.raises(InstallError, match="docs/clients/gemini.md"):
        configure(path, "gemini", native="/bin/gemini", runner=runner)
    assert [call[2] for call in runner.calls] == ["add"] and path.read_text() == before


def test_a_failed_adds_output_keeps_no_line_that_may_quote_the_file(tmp_path, stored):
    class Quoting(Scripted):
        def __call__(self, argv, **kwargs):
            message = f'Error: invalid config\n  http_headers = "Bearer {SENTINEL}"\nsee docs'
            return subprocess.CompletedProcess(argv, 1, message, "")

    out = io.StringIO()
    step = ConfigureClient("codex", str(tmp_path / "config.toml"), 8765, "x", COMMAND, "/bin/c")
    with pytest.raises(InstallError):
        Executor(dry_run=False, out=out, runner=Quoting()).run([step])
    text = out.getvalue()
    assert "| Error: invalid config" in text and "| see docs" in text
    assert SENTINEL not in text and "1 line(s) not shown" in text


def test_gemini_says_why_its_listing_shows_the_server_disabled(tmp_path, stored):
    listed = "○ proton-mail: /x connect (stdio) - Disabled"
    path = tmp_path / "settings.json"
    output, executor = configure(path, "gemini", native="/bin/gemini", runner=Scripted(listed))
    assert "folder it does not trust" in output and not executor.warnings


# ChatGPT and Codex share one file (S1) and a dry run refuses what a run would (S3) ----------


CHATGPTS = (
    "[mcp_servers.proton-mail]\n"
    'url = "http://127.0.0.1:8765/mcp"\n'
    'http_headers = { Authorization = "Bearer old" }\n'
)


@pytest.mark.parametrize("native", ["", "/bin/codex"])
def test_codex_keeps_chatgpts_http_entry_which_it_uses_too(tmp_path, stored, native):
    path = tmp_path / "config.toml"
    path.write_text(CHATGPTS)
    runner = Scripted()
    for dry_run in (True, False):
        output, executor = configure(path, "codex", dry_run, native, runner)
        assert "kept: the proton-mail entry there connects over HTTP" in output
        assert executor.records[0]["status"] == "unchanged" and not executor.warnings
    assert path.read_text() == CHATGPTS and runner.calls == [] and not list(tmp_path.glob("*.bak"))
    path.write_text(CHATGPTS.replace("8765", "9000"))
    output, executor = configure(path, "codex")
    [warning] = executor.warnings
    assert "does not point at this server's http://127.0.0.1:8765/mcp" in warning
    assert "--client chatgpt" in warning and "Bearer old" not in output


@posix
def test_chatgpt_replaces_codexs_connect_entry_and_says_so(tmp_path, stored):
    path = tmp_path / "config.toml"
    path.write_text(clients.toml_table(clients.entry("codex", 8765, "", COMMAND)))
    output, executor = configure(path, "chatgpt", dry_run=True)
    assert "starts a command, which Codex can use" in executor.warnings[0] and stored == []
    output, executor = configure(path, "chatgpt")
    assert "ChatGPT's direct HTTP entry replaces it" in executor.warnings[0]
    entry = tomllib.loads(path.read_text())["mcp_servers"]["proton-mail"]
    assert entry["url"] == "http://127.0.0.1:8765/mcp" and SENTINEL not in output


def test_chatgpts_dry_run_does_not_predict_a_change_it_cannot_know(tmp_path, stored):
    path = tmp_path / "config.toml"
    path.write_text(CHATGPTS)
    output, executor = configure(path, "chatgpt", dry_run=True)
    assert "a run rewrites it only if its token is not the stored one" in output
    assert "the entry there now" not in output and stored == [] and not executor.warnings


@pytest.mark.parametrize("client", ["chatgpt", "codex"])
def test_a_dry_run_refuses_a_file_the_real_run_would_refuse(tmp_path, stored, client):
    path = tmp_path / "config.toml"
    text = '[mcp_servers]\nproton-mail.command = "x"\n'
    path.write_text(text)
    with pytest.raises(InstallError, match="cannot edit"):
        configure(path, client, dry_run=True)
    assert path.read_text() == text and stored == []


@posix
def test_a_byte_order_mark_stays(tmp_path, stored):
    path = tmp_path / "mcp.json"
    path.write_bytes(codecs.BOM_UTF8 + b'{"k": 1}')
    configure(path)
    data = path.read_bytes()
    assert data.startswith(codecs.BOM_UTF8) and json.loads(data[3:])["k"] == 1


@posix
@pytest.mark.parametrize("newline", [b"\r\n", b"\n"])
def test_a_toml_without_the_entry_keeps_its_byte_order_mark_and_line_endings(
    tmp_path, stored, newline
):
    path = tmp_path / "config.toml"
    before = codecs.BOM_UTF8 + b'# My Codex settings\nmodel = "o3"\n'.replace(b"\n", newline)
    path.write_bytes(before)
    configure(path, "chatgpt")
    data = path.read_bytes()
    assert data.startswith(before + newline + b"[mcp_servers.proton-mail]" + newline)
    rest = data[3:].replace(newline, b"")
    assert b"\r" not in rest and b"\n" not in rest
    entry = tomllib.loads(data[3:].decode())["mcp_servers"]["proton-mail"]
    assert entry["http_headers"] == {"Authorization": f"Bearer {SENTINEL}"}


# setup --client in the plan ------------------------------------------------------------


def test_setup_configures_clients_after_the_self_check_and_then_says_to_restart():
    steps = plan(clients=("claude-desktop", "cursor"))
    check = next(index for index, step in enumerate(steps) if isinstance(step, SelfCheck))
    configured = [index for index, step in enumerate(steps) if isinstance(step, ConfigureClient)]
    assert configured and min(configured) > check
    assert [steps[index].client for index in configured] == ["claude-desktop", "cursor"]
    assert [steps[index].port for index in configured] == [8765, 8765]
    venv = "/home/you/.local/share/proton-mail-mcp/venv/bin/proton-mail-mcp"
    env_file = "/home/you/.config/proton-mail-mcp/proton-mail-mcp.env"
    assert {steps[index].command for index in configured} == {
        (venv, "connect", "--env-file", env_file)
    }
    restarts = needs(steps, "restart_client")
    assert [need.when for need in restarts] == ["after", "after"]
    assert "Quit Claude Desktop completely" in restarts[0].text
    assert "Restart Cursor" in restarts[1].text
    # No Node.js and no mcp-remote any more (M1).
    assert needs(steps, "node_for_mcp_remote") == []


def test_a_client_with_its_own_command_line_on_path_uses_it():
    probe = simulated_probe(simulated("macos"))
    probe.tools["claude"] = "/opt/homebrew/bin/claude"
    steps = plan("macos", probe, clients=("claude-code", "gemini"))
    code, gemini = [step for step in steps if isinstance(step, ConfigureClient)]
    assert code.native == "/opt/homebrew/bin/claude" and gemini.native == ""


def test_grok_bot_gets_the_connector_to_add_and_chatgpt_covers_codex():
    steps = plan(clients=("grok-bot", "codex", "chatgpt"))
    configured = [step.client for step in steps if isinstance(step, ConfigureClient)]
    assert configured == ["chatgpt"]
    text = said(steps)
    assert "Grok Bot, or any agent on this computer: add this stdio server" in text
    assert '"command": "/home/you/.local/share/proton-mail-mcp/venv/bin/proton-mail-mcp"' in text
    assert "only ChatGPT's is written; Codex uses it too" in text


def test_planning_never_reads_the_token(monkeypatch):
    def lookup(*_args, **_kwargs):
        pytest.fail("planning read the token")

    monkeypatch.setattr(credential_store, "lookup", lookup)
    for family in ("debian", "macos", "windows"):
        steps = plan(family, clients=tuple(clients.CLIENTS))
        # grok-bot is told, not written; Codex shares ChatGPT's entry.
        assert len([step for step in steps if isinstance(step, ConfigureClient)]) == 5


def test_skip_credentials_without_a_stored_token_configures_no_client():
    steps = plan(clients=("cursor",), skip_credentials=True)
    assert not [step for step in steps if isinstance(step, ConfigureClient)]
    assert "no client is configured now" in said(steps)
    probe = simulated_probe(simulated("debian"))
    probe.secrets = {("libsecret", TOKEN_SERVICE, USER)}
    steps = plan(probe=probe, clients=("cursor",), skip_credentials=True)
    assert [step.client for step in steps if isinstance(step, ConfigureClient)] == ["cursor"]


# Hand-offs to a person ------------------------------------------------------------------


def test_the_password_hand_off_sits_right_before_the_login_offer_and_prompt():
    steps = plan()
    [need] = needs(steps, "bridge_password")
    assert need.terminal and not need.blocking and need.when == "during"
    assert "not the Proton account password" in need.text and "two-factor" in need.text
    after = steps[steps.index(need) + 1 :]
    assert type(after[0]).__name__ == "BridgeLogin"
    assert type(after[1]).__name__ == "StoreSecret" and after[1].source == "prompt"


def test_a_stored_password_needs_no_hand_off():
    probe = simulated_probe(simulated("debian"))
    probe.secrets = {("libsecret", BRIDGE_SERVICE, USER), ("libsecret", TOKEN_SERVICE, USER)}
    assert needs(plan(probe=probe), "bridge_password") == []


def test_macos_warns_of_the_keychain_prompt_before_services_start():
    steps = plan("macos")
    [keychain] = needs(steps, "keychain_prompt")
    assert "Always Allow" in keychain.text and not keychain.terminal
    assert steps.index(keychain) < next(
        index
        for index, step in enumerate(steps)
        if isinstance(step, Command) and step.argv[:2] == ("launchctl", "bootstrap")
    )
    assert needs(plan("debian"), "keychain_prompt") == []


def test_installing_bridge_on_linux_hands_over_sudo_first():
    steps = plan("debian")
    [sudo] = needs(steps, "sudo")
    first = next(index for index, step in enumerate(steps) if getattr(step, "sudo", False))
    assert steps.index(sudo) == first - 1 and sudo.terminal
    probe = simulated_probe(simulated("debian"))
    probe.files["/usr/bin/protonmail-bridge"] = ""
    assert needs(plan(probe=probe), "sudo") == []


# A Bridge app already running (F5) --------------------------------------------------------


@pytest.mark.parametrize("family", ["macos", "debian"])
def test_a_running_bridge_app_comes_first_and_blocks(family):
    probe = simulated_probe(simulated(family))
    probe.succeeding.add(("pgrep", "-u", probe.user_id, "-x", "bridge-gui"))
    steps = plan(family, probe)
    need = steps[0]
    assert isinstance(need, NeedsUser) and need.id == "quit_bridge_app"
    assert need.blocking and need.when == "before"
    assert "Quit the app" in need.text and "--skip-bridge-service" in need.text


def test_a_windows_bridge_window_is_found_by_its_proton_path():
    probe = simulated_probe(simulated("windows"))
    probe.succeeding.add(
        (
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            planner._POWERSHELL_MODULES
            + "if (Get-Process -Name 'bridge-gui' -ErrorAction SilentlyContinue | "
            "Where-Object { $_.Path -like '*Proton*' }) { exit 0 }; exit 1",
        )
    )
    assert needs(plan("windows", probe), "quit_bridge_app")


def test_a_busy_imap_port_without_the_installers_service_means_another_bridge():
    probe = simulated_probe(simulated("macos"))
    probe.open_ports = {1143}
    [need] = needs(plan("macos", probe), "quit_bridge_app")
    assert "127.0.0.1:1143" in need.text


def test_the_installers_own_running_bridge_is_not_in_the_way():
    platform = simulated("macos")
    probe = simulated_probe(platform)
    probe.open_ports = {1143}
    paths = paths_for(platform, probe.env, probe.home)
    plist = f"{paths.agents_dir}/com.proton-mail-mcp.bridge.plist"
    probe.files[plist] = f"<!-- {MARKER} -->"
    probe.outputs[("launchctl", "print", "gui/501/com.proton-mail-mcp.bridge")] = (
        f"gui/501/com.proton-mail-mcp.bridge = {{\n\tpath = {plist}\n}}\n"
    )
    assert needs(plan("macos", probe), "quit_bridge_app") == []
    linux = simulated_probe(simulated("debian"))
    linux.open_ports = {1143}
    unit_dir = paths_for(simulated("debian"), linux.env, linux.home).unit_dir
    unit = "proton-mail-mcp-bridge.service"
    linux.files[f"{unit_dir}/{unit}"] = f"# {MARKER}\n"
    linux.outputs[("systemctl", "--user", "show", "--property=FragmentPath", "--value", unit)] = (
        f"{unit_dir}/{unit}\n"
    )
    query = ("systemctl", "--user", "show", "--property=ActiveState", "--value", unit)
    linux.outputs[query] = "active\n"
    assert needs(plan("debian", linux), "quit_bridge_app") == []
    linux.outputs[query] = "failed\n"
    assert needs(plan("debian", linux), "quit_bridge_app")


@pytest.mark.parametrize("flag", ["skip_bridge_service", "skip_services"])
def test_keeping_the_app_or_setting_up_no_service_needs_no_quit(flag):
    probe = simulated_probe(simulated("macos"))
    probe.succeeding.add(("pgrep", "-u", probe.user_id, "-x", "bridge-gui"))
    assert needs(plan("macos", probe, **{flag: True}), "quit_bridge_app") == []


# The service's Python and uv's cache (F4) -------------------------------------------------


def venv_command(steps):
    return next(step for step in steps if isinstance(step, Command) and "venv" in step.argv[1:3])


def test_a_python_in_uvs_cache_is_followed_out_of_it():
    probe = simulated_probe(simulated("macos"))
    probe.links[CACHED] = MANAGED
    assert venv_command(plan("macos", probe, python=CACHED)).argv[0] == MANAGED


def test_a_stable_name_outside_the_cache_is_not_resolved_further():
    probe = simulated_probe(simulated("macos"))
    stable = "/opt/homebrew/bin/python3.12"
    probe.links[CACHED] = stable
    probe.links[stable] = "../Cellar/python@3.12/3.12.8/bin/python3.12"
    assert venv_command(plan("macos", probe, python=CACHED)).argv[0] == stable


@pytest.mark.parametrize(
    ("python", "env"),
    [
        (CACHED, {}),
        ("C:\\Users\\you\\AppData\\Local\\uv\\cache\\archive-v0\\x\\Scripts\\python.exe", {}),
        ("/srv/uvc/envs/x/bin/python", {"UV_CACHE_DIR": "/srv/uvc"}),
    ],
)
def test_a_python_only_in_uvs_cache_stops_the_plan_and_names_python(python, env):
    family = "windows" if python.startswith("C:") else "macos"
    probe = simulated_probe(simulated(family))
    probe.env.update(env)
    with pytest.raises(PlanError, match="--python"):
        plan(family, probe, python=python)


def test_an_existing_venv_on_a_cached_python_is_reported():
    platform = simulated("macos")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    for name in ("", "/.proton-mail-mcp-installer", "/bin/python"):
        probe.files[paths.venv + name] = ""
    probe.files[f"{paths.venv}/pyvenv.cfg"] = (
        "home = /Users/you/.cache/uv/archive-v0/AbC/bin\nversion = 3.12.8\n"
    )
    text = said(plan("macos", probe))
    assert "runs on a Python in uv's cache" in text and "--venv" in text
    probe.files[f"{paths.venv}/pyvenv.cfg"] = "home = /opt/homebrew/bin\n"
    assert "uv's cache" not in said(plan("macos", probe))


def test_the_cli_starts_from_this_runs_base_python_outside_the_cache(monkeypatch):
    platform = simulated("macos")
    probe = simulated_probe(platform)
    probe.links[CACHED] = MANAGED
    monkeypatch.setattr(cli, "detect_host", lambda: platform)
    monkeypatch.setattr(cli, "HostProbe", lambda _platform: probe)
    monkeypatch.setattr(sys, "_base_executable", CACHED, raising=False)
    assert cli._interpreter() == MANAGED


# A stale LaunchAgent (F10) ----------------------------------------------------------------


def test_a_disabled_launch_agent_from_an_earlier_install_is_named():
    platform = simulated("macos")
    probe = simulated_probe(platform)
    agents = paths_for(platform, probe.env, probe.home).agents_dir
    stale = f"{agents}/com.proton-mail-mcp.plist.disabled"
    probe.files[stale] = "<string>old-token</string>"
    text = said(plan("macos", probe))
    assert f"{stale} is left from an earlier install" in text
    assert "old bearer token" in text and f"rm -f -- {stale}" in text
    assert "old-token" not in text


# The command line: --json, configure-client, the outline, show-token --copy ----------------


@pytest.fixture
def host(monkeypatch, tmp_path):
    """The real-host path on a simulated Debian machine whose home is tmp_path; setup ran
    before, so the settings and token are there, and Bridge is installed."""
    if os.name != "posix":
        pytest.skip("the executor writes these POSIX paths for real")
    platform = simulated("debian")
    probe = simulated_probe(platform)
    probe.home = str(tmp_path / "home")
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[paths.env_file] = (
        f"# {MARKER}\nPROTON_BRIDGE_USER={USER}\nPROTON_MCP_CREDENTIAL_STORE=libsecret\n"
        "PROTON_MCP_PORT=8799\n"
    )
    probe.files["/usr/bin/protonmail-bridge"] = ""
    probe.secrets = {("libsecret", TOKEN_SERVICE, USER)}
    # The service's environment holds a proton-mail-mcp that has connect.
    launcher = f"{paths.venv}/bin/proton-mail-mcp"
    probe.files[launcher] = ""
    probe.succeeding.add((launcher, "connect", "--help"))
    monkeypatch.setattr(cli, "detect_host", lambda: platform)
    monkeypatch.setattr(cli, "HostProbe", lambda _platform: probe)
    monkeypatch.setattr(cli, "terminal_available", lambda: True)
    monkeypatch.setattr(cli, "default_source", lambda: "/src/checkout")
    return SimpleNamespace(
        platform=platform, probe=probe, home=Path(probe.home), paths=paths, launcher=launcher
    )


def test_configure_client_uses_the_saved_port_and_prints_no_token(host, stored, capsys):
    assert cli.main(["configure-client", "--client", "chatgpt", "--yes"]) == 0
    captured = capsys.readouterr()
    entry = tomllib.loads((host.home / ".codex" / "config.toml").read_text())["mcp_servers"]
    assert entry["proton-mail"]["url"] == "http://127.0.0.1:8799/mcp"
    assert SENTINEL not in captured.out + captured.err
    assert "Quit the ChatGPT app completely" in captured.out


def test_configure_client_json_is_one_document_on_stdout_without_the_token(host, stored, capsys):
    argv = ["configure-client", "--client", "claude-code", "--client", "cursor", "--yes", "--json"]
    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["result"] == "done" and report["exit_code"] == 0
    assert report["command"] == "configure-client" and report["dry_run"] is False
    assert [step["status"] for step in report["steps"]] == ["done", "done"]
    restarts = [need for need in report["needs_user"] if need["id"] == "restart_client"]
    assert len(restarts) == 2 and all(need["when"] == "after" for need in restarts)
    assert report["connector"] == {
        "name": "proton-mail",
        "transport": "stdio",
        "command": host.launcher,
        "args": ["connect", "--env-file", host.paths.env_file],
        "url": "http://127.0.0.1:8799/mcp",
        "token": "not shown: connect reads it from the credential store each time it starts",
    }
    assert "configure-client plan for" in captured.err
    assert SENTINEL not in captured.out + captured.err
    # Neither client holds the token: both start connect, which reads it.
    entry = json.loads((host.home / ".claude.json").read_text())["mcpServers"]["proton-mail"]
    assert entry["command"] == host.launcher and stored == []


def test_configure_client_port_overrides_the_saved_one_and_must_be_a_port(host, stored, capsys):
    assert cli.main(["configure-client", "--client", "chatgpt", "--yes", "--port", "9001"]) == 0
    entry = tomllib.loads((host.home / ".codex" / "config.toml").read_text())["mcp_servers"]
    assert entry["proton-mail"]["url"] == "http://127.0.0.1:9001/mcp"
    assert cli.main(["configure-client", "--client", "cursor", "--yes", "--port", "0"]) == 2
    assert "between 1 and 65535" in capsys.readouterr().err


def test_configure_client_needs_a_setup_whose_environment_has_connect(host, capsys):
    host.probe.succeeding.discard((host.launcher, "connect", "--help"))
    assert cli.main(["configure-client", "--client", "cursor", "--yes", "--json"]) == 2
    assert "installed by an older setup" in json.loads(capsys.readouterr().out)["error"]
    del host.probe.files[host.launcher]
    assert cli.main(["configure-client", "--client", "cursor", "--yes", "--json"]) == 2
    assert "run setup first" in json.loads(capsys.readouterr().out)["error"]
    assert not host.home.exists()


def test_uninstall_hands_over_sudo_before_removing_bridge():
    platform = simulated("debian")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    steps = planner.uninstall_plan(platform, Options(remove_bridge=True), probe, paths)
    [sudo] = needs(steps, "sudo")
    assert getattr(steps[steps.index(sudo) + 1], "sudo", False)


def test_configure_client_without_a_stored_token_refuses_and_writes_nothing(host, capsys):
    host.probe.secrets = set()
    assert cli.main(["configure-client", "--client", "chatgpt", "--yes", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["result"] == "refused" and "Run setup first" in report["error"]
    assert not (host.home / ".codex").exists()


@pytest.mark.parametrize("flag", ["--json", "--non-interactive"])
def test_asking_nothing_needs_a_dry_run_or_yes(host, flag, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["setup", flag])
    assert caught.value.code == 2 and "--dry-run or --yes" in capsys.readouterr().err


def test_a_json_dry_run_holds_the_plan_and_every_hand_off(capsys):
    argv = ["setup", "--dry-run", "--json", "--simulate", "macos", "--bridge-user", USER]
    assert cli.main([*argv, "--client", "claude-desktop"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["result"] == "planned" and report["dry_run"] is True
    assert report["platform"]["family"] == "macos"
    assert report["steps"] and {step["status"] for step in report["steps"]} == {"planned"}
    assert report["steps"][-1]["kind"] == "ConfigureClient"
    found = {need["id"]: need for need in report["needs_user"]}
    assert {"bridge_password", "keychain_prompt", "restart_client"} <= set(found)
    password = found["bridge_password"]
    # As the same command with --yes would: stopped for the terminal it needs (S3).
    assert password["needs_terminal"] and password["stops_run"] and not password["blocking"]
    assert not found["keychain_prompt"]["stops_run"]
    assert password["command"] == (
        f"proton-mail-mcp setup --bridge-user {USER} --client claude-desktop"
    )
    assert report["launcher"] == "proton-mail-mcp"
    assert report["connector"]["args"][0] == "connect"
    assert "setup plan for" in captured.err and "setup plan for" not in captured.out


def test_json_apply_stops_for_the_password_and_hands_over_the_command(host, capsys):
    code = cli.main(["setup", "--yes", "--json", "--client", "cursor"])
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report["result"] == "needs_user"
    [password] = [need for need in report["needs_user"] if need["stops_run"]]
    assert password["id"] == "bridge_password"
    assert password["command"] == "proton-mail-mcp setup --client cursor"
    assert not host.home.exists()


def test_json_apply_stops_for_a_running_bridge_app_before_any_change(host, capsys):
    host.probe.secrets.add(("libsecret", BRIDGE_SERVICE, USER))
    host.probe.succeeding.add(("pgrep", "-u", "1000", "-x", "bridge-gui"))
    assert cli.main(["setup", "--yes", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert [need["id"] for need in report["needs_user"] if need["stops_run"]] == ["quit_bridge_app"]
    assert not host.home.exists()


def test_non_interactive_stops_for_sudo_unless_it_holds_credentials(host, capsys):
    del host.probe.files["/usr/bin/protonmail-bridge"]
    argv = ["setup", "--yes", "--non-interactive", "--skip-credentials"]
    assert cli.main(argv) == 2
    assert "sudo" in capsys.readouterr().err and not host.home.exists()
    host.probe.succeeding.add(cli.SUDO_CACHED)
    platform = host.platform
    paths = host.paths
    steps = install_plan(
        platform,
        Options(bridge_user=USER, source="/s", python="/usr/bin/python3", skip_credentials=True),
        host.probe,
        paths,
    )
    assert cli._stops(steps, cli.build_parser().parse_args(argv), host.probe) == []


@pytest.fixture
def applied(host, monkeypatch):
    """Apply for real on the fake host, with every program, check and token scripted."""
    host.probe.secrets.add(("libsecret", BRIDGE_SERVICE, USER))
    record = SimpleNamespace(commands=[], checks=[])

    def runner(argv, **kwargs):
        record.commands.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def check_service(port, token, say):
        record.checks.append((port, token))
        say("    ok: scripted")

    real = cli.Executor

    def factory(*, dry_run, **kwargs):
        return real(
            dry_run=dry_run,
            runner=runner,
            port_open=lambda _port: True,
            sleep=lambda _seconds: None,
            check_service=check_service,
            **kwargs,
        )

    monkeypatch.setattr(cli, "Executor", factory)
    return record


def test_json_setup_with_a_client_ends_configured_and_never_shows_the_token(
    host, applied, stored, capsys
):
    assert cli.main(["setup", "--yes", "--json", "--client", "cursor"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["result"] == "done"
    assert all(step["status"] in {"done", "skipped", "unchanged"} for step in report["steps"])
    assert report["steps"][-1] == {
        "n": len(report["steps"]),
        "kind": "ConfigureClient",
        "summary": "Add the proton-mail server to Cursor's config",
        "status": "done",
    }
    assert applied.checks == [(8765, SENTINEL)]
    assert SENTINEL not in captured.out + captured.err
    config = json.loads((host.home / ".cursor" / "mcp.json").read_text())
    assert config["mcpServers"]["proton-mail"]["args"][0] == "connect"
    for path in host.home.rglob("*"):
        if path.is_file():
            assert SENTINEL not in path.read_text(errors="replace"), path


def conversation(monkeypatch, capsys, *replies):
    """Script input() and keep what was printed before each question, in order."""
    transcript = []
    pending = list(replies)

    def fake(prompt=""):
        transcript.append(capsys.readouterr().out)
        transcript.append(f"?? {prompt}\n")
        assert pending, f"unexpected question: {prompt}"
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", fake)
    monkeypatch.setattr(cli, "on_terminal", lambda: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    return transcript


def test_setup_shows_what_it_will_do_before_asking_for_the_username(host, monkeypatch, capsys):
    host.probe.files[host.paths.env_file] = f"# {MARKER}\n"
    transcript = conversation(monkeypatch, capsys, USER, "", "n")
    assert cli.main(["setup"]) == 1
    text = "".join(transcript) + capsys.readouterr().out
    outline = text.index("Setup on Ubuntu")
    question = text.index("?? Bridge IMAP username")
    assert outline < question < text.index("setup plan for") < text.index("?? Apply these steps")
    assert "  1. " in text[outline:question] and "! For you:" in text[outline:question]
    assert not host.home.exists()


def test_on_a_terminal_a_running_bridge_app_can_be_kept(host, monkeypatch, capsys):
    host.probe.secrets.add(("libsecret", BRIDGE_SERVICE, USER))
    host.probe.succeeding.add(("pgrep", "-u", "1000", "-x", "bridge-gui"))
    transcript = conversation(monkeypatch, capsys, "", "k", "n")
    assert cli.main(["setup"]) == 1
    text = "".join(transcript) + capsys.readouterr().out
    assert "?? Press Enter once it has quit" in text
    plan_text = text[text.index("setup plan for") :]
    assert "proton-mail-mcp-bridge.service" not in plan_text
    assert "quit_bridge_app" not in plan_text and "The Proton Mail Bridge app" not in plan_text


def test_on_a_terminal_an_app_that_never_quits_stops_setup(host, monkeypatch, capsys):
    host.probe.succeeding.add(("pgrep", "-u", "1000", "-x", "bridge-gui"))
    conversation(monkeypatch, capsys, "", "", "", "")
    assert cli.main(["setup"]) == 2
    assert "Another Bridge still runs" in capsys.readouterr().err
    assert not host.home.exists()


def test_show_token_copy_hands_the_token_over_on_stdin_and_prints_nothing(monkeypatch, capsys):
    platform = simulated("macos")
    monkeypatch.setattr(cli, "detect_host", lambda: platform)
    monkeypatch.setattr(cli, "HostProbe", simulated_probe)
    monkeypatch.setattr(credential_store, "lookup", lambda *_args, **_kwargs: SENTINEL)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli, "on_terminal", lambda: True)
    assert cli.main(["show-token", "--copy", "--bridge-user", USER]) == 0
    [(argv, kwargs)] = calls
    assert argv == ["pbcopy"] and kwargs["input"] == SENTINEL
    assert kwargs["stdout"] is subprocess.DEVNULL and kwargs["stderr"] is subprocess.DEVNULL
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err and "Copied" in captured.out
    assert "Universal Clipboard" in captured.out


def test_show_token_copy_without_a_working_clipboard_says_what_else_to_do(monkeypatch, capsys):
    platform = simulated("debian")
    monkeypatch.setattr(cli, "detect_host", lambda: platform)
    monkeypatch.setattr(cli, "HostProbe", simulated_probe)
    monkeypatch.setattr(credential_store, "lookup", lambda *_args, **_kwargs: SENTINEL)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        cli.subprocess, "run", lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1)
    )
    monkeypatch.setattr(cli, "on_terminal", lambda: True)
    assert cli.main(["show-token", "--copy", "--bridge-user", USER]) == 2
    captured = capsys.readouterr()
    assert "wl-copy, xclip, xsel" in captured.err and "configure-client" in captured.err
    assert SENTINEL not in captured.out + captured.err


# Usage errors, the launcher a person runs, and unattended sudo (S2, S4, S5) ---------------


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["setup", "--json"], "--dry-run or --yes"),
        (["setup", "--json", "--yes", "--source", "/nowhere"], "--source must be"),
        (["setup", "--json", "--yes", "--python", "/nowhere/python"], "--python must name"),
        (["setup", "--json", "--simulate", "macos"], "--simulate only plans"),
        (["setup", "--json", "--client", "nobody"], "invalid choice"),
        (["configure-client", "--json", "--yes"], "--client"),
    ],
)
def test_with_json_a_usage_error_is_still_one_json_document(host, capsys, argv, error):
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert caught.value.code == 2 and report["exit_code"] == 2
    assert report["result"] == "refused" and error in report["error"]
    assert report["schema"] == cli.REPORT_SCHEMA and error in captured.err


def test_without_json_a_usage_error_prints_no_json(host, capsys):
    with pytest.raises(SystemExit):
        cli.main(["setup", "--non-interactive"])
    assert capsys.readouterr().out == ""


def test_under_uvx_the_hand_off_starts_the_same_copy_through_uvx(host, capsys, monkeypatch):
    cache = "/home/you/.cache/uv/archive-v0/AbC123"
    monkeypatch.setattr(cli.sys, "prefix", cache)
    monkeypatch.setattr(cli, "default_source", lambda: "/home/you/src/proton mail mcp")
    assert cli.handoff_launcher() == [
        "uvx",
        "--from",
        "/home/you/src/proton mail mcp",
        "proton-mail-mcp",
    ]
    assert cli.main(["setup", "--yes", "--json", "--client", "cursor"]) == 2
    report = json.loads(capsys.readouterr().out)
    [password] = [need for need in report["needs_user"] if need["stops_run"]]
    assert password["command"] == (
        "uvx --from '/home/you/src/proton mail mcp' proton-mail-mcp setup --client cursor"
    )
    assert report["launcher"] == "uvx --from '/home/you/src/proton mail mcp' proton-mail-mcp"


def test_outside_uvx_the_hand_off_names_the_running_script_by_path(tmp_path, monkeypatch):
    script = tmp_path / "bin" / "proton-mail-mcp"
    script.parent.mkdir()
    script.write_text("")
    monkeypatch.setattr(cli.sys, "argv", [str(script), "setup"])
    assert cli.handoff_launcher() == [str(script)]
    monkeypatch.setattr(cli.sys, "argv", ["-c"])
    assert cli.handoff_launcher() == ["proton-mail-mcp"]


def test_an_unattended_run_never_lets_sudo_ask(host, applied, stored, monkeypatch, capsys):
    made = []
    real = cli.Executor

    def factory(**kwargs):
        made.append(kwargs.get("sudo"))
        return real(**kwargs)

    monkeypatch.setattr(cli, "Executor", factory)
    cli.main(["setup", "--yes", "--json"])
    assert made and set(made) == {("sudo", "-n")}
    made.clear()
    cli.main(["setup", "--dry-run", "--bridge-user", USER])
    assert made == [("sudo",)]


def test_a_sudo_step_that_fails_unattended_says_to_refresh_sudo():
    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "sudo: a password is required")

    executor = Executor(dry_run=False, out=io.StringIO(), runner=runner, sudo=("sudo", "-n"))
    with pytest.raises(InstallError, match="sudo -v"):
        executor.run([Command(("apt-get", "update"), "Refresh", sudo=True)])


# --python (N6) and the Bridge login hand-off (N8, F26) ------------------------------------


def test_python_by_name_is_found_on_path_and_must_be_new_enough(host, monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/opt/{name}")
    monkeypatch.setattr(cli.os.path, "isfile", lambda path: path == "/opt/python3.11")
    monkeypatch.setattr(cli, "_new_enough", lambda python: False)
    with pytest.raises(SystemExit):
        cli.main(["setup", "--yes", "--python", "python3.11"])
    assert "Python 3.12 or newer" in capsys.readouterr().err
    assert cli._given_python("python3.12") == "/opt/python3.12"


def test_unattended_without_a_username_hands_over_the_login(host, capsys):
    host.probe.files[host.paths.env_file] = f"# {MARKER}\n"
    assert cli.main(["setup", "--yes", "--json", "--client", "grok-bot"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["result"] == "needs_user"
    found = {need["id"]: need for need in report["needs_user"] if need["stops_run"]}
    login = found["bridge_login"]
    assert login["needs_terminal"] and login["blocking"] and login["when"] == "before"
    assert login["command"] == "proton-mail-mcp setup --client grok-bot"
    assert "verification link" in login["text"] and "pasting works" in login["text"]
    assert not host.home.exists()


# What a dry run shows, and what stops a real run first (F24, F29, F34) --------------------


def test_a_dry_run_without_secret_tool_still_plans_and_names_the_package():
    probe = simulated_probe(simulated("debian"))
    del probe.tools["secret-tool"]
    with pytest.raises(PlanError, match="libsecret-tools"):
        plan(probe=probe)
    steps = plan(probe=probe, dry_run=True)
    [need] = needs(steps, "store_tools")
    assert need.blocking and need.when == "before" and "libsecret-tools" in need.text


def test_a_session_bus_without_a_secret_service_stops_before_anything(monkeypatch):
    probe = simulated_probe(simulated("debian"))
    probe.tools["dbus-send"] = "/usr/bin/dbus-send"
    probe.env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/user/1000/bus"
    query = planner._DBUS_QUERY
    probe.outputs[(*query, "org.freedesktop.DBus.ListNames")] = 'string "org.freedesktop.DBus"'
    probe.outputs[(*query, "org.freedesktop.DBus.ListActivatableNames")] = 'string "x"'
    [need] = needs(plan(probe=probe), "secret_service")
    assert need.blocking and "unix:path=/run/user/1000/bus" in need.text
    assert "--credential-store pass" in need.text
    probe.outputs[(*query, "org.freedesktop.DBus.ListActivatableNames")] = (
        'string "org.freedesktop.secrets"'
    )
    assert needs(plan(probe=probe), "secret_service") == []
    assert needs(plan(probe=probe, skip_credentials=True), "secret_service") == []


def test_a_token_file_from_an_earlier_manual_setup_is_named_but_never_read():
    probe = simulated_probe(simulated("debian"))
    path = "/home/you/.config/proton-mail-mcp/token"
    probe.files[path] = SENTINEL
    text = said(plan(probe=probe))
    assert f"{path} is left from an earlier manual setup" in text
    assert f"rm -f -- {path}" in text and "(401)" in text and SENTINEL not in text


def test_uninstall_says_what_client_configs_and_backups_keep():
    platform = simulated("debian")
    probe = simulated_probe(platform)
    paths = paths_for(platform, probe.env, probe.home)
    probe.files[f"{paths.state_dir}/service.pid"] = "4242"
    steps = planner.uninstall_plan(platform, Options(), probe, paths)
    text = said(steps)
    assert "dated .bak copy" in text and "docs/clients/" in text
    assert f'kill "$(cat {paths.state_dir}/service.pid)"' in text


# --print-config ---------------------------------------------------------------------------


def test_print_config_shows_every_entry_and_reads_no_token(host, capsys, monkeypatch):
    def lookup(*_args, **_kwargs):
        pytest.fail("print-config read the token")

    monkeypatch.setattr(credential_store, "lookup", lookup)
    assert cli.main(["setup", "--print-config"]) == 0
    out = capsys.readouterr().out
    for name, client in clients.CLIENTS.items():
        # Codex shares ChatGPT's file and table, so only ChatGPT's entry is shown.
        assert (f"# {client.label}: " in out) == (name != "codex")
    assert "only ChatGPT's entry is shown; Codex uses it too" in out
    assert out.count("[mcp_servers.proton-mail]") == 1
    assert f'"command": "{host.launcher}"' in out and clients.TOKEN_PLACEHOLDER in out
    assert "claude mcp add --scope user proton-mail -- " in out
    assert "cursor://anysphere.cursor-deeplink/mcp/install?name=proton-mail" in out
    assert not host.home.exists()


def test_print_config_with_json_lists_each_config_and_the_connector(host, capsys):
    argv = ["configure-client", "--print-config", "--json", "--client", "codex"]
    assert cli.main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["result"] == "printed"
    [config] = report["configs"]
    assert config["format"] == "toml" and config["path"].endswith("/.codex/config.toml")
    entry = tomllib.loads(config["snippet"])["mcp_servers"]["proton-mail"]
    assert entry == {
        "command": host.launcher,
        "args": ["connect", "--env-file", host.paths.env_file],
    }
    assert config["command"].startswith("codex mcp add proton-mail -- ")
    assert report["connector"]["url"] == "http://127.0.0.1:8799/mcp" and report["notes"] == []
    argv += ["--client", "chatgpt", "--client", "gemini"]
    assert cli.main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    assert [config["client"] for config in report["configs"]] == ["chatgpt", "gemini"]
    assert "Codex uses it too" in report["notes"][0]
    gemini = report["configs"][1]["command"]
    assert gemini.startswith(f"gemini mcp add --scope user proton-mail {host.launcher} -- connect")


def test_a_bad_saved_setting_is_named_and_no_flag_is_blamed(host, capsys):
    settings = host.probe.files[host.paths.env_file]
    host.probe.files[host.paths.env_file] = settings.replace("8799", "70000")
    assert cli.main(["configure-client", "--print-config", "--client", "cursor"]) == 2
    error = capsys.readouterr().err
    assert f"PROTON_MCP_PORT in {host.paths.env_file}" in error and "--port must" not in error
    host.probe.files[host.paths.env_file] = settings + "PMM_INSTALLER_VENV=relative/venv\n"
    assert cli.main(["configure-client", "--print-config", "--client", "cursor"]) == 2
    error = capsys.readouterr().err
    assert "PMM_INSTALLER_VENV in" in error and "--venv must" not in error


def test_only_the_whole_json_flag_counts_and_its_usage_errors_are_json(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["setup", "--dry-run", "--js"])
    assert caught.value.code == 2 and capsys.readouterr().out == ""
    with pytest.raises(SystemExit) as caught:
        cli.main(["setup", "--dry-run", "--json=1"])
    assert caught.value.code == 2
    assert json.loads(capsys.readouterr().out)["result"] == "refused"


@pytest.mark.parametrize("mode", ["--dry-run", "--yes"])
def test_a_config_a_step_would_refuse_refuses_the_plan_before_any_step_runs(host, capsys, mode):
    path = host.home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    text = '[mcp_servers]\nproton-mail.command = "x"\n'
    path.write_text(text)
    argv = ["configure-client", "--client", "cursor", "--client", "codex", mode, "--json"]
    assert cli.main(argv) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["result"] == "refused" and "cannot edit safely" in report["error"]
    assert [step["status"] for step in report["steps"]] == ["planned", "failed"]
    assert path.read_text() == text and not (host.home / ".cursor").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["configure-client", "--client", "chatgpt", "--dry-run"],
        ["configure-client", "--client", "chatgpt", "--yes"],
        ["configure-client", "--client", "codex", "--dry-run"],
        ["configure-client", "--client", "codex", "--yes"],
        ["setup", "--client", "chatgpt", "--dry-run"],
    ],
)
def test_a_crlf_config_toml_without_the_entry_is_planned_and_merged(host, stored, capsys, argv):
    path = host.home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    text = b'# My Codex settings\r\nmodel = "o3"\r\n'
    path.write_bytes(text)
    assert cli.main([*argv, "--json"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert SENTINEL not in captured.out + captured.err
    data = path.read_bytes()
    if "--dry-run" in argv:
        assert report["result"] == "planned" and data == text and stored == []
        return
    assert report["result"] == "done"
    assert data.startswith(text + b"\r\n[mcp_servers.proton-mail]\r\n")
    assert b"\n" not in data.replace(b"\r\n", b"") and b"\r\r" not in data
    assert "proton-mail" in tomllib.loads(data.decode())["mcp_servers"]


# Bridge's CLI through a pseudo-terminal (F31, F32, F33) -----------------------------------


INFO = (
    b"\x1b[1mConfiguration for you@example.test\x1b[0m\r\nIMAP Settings\r\n"
    b"Address:   127.0.0.1\r\nIMAP port: 1143\r\nUsername:  you@example.test\r\n"
    b"Password:  " + SENTINEL.encode() + b"\r\nSecurity:  STARTTLS\r\n"
)


def test_the_reader_types_info_after_a_login_and_exit_after_the_username():
    reader = cli.bridge_cli.InfoReader()
    assert reader.feed(b">>> login\r\nUsername: ") == b""
    assert reader.feed(b"Human Verification requested. Open https://verify.example.test\r\n") == b""
    assert reader.feed(b"Account \x1b[1myou\x1b[0m was added success") == b""
    assert reader.feed(b"fully.\r\n>>> ") == b"info\n"
    assert reader.feed(INFO[:40]) == b""
    assert reader.feed(INFO[40:]) == b"exit\n"
    assert reader.username == "you@example.test"
    assert reader.feed(INFO) == b""
    # Only an unfinished line is kept, never the password line once it ends.
    assert SENTINEL not in reader._partial


def test_info_typed_by_hand_is_read_too():
    reader = cli.bridge_cli.InfoReader()
    assert reader.feed(b">>> info\r\n" + INFO) == b"exit\n"
    assert reader.username == "you@example.test"


def test_setup_logs_in_first_and_reads_the_username_from_bridge(host, monkeypatch, capsys):
    host.probe.files[host.paths.env_file] = f"# {MARKER}\n"
    monkeypatch.setattr(cli.bridge_cli, "available", lambda: True)
    sessions = []

    def session(argv):
        sessions.append(argv)
        return 0, USER

    monkeypatch.setattr(cli.bridge_cli, "login_session", session)
    transcript = conversation(monkeypatch, capsys, "", "", "n")
    assert cli.main(["setup"]) == 1
    text = "".join(transcript) + capsys.readouterr().out
    assert sessions == [("/usr/bin/protonmail-bridge", "--cli")]
    assert "?? Bridge is installed, and setup does not know your Bridge account yet" in text
    assert "pasting works" in text and "verification link" in text
    assert f"Bridge's info shows the IMAP username {USER}; setup uses it." in text
    assert "?? Bridge IMAP username" not in text
    plan_text = text[text.index("setup plan for") :]
    assert f"--bridge-user {USER}" not in plan_text and "Offer Bridge's CLI" not in plan_text
    assert f"account {USER}" in plan_text


def test_without_a_username_from_bridge_setup_asks_for_it(host, monkeypatch, capsys):
    host.probe.files[host.paths.env_file] = f"# {MARKER}\n"
    monkeypatch.setattr(cli.bridge_cli, "available", lambda: True)
    monkeypatch.setattr(cli.bridge_cli, "login_session", lambda argv: (1, ""))
    transcript = conversation(monkeypatch, capsys, "", f" \x1b[200~{USER}\x1b[201~\n", "", "n")
    assert cli.main(["setup"]) == 1
    text = "".join(transcript)
    assert "without showing an IMAP username" in text and "?? Bridge IMAP username" in text


def test_a_running_bridge_means_no_cli_and_the_question_instead(host, monkeypatch, capsys):
    host.probe.files[host.paths.env_file] = f"# {MARKER}\n"
    host.probe.succeeding.add(planner.bridge_processes(host.platform, host.probe))
    monkeypatch.setattr(cli.bridge_cli, "available", lambda: True)
    monkeypatch.setattr(cli.bridge_cli, "login_session", lambda argv: pytest.fail("opened"))
    transcript = conversation(monkeypatch, capsys, USER, "", "n")
    cli.main(["setup"])
    assert "Bridge is running, so its CLI cannot open here" in "".join(transcript)


def test_a_login_showing_another_username_stops_before_the_password():
    from proton_mail_mcp.installer.steps import BridgeLogin

    executor = Executor(
        dry_run=False,
        out=io.StringIO(),
        ask=lambda _prompt: "",
        terminal=lambda: True,
        port_open=lambda _port: False,
        bridge_session=lambda argv: (0, "other@example.test"),
    )
    step = BridgeLogin(("/usr/bin/protonmail-bridge", "--cli"), "Offer", account=USER)
    with pytest.raises(InstallError, match="--bridge-user other@example.test"):
        executor.run([step])


def test_a_pasted_password_loses_its_paste_marks_and_line_break():
    answers = iter(["\x1b[200~s3cret\x1b[201~\n", " s3cret "])
    executor = Executor(dry_run=False, out=io.StringIO(), prompt=lambda _label: next(answers))
    assert executor._prompt_twice(USER) == "s3cret"


# A background service where no service manager runs (F28) ---------------------------------


@posix
def test_the_background_start_records_its_process_and_a_rerun_keeps_it(tmp_path, monkeypatch):
    from proton_mail_mcp.installer.steps import StartBackground

    started = []

    def spawn(argv, **kwargs):
        started.append((argv, kwargs))
        return SimpleNamespace(pid=4242)

    step = StartBackground(
        ("/venv/bin/proton-mail-mcp-service", "--env-file", "/cfg/env"),
        str(tmp_path / "state" / "service.log"),
        str(tmp_path / "state" / "service.pid"),
        8765,
        "Start",
        restart_on=("credentials",),
    )
    out = io.StringIO()
    Executor(dry_run=False, out=out, spawn=spawn, port_open=lambda _port: False).run([step])
    [(argv, kwargs)] = started
    assert argv == list(step.argv) and kwargs["start_new_session"] is True
    assert (tmp_path / "state" / "service.pid").read_text() == "4242\n"
    assert stat.S_IMODE((tmp_path / "state" / "service.log").stat().st_mode) == 0o600
    assert "started as process 4242" in out.getvalue()
    # Still running and nothing changed: left alone.
    monkeypatch.setattr("proton_mail_mcp.installer.execute._service_pid", lambda *_args: 4242)
    out = io.StringIO()
    executor = Executor(dry_run=False, out=out, spawn=spawn)
    executor.run([step])
    assert len(started) == 1 and executor.records[0]["status"] == "skipped"


def test_something_already_on_the_port_is_left_alone():
    from proton_mail_mcp.installer.steps import StartBackground

    step = StartBackground(
        ("/usr/bin/protonmail-bridge", "--noninteractive"), "/l", "/p", 1143, "S"
    )
    executor = Executor(
        dry_run=False,
        out=io.StringIO(),
        port_open=lambda _port: True,
        spawn=lambda *_args, **_kwargs: pytest.fail("started"),
    )
    executor.run([step])
    assert executor.records[0]["status"] == "skipped"


FAKE_BRIDGE = """\
import sys
print("Welcome to the fake Bridge shell")
while True:
    line = input(">>> ").strip()
    if line == "login":
        print("Account \\x1b[1myou\\x1b[0m was added successfully.")
    elif line == "info":
        print("IMAP Settings\\nUsername:  you@example.test\\nPassword:  not-the-real-one")
    elif line == "exit":
        sys.exit(0)
"""


@posix
# The child execs at once, so a test runner's threads cannot deadlock it.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_real_pseudo_terminal_carries_the_login_and_types_info_and_exit(tmp_path):
    script = tmp_path / "bridge.py"
    script.write_text(FAKE_BRIDGE)
    # A person types only `login`; the reader types the rest.
    reading, writing = os.pipe()
    os.write(writing, b"login\n")
    os.close(writing)
    saved = os.dup(0)
    try:
        os.dup2(reading, 0)
        status, username = cli.bridge_cli.login_session((sys.executable, "-I", str(script)))
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(reading)
    assert (status, username) == (0, "you@example.test")


def test_a_program_that_is_not_there_never_starts_a_session():
    with pytest.raises(OSError):
        cli.bridge_cli.login_session(("/nowhere/protonmail-bridge", "--cli"))
