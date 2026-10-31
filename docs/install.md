# Installer for Linux and macOS

## Quick start

```sh
pipx install proton-mail-mcp
proton-mail-mcp setup
```

With [uv](https://docs.astral.sh/uv/) one command does both:

```sh
uvx proton-mail-mcp setup
```

`setup` asks for your Bridge username, prints its full plan and asks before it
changes anything (add `--dry-run` to stop after the plan). Then it:

1. checks for Bridge, and installs it from official sources if it is missing;
2. installs proton-mail-mcp into its own private virtual environment, so the service
   keeps running after `pipx uninstall` or `uv cache clean`;
3. offers to open Bridge's CLI on your terminal, where you log in (`login`, `info`,
   `exit`);
4. asks for the **Bridge** password (the one `info` shows, not your Proton password)
   with a hidden prompt, and stores it and a new bearer token in the operating
   system's credential store;
5. writes and starts per-user services for Bridge and the MCP server, which binds only
   to `127.0.0.1`;
6. runs a self-check: the server must refuse a request without the token, accept the
   stored one, and read `INBOX` through Bridge with the stored password. A failure
   exits with status 1 and says what to fix; rerunning is safe.

Then point your MCP client at `http://127.0.0.1:8765/mcp` with the header
`Authorization: Bearer <token>`. The last lines of the run name the command that prints
the token.

Python 3.12 or newer is required: `uvx` finds or fetches one, and `pipx` uses the one
it runs on. On Debian and Ubuntu, `setup` needs `python3-venv` and stops before changing
anything if it is missing; `uvx --managed-python proton-mail-mcp setup` uses uv's own
Python instead.

**Not on PyPI yet:** until the first release is published, the commands above do not
install this project, and must not be run: anyone could hold that name on PyPI. Install
from a wheel you built from this repository instead:

```sh
uv build --wheel
uvx --from dist/proton_mail_mcp-1.0.0-py3-none-any.whl proton-mail-mcp setup
# or: pipx install dist/proton_mail_mcp-1.0.0-py3-none-any.whl && proton-mail-mcp setup
```

### Commands

