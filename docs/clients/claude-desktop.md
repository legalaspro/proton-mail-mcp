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

In your own terminal:

```sh
uvx mcp-proton-mail setup --client claude-desktop
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

`make mcpb` builds `dist/proton-mail-mcp-<version>.mcpb` from a clone; it uploads nothing, and
`.gitignore` keeps the built file out of git. Open the file with Claude Desktop to install it. The
bundle is only the connector: the manifest, a launcher and the license, with no copy of the
package. Claude Desktop runs the manifest's command:

```sh
uvx mcp-proton-mail@<version> connect --env-file=
```

uv downloads that release from PyPI, where the public repository publishes it with Trusted
Publishing, and Python 3.12 when there is none. So uv must be installed, and setup must already
have run on the same computer: the bundle installs no service and asks for no password or token.
Use the bundle or the entry setup writes, not both, or Claude sees every tool twice.

**Why `uvx` under the `python` type.** MCPB 0.4 has a `uv` server type, where the host installs
the project from a `pyproject.toml` in the bundle. Smithery's bundle publisher accepts only the
`python`, `node` and `binary` types, so the manifest uses spec 0.3 and the `python` type, and its
command is `uvx` instead of a Python interpreter, so nothing needs bundling. The bundle then runs
the same published wheel as every other client, not a build of a copy of the source.
`compatibility` names no Python version, because the host's own Python is never used.

**Why it goes through `/bin/sh` on macOS.** An app started from the Dock gets only
`/usr/bin:/bin:/usr/sbin:/sbin`, which leaves out where `uvx` usually is: `~/.local/bin` from uv's
installer or pipx, `~/.cargo/bin`, `/opt/homebrew/bin` or `/usr/local/bin` from Homebrew, the mise
and asdf shims, and the nix profiles. The manifest cannot add to the PATH the app has, because an
`env` PATH would replace it and mcpb has no variable for the old one. So on macOS the command is
`/bin/sh -c`, which puts those folders in front of PATH, or of the system folders when PATH is
empty, and then runs `exec uvx` with the same arguments; connect inherits that PATH, so it also
finds a Homebrew `pass` or `gpg`. On Windows and Linux the app has the login's PATH.
`server/main.py`, the entry point the manifest declares, runs the same command for a host that
starts it with Python instead; it also puts `~/.local/bin` after PATH and passes that PATH on.

**The one setting.** *Settings file* is optional: the `proton-mail-mcp.env` that setup wrote, for
when connect's default path is not that file. Left empty, it gives `--env-file=`, which connect
reads as its default. The Bridge account, the port and the credential store come from that file.

How this was checked: `mcpb validate` passes; the mcpb library's own `getMcpConfigForManifest`
gives the command above on each system, and the `/bin/sh` one on macOS; the released package,
started by that command, and the launcher from an unpacked bundle with PATH set to `/usr/bin:/bin`,
both reached connect. Not yet installed in Claude Desktop, and not yet tried on a Mac, so it is
open whether Claude Desktop accepts a `python` bundle that names no runtime on a Mac without a
Python of its own.

## MCP Apps views

connect passes the server's `ui://` resources and each tool's `_meta` through unchanged, so the
views reach Claude Desktop as they reach a client over HTTP. Claude Desktop renders a view but
passes it only the result's text, without the view's data in `_meta`. The view then calls its own
tool once more through Claude Desktop to read the whole result, so it should load; this is not yet
confirmed in a live Claude Desktop session. Through `mcp-remote`, a live test showed no views. See
[MCP Apps views](../mcp-apps.md#how-a-view-works).

## If it does not work

- Claude Desktop's MCP logs hold connect's messages: on macOS in `~/Library/Logs/Claude/`.
- **"Mailbox authentication failed".** The Bridge password is missing, empty or wrong, and the
  error says which and the command to run. When it names `--replace-credentials`, run
  `uvx mcp-proton-mail setup --replace-credentials --client claude-desktop` in your own terminal
  (PowerShell on Windows) and type or paste the password Bridge generated at its hidden prompt,
  never into the chat. On Windows, [paste in Windows Terminal](../windows.md#the-bridge-password)
  with Ctrl+Shift+V, or type it.
- [Troubleshooting](../clients.md#troubleshooting) covers a service that is not running, a
  refused token and a missing Bridge password.

How this was checked: the entry by tests, and connect against the real server in tests. Not yet
run in Claude Desktop.
