"""MCP client configurations: where each client keeps its servers, and the one entry setup adds.

Pure functions over paths and text, so every platform's answer is testable on Linux. Every client
but ChatGPT starts `proton-mail-mcp connect`, which reads the token itself when it starts, so their
files never hold it. ChatGPT takes the server over HTTP: its token reaches only entry() and the
text merged() returns; nothing here prints it, logs it or puts it in an error.
"""

import base64
import json
import ntpath
import posixpath
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

# The key under mcpServers, as docs/clients.md names it.
SERVER_NAME = "proton-mail"
# Stands for the token wherever a config is shown rather than written.
TOKEN_PLACEHOLDER = "<token: run show-token --copy>"


@dataclass(frozen=True)
class Client:
    label: str
    # What a person does after the file changed, for the client to read it.
    restart: str
    # "json": mcpServers in a JSON file; "toml": [mcp_servers] in Codex's config.toml; "none":
    # no file, the entry is shown for the agent or person to add.
    form: str = "json"
    # The client's own command line, used to add the server when it is on PATH.
    native: str = ""
    # Over HTTP with the token in the file, instead of the connect command.
    http: bool = False


CLIENTS = {
    "grok-bot": Client(
        "Grok Bot",
        "Add the proton-mail entry shown above to the bot's MCP servers and restart the bot if it "
        "reads them only at start.",
        form="none",
    ),
    "claude-desktop": Client(
        "Claude Desktop",
        "Quit Claude Desktop completely (on macOS Claude > Quit Claude, on Windows Quit from the "
        "tray icon; closing the window is not enough) and open it again.",
    ),
    "claude-code": Client(
        "Claude Code",
        "Start a new Claude Code session (exit, then run `claude` again); `claude mcp list` "
        f"should then show {SERVER_NAME} as connected.",
        native="claude",
    ),
    "chatgpt": Client(
        "ChatGPT",
        "Quit the ChatGPT app completely and open it again; Codex reads the same entry.",
        form="toml",
        http=True,
    ),
    "codex": Client(
        "Codex",
        f"Start a new Codex session; `codex mcp list` should show {SERVER_NAME}.",
        form="toml",
        native="codex",
    ),
    "gemini": Client(
        "Gemini CLI",
        f"Start a new Gemini CLI session; its /mcp command should list {SERVER_NAME}.",
        native="gemini",
    ),
    "cursor": Client(
        "Cursor",
        f"Restart Cursor, or turn {SERVER_NAME} off and on again under Cursor Settings > MCP.",
    ),
}


class ConfigError(ValueError):
    """The file cannot be merged safely; the text never quotes the file's content."""


def config_path(client: str, family: str, env: Mapping[str, str], home: str) -> str:
    """The client's own config file on this platform, for the user whose home this is; "" for
    a client whose file setup does not know."""
    flavor = ntpath if family == "windows" else posixpath
    if client == "claude-desktop":
        if family == "macos":
            folder = flavor.join(home, "Library", "Application Support")
        elif family == "windows":
            folder = env.get("APPDATA") or flavor.join(home, "AppData", "Roaming")
        else:
            # Claude Desktop has no official Linux build; community builds use this path.
            config = env.get("XDG_CONFIG_HOME", "")
            folder = config if config.startswith("/") else flavor.join(home, ".config")
        return flavor.join(folder, "Claude", "claude_desktop_config.json")
    if client == "claude-code":
        # User scope: every project sees it. CLAUDE_CONFIG_DIR moves the file.
        return flavor.join(env.get("CLAUDE_CONFIG_DIR") or home, ".claude.json")
    if client in {"chatgpt", "codex"}:
        # The ChatGPT app and Codex share this file; CODEX_HOME moves it.
        return flavor.join(env.get("CODEX_HOME") or flavor.join(home, ".codex"), "config.toml")
    if client == "gemini":
        return flavor.join(home, ".gemini", "settings.json")
    if client == "cursor":
        return flavor.join(home, ".cursor", "mcp.json")
    if client == "grok-bot":
        return ""
    raise ValueError(f"unknown client {client}")


def server_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/mcp"


