# Privacy Policy

Last updated: 2026-11-03

This policy covers proton-mail-mcp: the `mcp-proton-mail` package, its Claude Code plugin and its
Claude Desktop bundle. It is unofficial and not affiliated with Proton AG.

## In short

proton-mail-mcp runs only on your computer and nothing is hosted. It has no telemetry or
analytics, and it sends nothing to its author. It reaches your mail only through Proton Mail
Bridge, and what it keeps stays in files on your computer. Besides Bridge and the sources it
installs from, it connects only to an unsubscribe link you previewed and ran, and mail you send
goes out through Bridge. When `setup` or `configure-client` adds it to a client whose own command
line is installed, it also runs that client's `mcp list`, which checks every server the client
has. [Network connections](#network-connections) lists each one.

What your assistant reads goes to your MCP client, and from there to its model provider under that
provider's terms. Bridge syncs with Proton as it does for any mail app, under Proton's own policy.

## What it reads

- **Your mail,** through Bridge's IMAP: messages, headers, folders, labels and attachments when
  your assistant calls a tool. When the service starts with its search index missing or
  incomplete, it also reads message headers in the background to rebuild it;
  `PROTON_MCP_INDEX_AUTOREBUILD_ENABLED=0` turns that off. The new-mail watcher and background
  sync read in the background too, once you turn them on. The optional Linux rules timer, which
  you install by hand, moves inbox mail by the packaged and saved rules. Its first run starts with
  the mail already in the inbox and works through it in capped batches; after that it takes only
  new mail. Before a move it also reads headers in other folders, to find where else each message
  is filed. Reads mark nothing as seen.
- **Files in the attachments folder,** when a draft attaches them by name.
- **Your Bridge password and proton-mail-mcp's bearer token,** from your system's credential
  store, under the Bridge username in the settings file: Keychain on macOS, Secret Service or
  `pass` on Linux, Credential Manager on Windows. With `pass`, that means your password store and
  GnuPG home.
- **The settings file,** each time the service or `connect` starts.
- **Files you name:** the protected-senders list in `PROTON_MCP_PROTECTED_CONFIG`, which can hold
  addresses; Bridge's certificate in `PROTON_BRIDGE_CERT`, which sending needs unless you set
  `PROTON_BRIDGE_SMTP_ALLOW_UNVERIFIED=1`; and a token file you give `connect --token-file`.

## What it stores, and where

On Linux and macOS each file that holds your data, settings or logs is created readable by your
account alone. Folders above them that did not exist yet, such as `~/.local/state`,
`~/.cache/proton-mail-mcp` or `~/Library/LaunchAgents`, can be readable by others, and so can the
systemd units, which hold only paths and the port. On Windows the server refuses a file or folder
of its own that anyone but you, SYSTEM and Administrators can open.

| What | Linux | macOS | Windows |
| --- | --- | --- | --- |
| Settings: your Bridge username (an address), the credential store and its `pass` locations, hosts and ports, where your data and the service's copy are, the approval-form switch, and any setting you add, such as your own addresses in `PROTON_MCP_SEND_FROM`. No password or token: a settings file that holds one is refused | `~/.config/proton-mail-mcp/proton-mail-mcp.env` | `~/Library/Application Support/proton-mail-mcp/proton-mail-mcp.env` | `%LOCALAPPDATA%\proton-mail-mcp\proton-mail-mcp.env` |
| Action and audit log, saved rules, the rules timer's state, search and identity indexes, their lock files, installer notes | `~/.local/state/proton-mail-mcp/` | `~/.local/state/proton-mail-mcp/` | `%LOCALAPPDATA%\proton-mail-mcp\state\` |
| Saved attachments | `~/.local/share/proton-mail-mcp/attachments/` | `~/.local/share/proton-mail-mcp/attachments/` | `%LOCALAPPDATA%\proton-mail-mcp\attachments\` |
| Service logs | `journalctl --user -u proton-mail-mcp` and `-u proton-mail-mcp-bridge`; without systemd, `service.log`, `bridge.log` and their `.pid` files in the state folder | `~/Library/Logs/proton-mail-mcp/` | `%LOCALAPPDATA%\proton-mail-mcp\logs\mcp.log` |
| The service's own copy of the package, a virtual environment | `~/.local/share/proton-mail-mcp/venv/` | `~/Library/Application Support/proton-mail-mcp/venv/` | `%LOCALAPPDATA%\proton-mail-mcp\venv\` |
| Service definitions: one for the service and, unless setup ran with `--skip-bridge-service`, one for Bridge. They hold paths, and on Linux and Windows the port; the Windows tasks also hold your Windows user name | `proton-mail-mcp.service` and `proton-mail-mcp-bridge.service` in `~/.config/systemd/user/` | `com.proton-mail-mcp.plist` and `com.proton-mail-mcp.bridge.plist` in `~/Library/LaunchAgents/` | Scheduled tasks named with your Windows user name, and their definitions in `%LOCALAPPDATA%\proton-mail-mcp\tasks\` |
| The optional rules timer, which you install and create by hand: its two units, and `rules.env`, which holds your Bridge username (an address) and the settings the timer needs | `proton-mail-mcp-rules.service` and `proton-mail-mcp-rules.timer` in `~/.config/systemd/user/`, and `~/.config/proton-mail-mcp/rules.env` | Not used | Not used |
| Bridge downloads on Debian, Ubuntu and Fedora: Proton's signing key, on Debian and Ubuntu its debsig policy, a marker file, and the temporary folders the signature check leaves; the Bridge package goes once installed | `~/.cache/proton-mail-mcp/installer/` | Not used | Not used |

- **Your system's credential store** holds the Bridge password as `proton-mail-mcp` and the bearer
  token as `proton-mail-mcp-token`, each under your Bridge username, an address. With `pass` they
  are the files `proton-mail-mcp/<address>.gpg` and `proton-mail-mcp-token/<address>.gpg` in your
  password store, so the address is a file name there. If that store is a git repository, `pass`
  commits each change, and the encrypted entries stay in its history after uninstall removes them.
- **The search index** holds the subject, sender, To and Cc of the messages it indexed, with
  their message IDs, dates and sizes, so that search is fast; no body text. **The identity
  index** holds message IDs per folder.
- **Saved rules** keep each rule's name and conditions as you wrote them, which can include
  addresses and subject words.
- **The action and audit log** records each change and each write call: the tool, its outcome,
  counts, the client's name, and the folders and message IDs that undo needs. Subjects, senders
  and rule text appear only as hashes, and a sent message's record keeps only the recipients'
  domains.
- **proton-mail-mcp's own logs and the audit log** never hold a token, a password, a mail body,
  a subject or a full address. On Linux and macOS the service logs also capture Bridge's own
  output, and what that holds is up to Bridge. The systemd journal keeps the Linux logs under its
  own access rules, which also let groups such as `systemd-journal` or `adm` read them. Approval
  tokens are never written to disk: the service hands each one to your client and keeps only its
  hash, in memory and in the log.
- **`setup --client` and `configure-client`** add an entry to the client's config file, and each
  change leaves a dated `.bak` copy of the file as it was. A copy holds the whole config, which can
  include other servers' secrets. An entry for a client that connects over HTTP instead of through
  `connect` holds the bearer token, and so does each `.bak` copy taken after that entry was added.
  The plugin and the bundle change no config.
- **`setup --force`** keeps a service definition you wrote yourself as a `.bak` file beside it. A
  LaunchAgent you wrote by hand can hold an old bearer token.
- **`show-token --copy`** puts the bearer token on your clipboard. Other programs can read it
  there, and clipboard history or sync can keep a copy after it is replaced.

`PROTON_MCP_ACTION_LOG` moves the log, and the search index, saved rules and the rules timer's
state with it; `PROTON_MCP_SAVED_RULES`, `PROTON_MCP_IDENTITY_INDEX_DIR` and
`PROTON_MCP_ATTACHMENTS_DIR` move the rest. `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`
and `XDG_CACHE_HOME` move the Linux folders, `XDG_STATE_HOME` and `XDG_DATA_HOME` the macOS state
and attachments folders, and `LOCALAPPDATA` the Windows ones. `setup --venv` puts the service's
copy elsewhere, and an earlier copy stays where it was.

Nothing expires: the action log, saved rules, attachments and log files stay until you delete
them. The indexes drop entries for mail that leaves a folder they still read, and are rebuilt
when out of date. Entries for a folder you deleted, and an index kept for an earlier Bridge
username, host or port, stay until you delete them.

## Deleting it

1. If you installed the rules timer, which uninstall leaves in place, turn it off with
   `systemctl --user disable --now proton-mail-mcp-rules.timer`, delete
   `proton-mail-mcp-rules.service` and `proton-mail-mcp-rules.timer` from `~/.config/systemd/user/`,
   and run `systemctl --user daemon-reload`.
2. Run `uvx mcp-proton-mail uninstall --remove-credentials`. It stops the services and removes
   their definitions, then the settings file once it has proved the service stopped, deletes the
   Bridge password and the bearer token from the credential store, and lists where your data is
   and each virtual environment it leaves. On Linux without systemd it stops nothing: run the
   `kill` command it prints for each process setup started, then remove the settings file with the
   command it prints. `--remove-bridge` also uninstalls Bridge; Bridge's own settings and mail cache
   stay, and so do debsig-verify and GnuPG if setup installed them.
3. Delete the folders: on Linux `~/.config/proton-mail-mcp`, `~/.local/state/proton-mail-mcp`,
   `~/.local/share/proton-mail-mcp` and `~/.cache/proton-mail-mcp`; on macOS
   `~/.local/state/proton-mail-mcp` and `~/.local/share/proton-mail-mcp`, and `proton-mail-mcp` in
   `~/Library/Application Support`, `~/Library/Logs` and `~/Library/Caches`; on Windows
   `%LOCALAPPDATA%\proton-mail-mcp`. Also delete any virtual environment uninstall listed outside
   these, any folder a setting above moved data to, and any `.bak` file `setup --force` left in
   `~/.config/systemd/user/` or `~/Library/LaunchAgents/`. On Debian, Ubuntu and Fedora, setup's
   key check runs GnuPG, which creates `~/.gnupg` if you had none; delete it if nothing else uses
   it. The systemd journal expires its logs by its own settings.
4. Remove the `proton-mail` entry and its `.bak` copies from each client's config, uninstall the
   plugin or bundle in its client, and run `uv cache clean mcp-proton-mail`.

## Network connections

**While it runs:**

- Your MCP client starts `proton-mail-mcp connect`, which relays to the service at
  `http://127.0.0.1:PORT/mcp`. The service listens only on `127.0.0.1`. If you put Tailscale Serve
  in front of it and set `PROTON_MCP_PUBLIC_HOST`, it also accepts requests through that.
- The service reaches Bridge's IMAP on `127.0.0.1` with STARTTLS. If `PROTON_BRIDGE_HOST` names
  another host, IMAP goes there over TLS with a verified certificate.
- Sending is off until you set `PROTON_MCP_ENABLE_SEND=1`, and then uses Bridge's SMTP on
  `127.0.0.1` or `::1` only, even with a remote Bridge.
- An HTTPS unsubscribe link is opened only when you run an unsubscribe you previewed, which names
  each link's host, and, unless you set `PROTON_MCP_CONFIRM_MUTATIONS=0`, after you approve it in
  your client's form. The link comes from the message's `List-Unsubscribe` header or, failing
  that, from a link in its body on the sender's own domain. It is one request: a POST when the
  header offers one-click unsubscribe (RFC 8058), else a GET, with no cookies and no redirects,
  and only when your system's DNS resolves the host to public addresses alone. A `mailto:`
  unsubscribe only saves a draft, and is refused while sending is off.
- The views load nothing from the network. A link in a message opens only when you click it and
  confirm, and your client opens it.

**Each time a client starts the plugin or the bundle:** `uvx` checks PyPI, or the package index
you configured, for the pinned release and its dependencies, and downloads only what it has not
cached. On the first start, if you have no Python 3.12 or newer, uv also downloads one from the
hosts listed below. A client set up by `setup` runs its own copy.

**Install, setup and uninstall only:**

- PyPI, or the package index you configured, for the package and its dependencies, including
  each `uvx mcp-proton-mail` command such as `uninstall`. If you have no Python 3.12 or newer, uv
  downloads one: uv 0.13.6, the version this project's checks pin, fetches it from
  `releases.astral.sh` and, if that fails, from GitHub. Other uv versions may use other
  hosts, and `UV_PYTHON_INSTALL_MIRROR` names your own.
- `setup`, which the plugin and the bundle never run, installs Bridge. On Debian, Ubuntu and
  Fedora it downloads Bridge, Proton's signing key and, on Debian and Ubuntu, Proton's debsig
  policy from `https://proton.me/download/bridge`, following only HTTPS redirects, then installs
  it with apt or dnf. On Arch, macOS and Windows it uses pacman, Homebrew or winget. Each package
  manager also reaches its own sources, and may report the install under its own settings. On
  macOS, setup then has Gatekeeper check the Bridge app, which may contact Apple.
- `setup --client` and `configure-client`, for a client whose own command line is on your PATH,
  add the entry with that client's `mcp add`, after its `mcp remove` where it needs one, then run
  its `mcp list`. Listing makes the client check every server configured in it, which can reach
  servers on other hosts.
- `uninstall --remove-bridge` removes Bridge with the same package manager, which may reach its
  own sources.
- Adding the Claude Code plugin's marketplace makes Claude Code fetch it from GitHub.

## Sending mail

Each send is a saved draft, to the To, Cc and Bcc recipients in it, through Bridge, after a
preview and a single-use token. You approve it in your client's form. On a client with no form,
the approval is an OK you type in chat, which the server cannot tell apart from one the model
writes. The same holds for a client you list in `PROTON_MCP_SEND_TYPED_OK_CLIENTS`, and
`PROTON_MCP_SEND_TYPED_OK=0` turns both off. With `PROTON_MCP_SEND_SELF_NO_FORM=1`, which
you have to set, a plain note to only your own addresses needs no approval.

## Contact

Questions about this policy: [dmitri@manajev.com](mailto:dmitri@manajev.com). A security problem
goes to [SECURITY.md](SECURITY.md). Changes to this policy are made in this file, and its history
shows each one.
