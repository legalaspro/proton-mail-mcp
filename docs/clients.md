# Connect an MCP client

Setup adds the server to seven clients itself: name them with `--client` (repeatable). Every one
but ChatGPT starts [`proton-mail-mcp connect`](#the-connect-command), a local stdio server that
reads the bearer token when it starts, so its config holds no token. ChatGPT takes the server
directly over HTTP, with the token in its config. Any other client connects
[over HTTP](#other-clients-mcp-over-http).

| `--client` | How it connects | Its config |
| --- | --- | --- |
| [`claude-desktop`](clients/claude-desktop.md) | connect | `claude_desktop_config.json` |
| [`claude-code`](clients/claude-code.md) | connect, added with `claude mcp add` when `claude` is on `PATH` | `~/.claude.json` |
| [`chatgpt`](clients/chatgpt.md) | HTTP, with the token | `~/.codex/config.toml`, shared with Codex |
| [`codex`](clients/codex.md) | connect, added with `codex mcp add` when `codex` is on `PATH` | `~/.codex/config.toml` |
| [`gemini`](clients/gemini.md) | connect, added with `gemini mcp add` when `gemini` is on `PATH` | `~/.gemini/settings.json` |
| [`cursor`](clients/cursor.md) | connect | `~/.cursor/mcp.json` |
| [`grok-bot`](clients/grok-bot.md) | connect; no file, the entry is shown for the agent to add | The agent's own |

Each client's page has its entry, the commands, a first question to try and what was checked.
Vendors change their settings often, so trust their current docs if they differ.

## The connect command

A client that starts local servers as commands runs the service environment's own
`proton-mail-mcp`, by absolute path, with `connect` and the settings file setup wrote:

| System | Command | Settings file |
| --- | --- | --- |
| Linux | `~/.local/share/proton-mail-mcp/venv/bin/proton-mail-mcp` | `~/.config/proton-mail-mcp/proton-mail-mcp.env` |
| macOS | `~/Library/Application Support/proton-mail-mcp/venv/bin/proton-mail-mcp` | `~/Library/Application Support/proton-mail-mcp/proton-mail-mcp.env` |
| Windows | `%LOCALAPPDATA%\proton-mail-mcp\venv\Scripts\proton-mail-mcp.exe` | `%LOCALAPPDATA%\proton-mail-mcp\proton-mail-mcp.env` |

The config names both with your home folder in full, and the environment from `--venv` if you gave
one. An absolute path means a desktop app's short `PATH` does not matter.

```sh
proton-mail-mcp connect [--env-file PATH] [--port PORT] [--token-file PATH]
```

connect relays every MCP message between the client, on stdin and stdout, and the service at
`http://127.0.0.1:PORT/mcp`, unchanged: client capabilities, `_meta` and the `ui://` resources of
[MCP Apps views](mcp-apps.md) pass through as they are. It adds the bearer token to each request.

- **The token** comes from `PROTON_MCP_TOKEN` in connect's environment if that is set, else from
  `--token-file` (a file only you can read), else from the credential store the settings file
  names, read each time connect starts. connect never prints it, logs it or puts it in a message.
- **The port** comes from `--port`, else `PROTON_MCP_PORT` in the settings file, else 8765.
  `--env-file` defaults to the settings file for this computer.
- **Errors** reach the client as JSON-RPC errors, with one line on stderr, which clients keep in
  their logs: the service is not running, the token was refused, or there is no token to read. A
  refused token is read again once, and a service that restarted gets the session set up again,
  before connect gives up on a request.

## Let the installer write the config

`setup --client <name>` adds each client at the end of a setup. Afterwards,
`configure-client` does the same on its own, with the launcher you use in front:

```sh
proton-mail-mcp configure-client --client claude-desktop   # repeatable
```

It reads the port from the settings `setup` wrote (`--port` overrides it) and adds one
`proton-mail` entry, in the shape each client's page gives:

- Every other key and server in the file stays, and so do its byte-order mark and line endings.
  In TOML, comments stay too, except those inside the replaced `proton-mail` table: comments and
  blank lines at its end, such as a note above the next table, stay where they are.
- Before a change, the old file is kept beside it as `<name>.<YYYYMMDD-HHMMSS>.bak`, owner-only;
  a backup never replaces an earlier one. A ChatGPT entry, and its backups, may hold a token.
- The file is written owner-only (mode `0600`).
- An entry that already says the same leaves the file untouched, keys added to it by hand
  included. A different `proton-mail` entry is replaced, and the run says so; anything added to
  it by hand is then only in the backup.
- A file that is not valid JSON or TOML, is a symbolic link, or holds the entry, or the
  `mcp_servers` table it goes in, in a form it cannot edit safely is refused and left as it
  was. The plan checks this before any step runs, so such a file stops `--dry-run` and `--yes`
  alike, with `result` `refused`, and nothing changes.
- `--dry-run` shows what would change without any value already in the file: the new entry,
  with a placeholder for a token, whether an entry is replaced, and the names of the servers
  kept.
- Claude Code, Codex and Gemini CLI get the entry through their own `mcp add` when their command
  is on `PATH`; each one's page has the commands. A failed `mcp add` never loses an entry that
  was there.
- ChatGPT and Codex share `~/.codex/config.toml` and its one `proton-mail` table. Codex keeps
  ChatGPT's direct HTTP entry, which it uses too, and warns when that entry points elsewhere.
  ChatGPT replaces a Codex entry with its own and warns, as the file then holds the token.
- The token is read only for ChatGPT, only when applied, and is never printed.

`--print-config` prints each client's entry with this computer's paths, its own `mcp add`
command, and for Cursor a one-click link, and changes nothing. It reads no token: ChatGPT's entry
shows a placeholder. `setup --print-config` with no `--client` prints all seven but Codex: with
ChatGPT it shares one table, so only ChatGPT's is printed, and a note says so (`--client codex`
alone prints Codex's). With `--json`, the document's `result` is `printed`, `configs` lists each
client's `path`, `format`, `snippet` and, where there is one, `command` and `link`, and `notes`
holds such notes.

## Other clients: MCP over HTTP

The server speaks MCP over Streamable HTTP:

| Setting | Value |
| --- | --- |
| URL | `http://127.0.0.1:8765/mcp` |
| Header | `Authorization: Bearer <token>`, on every request |
| Session | Stateful with SSE: initialize a session first. Only with both `PROTON_MCP_ENABLE_SEND` and `PROTON_MCP_CONFIRM_MUTATIONS` at `0` is it stateless |

A client that can start a local command is better served by [connect](#the-connect-command),
which keeps the token out of its config. Otherwise, at a terminal,
`proton-mail-mcp show-token --copy` puts the token on the clipboard without showing it. Keep it in
your client's secret storage, and out of version control and logs. A missing or wrong token
returns HTTP `401`.

The client must run where it can reach that address, which means on the same computer as the
server. To reach the server from another device on your tailnet, use the HTTPS address from
[Tailscale Serve](install.md#expose-through-tailscale-serve) as the URL; the header stays the
same. A client that runs in someone else's cloud cannot reach `127.0.0.1` on your computer.

To check the URL and token without any client, send `initialize` with `curl`. A working server
answers `200` with `serverInfo`; a wrong token gets `401`. `read -rs` takes the token at a hidden
prompt (paste it after `show-token --copy`), and `-H @-` gives it to curl on stdin, so it never
appears in the process list or the shell history:

```sh
read -rs TOKEN
printf 'Authorization: Bearer %s\n' "$TOKEN" | curl -sS -i http://127.0.0.1:8765/mcp \
  -H @- \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl-check","version":"1"}}}'
```

The server keeps the sessions that forms need whenever sending or confirmation is on, which is
the default, so a client must initialize a session and read Server-Sent Events.

## Troubleshooting

- **"proton-mail-mcp is not running on 127.0.0.1:8765".** The service is down. On Linux,
  `systemctl --user status proton-mail-mcp` says why; on a host without systemd, rerun setup,
  which starts it again ([Hosts without systemd](install.md#hosts-without-systemd)). A rerun of
  setup also runs the self-check, which names the cause.
- **"the proton-mail-mcp service refused the bearer token".** The running service uses another
  token than the one connect read: restart the service, or rerun setup. With `--token-file`, the
  file most likely holds an older token; delete it and drop `--token-file`. Setup warns about a
  `token` file left from an earlier manual setup in the settings folder.
- **No token to read.** The credential store is locked or holds none: unlock it, or run setup
  first. On macOS, choose Always Allow when Keychain asks whether `security` may read it. On Linux
  the client must reach the same Secret Service as setup did.
- **"Mailbox authentication failed".** The service has no Bridge password it can use, or Bridge
  refused the login, and the error says which case and the command to run in your own terminal
  (PowerShell on Windows). No Bridge password could be read: run `uvx mcp-proton-mail setup`,
  which keeps the token. The stored one is empty: in Credential Manager or the Linux keyring,
  plain `setup` asks again; in the Keychain or `pass` it would keep the empty entry, so the error
  names
  `uvx mcp-proton-mail setup --replace-credentials --client <client>`. Bridge rejected the Bridge
  username or password: run that command too, which also stores a new token and gives it to that
  client, adding `--bridge-user` if setup was given another username than the one in Bridge's
  Mailbox details. It is the password Bridge generated, not your Proton password: in the Bridge
  app, select your mailbox, open Mailbox details and copy it under Password. Never paste it into a
  chat.
- **"Bridge STARTTLS failed".** Something answers on Bridge's IMAP port, but STARTTLS did not
  complete. The server speaks only STARTTLS to Bridge: in the Bridge app's settings, under
  Connection mode, choose STARTTLS for IMAP.
- **Searches come back incomplete after a first login.** Bridge is still downloading the mailbox;
  the index catches up by itself ([`bridge_syncing`](search-index.md#automatic-startup-recovery)).

## Forms and typed OK

What a client declares in `initialize` decides how you approve changes:

| Your client | Mailbox changes | Sends, when enabled |
| --- | --- | --- |
| Declares `elicitation` and shows forms | Form | Form |
| Omits `elicitation` | Refused: `this client did not show a form` (audit outcome `confirmation_unavailable`) | [Typed OK](sending.md#typed-ok-when-the-client-has-no-form), unless `PROTON_MCP_SEND_TYPED_OK=0` refuses it |
| Declares `elicitation` but never shows the form | Refused | Refused; the operator can list it in `PROTON_MCP_SEND_TYPED_OK_CLIENTS` to get the typed OK |
| Listed in `PROTON_MCP_SEND_TYPED_OK_CLIENTS`, with the typed OK on | Refused at once | Typed OK |

A host that answers forms automatically defeats this check and is unsuitable. To let mailbox
changes run on a host without forms, the operator can deliberately set
[`PROTON_MCP_CONFIRM_MUTATIONS=0`](safety.md#confirmation-and-approval-binding); the token, which
passes through the assistant, then authorizes the change by itself.

To see what your client declares, read the service log after its first tool call. It records, at
INFO, a line such as:

```text
MCP client name "example-client", version "1.0", elicitation declared: True
```

`PROTON_MCP_SEND_TYPED_OK_CLIENTS` matches that name. It is self-reported and unauthenticated.

## Client timeouts

Keep your client's tool timeout above the server's budget. The server's default call budget is
120 seconds, and `undo_action` can use all of it, so use at least 150 seconds for that default.
A confirmation form may also stay unanswered for up to `PROTON_MCP_CONFIRM_TIMEOUT` (120 seconds
by default). If a client gives up first, the call may still be running: use `list_actions` to find
out what happened before you retry. See
[Call budgets](configuration.md#call-budgets-and-token-lifetimes) and
[Recovery](RECOVERY.md).