def connect_command(launcher: str, env_file: str) -> tuple[str, ...]:
    """The command a client starts: the service's own proton-mail-mcp, by absolute path, so a
    desktop app's short PATH never matters, with the settings file it reads named."""
    return (launcher, "connect", "--env-file", env_file)


def entry(client: str, port: int, token: str = "", command: tuple[str, ...] = ()) -> dict[str, Any]:
    """This server's entry in the client's config, in the shape that client's page gives."""
    if CLIENTS[client].http:
        return {"url": server_url(port), "http_headers": {"Authorization": f"Bearer {token}"}}
    program, *args = command
    if client == "claude-code":
        return {"type": "stdio", "command": program, "args": args, "env": {}}
    return {"command": program, "args": args}


def same(found: Any, wanted: Mapping[str, Any]) -> bool:
    """Whether an entry already says what this one does. Keys it has besides, such as a
    timeout a person added or the empty fields a client's own `mcp add` writes, are kept."""
    if not isinstance(found, dict):
        return False
    return all(found.get(key) == value for key, value in wanted.items())


def servers_in(text: str | None, form: str) -> dict[str, Any]:
    """The servers a config holds, by name; nothing else of the file is kept."""
    if text is None or not text.strip():
        return {}
    data = _load(text, form)
    servers = data.get(_KEY[form], {})
    if not isinstance(servers, dict):
        raise ConfigError(f"has an {_KEY[form]} value that is not a {_TABLE[form]}")
    return servers


_KEY = {"json": "mcpServers", "toml": "mcp_servers"}
_TABLE = {"json": "JSON object", "toml": "table"}


def _load(text: str, form: str) -> dict[str, Any]:
    try:
        data = json.loads(text) if form == "json" else tomllib.loads(text)
    except ValueError:
        raise ConfigError(f"is not valid {form.upper()}") from None
    if not isinstance(data, dict):
        raise ConfigError("does not hold a JSON object")
    return data


def merged(text: str | None, server: Any, form: str = "json") -> str | None:
    """The file's new text, with server as this entry and every other key kept in place, in the
    file's own line endings; None when the entry already says the same, so an unchanged file is
    not touched."""
    if form == "toml":
        return _merged_toml(text or "", server)
    data: Any = _load(text, "json") if text is not None and text.strip() else {}
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ConfigError("has an mcpServers value that is not a JSON object")
    if same(servers.get(SERVER_NAME), server):
        return None
    servers[SERVER_NAME] = server
    try:
        # A number too large for a float was read as infinity, which JSON cannot hold.
        new = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    except ValueError:
        raise ConfigError("holds a number too large to write back unchanged") from None
    return new.replace("\n", _newline(text or ""))


def _newline(text: str) -> str:
    """The file's line ending, by its first line; JSON and TOML strings never hold a raw one."""
    first = text.find("\n")
    return "\r\n" if first > 0 and text[first - 1] == "\r" else "\n"


# A table header for this server or one of its subtables, such as its http_headers.
_OWN_TABLE = re.compile(
    r"\s*\[\s*mcp_servers\s*\.\s*(?:proton-mail|\"proton-mail\"|'proton-mail')\s*"
    r"(?:\.[^\]]*)?\]\s*(?:#.*)?"
)
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _merged_toml(text: str, server: dict[str, Any]) -> str | None:
    """Python reads TOML but cannot write it, so the entry's own tables are cut out as lines,
    and the new one goes where the first of them was, or at the end. Comments and blank lines
    that end one of those tables stay: they are usually about what follows. The result must then
    parse to exactly the old data with only this entry changed; anything else, such as an entry
    written as dotted keys, is refused."""
    data = _load(text, "toml") if text.strip() else {}
    servers = data.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ConfigError("has an mcp_servers value that is not a table")
    if same(servers.get(SERVER_NAME), server):
        return None
    # Split on "\n" alone, so each line keeps a "\r" it had, and a kept line is kept exactly.
    cr = "\r" if _newline(text) == "\r\n" else ""
    table = [line + cr for line in toml_table(server).splitlines()]
    kept: list[str] = []
    own: list[str] | None = None
    placed = False
    for line in text.split("\n") if text else []:
        if line.lstrip().startswith("["):
            if own is not None:
                kept += _closing_notes(own)
            own = None
            if _OWN_TABLE.fullmatch(line):
                own = []
                if not placed:
                    kept += table
                    placed = True
                continue
        if own is None:
            kept.append(line)
        else:
            own.append(line)
    if own is not None:
        kept += _closing_notes(own)
    if not placed:
        body = "\n".join(kept).rstrip()
        kept = ([body + cr, cr] if body else []) + table
    new = "\n".join(kept)
    if not new.endswith("\n"):
        # The last line has no ending yet. A table line has its "\r" already, and a line of the
        # file has none: TOML allows no bare "\r".
        new += "\n" if new.endswith("\r") else cr + "\n"
    expected = {**data, "mcp_servers": {**servers, SERVER_NAME: server}}
    try:
        proven = tomllib.loads(new) == expected
    except ValueError:
        proven = False
    if proven:
        return new
    if SERVER_NAME in servers:
        raise ConfigError(
            f"has a {SERVER_NAME} entry, or lines next to it, that the installer cannot edit "
            "safely (dotted keys or an inline table, for example)"
        )
    raise ConfigError(
        f"has no {SERVER_NAME} entry, and the installer cannot add one safely: mcp_servers may "
        "be an inline table, or a multi-line string may hold a line like a table header"
    )


