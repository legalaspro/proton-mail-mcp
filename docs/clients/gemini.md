# Gemini CLI

Gemini CLI gets `proton-mail-mcp connect` as a stdio server at user scope. connect reads the
bearer token itself when it starts, so `~/.gemini/settings.json` holds no token.

## The entry

The paths are the macOS ones; on Linux and Windows they are those of your own install, as
[The connect command](../clients.md#the-connect-command) lists:

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

It goes in `~/.gemini/settings.json` (on Windows `%USERPROFILE%\.gemini\settings.json`).

## Set it up

In your own terminal, in the clone:

```sh
uvx --from . proton-mail-mcp setup --client gemini
```

After setup, `proton-mail-mcp configure-client --client gemini` adds it on its own, with your
launcher in front.

When `gemini` is on `PATH`, setup adds the entry with Gemini CLI's own command line, and keeps the
old file as a dated `.bak` first:

```sh
gemini mcp add --scope user proton-mail '/Users/you/Library/Application Support/proton-mail-mcp/venv/bin/proton-mail-mcp' -- connect --env-file '/Users/you/Library/Application Support/proton-mail-mcp/proton-mail-mcp.env'
gemini mcp list
```

The program comes before `--`: Gemini CLI takes only what is before it as the command, and what
follows as its arguments. `add` replaces an entry of the same name, so nothing is removed first,
and a failed `add` leaves the old entry as it was. Setup prints the `proton-mail` line of
`gemini mcp list`. In a folder Gemini CLI does not trust, that line says `Disabled`: it turns MCP
servers off there, and starts this one in the folders you trust. Without `gemini` on `PATH`,
setup merges the entry into the file itself, as
[Let the installer write the config](../clients.md#let-the-installer-write-the-config) says. That
merge needs plain JSON: a settings file with comments is refused and left as it was, so add the
entry by hand or install Gemini CLI first.

Then start a new Gemini CLI session; its `/mcp` command should list `proton-mail`.

## Try it

Ask: "How many unread messages are in my inbox?" Gemini CLI should call `count_mail`, which reads
and changes nothing.

## If it does not work

[Troubleshooting](../clients.md#troubleshooting) covers a service that is not running and a
refused token.

How this was checked: on 2 Nov 2026 Gemini CLI 0.66.0, with a throwaway home folder, refused
the program after `--` (`Not enough non-option arguments`), took the command above, and replaced
the entry when run again. Talking to the server from a Gemini CLI session was not tried.
