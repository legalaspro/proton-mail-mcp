# Claude Desktop

Claude Desktop starts local MCP servers as commands. Setup gives it `proton-mail-mcp connect`,
which reads the bearer token itself each time Claude Desktop starts it. The config holds no
token, and no Node.js or `mcp-remote` is needed.

## The entry

Setup adds this under `mcpServers`. The paths are the macOS ones; on Linux and Windows they are
those of your own install, as [The connect command](../clients.md#the-connect-command) lists:

```json
{
  "mcpServers": {
    "proton-mail": {
      "command": "/Users/you/Library/Application Support/proton-mail-mcp/venv/bin/proton-mail-mcp",
      "args": [
        "connect",
        "--env-file",
        "/Users/you/Library/Application Support/proton-mail-mcp/proton-mail-mcp.env"
      ]
    }
  }
}
```

The command is an absolute path, so the short `PATH` a desktop app gets does not matter.

| System | File |
| --- | --- |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux, community builds | `~/.config/Claude/claude_desktop_config.json`, or under `$XDG_CONFIG_HOME` |

## Set it up

In your own terminal, in the clone:

```sh
uvx --from . proton-mail-mcp setup --client claude-desktop
```

After setup, `proton-mail-mcp configure-client --client claude-desktop` adds it on its own, with
your launcher in front. Either one keeps every other server in the file and the old file as a
dated `.bak`, as [Let the installer write the config](../clients.md#let-the-installer-write-the-config)
says. An earlier `npx mcp-remote` entry for `proton-mail` is replaced.

Then quit Claude Desktop completely (on macOS Claude > Quit Claude, on Windows Quit from the tray
icon; closing the window is not enough) and open it again. On macOS, Keychain may ask whether
`security` may read the token the first time Claude Desktop starts connect: choose Always Allow.

To add it by hand, open **Settings**, then **Developer**, then **Edit Config**, and paste the
entry that `proton-mail-mcp configure-client --client claude-desktop --print-config` prints with
your own paths.

## Try it

Ask: "How many unread messages are in my inbox?" The assistant should call `count_mail`, which
reads and changes nothing. Claude Desktop asks before it allows each tool the first time.

## The .mcpb bundle

`make mcpb` builds `dist/proton-mail-mcp-<version>.mcpb` from a clone. The bundle is only the
connector: Claude Desktop runs `proton-mail-mcp connect` from it with `uv run`, so uv must be
installed, and setup must already have run on the same computer. It is not published anywhere,
and it has not yet been tried in Claude Desktop.

## MCP Apps views

connect passes the server's `ui://` resources and each tool's `_meta` through unchanged, so the
views reach Claude Desktop as they reach a client over HTTP. Whether Claude Desktop renders them
for a local server has not been checked yet: through `mcp-remote`, a live test showed none. See
[MCP Apps views](../mcp-apps.md).

## If it does not work

- Claude Desktop's MCP logs hold connect's messages: on macOS in `~/Library/Logs/Claude/`.
- [Troubleshooting](../clients.md#troubleshooting) covers a service that is not running and a
  refused token.

How this was checked: the entry by tests, and connect against the real server in tests. Not yet
run in Claude Desktop.
