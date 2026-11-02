# proton-mail-mcp

<!-- mcp-name: io.github.legalaspro/proton-mail-mcp -->

An MCP server that lets an AI assistant read your Proton Mail, write drafts and tidy your
mailbox, with a preview you approve before mail is moved, deleted or sent. It runs on your own
computer, under the same login as [Proton Mail Bridge](https://proton.me/mail/bridge), and listens
only on `127.0.0.1`.

proton-mail-mcp is unofficial and is not affiliated with or endorsed by Proton AG.

## What it does

- **Search and read.** Search and count mail, read whole threads, and read or save attachments.
  Nothing gets marked as read.
- **Write drafts.** Create new drafts, replies and forwards. A draft stays unsent until you send it.
- **Clean up.** Move, archive, trash, mark read or unread, add or remove labels, unsubscribe,
  delete drafts and run folder rules (beta), each after a preview you approve.
- **Send, if you turn it on.** Sending is off by default, and there is no way to send without a
  saved draft and an approval.
- **Show mail in the chat.** Clients that support MCP Apps get a thread view, a composer and a
  send review. Every other client gets the same results as text.

It also keeps a private local index of mail headers so that searches stay fast. The index holds
no message bodies.

## How it keeps your mail safe

**Reading changes nothing.** Search and read open mailboxes read-only and never mark mail as seen.

**Changes to your mail take two steps.**

1. **Preview.** The assistant asks what a change would do, and nothing changes yet. It shows you
   the count, samples and target, and gets a **token**: single use, short-lived and bound to
   exactly those messages. Before acting, the server checks them again and refuses if anything
   differs. New mail never slips into an approval.
2. **Confirm.** Your MCP client shows a form with the exact change, and you accept or decline. A
   token alone is not enough, and declining retires it. The form is only as good as the client: a
   client that answers forms automatically defeats this check.

| Action | What it takes | Can it be undone? |
| --- | --- | --- |
| Search, count, read, list | Nothing | Nothing to undo |
| Save a draft or an attachment | Nothing; never sends, always logged | Saved items stay until you remove them |
| Move, trash, archive, mark read or unread, labels, folder rules | Preview, token, form | Yes, with `undo_action` |
| Unsubscribe | Preview, token, form | No. With sending on, a mailto target only prepares an unsent draft, which needs its own send approval; with sending off, mailto targets are not used |
| Delete drafts | Preview, token, form | No, it is permanent |
| Create a folder, save a rule | Preview, token, form | No, remove them by hand |
| Rebuild the local search index | Preview, token; no form, because no mail changes | No |
| Send a draft | Off by default; then preview, token, and a form or typed OK | No |
| Create a Sieve filter | Previews only. It always refuses, because Bridge has no way to apply one | Nothing changes |
| Reverse a logged change | `undo_action`: no token or form, once per change, always logged | Not applicable |

Permanent deletion only ever reaches approved messages in Drafts; removing a label expunges only
that label's copy. There is no tool to empty Trash, or to delete or rename a mailbox. Every write
call goes into an append-only audit log, which holds no message bodies, subjects, full addresses or
tokens.

### Sending

Set `PROTON_MCP_ENABLE_SEND=1` to add the send tools, plus `PROTON_MCP_SEND_FROM` (the addresses
you send from) and `PROTON_BRIDGE_CERT` (Bridge's certificate); previews refuse without them.

There is no free-form send: the path is `create_draft`, then `preview_send`, then `send_draft`, and
any edit after the preview refuses. A client with a form shows you one. On a client that declares
no form, the assistant shows you a plain summary and sends after you type OK in chat. **The server
cannot prove that a human typed the OK:** only the assistant reports it, so a misled assistant could
send without asking. `PROTON_MCP_SEND_TYPED_OK=0` turns this path off.
[How approvals work](docs/safety.md#send-approval) has the details.

Mailbox changes have no typed OK. A client without a form is refused, unless you deliberately set
`PROTON_MCP_CONFIRM_MUTATIONS=0`, which lets the token alone authorize a change.

### Protected senders

`PROTON_MCP_PROTECTED_CONFIG` can name a local list of senders that cleanup, unsubscribe, draft
deletion and rules must not touch. **No list ships with the project, so by default mail from a
bank or a friend can be approved like any other.** The list checks who sent the mail being acted
on, never who a draft is addressed to. See
[Protected senders](docs/configuration.md#protected-senders).

### Off by default

| Feature | Setting |
| --- | --- |
| Sending | `PROTON_MCP_ENABLE_SEND=1`, with `PROTON_MCP_SEND_FROM` and `PROTON_BRIDGE_CERT` |
| No form for your own addresses | `PROTON_MCP_SEND_SELF_NO_FORM=1` |
| Typed OK for clients you name | `PROTON_MCP_SEND_TYPED_OK_CLIENTS` |
| Protected senders | `PROTON_MCP_PROTECTED_CONFIG` |
| New-mail watcher | `PROTON_MCP_IMAP_IDLE_ENABLED=1` |
| Background index sync | `PROTON_MCP_BACKGROUND_SYNC_ENABLED=1` |
| Rules timer | A Linux timer you install yourself; no tool can start it |

On by default: the confirmation form, the typed OK once sending is on, the MCP Apps views and
index recovery at startup. [Configuration](docs/configuration.md) has the setting for each.

## Install

You need:

- A paid Proton plan, which Bridge requires.
- Linux (Debian/Ubuntu, Fedora/RHEL or Arch) or macOS, with [Homebrew](https://brew.sh) unless
  Bridge is already installed. Native Windows support is new and has run only on Windows Server
  runners: read [docs/windows.md](docs/windows.md) first, or use WSL2.
- [uv](https://docs.astral.sh/uv/), which brings Python 3.12 or newer if you have none, and git.

> **Not on PyPI yet.** Install from a clone of this repository, as below. Do not run
> `uvx proton-mail-mcp` by name: until the first release is published, anyone could hold that name
> on PyPI. Installing from PyPI, and a listing in the MCP Registry, come later.

**1. Get the code.**

```sh
git clone https://github.com/legalaspro/proton-mail-mcp.git
cd proton-mail-mcp
```

Or unpack an archive of the repository you were given and run the rest there.

**2. Quit the Proton Mail Bridge app** if it runs, and turn off its start-at-login setting. Setup
runs Bridge as a background service, which cannot run beside the app; the app's login carries
over. To keep using the app instead, add `--skip-bridge-service` below.

**3. Run the guided setup in your own terminal**, naming your MCP client:

```sh
uvx --from . proton-mail-mcp setup --client claude-desktop
# or claude-code, chatgpt, codex, gemini, cursor, grok-bot; repeatable
```

It shows what it will do, asks its questions, prints the full plan and changes nothing else
before you approve it (`--dry-run` stops after the plan). The one exception: when Bridge is
installed but setup does not know your account yet, it first offers, at a `[Y/n]`, to open
Bridge's CLI so you log in to Bridge. It checks or installs Bridge, stores the Bridge
password and a new bearer token in your system's credential store, starts per-user services,
checks that the server reads your inbox, and adds the server to your client, keeping a backup of
its config. Most clients start `proton-mail-mcp connect`, which reads the token itself, so their
config holds none. The token is never shown. Rerunning it is safe. If it stops on Debian or Ubuntu
because `python3-venv` is missing, run `sudo apt-get install python3-venv`, or add
`--managed-python` after `uvx`.

Only you can do these, and setup says when:

- **Log in to Bridge**, if it is not logged in yet: setup opens Bridge's CLI, where you type
  `login`, your Proton email and password and the two-factor code, and open any verification link
  it prints in a browser. Pasting works. Setup then reads your IMAP username from Bridge.
- **Type the Bridge password** at a hidden prompt: the one Bridge's `info` (or the Bridge app)
  shows, not your Proton password. That is why setup runs in your own terminal.
- **On macOS, allow Keychain access** when asked: choose Always Allow.
- **Restart your client** at the end, as setup says.

**4. Check it.** Ask your assistant which Proton Mail tools it has; it should name tools such as
`search_mail`.

`uvx --from . proton-mail-mcp configure-client --client <name>` adds another client later, and
[Connect an MCP client](docs/clients.md) has a page for each client and covers the others. An AI
agent setting this up for you follows [llms-install.md](llms-install.md): it does everything else
and hands you only the Bridge login, as one command for your own terminal.

## Documentation

- **New here:** [Getting started](docs/getting-started.md) walks through a first session.
- **Setting up:** [Install](docs/install.md), [Connect an MCP client](docs/clients.md),
  [Configuration](docs/configuration.md) and [Windows](docs/windows.md).
- **Looking something up:** [Tool reference](docs/tools.md) and
  [Results, refusals and limits](docs/results.md).
- **Understanding it:** [How approvals work](docs/safety.md), and the
  [documentation map](docs/README.md) for every page.
- **What changed:** [Changelog](CHANGELOG.md).

On PyPI these relative links do not resolve; read them in the
[repository](https://github.com/legalaspro/proton-mail-mcp/tree/main/docs).

## For AI assistants and coding agents

- **An assistant connected to this server** needs nothing from this repository: the server gives
  it its instructions and tool descriptions when it connects.
- **An assistant reading about this project** can start at [`llms.txt`](llms.txt), an index of
  these docs in Markdown.
- **An assistant installing it for someone** follows [`llms-install.md`](llms-install.md), which
  hands the person the one step only they can do.
- **A coding agent changing this repository** should read [`AGENTS.md`](AGENTS.md).

## License

[Apache-2.0](LICENSE).
