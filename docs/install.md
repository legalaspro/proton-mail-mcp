# Install on Linux and macOS

The guided installer, then the [manual steps for a Mac](#install-and-run-on-your-mac),
[running at login with launchd](#run-at-login-with-launchd) and
[exposing the server through Tailscale Serve](#expose-through-tailscale-serve). Windows has its
own [page](windows.md).

## Quick start

[uv](https://docs.astral.sh/uv/) installs the package from
[PyPI](https://pypi.org/project/mcp-proton-mail/) in one command. uv finds or fetches a compatible
Python; Debian and Ubuntu may also need `python3-venv`, as below:

```sh
uvx mcp-proton-mail setup --dry-run
uvx mcp-proton-mail setup --client claude-desktop
# or, with uv's own Python: uvx --managed-python mcp-proton-mail setup
```

Or with pipx:

```sh
pipx install mcp-proton-mail
proton-mail-mcp setup
```

The package is named `mcp-proton-mail`, because PyPI refuses `proton-mail-mcp`. It installs one
command under two names: `proton-mail-mcp`, which these pages use, and `mcp-proton-mail`, which
`uvx mcp-proton-mail` runs.

`setup` first lists what it will do, then finds your Bridge username, prints its full plan and
asks before it changes anything (add `--dry-run` to stop after the plan). Where Bridge is
installed but no username is known yet, it offers Bridge's CLI first and reads the username from
it ([Bridge login](#bridge-login)); otherwise it asks. Then it:

1. checks for Bridge, and installs it from official sources if it is missing;
2. installs proton-mail-mcp into its own private virtual environment, so the service
   keeps running after `pipx uninstall` or `uv cache clean`;
3. offers to open Bridge's CLI on your terminal, where you log in, unless a Bridge password is
   stored already or you just logged in;
4. asks for the **Bridge** password (the one Bridge's Mailbox details and `info` show, not your
   Proton password)
   with a hidden prompt, or reads it from stdin with `--password-stdin`, and stores it and a new
   bearer token in the operating system's credential store;
5. writes and starts per-user services for Bridge and the MCP server, which binds only
   to `127.0.0.1`;
6. runs a self-check: the server must refuse a request without the token, accept the
   stored one, and read `INBOX` through Bridge with the stored password. A failure
   exits with status 1 and says what to fix: for a Bridge password problem or a failed
   STARTTLS, the same text a client would get from the server, and for a Bridge that is not
   running, `Bridge not running: nothing answers on 127.0.0.1:1143` with the address the server
   used. Rerunning is safe;
7. with `--client` (repeatable: `claude-desktop`, `claude-code`, `chatgpt`, `codex`, `gemini`,
   `cursor` or `grok-bot`), adds the server to that client, as
   [Connect an MCP client](clients.md#let-the-installer-write-the-config) describes, and says
   how to restart the client. Every client but ChatGPT starts
   [`proton-mail-mcp connect`](clients.md#the-connect-command), which reads the token itself.

Without `--client`, `configure-client --client <name>` adds a client later. The token is never
shown: [Copy the token](#copy-the-token) says how to get it otherwise.

### Before you start: a running Bridge app

`setup` runs Bridge as a background service, which cannot run beside the Bridge desktop app. If it
finds the app's window process (`bridge-gui`), or something listening on Bridge's IMAP port while
its own Bridge service is not running, it stops before changing anything. On a terminal it waits:
quit the app (in its menu, Quit Bridge), turn off its start-at-login setting and press Enter, or
type `k` to keep the app running Bridge, as `--skip-bridge-service` does. The app's login and
Bridge password carry over to the service, so copy the password from the app first if it is
handy. The installer never quits the app itself.

Python 3.12 or newer is required: `uvx` finds or fetches one, and `pipx` uses the one
it runs on. On Debian and Ubuntu, `setup` needs `python3-venv` and stops before changing
anything if it is missing: run `sudo apt-get install python3-venv`, or add
`--managed-python` after `uvx` to use uv's own Python.

### Commands

| Command | What it does |
| --- | --- |
| `proton-mail-mcp` or `proton-mail-mcp serve` | Start the server from `PROTON_*` settings in the foreground, as before. Configure Bridge and authentication first. |
| `proton-mail-mcp setup` | The guided install above. `--print-config` prints each `--client`'s entry and changes nothing. |
| `proton-mail-mcp configure-client --client <name>` | Add the server to a client: `claude-desktop`, `claude-code`, `chatgpt`, `codex`, `gemini`, `cursor` or `grok-bot`. `--print-config` prints the entry instead. |
| `proton-mail-mcp connect` | The [stdio server](clients.md#the-connect-command) a client starts: it relays to the service and reads the token itself. |
| `proton-mail-mcp introspect` | List the tools on stdio with no Bridge, settings or credentials, for registries such as Glama that check a server in a container; every tool call refuses. The repository's `Dockerfile` runs it. |
| `proton-mail-mcp uninstall` | [Stop and remove](#uninstall) the services and settings. |
| `proton-mail-mcp show-token` | Print the stored bearer token, to a terminal only; `--copy` puts it on the clipboard instead, also only at a terminal. |
| `proton-mail-mcp detect` | Show the detected platform, credential store and paths. `--json` for scripts. |
| `proton-mail-mcp --version` | Print the installed version. |

Services do not use this command: they keep running
`proton-mail-mcp-service --env-file <settings>` from the private environment.

With `uvx`, put `uvx mcp-proton-mail` in place of `proton-mail-mcp`, as in
`uvx mcp-proton-mail configure-client --client cursor`. `setup` also leaves
`~/.local/share/proton-mail-mcp/venv/bin/proton-mail-mcp` (on macOS under
`~/Library/Application Support/proton-mail-mcp/venv/bin/`), which works without `uvx`.

The files in this repository's `examples/` directory, such as the optional units for
the rules timer, ship inside the package at
`<environment>/lib/python3.X/site-packages/proton_mail_mcp/examples/`.

### Where the service's copy comes from

The private environment gets the same copy that ran `setup`:

- installed from a local wheel or directory: that same path, and never an index, which
  could hold something else under this name. A wheel is reinstalled on every run, so a
  rebuild of the same version takes effect. If the path is gone, or the copy came from
  any other URL, `setup` stops and asks for `--source <checkout or wheel file>`;
- installed by name: that version, from a wheel on an index (`mcp-proton-mail==<version>`),
  and then a comparison of what it installed with the running copy;
- run from a checkout: that checkout.

An install by name leaves no record of its index in the package itself. pipx and `uv tool`
keep their own, so `setup` passes on the `--index-url`, `--extra-index-url`, `--find-links`
and `--no-index` they used (and pipx's `--trusted-host`), and pip's own `pip.conf` and
`PIP_*` settings apply as usual. If that record cannot be read, or holds a URL with a user
name, password or query (the plan would print it) or a relative path, `setup` stops before
changing anything and asks for `--source`.

Plain `pip` and `uvx` keep no record, so the new pip looks on its own configured index,
which may not be where this copy came from. If it finds nothing, or anything but this
exact copy, `setup` stops before starting or restarting any service. By then Bridge may
have been installed in this run, and the private environment holds what pip put there. A
rerun with `--source <wheel>` replaces it; a rerun by name does not. For a private index or
a wheel directory, pass the wheel instead:

```sh
pip download --no-deps --index-url <your index> mcp-proton-mail==<version>
proton-mail-mcp setup --source ./mcp_proton_mail-<version>-py3-none-any.whl
```

### The service's Python

The private environment is made from the base Python of the run, never from the temporary
environment `uvx` or pipx made around it. uv keeps those environments in its cache, which
`uv cache clean` empties: a Python found there is followed out of the cache through its links, to
the first path outside it, such as uv's own install under `~/.local/share/uv/python/`. A stable
name such as `/opt/homebrew/bin/python3.12` is kept as it is, not resolved to a versioned path an
upgrade removes. If no link leads out of the cache, the plan stops before anything changes and asks
for `--python` with a Python that stays, for example the path `uv python find 3.12` prints. A rerun
whose existing environment was made from a Python in uv's cache says so, and how to rebuild it.

### Agents and scripts

An AI agent can drive setup and hand over only what needs a person;
[llms-install.md](../llms-install.md) is its step-by-step guide. `--json` asks nothing, opens no
prompt and prints one JSON document on stdout, with the usual progress on stderr; it needs
`--dry-run`, `--yes` or `--print-config`. `--non-interactive` does the same with the usual output.
With `uvx`, put `uvx mcp-proton-mail` in place of `proton-mail-mcp`:

```sh
proton-mail-mcp setup --dry-run --json --client claude-desktop
proton-mail-mcp setup --yes --json --client claude-desktop
proton-mail-mcp configure-client --client cursor --yes --json
proton-mail-mcp configure-client --client grok-bot --print-config --json
proton-mail-mcp detect --json
```

The document holds:

- `result`: `planned`, `printed`, `done`, `needs_user`, `refused`, `failed` or `interrupted`;
  `exit_code`; `error`; and `next`, what to do now.
- `steps`: each step's number, kind, summary and status; `notes` and `warnings`.
- `launcher`: how a person starts this same copy in their own terminal. Under `uvx` that is
  `uvx --from <source> proton-mail-mcp`, since `proton-mail-mcp` is not on `PATH` there; for a
  run from PyPI, `<source>` is `mcp-proton-mail==<version>`. Otherwise it is the running script,
  by absolute path.
- `connector`, for `setup` and `configure-client`: the `command` and `args` any MCP client on
  this computer can start, the server's `url`, and a `token` field that only says it is not
  shown.
- `configs`, with `--print-config`: each client's entry, as
  [Let the installer write the config](clients.md#let-the-installer-write-the-config) says.
- `needs_user`: what only a person can do. Each has an `id`, `when` it comes (`before`, `during`
  or `after` the run), the `text` to relay, `blocking` (it stops any run that asks nothing),
  `stops_run` (this run stopped, or in a dry run would stop with `--yes`, before changing
  anything until it is done), `needs_terminal`, and for those the `command` the person runs in
  their own terminal, with the launcher in front.

A usage error, such as a missing `--yes` or an unknown `--client`, is a document too, with
`result` `refused` and `exit_code` 2. The token never appears in the document, the plan or any
output.

| `id` | What the person does |
| --- | --- |
| `quit_bridge_app` | Quit the Bridge app, or rerun with `--skip-bridge-service` to keep it. Blocking |
| `bridge_login` | No Bridge username is known yet: run `command` in their own terminal, which logs in to Bridge first ([Bridge login](#bridge-login)). Blocking |
| `bridge_password` | Run `command` in their own terminal: Bridge's login if needed, then the Bridge password at a hidden prompt. Stops a `--json` run |
| `sudo` | Linux, to install Bridge: the sudo password, in their own terminal. Stops a `--json` run unless sudo still holds their credentials |
| `store_tools` | A dry run only: install `secret-tool` or set up `pass`, as `text` says; a real run refuses until then |
| `secret_service` | Linux: no Secret Service answers on this D-Bus session; run setup in the desktop session whose keyring is unlocked, or use `--credential-store pass`. Blocking |
| `keychain_prompt` | macOS: choose Always Allow when Keychain asks, during the run |
| `restart_client` | Restart the client, after the run |

A typical agent run: plan with `--dry-run --json` and tell the person what they will be asked to
do; ask them to quit the Bridge app if `quit_bridge_app` is listed; if `bridge_login`,
`bridge_password` or `sudo` would stop the run, hand them its `command`, which does the whole
setup in their terminal; then apply with `--yes --json`, which finds everything done. Last, relay
`restart_client` and the `connector`. An agent can also install everything but the credentials
with `--yes --skip-credentials --bridge-user <username>`, and leave the person one short `setup`
in a terminal.

The Bridge password is never read from an argument, an environment variable or a pipe,
so it needs a person at a terminal. Without a terminal, a `setup` that would have to ask
for it stops before changing anything. Bridge's own login also needs a person at a terminal;
setup does not open it with `--yes` or without a terminal, and hands over the command instead.

A run that asks nothing calls `sudo -n`, which never prompts: if sudo's cached login runs out
during the run, the step fails and says to run `sudo -v` in a terminal, then rerun.

`setup` exits 0 when it finished, warnings included; 1 when a step or the self-check
failed, the plan was declined, or a file the installer did not write sits at a path it
would write (it refuses that before any change); 2 when it refused anything else before
changing anything, a person's turn (`needs_user`) and a usage error included; and 130 when
interrupted.

### PyPI and the MCP Registry

The package is on [PyPI](https://pypi.org/project/mcp-proton-mail/) as `mcp-proton-mail`, so
`uvx mcp-proton-mail setup` and `pipx install mcp-proton-mail` work by name, and the server is
listed in the [MCP Registry](https://registry.modelcontextprotocol.io/) as
`io.github.legalaspro/proton-mail-mcp`. That listing comes from the repository's `server.json`,
which names the PyPI package and its `connect` command, and the README's `mcp-name` line proves
the package's ownership. `make mcpb` builds a Claude Desktop bundle locally
([Claude Desktop](clients/claude-desktop.md#the-mcpb-bundle)), and the repository is a Claude Code
plugin marketplace ([Claude Code](clients/claude-code.md#as-a-plugin)). The public repository's
`publish.yml` publishes a `v*` tag to PyPI, and the registry listing is a step by hand
([CI](ci.md#publishing-to-pypi)). Nothing else here publishes or registers anything.

## Running from a checkout

For development, or to try a change before it is released:
`uvx --from <checkout> proton-mail-mcp setup` runs the guided setup from a clone of this repository,
and builds the clone again after a `git pull` or an edit. The service gets that same copy, as
[Where the service's copy comes from](#where-the-services-copy-comes-from) says.

`scripts/install.sh install` runs the same plan from a clone of this repository, without
the username question, the login offer and the self-check. The script only locates
Python 3.12 or newer and then runs the standard-library module
`proton_mail_mcp.installer` from that checkout. Every plan can be printed first with
`--dry-run`, which changes nothing.

## What the installer never does

The installer never:

- prints, logs or writes the Bridge password or the bearer token to a file,
  service definition or command line, and never reads the password with echo;
- enables guarded send, turns off mutation confirmation
  (`PROTON_MCP_CONFIRM_MUTATIONS=1` is written on first install), or installs
  the optional rules timer;
- bundles or mirrors Bridge, or pipes a download into a shell;
- runs as root. Package steps call `sudo` themselves on Linux;
- stops, disables, unloads or deletes a service, or removes, replaces or
  overwrites a file or directory, that it did not create (except with `--force`,
  which keeps the replaced file as a `.bak`). Downloads go only into
  new files, temporary files get fresh names, and a backup never replaces an
  earlier one;
- deletes a directory that has content, not even a virtual environment or the
  download directory it made. It prints their size and, where that is safe, the
  command that removes them, and you decide.

## Supported platforms

| Platform | Bridge source and verification | Credential store | Services |
| --- | --- | --- | --- |
| Debian, Ubuntu and derivatives (x86_64) | Proton's `.deb` from `proton.me/download/bridge`; `debsig-verify` against temporary policy and keyring directories that hold only Proton's policy and the pinned key must report `Verified package from` before `apt-get` runs | libsecret (`secret-tool`) or `pass` | systemd user units |
| Fedora, RHEL and clones (x86_64) | Proton's `.rpm`; `rpm --checksig` against a temporary rpm database that holds only the pinned key must report `digests signatures OK` before `dnf` runs | libsecret or `pass` | systemd user units |
| Arch Linux and derivatives | `protonmail-bridge-core` from Arch `[extra]`; pacman checks the repository signature | libsecret or `pass` | systemd user units |
| macOS | Homebrew cask `proton-mail-bridge` (the cask pins the download's SHA-256), then `codesign --verify`, Gatekeeper `spctl --assess` and a Proton Developer ID check | login Keychain | launchd LaunchAgents |

On Debian and Ubuntu a failed `apt-get update`, such as from one broken third-party source,
no longer stops the install: the local package installs from the package lists apt still has.
If apt cannot install it at all, install Bridge by hand, then rerun with `--skip-bridge`:

1. From `proton.me/download/bridge`, download `protonmail-bridge_<version>-1_amd64.deb`,
   `bridge_pubkey.gpg` and `bridge.pol`.
2. Check that the key's fingerprint is `D51E64D3E63EDC3EEF7864CEE2C75D68E6234B07`
   (`gpg --show-keys bridge_pubkey.gpg`), and verify the package with `debsig-verify` against
   that key and policy, as [Proton describes](https://proton.me/support/verifying-bridge-package).
3. `sudo apt-get install ./protonmail-bridge_<version>-1_amd64.deb`. Where apt cannot help,
   `sudo dpkg -i` needs Bridge's dependencies first; a minimal image may lack
   `libpulse-mainloop-glib0` and `fonts-dejavu`, which only apt resolves for you.

Proton officially supports Bridge on the latest Ubuntu LTS and the latest Fedora.
Proton publishes Linux packages for x86_64 only; elsewhere install Bridge
yourself and pass `--skip-bridge`. RHEL clones may need EPEL for Bridge's
dependencies.

Proton documents the signing key's long ID `E2C75D68E6234B07`. The installer pins
the full fingerprint `D51E64D3E63EDC3EEF7864CEE2C75D68E6234B07`, which ends in
that ID; it was the primary key of both the copy on `proton.me` and the one on
`keyserver.ubuntu.com` on 28 Oct 2026. The downloaded key file must hold exactly
one primary key, and it must be that one: a file in which the pinned key or its ID
appears only as a subkey, or that holds any other primary key, is refused, as is
a debsig policy that names any other key. On Fedora the system rpm database is
neither used nor changed, because it trusts every configured repository's key.
The installer first checks that the empty temporary database verifies nothing,
then imports only the pinned key, then requires `digests signatures OK`. On
Debian the system debsig directories (`/etc/debsig/policies` and
`/usr/share/debsig/keyrings`) are likewise neither used nor changed, because they
trust every configured origin. `debsig-verify --policies-dir --keyrings-dir` runs
on private temporary directories: they must first verify nothing while empty, then
receive Proton's checked policy and the pinned key, whose converted keyring gpg
must again list as that one primary key, and then the package must verify. The
check creates each temporary directory fresh, with a random name (`.rpmdb-*` or
`.debsig-*`) inside the download directory. It stays afterwards, whether or not the
check passes, and the installer prints its path: like every directory with content,
it is never deleted. The download directory's note at uninstall covers it.

The Linux packages default to Bridge 3.27.1 (current on 28 Oct 2026); choose another
release with `--bridge-version`. Homebrew always installs its current release.

### Windows

`proton-mail-mcp setup` and `scripts/install.ps1` plan and apply the same steps on Windows:
winget and an Authenticode check for Bridge, Credential Manager for secrets, and Task
Scheduler logon tasks for the services. **This has run on Windows Server runners only, not on
Windows 10 or 11 or with a real Proton account.** Read [docs/windows.md](windows.md), which says
what was checked and what was not.
Python from Cygwin or MSYS2 is refused; use Python for Windows, or
[WSL2](https://learn.microsoft.com/windows/wsl/install) and the Linux quick start.

## Prerequisites

- A paid Proton plan (Bridge requires one).
- Python 3.12 or newer, with `pipx` or `uv`; or this repository checked out, where
  `scripts/install.sh` prints the package command if no Python is found.
  Debian and Ubuntu also need `python3-venv`, unless you use uv's own Python.
- Linux: `sudo`, and `secret-tool` (`libsecret-tools` on Debian and Ubuntu,
  `libsecret` on Fedora and Arch) in a desktop session, or an initialised
  [`pass`](https://www.passwordstore.org/) store (`pass init <gpg-key-id>`) on a
  headless host. Bridge itself also needs one of these for its own vault.
  Services read secrets unattended: a libsecret keyring unlocks at desktop login,
  and `pass` needs a GPG key without a passphrase or a gpg-agent that has it
  cached. Without the tool, a real run stops before anything changes; a dry run still shows the
  whole plan, with a `store_tools` hand-off that names the package.
- Linux with libsecret: a Secret Service, such as gnome-keyring or KeePassXC, on the D-Bus session
  setup runs in. On a host with several sessions, a shell's `DBUS_SESSION_BUS_ADDRESS` may point
  at a bus without one, which shows only later as a failed store, and Bridge then cannot keep its
  vault key. Where `dbus-send` is there to ask, setup checks first and stops with a
  `secret_service` hand-off: run it in the desktop session whose keyring is unlocked, set
  `DBUS_SESSION_BUS_ADDRESS` to that session's bus, or use `--credential-store pass`.
- Linux services: systemd with a reachable user manager (`systemctl --user`
  works in a normal login session). On a host systemd does not run at all (a
  container, or a distribution with another init system such as Artix), see
  [Hosts without systemd](#hosts-without-systemd). On a
  systemd host where `systemctl` is missing from `PATH` or cannot reach the user
  manager (no login session), services may be running unseen, so the installer
  stops before anything changes; rerun in a login session, or pass
  `--skip-services`.
- macOS: [Homebrew](https://brew.sh), unless Bridge is already installed.

## Install

Read the plan, then apply it:

```sh
proton-mail-mcp setup --dry-run
proton-mail-mcp setup
```

From a checkout, the same plan without the guided parts:

```sh
scripts/install.sh install --dry-run --bridge-user you@proton.me
scripts/install.sh install --bridge-user you@proton.me
```

The installer prints the full plan and asks before applying it (`--yes` skips the
question; without a terminal it is required). The steps are:

1. Install Bridge if it is missing, verifying it as described above.
2. Create the virtual environment and `pip install` proton-mail-mcp into it.
3. Store the Bridge password and a newly generated bearer token.
4. Write the non-secret settings file.
5. Write, enable and start the Bridge and MCP services, then check that
   `127.0.0.1:8765` accepts connections.

`setup` adds these. On a terminal it first lists what it will do, before any question. If
neither `--bridge-user` nor the settings file gives the Bridge username, it reads it from Bridge
after a login, or asks for it on a terminal ([Bridge login](#bridge-login)). It
shows the folder attachment tools use and lets you change it, at a question on a terminal or
with `--attachments-dir`. Just before step 3 it offers Bridge's CLI on this terminal
(`--skip-bridge-login` turns that off). Then it runs the self-check described in the
[quick start](#quick-start), and last it adds each `--client`.

A stored Bridge password means an account most likely logged in already, so then `setup`
does not offer the CLI; its note gives the commands to log in again by hand, which keeps the
token. `--replace-credentials` stores a new Bridge password and also a new token, which each
client then needs again. The CLI cannot run beside a running Bridge. On a rerun where the
installer's own Bridge service runs and no password is stored yet, the offer is to stop that
service, open the CLI and start the service again, even if the CLI fails; Enter agrees. Any
other Bridge, shown by something on that port or by a `--noninteractive` process (`pgrep`),
means no CLI: the installer stops only its own service. If `pgrep` cannot answer, it also opens
no CLI. A person at a terminal has to agree, so `--yes`, `--json` or no terminal never stops
anything or opens the CLI.

On a dry run with no username, `setup` shows `you@proton.me` as a placeholder and asks no
credential store about it. With `--yes`, `--json` or no terminal it cannot log in for you, so it
stops before any change with the `bridge_login` hand-off and the command to run in a terminal.

`secret-tool` answers the same for "no entry" and for a locked one, so on a first
install it cannot show that nothing is stored. `install` then stops and asks you to pass
`--replace-credentials`. `setup` asks you instead, and stores new values only for what
the store cannot show, so a token it can read is kept. Without a terminal, or with
`--yes`, it stops like `install`.

At step 3 the installer asks for the **Bridge** password, not your Proton account
password: the one Bridge generated, which Bridge's `info` showed, and which the Proton Mail Bridge
app shows when you select your mailbox and open Mailbox details, under Password. The marks some
terminals put around a paste, and the spaces and line break a copied line brings along, are
dropped. In classic PowerShell or cmd, pasting at a hidden prompt often doesn't work. Use Windows
Terminal and paste with Ctrl+Shift+V, or type the password. Setup refuses an empty entry and one
shorter than 16 characters (Bridge's passwords are 22), at either prompt, and two entries that
differ; it shows that hint and asks again, three times in all, then stops and stores nothing. It
never shows what was typed or how long it was. `--skip-credentials` leaves the password for later.
On macOS the Keychain `security` tool prompts for the password itself, so setup cannot see what
you typed and checks none of this; an empty password there is kept by a plain rerun, so replace
it with `uvx mcp-proton-mail setup --replace-credentials --client <client>`, which the server's
error names too. If the terminal cannot hide what
you type, the installer stops rather than read the password with echo; rerun it in a terminal,
or pass `--skip-credentials` and store the password yourself under the names in
[What goes where](#what-goes-where).

`setup --password-stdin` reads the password from stdin instead, for a person who pipes it in from
their own shell: one line, whose LF or CRLF is dropped, with the same checks. It asks nothing
again: an empty or short line, or more than one, exits with status 2 before any step runs and
stores nothing, and so does a terminal on stdin, where the hidden prompt is the way. stdin holds
the password, so the run needs `--yes`; a dry run reads nothing. Where a Bridge password is stored
already, add `--replace-credentials`, which also stores a new token.
[Windows](windows.md#the-bridge-password) shows it in PowerShell, with no password in the command
or its history.

Bridge's CLI and its service cannot run at the same time. `setup` on a terminal can
stop and restart the service for you. To log in by hand, stop the Bridge service first
and start it again afterwards; the plan prints both commands for your platform. The MCP
service keeps running meanwhile: on Linux
its unit only `Wants=` the Bridge unit, so stopping Bridge does not stop it, and
it reaches Bridge again on its next request.

### Bridge login

Bridge needs one login per Proton account, which only you can do. Its desktop app does it in a
window, which on a host without a desktop, or over a remote desktop, may be unusable: a
human-verification step there has left the app waiting. `setup` uses Bridge's CLI instead, on
your terminal:

1. You type `login`, then your Proton email and password, and the two-factor code if it asks.
   Pasting works, which helps where a remote desktop drops keys.
2. If Bridge prints a human-verification link, open it in any browser, on any device, finish the
   check there, then press Enter in the terminal. Bridge waits for that.
3. Once Bridge says the account was added, setup types `info` and then `exit` for you, and reads
   the IMAP username from what `info` prints. Already logged in? Type `info` instead of `login`.

Setup runs the CLI through a pseudo-terminal so it can read the username; it keeps only that
line, and `info`'s other lines, the Bridge password among them, are dropped as they pass. The
password still reaches the credential store only through the hidden prompt. If no username shows,
setup asks for it. On Windows, which has no pseudo-terminals, the CLI runs in the terminal
directly and setup asks for the username. If the username Bridge shows differs from the one setup
was given, setup stops before storing anything and names the right one.

After a first login Bridge downloads the whole mailbox, which can take from minutes to hours.
Meanwhile searches may come back incomplete: the search index reports `bridge_syncing` and
catches up by itself, as [Automatic startup recovery](search-index.md#automatic-startup-recovery)
says. Bridge's own window or CLI shows the sync's progress.

### Hosts without systemd

In a container, or on a system another init system runs, the installer sets up no service.
`setup` then starts Bridge (`--noninteractive`) and the MCP server as background processes
itself, so its self-check can run:

- Their output goes to `bridge.log` and `service.log`, and their process ids to `bridge.pid` and
  `service.pid`, all in the state directory (`~/.local/state/proton-mail-mcp/`).
- A rerun leaves a running server alone, or stops it by that process id, and only while it still
  runs the server, when something it reads changed, then starts it again. Something already on a
  port is left alone.
- They stop when the host restarts: rerun setup then, or have your own init system run
  `<venv>/bin/proton-mail-mcp-service --env-file <settings>`, and Bridge, at boot.
- To stop one, run `kill "$(cat ~/.local/state/proton-mail-mcp/service.pid)"`, or `bridge.pid`
  for Bridge.

`install` starts nothing there: it prints the command, and a `nohup` line that runs it in the
background. `--skip-services` starts nothing either.

### Copy the token

The token is never shown. To give it to a client:

| Command | What it does |
| --- | --- |
| `proton-mail-mcp configure-client --client <name>` | Adds the server to a client. Most clients start `connect`, which reads the token itself; ChatGPT's config gets the token; nothing prints it |
| `proton-mail-mcp show-token --copy` | Puts it on the clipboard (`pbcopy`, `clip`, or `wl-copy`, `xclip` or `xsel`) without showing it, at a terminal only |
| `proton-mail-mcp show-token` | Prints it, to a terminal only; or `scripts/install.sh show-token` from a checkout |

Both `show-token` forms refuse unless a person runs them at a terminal: any process can read a
clipboard, as it can a pipe. After pasting from the clipboard, copy something else: a clipboard
history may keep it, and on macOS Universal Clipboard may hand it to your other devices.
Configure any other client with `http://127.0.0.1:8765/mcp` and the header
`Authorization: Bearer <token>`.

A `token` file in the settings folder (`~/.config/proton-mail-mcp/token` on Linux), left from an
earlier manual setup, goes stale once setup stores a new token, and whatever still reads it gets
`401`. Setup names it, with the command that deletes it, and never reads or deletes it itself.

## What goes where

| | Linux | macOS |
| --- | --- | --- |
| Settings | `~/.config/proton-mail-mcp/proton-mail-mcp.env` | `~/Library/Application Support/proton-mail-mcp/proton-mail-mcp.env` |
| Virtual environment | `~/.local/share/proton-mail-mcp/venv` | `~/Library/Application Support/proton-mail-mcp/venv` |
| Bridge service | `~/.config/systemd/user/proton-mail-mcp-bridge.service` | `~/Library/LaunchAgents/com.proton-mail-mcp.bridge.plist` |
| MCP service | `~/.config/systemd/user/proton-mail-mcp.service` | `~/Library/LaunchAgents/com.proton-mail-mcp.plist` |
| Audit, undo and index state | `~/.local/state/proton-mail-mcp/` | `~/.local/state/proton-mail-mcp/` |
| Service logs | `journalctl --user -u proton-mail-mcp` | `~/Library/Logs/proton-mail-mcp/` |
| Downloads | `~/.cache/proton-mail-mcp/installer` | `~/Library/Caches/proton-mail-mcp/installer` |

XDG variables are honoured on Linux, and `XDG_STATE_HOME` on macOS too. Services
do not inherit the installing shell, so the installer writes the state locations
it resolved into the settings file: `PROTON_MCP_ACTION_LOG` (audit and undo log;
search and rules state sit next to it) and `PROTON_MCP_IDENTITY_INDEX_DIR`. A value
exported in the installing shell is written as given and must be an absolute
path. Command-line tools read only their own environment, so run them with the
same two values.

Credentials use service `proton-mail-mcp` (Bridge password, the name the macOS
Keychain path always used) and `proton-mail-mcp-token` (bearer token), with the
Bridge username as the account. In `pass` they are `proton-mail-mcp/<user>` and
`proton-mail-mcp-token/<user>`.

With `pass`, the settings file also names the store and, if you use a custom one,
the GnuPG home: `PROTON_MCP_PASSWORD_STORE_DIR` (from `PASSWORD_STORE_DIR`, else
`~/.password-store`) and `PROTON_MCP_GNUPGHOME` (from `GNUPGHOME`, only when it is
set). Both must be absolute paths. Every `pass` call of the service, of
`show-token` and of `uninstall --remove-credentials` gets them as
`PASSWORD_STORE_DIR` and `GNUPGHOME`, so a store set only in the installing shell
is still the one the service reads. On reruns a value exported in the shell wins
and the change is reported; existing entries are not moved.

The services run `proton-mail-mcp-service --env-file <settings>`. That launcher
accepts only `PROTON_*` and `PMM_*` settings, refuses a file that contains
`PROTON_MCP_TOKEN` or `PROTON_BRIDGE_PASSWORD`, refuses a settings file that other
users can write, and loads the token from the credential store named by
`PROTON_MCP_CREDENTIAL_STORE`. The token is passed to the server directly and is
not exported to child processes. Values are literal: no quotes or `$` expansion.

On Linux the installer enables the units by path (`systemctl --user enable
<unit file>`), so a user manager started with another `XDG_CONFIG_HOME` than the
installing shell links them into its own configuration directory; it then reloads
systemd before starting them. `uninstall` disables them by name, which also removes
those links.

On Linux, units start at login; on a headless host run
`sudo loginctl enable-linger $USER` once so they keep running after logout and
start at boot. LaunchAgents read the login Keychain and run while you are logged
in.

## Reruns, changes and options

Rerunning the installer is safe. It skips an installed Bridge, the virtual
environment it created and stored credentials, and rewrites a file only when its
content changes. It restarts the MCP service on every run, because pip always runs,
and restarts Bridge only when its definition changed. A failed run can be repeated after
fixing the cause.

The installer reuses a virtual environment only if it created it: it writes the
marker file `.proton-mail-mcp-installer` into the directory before creating the
environment. Any other directory at that path stops the plan before anything
changes; remove it yourself, or pass `--venv` with a path that does not exist yet.
That path is saved as `PMM_INSTALLER_VENV` in the settings file, so reruns and
`uninstall` find it. The download directory follows the same rule. A download
never replaces a file already at its name, for example one a failed run left: the
installer stops and prints the `rm -f -- <file>` command, and you check and remove
the file before rerunning.

### Directories the installer made stay

The installer never deletes a directory that has content, not even one it made.
Moving the environment with a new `--venv` leaves the previous one where it is,
every byte of it: nothing is moved, and the settings keep pointing where they
pointed. A data location exported in the installing shell still replaces the saved
one, and the plan says so; whatever is at the old place stays there. Earlier
environments the installer made are listed as `PMM_INSTALLER_PREVIOUS_VENVS` in the
settings file. Every run prints a note on each one that still exists, and appends
the note to `installer.log` in the state directory (`~/.local/state/proton-mail-mcp`).
The note gives the directory's size. Where that cannot obviously harm this
installation it also gives the command that removes the directory by hand, such as
`rm -rf -- /home/you/old-venv`. It gives no command for a directory that:

- has no `.proton-mail-mcp-installer` marker, or is itself a symbolic link;
- is the current virtual environment under another name, holds it or lies inside
  it;
- holds, by name or with symbolic links resolved, the settings file, the config,
  state, log or service directories, the Bridge executable, GnuPG's or pass's
  default home, the default attachments folder, or a path that a data setting names
  now or named before this run
  (`PROTON_MCP_ACTION_LOG`, `PROTON_MCP_IDENTITY_INDEX_DIR`, `PROTON_MCP_SAVED_RULES`,
  `PROTON_MCP_ATTACHMENTS_DIR`, `PROTON_MCP_PROTECTED_CONFIG`, `PROTON_BRIDGE_CERT`,
  `PROTON_MCP_PASSWORD_STORE_DIR`, `PROTON_MCP_GNUPGHOME`); the note names them, so
  you can move them out and update their settings first;
- is a previous environment, while this run switched no service away from it
  (`--skip-services`, or a host without systemd);
- at uninstall, is a virtual environment while no MCP service of the installer's
  was stopped and checked: one of yours may still run from it.

The installer has not checked whether anything else of yours still runs from the
directory, keeps data in it or links into it, and the note says so. It reads no
service definition, drop-in, environment file, LaunchAgent or task that it did not
write, so nothing from one can reach the screen or `installer.log`. Check before you
paste the command.

The settings file keeps lines the installer does not own, for example
`PROTON_MCP_PUBLIC_HOST`. The installer updates the account, store, host and port
lines. It writes `PROTON_MCP_CONFIRM_MUTATIONS=1`, `PROTON_MCP_ACTION_LOG` and
`PROTON_MCP_IDENTITY_INDEX_DIR` only if they are missing, unless the installing
shell exports one of the last two; it reports a file that turns confirmation off
or enables send, naming the setting but never its value. A settings file at that
path that the installer did not write (it lacks the installer's
`# Managed by proton-mail-mcp installer.` line) stops
install, uninstall and `show-token` before anything changes: the installer neither
uses nor shows its values, only its keys. Move it elsewhere, then rerun.

| Option | Meaning |
| --- | --- |
| `--dry-run` | Print the plan; change nothing. |
| `--yes` | Apply without asking. |
| `--bridge-user USER` | Bridge IMAP username, the one shown in Bridge's Mailbox details; reruns read it from the settings file. |
| `--credential-store libsecret\|pass` | Linux only; default libsecret when `secret-tool` exists, else an initialised `pass`. |
| `--port N` | MCP port on `127.0.0.1` (default 8765). |
| `--bridge-imap-port N` | Bridge IMAP port, which must use STARTTLS (default: the one the settings file saved, else 1143). |
| `--bridge-version X.Y.Z` | Linux `.deb`/`.rpm` release. |
| `--bridge-bin PATH` | Bridge executable for the Bridge service; a relative path is resolved against the current directory. |
| `--venv PATH` | Virtual environment path, new or made by the installer; saved for later runs. The previous one stays. |
| `--source PATH` | The proton-mail-mcp checkout or wheel file to install. Default: see [where the service's copy comes from](#where-the-services-copy-comes-from). |
| `--skip-bridge-login` | `setup` only: do not offer to open Bridge's CLI. |
| `--client NAME` | Also add the server to this client: `claude-desktop`, `claude-code`, `chatgpt`, `codex`, `gemini`, `cursor` or `grok-bot`. Repeatable. |
| `--print-config` | `setup` only: print each `--client`'s entry, or every client's, and change nothing. |
| `--python PATH` | The Python 3.12 or newer the private environment is made from: a path, or a name found on `PATH`, which setup checks before it plans; see [the service's Python](#the-services-python). |
| `--json` | Ask nothing and print one JSON document on stdout; needs `--dry-run`, `--yes` or `--print-config`. See [Agents and scripts](#agents-and-scripts). |
| `--non-interactive` | Ask nothing and open no prompt, with the usual output; needs `--dry-run` or `--yes`. |
| `--attachments-dir` | `setup` only: a folder that differs from the one in use is written to the settings file as `PROTON_MCP_ATTACHMENTS_DIR` and created, mode `0700`. On a terminal, `setup` asks instead, showing the folder in use. |
| `--skip-bridge` | Do not install or verify Bridge. |
| `--skip-bridge-service` | Leave Bridge to its own autostart, for example the desktop app. |
| `--skip-credentials` | Do not prompt or store credentials. |
| `--skip-services` | Install files only; no service is written, stopped or started. On a Linux host that does not run systemd, `setup` [starts the processes itself](#hosts-without-systemd) instead. |
| `--replace-credentials` | Prompt again and generate a new token. |
| `--password-stdin` | `setup` only: read the Bridge password from one line on stdin, a pipe, instead of the hidden prompt; needs `--yes`. See [Install](#install). |
| `--force` | Replace a file the installer did not write (not the settings file), keeping it in a new `.bak` file beside it; an earlier backup is never replaced. |
| `--simulate FAMILY` | With `--dry-run`, print the plan for another platform. |

`detect` shows the detected platform, default store and paths.

### Moving from a manual LaunchAgent

The [manual LaunchAgent](#run-at-login-with-launchd) also uses `com.proton-mail-mcp.plist`, with the
token inside. The installer refuses to overwrite a file it did not write, and
stops before changing anything when launchd already runs that label from another
file or without the installer's file. Unload the old agent (`launchctl bootout gui/$(id -u)/com.proton-mail-mcp`), then either
move the plist away or rerun with `--force`, which keeps it in a new file such as
`com.proton-mail-mcp.plist.k2x9q1.bak` (the run prints the name; an earlier backup
is never replaced). That backup still contains the old token: delete it once the
new agent works, and give clients the new token. A `com.proton-mail-mcp.plist.disabled`, or the
same for the Bridge agent, left from an earlier install is named in the plan with the command
that removes it: launchd ignores it, but it may still hold an old token.

## Uninstall

```sh
proton-mail-mcp uninstall --dry-run
proton-mail-mcp uninstall
```

With uv, use `uvx mcp-proton-mail uninstall`; from a checkout, also
`scripts/install.sh uninstall`.
All of them run the same plan.

Uninstall stops the installer's own services, then proves each one stopped before it
removes anything. After `systemctl --user disable --now` systemd must report every
unit `inactive` or `failed`; after `launchctl bootout`, `launchctl print` must
report that it cannot find the agent (exit status 113, "Could not find specified
service"). Any other answer, a failed or killed query included, proves nothing,
and neither does the stop commands' exit status. If a check fails, uninstall stops,
nothing is removed, and it prints how to stop the service yourself before you rerun
it (a unit with `RefuseManualStop=yes` needs that setting removed first;
`systemctl --user status <unit>` shows what keeps it running).
A systemd unit or LaunchAgent is stopped only when the definition the service
manager uses is the installer's file and carries its marker. If a definition the
installer did not write holds one of its service names, or the service manager does
not say which file it uses (a failed `systemctl --user show` query, or a
`launchctl print` answer without a path), uninstall stops nothing and changes
nothing, and prints how to look at that definition; remove or rename it if it is
yours, then rerun. Install stops the same way while the service manager runs such a
definition or does not name the one it runs. Otherwise uninstall removes
the single files it wrote and marked: the units or LaunchAgents and the settings
file, and the config and data directories once they are empty. The settings file
goes only when uninstall stopped and checked the installer's own MCP service.
Otherwise it stays, with the command that removes it by hand: a service you run
yourself may read it (after `--skip-services`, or on a host without systemd). The
virtual environment, earlier ones and the download directory stay, each with a note
as above. When the installer's units are there but `systemctl` is missing from
`PATH` or cannot reach the user manager (or the host does not run systemd),
uninstall changes nothing, since it can neither stop them nor tell that they are
stopped. It prints how to remove the units by hand (`systemctl --user disable --now
...` where systemd runs, then `rm -f -- <unit files>`) before rerunning uninstall.
Without any unit of the installer's (as in a container), it removes the files only.
Uninstall keeps:

- the virtual environments and the download directory, with their sizes and, where
  safe, the command that removes them;
- audit, undo and index state, saved attachments and service logs; the last note lists
  where each data setting points;
- the stored credentials, unless `--remove-credentials`;
- Bridge, unless `--remove-bridge`, which runs the package manager's removal.
  Bridge's own settings and cache always stay;
- the `proton-mail` entry in each MCP client's config, and the dated `.bak` copies setup left
  beside them, which for ChatGPT may hold an old token. Remove the entry in each client (its page
  under [Connect an MCP client](clients.md) says where) and delete the copies you no longer need;
- a server or Bridge setup started in the background on a host without systemd: uninstall says so
  when their pid files are there, with the `kill` command.

## Install and run on your Mac

Run it on the same Mac and under the same login account as Bridge.

These are the manual steps. To automate them on macOS or Linux (Debian/Ubuntu,
Fedora/RHEL, Arch), including Bridge, the credential store and per-user
services, use the [quick start](#quick-start) above.

Install and sign in to Proton Mail Bridge yourself. Copy its Bridge username
and generated password from the account's IMAP settings; use the Bridge
password, not your Proton account password. IMAP defaults to port `1143`, using
STARTTLS. Optional guarded send uses Bridge SMTP on `1025` with STARTTLS.
Check your account settings if Bridge uses a different port. See
[Proton's Bridge documentation](https://proton.me/support/imap-smtp-and-pop3-setup)
and [port guidance](https://proton.me/support/mail/bridge/common-issues-questions).

As a secondary option, install in a virtual environment with Python 3.12+ from a
checkout of this repository:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
export PROTON_BRIDGE_USER='your-bridge-username'
security add-generic-password -s proton-mail-mcp -a "$PROTON_BRIDGE_USER" -w
export PROTON_MCP_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/proton-mail-mcp
```

The Keychain command prompts for the Bridge password. The service name must be
`proton-mail-mcp`, and the account must equal `PROTON_BRIDGE_USER`. The server
retrieves it with:

```sh
security find-generic-password -s proton-mail-mcp -a "$PROTON_BRIDGE_USER" -w
```

Run in the foreground first to allow any macOS Keychain access prompt. A
LaunchAgent uses the logged-in user's Keychain, which must be unlocked. Do not
put the Bridge password in the LaunchAgent. `PROTON_BRIDGE_PASSWORD` is an
environment override for offline tests or environments without macOS Keychain.
Treat it as a secret if you use it.

Save the generated bearer token in your MCP client's secret storage and in your
local LaunchAgent configuration. Keep it out of version control and logs. The
server refuses to start without a token of at least 32 characters. Every HTTP
request, including initialization and subsequent session requests, must include:

```text
Authorization: Bearer <your-token>
```

Missing or incorrect tokens return HTTP `401`; comparison uses
`hmac.compare_digest`. Configure your client's Streamable HTTP server URL as
`http://127.0.0.1:8765/mcp` for use on the Mac, or the tailnet HTTPS URL below.

## Run at login with launchd

Use [`examples/com.proton-mail-mcp.plist`](../examples/com.proton-mail-mcp.plist) as a
template for a **LaunchAgent**, running as your logged-in user. It includes
`EnvironmentVariables`, automatic restart, and stdout/stderr log paths.
`launchd` does not expand `~`, `$HOME`, or shell substitutions in this file.
Replace every `/Users/YOUR_USER` path with an absolute path, set the real Bridge
username, and replace the token placeholder with your generated token. Keep the
working directory and executable paths pointed at your checkout and its venv.

Prepare the local files, edit the copy, then validate and load it:

```sh
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/proton-mail-mcp"
chmod 700 "$HOME/Library/Logs/proton-mail-mcp"
touch "$HOME/Library/Logs/proton-mail-mcp/stdout.log" "$HOME/Library/Logs/proton-mail-mcp/stderr.log"
chmod 600 "$HOME/Library/Logs/proton-mail-mcp/"*.log
cp examples/com.proton-mail-mcp.plist "$HOME/Library/LaunchAgents/com.proton-mail-mcp.plist"
chmod 600 "$HOME/Library/LaunchAgents/com.proton-mail-mcp.plist"
# Edit the copied plist now: paths, username, and token.
plutil -lint "$HOME/Library/LaunchAgents/com.proton-mail-mcp.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.proton-mail-mcp.plist"
launchctl print "gui/$(id -u)/com.proton-mail-mcp"
```

The server keeps credentials and the bearer token out of application errors and
logs. Log files may still reveal operation timing and service diagnostics; keep
their permissions restricted. Keep Bridge running and signed in. This agent
starts the MCP server, not Bridge. An unavailable Bridge causes read tools to
return an error. Search, count and thread-read timeouts return `timed_out=true`,
`partial=true` and a clear `error`, even before any mailbox data is read.
Available results and successfully fetched text remain in the response.
An IMAP timeout leaves the MCP session available for later requests; the failed
IMAP connection is closed and the next mailbox call opens a fresh connection.
When a client cancels a request, its late tool result is discarded and the MCP
session remains available for later requests.

After changing the plist, unload and reload it:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.proton-mail-mcp.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.proton-mail-mcp.plist"
```

Use the `bootout` command alone to stop the LaunchAgent. Rotate a token by
updating the local plist and client secret, then reloading the agent.

## Expose through Tailscale Serve

With Tailscale already installed and signed in on the Mac:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:8765
tailscale serve status
```

Serve terminates HTTPS and forwards to the loopback HTTP server. Get the exact
HTTPS hostname from `tailscale serve status`, for example
`your-mac.your-tailnet.ts.net`. Set `PROTON_MCP_PUBLIC_HOST` to that hostname,
without `https://`, a port, or a path, and restart the MCP server. For launchd,
enable the commented `PROTON_MCP_PUBLIC_HOST` entry in your local plist, replace
its value, then use the `bootout` and `bootstrap` commands above. For a
foreground process, export the variable before starting the server:

```sh
export PROTON_MCP_PUBLIC_HOST='your-mac.your-tailnet.ts.net'
.venv/bin/proton-mail-mcp
```

The SDK's DNS rebinding protection allows only local HTTP hosts by default;
`PROTON_MCP_PUBLIC_HOST` adds the exact Serve hostname and its HTTPS origin to
that allowlist. The server remains bound to `127.0.0.1`. Use the HTTPS hostname
with `/mcp` appended as your client's URL, for example
`https://your-mac.your-tailnet.ts.net/mcp`. The client must still send the bearer
header on every request. Restrict access to the Mac through your tailnet policy.
This setup uses Serve for tailnet access; do not enable public Funnel exposure.

The `--bg` configuration persists across Tailscale restarts. To stop this
endpoint, use `tailscale serve --https=443 off`. See the current
[Tailscale Serve CLI reference](https://tailscale.com/docs/reference/tailscale-cli/serve)
for prerequisites, HTTPS setup, and configuration behavior.

## Tests and CI

`make check` runs the installer's unit tests on Linux without network access:
detection, the plans for every platform (built from simulated hosts), the executor
against temporary directories,
the credential stores against a fake `subprocess.run`, the service launcher, the
`proton-mail-mcp` command and `setup`, the self-check against the real server app in
memory, and `install.sh` itself through bash, plus `shellcheck` and PowerShell when they
are installed.

`make wheel-smoke` builds the wheel and proves the quick start from it, in clean `uvx`
and `pipx` environments. It needs `uv` and network access for the dependencies, and it
uploads nothing. It runs with its own `HOME` and no session bus, with stand-ins for
`systemctl`, `pgrep`, `secret-tool` and Bridge first on `PATH`, so it touches no real unit,
keyring or Bridge. On Linux it applies `setup` on a pseudo-terminal: a real virtual
environment, the wheel's own server started from the unit `setup` wrote, the self-check
over HTTP and IMAP, a typed password that must not appear in the output or in any file,
argument list or service environment, a rerun, a login around the running Bridge service,
and `uninstall`. It also installs by name from a local index with pipx, `uv tool`, plain
`pip` and `uvx`, and proves what each one's `setup` does with that copy. Their dependencies come
from PyPI through a local filter that hides this package's own releases, so pip cannot pick a
published build over the wheel under test.

`scripts/docker_smoke.sh` builds the repository's `Dockerfile`, the image a registry such as Glama
starts ([Glama](glama.md) has the settings its listing uses), and checks it the way such a registry
does: over stdio, `initialize` and `tools/list` must answer and a tool call must refuse, from a
non-root user with no network, a read-only filesystem and no capabilities. It needs Docker and
network access for the build, touches only its own image and container, and uploads nothing. No
workflow runs it.

### Docker image

The image's inputs are pinned so that every build of one commit installs the same thing: both
stages start from `python:3.12-slim` by one multi-arch index digest, and
[`docker/constraints.txt`](../docker/constraints.txt) gives every runtime package the exact
version the test suite ran with. `tests/test_introspect.py` fails when a pin falls outside the
ranges in `pyproject.toml` or a runtime dependency has no pin. Nothing updates these pins on its
own, so refresh them after a dependency change, and now and then for base-image security fixes:

```sh
docker buildx imagetools inspect python:3.12-slim   # the Digest line goes in both FROM lines
.venv/bin/pip freeze --exclude-editable > /tmp/freeze.txt
uv pip compile pyproject.toml --python-version 3.12 --python-platform linux \
    --constraint /tmp/freeze.txt --no-header --no-annotate -o docker/constraints.txt
scripts/docker_smoke.sh
```

[`.github/workflows/check.yml`](../.github/workflows/check.yml) runs these tests and
real-host dry runs on GitHub-hosted `ubuntu-latest` on every pull request to the public release
repo, and on `windows-latest` every night and on each tag there; each pull request also runs the
wheel smoke on `macos-latest`.
[`.github/workflows/installer.yml`](../.github/workflows/installer.yml) adds the wheel smoke and
Fedora, AlmaLinux 9 and Arch containers, on a pull request that changes the installer's paths,
every night and on each tag. In the public release repo only, it also runs the wheel
smoke and dry runs on `macos-latest` (including the bash 3.2 in `/bin/bash`). The private dev
repo runs no macOS job, so test macOS by hand there, as described below.

Every Linux dry run goes through the service preflight without `--skip-services`: Ubuntu starts a
user manager for the runner (`loginctl enable-linger`) and checks that the plan sets up the units;
the containers run no systemd and check that the plan says so and sets up none. The
`windows-latest` job round-trips a throwaway entry through Credential Manager and dry-runs
`install.ps1` under PowerShell 7 and Windows PowerShell 5.1. Dry
runs install nothing.

[ci.md](ci.md) says what runs when.

### UTM virtual machines when hosted runners are unavailable

Hosted-runner billing can block those jobs. Until it works, repeat them by hand
in [UTM](https://mac.getutm.app/) virtual machines on a Mac:

1. Create one VM per target: Ubuntu 24.04 LTS, the latest Fedora Workstation,
   Arch Linux and, on Apple silicon, a macOS guest. Linux targets on Apple
   silicon need x86_64 emulation for Proton's packages, or use `--skip-bridge`
   with Arch Linux ARM's package. Take a snapshot after the OS setup.
2. Install Python 3.12 or newer and copy or clone this checkout into the VM.
3. Run the unit tests: `python -m pip install -e '.[dev]'`, then
   `python -m pytest -q tests/test_installer_*.py tests/test_credential_store.py tests/test_service_entry.py`.
4. Run `detect`, `install --dry-run` and `uninstall --dry-run`, as in the
   workflow.
5. For an end-to-end check, install for real with a test Proton account, confirm
   Bridge and the MCP service start after a reboot, list tools with the token,
   run `uninstall`, and revert to the snapshot.
