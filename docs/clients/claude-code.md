# Claude Code

Claude Code gets `proton-mail-mcp connect` as a stdio server at user scope, so every project has
it. connect reads the bearer token itself when it starts, so `~/.claude.json` holds no token.

## The entry

The paths are the macOS ones; on Linux and Windows they are those of your own install, as
[The connect command](../clients.md#the-connect-command) lists:

```json
{
  "mcpServers": {
    "proton-mail": {
      "type": "stdio",
      "command": "/Users/you/Library/Application Support/proton-mail-mcp/venv/bin/proton-mail-mcp",
      "args": [
        "connect",
        "--env-file",
        "/Users/you/Library/Application Support/proton-mail-mcp/proton-mail-mcp.env"
      ],
      "env": {}
    }
  }
}
```

It goes in `~/.claude.json`, or `$CLAUDE_CONFIG_DIR/.claude.json`.

## Set it up

In your own terminal, in the clone:

```sh
uvx --from . proton-mail-mcp setup --client claude-code
```

After setup, `proton-mail-mcp configure-client --client claude-code` adds it on its own, with
your launcher in front.

When `claude` is on `PATH`, setup adds the entry with Claude Code's own command line, which knows
its file and its locking, and keeps the old file as a dated `.bak` first:

```sh
claude mcp remove --scope user proton-mail
claude mcp add --scope user proton-mail -- '/Users/you/Library/Application Support/proton-mail-mcp/venv/bin/proton-mail-mcp' connect --env-file '/Users/you/Library/Application Support/proton-mail-mcp/proton-mail-mcp.env'
claude mcp list
```

Claude Code's `add` refuses a name it has, so the first runs only when a user-scope
`proton-mail` entry is there; if `add` then fails, setup puts that entry back into the file. The
last should list `proton-mail` as connected; setup prints that line. Without `claude` on `PATH`,
setup merges the same entry into the file itself, as
[Let the installer write the config](../clients.md#let-the-installer-write-the-config) says. An
entry that already says the same is left alone.

Then start a new Claude Code session: exit, and run `claude` again.

## Try it

Ask: "How many unread messages are in my inbox?" Claude Code should call `count_mail`, which reads
and changes nothing.

## If it does not work

- **A project's own entry wins there.** An entry added in a project without `--scope user`, such as
  one from an earlier `claude mcp add --transport http`, is local scope, and Claude Code uses it in
  that project instead. Setup warns and names the project; in that folder run
  `claude mcp remove --scope local proton-mail`.
- [Troubleshooting](../clients.md#troubleshooting) covers a service that is not running and a
  refused token.

How this was checked: Claude Code 2.1.302 connected to this server over HTTP on 1 Nov 2026. The
connect entry and `claude mcp add` were checked by tests only, not yet with Claude Code.
