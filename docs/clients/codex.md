# Codex

Codex (the CLI, IDE extension and app) gets `proton-mail-mcp connect` as a stdio server. connect
reads the bearer token itself when it starts, so `~/.codex/config.toml` holds no token.

## The entry

The paths are the macOS ones; on Linux and Windows they are those of your own install, as
[The connect command](../clients.md#the-connect-command) lists:

```toml
[mcp_servers.proton-mail]
command = "/Users/you/Library/Application Support/proton-mail-mcp/venv/bin/proton-mail-mcp"
args = ["connect", "--env-file", "/Users/you/Library/Application Support/proton-mail-mcp/proton-mail-mcp.env"]
```

It goes in `~/.codex/config.toml` (on Windows `%USERPROFILE%\.codex\config.toml`), or in
`config.toml` under `$CODEX_HOME`.

## Set it up

In your own terminal, in the clone:

```sh
uvx --from . proton-mail-mcp setup --client codex
```

After setup, `proton-mail-mcp configure-client --client codex` adds it on its own, with your
launcher in front.

When `codex` is on `PATH`, setup adds the entry with Codex's own command line, and keeps the old
file as a dated `.bak` first:

```sh
codex mcp add proton-mail -- '/Users/you/Library/Application Support/proton-mail-mcp/venv/bin/proton-mail-mcp' connect --env-file '/Users/you/Library/Application Support/proton-mail-mcp/proton-mail-mcp.env'
codex mcp list
```

`add` replaces an entry of the same name, so nothing is removed first; setup prints the
`proton-mail` line of the last. Without `codex` on `PATH`, setup merges the table into the file
itself: every other table and key stays, as do all comments except those inside the replaced
table. A file that defines `proton-mail` with dotted keys or an inline table is refused and left
as it was, by `--dry-run` too.

The ChatGPT Mac app reads the same file. With `--client chatgpt --client codex`, setup writes only
[ChatGPT's](chatgpt.md) direct HTTP entry, and Codex uses that one. A later `--client codex` keeps
ChatGPT's entry too, as replacing it would cut the ChatGPT app off; it warns when that entry
points at another port, and `configure-client --client chatgpt` updates it for both. To have Codex
start connect instead, remove that entry first (`codex mcp remove proton-mail`); ChatGPT then has
none.

Then start a new Codex session; `codex mcp list` should show `proton-mail`.

Codex's default tool timeout is 60 seconds, shorter than the server's call budget, so add
`tool_timeout_sec = 150` to the table by hand (see
[Client timeouts](../clients.md#client-timeouts)). A later `configure-client` keeps it while the
rest of the entry is unchanged; one that replaces the entry keeps the old table in the dated
backup, so add it again then.

## Try it

Ask: "How many unread messages are in my inbox?" Codex should call `count_mail`, which reads and
changes nothing.

## If it does not work

[Troubleshooting](../clients.md#troubleshooting) covers a service that is not running and a
refused token.

How this was checked: Codex CLI 0.162.1 accepted an HTTP entry for this server on 1 Nov 2026. On
2 Nov 2026, with a throwaway `CODEX_HOME`, its `mcp add` wrote the connect entry and replaced an
existing `proton-mail` entry, an HTTP one included. Talking to the server through connect from a
Codex session was not tried.