def _closing_notes(lines: list[str]) -> list[str]:
    """The comments and blank lines a table ends with."""
    start = len(lines)
    while start and (not lines[start - 1].strip() or lines[start - 1].lstrip().startswith("#")):
        start -= 1
    return lines[start:]


def toml_table(server: Mapping[str, Any]) -> str:
    """The entry as Codex's [mcp_servers.proton-mail] table."""
    lines = [f"[mcp_servers.{SERVER_NAME}]"]
    lines += [f"{_toml_key(key)} = {_toml_value(value)}" for key, value in server.items()]
    return "\n".join(lines) + "\n"


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    # JSON's string escapes are all valid in a TOML basic string.
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = ", ".join(f"{_toml_key(key)} = {_toml_value(item)}" for key, item in value.items())
        return "{ " + pairs + " }" if pairs else "{}"
    raise TypeError(f"no TOML form for {type(value).__name__}")


def native_commands(
    client: str, program: str, command: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """The client's own commands that remove an earlier entry, add this one at user scope, and
    list what it has. Only Claude Code's `add` refuses a name it has, so the others have no
    remove: their `add` replaces the entry. `--` keeps connect's options away from the client's
    own parser."""
    if client == "claude-code":
        return (
            (program, "mcp", "remove", "--scope", "user", SERVER_NAME),
            (program, "mcp", "add", "--scope", "user", SERVER_NAME, "--", *command),
            (program, "mcp", "list"),
        )
    if client == "codex":
        return (
            (),
            (program, "mcp", "add", SERVER_NAME, "--", *command),
            (program, "mcp", "list"),
        )
    if client == "gemini":
        # Gemini CLI takes the program before `--`: what follows it only extends the arguments.
        launcher, *args = command
        return (
            (),
            (program, "mcp", "add", "--scope", "user", SERVER_NAME, launcher, "--", *args),
            (program, "mcp", "list"),
        )
    raise ValueError(f"{client} has no command line of its own")


def snippet(client: str, server: Mapping[str, Any]) -> str:
    """The entry as a person pastes it into this client's file."""
    if CLIENTS[client].form == "toml":
        return toml_table(server)
    return json.dumps({"mcpServers": {SERVER_NAME: server}}, indent=2) + "\n"


def cursor_link(server: Mapping[str, Any]) -> str:
    """Cursor's one-click install link for this entry."""
    config = base64.b64encode(json.dumps(server, separators=(",", ":")).encode()).decode()
    return (
        f"cursor://anysphere.cursor-deeplink/mcp/install?name={SERVER_NAME}&config={quote(config)}"
    )


def vscode_link(server: Mapping[str, Any]) -> str:
    """VS Code's install link for this entry: the entry, with its name, as URL-encoded JSON."""
    config = {"name": SERVER_NAME, "type": "stdio", **server}
    return "vscode:mcp/install?" + quote(json.dumps(config, separators=(",", ":")))
