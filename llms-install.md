# Installing proton-mail-mcp: a guide for AI agents

You are an AI agent installing proton-mail-mcp for a person on their own computer. You run the
installer; the person does the one thing only they can do, the Bridge login, in their own
terminal. Never ask the person for their Proton password, their Bridge password, a two-factor code
or the bearer token, and never put any of them in a command, a file or a message.

The installer prints one JSON document on stdout with `--json`. Read `result`, `next` and
`needs_user` in it; [Agents and scripts](docs/install.md#agents-and-scripts) describes every field.

## 1. What you need

- A paid Proton plan, which Proton Mail Bridge requires.
- Linux (Debian/Ubuntu, Fedora/RHEL or Arch), macOS or Windows. On macOS,
  [Homebrew](https://brew.sh) unless Bridge is already installed. On Linux, `secret-tool`
  (`libsecret-tools` on Debian and Ubuntu) with an unlocked keyring, or an initialised `pass`
  store. On Windows, PowerShell, where the person runs every command they are handed, and Windows
  Credential Manager, which holds the Bridge password and the token; native Windows support is new,
  see [Windows](docs/windows.md).
- Bridge's IMAP connection mode set to STARTTLS, its default: the server speaks only STARTTLS to
  Bridge. If the person changed it to SSL, they set it back in the Bridge app's settings, under
  Connection mode.
- [uv](https://docs.astral.sh/uv/).

The package is `mcp-proton-mail` on PyPI, so there is nothing to clone: run every command below
with `uvx mcp-proton-mail` as the launcher. For a wheel the person gives you, use
`uvx --from <wheel> proton-mail-mcp` in its place.

## 2. Plan

```sh
uvx mcp-proton-mail setup --dry-run --json --client <client>
```

`<client>` is one of `claude-desktop`, `claude-code`, `chatgpt`, `codex`, `gemini`, `cursor` or
`grok-bot`, repeatable; see [Connect an MCP client](docs/clients.md). Nothing changes. Tell the
person what `needs_user` says they will do. Each entry has `id`, `text`, `when` (`before`,
`during` or `after`), `blocking`, `stops_run` (it would stop the same command run with `--yes`),
`needs_terminal`, and for those the `command` the person runs.

If `quit_bridge_app` is listed, ask the person to quit the Proton Mail Bridge app and turn off its
start-at-login setting, or rerun with `--skip-bridge-service` to keep the app running Bridge. If
`store_tools` or `secret_service` is listed, its `text` says what to install or where to run. If
`sudo` is listed, installing Bridge needs the person's sudo password, which only their own
terminal may take: hand it over as in step 3.

## 3. The human login handoff

If `bridge_login`, `bridge_password` or `sudo` is listed, stop and hand the person its `command`
exactly as given, to run in their own terminal; it is the same command for each. Do not run it
yourself, and do not run it through a pseudo-terminal or pipe of yours.

Tell them what happens there:

- Setup opens Bridge's own CLI. They type `login`, their Proton email and password (pasting
  works), and the two-factor code if asked.
- If Bridge prints a verification link, they open it in any browser, finish the check, then press
  Enter in the terminal.
- Setup reads the IMAP username from Bridge, then asks for the Bridge password at a hidden prompt:
  the one Bridge's `info` just showed, not their Proton password. In classic PowerShell or cmd,
  pasting at a hidden prompt often doesn't work: they use Windows Terminal and paste with
  Ctrl+Shift+V, or type the password. Setup refuses an empty or too short entry and asks again,
  three times in all.
- On macOS, Keychain may ask for access: choose Always Allow.
- It shows its plan and asks before applying it; then it does the rest, the install included
  (Bridge's package, the server's environment, the services), so there is no separate install
  for you to run first.

Everything they type goes into Bridge's prompts or the installer's hidden prompt in their terminal,
never through you. Wait until they say it finished.

## 4. Apply and verify

```sh
uvx mcp-proton-mail setup --yes --json --client <client>
```

After the person's run, this finds everything in place, and repeating it is safe. Expect
`result` `done`. If it is `needs_user`, go back to step 3 with the entries whose `stops_run` is
true; if `failed` or `refused`, `error` says why and what to do. A client config the installer
cannot edit safely refuses the whole plan before any step runs, with `--dry-run` as with `--yes`.

`uvx mcp-proton-mail configure-client --yes --json --client <client>` adds another client later.
Report the document's `connector` to the person: the command and arguments any MCP client on this
computer can start, and the server's URL. It holds no token, and you never need one.

Last, relay each `needs_user` entry whose `when` is `after`, such as restarting the client, and
suggest a first read-only question: "How many unread messages are in my inbox?"

## Mailbox authentication failed

A tool error that says `Mailbox authentication failed` means the server has no Bridge password it
can use, or Bridge refused the login. Never ask the person for the password, never accept it in the
chat, and never put it in a command, a file or a message. The error says which case it is and the
command to hand over; hand over that command as it is:

- **No Bridge password could be read**: the person runs `uvx mcp-proton-mail setup` in their own
  terminal (PowerShell on Windows). It asks only for what is missing and keeps the token. If the
  store is locked, unlocking it is enough.
- **The stored Bridge password is empty**: with Credential Manager or the Linux keyring, the same
  `uvx mcp-proton-mail setup`. With the Keychain or `pass`, plain setup would keep the empty entry,
  so the error names
  `uvx mcp-proton-mail setup --replace-credentials --client <client>`.
- **Bridge rejected the Bridge username or password**: the person runs
  `uvx mcp-proton-mail setup --replace-credentials --client <client>` there, with the `<client>`
  you set up. It stores a new Bridge password and a new token, and gives that client the token. If
  setup was given another username than the one shown in Bridge's Mailbox details, they add
  `--bridge-user` with that one.

Tell them the password is the one Bridge generated, not their Proton password: in the Proton Mail
Bridge app they select their mailbox, open Mailbox details and copy the password under Password
(`info` in Bridge's CLI). They type it or paste it at the hidden prompt; on Windows, in Windows
Terminal with Ctrl+Shift+V, since pasting there often doesn't work in classic PowerShell or cmd. It
goes into Credential Manager on Windows, the Keychain on macOS or the Linux keyring. Wait until
they say it finished, then retry the read.

A tool error that says `Bridge STARTTLS failed` means STARTTLS failed on Bridge's IMAP port. A
Bridge set to SSL is not named: reads time out and nothing is logged. In either case, when Bridge
is running, the person checks that its connection mode is STARTTLS in the Bridge app's settings,
under Connection mode.

## Hosts without systemd

In a container or on a system without systemd, setup starts the MCP server and Bridge as
background processes and runs its self-check. They stop when the host restarts: run setup again
then. See [Hosts without systemd](docs/install.md#hosts-without-systemd).
