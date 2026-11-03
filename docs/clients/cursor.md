# Cursor

Cursor gets `proton-mail-mcp connect` as a stdio server. connect reads the bearer token itself
when it starts, so `~/.cursor/mcp.json` holds no token.

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

It goes in `~/.cursor/mcp.json` (on Windows `%USERPROFILE%\.cursor\mcp.json`).

## Set it up

In your own terminal:

```sh
uvx mcp-proton-mail setup --client cursor
```

After setup, `proton-mail-mcp configure-client --client cursor` adds it on its own, with your
launcher in front, as
[Let the installer write the config](../clients.md#let-the-installer-write-the-config) says.

Then restart Cursor, or turn `proton-mail` off and on again under Cursor Settings > MCP.

## One-click links

`proton-mail-mcp configure-client --client cursor --print-config` prints the entry with your own
paths and a `cursor://anysphere.cursor-deeplink/mcp/install?...` link that adds it to Cursor. It
reads no token and changes nothing.

These buttons do the same for setup's default paths, with Cursor's `${userHome}` for your home
folder. They work only after setup has run with its default paths, not with `--venv`, and they
have not yet been tried in either app. VS Code is not one of setup's clients; its button adds the
same stdio entry.

| | Cursor | VS Code |
| --- | --- | --- |
| macOS | [![Add to Cursor on macOS](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=proton-mail&config=eyJjb21tYW5kIjoiJHt1c2VySG9tZX0vTGlicmFyeS9BcHBsaWNhdGlvbiBTdXBwb3J0L3Byb3Rvbi1tYWlsLW1jcC92ZW52L2Jpbi9wcm90b24tbWFpbC1tY3AiLCJhcmdzIjpbImNvbm5lY3QiLCItLWVudi1maWxlIiwiJHt1c2VySG9tZX0vTGlicmFyeS9BcHBsaWNhdGlvbiBTdXBwb3J0L3Byb3Rvbi1tYWlsLW1jcC9wcm90b24tbWFpbC1tY3AuZW52Il19) | [![Install in VS Code on macOS](https://img.shields.io/badge/VS_Code-Install_Server-0098FF)](https://insiders.vscode.dev/redirect/mcp/install?name=proton-mail&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22%24%7BuserHome%7D%2FLibrary%2FApplication%20Support%2Fproton-mail-mcp%2Fvenv%2Fbin%2Fproton-mail-mcp%22%2C%22args%22%3A%5B%22connect%22%2C%22--env-file%22%2C%22%24%7BuserHome%7D%2FLibrary%2FApplication%20Support%2Fproton-mail-mcp%2Fproton-mail-mcp.env%22%5D%7D) |
| Linux | [![Add to Cursor on Linux](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=proton-mail&config=eyJjb21tYW5kIjoiJHt1c2VySG9tZX0vLmxvY2FsL3NoYXJlL3Byb3Rvbi1tYWlsLW1jcC92ZW52L2Jpbi9wcm90b24tbWFpbC1tY3AiLCJhcmdzIjpbImNvbm5lY3QiLCItLWVudi1maWxlIiwiJHt1c2VySG9tZX0vLmNvbmZpZy9wcm90b24tbWFpbC1tY3AvcHJvdG9uLW1haWwtbWNwLmVudiJdfQ%3D%3D) | [![Install in VS Code on Linux](https://img.shields.io/badge/VS_Code-Install_Server-0098FF)](https://insiders.vscode.dev/redirect/mcp/install?name=proton-mail&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22%24%7BuserHome%7D%2F.local%2Fshare%2Fproton-mail-mcp%2Fvenv%2Fbin%2Fproton-mail-mcp%22%2C%22args%22%3A%5B%22connect%22%2C%22--env-file%22%2C%22%24%7BuserHome%7D%2F.config%2Fproton-mail-mcp%2Fproton-mail-mcp.env%22%5D%7D) |

## Try it

In Agent mode, ask: "How many unread messages are in my inbox?" Cursor should call `count_mail`,
which reads and changes nothing.

## If it does not work

[Troubleshooting](../clients.md#troubleshooting) covers a service that is not running and a
refused token.

How this was checked: the entry and links by tests only; not yet run in Cursor or VS Code.