| Command | What it does |
| --- | --- |
| `proton-mail-mcp` or `proton-mail-mcp serve` | Start the server from `PROTON_*` settings in the foreground, as before. |
| `proton-mail-mcp setup` | The guided install above. |
| `proton-mail-mcp uninstall` | [Stop and remove](#uninstall) the services and settings. |
| `proton-mail-mcp show-token` | Print the stored bearer token, to a terminal only. |
| `proton-mail-mcp detect` | Show the detected platform, credential store and paths. `--json` for scripts. |
| `proton-mail-mcp --version` | Print the installed version. |

Services do not use this command: they keep running
`proton-mail-mcp-service --env-file <settings>` from the private environment.

With `uvx`, put `uvx` before each of these. `setup` also leaves
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
- installed by name: that version, from a wheel on an index (`proton-mail-mcp==<version>`),
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
exact copy, `setup` stops before starting or restarting any service; only the private
environment has changed, and a rerun replaces what pip put there. For a private index or
a wheel directory, pass the wheel instead:

```sh
pip download --no-deps --index-url <your index> proton-mail-mcp==<version>
proton-mail-mcp setup --source ./proton_mail_mcp-<version>-py3-none-any.whl
```

### Agents and scripts

An agent can preview everything and install everything but the password:

```sh
proton-mail-mcp setup --dry-run --bridge-user you@proton.me
proton-mail-mcp setup --yes --skip-credentials --bridge-user you@proton.me
proton-mail-mcp detect --json
```

The Bridge password is never read from an argument, an environment variable or a pipe,
so it needs a person at a terminal. Without a terminal, a `setup` that would have to ask
for it stops before changing anything. After `--skip-credentials`, a person runs
`proton-mail-mcp setup` in a terminal once; everything else is skipped or repeated
safely. Bridge's own login also needs a person at a terminal; setup does not open it
with `--yes` or without a terminal, and prints the command instead.

`setup` exits 0 when it finished, warnings included; 1 when a step or the self-check
failed, or the plan was declined; 2 when it refused before changing anything; and 130
when interrupted.

## Running from a checkout

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
  overwrites a file or directory, that it did not create. Downloads go only into
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

### Windows is not yet supported

The MCP server keeps its audit, undo and index state with POSIX file locking
(`fcntl`), so its service cannot start on Windows. `proton-mail-mcp setup`,
`scripts/install.ps1` and the installer module therefore refuse to install or uninstall
on Windows, including Cygwin and MSYS2, or to plan for it with `--simulate windows`.
They exit with status 2 before changing anything, and `proton-mail-mcp serve` refuses
the same way. `detect` still reports the platform, with `supported: false`.
Until native support, planned for v1.1, use [WSL2](https://learn.microsoft.com/windows/wsl/install):
run `wsl --install`, then run the quick start inside Linux. The Windows plan code
(winget, Authenticode, Credential Manager and Task Scheduler logon tasks) stays in the
module and its unit tests, so it is ready once the server runs on Windows.

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
  cached.
- Linux services: systemd with a reachable user manager (`systemctl --user`
  works in a normal login session). On a host systemd does not run at all (a
  container, or a distribution with another init system such as Artix) the
  preflight says so and the plan goes on without services, as with
  `--skip-services`; have your own service manager run the printed
  `<venv>/bin/proton-mail-mcp-service --env-file <settings>` command at login. On a
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

`setup` adds four things. It asks for `--bridge-user` on a terminal if neither the flag
nor the settings file gives it. It shows the folder attachment tools use and lets you
change it, at a question on a terminal or with `--attachments-dir`. Just before step 3 it
offers Bridge's CLI on this terminal (`--skip-bridge-login` turns that off). Last, it
runs the self-check described in the [quick start](#quick-start).

The CLI cannot run beside a running Bridge. On a rerun where the installer's own Bridge
service runs, the offer is to stop that service, open the CLI and start the service
again, even if the CLI fails. Bridge listens on its IMAP port whether or not an account is
logged in, so the port cannot tell the installer which case this is. If no Bridge password
is stored yet, the answer defaults to yes; if one is stored, to no, so pressing Enter on a
healthy rerun stops nothing. Any other Bridge, shown by something on that port or by a
`--noninteractive` process (`pgrep`), means no CLI: the installer stops only its own
service. If `pgrep` cannot answer, it also opens no CLI. A person at a terminal has to
agree, so `--yes` or no terminal never stops anything or opens the CLI.

On a dry run with no username, `setup` shows `you@proton.me` as a placeholder and asks no
credential store about it.

`secret-tool` answers the same for "no entry" and for a locked one, so on a first
install it cannot show that nothing is stored. `install` then stops and asks you to pass
`--replace-credentials`. `setup` asks you instead, and stores new values only for what
the store cannot show, so a token it can read is kept. Without a terminal, or with
`--yes`, it stops like `install`.

At step 3 the installer asks for the **Bridge** password, not your Proton account
password. If Bridge is not signed in yet, open another terminal, run Bridge's CLI
(`protonmail-bridge --cli`; the plan prints the exact path), use `login`, then
`info` to see the IMAP username and password, and `exit`. On macOS the Keychain
`security` tool prompts for the password itself. Press Enter to skip the prompt and
rerun the installer later. If the terminal cannot hide what you type, the
installer stops rather than read the password with echo; rerun it in a terminal,
or pass `--skip-credentials` and store the password yourself under the names in
[What goes where](#what-goes-where).

Bridge's CLI and its service cannot run at the same time. `setup` on a terminal can
stop and restart the service for you. To log in by hand, stop the Bridge service first
and start it again afterwards; the plan prints both commands for your platform. The MCP
service keeps running meanwhile: on Linux
its unit only `Wants=` the Bridge unit, so stopping Bridge does not stop it, and
it reaches Bridge again on its next request.

The token is never shown. Copy it into your MCP client's secret storage with:

| Store | Command |
| --- | --- |
| Keychain | `security find-generic-password -s proton-mail-mcp-token -a you@proton.me -w` |
| libsecret | `secret-tool lookup service proton-mail-mcp-token account you@proton.me` |
| pass | `pass show proton-mail-mcp-token/you@proton.me` |
| Any | `proton-mail-mcp show-token`, or `scripts/install.sh show-token` from a checkout |

`show-token` prints only to a terminal and refuses when its output is redirected.
Configure the client with `http://127.0.0.1:8765/mcp` and the header
`Authorization: Bearer <token>`.

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
reads only `PROTON_*` and `PMM_*` settings, refuses a file that contains
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
environment it created and stored credentials, rewrites a file only when its
content changes, and restarts a service only when its definition, the installed
code, the settings or the credentials changed. A failed run can be repeated after
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
| `--bridge-user USER` | Bridge IMAP username; reruns read it from the settings file. |
| `--credential-store libsecret\|pass` | Linux only; default libsecret when `secret-tool` exists, else an initialised `pass`. |
| `--port N` | MCP port on `127.0.0.1` (default 8765). |
| `--bridge-imap-port N` | Bridge IMAP port (default 1143). |
| `--bridge-version X.Y.Z` | Linux `.deb`/`.rpm` release. |
| `--bridge-bin PATH` | Bridge executable for the Bridge service; a relative path is resolved against the current directory. |
| `--venv PATH` | Virtual environment path, new or made by the installer; saved for later runs. The previous one stays. |
| `--source PATH` | The proton-mail-mcp checkout or wheel file to install. Default: see [where the service's copy comes from](#where-the-services-copy-comes-from). |
| `--skip-bridge-login` | `setup` only: do not offer to open Bridge's CLI. |
| `--attachments-dir` | `setup` only: write this folder to the settings file as `PROTON_MCP_ATTACHMENTS_DIR` and create it, mode `0700`. On a terminal, `setup` asks instead, showing the folder in use. |
| `--skip-bridge` | Do not install or verify Bridge. |
| `--skip-bridge-service` | Leave Bridge to its own autostart, for example the desktop app. |
| `--skip-credentials` | Do not prompt or store credentials. |
| `--skip-services` | Install files only; no service is written, stopped or started. Implied on a Linux host that does not run systemd. |
| `--replace-credentials` | Prompt again and generate a new token. |
| `--force` | Replace a file the installer did not write (not the settings file), keeping it in a new `.bak` file beside it; an earlier backup is never replaced. |
| `--simulate FAMILY` | With `--dry-run`, print the plan for another platform (not `windows`). |

`detect` shows the detected platform, default store and paths.

### Moving from a manual LaunchAgent

The README's manual LaunchAgent also uses `com.proton-mail-mcp.plist`, with the
token inside. The installer refuses to overwrite a file it did not write, and
stops before changing anything when launchd already runs that label from another
file or without the installer's file. Unload the old agent (`launchctl bootout gui/$(id -u)/com.proton-mail-mcp`), then either
move the plist away or rerun with `--force`, which keeps it in a new file such as
`com.proton-mail-mcp.plist.k2x9q1.bak` (the run prints the name; an earlier backup
is never replaced). That backup still contains the old token: delete it once the
new agent works, and give clients the new token.

## Uninstall

```sh
proton-mail-mcp uninstall --dry-run
proton-mail-mcp uninstall
```

(`uvx proton-mail-mcp uninstall` with uv, or `scripts/install.sh uninstall` from a
checkout: all three run the same plan.)

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
  Bridge's own settings and cache always stay.

## Tests and CI

`make check` runs the installer's unit tests on Linux without network access:
detection, the plans for every platform (built from simulated hosts, including
the Windows plan kept for later), the executor against temporary directories,
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
`pip` and `uvx`, and proves what each one's `setup` does with that copy.

[`.github/workflows/installer.yml`](../.github/workflows/installer.yml) runs the
same tests and real-host dry runs on GitHub-hosted `ubuntu-latest` and
`macos-latest` (including the bash 3.2 in `/bin/bash`), and in Fedora,
AlmaLinux 9 and Arch containers. Every Linux dry run goes through the service
preflight without `--skip-services`: Ubuntu starts a user manager for the runner
(`loginctl enable-linger`) and checks that the plan sets up the units; the
containers run no systemd and check that the plan says so and sets up none. The `windows-latest` job runs the tests that do
not start the server (those that do are skipped there), round-trips a throwaway
entry through Credential Manager, and checks that `install.ps1` refuses install
and uninstall with status 2 under PowerShell 7 and Windows PowerShell 5.1. Dry
runs install nothing.

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
