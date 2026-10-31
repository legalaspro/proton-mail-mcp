# proton-mail-mcp

A Python 3.12+ MCP server for Proton Mail Bridge, built with the official Python
[`mcp` SDK / FastMCP v1](https://py.sdk.modelcontextprotocol.io/v1/).
Run it on the same Mac and under the same login account as Bridge. It provides
mail search and counts, thread reading, draft creation, and guarded cleanup over Streamable HTTP at
`http://127.0.0.1:8765/mcp`.

An optional [IMAP IDLE watcher](docs/imap-idle.md) keeps the search index current
as mail arrives or is expunged. It is off by default and never takes actions on mail.

Clients that support MCP Apps render [interactive views](docs/mcp-apps.md) for
some tools; every view tool still returns its ordinary text result for other
clients. Views load nothing from the network and never hold approval tokens.

**Sending is off by default; send tools are absent unless explicitly enabled.**
Guarded send requires a saved draft and preview token, with user confirmation through
the client's elicitation form unless the own-address-only token approval below applies.
A client whose `initialize` omits elicitation takes a [typed OK](#typed-ok-when-the-client-has-no-form)
instead, which the server cannot verify, unless `PROTON_MCP_SEND_TYPED_OK=0` refuses it.
So does a client the operator names in `PROTON_MCP_SEND_TYPED_OK_CLIENTS`, which is empty by default.
Search and read use read-only
mailboxes and `BODY.PEEK`; they do not mark messages as seen. Draft creation only
appends a new message to Drafts with the `\Draft` flag. Draft deletion, cleanup, rules, saving custom
rules, unsubscribe, folder creation and Sieve filter creation require a separate
read-only preview, a single-use token bound to the exact previewed action set and, by
default, the user's confirmation in the client's form. **Sieve filters can be
previewed but not applied:** Proton Mail Bridge has no Sieve or filter interface, so
`create_sieve_filter` always refuses without changes and no setting enables it; see
[Sieve filters](#sieve-filters).
Every write tool call is recorded in an append-only audit log. Permanent deletion is
limited to approved messages in Drafts through `delete_draft`; it cannot be undone.
There is no empty Trash or mailbox deletion/rename tool. Label removal also uses
targeted expunge in its `Labels/` mailbox, as explained below.
Read tools list attachment metadata; [attachment tools](#attachments) read and save
single attachments within size caps, and local files only in the one folder you allow.
Guarded send reads complete drafts and attachments.

This repository supplies code and an example LaunchAgent. Building or testing
the repository does not install anything on a Mac or connect to real mail.
The optional [installer](docs/install.md) (`proton-mail-mcp setup`, or
`scripts/install.sh` from a checkout, for Linux and macOS) sets up Bridge,
credentials and services only when you run it, and `--dry-run` prints its plan
first. Windows is not yet supported: the server's state storage needs POSIX file
locking, so `setup` and `scripts/install.ps1` refuse to install and point to WSL2;
native support is planned for v1.1.

## Quick start

With [uv](https://docs.astral.sh/uv/) on macOS, Windows or Linux, install and run
in one command. uv supplies a compatible Python automatically; no separate Python
installation is needed. On Windows, run uv and the server inside WSL2 because
native Windows support is not yet available.

```sh
uvx proton-mail-mcp
```

Configure Bridge and authentication before starting the server. For guided setup
on macOS or Linux (Debian/Ubuntu, Fedora/RHEL, Arch), including Linux in WSL2, run:

```sh
uvx proton-mail-mcp setup
```

`setup` shows its plan and asks first. It checks for Bridge or installs it, offers
Bridge's login, stores the Bridge password from a hidden prompt, installs per-user
services and runs a self-check. See the
[installer guide](https://github.com/legalaspro/proton-mail-mcp/blob/main/docs/install.md),
which also explains how to install from a local wheel until the first PyPI release.

proton-mail-mcp is unofficial and is not affiliated with or endorsed by Proton AG.

## Install and run on your Mac

These are the manual steps. To automate them on macOS or Linux (Debian/Ubuntu,
Fedora/RHEL, Arch), including Bridge, the credential store and per-user
services, use the quick start above and the [installer guide](docs/install.md).

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
environment fallback for offline tests or environments without macOS Keychain.
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

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROTON_MCP_TOKEN` | Required | Bearer token, at least 32 printable ASCII characters with no whitespace. |
| `PROTON_MCP_HOST` | `127.0.0.1` | HTTP bind address. Only the literal `127.0.0.1` is accepted. |
| `PROTON_MCP_PORT` | `8765` | HTTP port. |
| `PROTON_MCP_PUBLIC_HOST` | Unset | Exact Tailscale Serve hostname allowed by HTTP Host/Origin checks; no scheme, port, or path. Does not change the bind address. |
| `PROTON_BRIDGE_USER` | Required | Bridge username and Keychain (or credential store) account. |
| `PROTON_BRIDGE_PASSWORD` | Unset | Password fallback for tests; prefer Keychain on macOS. |
| `PROTON_MCP_CREDENTIAL_STORE` | `keychain` | Where the Bridge password is read when `PROTON_BRIDGE_PASSWORD` is unset: `keychain` (macOS), `libsecret` (`secret-tool`) or `pass` (Linux), or `wincred` (Windows Credential Manager, for when Windows is supported). Service `proton-mail-mcp`, account `PROTON_BRIDGE_USER`; see the [installer guide](docs/install.md). |
| `PROTON_MCP_PASSWORD_STORE_DIR` | Unset | Absolute `pass` store directory, passed to `pass` as `PASSWORD_STORE_DIR`; unset uses the process environment or `~/.password-store`. The installer writes it for `pass`. |
| `PROTON_MCP_GNUPGHOME` | Unset | Absolute GnuPG home passed to `pass` as `GNUPGHOME`; unset uses the process environment or GnuPG's default. |
| `PROTON_BRIDGE_HOST` | `127.0.0.1` | Bridge IMAP hostname or address. |
| `PROTON_BRIDGE_IMAP_PORT` | `1143` | Bridge IMAP port, using STARTTLS. |
| `PROTON_BRIDGE_CERT` | Unset | PEM Bridge certificate to pin. Required for send unless SMTP verification is explicitly disabled; optional for existing IMAP operations. |
| `PROTON_BRIDGE_DRAFTS_FOLDER` | `Drafts` | Exact IMAP mailbox name used for new drafts. |
| `PROTON_BRIDGE_TIMEOUT` | `30` | IMAP socket timeout in seconds; greater than zero and at most 300. |
| `PROTON_MCP_CALL_TIMEOUT` | `120` | Mailbox call budget in seconds, including the upper limit for cleanup execution and undo; greater than zero and at most 300. Search and count cap this at 20 seconds, and thread reads at 15 seconds; those tools also cap each IMAP command at 10 seconds. Each command's socket timeout and watchdog use the remaining budget, reserving time for connection cleanup and the response. Slowly trickling responses cannot extend a command or call deadline. |
| `PROTON_MCP_EXECUTE_TIMEOUT` | Call budget (`120`) | Additional cleanup execution and undo limit in seconds; greater than zero and at most 300. The effective budget is the smaller of this value and `PROTON_MCP_CALL_TIMEOUT`. |
| `PROTON_MCP_PREVIEW_TIMEOUT` | Call budget | Optional shorter cleanup preview budget, greater than zero and no greater than `PROTON_MCP_CALL_TIMEOUT`. Larger values are rejected at startup. `preview_rules` additionally caps each call at 20 seconds. |
| `PROTON_MCP_CLEANUP_TOKEN_TTL` | `600` | Preview lifetime in seconds; greater than zero and at most 1800 (30 minutes). Starting cleanup or `apply_rules` establishes a separate, fixed 30-minute resume deadline. |
| `PROTON_MCP_CONFIRM_MUTATIONS` | `1` | Exact `1` or `0`. `1` requires the user's confirmation in the client's form before a cleanup, rules, rule-saving, unsubscribe or folder token is redeemed; `0` is an explicit opt-out to token-only redemption for hosts without form elicitation. See [Confirmation and approval binding](#confirmation-and-approval-binding). |
| `PROTON_MCP_CONFIRM_TIMEOUT` | `120` | Seconds a confirmation form may stay unanswered; positive and at most 300. |
| `PROTON_MCP_APPS_ENABLED` | `1` | Exact `1` or `0`. `1` registers the [MCP Apps views](docs/mcp-apps.md): `ui://` resources and their view tools. `0` restores the plain tool list. |
| `PROTON_MCP_PROTECTED_CONFIG` | Unset | Path to a local JSON policy of [protected senders](#protected-senders), an optional operator block list, off by default. It checks cleanup senders, not a draft's recipients. Personal addresses belong in this uncommitted file. |
| `PROTON_MCP_ACTION_LOG` | User state directory | Optional absolute path for the append-only action and audit JSONL log; see the log section below. |
| `PROTON_MCP_SAVED_RULES` | `saved-rules.json` beside the action log | Optional path of the private [saved custom rules](#saved-custom-rules) store that `add_rule` appends to and the rules engine and timer read. |
| `PROTON_MCP_ATTACHMENTS_DIR` | `$XDG_DATA_HOME/proton-mail-mcp/attachments` if `XDG_DATA_HOME` is absolute, else `~/.local/share/proton-mail-mcp/attachments` | Absolute path of the one folder that [attachment tools](#attachments) may use for local files. It must be a real directory, not a symbolic link, and not writable by everyone. The path must be normalized, with no `.` or `..` steps and no trailing slash. A folder you set must exist already; the default one is created with mode `0700` on the first save, and an existing one keeps its mode. `setup` shows the path in use and can change it. |
| `PROTON_MCP_IDENTITY_INDEX_ENABLED` | `1` | Reuse a certified identity inventory when the server supplies complete CONDSTORE evidence; `0` retains full discovery scans. |
| `PROTON_MCP_IDENTITY_INDEX_DIR` | User state directory | Optional identity-index directory; defaults to `$XDG_STATE_HOME/proton-mail-mcp/identity-index` or `~/.local/state/proton-mail-mcp/identity-index`. |
| `PROTON_MCP_SEARCH_INDEX_CATCHUP_MAX_MESSAGES` | `2000` | Maximum missing All Mail headers to catch up during search or optional IDLE updates; also caps combined source/target metadata fetches for folder mapping. Integer from 0 to 10000; `0` disables these optional fetches. Catch-up preserves time for the requested search and reconciles expunges. |
| `PROTON_MCP_INDEX_AUTOREBUILD_ENABLED` | `1` | Exact `1` or `0`. Recover a discarded, empty or incomplete header index in the background at server startup; independent of `PROTON_MCP_BACKGROUND_SYNC_ENABLED`. Set `0` to disable [automatic recovery](docs/search-index.md#automatic-startup-recovery). |
| `PROTON_MCP_REINDEX_BATCH_SIZE` | `100` | Header UIDs fetched per batch by automatic recovery and the manual background All Mail and real-folder search-index rebuild; integer from 1 to 200. |
| `PROTON_MCP_REINDEX_BATCH_SLEEP` | `0.25` | Seconds to pause between background rebuild batches; from 0.01 to 60. Each batch also retains the normal Bridge timeout. |
| `PROTON_MCP_IMAP_IDLE_ENABLED` | `0` | Exact `1` or `0`. Enable the read-only [new-mail watcher](docs/imap-idle.md) at server startup. |
| `PROTON_MCP_IMAP_IDLE_FOLDERS` | `["INBOX"]` | JSON array of 1–8 distinct, nonempty IMAP folder names. Commas and Unicode names are supported; control characters are refused. |
| `PROTON_MCP_IMAP_IDLE_POLL_INTERVAL` | `60` | Seconds between NOOP/inventory checks only when Bridge lacks IDLE; from 30 to 300. |
| `PROTON_MCP_BACKGROUND_SYNC_ENABLED` | `0` | Exact `1` or `0`. Enable the read-only periodic [background All Mail sync](docs/search-index.md#background-all-mail-sync) at server startup. Off disables that periodic worker; automatic startup recovery is controlled separately. It stays inactive while `PROTON_MCP_IMAP_IDLE_ENABLED=1`. |
| `PROTON_MCP_BACKGROUND_SYNC_INTERVAL` | `900` | Seconds between its wakes, plus up to 10% jitter; from 300 to 3600. |
| `PROTON_MCP_BACKGROUND_SYNC_MAX_COMMANDS` | `1500` | Its IMAP commands per UTC day; integer from 100 to 5000. |
| `PROTON_MCP_UNSUBSCRIBE_MAX_BATCH` | `50` | Unsubscribe batch limit; integer from 1 to 50. Cannot raise the hard cap. |
| `PROTON_MCP_RULES_MAX_MOVES` | `50` | Optional timer's per-run move cap; integer from 1 to 200. Does not enable the timer. |
| `PROTON_MCP_APPROVAL_MAX_MESSAGES` | `500` | Hard upper bound for one [bounded cleanup approval](#bounded-approvals), requested with a `preview_action` `limit` above 200; integer from 200 to 1000. Every cleanup preview except unsubscribe and draft deletion reports it as `approval_max_messages` and a bounded token binds it. `200` disables bounded approvals. |

See [Guarded send](#guarded-send) for its opt-in configuration.

Guarded cleanup confirmation (move, trash, archive, mark, label and manual rules)
uses `min(PROTON_MCP_CALL_TIMEOUT, PROTON_MCP_EXECUTE_TIMEOUT, 48 seconds)`.
`EXECUTE_RESPONSE_BUDGET_S = 48` includes the existing response reserve: 10% of
the effective budget, at most five seconds. The reserve formula is unchanged.
At the 48-second ceiling, mailbox I/O stops by 43.2 seconds, leaving 4.8
seconds for cleanup and serialization within the ceiling, two seconds
below the tested 50-second response bound. The one-second journal reserve and
the label actions' 2.0/2.05-second per-message mutation allowances remain
unchanged; moves and read-state changes are budgeted per command instead (see
[Batched moves and read-state changes](#batched-moves-and-read-state-changes)).
Choosing 48 preserves existing configured 48-second chunk capacity. Lower configured limits
still win. Keep the client's timeout above the server budget with transport margin.
Undo retains `min(PROTON_MCP_CALL_TIMEOUT, PROTON_MCP_EXECUTE_TIMEOUT)` (120
seconds by default); use at least 150 seconds for that default. Raising only
`PROTON_MCP_EXECUTE_TIMEOUT` cannot extend a call beyond `PROTON_MCP_CALL_TIMEOUT`.
A call stops with partial progress when the remaining work no longer fits. If
`resume` is true, call the same tool with the same `target`, `state` or `op` and
`continuation_token` to continue the same approval. There is no background continuation. Each continuation contains only
unchanged, unexecuted approved handles. The first successful token claim starts
a fixed 30-minute resume window for cleanup and `apply_rules`; the response's
`expires_at` reports that deadline. Later continuations never renew it, so a batch
can cross its preview expiry without losing its approved remainder. For
`move_to_folder` approvals, the original token or latest received continuation also retries
that same remainder after a lost response;
other cleanup actions retain single-use rotation. Use read-only `list_actions`
to inspect progress and continuation availability.

An execution budget below the general call budget is accepted. Preview checks
both budgets before issuing approval, using observed resolution time to estimate
confirmation work in bounded chunks and reports `estimated_calls`. A large set
need not fit one confirmation. Execution checks actual remaining time again; server delays
between calls can still cause a safe refusal or partial completion.

### Bridge TLS

STARTTLS is always required before authentication. To accommodate Bridge's
self-signed certificate, certificate verification can be relaxed only for a
literal loopback address, or `localhost` when all resolved addresses are
loopback addresses. The server connects to the resolved numeric loopback
address. Other hosts use normal certificate and hostname validation.

For stronger local certificate checking, export Bridge's certificate as a PEM
file and set `PROTON_BRIDGE_CERT` to its absolute path. This restricts the trust
roots to that file and verifies that the presented certificate exactly matches
the configured certificate. Hostname checking is disabled for local connections
because Bridge's certificate may not identify the loopback IP; certificate chain
verification still applies. A remote host retains hostname checking. Update
the pin if Bridge regenerates its certificate. Protect the file and use the
default loopback Bridge connection on the owner's Mac.

## Run at login with launchd

Use [`examples/com.proton-mail-mcp.plist`](examples/com.proton-mail-mcp.plist) as a
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

## Tools

The server exposes 30 tools by default, or 33 with sending enabled. With
`PROTON_MCP_APPS_ENABLED=0` it exposes 28, or 30 with sending enabled, without
the [MCP Apps view](docs/mcp-apps.md) tools.

New mail arriving during a tool call is out of scope for that call and is picked
up on the next request. Request mail again when you need newer mail.

**Beta: folder rules.** The rules engine is not fully reliable yet; preview first
and check the results. This applies to `preview_add_rule`, `add_rule`, `list_rules`,
`preview_rules` and `apply_rules`.

| Tool | Behavior |
| --- | --- |
| `search_mail(from_, to, subject, text, since, before, folder="INBOX", limit=20, cursor=None, include_snippets=True, unread_only=False, exclude_subject=None, exclude_text=None, query=None)` | Returns `{messages, next_cursor, total, partial, total_is_exact}` and `partial_reason` when partial. Results limited by incomplete index coverage also carry `index_note` and `index_status` progress annotations. See [Local header search index](#local-header-search-index). Messages include Message-ID, the [canonical](#canonical-message-handles) `folder`, `uidvalidity` and `uid`, an `alias` when found through a view, sender, recipients, subject, date and `seen` (the IMAP `\Seen` flag). Dates use `YYYY-MM-DD`; `since` is inclusive and `before` exclusive. Limits above 100 are capped at 100. `unread_only`, `exclude_subject`, `exclude_text` and the `and`/`or`/`not` `query` are described in [Unread mail, exclusions, queries and counts](#unread-mail-exclusions-queries-and-counts). |
| `count_mail(from_, to, subject, text, since, before, folder="INBOX", unread_only=False, exclude_subject=None, exclude_text=None, query=None)` | Read-only `{folder, count}` for the same filters, computed by IMAP `UID SEARCH` alone: no headers or bodies are fetched. Exact IMAP counts do not depend on index coverage and omit `index_note` and `index_status`. Bridge matches every condition, including raw `From`, `To` and `Subject` text; see [Unread mail, exclusions, queries and counts](#unread-mail-exclusions-queries-and-counts). |
| `reindex(op, token=None, run_id=None)` | Changes only the local index, never mail. `op="preview"` (no other argument): All Mail header coverage, missing count, estimated rebuild duration, `log_id`, and a single-use token valid for five minutes; during a rebuild, returns `state="running"` and its `run_id` without another inventory or token. `op="execute"` (`token` only): redeems that token to start one background header rebuild of All Mail and LIST-vetted real folders; returns `log_id` and `run_id`. Existing searches keep using the old index. A running rebuild refuses another start with `"already running"`, `state="running"` and its `run_id`. `op="cancel"` (`run_id` only): requests cooperative cancellation of that active rebuild; discards its candidate and preserves the old index. Any other combination refuses. Every call, including a preview, is audited. |
| `reindex_status()` | Current indexed All Mail count/max UID, last observed All Mail inventory, incremental sync times, automatic recovery progress (`state="rebuilding"` only while queued or running), and manual `last_full_run` counters. Progress includes `indexed_count`, `target`, `percent` and observed-rate `eta_seconds` (`null` until enough rate samples). While the optional [background sync](docs/search-index.md#background-all-mail-sync) is enabled, also its `background_sync` block. Does not start work or contact Bridge. |
| `read_thread(message_id, include_thread=True)` | Returns `{messages, partial}` with the canonical `folder`, `uidvalidity` and `uid` when resolved, an `alias` for the source mailbox handle read, headers, capped text bodies, attachment filename, type and size (encoded bytes, as the result's `size_note` says) and a per-message `truncated` flag. A read limited by incomplete index coverage includes `index_note` and `index_status`; a read fully resolved through live IMAP omits them. Set `include_thread=False` to read only the requested message and skip canonical lookup, leaving every message's canonical fields null, including direct real-folder reads. Prefers plain text, with stripped HTML as a fallback. |
| `show_thread(message_id, include_thread=True)` | The same bounded read as `read_thread`, preferring HTML parts, for showing mail to the user. Returns `read_thread`'s shape as text (HTML bodies converted to text, plus a hexadecimal `view_ref` per message) and, for [MCP Apps](docs/mcp-apps.md) clients, a read-only thread view with sanitized HTML in a sandboxed frame; remote images, styles and scripts are blocked. The text carries `size_note`, and the view marks each attachment's size `encoded`. |
| `compose_reply(message_id=None, draft_id=None, reply_all=False, to=None, cc=None, subject="", body="", attachments=None, from_address="")` | Read-only: reads one message (or reopens a saved draft) and returns prefilled reply fields: To (Reply-To, else From), optional reply-all Cc without this account's addresses, `Re:` subject, `in_reply_to` and the quoted body, with each run of empty `>` lines collapsed to one. With neither id it reads nothing and returns a new draft's fields, blank or prefilled with the given `to`, `cc`, `subject` and `body`. The [MCP Apps](docs/mcp-apps.md) composer lets the user edit them and saves each version as an unsent draft through `create_draft`; it never sends or deletes. A reopened draft keeps its attachments, listed in `attached`; a reply only offers the original's, unless that is a saved draft. `attachments` adds files from the allowed folder in any mode. In `attached`, `size_encoded` marks a saved message's encoded bytes and any other size is a local file's decoded bytes, as `size_note` says. A reopened draft's Bcc recipients, which `create_draft` cannot write, are listed in `not_copied` with a warning that the new draft leaves them out. |
| `list_attachments(message_id)` | Read-only: each attachment's `index`, `filename`, `type` and `size` (transfer-encoded bytes, as the result's `size_note` says), from one bounded message read. Downloads nothing. See [Attachments](#attachments). |
| `read_attachment(message_id, attachment, offset=0)` | Read-only: text from a PDF, TXT or CSV, in pieces of 65,536 characters (`next_offset` continues), or an image as image content. At most 10 MiB, images 5 MiB. |
| `save_attachment(message_id, attachment, filename="")` | Saves one attachment, at most 25 MiB, as a new private file in `PROTON_MCP_ATTACHMENTS_DIR`. Never overwrites and never writes anywhere else. Audited, without file names or content. |
| `list_from_addresses()` | Read-only, no mailbox access: the account's `default` From address and the `allowed` list that `from_address` and guarded send accept. See [Sending from an alias](#sending-from-an-alias). |
| `forward_message(message_id, to, cc=None, *, attachments, note="", from_address="")` | Saves an unsent draft forwarding one message, with only the attachments you list by index (`[]` for none). It never sends: `preview_send` and `send_draft` send the returned `draft_id`, with the host form, or a [typed OK](#typed-ok-when-the-client-has-no-form) when the client has none. See [Forwarding](#forwarding). |
| `create_draft(to, cc=None, *, subject, body, in_reply_to=None, attachments=None, attachments_from="", attachment_indexes=None, from_address="")` | Appends a new draft with `\Draft` and returns its Message-ID. For replies, sets `In-Reply-To` and `References`. `attachments` names files inside the allowed folder; `attachments_from` with `attachment_indexes` copies attachments of a saved draft or message. See [Attachments](#attachments). `from_address` writes it from an [allowed alias](#sending-from-an-alias). |
| `review_send(draft_id)` | Available only with send enabled and views on. Runs every `preview_send` check and returns the same preview plus the draft's text, but **no token**; its [MCP Apps view](docs/mcp-apps.md) can only ask the assistant to run `preview_send` and `send_draft`, which keep their approval path. |
| `preview_send(draft_id)` | Available only with send enabled. Reads the saved draft; returns sender, recipients, subject, attachment names, types and decoded sizes, canonical hash, caps, `approval` path, `action_hash` and a single-use token; earlier live tokens for that draft are retired. On a client whose `initialize` omits elicitation, or that `PROTON_MCP_SEND_TYPED_OK_CLIENTS` lists, `approval` is `typed_ok` and it adds a plain-text `summary` to show the user, which says this app has no confirmation form, so type OK to send. |
| `send_draft(token, draft_id=None)` | Available only with send enabled. Requires user acceptance in the client's elicitation form, unless the preview reported `approval: "token"` ([own-address-only](#own-address-only-token-approval)) or `typed_ok` ([typed OK](#typed-ok-when-the-client-has-no-form), which needs `draft_id`), then rechecks the draft and sends one message. `draft_id` can only refuse: a token from another draft refuses. Refuses a draft already sent or possibly sent. Cannot be undone. |
| `preview_create_folder(name)` / `create_folder(token)` | Preview an exact `Folders/` name, then create it after approval and the user's confirmation in the client's form. An existing folder is a successful no-op without issuing or consuming a token. |
| `preview_sieve_filter(name, folder, conditions, match="all", mark_read=False)` | Read-only: validates one Proton Sieve filter for new mail (1 to 10 `from`/`to`/`subject` conditions, an existing real `Folders/` target, optional mark read) and returns the exact script, `script_sha256`, the `apply` backend status and a single-use token bound to that script. See [Sieve filters](#sieve-filters). |
| `create_sieve_filter(token)` | Adds exactly the previewed script as one new filter through the apply backend, after the user's confirmation in the client's form; never replaces, edits or removes filters. **It always refuses before any form, without changes**: Proton Mail Bridge offers no supported apply path. |
| `preview_add_rule(name, folder, conditions, match="all", mark_read=False, include_matches=False)` | Read-only: validates one custom folder rule with the Sieve filter model and returns the normalized rule, `rule_sha256`, the informational Sieve `script`, the `apply` status, optional `matches` (a bounded, read-only INBOX count with samples) and a single-use token. See [Saved custom rules](#saved-custom-rules). |
| `add_rule(token)` | Appends exactly the previewed rule to the saved rules store after the user's confirmation in the client's form; never replaces, edits, reorders or removes rules. Mail moves only when rules run. |
| `list_rules()` | Read-only: every folder rule in the order rules run, `built_in` (the packaged file) then `saved`, with its target. No mailbox access. |
| `preview_action(action, query=None, ids=None, params=None, limit=None)` | Read-only preview: `{count, samples, blocked, token, expires_at, action_hash, action, target}` plus completion information, `approval_max_messages` and `folder_creation` where applicable. Moves, archive, draft deletion and unsubscribe also return `log_id` for the preview record. Samples contain at most 10 messages with `from`, `subject`, `date`, the canonical `folder`, `uidvalidity` and `uid` (plus `alias` for a label or All Mail source), `message_id` (also `id` for compatibility). Supply exactly one of `query` or `ids`. A `limit` above 200 requests a [bounded approval](#bounded-approvals); `delete_draft` accepts no `limit` or nonempty `params`. `move_to_folder` accepts `params.create_folder_if_missing=true` to [create a missing destination in the same approval](#creating-the-destination-in-the-same-approval). The `action` names the tool that redeems the token: `move` for `move_to_trash`, `archive` and `move_to_folder`; `set_read_state` for `mark_read` and `mark_unread`; `label` for `add_label` and `remove_label`; `delete_draft` and `unsubscribe` under their own names. |
| `delete_draft(token)` | Permanently deletes only the approved messages still in Drafts, after rechecking the whole batch. Requires UIDPLUS; uses UID STORE `\Deleted` and UID EXPUNGE for only the targeted UIDs. Returns `log_id`, `count`, `messages`, `partial`, `token_consumed` and `undo_supported: false`; cannot be undone. |
| `move(target, token, verbose=False, limit=None)` | Moves the approved batch to the destination its token pins, with the read-state option bound in the preview. `target` states that destination as the preview's `target` reports it: `"Trash"` or `"Archive"` (both must exist) or the exact `Folders/` name, which must exist unless the approval includes creating it, which happens first. Any other `target` refuses without consuming the token. |
| `set_read_state(state, token, verbose=False, limit=None)` | `state="read"` redeems a `mark_read` preview and `state="unread"` a `mark_unread` preview, setting or clearing only `\Seen` for the previewed messages; undo restores each message's prior read state. The other `state` refuses without consuming the token. |
| `label(op, token, verbose=False, limit=None)` | `op="add"` redeems an `add_label` preview, copying the approved batch to the existing `Labels/` mailbox bound in the preview. `op="remove"` redeems a `remove_label` preview, removing only the approved label membership and preserving the message in All Mail. The other `op` refuses without consuming the token. |
| `unsubscribe(token)` | Performs approved HTTPS unsubscribe requests or prepares unsent mailto drafts with separate send previews. HTTPS cannot be undone; mailto sending requires `send_draft` approval. |
| `preview_rules(folder="INBOX", mark_read=False, limit=200, order="oldest", cursor=None)` | Read-only per-rule counts, samples, protected skips and `excluded_*` fields, plus a single-use token for up to `limit` reviewed matches. Calls are capped at 20 seconds; pass `next_cursor` as `cursor` to continue unfinished review or browse the next batch. `order` accepts `oldest` or `newest`. Packaged rules come first, then [saved custom rules](#saved-custom-rules). |
| `apply_rules(token, verbose=False, limit=None)` | Manually applies the approved moves and read-state option, plus each saved rule's own `mark_read`. Returns `log_ids` for `undo_action`. |
| `list_actions(token=None, since=None, before=None, limit=100, action=None, client_name=None)` | Read-only lookup of action, preview and reindex log IDs, linked runs and audited write calls, filtered by confirmation token, timestamp, tool (`action`: a write tool or `preview_action`, or one exact action of a merged tool such as `archive`; `move`, `set_read_state`, `label` and `reindex` find all of theirs) or MCP client name. Reindex records include run metadata and cannot be undone. Pending unsubscribe drafts include `draft_ids` for recovery. No source message IDs, recipients, senders, subjects, bodies, token digests or token IDs are returned. |
| `undo_action(log_id)` | Reverses a logged action once, logs that undo, and reports missing messages. It takes no token; each call is audited. A large chunk can take several calls: repeat while it returns `partial: true` with "Undo time budget reached" (see [Action log, undo and interrupted calls](#action-log-undo-and-interrupted-calls)). |

Mailbox and saved-rule tools that redeem a token first ask the user to confirm the exact
stored change in the client's form, unless the operator disabled that; see
[Confirmation and approval binding](#confirmation-and-approval-binding). Every
write tool call is audited; see [Audit trail](#audit-trail). The local-only
`reindex(op="execute")` uses its preview token without a mailbox-change confirmation.

Mailbox cleanup tools return compact results by default: counts, run ID, stop
reason, chunk log IDs, continuation token/expiry/availability, and `message_ids`.
`blocked_ids`, `missing_ids` and `mismatched_ids` name unchanged or unverified
problem rows. `verbose=true` includes the complete per-message `messages`,
`blocked`, `missing` and `mismatched` details. The internal execution records and undo journal always retain
full evidence. `list_actions` continues to omit confirmation secrets.

Action-log identifiers are strings of 32 lowercase hexadecimal characters.
One logged action uses `log_id`, including single-action results and
`list_actions.actions` entries; a result without an action record omits it.
Batch cleanup tools (`move`, `set_read_state`, `label` and `apply_rules`) always return
`log_ids` as a list, including `[]` or a one-element list. `list_actions.runs`
also groups chunk IDs in `log_ids`. Move, archive, draft deletion and unsubscribe
previews return `log_id` for a preview record visible in `list_actions`, including
blocked returned previews. These records cannot be undone. A `reindex(op="preview")`
that issues a token also returns a non-undoable `log_id`. Other previews return
no action-log ID. Preview tokens,
`run_id` and `audit_id` identify separate approval, run and audit records.
`undo_action(log_id)` still accepts one action-log ID. Clients migrating from the
former nullable `log_id` in batch results should iterate `result["log_ids"]`,
or use `result["log_ids"][0]` when exactly one chunk is known to exist.
Undo returns its own attempt's `log_id`; retry with the original action's ID.
Each `unresolved` row identifies a prior undo attempt with `log_id` as well.

`preview_action` excludes candidates whose Message-ID or membership identity is
ambiguous, whose copies have inconsistent sender headers, or whose item-level
action prerequisites fail. It returns `excluded_count` (distinct identities),
`excluded_ids` (at most 20 IDs and 4 KiB of encoded IDs), `excluded_truncated`, and
`excluded_reasons` (reason code to distinct-identity count). Each identity has one
primary reason, such as `inconsistent_sender_headers`. Excluded IDs never enter
the approval or its fingerprint. If all candidates are excluded, the response
explains why no token was issued. This also applies to unsubscribe previews.
Draft deletion instead refuses the whole batch if any requested message is missing,
outside Drafts or ambiguous; it never approves a subset of a mixed batch.
Protected-sender policy failures still refuse approval of the batch; mailbox
inventory failures, caps and deadlines remain whole-preview safeguards.

`preview_action(..., limit=30)` selects at most 30 clean rows, deterministically ordered
by folder, UIDVALIDITY and numeric UID. The limit is a positive integer at most
200, or at most `PROTON_MCP_APPROVAL_MAX_MESSAGES` for a
[bounded approval](#bounded-approvals); booleans, fractions and strings are
refused. The preview fingerprint binds exactly the selected rows and
`estimated_calls` describes that set. Omitting the limit retains the existing
200-row refusal. Unsubscribe retains its separate cap
and does not accept this option. Draft deletion accepts no limit and has a strict
200-message cap. Cleanup execution and `apply_rules` also accept
`limit` to bound the number of approved rows attempted in one call, preserving
an exact continuation for the rest. An execute limit cannot exceed the originally
approved total or include any unapproved row. `preview_rules` has its own limit and
ordering semantics; see [Manual connector folder rules](#manual-connector-folder-rules).
Initial execute revalidation still refuses a changed approved batch before any
claim or mutation. Continuations keep their existing approved-row checks and
never discover new candidates; a fresh preview creates a new approval.
`preview_rules` excludes the same way, with the same four fields, a message whose
copies have inconsistent sender headers or ambiguous label memberships; it keeps
every other refusal. `apply_rules` and folder-rules continuation stay whole-batch
strict on inconsistent sender headers and identity ambiguity.

Search and thread responses now wrap messages in an object. MCP structured content
changed from `{"result": [...]}` to `{"messages": [...], ...}`; callers should read
the top-level `messages` field.
Message IDs identify messages. Folder and UID fields follow
[Canonical message handles](#canonical-message-handles). Draft creation does not
submit a message for delivery.

Search orders messages newest first by IMAP `INTERNALDATE`, breaking ties by UID.
This reflects mailbox arrival chronology even when Bridge's UIDs are out of order;
the returned `date` field is the original message's Date header. Messages with an
unusable INTERNALDATE are omitted and mark the response partial with an inexact total.
Metadata is fetched in UID batches. Sender, recipient and subject filters use
case-insensitive substring matching on decoded headers locally, after a cheap server
date-window search (or `ALL` without dates). A local SQLite index remembers
headers already read and evaluates header filters across the full indexed folder
using `instr` on casefolded decoded values. This includes short terms, exclusions
and nested header-only queries before any uncached candidate cap. Missing
entries still take the bounded header-fetch path; an unavailable index uses that
same path. Cached decoded headers satisfy local filters without another header
fetch. With complete indexed dates, header-only searches select their chronological
page and exact total in SQLite, then fetch live flags and display data only for
that page. A broad recipient match does not fetch metadata for every match.
Text and unread predicates retain their live Bridge checks. See
[Local header search index](#local-header-search-index). `text`
still uses Bridge's server-side `TEXT` scan, with `SINCE`/`BEFORE` placed first;
Bridge may still scan the folder, so the deadline bounds the wait. Text criteria are
quoted/escaped or sent as UTF-8 literals.

For **All Mail** (identified by its IMAP LIST `\All` attribute, including localized
mailbox names), a complete header index searches all dates: the client verifies
every UID against a live `ALL` inventory, or reuses a certified local inventory
when the live mailbox epoch is unchanged. Header predicates use
the decoded header index; `text` still searches the entire raw message through
Bridge because the header index does not store message bodies. Completed
searches have exact totals and `partial=false`, subject to the deadline and metadata
guards. More than 1,000
indexed header matches remain fully reachable through `next_cursor`; the candidate
cap bounds only remaining uncached header work.

Search catches up small All Mail header gaps with read-only `BODY.PEEK` header and
INTERNALDATE fetches, including UIDs beyond the previously indexed maximum. An
authoritative `ALL` inventory also removes expunged entries.
`PROTON_MCP_SEARCH_INDEX_CATCHUP_MAX_MESSAGES` defaults to `2000` and accepts
integers from `0` to `10000`; `0` disables catch-up fetches. Unchanged certified
mailbox epochs need no new inventory. Catch-up preserves time for the requested
search; a stopped attempt retains the ordinary fallback and reports any
limitation through `partial` and `partial_reason`.
The optional IDLE watcher uses the same bounded update path for an existing index.

Without complete coverage, omitting both `since` and `before` applies a default
30-day `SINCE` window, reported as `applied_since`. These results have
`partial=true` and `total_is_exact=false` relative to the whole mailbox; `total`
counts matches gathered inside the window. Up to 1,000 uncached headers are retained
during the scan, so a later deadline can still return completed results. An old-only
mailbox can return an empty window. Supply either date bound to search a different period.

Search, count and read replies whose results depend on incomplete index coverage
include a shared `index_note`, for example
`"search index rebuilding: 250/1000 messages indexed (25%); results may be incomplete"`,
and `index_status` with `state`, `indexed_count`, `target`, `percent`
and `eta_seconds`. The state is `"rebuilding"` only while recovery is queued or
running; inactive incomplete coverage reports `"incomplete"`, `"disabled"`,
`"unavailable"` or `"failed"` as appropriate. `target` is the affected mailbox's
latest observed message total: search uses the selected folder, and a limited
seed read uses its live seed-folder observation. `target` and
`percent` are `null` until that total is known. Percent is clamped to 0–100; an
empty target is 100%. `eta_seconds` is `null` for these result-specific scopes
without an observed rate. Complete coverage, exact IMAP counts and reads fully
resolved through live IMAP omit both fields.
These progress annotations do not replace `partial` and `partial_reason` as the
search completeness signals.

`partial` is the single completeness signal. With `partial=false`, follow
`next_cursor` until it is null to collect the complete answer for the requested
folder, filters and dates within the search's UID snapshot. Explicitly malformed
INTERNALDATE values are omitted from matches and totals. Every partial response
includes `partial_reason`, such as
`date_window`, `candidate_cap`, `metadata_missing`, `timed_out`,
`unresolved_handles`, `unresolved_handles_skipped`, `canonical_unresolved` or
`no_resumable_cursor`. `total_is_exact` independently describes the count: an exact
total stays exact when unresolved canonical handles omit matching page rows.
`next_cursor` continues the original snapshot. A terminal partial response uses
`partial_reason="no_resumable_cursor"`; `applied_since`, `timed_out` and `error`
retain window and timeout context when that reason replaces the original limitation.
Index maintenance remains visible through `reindex(op="preview")`, `reindex_status` and
the server log.

Migration: replace reads of `search_index`, `search_index_details` and `retryable`
with `partial` / `partial_reason` for completeness and `next_cursor` for continuation.
Match `canonical_unresolved` instead of `canonical_lookup_failed` in `partial_reason`.

INBOX and other folders can reuse complete All Mail search headers through a live
ENVELOPE/INTERNALDATE inventory of their own UIDs. Unique Message-IDs map cached
search fields onto each folder's own UIDVALIDITY and UID; they never substitute
All Mail UIDs or establish canonical action identity. Missing or ambiguous IDs
retain the uncached-header fallback. Mapping shares the configured catch-up cap
between both folders, reserves search time and pauses during a rebuild. An
interrupted stream reconnects within the same deadline.

Complete scans retain chronological order if index availability changes. The
[continuation rules](#search-continuation) preserve the original window start date;
a subsequent day's default window requires a new first page.

Search uses at most 20 seconds (or the shorter configured call deadline), with a
10-second per-command timeout enforced by both the socket and a watchdog. Connection
setup shares the same budget. After local index pruning, header filters fetch at
most 1,000 uncached candidate UIDs. A larger uncached set returns `partial=true`,
`partial_reason="candidate_cap"`, a lower-bound `total` and `total_is_exact=false`,
with the [continuation rules](#search-continuation) below; the returned sample
is sorted by INTERNALDATE but may omit newer messages outside the candidate cap.
Continuation can return those newer messages after an earlier capped sample.
Cached header filters with indexed dates select each page in SQLite. Older caches
warm missing date/membership metadata through cheap database-backed IMAP fetches
without refetching decoded header content. Plain, date-only and unread-only
searches retain chronological paging and current flags, including All Mail's
default window. Narrow the date range or filters to finish an uncached header scan.
A timeout returns `timed_out=true`, `partial=true` and a clear `error` alongside
available results, including when connection setup or the initial search times out.
Its reason is `timed_out` for search work or `canonical_unresolved` for canonical
lookup; exhausted continuation replaces either with `no_resumable_cursor`.
Timed-out connections are closed before another call opens a fresh connection.

Page headers, `INTERNALDATE`, and optional `BODYSTRUCTURE` use one UID FETCH.
Snippets default to enabled: the first 20 messages on each page may fetch text,
with a 4,096-byte shared raw budget per message and 200 visible characters.
HTML-only bodies use visible HTML text. `snippet_omitted=true` identifies a snippet
skipped by the page budget, deadline, or malformed message content. Set
`include_snippets=False` to omit snippets and avoid body/structure retrieval entirely;
headers already fetched for filtering are reused.

<a id="search-continuation"></a>

Pass `next_cursor` with the same folder and filters to continue the original UID
snapshot and date scope; changed UIDVALIDITY or query is rejected with a validation
error. New higher UIDs do not shift pages; deleted messages disappear. A resumable
partial response always includes a cursor, capped at 2,048 characters. Missing
metadata and unresolved handles retry within a bounded budget; exhausted handle
retries skip the row with `partial_reason="unresolved_handles_skipped"` so paging
continues. Explicitly malformed INTERNALDATE values are omitted from matches and
totals. If an exhausted window, retry budget, unresolvable state or size limit
prevents continuation, the response
has `partial=true`, `next_cursor=null`, `partial_reason="no_resumable_cursor"` and a
`hint` to narrow dates/folder/from/to or run `reindex(op="preview")`, then obtain explicit
user approval before `reindex(op="execute")`. Null means finished only when
`partial=false`. `total` counts matches in the UID snapshot; an inexact total,
including after a deadline, is a lower bound.

Thread lookup prefers the mailbox flagged `\All` exclusively, avoiding repeated
scans of Bridge labels. Canonical real-folder lookup follows the bodies when
`include_thread=true`, as described below. Without `\All` it tries INBOX and Sent
before other selectable folders. It retrieves the requested message before expanding relatives.
A read-only search index hint for it is confirmed by one `UID FETCH` of its
headers. After every folder's hint has been checked, nonempty folders without a
confirmed hint use a bounded live Message-ID lookup. Small UID spaces need one
100-UID range; larger UID spaces use one `HEADER Message-ID` search per folder.
All fallback discovery shares a 12-second limit within the 15-second thread
budget, avoiding hundreds of range searches while the index is incomplete.
Returned raw headers must verify the requested Message-ID before the message is read. A slow or stalled fallback returns `partial=true` with a reason
explaining the incomplete-index lookup limit, without `timed_out` or `error`.
`compose_reply` preserves a limited lookup's reason and index status and returns a
retryable lookup error, so an unfinished lookup does not report a missing message
or draft.
Relatives are then searched by ASCII Message-ID, References and In-Reply-To
criteria, combined with OR in bounded chunks per frontier. Unicode IDs use
individual synchronizing literal searches for each header field, including when
mixed with ASCII IDs.
Thread headers are fetched in batches and Message-IDs are deduplicated. Headers
are retained before fetching MIME structure and bounded text for kept messages,
so a slow MIME fetch still returns the requested message's headers as partial.
Thread reads skip synchronous search-index writes and index catch-up. Canonical
lookup uses the index for folders with a current proof and live metadata for the
folders that fail theirs, and does not require a complete index. After the bounded
seed fallback, the requested body retains the remaining thread budget. Expansion
and remaining body fetches stop early to reserve up to 3 seconds of the call budget for
live canonical lookup; reaching this boundary returns `partial=true` with
`partial_reason="thread expansion limited"`, possibly with `timed_out=true` and
`error` if an in-flight operation times out. After the bodies are read, live
canonical lookup has its own 3-second cap within the call deadline. If only that
cap expires, the reply keeps the messages and bodies with unresolved view
messages' canonical fields null, `partial=true` and a `partial_reason` explaining
the canonical lookup time limit, without `timed_out` or `error`.
If both limits are reached, the reason combines them as
`"thread expansion limited; Canonical lookup reached its time limit; some canonical handles are unavailable"`.
`include_thread=false` skips relative-message searches and canonical lookup;
canonical `folder`, `uidvalidity` and `uid` are always null, including direct
real-folder reads. Every source handle is kept as `alias` without becoming partial
solely because lookup was skipped. Use `search_mail` for real-folder handles.
Malformed references are skipped. Expansion is bounded at four breadth-first rounds,
100 messages, 200 reference IDs, 100 fallback folders and 1,000 candidates. Each
call uses at most 15 seconds (or the shorter configured call deadline), including
connection setup and the initial Message-ID lookup, plus a cleanup grace of at
most 0.25 seconds when that budget runs out. Each IMAP command is capped
at 10 seconds, including `UID SEARCH HEADER Message-ID`; a watchdog also interrupts
responses that keep trickling bytes. When the budget ends, the reply waits no longer
than that grace for a step that has not returned, such as a relatives search that
no watchdog can interrupt; it carries the messages already read and reports the
timeout even if that step returns within the grace. Headers use partial PEEK
reads capped at 32,768 bytes per message. Text uses at most 16
partial `BODY.PEEK[section]<0.N>` reads sharing a 65,536-byte raw fetch budget
and a 65,536-character output cap; attachments are metadata only. Messages report
`size_bytes` (RFC822.SIZE), `body_size_bytes` (the preferred text parts' encoded
size), `body_fetched_bytes`, `body_limit_bytes`, `body_parts_fetched` and
`body_parts_limit`, plus `headers_fetched_bytes` and `headers_truncated`.
Attachment sizes are transfer-encoded byte counts from
`BODYSTRUCTURE`, rather than decoded file sizes, as `size_note` says whenever attachments
are listed.

A deadline or traversal limit returns available messages with `partial=true`.
Timeouts include `timed_out=true`, a clear `error` and `partial_reason`; even a
timeout before finding the requested message returns that structured result.
Completed text prefixes remain readable.
The requested message's body is fetched before expansion, so it survives a later
expansion timeout or mailbox/content error; `truncated=true` also marks an unfinished
body. Each call owns its connection and deadline for safe concurrency. Expired sockets
are closed rather than waiting for graceful logout. No persistent connection pool is
used. Invalid tool inputs are rejected before connecting, except cursor UIDVALIDITY
validation, which requires opening the mailbox read-only.

Reply drafts use the same All Mail preference for parent lookup. Lookup uses at most
half the call budget, reserving time for a fresh draft connection. If lookup times
out, the draft still uses the parent ID for `In-Reply-To` and `References`. A timeout
after APPEND sends the message can leave a saved draft even when the call reports
an error; check Drafts before retrying.

### Local header search index

The server automatically uses a private, account-specific SQLite index when
available. It records decoded subject, From/To/Cc names and addresses, plus dates
and membership metadata. It stores no snippet or body text and adds no body
fetches. Header searches evaluate exact casefolded
substring matches in SQLite, including short terms, exclusions and nested Boolean
header queries, before fetching page data or capping uncached header work.
Indexed INTERNALDATE metadata selects the requested page without fetching every
match. Matching preserves ordering, complete cursor paging and live unread handling.
Messages not yet indexed stay on the ordinary bounded search path. `text`,
`exclude_text` and `count_mail` continue to use Bridge: the header index cannot
answer full-message text predicates, and Bridge's count matches raw headers.

The database lives in `search-index/` beside the action log, normally
`$XDG_STATE_HOME/proton-mail-mcp/search-index/` or
`~/.local/state/proton-mail-mcp/search-index/`. Its filename is a digest of the
Bridge account and endpoint. This cache contains mail content; keep it private
and outside the checkout. Entries use `(folder, UIDVALIDITY, UID)`. Ordinary reads
update entries incrementally, moves and expunges remove source entries, and a
UIDVALIDITY change discards that folder's old entries. Complete mailbox inventories
also reconcile messages removed elsewhere. Ordinary calls do not rebuild the whole index.

A missing index warms through automatic startup recovery and as messages are read.
A corrupt or unavailable index falls back to the existing search path, with one
diagnostic per client instance identifying a safe reason. Temporary lock contention
is retried on the next use;
schema and private-file checks remain mandatory. The v2 schema has no FTS5 dependency.
Pre-v2 caches are rebuilt from scratch. Opening a cache whose SQLite
`user_version` is not `2`, or which still contains an FTS5 table, closes and
discards its database and its `-wal` and `-shm` files before creating a fresh v2
schema. This also works when SQLite has no FTS5 support. Old headers and coverage
proofs are discarded. Automatic startup recovery restores header coverage without
requiring a search or an approved manual rebuild. One log line reports that the old
cache was discarded without including its path.

By default, startup checks for a discarded, empty or incomplete index and queues a
read-only header recovery of All Mail and LIST-vetted real folders. It runs even
with `PROTON_MCP_BACKGROUND_SYNC_ENABLED=0`; set
`PROTON_MCP_INDEX_AUTOREBUILD_ENABLED=0` to disable it. The worker uses its own
preemptible IMAP connection and bounded header batches, giving way to user requests.
It fetches no bodies and changes no flags or mailbox membership. Each completed
batch updates the serving cache, so searches can use that progress immediately and
the next startup resumes from cached headers. Start, progress and finish are logged
without mail content or credentials. See
[automatic startup recovery](docs/search-index.md#automatic-startup-recovery).

To rebuild All Mail and real-folder headers while the server stays available, call
`reindex(op="preview")`, then `reindex(op="execute", token=token)` within five minutes. The token
is valid for execution until consumed or expired; it is not bound to the preview's
mailbox inventory. The preview labels its rough duration with
`eta_is_estimate=true`; estimates include All Mail and real-folder header copies.
Poll `reindex_status()` for `idle`, `rebuilding`, `running`, `succeeded`,
`failed`, `cancelled`, `incomplete`, `disabled` or `unavailable`, progress, estimated
time remaining, counts, and the final swap outcome. Only one run can be active; another start returns `"already running"`,
`state="running"` and the active `run_id`. A preview during that run returns its
`run_id` without performing another inventory or issuing a token.
Use `reindex(op="cancel", run_id=run_id)` to stop the run cooperatively.

Status always reports current locally indexed All Mail headers as `indexed_count`
and `indexed_max_uid`. `all_mail_count` and `all_mail_max_uid` describe the latest
locally observed All Mail inventory, timestamped by `all_mail_observed_at`;
automatic recovery also exposes `max_uid` as an alias of `all_mail_max_uid`.
During automatic recovery, `source="auto"` and `state="rebuilding"` describe
queued or running recovery. With no recovery active, incomplete or discarded
coverage cannot appear as an older successful run. Failed and cancelled manual
runs retain their error, run ID and swap outcome after automatic work stops.
During automatic recovery and catch-up,
`indexed_count`, `target`, `percent` and `eta_seconds` describe serving All Mail
coverage. `target` is the latest known
All Mail message total, with `null` for an unknown inventory. `percent` is
`100 * indexed_count / target`, clamped to 0–100, or `null` while the target is
unknown; an empty target is 100%. Incomplete nonempty coverage is capped at 99.9%.
`eta_seconds` is `null` until active recovery has a positive observed rate.
Real-folder warming can keep the state `"rebuilding"` after All Mail reaches 100%;
ETA is `null` during that phase rather than zero.
The existing `percent_done` field remains available for compatibility.
Status never contacts Bridge. `last_catchup_at` records the
last completed incremental sync or committed automatic recovery batch, and
`last_expunge_reconciliation_at` records the last inventory reconciliation, even
when no messages were removed. These timestamps survive restart.
UID maxima are numeric strings (`"0"` for an empty mailbox); unavailable fields
are `null`, including inventory maxima/timestamps from older caches until refreshed.
`last_full_run` contains the current or last full rebuild's `old_count`, `new_count`
and `all_mail_count`, or is `null` before a full run. Its `all_mail_count` is the
count at that full rebuild; the top-level `all_mail_count` is the live count from
the latest locally observed inventory. These run counters survive
restart and retain their values after incremental catch-up, failure or cancellation.

The manual background worker warms All Mail and LIST-vetted real folders such as
INBOX, Archive, Sent and Trash, using each folder's own UIDVALIDITY and UID. It skips
label views. Older All-Mail-only caches can also warm real-folder search headers
through live membership mapping. Counts remain based on All Mail; running progress
and ETA include the real-folder copies. Missing or unsafe headers and arrivals
during a folder's scan can still leave its coverage incomplete; unmapped, uncached
search work retains its candidate cap until those headers are observed.

The manual background worker has a separate read-only IMAP connection, uses
EXAMINE and `BODY.PEEK`, and fetches headers only. It never issues STORE, EXPUNGE or MOVE.
`PROTON_MCP_REINDEX_BATCH_SIZE` and `PROTON_MCP_REINDEX_BATCH_SLEEP` bound its
batches and pause between them so Bridge remains usable. Searches keep using
the old SQLite index while the worker builds a private candidate beside it.
The candidate replaces the index atomically only when it covers at least 99% of
the current All Mail UID inventory. Errors, cancellation or lower coverage discard
the candidate and keep the old index. A successful 99% swap can still leave
incomplete header coverage until every current UID has known headers.
Headers containing NUL are not marked known, so even a successful rebuild may
leave gaps while such a message is present. A successful swap replaces the whole
cache with the new All Mail and real-folder headers. Label-view headers are
discarded and warm again through ordinary reads.

At startup, stale rebuild candidates and their lock files are removed only after
taking each candidate lock exclusively without waiting; held locks are skipped.
Rebuild execute records left running without a result receive an `interrupted`
result. A restart does not resume that manual candidate rebuild; automatic recovery
resumes from headers already committed to the serving cache.

When enabled, the [IMAP IDLE watcher](docs/imap-idle.md) also adds newly observed
messages' headers incrementally and removes expunged or obsolete UIDVALIDITY
entries. It fetches no bodies and uses separate connections from tool calls.

The opt-in [background All Mail sync](docs/search-index.md#background-all-mail-sync)
(`PROTON_MCP_BACKGROUND_SYNC_ENABLED=1`, off by default) keeps an already built index
current between requests without a watcher. It polls with STATUS, fetches only new
headers and gives way to every tool call. Automatic startup recovery handles an
empty cache independently of this periodic worker.

### Unread mail, exclusions, queries and counts

Every `search_mail` message has `seen`: `true` when its IMAP `\Seen` flag is set.
The flags come from the batched `UID FETCH` that already reads each candidate's
`INTERNALDATE` (`UID INTERNALDATE FLAGS`, served from Gluon's database), so no
command is added per message or per page. Search still opens mailboxes read-only
and never marks anything as seen.

- `unread_only=true` adds the `UNSEEN` key, which Gluon answers from message flags
  alone. A message whose fetched flags show it was read after the search is also
  dropped, so every returned message has `seen: false`. Only a JSON boolean is
  accepted.
- `exclude_subject` drops messages whose subject contains the value. Like `subject`,
  it is matched locally on the decoded header, ignoring case.
- `exclude_text` drops messages whose text contains the value. Like `text`, it is a
  server key: `NOT TEXT` in the same `UID SEARCH`.
- `query` composes conditions with `and`, `or` and `not`. The entries of one object
  all apply. `from`, `to`, `subject` and `text` match case-insensitive substrings
  (`from_` is accepted for `from`); `and` and `or` take a list of 1 to 20 queries and
  `not` takes one query:

```json
{"or": [{"from": "alerts@example.test"},
        {"subject": "invoice", "not": {"text": "unsubscribe"}}]}
```

A query holds at most 20 conditions in at most 5 nested levels, and each value is
nonempty text of at most 1,000 characters. Other keys, other value types and control
characters are refused before any connection, with messages that do not repeat the
input. Filters, exclusions and the query all apply together.

In `search_mail`, `from`, `to` and `subject` conditions inside a query are matched
locally on decoded headers, exactly like the header filters, after the cheap date
search: they add no Bridge header scan. `text` conditions go to Bridge. Text
conditions that stand on their own, with `not` and `or` between them, join the one
`UID SEARCH`. A text condition combined with header conditions under `or` or `not`,
for example `{"or": [{"subject": "invoice"}, {"text": "receipt"}]}`, costs one more
`UID SEARCH` per distinct text value, with the same date keys, and is then combined
per message. Each such text search makes Bridge read every message in the folder.

Conditions become RFC 3501 SEARCH keys in `UID SEARCH CHARSET UTF-8`. `not` is
`NOT key`; `or` is `OR key key`, and a longer list chains (`OR a OR b c`); an `and`
group inside `not` or `or` is one parenthesized key with no inner padding, such as
`(FROM "a" SUBJECT "b")`, the form Gluon parses. Keys come only from that fixed
vocabulary. Each value is one quoted string with `\` and `"` escaped, so quotes,
parentheses or keywords inside a value never become syntax, and CR, LF and other
controls are refused. A non-ASCII value is sent as the command's synchronizing
literal. imaplib sends one literal per command, after the last key, so a condition
whose non-ASCII value cannot be that last key (a further non-ASCII value, or one
inside a parenthesized group) is evaluated per message instead: each of its values
costs one more `UID SEARCH` with the same date keys. This `count_mail` call:

```python
count_mail(
    from_="alice",
    since="2026-10-01",
    unread_only=True,
    exclude_subject="weekly",
    query={"or": [{"to": "team"}, {"not": {"text": "draft"}}]},
)
```

sends exactly one command and no FETCH:

```text
UID SEARCH CHARSET UTF-8 SINCE 01-Oct-2026 UNSEEN FROM "alice" NOT SUBJECT "weekly" OR TO "team" NOT TEXT "draft"
```

Search cursors bind `unread_only`, the exclusions and the normalized query; the order
of keys in a query object does not matter. A search without these options keeps its
previous cursor key, so cursors issued before they existed continue to page.

`count_mail` accepts the same filters, `unread_only`, exclusions and `query` and
returns `{"folder": "INBOX", "count": 12}`. It sends only `UID SEARCH` and never
fetches headers or bodies; an empty folder needs no search, and without any condition
the count is the folder's `EXISTS` from `EXAMINE`. Unlike `search_mail`, Bridge
matches every condition. `FROM`, `TO` and `SUBJECT` compare the raw header text:
`Subject: =?utf-8?q?Caf=C3=A9?= weekly digest` matches `weekly digest` but not `Café`,
so a count can be lower than `search_mail` finds for non-ASCII header values. Header
and text conditions make Gluon read every message in the folder (date keys do not
narrow that work), within the call deadline; dates and `unread_only` use only its
database and flags. The folder is matched by its LIST name as in `search_mail`, no
30-day All Mail window applies, and a view such as All Mail counts each message once.
The complete call, including connection setup and all fallback searches, uses at
most 20 seconds, with at most 10 seconds per IMAP command; shorter configured
limits still apply. A timeout returns `count=0`, `partial=true`, `timed_out=true`,
`partial_reason="timed_out"` and a clear `error`, so the zero is not a completed count. The interrupted
connection is closed and the next call opens a fresh one.
A count carries no handles; use `search_mail` for each match's canonical `folder`,
`uidvalidity` and `uid`.

### Canonical message handles

Every tool that reports a message's mailbox location uses one canonical handle:
`folder`, `uidvalidity` and `uid` of the message's **real folder**. A real folder
is `INBOX`, `Archive`, `Sent`, `Drafts`, `Spam`, `Trash` or a `Folders/` mailbox
whose LIST entry carries no `\All`, `\Flagged` or `\Noselect` attribute, in any
letter case: the same allowlist as guarded move sources, applied identically by
search, threads and previews. Bridge keeps each message in exactly one
real folder and shows it again in views (All Mail, Starred, Scheduled and
`Labels/` mailboxes), each with its own UID space. A message can be UID 20321 in
INBOX and UID 47175 in All Mail: `search_mail` reports INBOX 20321.
`read_thread` reports the same handle when its canonical lookup succeeds and
keeps the All Mail handle as `alias`; skipped or capped lookups leave unresolved
view messages' canonical fields null. With `include_thread=false`, every message's
canonical fields are null, including messages read directly from real folders.

The real folder is canonical because moves, read-state changes and folder rules
take it as their source and their confirmation tokens bind exactly these
`(folder, UIDVALIDITY, UID)` handles. Future Sieve filters also act on real
folders. A UID is meaningful only together with its folder and UIDVALIDITY; if
UIDVALIDITY changes, search again and request a fresh preview.

- `search_mail` on a real folder returns that folder's handles directly; it adds
  at most one LIST command. The requested `folder` is matched to its LIST name
  first: `INBOX` in any ASCII case (`inbox`, `Inbox`) is selected and reported
  as LIST spells it, while every other name must match exactly.
- `search_mail` on a view, such as All Mail or a label, keeps the view's handle
  only as `alias` (`{"folder", "uidvalidity", "uid"}`) and resolves the real folder
  after reading the page. `read_thread` also keeps the view alias, but resolves
  canonical fields only with `include_thread=true`, after reading the bodies,
  within a reserved 3-second budget and the call deadline. Direct reads skip that
  lookup and return null canonical fields without setting `partial=true`; use
  `search_mail` for a real-folder handle.
- `search_mail` returns only messages with non-null canonical handles. Unresolved
  page rows are dropped with `partial=true`. Their `unresolved_handles` reason
  replaces `date_window` or `candidate_cap`, while other existing failure reasons
  are retained; failed lookups report `partial_reason="canonical_unresolved"`.
  The matching `total` can
  therefore exceed the displayed row count while `total_is_exact` stays true.
  The [continuation rules](#search-continuation)
  govern retries; resolved rows on either side are retained and excluded from later
  pages.
  Thread and preview rows can still retain `null` canonical fields and their view alias.
- Previews (`preview_action`, including unsubscribe, and `preview_rules`)
  report the canonical handle in `samples` and `blocked`.
  A label or All Mail source handle appears as `alias`. After the approval checks,
  previews resolve it with the same lookup as `search_mail`, never from the
  discovered memberships, which share only a Message-ID with the source and may
  be other messages. The lookup reads All Mail and every real folder, much as the
  preview's membership checks did, so it starts only if the preview has used at
  most 40% of its budget (`PROTON_MCP_PREVIEW_TIMEOUT`, else the call budget):
  a lookup of similar cost then still ends within the 80% that approval must fit.
  If the lookup is skipped or fails, the canonical fields are `null` and the
  preview reports `partial=true`; its token and bound handles are unchanged.
  Execution refusals still name the bound source folder.
- Execution, undo and journal records keep the exact handles they changed
  (`source_folder`, `uid_before`, `uidvalidity_before`, ...). For moves, archive,
  trash, read-state changes and rules these are the canonical handles.

```json
{"message_id": "<weekly@example.test>", "folder": "INBOX", "uidvalidity": "1001",
 "uid": "20321", "alias": {"folder": "All Mail", "uidvalidity": "1007", "uid": "47175"}}
```

Resolving a view handle never reads bodies and never issues a HEADER search.
All Mail searches and threads can reuse indexed envelope/date/size fingerprints when
All Mail's complete inventory is certified against its live epoch. All Mail lists
each message once, so one copy there rules out a twin, and a real folder needs its own
proof only if it holds a cached copy of a wanted message. No folder gives a handle
from its index unless its certified inventory matches live STATUS, before and after
the lookup. This avoids rescanning same-day messages for every page. A message with
no proven copy gets the live lookup below in the folders that fail their proof, and
no others, stopping at its first copy. So a message listed in two real folders can
resolve to the proven or first-found copy, and stays unresolved when the lookup sees
both. A stale All Mail, label views and legacy or incomplete All Mail fingerprints use
the whole live lookup under the call deadline.

Apart from the view that was read, the live lookup never opens a label or Starred.
In All Mail and each real folder it sends `UID SEARCH` with an `OR` of `ON <day>` keys (at most
50 days per command) for the INTERNALDATE days involved, then fetches
`UID INTERNALDATE RFC822.SIZE ENVELOPE` in batches of 500. Gluon serves these
values from its database, shares them between a message's mailbox copies, and
reports INTERNALDATE in UTC, the day ON compares. A handle resolves only when
exactly one real-folder copy, and exactly one All Mail copy, share the envelope,
INTERNALDATE and size. Otherwise, including identical duplicate deliveries or mail
with no real folder, the handle remains unresolved. Search drops those rows and
reports the unresolved count; threads and previews retain `null` canonical fields.
A deadline or mailbox error during resolution sets `partial=true`. Incomplete
metadata also prevents resolution: a
record missing from the FETCH response, or one without a well-formed `ENVELOPE`,
`INTERNALDATE` or `RFC822.SIZE`, for the view handle or for any copy that a
`UID SEARCH` returned. Such a record could belong to another copy of the message,
so the remaining copies never establish uniqueness.

Before a handle resolves, the lookup opens the view, All Mail and every real
folder it read once more (`EXAMINE`). Each must report the UIDVALIDITY, UIDNEXT
and message count it was scanned with. UIDNEXT never repeats, so this proves that
no identical message arrived, for example in INBOX after All Mail was scanned,
and that no copy left while the folders were read. A mailbox that changed, or one
that reports no UIDNEXT, leaves the handle unresolved with `partial=true`.
Without an All Mail view, no view handle resolves and no lookup runs: no other
mailbox lists each message exactly once, so a Starred or label message without a
real folder would be indistinguishable from an identical message in INBOX. Search
cursors still page through the searched mailbox's own UIDs.

## Confirmation and approval binding

Every mutation token is bound to its exact preview. `approval_hash` in
[`guarded.py`](src/proton_mail_mcp/guarded.py) is one SHA-256 over the action,
its target and params, the selector and the approved message rows: identity,
sender and membership data, read state where applicable, the unsubscribe target
and details, the exact Sieve script, and for send the exact draft bytes and SMTP
envelope. Every preview that issues a token also returns this `action_hash`. The
token store keeps only the token's digest beside the stored approval and recomputes
the hash on every lookup, so a token cannot redeem any other action set. Before any change,
execution re-derives the rows from the mailbox (or re-reads the draft) and refuses
when anything differs: identity, sender headers, flags, membership, unsubscribe
links, folder rules or draft bytes. A continuation instead leaves a changed row
untouched and reports it as blocked. Unstarted approvals expire according to
`PROTON_MCP_CLEANUP_TOKEN_TTL` or `PROTON_MCP_SEND_TOKEN_TTL`. The first successful
claim for cleanup or `apply_rules` starts one fixed 30-minute resume window;
continuations never renew it. Other approvals retain their preview expiry.
Tokens remain single use except for the `move_to_folder` aliases that authorize
only the same durable remainder. Completion, expiry, a different action or account,
a `target`, `state` or `op` the token does not pin, and a changed payload all refuse.
A refused selector consumes nothing and shows no form; the change that runs is always
the stored one, never the selector's value.

A token alone cannot authorize a change. By default
(`PROTON_MCP_CONFIRM_MUTATIONS=1`), `move`, `set_read_state`, `label`, `apply_rules`,
`delete_draft`, `unsubscribe`, `create_folder`, `create_sieve_filter` and `add_rule` first show the
stored approval in an MCP **form elicitation**: one sentence such as
`Move 12 message(s) to "Trash".`, the approval hash prefix, counts, the target, up
to five samples (sender, subject, date, folder), per-rule targets for rules,
method/host for unsubscribe (never URLs), the exact script for Sieve filters and the
exact rule and `rule_sha256` for saved rules.
Only an explicit accept with `confirm=true` proceeds. The summary comes from the
stored approval, never from tool arguments, and unknown arguments are refused. The token store itself refuses to redeem a token handed to an MCP client
until its form accepted that exact action hash, so no internal path skips the
form. A declined, cancelled, unanswered (`PROTON_MCP_CONFIRM_TIMEOUT`) or failed
form, and a client without form support, retire the token; preview again. A
continuation token belongs to an approval the user already confirmed and runs
without another form; it can only narrow that approval. As for guarded send, a
host that answers forms automatically defeats this check. Forms need stateful
Streamable HTTP sessions with SSE, so the server uses them whenever confirmation
or send is enabled; clients must initialize a session.

`PROTON_MCP_CONFIRM_MUTATIONS=0` is an explicit operator opt-out for hosts
without form elicitation. It restores token-only redemption: the token, which
passes through the assistant, then authorizes its bound action by itself.
Binding, single use, expiry and the audit trail still apply. **Before upgrading
an existing install whose host has unverified form support, verify forms first or set the opt-out deliberately**; otherwise every
mutation is refused. Guarded send keeps its own confirmation path, including the
own-address token approval; this switch does not affect it. `undo_action` takes
no token: it reverses one logged action once and is audited.

## Attachments

`list_attachments`, `read_attachment` and `save_attachment` work on one message at a
time, found like `read_thread` finds it. Every read uses `BODY.PEEK`, so nothing is
marked read and nothing in the mailbox changes. An attachment is named by its `index`
from `list_attachments`, counted from 1.

One file has two sizes. Decoded bytes are the file itself. Encoded bytes are the
transfer-encoded count stored in the message, the only size `BODYSTRUCTURE` gives without
downloading the file: base64 is about a third larger, and a 7bit part is the same size. A
29,218-byte PDF in base64 is 39,984 encoded bytes.

| Size | Reported by |
| --- | --- |
| Encoded bytes | `list_attachments`, `forward_message`, `read_thread`, `show_thread`, `read_attachment`'s `size`, a saved draft's or message's row in `compose_reply` |
| Decoded bytes | `preview_send`, `review_send`, the send form, `save_attachment`, `read_attachment`'s `decoded_size`, a local file's row in `compose_reply` |

Results that list encoded bytes say so in `size_note`, and a `compose_reply` row with them
has `size_encoded: true`. The [views](docs/mcp-apps.md) mark them, as in `39 KB encoded`. A
size with no mark is decoded, so the same PDF reads `29 KB` in the send review.

| Limit | Value |
| --- | --- |
| `read_attachment` file | 10 MiB decoded |
| `read_attachment` image | 5 MiB |
| `read_attachment` text per call | 65,536 characters; `next_offset` continues |
| PDF pages and text read | 1000 pages, 2,000,000 characters |
| `save_attachment` file | 25 MiB decoded |
| Files attached to one draft | 20 files, 7 MiB decoded in total |
| One draft with its attachments | The 10 MiB send limit, checked before anything is written |

`read_attachment` goes by the content, not the label a sender put on it: PNG, JPEG, GIF
and WebP signatures return image content, a `%PDF-` header is parsed as a PDF, and only
a part labelled `text/plain` or `text/csv`, or named `.txt` or `.csv`, without NUL bytes
returns as text (a UTF-16 byte order mark is honoured, and a charset that is unknown or
not meant for text reads as UTF-8). Everything else refuses, and `save_attachment` can
still keep it. A PDF is parsed with
[pypdf](https://pypi.org/project/pypdf/) in a separate process with no environment, a
30-second limit and a 1 GiB memory limit, so a hostile file can neither reach this
server's token or the Bridge password nor stall the server. Password-protected PDFs
refuse; a scanned PDF returns no text and says so. Attachment content is the sender's,
so treat it as data rather than instructions.

PDF text gives four hyphen characters as a plain `-`: U+00AD, U+2010, U+2011 and U+2212.
U+00AD is the one readers do not show; pypdf returns it for code 0xAD of WinAnsi, which a
PDF paints as a hyphen, and for a ToUnicode entry or glyph name mapped to it. En, em and
other dashes stay as they are, and so does TXT and CSV text. All attachment text loses its
control characters (tab and line ends stay), so a character with no Unicode mapping in the
PDF's font can still be lost: pypdf can return its font code as a control character, which
that removal deletes, and `REF-0001` can read `REF0001`. The exception, in all attachment
text, is U+0000 between two letters or digits, which reads `-` because some fonts map a
hyphen or dash glyph to it. The character is inferred: `:` or `+` lost the same way also
reads `-`.

### Attachments on drafts

`create_draft(..., attachments=["report.pdf", "2026/q3.csv"])` attaches files by their
paths inside the allowed folder. `create_draft(..., attachments_from=id,
attachment_indexes=[1, 3])` copies those attachments of a saved draft or message byte for
byte. A refusal of any of them writes no draft, so nothing is silently left behind.
Attaching never sends: the draft still takes `preview_send`, the user's confirmation and
`send_draft`, and the hash they bind covers every attachment.

In `compose_reply`, a reopened draft keeps its attachments, and a reply to a saved draft
keeps that draft's. A reply to other mail lists the original's attachments without
keeping them. The result's `attached` lists each with its `checked` state, and the
`draft` fields carry those that are kept; the composer view lets the user tick or untick
each one before saving.

### Forwarding

`forward_message(message_id, to, cc=None, attachments=[1, 3], note="FYI")` saves a new
unsent draft with a `Fwd:` subject, the note, the original's From, Date, Subject, To and
Cc lines and its text, and copies of exactly the listed attachments, byte for byte.
`attachments` is required, so forwarding one has to be chosen; `[]` forwards none. The
result names what went in (`attachments`) and what stayed out (`not_forwarded`). The
original's text is cut at `read_thread`'s 65,536-byte limit, and the draft and `warnings`
say so. If that text cannot be fetched, or the original's selected headers exceed 32 KiB,
the forward refuses and writes no draft, rather than saving one without the text. The draft
has no `In-Reply-To`, so it does not join the original's thread.

There is no separate way to send a forward. The draft goes through `preview_send` and
`send_draft` like any other, with the host form, or a [typed OK](#typed-ok-when-the-client-has-no-form)
when the client has none, and `review_send` shows it in the same view.

### The allowed folder

`PROTON_MCP_ATTACHMENTS_DIR` names the only folder that attachment tools read or write;
unset, they use a [private per-user default](#configuration). A missing or unsafe
folder refuses before reading any mail. It must be a real directory rather than a
symbolic link, and not be writable by everyone. Names are relative to it:

- An absolute path, `..`, a hidden name (`.x`), a backslash, a control or invisible
  character or an empty path step refuses.
- Each step opens below an already open directory and never follows a link, so a link
  inside the folder leads nowhere, even one that appears after a check. Only regular
  files with a single name attach: no hard link, FIFO, device or directory.
- `save_attachment` creates a new file with mode `0600` and never overwrites: an
  existing name, or a link at that name, gets a number (`plan (1).pdf`). The result's
  `filename` says which name was used. Its `filename` argument is one plain name.
- A sender's file name is cleaned first: path parts, invisible characters, leading dots
  and dashes and trailing dots and spaces go, and it is cut to 200 bytes, keeping the
  extension. `=?` becomes `=_`, so a name cannot decode again when a draft's header is
  written. Files are never executable.

## Guarded draft deletion

Call `preview_action(action="delete_draft", ids=[message_id])`, or select drafts
with `query={"folder": "Drafts", ...}`. Show the preview and obtain explicit
approval before calling `delete_draft(token)`. The confirmation form states that
the deletion is permanent, names the resolved Drafts mailbox and warns that it
cannot be undone. The preview returns a `log_id`; execution returns a separate
`log_id` and `undo_supported: false`.

Drafts resolves from a unique selectable special-use `\Drafts` mailbox when
available, otherwise from `PROTON_BRIDGE_DRAFTS_FOLDER`. The preview refuses the
entire request if any message is missing, ambiguous or outside that mailbox,
including mixed batches. Execution resolves Drafts again and revalidates every
approved message before any mutation. For this tool only, a single From address
matching the authenticated Bridge account may bypass its own personal-provider
restriction inside the verified Drafts mailbox. Other personal addresses in
Sender or Reply-To, and explicit protected address/domain/display-name rules,
remain protected. Other folders and tools retain the existing policy.
Both stages require UIDPLUS. At most 200
messages can be approved; `limit` and nonempty `params` are refused. Only the
targeted UIDs receive `UID STORE +FLAGS.SILENT (\Deleted)` followed by scoped
`UID EXPUNGE`; ordinary/global EXPUNGE is never used.

## Guarded send

Enable with `PROTON_MCP_ENABLE_SEND=1`, configure `PROTON_MCP_SEND_FROM` as a
comma-separated list of exact sender addresses, and set `PROTON_BRIDGE_CERT` to
the exported Bridge PEM certificate. There is no free-form send tool:
`create_draft` → `preview_send(draft_id)` → `send_draft(token)`. With
[MCP Apps views](docs/mcp-apps.md), `review_send(draft_id)` shows the same preview
in a view without issuing a token; sending still takes that path. Preview reads only
Drafts, at most 10 MiB, and binds the token to its mailbox identity and SHA-256
of the exact serialized message bytes plus the SMTP envelope sender and all
To/Cc/Bcc recipients. This covers every delivered header, body and attachment;
Bcc recipients remain in the envelope but are removed from delivered headers.
Recipients must be unambiguous ASCII addresses and may carry a plain display name,
never an encoded one; sender matching is case-sensitive. Ambiguous sender
headers and address/subject control characters are refused, as is any C0 control
or DEL in another top-level header, raw or inside an encoded word, apart from a
tab and CRLF line folding.

One draft has one live token: a new preview retires earlier tokens for that draft.
A draft whose exact bytes and envelope the journal already records as `sent` or
`partial` refuses delivery with `already_sent`; edit it or create a new draft to
send again. When the server may have accepted the message, `send_draft` returns
`uncertain: true` and the `send_result` journal record says the same: DATA started
and no explicit refusal came back, such as a lost reply or the SMTP timeout. That
draft then refuses with `possibly_sent`, as does one whose journal holds a reservation
without a result, after a crash or a failed journal write, even if the crash came
before DATA. Check Sent, then edit the draft or create a new one to send again. A
failure before DATA and an explicit 4xx or 5xx reply to DATA report
`uncertain: false`, and a fresh preview can retry them. These refusals come before
any form opens. Only the recipient check (for the own-address path), the form and the
daily cap limit how many different drafts can go out. On the typed-OK path, only the
assistant's conduct and the daily cap do.

By default, `send_draft` displays the From address, recipients, subject, attachments, caps and
the hash prefix in a **host-side form elicitation**. Its first line names the From address and
each attached file's name, type and decoded size. A name keeps its own letters, in quotes, while control,
line-separator and invisible characters show as `_`, so it can neither start a line of its own
nor hide its real extension. The JSON details below stay ASCII, so a look-alike letter can be
checked there. Only an explicit accept with `confirm=true`
proceeds. This requires a trusted host that presents the form to a person; hosts
that automatically answer elicitation are unsuitable. Decline, cancel, timeout and errors fail closed before SMTP and never fall
back. A client whose `initialize` omits elicitation, or that
`PROTON_MCP_SEND_TYPED_OK_CLIENTS` lists, takes the
[typed OK](#typed-ok-when-the-client-has-no-form) path instead. Tool arguments
cannot supply confirmation: a call with extra
arguments is refused and audited before it redeems anything. Each redemption
burns the token, including declined confirmation; request a fresh preview to retry.
After confirmation, the draft is rebuilt and checked before the journal reservation
and again before SMTP DATA. Changes to the message or its mailbox identity refuse
delivery permanently for that token, even if the draft is later restored.
Request cancellation before DATA also burns the approval and aborts the connection
without submitting the message. A retry requires a fresh preview and confirmation.

### Sending from an alias

`create_draft`, `forward_message` and `compose_reply` take `from_address`, and
`list_from_addresses` lists what they accept: exactly the addresses in
`PROTON_MCP_SEND_FROM`, the same list that guarded send has always checked. An address
off the list refuses before any mail work, including another spelling, case or display
name of a listed one. The address must also be one that your Proton account can send
from; Bridge refuses others at delivery. Without `from_address` a draft comes from the
account's own address, as before.

An alias takes the normal path: the draft is saved, and `preview_send`, the host form and
`send_draft` send it. The form says the sender is an alias before anything is sent, the
hash binds the From address and the SMTP envelope uses it, while the Bridge login stays
the account's. An alias sender skips the form only on the
[own-address path](#own-address-only-token-approval), when every recipient is your own, or
on a [client with no form](#typed-ok-when-the-client-has-no-form), whose summary says the
sender is an alias. In
the [composer](docs/mcp-apps.md), From is a choice of the allowed addresses, and a reopened
draft keeps an allowed alias.

### Typed OK when the client has no form

`preview_send` reports `approval: "typed_ok"` only when `PROTON_MCP_SEND_TYPED_OK` is on, which
is the default, and the client's `initialize` capabilities omit `elicitation` or its
`clientInfo.name` is on `PROTON_MCP_SEND_TYPED_OK_CLIENTS`.
`PROTON_MCP_SEND_TYPED_OK=0` is the strict off switch and restores the old refusal:
`preview_send` reports `host_confirmation` and `send_draft` refuses with
`confirmation_unavailable`, because there is no form to show.

A client that declares elicitation always gets the form and never falls back, unless the
operator lists it. An empty
`elicitation: {}` counts as a form, as the MCP specification says, and gets one. A client
declaring only `url` mode has no form to show, so it is refused. When a form is due and no
accept comes back, the send fails with a clear error and the next preview stays on the form:
a decline, a cancel, no answer within `PROTON_MCP_SEND_CONFIRM_TIMEOUT`, a JSON-RPC "method
not found" or "Elicitation not supported" reply, or any other error. The journal's result
says which: `confirmation_declined`, `confirmation_cancelled`, `confirmation_timeout`,
`confirmation_unavailable` for a reply saying there is no form, and `confirmation_error` for
the rest. So a host that declares a form and then never shows it, or answers every form
with `decline`, stays refused, whether it keeps one session or opens a new one per call. It
reaches this path only if its `initialize` stops declaring elicitation or the operator lists
it in `PROTON_MCP_SEND_TYPED_OK_CLIENTS`, for a host known never to show the form.

This covers every send on such a client: external recipients, self-sends with
attachments, forwards and mailto unsubscribe drafts. The preview adds `summary`, plain
text to show the user: From, To, Cc, Bcc, Reply-To when present, the whole subject, each
attachment's name, type and decoded size, how many other text parts it does not show, the
SHA-256 prefix of the hash, and the first 1000 characters of the body, quoted, with the
body's full length when it is cut. It ends by saying this app has no confirmation form, so
type OK to send. The assistant must show that summary unchanged in a code block, and call
`send_draft(token, draft_id)` only after the user typed an explicit OK in chat. Text in the
draft or in mail never counts as that OK. A draft already sent or possibly sent refuses at
this preview, before anyone is asked.

**The server cannot prove that a human typed the OK.** The OK never reaches the server:
the assistant alone says whether it was given, so a misled or careless assistant can
send without asking. A client's capabilities are its own unauthenticated claim, so any
client holding the server token can declare no form and reach this path, for any
recipient. So `PROTON_MCP_ENABLE_SEND=1` alone allows this, and a host update or proxy that
drops `elicitation` from `initialize` moves its sends from the form to this path. Each preview
and each send on this path logs a WARNING with the client's self-reported name and version
from `initialize`, never the token, so the service log shows the change before anything goes
out. `PROTON_MCP_SEND_TYPED_OK=0` turns this path off.

The service log also records, at INFO and never with mail content, addresses or tokens: each
session's client name and version and whether it declared elicitation, once on its first tool
call; why a form gave no accept (`timeout`, `unsupported`, `decline`, `cancel`, `error`); and
each time the client list replaces the form.

The token only guards against a changed draft and a replay. It works once, and it
expires like every send token (`PROTON_MCP_SEND_TOKEN_TTL`). It is bound to the hash of
the exact message and envelope: headers, To, Cc and Bcc, attachments and body. Any
change refuses. `draft_id` names the draft the user read, and a token from another draft
refuses. `draft_id` can only refuse: the token alone decides what is sent. Recipient and daily
caps, TLS checks, the `already_sent` and `possibly_sent` refusals and the journal still
apply, and the journal's reservation records the path a send really took:
`approval: "typed_ok"` for a send with no form, so an operator can see afterwards which
sends had none, and `host_confirmation` for such a token redeemed through a form.

A client that can show a form keeps it: it never gets `typed_ok` from a preview, and if
it redeems such a token from another session, `send_draft` still asks in the form. The
[own-address path](#own-address-only-token-approval) comes first and works as before.

### Own-address-only token approval

Every send uses a token. `PROTON_MCP_SEND_SELF_NO_FORM=1` skips the form when every
recipient is the operator's own address, a narrowly scoped alternative to form
elicitation. It is **off by default**: `0` or unset leaves it off, and any other value
is a configuration error. Sending must still be separately enabled. Preview reports
`approval: "token"` only when the flag is on, the draft has no attachment and no forward
subject (`Fwd:` or `Fw:`), and one of these holds:

- Every To, Cc and Bcc address, together the whole SMTP envelope, is the account's own
  address or on `PROTON_MCP_SEND_FROM`. The draft may come from any allowed address,
  including an alias.
- The draft comes from the account's own address and has no Cc or Bcc header, and its
  single To address, which is then the whole SMTP envelope, matches the single operator
  address pinned in code as a SHA-256 digest.

Other valid previews report
`approval: "host_confirmation"` and use the existing elicitation path, or
[`typed_ok`](#typed-ok-when-the-client-has-no-form) on a client without form support.

This flag was named `PMM_SEND_TOKEN_APPROVAL` before. The old name is now ignored, so
a deployment that still sets it returns to the form until it sets the new name, and a
client without form support needs a typed OK for every self-send. Before deploying this
version, set `PROTON_MCP_SEND_SELF_NO_FORM=1` if the deployment uses this path. Startup and the
installer warn while the old name is set. Keep it until rollback is no longer wanted:
earlier builds read only the old name and this one reads only the new name.

Eligibility compares the parsed address with the account's own address and
`PROTON_MCP_SEND_FROM` ignoring case, and exactly with the pin, which must be
canonical lowercase ASCII. No other folding or alias normalization is performed: a plus
tag or IDN encoding is a different address, which needs the form unless it is listed
itself, like every other address.
Whitespace around the header value and a plain ASCII display name do not change the
parsed address, so `To:pin@example`, `To:  pin@example` and
`"Plain Owner" <pin@example>` all qualify. Comments, groups, Unicode and encoded
display names are refused at preview, with the flag on or off.
The pin cannot be set or widened through configuration;
a draft's From still has to match the allowlist exactly.

The existing `send_draft(token)` argument redeems the token exactly once. The
server stores only its digest and binds the approval to the draft identity,
From, the separate To/Cc/Bcc lists, full SMTP envelope and exact message hash.
Its lifetime remains 300 seconds by default and at most 600 seconds. Reuse,
expiry, an unknown token or a changed draft or recipient envelope refuses delivery
and requires a fresh preview. The flag is read at startup and tokens do not survive
a restart.
Draft and envelope checks run again before the journal reservation and SMTP DATA.
Failures and uncertain outcomes never trigger an automatic send retry.

The token proves possession of a short-lived approval for one exact draft; it
does **not** attest to human consent because it passes through the assistant.
The safety boundary is the own-address recipient check, single use, exact draft
and envelope binding, and short lifetime. **`PROTON_MCP_SEND_FROM` is part of that
boundary: with the flag on, any address listed there receives mail with no form, so
list only addresses that are really yours.** With sending enabled, startup and the
installer repeat this warning while the flag is on. Any other recipient
takes the form, or a [typed OK](#typed-ok-when-the-client-has-no-form) on a client with no form, which
proves no consent. Proving it without a form requires out-of-band approval: design 2 uses
a link or code delivered through the operator's own independent channel; design 3 uses an authenticated
server-side approval queue. The typed OK goes past this boundary: it is on by default for a
client whose `initialize` omits elicitation, and `PROTON_MCP_SEND_TYPED_OK=0` closes it. It also
covers clients the operator names in `PROTON_MCP_SEND_TYPED_OK_CLIENTS`, none by default. A token
or any other configuration change must never widen this boundary further. Recipient and daily caps, TLS checks, cancellation, audit records
and budget reservations still apply.

| Variable | Default | Bound / behavior |
| --- | --- | --- |
| `PROTON_MCP_ENABLE_SEND` | `0` | `1` registers the two send tools (plus the token-free `review_send` view tool when views are on) and enables HTTP sessions/SSE for elicitation. With `PROTON_MCP_SEND_TYPED_OK` on, the default, it also lets a client whose `initialize` omits elicitation, or that `PROTON_MCP_SEND_TYPED_OK_CLIENTS` lists, send to any recipient on a [typed OK](#typed-ok-when-the-client-has-no-form), which the server cannot verify. |
| `PROTON_MCP_SEND_TYPED_OK` | `1` | Exact `1` or `0`. `1` allows the [typed OK](#typed-ok-when-the-client-has-no-form) for a client whose `initialize` omits elicitation, or that `PROTON_MCP_SEND_TYPED_OK_CLIENTS` lists, and logs a WARNING for each preview and send on it. `0` ignores that list and refuses every send from a client whose `initialize` omits elicitation that the own-address path does not cover. A client that declares elicitation gets the form with `0`, and with `1` unless the list names it. |
| `PROTON_MCP_SEND_TYPED_OK_CLIENTS` | empty | Off by default; an operator choice for a host that declares elicitation but never shows the form. Comma list of `clientInfo.name` values, matched exactly but ignoring case and surrounding spaces; empty entries are ignored. In the env file, write names without quotes, as in `Example Client,Other`: values are read literally, so a quoted name keeps its quotes and never matches. A listed client is treated as having no form: sends take the [typed OK](#typed-ok-when-the-client-has-no-form) and mailbox changes that need a form refuse at once with `confirmation_unavailable`. `clientInfo.name` is self-reported and unauthenticated, the same trust level as the elicitation flag, so any client holding the server token can claim a listed name. `PROTON_MCP_SEND_TYPED_OK=0` overrides the list. |
| `PROTON_MCP_SEND_SELF_NO_FORM` | `0` | Exact `1` or `0`. `1` skips the form when every recipient is the account's own address or on `PROTON_MCP_SEND_FROM`, or the sole recipient is the pinned own address. No other configurable recipient allowlist. Formerly `PMM_SEND_TOKEN_APPROVAL`. |
| `PROTON_MCP_SEND_FROM` | Empty | Exact ASCII addresses, the account's own and any [aliases](#sending-from-an-alias) to send from; an empty allowlist refuses all sends. With `PROTON_MCP_SEND_SELF_NO_FORM=1` they also receive mail with no form, so list only your own. |
| `PROTON_MCP_SEND_MAX_RECIPIENTS` | `10` | Total To/Cc/Bcc addresses per message; 1–50. |
| `PROTON_MCP_SEND_DAILY_CAP` | `20` | Rolling 24-hour send attempts reserved before SMTP; 1–200. |
| `PROTON_MCP_SEND_TOKEN_TTL` | `300` | Preview lifetime in seconds; positive, at most 600. |
| `PROTON_MCP_SEND_CONFIRM_TIMEOUT` | `120` | User form timeout in seconds; positive, at most 300. A timeout refuses the send with a clear error and never falls back. |
| `PROTON_BRIDGE_SMTP_HOST` | `127.0.0.1` | Only literal `127.0.0.1` or `::1`; no DNS or remote SMTP. |
| `PROTON_BRIDGE_SMTP_PORT` | `1025` | Bridge SMTP port. |
| `PROTON_BRIDGE_SMTP_TLS` | `0` | STARTTLS; `1` selects implicit TLS. |
| `PROTON_BRIDGE_SMTP_ALLOW_UNVERIFIED` | `0` | `1` explicitly permits unverified loopback TLS when no pin is configured. |

One call sends one message. The locked, append-only action log persists the daily
total across restarts; SMTP errors and uncertain outcomes retain their reservation.
Keep the same log path and its history to preserve this budget. It records send
attempts and results, the approval path, hashes, recipient domains/count and Message-ID,
without bodies, full recipient addresses, credentials or confirmation data. No token reaches
the service log either: every log record loses token-shaped text and any value the SDK echoes
from a malformed request. There is no automatic retry, undo or draft deletion; inspect uncertain results before sending
again. Send records are in the journal; `list_actions` shows send calls in its audit list.
SMTP uses the existing Bridge credential provider and checks the pinned certificate
before authentication. A peer certificate failure necessarily involves a TLS
connection, but permits no login or message submission.

## Guarded unsubscribe

Call `preview_action(action="unsubscribe", ids=[message_id])` (or use the existing
envelope query selector). Present the samples and target hosts/methods to the
user. Only after explicit approval, call `unsubscribe(token)`; the client then shows
the confirmation form with the target hosts and methods. Preview never visits a link; confirmation revalidates message identity, sender protection,
headers, links and the single-use token. The protected-sender policy and approved
personal-provider allowlist have the same semantics as cleanup.

An HTTPS URL from `List-Unsubscribe` uses POST only with exactly one
`List-Unsubscribe-Post: List-Unsubscribe=One-Click` header. Its only form body is
`List-Unsubscribe=One-Click`. Otherwise GET requires the sender's own domain or
an ESP explicitly reviewed for that sender. Header HTTPS targets are preferred,
then header `mailto:` targets, then HTML anchors recognized by unsubscribe/opt-out
text or URL. HTML anchors can only use GET.

Headers are fetched independently of displayed message bodies. A usable header
method needs no body fetch, and body changes do not invalidate that header approval.
A header mailto is usable only when guarded send is enabled and its recipient count
fits the send cap; otherwise discovery falls through to HTML links.
When headers supply no permitted method, discovery
reads up to 1 MiB of HTML MIME parts, independently of the display/snippet limit.
A permitted link within that bound can be approved even if more body remains;
otherwise the preview clearly reports no method and whether the scan was truncated.
Attachments are skipped, and reads preserve Seen flags.

Mailto requires guarded send to be enabled. The preview and confirmation show the
exact decoded `to`, `subject`, and `body`, including all recipients. RFC 6068 percent
escapes are decoded as UTF-8; literal `+` stays `+`, and RFC 2047 encoded-words in
the subject are decoded before preview and draft matching. For compatibility,
literal `=` and `/` are accepted in the address part, and `/` and `?` in field
values; an address-part `?` must be percent-encoded to distinguish it from the query
delimiter. Unsupported mailto fields
(including Cc/Bcc), ambiguous addresses, malformed escapes, and header injection
are refused. Approving `unsubscribe(token)` creates an **unsent draft**, reports
`pending_send`, and returns its `send_preview`. Review that preview and approve it
before calling `send_draft(send_preview.token)`. The draft must match the discovered
recipient list, subject and body before a send token is issued; subsequent edits
invalidate that token. A draft is never counted as a completed unsubscribe.

The existing guarded-send rules still apply: `PROTON_MCP_SEND_SELF_NO_FORM=1` allows
token-only sending solely to the user's own addresses; third-party unsubscribe mail
requires host confirmation, or a [typed OK](#typed-ok-when-the-client-has-no-form) on a client
with no form. Send caps, token lifetime, TLS checks, and durable send
journaling are unchanged. Unsubscribe never submits SMTP itself. If a send preview
fails after draft creation, the response retains the draft ID and a clear error;
inspect that draft before requesting another send preview.
Mailto targets have no sender-domain ownership check; they rely on the two explicit
approvals and host confirmation for third-party recipients.

The stdlib has no public-suffix database. To avoid guessing registrable domains,
[`unsubscribe_http.py`](src/proton_mail_mcp/unsubscribe_http.py) contains a small
reviewed ownership list: `github.com`, `proton.me`, and the reserved example
domain `example.com`. Those senders may use sibling/subdomain links within that
boundary. Other senders require an exact hostname match; valid sibling links
are conservatively refused until their ownership boundary is reviewed.
`ESP_DOMAINS_BY_SENDER` is an initially empty mapping from a sender boundary to
its reviewed ESP boundaries. Adding an ESP for one sender never authorizes it
for other senders. Private/shared suffixes such as `github.io` must not become
ownership boundaries. Changes to either constant require code review.

HTTP execution accepts only HTTPS on port 443. All redirects are refused, including
same-domain redirects. HTTP requests have no credentials, cookies, proxies or
response-body reads. Non-HTTPS/non-mailto targets are refused. DNS must resolve only to
public addresses; the numeric connection is pinned while TLS verifies the
original hostname. Each request has a maximum five-second deadline within the
overall execution budget.

Each attempt is journaled before HTTP or draft APPEND using message/header/URL
digests, with progress appended afterward. Pending mailto drafts are reported
separately from completed requests and uncertain attempts. The unsubscribe record
tracks draft preparation; final delivery is recorded separately by `send_draft`.
When every item is a completed HTTPS request or a prepared draft, a run with drafts
has `status: pending_send` and `partial: false` in its result and journal, and
`outcome: pending_send` in its audit. Uncertain or unattempted items keep the run
`partial: true`.
No sender, recipient, URL, mailto subject/body or query secret is written to the
unsubscribe log. `list_actions(token=...)` recovers outcomes and confirmed pending
`draft_ids` after a lost response, without disclosing draft content or approval
tokens. Inspect each recovered draft and request `preview_send(draft_id)` before
approving any send. That fresh preview binds the current draft; it does not recover
the lost mailto-specific content check or send token. Compare its recipients,
subject and body with the original unsubscribe preview before approving it.
An uncertain outcome may already have changed the subscription or saved a draft;
review it before requesting another preview. There is no automatic retry or
unsubscribe undo.

## Manual connector folder rules

**Beta: folder rules.** The rules engine is not fully reliable yet; preview first
and check the results.

Rules live in the reviewed repository file
[`folder_rules.json`](src/proton_mail_mcp/folder_rules.json), initially empty.
For example, a reviewed change can add:

```json
{"version": 1, "rules": [
  {"field": "from", "contains": "notifications@github.com", "folder": "Folders/GitHub"}
]}
```

At most 20 rules match case-insensitive `from`, `to` or `subject` substrings.
The first matching rule wins. Destinations must already exist as real
`Folders/` mailboxes; labels and system folders are refused. Invalid or missing
configuration fails closed, and a config change invalidates outstanding rule
previews. No tool can edit the file. Rules saved with `add_rule` run after these,
from their own store; see [Saved custom rules](#saved-custom-rules).

Rules sources are restricted to `INBOX`, case-insensitive in ASCII, for both
manual rules and the timer. The server's actual LIST name is used for selection;
whitespace, Unicode lookalikes, prefixes and every other mailbox name are refused.
Benign LIST flags, including unknown non-special-use flags, are accepted.
`\Drafts`, `\Sent`, `\Junk`, `\Trash`, `\All`, `\Archive`, `\Flagged`,
`\Noselect` and `\NonExistent` veto an INBOX entry. The timer keeps one INBOX
high-water mark across case variants.
The rules file's `folder` field is the destination; source overrides are unsupported
and rejected. Source refusals appear in preview's `blocked` list with no token;
confirmation rechecks the entire plan before any move.

Run `preview_rules()` manually and present each rule's counts, samples and
protected skips. `limit` is an integer from 1 to 200 (default 200); `order` is
`oldest` (default) or `newest`, sorted by INTERNALDATE and then numeric UID.
`total_matches` counts all matches, `batch_count` counts the selected reviewed
matches (including protected skips), and `remaining` counts matches after this batch.
For 545 eligible matches, full default batches approve 200, 200, then 145;
the first reports 345 remaining.
Time budgeting can make batches smaller than `limit`, with a token and
`next_cursor` still returned. A new preview chain first reviews up to 4 matches
and grows that batch only as far as the measured review time allows. While fewer
than `limit` matches remain, it keeps growing as the refit times allow, so a small
rule, such as 38 matches, can finish in one call. If a larger review misses the
deadline, the call returns the batch already reviewed, with its token. Once per
chain, if finishing the audit leaves at most four reviewed matches and more can
fit the requested limit, the call instead returns a partial reply without a token.
The next call uses a warm one-row review to try a full batch. Later calls fit the
fixed and per-row review costs and reach `limit` when the call budget allows;
on a slow server, 545 matches can take one partial reply followed by batches of
200, 200 and 145.
Once a one-row review has shown that a review costs half the time left in a call
or more, two cannot fit, so the call skips the one-row review and its only review
takes a whole batch: up to `limit` matches of the rule the first pending match
belongs to. `apply_rules` reviews once per rule, so a batch across rules would leave
it no time to move anything at this cost. On such a slow mailbox each rule therefore
needs its own approval, or several if its batch is halved, and `order` holds only
within a rule: one rule's later matches can come before another rule's earlier ones.
This timing stays with the server, so a fresh preview after an apply skips the
one-row review too. What the last chain learned about overruns does not stay, so
that first batch can miss the deadline again. A review costing more than all the
time left still runs one row. If a whole batch misses the deadline, the call
returns a partial reply without a token and the next call reviews half as many.
More runs are needed when `remaining` is positive.
A message whose copies have inconsistent sender headers or ambiguous label
memberships leaves the batch and stays in INBOX; the review carries on with the
rest. `excluded_count`, `excluded_ids`, `excluded_truncated` and `excluded_reasons`
report it, as for `preview_action`, in every later reply of that cursor chain while
it stays in INBOX, even if it also has a protected copy, which then does not appear in
`blocked`. It never enters the token, and `apply_rules` rechecks only the rows the token
holds, so it cannot refuse them. `total_matches` still counts it; `batch_count` and
`remaining` do not. If only excluded messages are left, the reply has no token or cursor
and says so. A fresh preview checks the message again.
A message whose identity or sender cannot be verified still refuses with `Rules
batch has unresolved messages`. On a whole-batch call the reply is instead partial,
with no token, and the next call reviews half as many, so the clean rows before that
message are still served; the review of it alone refuses, and so does every retry.
Rows behind it cannot be reached in that `order`. A larger review after a one-row
review refuses the call at once.
Protected rows stay in INBOX and can appear in later previews; a batch consisting
entirely of protected rows cannot authorize moves. After the owner approves
the preview, call `apply_rules(token)`; the first manual run requires the owner's OK.
These are connector rules, not Proton filters. The existing move path handles chunking, journal-before-move,
timeouts and sender guards. Apply returns one or more `log_ids`; pass each to
`undo_action` to reverse its moves. Moves preserve read state by default. To also
mark read, approve `preview_rules(mark_read=True)` and then call
`apply_rules(token)`. `mark_read` must be a JSON boolean; `"yes"`, `"true"`, `1` and
similar values are refused rather than read as true. The token alone determines the
read-state option; confirmation has no `mark_read` argument. Undo restores both
location and each message's prior read state. Rules never trash or delete mail; the
timer never changes Seen.

Each `preview_rules` call has a 20-second budget, or the shorter configured
call/preview budget. Discovery and larger reviews leave a response reserve;
only the first one-row review may use that reserve to finish a batch. A command
already in progress can carry the reply past the I/O cutoff. Discovery uses
existing indexed headers to narrow candidates without an inline index rebuild or
catch-up. Global INTERNALDATE ordering can require scanning source candidates
first; raw From matching retains its sender checks. Up to `limit` rows then enter the
strict cross-folder membership review. Large or unindexed folders may need several
calls to finish that review. Single-row calls, partial replies and batches with
matches remaining include the current stage and per-stage milliseconds in `reason`
for diagnosing slow previews.

A deadline before any complete review returns available match counts with
`partial=true`, `total_is_exact=false` and no approval token. A completed review
batch keeps its token and cursor if a later review runs out of time or detects
changed mailbox membership, except for the once-per-chain audit hold described
above, which returns a partial reply without a token. A whole-batch call has no
earlier batch to keep. Incomplete discovery counts,
including `remaining`, are lower bounds until discovery completes. Pass `next_cursor`
as `cursor` to `preview_rules` with the same folder, limit, order and mark-read options to retain
completed scan batches. A completed preview also returns `next_cursor` when more
matches can be reviewed. Each resulting approval still covers only its displayed
batch and needs separate approval. Cursors never authorize moves; they expire
after ten idle minutes or server restart. A reply spends its cursor. A call that
ends in an error instead, either a refusal or a mailbox failure such as a lost Bridge
connection, keeps the cursor it was given and its saved work, so retry with it; a
permanent refusal repeats until a fresh preview. Changed options refuse without
spending the cursor. A rules change or a UIDVALIDITY reset
invalidates retained evidence and requires a fresh preview. Between calls,
arrivals and removals are reconciled against fresh mailbox evidence: surviving
UIDs keep their completed discovery work, removed UIDs are dropped, and new UIDs
are scanned.
Reviewed UIDs are tracked so reordered matches do not skip the next batch. After
applying a batch, start a fresh preview to discover the updated INBOX.

After a stall without saved progress, continuations reduce FETCH batch sizes down
to one UID and discard completed review timings so the next call calibrates again.
A larger review that overruns halves the later review cap and keeps its elapsed
time as a lower bound for that batch size when sizing retries. A whole-batch call
that overruns counts as progress, since each one halves the batch, so the stall
count starts when the review is down to one row. Smaller batches
can require more preview → approve → apply rounds. Six consecutive calls without
saved progress stop with a `cannot make progress` reason and no `next_cursor`.
Only replies with a non-null `next_cursor` request continuation; an expired saved
preview requires a fresh call.

Confirmation uses only the token's exact UIDVALIDITY, UIDs and per-message
fingerprints; it never reruns the selector or includes newly arrived matches.
When SELECT supplies UIDNEXT, apply can reuse the preview's in-memory raw identity
audit after the same scope, expiry, checksum and mailbox-allocation checks used by
preview. It reconciles changed allocations and reads matching occurrences' headers
and policy evidence fresh. This audit supplies discovery only; the approved rows,
confirmation, expiry and single-use token checks still authorize every move.
Without SELECT UIDNEXT, apply retains the full raw reread. Reuse accepts the
same-UID content-change trade-off described under [Guarded cleanup](#guarded-cleanup):
an unrelated row gaining a second matching Message-ID under the same UID is not
re-read, so apply would move the approved message despite that hidden duplicate.

Before approval and again at confirmation, rules validate raw `Message-ID`
fields on selected candidates and matching cross-folder copies, including All
Mail and protected rows. Cross-folder discovery reuses a certified raw-identity
index where available, otherwise audits raw Message-ID fields in bounded
batches, including identities hidden in a repeated field whose first
value differs from the envelope. For All Mail, the rules-preview raw fallback
derives `UID FETCH` ranges of at most 200 UIDs from UIDNEXT instead of issuing
`UID SEARCH ALL` or per-ID `SEARCH HEADER Message-ID`. It checks the deadline
before each range and saves completed ranges in the cursor after every batch, so a command that
overruns the call window can still leave progress for the next call. This path
also applies to servers without CONDSTORE and retries smaller ranges after repeated
interruptions. Serialized range state is capped at 16 MiB; exceeding it refuses
approval. Certified-index validation retains its STATUS and UID-set checks,
which may issue `UID SEARCH ALL`. On supported servers, every resumed scan
reconciles CHANGEDSINCE deltas before reusing completed audit batches.
Rules previews retain certified discovery in memory without updating saved
identity indexes. Without CONDSTORE, completed raw audits are also cached per
client and mailbox for ten minutes, with at most 128 cached mailboxes. Each reuse
reconciles current mailbox membership and reads raw identity fields only for new UIDs;
matching candidates still receive fresh identity and sender checks. Missing,
repeated or malformed candidate fields
refuse the batch; entirely unrelated messages are outside this identity lookup.
Tokens bind every reviewed source UID, including protected skips, and confirmation
rechecks their identities. Skipped candidates are also checked between mutation
chunks and rule groups. The raw identity and sender fields are fetched with
`BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SENDER REPLY-TO)]`, preserving Seen
flags. An unfinished preview audit returns a cursor with no token until complete
evidence is available. At confirmation, `apply_rules` can reuse the kept All Mail
audit and read matching occurrences fresh under the checks and accepted trade-off
above; this path does not repeat a full All Mail audit or `UID SEARCH ALL`.
Missing or unusable evidence requires new discovery, which can refuse cleanly
if it cannot fit the budget.

To create a destination, call `preview_create_folder("Folders/GitHub")`, show the
exact name, and obtain approval before `create_folder(token)`. Names must stay
under `Folders/`; system folders, labels, empty/relative segments, controls,
wildcards and names over 500 characters are refused. Names are preserved exactly.
The short-lived token binds that name. Both steps check LIST: an existing folder
returns `exists: true` without issuing or consuming a token, including one created
between preview and confirmation. Creation is journaled; there is no folder delete
or rename operation. **Removing a created folder is manual in the Proton app**,
not an `undo_action` operation.

The optional Linux timer is **off by default; enabling the timer needs the owner's OK**.
Nothing installs or enables it automatically, and no MCP tool can start it. It runs
`proton-mail-mcp-rules-run` for INBOX every five minutes, with a default cap of 50
moves (`PROTON_MCP_RULES_MAX_MOVES=1..200`). Interval and cap are proposed defaults
for the owner's OK. The CLI accepts only INBOX (case-insensitive) and uses the existing Bridge
credentials, certificate settings and protected policy;
it requires loopback IMAP and makes no HTTP or SMTP requests.

After approval, copy the two [example units](examples/systemd/) into
`~/.config/systemd/user/`, adjust the service's `ExecStart` to the installed CLI,
and create a private (`0600`) `~/.config/proton-mail-mcp/rules.env` with the existing
Bridge environment settings (including the Bridge password on Linux and the
certificate path), plus the server's `PROTON_MCP_ACTION_LOG`,
`PROTON_MCP_SAVED_RULES` and `PROTON_MCP_PROTECTED_CONFIG` if it sets them, so the
timer reads the same journal, saved rules and sender policy. No MCP bearer token is
needed. Then run:

```sh
systemctl --user daemon-reload
systemctl --user enable --now proton-mail-mcp-rules.timer
```

Each run reloads the reviewed rules and the saved custom rules, and only moves: a saved
rule's `mark_read` never applies here. A private `rules-state.json` beside the action
journal stores each source's UIDVALIDITY and high-water UID; use a separate journal
directory for each account. On first installation, the timer starts at UID zero
and drains the existing backlog over successive capped runs.
A UIDVALIDITY change logs a reset to the current maximum, moving nothing. Later
runs process UIDs above the mark in ascending order, examining at most 200 new
candidates and moving at most the configured cap. Excess waits for the next run;
the atomic mark advances only past completed moves or deliberate skips, including
nonmatches and verified protected senders. Old mail below the mark stays untouched.
Existing saved high-water marks retain their meaning. The timer uses ascending
UID order to keep that mark contiguous, while manual previews use the requested
date order. Membership safety checks still read older headers without moving them.

A nonblocking file lock prevents overlapping runs; the second exits successfully
without accessing mail. Invalid rules/state, a missing destination or an unwritable
journal fail closed with a nonzero exit before moves. A refused source is logged
without its supplied name, exits nonzero before SELECT or mutation, and leaves
the high-water mark unchanged. The timer never creates
destinations or changes Seen flags. Interrupted work stops with journaled undo IDs;
a pending state marker blocks unattended retries after a crash or uncertain outcome.
Inspect the journal and mailbox before manually clearing that marker; do not delete
the state file to retry, since that can make old messages eligible again.

To undo, first stop the timer with `systemctl --user disable --now
proton-mail-mcp-rules.timer`, then pass each run's printed `log_ids` to
`undo_action(log_id)` (or recover IDs with `list_actions` by time). Read and unread
states are preserved both ways. Review before resuming: undo gives restored mail
new source UIDs, so it may match the rules again.

## Sieve filters

`preview_sieve_filter(name, folder, conditions, match="all", mark_read=False)`
validates one Proton Sieve filter for new incoming mail, renders its exact script and
returns a single-use token bound to that script. `create_sieve_filter(token)` is the
guarded confirm step. Proton runs filters when mail arrives; a filter does not move
existing mail (use `preview_action` or the connector rules for that).

**Applying a filter is not possible through Proton Mail Bridge, so
`create_sieve_filter` always refuses, and no setting changes that.** This was checked
in the sources of Bridge `v3.27.1`, the IMAP server it embeds (`gluon`) and the Proton
API client it pins (`go-proton-api`). Bridge serves mail clients only IMAP and SMTP on
loopback (its other sockets serve its own GUI and single-instance check) and has no
ManageSieve (RFC 5804) service. Its IMAP server advertises only `IMAP4rev1`,
`STARTTLS`, `IDLE`, `UNSELECT`, `UIDPLUS`, `MOVE`, `ID` and `AUTH=PLAIN`, so neither
METADATA (RFC 5464) nor IMAPSIEVE (RFC 6785) is available. None of the three mentions
Sieve, and the API client has no filter call. Proton creates filters in its own apps,
under Settings > All settings > Proton Mail > Filters > Add sieve filter, through the
Proton API behind them, which needs a signed-in Proton account session (the account
password, not the Bridge password). This server holds only the Bridge password, talks
only to the loopback Bridge and never signs in to the Proton API, so its only apply
backend (`unavailable`) refuses with
`Sieve filters cannot be applied: Proton Mail Bridge offers mail clients only IMAP and SMTP, with no ManageSieve, Sieve or filter interface, and this server has no other apply path; nothing was changed`.
That refusal comes before any confirmation form, filter journal record or mailbox
access, and leaves the token unused; the call's audit records (`started`, then
`refused` with this reason) are its only writes. The preview reports the backend in
`apply` (`{"backend": "unavailable", "available": false, "reason": ...}`) and still
issues its token through the same binding a supported backend would redeem; here that
token can only be refused or expire. A previewed script can be pasted into Proton's
"Add sieve filter" editor by hand; that manual step is outside this server's checks,
journal and audit trail.

A request names the filter, its target and its conditions:

```json
{"name": "GitHub notifications", "folder": "Folders/GitHub", "mark_read": false,
 "conditions": [{"field": "from", "contains": "notifications@github.com"}]}
```

- `conditions`: 1 to 10 objects, each `{"field": "from" | "to" | "subject"}` plus
  exactly one of `"contains"` or `"is"`. `from` and `to` match the address in the
  From or To header (`address :all`), `subject` the decoded Subject header; matching
  is case-insensitive (`i;unicode-casemap`, the comparator Proton's own filter editor
  writes). Repeated conditions are refused. `match` is `all` (`allof`, the default)
  or `any` (`anyof`).
- Values and the name are single-line text, 1 to 500 characters (the name at most
  100) without surrounding spaces. CR, LF, tab, NUL and every other control,
  format (bidi overrides, zero-width), surrogate, private-use, unassigned and line or
  paragraph separator character is refused, and so is `${`, because the spam guard
  below loads Sieve variables. The name labels the filter in Proton and is never part
  of the script.
- `folder` is the canonical name of an existing real folder under `Folders/`, as
  `search_mail` reports it; `Folders/Work/Receipts` files into `Work/Receipts`.
  LIST must show it as a real folder at preview and again before applying. Labels,
  system folders, views, folders with a special-use attribute (`\Drafts`, `\Sent`,
  `\Junk`, `\Trash`, `\Archive`), names spelled like a Proton system folder (Inbox,
  Archive, Spam, Trash, ...) and a path that a label or another folder differing only
  in case also names are refused, since `fileinto` could not tell them apart.
- The only actions are `fileinto` that folder and, only with `mark_read=true`,
  `addflag "\\Seen"`. A filter never discards, rejects, redirects, replies, keeps a
  copy, moves to Trash or Spam, expires or changes other flags. `mark_read` must be a
  JSON boolean; `"yes"`, `"true"`, `1` and similar values are refused.

Every script starts with the spam guard of the filters Proton's editor generates, so a
filter never moves mail that Proton classified as spam. The request above renders
exactly this script:

```sieve
require ["include", "environment", "variables", "relational", "comparator-i;ascii-numeric", "spamtest"];
require ["fileinto", "imap4flags"];

# Never run on spam, like the filters Proton's editor generates.
if allof (environment :matches "vnd.proton.spam-threshold" "*",
          spamtest :value "ge" :comparator "i;ascii-numeric" "${1}") {
    return;
}

if allof (
    address :all :comparator "i;unicode-casemap" :contains "From" "notifications@github.com"
) {
    fileinto "GitHub";
}
```

Values are written as RFC 5228 quoted strings: `\` becomes `\\` and `"` becomes
`\"`, nothing else. A strict parser for the Sieve grammar
([`sieve.py`](src/proton_mail_mcp/sieve.py)) then reads the script back and refuses
it unless its commands, tests, tags and decoded strings are exactly those of the
filter, built separately from the filter's fields; a value such as
`x"; discard; #` therefore stays one string. Without a supported apply path, Proton
itself has not checked these scripts: the dialect copies the commands, extensions and
comparator that Proton's web client generates for its own filters. During
development the scripts also parsed with the independent `sievelib` parser, after
registering the `spamtest` and `include` extensions it lacks; that one-off check is
not part of `make check`.

The token binds the name, folder, conditions, match, mark-read choice, the exact
script, its SHA-256 and the backend name (`action_hash`). Before applying, the
stored script must render again byte for byte from the bound filter and pass the
parse-back check, the folder must still be listed, and the backend must report no
existing filter of that name in any letter case: existing filters are never
replaced, edited, disabled or removed, and the backend interface can only add one.
The journal record follows, then the token is claimed and the backend is called
once. A wrong action, changed payload, replay or expiry refuses as for every other
token; so does a backend that changed after the preview.

The journal record (`action: "create_sieve_filter"`) holds SHA-256 digests of the
name and script, the folder, match, condition fields and operators (never their
values), the mark-read choice and the backend, with `uncertain` journaled before the
call and `done` after it. If the intent record cannot be journaled, the call fails
before claiming the token or calling the backend. Otherwise a failed call, or an
outcome that cannot be journaled, returns `partial: true` with one of three states.
`created: true` means the backend added the filter but a later journal append
failed. `attempted: true` without `created` means `uncertain` was journaled and no
success followed: check Proton's filter list before trying again. `attempted: false`
means the backend was never called, so no filter was created (the entry stays
`not_attempted`, or keeps its `uncertain` line if the deadline expired right before
the call). The audited outcome counts the filter once, as `done`, `uncertain`
or `remaining` (never attempted), reports any unfinished journal as
`outcome: "partial"` and also carries `script_sha256`. `undo_action` refuses these
records: remove a filter in the Proton app.

[`sieve.py`](src/proton_mail_mcp/sieve.py) keeps script generation and the apply
backend apart from the guarded flow; [saved custom rules](#saved-custom-rules) reuse
both. A backend has a `name` and three methods: `status()` returns
`{"available": bool, "reason": ...}` without side effects, `filter_names(timeout=...)`
lists existing filter names read-only, and `create_filter(name, script, timeout=...)`
adds one enabled filter with exactly that script, raising on any failure or doubt.
`MailClient.sieve_backend` holds it. `None` means the refusing `unavailable` backend,
and no setting or environment variable selects another: only tests plug in offline
fakes. A real backend first needs a supported Proton interface for filters.

## Saved custom rules

`preview_add_rule(name, folder, conditions, match="all", mark_read=False,
include_matches=False)` previews saving one custom folder rule, and `add_rule(token)`
saves it after the user confirms the exact rule in the client's form. A saved rule
takes effect wherever rules run: `preview_rules`/`apply_rules` and the optional
[rules timer](#manual-connector-folder-rules) apply it to INBOX after the packaged
[`folder_rules.json`](src/proton_mail_mcp/folder_rules.json) rules, through the same
guarded move path. Protected senders, 200-match batches, continuations, the journal
and undo are unchanged. Saving a rule moves no mail. `list_rules()` lists every rule
in the order rules run (`kind` is `built_in` or `saved`) with `built_in_count`,
`saved_count`, `max_saved_rules` and the backend `apply` status.

A saved rule uses the [Sieve filter](#sieve-filters) model and its validation
unchanged: a name, 1 to 10 `from`/`to`/`subject` conditions with `contains` or `is`,
`match` `all` or `any`, an existing real `Folders/` target that passes the same LIST
and shadow checks, and `mark_read`. Validation messages therefore name Sieve filter
fields. For example:

```json
{"name": "GitHub notifications", "folder": "Folders/GitHub", "mark_read": true,
 "conditions": [{"field": "from", "is": "notifications@github.com"}]}
```

The rules engine decides a saved rule the way its Sieve script reads. `from` and `to`
compare each address (`local@domain`) in the From or To header, never display names;
`subject` compares the decoded Subject. `contains` is a substring and `is` the whole
value, both case-insensitive (Python `casefold`). `match: "all"` needs every condition
and `any` one of them. Packaged rules keep their substring match on the whole From or
To text. As for packaged rules, a message whose From or To header the server's
envelope cannot parse safely (group syntax, encoded or non-ASCII display names) never
matches a `from` or `to` condition. Without complete indexed headers, each rule
retains its IMAP search narrowing:
`from`/`to` values narrow by their longest word, `subject` by its whole value,
and `any` uses an `OR` chain. Bridge compares raw subject text, so an RFC
2047-encoded subject can be missed on that path. Manual previews use cached header
predicates where possible and evaluate candidates locally in resumable batches
within the preview deadline. Unknown indexed headers remain candidates. Rules
that cannot narrow safely scan INBOX candidates within that same deadline.

The first matching rule wins: packaged rules first, then saved rules in the order they
were saved. A saved rule with `mark_read: true` marks its own matches read in
`apply_rules`, as `preview_rules(mark_read=True)` does for all rules; the preview lists
each saved rule's `name`, `conditions`, `match` and `mark_read`, and the confirmation
form says when rules mark mail read. **The timer never changes Seen**: it only moves a
saved rule's matches.

`preview_add_rule` returns the normalized rule, `rule_sha256` (the SHA-256 of its
canonical JSON), the same rule as a Proton Sieve `script` with `script_sha256`, the
backend's `apply` status and a single-use token. The script is informational: pasted
into Proton's "Add sieve filter" settings by hand, it would make Proton file new mail
itself, outside this server. `include_matches: true` adds `matches` from one read-only
`UID SEARCH` of INBOX and an envelope fetch of at most the 200 newest candidates:
`count` (matches among the `scanned` candidates), `candidates`, `total_is_exact` and
up to five newest `samples` with their canonical handles. It ignores earlier rules and
protected senders; `preview_rules` decides what moves. If Bridge refuses that read or
it runs out of time, `matches` reports `partial: true` and the token is still issued
(a broken connection fails the preview, as elsewhere). `mark_read`
and `include_matches` must be JSON booleans. A name that a saved rule already uses in
any letter case or Unicode compatibility form is refused, as are a full store and an
invalid store.

The token binds no messages. It binds the rule, `rule_sha256`, the script and
`script_sha256`, the backend name and the store target: a digest of the store's real
path (symlinks and `..` in its directory resolved) and of the deepest directory on that
path that exists at preview, which is the store's own directory once it exists.
`add_rule` refuses before any form when the backend cannot save, for example when the
store is full or invalid. After the user confirms the exact rule in the form (name,
folder, conditions, match, mark-read choice, backend and `rule_sha256`), it renders the
script again byte for byte and checks both digests, and it requires the same backend
and store, the folder to pass the LIST and shadow checks again and the name to be
still unused. The same store means the path still resolves to the same real path and
the directory the token bound is still that directory, not a replacement; otherwise
`add_rule` refuses ("Saved rules store changed after preview") before the journal, the
token or the store changes. A token previewed while the store's directory was missing
names the directory that existed then, so it still saves once an earlier save created
the rest. From this check on it holds open the store's directory, or the deepest
directory on its path that exists, and the name check and the save use only that
directory and the missing ones they create below it, even if a symlink in the path
changes or a directory on it is replaced meanwhile. It then journals an `add_rule`
intent record, claims the token and appends the rule once. The record holds SHA-256
digests of the name, rule and script, the folder, match, condition fields and operators
(never their values), the mark-read choice, the backend and the store digest. Like
`create_sieve_filter`, an interrupted call returns `partial: true` with `added` and
`attempted`: `added: true` means a later journal append failed; `attempted: true`
without `added` means the outcome is uncertain, so check `list_rules` and the action log
before trying again; `attempted: false` means nothing was written. The audited outcome
counts the rule once and carries `rule_sha256`. `undo_action` refuses these records.

### The saved rules store

Saved rules live in `saved-rules.json` beside the action journal (by default
`$XDG_STATE_HOME/proton-mail-mcp/saved-rules.json`) or at `PROTON_MCP_SAVED_RULES`,
never in the packaged `folder_rules.json`. The rules timer reads the same file when
it runs with the same `PROTON_MCP_ACTION_LOG` or `PROTON_MCP_SAVED_RULES`. The file
holds `{"version": 1, "rules": [...]}`, each rule in its canonical request form, and
at most 20 rules. It must be a private regular file: owned by the server's user,
without group or other permissions (`0600`), with one link and not a symlink. A missing
file holds no rules. Anything else fails closed (a wrong version, a repeated key or
name, an invalid rule, more than 20 rules or an oversized file): `preview_rules`,
`apply_rules`, the timer, `preview_add_rule`, `add_rule` and `list_rules` refuse until
the file is fixed or moved.

`add_rule` only appends. Under a private lock file beside the store
(`saved-rules.json.lock`; one that is not a regular file of the server's user with one
link is refused and left unchanged), it writes every existing rule, unchanged and in
order, plus the new one to a `0600` temporary file, checks that this reads back as
exactly those rules, renames it over the store in one step and verifies the result.
It opens the store's directory one component at a time without following a symlink
and finds the lock, temporary file and store in that open directory by name. Missing
directories are created the same way, below the directory the token bound, the last
one `0700`. Saving a rule
changes the rules digest, so outstanding `preview_rules` tokens refuse with "Folder
rules changed after preview". No tool edits, reorders or removes saved rules: to remove
one, stop the server and the timer, edit the file by hand (keeping mode `0600`), then
preview again.

### The rules backend

`add_rule` reaches the store through the [Sieve filter](#sieve-filters) backend
interface. `MailClient.rules_backend` holds the backend; `None` means the local store
backend (`local_rules`, [`rules_store.py`](src/proton_mail_mcp/rules_store.py)). It
implements `status()`, `filter_names()` and `create_filter(name, script)` and reads the
rule back from the exact reviewed script, so only a rendered rule can be stored. A
supported Sieve backend could be selected there later to create Proton filters
instead. Today the Sieve backend stays `unavailable` and refuses, `create_sieve_filter`
is unchanged, and no setting selects another backend. Saving rules uses no Proton
account API and no network beyond the loopback Bridge.

## Guarded cleanup

First call `preview_action`, show its count, samples, target and blocked results to
the user, and wait for explicit approval. Only then call the matching action with
its token. Cleanup action descriptions repeat this requirement, and their MCP
annotations are `destructiveHint=true`, `readOnlyHint=false`. The server enforces
the token flow and, by default, asks the user to confirm the exact change in the
client's form before redeeming a token. With the operator opt-out it cannot verify
that a human approved a conversation; treat the token as authorization and keep it
private.

For example, preview archiving newsletters with:

```json
{"action": "archive", "query": {"folder": "INBOX", "from_": "news@example.test", "before": "2026-01-01"}}
```

`query` accepts `folder`, `from_` (or `from`), `to`, `subject`, `since`, `before`;
`folder` defaults to `Labels/<label>` for `remove_label` and INBOX otherwise.
It selects the complete matching set, without search
pagination. Alternatively, pass `ids: ["<message@example.test>"]` using RFC
Message-ID header values, not IMAP UIDs. For moves and read-state changes, all
matching UIDs in one query source folder are approved together, including repeated
IDs in the default INBOX.
ID-only selectors and queries without an explicit folder still refuse IDs shared across source
folders; set `query.folder` to select one source explicitly.
Cleanup accepts unambiguous dot-atom Message-IDs; unusual quoted or obsolete
forms require manual handling. Surrounding whitespace is trimmed and bracketless
IDs are wrapped in `<...>` before lookup, preview, token binding and execution
checks. Message-ID matching is otherwise exact and case-sensitive; case variants
are distinct identifiers. Cleanup `text` queries are refused because
Bridge must decrypt message bodies to evaluate them. ID lookup uses ordinary
source folders; a query can explicitly select All Mail for label additions.
Folder moves still require an ordinary source folder.

Source query previews pin `(UIDVALIDITY, UIDNEXT, EXISTS)` and exclude all UIDs
at or above that UIDNEXT. A cheap UID SEARCH verifies that the exact set below
the boundary still exists after fetching. Append-only growth is accepted only
with unchanged UIDVALIDITY and consistent count/UIDNEXT growth. A concurrent
move-out retries that folder inventory twice within the same preview deadline;
continued churn refuses with "mailbox is changing; retry" and no token.
UIDVALIDITY changes refuse immediately. A later visit to that source uses the
same low-UID proof; other folders' membership/identity inventories stay strict.

Execution refreshes each chunk's approved rows before mutating them (at most 200
for moves and read-state changes, 50 for label actions), including identity,
protection, fingerprint, membership and applicable read-state checks.
Folder moves, trash and archive with complete identity and label evidence refresh only their
approved UID handles, including queries that use the implicit INBOX source.
Implicit-source approvals also pin physical-folder UIDVALIDITY and UIDNEXT to
retain cross-folder identity ambiguity checks. Unexpected folder changes fall
back to complete discovery. If the destination supplies UIDNEXT,
recovery preparation uses that append boundary instead of another inventory.
Preview discovery time is then excluded from the measured per-row verification
cost. Missing or invalid certificates retain full discovery. Labeled trash
approvals also keep discovery because servers can remove those labels on MOVE. The shared executor
also applies the 48-second response ceiling before choosing each physical chunk.
It also retains the final per-row check before each mutation. A continuation
never repeats the original selector. Changed rows become `blocked`, remain
unchanged, and are excluded from subsequent continuations. They require a new
preview. A different action/target and concurrent mutation are refused.
After a lost response to a folder `move`, retry the original preview token or the
latest continuation received: either can authorize only that same unfinished
run's untouched, unblocked remainder, within the fixed 30-minute resume deadline
established by its first successful claim. Tokens remain in memory; restart or
expiry requires inspecting the run and previewing the
remainder again. Completed runs refuse all their tokens.
`move_to_trash` and `archive` take no params. `move_to_folder` requires
`params: {"folder": "Folders/Receipts"}`. Label actions require
`params: {"label": "Reviewed"}` for `Labels/Reviewed`.

`mark_read` and `mark_unread` take no params: use `preview_action` with that action,
obtain approval, then call `set_read_state(state="read", token=token)` or
`set_read_state(state="unread", token=token)`. The cap is
200 messages, or the bound of a [bounded approval](#bounded-approvals); any
protected sender blocks the whole action, as for manual moves.
Drafts and Sent are refused at preview and confirmation, by name (including case,
whitespace and `Folders/` variants) and by IMAP special-use flags on renamed folders.
The same refusal applies to moves with `mark_read=true`.
Only `\Seen` changes. Undo restores each prior read state, including messages that
already had the requested state. To mark read while moving, preview
`move_to_folder` with `params: {"folder": "Folders/Receipts", "mark_read": true}`,
then confirm with `move(target="Folders/Receipts", token=token)`. The flag is shown in the preview and
bound into its token; omitting it at preview defaults to false. Confirmation has
no `mark_read` argument. A read-state change after preview requires a fresh preview.
Both the move and prior read state are journaled for undo. The timer has no such option.

Previews use read-only mailbox selection and batched header-only metadata fetches.
They never change messages or flags. A batch over 200 returns its count without
a token. An incomplete preview, missing/ambiguous identity, or any protected
message also cannot authorize changes. Narrow the query and preview again;
protected messages are never silently dropped from a batch.

Cleanup narrows source candidates with server-side `UID SEARCH` using the selector's
recipient, subject and date criteria, then rechecks substring and date semantics
locally. Search and preview use the same local sender matcher, including decoded
display names and Unicode case-folding. Sender queries do not use a server `FROM`
prefilter, whose weaker Unicode matching could omit valid matches. This reads
the sender headers of every candidate left by the other filters, within the
existing preview deadline. Matching a sender is separate from authorizing changes:
ambiguous sender headers remain visible but cannot receive
a destructive-action token. Subject selectors always send the complete term in
`UID SEARCH SUBJECT`, including one-character terms and subject rules. ASCII
terms are safely quoted; non-ASCII terms use `CHARSET UTF-8` with an IMAP literal.
If a term cannot be sent safely, the preview fails closed instead of falling
back to a full source-folder fetch. The client still applies its Unicode
casefold substring check to every returned candidate. For server-filtered subjects
and recipients, weaker Unicode matching can omit a client match (for example, `STRASSE` versus `Straße`,
ligatures or Greek sigma variants). This can yield fewer proposed actions,
never additional actions; preview the returned selection before approving it.
Recipient searches use a word fragment plus
escaped or encoded header candidates to preserve matching across quotes, comments and spacing.
Candidate metadata is fetched in batches of at most
200 UIDs, including `BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SENDER REPLY-TO)]`,
`ENVELOPE`, size, internal date and flags when needed. No message body is requested
and previews never set `\Seen`. Identity and membership checks never issue
`SEARCH HEADER Message-ID`; the rules-preview raw fallback also avoids All Mail
`UID SEARCH ALL` by checkpointing UIDNEXT-derived ranges of at most 200 UIDs.
Certified-index UID-set checks are unchanged. Every selected UID must
have exactly one raw Message-ID field,
strictly recoverable as UTF-8, whose canonical identity equals the requested
identity and the canonical envelope identity. Missing, mismatched, repeated or
unparseable evidence refuses authorization. The shared check applies to ordinary
cleanup and rules, both selectors, confirmation, every mutation recheck and
logged-handle undo. Supported non-ASCII identifiers continue to work.

Identity discovery uses a certified persistent occurrence index where the
server supports the contract below. Otherwise ordinary cleanup inventories
complete envelopes and rules audit complete raw identity fields in bounded
batches, retaining completed All Mail preview ranges across calls. Checks cover
every relevant folder, including All Mail, Starred and
labels, without source date limits: old protected or ambiguous copies still
block. If complete evidence cannot be collected within budget, no token is
issued. Confirmation fetches approved source UIDs directly; it never reruns
the source selector or admits new arrivals.

**The identity-index speedup does not apply to Proton Mail Bridge.** Public
Bridge release `v3.27.1` pins `ProtonMail/gluon` commit `7e800978ab4a`, whose
capability list omits CONDSTORE and whose command parsers do not support
MODSEQ or HIGHESTMODSEQ. This was checked in `imap/capabilities.go`,
`internal/session/session.go`, `imap/command/fetch.go` and
`imap/command/status.go`. The same limitation is present in gluon development
commit `2561acfbad82`. Bridge therefore uses the raw-audit fallback described
above, including apply's reuse of a kept audit; the certified warm measurements
below are synthetic results for a server implementing the required extension,
not Bridge timings.

The index is a discovery aid. It stores only folder-local UIDs, envelope
identities, every raw Message-ID occurrence (including duplicate fields), and
identity-validity markers. It cannot authorize sender protection, fingerprints,
read state or a mutation; those still require fresh candidate FETCHes. Files
are account/endpoint/folder scoped using hashed names, owned by the current
user, private, checksummed and replaced atomically. No bodies, sender/subject
metadata, credentials or tokens are stored. Unsupported servers do not open
the index directory.

Certification requires persistent CONDSTORE support, UIDVALIDITY, UIDNEXT,
HIGHESTMODSEQ, and an exact current UID set consistent with the message count.
Stable STATUS/UID-set snapshots bracket a refresh. New UIDs at or above the
saved UIDNEXT and changed UIDs reported by CHANGEDSINCE are fetched; expunged
UIDs are removed by set reconciliation. Unchanged MODSEQ alone is insufficient.
The protocol contract requires immutable message contents within a UID and
UIDVALIDITY, and honest persistent modification sequences. A server that
silently changes immutable content or lies about changes cannot certify reuse.
Every resolution recertifies the index; confirmation and mutations clear the
connection-local inventory just as they do for full scans.

Missing, corrupt or obsolete inventories require a full raw rebuild within
the deadline. Unsupported/malformed change tracking, concurrent drift, unsafe
storage, locking failure or unreconciled deltas discard the index path and
require the existing complete discovery scan or refusal. UIDVALIDITY changes,
regressing counters and new UIDs below the previous UIDNEXT also require a
rebuild. There is no time-based stale-cache grace period. Warm UID reconciliation
still enumerates all mailbox UIDs, but expensive header FETCH work depends on
candidates plus newly arrived or changed messages.

The raw-audit fallback accepts a narrower contract than the certified index:
when SELECT supplies UIDNEXT, `apply_rules` reuses the kept All Mail audit after
fresh allocation checks and reads known matching occurrences' headers and policy
evidence again. This does not certify unchanged content for every saved UID.
An unrelated row can gain a second raw Message-ID matching an approved message
while its UID, UIDVALIDITY, EXISTS and UIDNEXT remain unchanged. Apply does not
re-read that unrelated row, misses the hidden duplicate and would move the
approved message; a full raw reread would refuse. Proton Bridge/Gluon assigns a
new UID on edit, so this is unlikely in practice. This same-UID identity-drift
limitation is an accepted trade-off of kept-audit reuse. It does not relax the
certified index's change-tracking requirements or the fresh reads of known
matching rows.

Missing, malformed, non-ASCII or otherwise unsupported envelope mailbox addresses
are unknown and protected; valid UTF-8 display names are supported. A missing or
unusable envelope Message-ID blocks a selected source candidate; unrelated malformed
identities in membership, remove-label preflight and undo lookup scans are ignored
unless their raw fields contain a requested identity. An identity containing more than one requested
ID is ambiguous and blocks approval. There is no body-fetch fallback.
Identity refusals and timeouts identify
the stage and a safely quoted folder name. Folder and label names are user data;
the reason omits message contents.

A preview issues a token only when its complete resolution uses at most 80% of
its preview budget and the observed per-row verification estimate leaves room
for one mutation, the journal reserve and the existing response reserve in the
execute budget. `estimated_calls` reports an estimate for the full approved set;
it is not a latency guarantee. Confirmation verifies the next bounded subset,
then checks its actual headroom before claiming the token. It uses one deadline
bounded by both configured call and execution timeouts and the 48-second
response ceiling, including setup and verification. `estimated_calls` uses this
same capped budget and unchanged reserves, conservatively planning one full
chunk per call. Chunks contain at most 200 rows for moves and read-state changes
and 50 rows for label actions, shrinking to fit the measured verification
allowance and the remaining mutation budget. Uncertified paths also reserve measured full inventory time
before another chunk. Certified moves with destination UIDNEXT
use observed bound-UID verification cost without charging the previous whole
chunk as a fixed inventory floor. Before freezing a certified chunk, execution
also reserves its final pre-claim identity check; observed mutation costs can
increase the existing two-second planning floor without raising any timeout.

A certified continuation resumes stored untouched handles without rerunning the
selector or a full source inventory. Unchanged UIDVALIDITY/UIDNEXT certificates
use exact bound UID reads. Small source append ranges are checked against every
untouched approved identity, bounded by the chunk size. Changed validity, a large
append range or ambiguous identity refuses continued certification and requires
review; no mailbox-wide fallback spends the continuation budget.

The mutation allowance retains a two-second floor. The repository's synthetic
Bridge work model prices a slow one-second MOVE plus two final metadata/raw
FETCHes and SELECT at 1.0076 seconds. The floor leaves margin for mutation and
final checks; complete membership scans are budgeted separately using observed
verification time. Remove-label retains its additional 0.05 seconds per row for
All Mail recovery preflight. Batched moves and read-state changes apply the
two-second floor per command, covering its first UID, plus 0.05 seconds per
further UID. One second is reserved for journaling in addition
to the connection/response reserve. The estimates are **not live measurements or
upper bounds**; the socket watchdog and per-message intent journal remain active.
Headroom is checked after preflight and again immediately before mutation.
Execution uses the remaining safe budget rather than stopping at 80% of the
configured timeout, and admits a smaller trailing chunk when it fits. The 80%
gate remains a preview requirement. Execution deadlines, response and journal
reserves, and the final check of the frozen chunk remain unchanged. Only a
[bounded approval](#bounded-approvals) raises the per-approval cap, up to
`PROTON_MCP_APPROVAL_MAX_MESSAGES`; each chunk still holds at most 200 messages.

A call returns partial progress at a safe chunk boundary when the next work no
longer fits. `resume: true` carries a new capability for the same approval's
untouched eligible rows. The first successful claim establishes its fixed
30-minute resume deadline; later continuations and retries never extend it.
For `move_to_folder` approvals, the original token and previously issued continuations
also resolve to that exact remainder after durable partial progress. All aliases
are claimed together, so an overlapping retry is refused and no completed or
uncertain row is authorized again. Other actions retain single-use rotation.
An initial headroom refusal leaves that supplied token valid until
expiry. If only the response ceiling interrupts read-only verification before
the first chunk, a durable partial run can return zero done with the exact
untouched continuation; it does not claim any message was changed. A continuation verifies
its exact remaining handles and reports changed rows as blocked. The rules path
also retains its whole reviewed-set preflight, including protected skips, before
any group mutates; a large rules verification may still refuse without progress.
Set the client's timeout above the configured server budget as described in
Configuration; the server cannot infer an arbitrary shorter client deadline.

For a single query source folder, identity is `(folder, UIDVALIDITY, UID)`.
The token binds the exact UID set and each UID's Message-ID, normalized Date and
RFC822.SIZE, with sender and membership protection checks retained. Confirmation
re-selects the folder and re-fetches those UIDs; a changed UIDVALIDITY, missing UID
or changed fingerprint refuses mutation. Repeated Message-IDs within that folder
are supported, including across execution chunks. Cross-folder ambiguity still
requires narrowing to one source folder. Labels whose membership cannot be tied
to one duplicate remain ambiguous and are refused.

Tokens come from Python's `secrets` module. The server keeps their bindings in
memory and compares token material with `hmac.compare_digest`. The binding covers
the action, exact parameters, selector, RFC Message-ID, source folder, UIDVALIDITY,
UID, discovered memberships, and personal-provider allowlist configuration and
bypasses. Before execution, the bound selection, cap and protection policy are
checked again. Disappearance, UIDVALIDITY change or changed identity requires a
fresh preview. Unbound arrivals stay untouched. Unstarted previews expire after
ten minutes by default, never more than thirty minutes. The first successful
claim for cleanup or `apply_rules` establishes a fixed 30-minute resume deadline,
reported as `expires_at` in execution responses and live continuation metadata.
Confirmation claims a token only after identity checks, headroom checks and
preflight succeed, immediately before mutations.
A timeout during those read-only checks leaves the unexpired approval available
for retry. During execution, every token for the same move approval is unavailable.
Only durable partial progress can reopen its exact untouched remainder for a
later call. Completion, the resume deadline, or an unprovable journal outcome
cannot reopen it.
The retry must use the same tool and selector, and any supplied IDs must
match the currently approved remainder. Other cleanup actions retain single-use
tokens and rotating continuations. These capabilities never widen the approved
set, change its account or target, or renew the resume deadline. If no continuation
is available, inspect the logged outcomes and obtain a fresh preview for untouched
messages. Issuing, inspecting or claiming tokens, and finishing cleanup/undo, prune
expired grants. The store retains at most 1,000 pending token digests and a bounded
set of retired digests and expiry times for errors; retired entries cannot
authorize changes.
Restarting the process invalidates all pending tokens. Only the SHA-256 digest
of a confirmation token is written to new action records, for read-only recovery
lookup; plaintext tokens are never logged. Lookup does not authorize execution or
restore a consumed token.

### Batched moves and read-state changes

`move` and `set_read_state` send
one IMAP command per source folder and chunk instead of one per message: a
`UID MOVE` or `UID STORE +FLAGS.SILENT (\Seen)` (`-FLAGS.SILENT` to mark unread)
with a compressed UID set such as `7:9,12`, at most 200 UIDs. A folder `move` with
`mark_read` sends the STORE, verifies it, then sends the MOVE. Copies of one
message identity in the same folder go in successive commands so each destination
copy stays attributable. Label actions keep their per-message commands.

The per-message safety steps are unchanged: a final writable per-UID check of
identity, fingerprint, protection and read state immediately before the command,
a durable per-message `uncertain` intent before it, and an outcome for every UID
after it. A row that changed before the command stays untouched; it is reported
in `mismatched` and becomes `blocked` for the continuation. After STORE, every UID's
`\Seen` flag is read back. After MOVE, one read of the source folder shows which
UIDs left it: UIDs are never reused within one UIDVALIDITY, so a UID still present
there did not move, even if `COPYUID` names it. Leaving the source does not prove
the move: another client may have moved the message elsewhere just before, and the
server then skips it and leaves it out of `COPYUID`. A UID therefore counts as done
only once its new copy in the destination is confirmed. A `COPYUID` response maps
named source UIDs to destination UIDs (positionally, as RFC 4315 specifies); each
named destination UID must be new and hold the same message, and an incomplete or
inconsistent mapping is never trusted. Any other UID needs exactly one new copy in
the destination with its Message-ID, fingerprint, header digest and internal date,
found at or above the destination's UIDNEXT from before the move or, without one, by
a Message-ID lookup. A UID that stayed in place or has no confirmed destination
copy, which includes every UID whose source or destination read failed, is reported
in `mismatched` (with `uncertain` status when it was attempted) and never counted as
done. A command that the server refuses, or whose reply is lost, leaves every
message it named `uncertain`, because the server may have applied part of it: that
is up to 200 messages where a per-message command left one. Inspect them with
`list_actions` before another action.

Each command is budgeted at two seconds, covering its first UID, plus 0.05 seconds
for each further UID: 200 messages from one folder reserve about 12 seconds of
mutation allowance instead of 400. A slow server therefore no longer splits a
modest batch across calls with "Insufficient execute headroom for verification".

### Bounded approvals

Without `limit`, or with `limit` up to 200, a preview approves at most 200
messages, exactly as before. A `limit` above 200 requests one bounded approval of
up to `limit` clean messages, which must not exceed the operator's hard bound
`PROTON_MCP_APPROVAL_MAX_MESSAGES` (default 500, at most 1000). Every cleanup
preview except unsubscribe and draft deletion reports that bound as `approval_max_messages`; a
bounded token binds it together with the full clean set, and the confirmation
form shows it. Like any `limit`, it selects the first `limit` clean messages in
folder and UID order; a `limit` above the bound is refused.

Confirmation still runs in chunks of at most 200 messages. When a call's budget
ends, it returns `resume: true` and a continuation token that names exactly the
untouched, unblocked remainder of that approval; it never adds rows, even if more
mail now matches, and keeps the fixed 30-minute resume deadline established by
the first successful claim. A continuation needs no new preview or approval.
Continuations are idempotent: done rows are never repeated,
and after a lost response to a folder `move` the original token or latest
continuation resumes the same remainder. If the configured bound is lowered below
an issued bounded approval, confirmation refuses it before any change; request a
new preview. Earlier releases refuse an action log that holds a bounded run
("Cleanup run journal has invalid approval"); do not downgrade while it does.

### Creating the destination in the same approval

To move mail into a `Folders/` mailbox that does not exist yet, preview with
`params: {"folder": "Folders/Receipts", "create_folder_if_missing": true}`. The
preview creates nothing. When the folder is missing, it reports
`folder_creation: {"name": "Folders/Receipts", "exists": false, "create": true}`,
and the one token binds both the folder creation and the exact move set; the
confirmation form says the folder will be created. Confirmation verifies the
approved moves, journals the creation, creates the folder once, then moves.
Tampered, expired or declined tokens create nothing, and a move approval without
creation never creates a folder that disappeared after its preview. When the
folder already exists, the preview reports `create: false` and the move proceeds as
an ordinary move. A failed creation consumes the token and moves nothing; the
creation's log record can be inspected with `list_actions`. Undo restores moved
messages but does not delete the created folder.

The `move` response reports `folder_creation` with `created`, `exists`
(the folder was already there), `ready` (it is listed for the moves) and
`uncertain`: CREATE was sent without a confirmed reply, which includes a refusal,
so check the folder list before a new preview. A call that stops before creating
reports `created: false, exists: false`. Once the folder is created the approval is
consumed: if a check then refuses the first chunk, the response still reports the
creation and returns a continuation for the approved remainder. When a call has
room for the creation but not also for one move, it creates the folder alone and
returns that continuation. The audit record notes `folder_created`, and
`folder_creation_uncertain` for an unconfirmed creation; `list_actions` shows both
in `audit`. The `create_folder` record names the approval's run, so `list_actions`
lists it with that run, also with `action: "move_to_folder"` or a used
continuation token, but never counts it as one of the run's chunks.

### Protected senders

Protected domains are an optional operator block list set via
`PROTON_MCP_PROTECTED_CONFIG`, off by default. The shipped
[`protected_default.json`](src/proton_mail_mcp/protected_default.json) lists nothing, so
with the variable unset no address, domain or display name is protected. The list
applies to the senders of mail that cleanup, unsubscribe, draft deletion and rules would
act on. It never applies to a draft's recipients: `create_draft`, `preview_send` and
`send_draft` do not read it.

A configured list checks every visible address in the raw From, Sender and Reply-To
headers, including subdomains and display names. With or without a list, a missing From,
unusable sender address, or NIL Sender/Reply-To fails closed. Gluon copies From into Sender and Reply-To
when their headers are absent; NIL with a usable From therefore means a present
but empty, unparsable or undecodable header, and blocks cleanup. Destructive actions
and rules require one From mailbox and at most one Sender mailbox. Multiple From
addresses, duplicate From/Sender/Reply-To fields, groups and display names containing
a different mailbox address are refused. Valid UTF-8 and RFC 2047 display names
use the same parsing and matching as read-only search, and remain subject to the
protected-sender policy.

ENVELOPE exposes only the **first** From, Sender, Reply-To and Message-ID header.
Cleanup and rules also fetch the raw identity and sender fields at preview,
confirmation and mutation rechecks, so hidden duplicate fields cannot authorize
changes. Message-ID must agree canonically between the raw field and envelope.
Without a list, these header checks and each action's preview, token, confirmation and
journal still apply, but mail from a bank, an authority or a friend can be approved like
any other, and the [optional timer](#manual-connector-folder-rules) moves whatever a rule matches.
To keep such senders out, list them. Generic `domain_patterns` such as `*bank*` and
`*.gov`, display-name `keywords` such as `insurance`, and `personal_domains` such as
`gmail.com` cover senders you have not named, and deliberately favor false positives.
A sender that matches nothing is not protected. This is a conservative header policy,
not sender authentication or an exhaustive registry of institutions.

Keep the list in a local file:

```sh
cp examples/protected-senders.example.json protected-senders.local.json
chmod 600 protected-senders.local.json
# Edit this local file with your own protected contacts.
export PROTON_MCP_PROTECTED_CONFIG="$PWD/protected-senders.local.json"
```

A server started from this shell reads the export. An installed service does not: put
`PROTON_MCP_PROTECTED_CONFIG=<absolute path>` in its
[settings file](docs/install.md#what-goes-where), with no quotes, or under
`EnvironmentVariables` in a hand-written plist, and in `rules.env` if the timer is on.
Then restart it.

The local filename above is git-ignored. Do not put real contacts or institutions
in the example or committed defaults.
JSON fields are `mode` (`extend`, the default, or `replace`),
`addresses` (exact addresses), `domains` (domains and all their subdomains),
`domain_patterns` (shell-style globs against normalized domains), `keywords`
(case-insensitive display-name substrings), `personal_domains`,
`personal_provider_allowlist` (exact addresses), and
`protect_personal_domains` (boolean, default true). All rule collections are arrays
of strings. The committed lists are empty, so `extend` and `replace` give the same rules;
both stay accepted. Setting
`protect_personal_domains` to false disables only the consumer-domain category.
An unreadable or invalid policy refuses cleanup, and never turns the list off. Restart after changing the policy
so pending previews cannot carry an earlier policy into a new configuration.

### Approved personal-provider sender allowlist

`personal_provider_allowlist` permits cleanup of an approved sender hosted on a
personal mail provider. The committed list is empty. The allowlist only matters
alongside `personal_domains`: it exempts a sender on a listed personal domain, and
has no effect without one. **Adding entries requires
the owner's OK.** After approval, add full addresses to the optional local JSON
policy, for example:

```json
{
  "mode": "extend",
  "personal_domains": ["personal-provider.example"],
  "personal_provider_allowlist": ["newsletter@personal-provider.example"]
}
```

Entries match the complete address after case folding and IDNA domain
normalization, using the same normalization as sender checks. Exact protected
addresses and domain rules also normalize IDNA domains, so an equivalent Unicode
protected entry still takes precedence. Bare domains,
wildcards, patterns and invalid addresses are rejected when loading configuration.
Unknown fields also fail closed, and errors reveal neither policy contents nor
the configured path. `extend` combines this list with the committed allowlist;
`replace` replaces all rule collections, so omitting the allowlist in replace mode
leaves it empty. `protect_personal_domains: false` still disables only the
personal-provider check and does not produce allowlist bypasses.

An entry suppresses **only** the personal-provider reason for that exact address.
Every address in the visible From, Sender and Reply-To fields is checked
independently: an allowlisted From with a non-allowlisted personal Sender or
Reply-To still blocks the entire batch. An allowlisted address still blocks when
it matches protected `addresses`, `domains`, financial/insurance/government
`domain_patterns`, or display-name `keywords`. Missing, unparseable, ambiguous,
NIL or otherwise unsupported sender headers remain protected. The preview and
bound approval token, the default 200-message cap (a
[bounded approval](#bounded-approvals) is limited by its configured bound instead),
fresh identity and membership checks, execution headroom guard, All Mail requirement
for label removal, and existing mutation restrictions all still apply.

Preview samples that use an exception include `personal_provider_bypasses`, and
the top-level `personal_provider_bypass_count` counts messages with a
personal-provider reason suppressed, even if another rule still blocks them. Each
bypass identifies its header and the SHA-256 digest of the normalized full
allowlist entry. Move, archive, draft deletion and unsubscribe previews log their
outcome and counts without message content. Execute and undo record
the bypasses used for each attempted message under `personal_provider_bypasses`
as `[{"header": "From", "entry_sha256": "<64 hexadecimal characters>"}]`, or
`[]` when no exception was used. The same digest identifies the same approved
entry across headers without adding plaintext senders or subjects to the log.
Interrupted or failed commands retain these details for attempted messages in
the partial record, through the existing locked append-only log path. Older
records without the field remain readable.

The allowlist configuration and per-message bypasses participate in the preview
fingerprint. Changing the loaded allowlist invalidates earlier approvals, even
when the selected messages still pass protection. Policy files are loaded on the
first cleanup call and kept for the process lifetime; restart after editing them,
which also discards pending tokens.

### Bridge mutation rules

Folder actions require the server's MOVE capability and use UID MOVE, one command
per source folder and chunk of at most 200 UIDs (see
[Batched moves and read-state changes](#batched-moves-and-read-state-changes)).
There is no COPY/flag/expunge fallback for ordinary folders; unsupported MOVE
refuses the action. Trash and Archive are existing dedicated destinations. General
folder moves accept only mailboxes under `Folders/` that exist, or that their
approval creates; they cannot target Trash, Spam, Sent, Drafts or All Mail. Only
`create_folder` and a `move_to_folder` approval with `create_folder_if_missing`
create a `Folders/` mailbox; no tool deletes or renames a mailbox. Move sources
and folder undo destinations are restricted to `INBOX`, `Archive`, `Sent`,
`Drafts`, `Spam`, `Trash`, and valid existing `Folders/<name>` mailboxes.
`Starred`, `Scheduled`, label views, and any mailbox advertised with `\Flagged`,
`\All`, or `\Noselect` are refused.

Labels are existing mailboxes under `Labels/`. Adding a label uses UID COPY.
Removing a label requires UIDPLUS and first verifies the message is present in
All Mail. The mutation code selects only the matching `Labels/<name>` mailbox,
marks exactly the authorized UID with `+FLAGS.SILENT (\Deleted)`, and issues
UID EXPUNGE for that same UID. Under Bridge's label semantics, this removes the
label membership while the message remains in All Mail. This narrow exception
never issues ordinary EXPUNGE, never selects Trash for expunge, and cannot expunge
unrelated UIDs. Without UIDPLUS it refuses. The tests assert these command bounds.

### Action log, undo and interrupted calls

Cleanup uses an append-only action journal with mode `0600`, opened with
`O_APPEND`; it never truncates or rewrites previous records. Each action records
its log ID, timestamp, action, parameters and per-message RFC Message-ID, source,
original memberships, destination and before/after UIDs (COPYUID when available),
plus a fingerprint and digest of identifying headers, message size and internal
date for recovery when COPYUID is unavailable. Seen changes also record the
prior per-message Seen state before STORE.
Treat this log as private mailbox metadata. It contains no bodies or confirmation
tokens. Keep it available if you want to undo actions. The same journal holds the
send records and the [audit trail](#audit-trail) of every write tool call.

The default is `$XDG_STATE_HOME/proton-mail-mcp/actions.jsonl`, or
`~/.local/state/proton-mail-mcp/actions.jsonl` when `XDG_STATE_HOME` is unset.
Set `PROTON_MCP_ACTION_LOG` to choose another path. Symlinks, hard-linked files and
files owned by another user are refused. An exclusive nonblocking file lock
serializes changes and undo across processes; contention refuses the operation.

Each chunk starts with an `in_progress` action record appended and fsynced
before its first mutation. Under the same lock, an intent marks each message
`uncertain` before its command, an outcome records the result, and a final record
closes the chunk. Earlier lines are never rewritten. A crash between MOVE and its
outcome therefore preserves the original undo ID and the attempted identity.

Records are JSON lines. An incomplete final line leaves earlier records readable;
lookup leaves the fragment untouched, and the next writer appends a NUL, its
SHA-256 checksum and a newline before writing another record. Repaired journals
are therefore not ordinary JSONL: use `list_actions` or the `ActionLog` journal
reader. Other malformed complete lines or invalid repair seals refuse recovery.

Each approval also has a durable `run_start` before its first mutation, followed
by `run_progress` after each chunk and `run_end` when the call returns. Resuming
appends `run_resume`. These records contain a random run ID, action, token digest
(never the token), approved total, chunk IDs and done/uncertain/remaining counts.
Each chunk carries `run_id`, `chunk_index` and `approved_total`. Recovery derives
counts from per-message chunk evidence, so a crash after a chunk outcome but
before a checkpoint still has correct counts. A start/resume without an end reads
as `interrupted`, even when the last chunk completed. A run is `completed` only
when all approved rows are done and no chunk reports a partial outcome.

The response's `log_ids` lists this call's chunk records; undo each ID separately.
It is always a list, empty when no chunk was recorded, with no singular alias.
`count`/`done` and `uncertain` describe this call;
`run_done`/`run_uncertain`, `approved_total`
and `remaining` describe the entire approval. `remaining` includes blocked rows
and means unchanged by this run. `run_done + run_uncertain + remaining` equals
`approved_total`. `run_id` stays the same across continuations. `partial` is the
run outcome, and `token_consumed` describes the supplied capability. `resume`
and `continuation_token` authorize another call only for the remaining eligible
rows; `expires_at` changes from the preview expiry to the fixed 30-minute resume
deadline on the first successful claim, then stays unchanged across continuations.
`available` is true exactly when a token is returned. A read-only attempt that
stops before claiming returns its still-valid
input token. If no first chunk fits, `error_code=first_chunk_unavailable` makes
the refusal explicit. A nonterminal budget stop retains a usable continuation;
hard failures or expiry report `available=false` and `continuation_reason`.
`blocked_ids` and `missing_ids` identify unchanged problem rows; `verbose=true`
adds their full details. `stop_reason` gives the interruption reason, or `null`
on completion.

`list_actions(token=...)` finds the whole linked run and its chunks from the
original token or a continuation that wrote a chunk, including after restart.
A continuation that only blocks changed rows returns the run ID; use the original
token or time bounds for its lookup. Time
bounds remain timezone-aware ISO timestamps (`since` inclusive, `before`
exclusive). `actions`/`truncated` retain their existing fields and chunk recovery
IDs. A completed chunk in a partial run is shown as partial. `runs`/`runs_truncated` expose the
run's status, approved total, done/uncertain/remaining/blocked counts and chunk
IDs through `log_ids`, linking each chunk to its approval-wide progress. A run
also accompanies matching chunks when its start falls outside the time window.
Each run's `continuation` reports live `available`, `expires_at` and `remaining`
metadata. The secret appears only in the execution response; lookup cannot
recover it from its stored digest. After a lost response to a folder `move`, inspect
the run and retry the original preview token or latest received continuation
while `available` is true. A retry during an active operation is refused cleanly.
The same run continues with exactly its untouched eligible rows; new arrivals
are excluded. After the resume deadline or process restart, obtain a fresh preview
and approval of the current remainder. That is a new approval and cannot extend or
widen the old capability.
Each list is independently capped by `limit` (default 100, maximum 200).
Legacy unlinked records retain their existing status and fields. No message
identities, parameters or token digests are added to lookup output.

Lookup opens the journal read-only and does not create/chmod it or change mail
or tokens. A busy log refuses promptly; retry after the operation finishes.
See [Recovery](docs/RECOVERY.md) for lost responses and interrupted runs.

A failed initial append prevents mutations. A later disk write/fsync failure stops
execution; the durable intent may leave the last message uncertain. No journal can
make an IMAP command and a local disk append atomic. Keep the log on reliable local
storage and inspect unresolved outcomes before attempting further cleanup.

`undo_action(log_id)` verifies the recorded destination UID and UIDVALIDITY,
including the All Mail recovery identity for removed labels, and checks the
Message-ID and the recorded fingerprint before reversing a folder move, Seen
change or label change. A changed mailbox identity, absent UID, different
fingerprint, or failed fetch is reported as
missing with `partial=true`; a logged UID never falls back to a Message-ID search.

Each moved message is pinned to its new destination UID by a confirmed COPYUID
mapping or, without one, a unique new fingerprint match; a message with neither
stays `uncertain`. For duplicate moves, existing destination handles are captured
before mutation and are excluded. An ambiguous or interrupted mapping is refused
for manual inspection. Undo never substitutes another same-Message-ID copy.

For older moves and uncertain outcomes without an established destination
UID/UIDVALIDITY, folder-MOVE undo searches all
exposed folders, without MOVE-source allowlist exclusions, for the logged
Message-ID, header digest, size and internal date. It restores only when exactly
one physical copy matches and that copy is in the recorded Trash destination.
Labels and All Mail are virtual views, not extra copies. Zero or multiple matches,
a match outside Trash, or any ambiguity refuses the entry with a clear reason and
leaves it unresolved for manual handling. Recorded destination handles retain
their strict checks.

Older log formats remain readable, but legacy records
without size/date require a recorded destination UID and UIDVALIDITY. A legacy
digest-based match can also be refused if its original header formatting differs
from the envelope representation, or if an older envelope digest used different
escape decoding. Legacy records with logged UIDs retain their strict identity
checks. Undo never fetches raw headers to recover a digest difference. Records
without either identity or digest fail closed. Undo writes a new log entry.
Folder undo also restores missing original labels; it does not roll back unrelated
edits made later by another client. Undo also enforces
the 200-message cap and current sender protection. Each logged message is restored
at most once; missing messages are reported explicitly. An undo does not provide a
permanent delete path.

Before an undo starts changing mail, an auxiliary `undo_claim` record durably
reserves the original log ID. The final `undo_action` outcome is another appended
line. An outstanding claim blocks another undo, including after a process
interruption without a durable outcome. A completed partial attempt keeps the
original log ID retryable for messages recorded as `not_attempted`; a retry skips
messages already restored and never widens the original set. Missing messages and
uncertain outcomes remain held for inspection and are never retried automatically.
Partial undo records from older versions lack the required retry bindings and
also remain held for inspection.
Undo restores a chunk one message at a time, and a batched chunk can hold 200
messages. An attempt keeps at least half of its call budget for restores: once its
per-message lookups have used the other half, it claims and restores only the
messages it already examined, and the rest stay retryable without being claimed.
Restores also stop before one that could not finish, judged by the slowest
restore so far in that call (at least two seconds). Such an attempt returns
`partial: true` and "Undo time budget reached; call undo_action again to restore the
remaining messages", plus `deferred` (messages it did not examine) when lookups were
cut short. Repeat `undo_action` with the same log ID until it is no longer partial.
Read-state restoration stays one exact set: when an attempt finds an identity drift,
it refuses every remaining restore of that chunk, including messages an earlier
attempt left unexamined; messages already restored stay restored.
Each attempt records its own restored (`done`), uncertain and not-attempted
outcomes; its count includes only that attempt's confirmed restorations. Only a
fully completed undo consumes the original log ID.

Cleanup execution and undo use the smaller call/execution deadline and socket watchdog. If
mutation is interrupted, its response and log describe a partial result, distinguishing
confirmed changes from commands whose server outcome is uncertain. A server may
apply a command before the connection fails to deliver its response. Inspect the
log and mailbox state before another preview; never blindly repeat a token or an
uncertain action. IMAP does not provide a transaction spanning the full batch.
Direct cleanup revalidates each chunk before changes and checks each UID again
immediately before its command. Manual rules retain their whole reviewed-set
preflight. Another mail client can still change state
between commands; a detected race stops the batch and reports partial outcomes.
Per-message statuses are `done`, `uncertain`, `flagged` (a label flag was set but
removal did not finish), `moved` (folder restoration succeeded but restoring its
labels did not finish), and `not_attempted`. A process crash or disk failure can
interrupt outcome logging; the log is not an atomic transaction with the IMAP
server. Keep the log on reliable local storage and inspect state after such a failure.

### Audit trail

The same journal records every write tool call; there is no second log. Each MCP
call to `create_draft`, `send_draft`, `delete_draft`, `move`, `set_read_state`, `label`,
`apply_rules`, `unsubscribe`, `create_folder`, `create_sieve_filter`, `add_rule`,
`reindex` or
`undo_action` appends an `audit` record with `outcome: "started"` before the tool runs, then one outcome record
with the same `audit_id`. The record's `tool` names the exact action a merged tool's
argument selected: `move_to_trash`, `archive` or `move_to_folder` for `move`;
`mark_read` or `mark_unread` for `set_read_state`; `add_label` or `remove_label` for
`label`; `reindex_preview`, `reindex_execute` or `reindex_cancel` for `reindex`. A call
whose arguments select no action keeps the tool's own name and the outcome
`invalid_arguments`. `list_actions(action="move")` finds all of a merged tool's records,
and records from before the merge read the same. An outcome record looks like this
(values shortened):

```json
{"action":"audit","audit_id":"5d0c…","timestamp":"2026-10-25T01:12:19.329028+00:00",
 "client":{"name":"example-host","version":"1.0","transport":"http"},
 "tool":"archive","outcome":"completed","token_id":"3f6c0a9e41b27d58",
 "action_hash":"9b1e…","counts":{"approved":12,"done":12,"uncertain":0,"remaining":0},
 "log_ids":["a41f…"],"run_id":"c07d…","token_consumed":true}
```

`outcome` is `completed`, `partial`, `refused` (with the server's own `reason`),
`failed` (backend error, without details), `invalid_arguments`, `cancelled`, or a
confirmation result such as `confirmation_declined`, `confirmation_unavailable` or
`confirmation_timeout`. A `started` record without an outcome means the call is
still running or was interrupted; inspect its run and chunk records. `client` is
the MCP client's self-reported `clientInfo` name and version (not authenticated)
plus the transport; stateless HTTP leaves name and version null. `token_id` is the
first 16 hex digits of the token's SHA-256, matching the `token_digest` prefix of
run and chunk records; tokens themselves are never written. `create_draft`
records only recipient domains, the recipient count, SHA-256 digests of subject
and body, and the new draft's Message-ID. Recipients are counted as the draft's
To and Cc headers are written, from the arguments as the MCP SDK decodes them
(it accepts JSON-encoded recipient lists); the outcome record describes the
arguments the tool received. A recipient list the tool refuses records a `null`
count and domains. Send records the draft Message-ID and
send result beside its existing hashed send records. `create_folder`,
`create_sieve_filter` and `add_rule` outcomes count their one object at most once: as
`done`, as `uncertain` or, for a filter or rule the backend was never asked to create,
as `remaining`; an existing folder counts in none. `create_sieve_filter` outcomes also
add the `script_sha256` of the approved script, `add_rule` outcomes the `rule_sha256`
of the approved rule. No record contains message
bodies, subjects, full addresses, unsubscribe URLs, credentials or confirmation
data. Every other top-level journal record (cleanup chunks, runs, unsubscribe,
folder, send and undo records) also carries `client`; per-message progress lines
belong to the record they extend. Rules timer records name the CLI with
`"transport": "cli"`.

If the `started` record cannot be appended (an unwritable or invalid log, or one
still busy after a brief wait), the write tool refuses before changing anything.
A failed outcome append is logged as a warning without details; the `started`
record remains as evidence.

`list_actions` adds an `audit` list (newest first, capped by `limit`) with each
call's `audit_id`, start `timestamp`, `tool`, final `outcome`, `client`,
`action_hash` and `counts`. It never shows token IDs, reasons or Message-IDs.
`action` filters by write tool (rules chunks match `apply_rules` through their run),
`client_name` by MCP client name; `token`, `since` and `before` apply as well.

### Location and rotation

The journal, including the audit trail, is `$XDG_STATE_HOME/proton-mail-mcp/actions.jsonl`
(`~/.local/state/proton-mail-mcp/actions.jsonl` without `XDG_STATE_HOME`) or
`PROTON_MCP_ACTION_LOG`, with mode `0600`. It is never rotated, truncated or
rewritten automatically; it grows by a few hundred bytes per audited call plus
the per-message cleanup evidence. It is also the undo store, the rolling send
budget and the run evidence, so rotate deliberately:

1. Stop the server and the rules timer, so nothing holds the journal lock.
2. Move the file to an archive name beside it, such as `actions-2026-10.jsonl`.
   Keep it private (`.gitignore` already ignores `actions*.jsonl`). Never
   copy-and-truncate a live journal.
3. Start the server again; the next write creates a new, empty journal.

After rotation, `undo_action` cannot reverse actions recorded only in the archive,
interrupted runs there cannot be resumed, and the rolling 24-hour send cap starts
from zero, so rotate at least 24 hours after the last send. Inspect an archive
read-only with the `ActionLog` reader shown in [Recovery](docs/RECOVERY.md),
pointed at the archived path.


## Development and offline checks

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
make check
# Or select an installed interpreter and formatter explicitly:
make check PYTHON=/path/to/python3.14 RUFF=/path/to/ruff
```

`make check` runs lint and the full suite before merging. It shards whole files
across up to twelve plain pytest processes, with no additional
dependencies. Set `JOBS=1` for serial debugging or `JOBS=4` to limit concurrency;
`PYTHON` and `RUFF` select existing tools. Each invocation saves
shard logs, JUnit reports, and per-file timings under `out/test-runs/`.
Temporary test state uses the system temporary directory; `TMPDIR` can select
another directory outside Git, as the search index refuses cache paths in a checkout.
After major suite changes, refresh balancing by copying a successful full run's
`file-times.json` to `scripts/test_file_times.json`.

Tests use fake IMAP connections
and real local Unix socket pairs speaking IMAP through the standard library parser.
Tests do not access real mail, Keychain credentials, or external network endpoints.
Coverage includes bearer authentication, TLS restrictions, safe search
criteria, thread/body extraction, read-only behavior, draft append, secret
redaction, single-use preview tokens, Sieve script escaping and parse-back
verification, cap/protection checks, append-only logging,
undo, deadline partial outcomes, command allowlists, and a package scan confining
SMTP to the guarded send module. Fake SMTP and elicitation tests cover send guards;
socket tests exercise
preview → confirmation → undo for every cleanup action. These tests do not validate
behavior against a live Bridge. Deployment and a live mailbox test require a
separate review and explicit approval.

Preview speed regressions use 45 and 200 matching messages amid thousands of
unrelated messages, with deterministic per-command latency. They check bounded
round trips, batched header-only PEEK, unchanged client matching, identity
ambiguity, and refusal when preview or execute headroom is genuinely missing.
The larger mailbox fixtures also exercise confirmation, undo and deadline drift.

The fallback path used by Bridge retains Option A, which replaces candidate-only
cost limits with bounded full-mailbox discovery. Complete envelope scans preserve
old/protected-copy and ambiguity checks; rules also need complete raw identity
audits to detect selected identities hidden in second fields. The All Mail
rules-preview raw fallback uses UIDNEXT-derived FETCH ranges of at most 200 UIDs,
without SEARCH ALL or HEADER searches, and saves progress after each range for continuation. Both
scans use bounded batches and caches, so fetched rows and commands
grow at most linearly with the mailbox views inspected. The speed tests check
two mailbox sizes and retain the previous limits wherever they still pass.

The certified columns require synthetic CONDSTORE support and do not describe
Bridge performance.

| Synthetic workload | #12 | #13 Option A | Certified warm | Certified cold rebuild |
| --- | ---: | ---: | ---: | ---: |
| 45-message move, modeled seconds | 10.006 | 42.759 | 12.406 | 46.359 |
| Subject rule, 200 matches, modeled seconds | 12.820 | 59.020 | 15.220 | 52.900 |
| 45-message move, fetched DB rows | 135 | 18,225 | 135 | 18,225 |
| Subject rule, fetched DB rows | 600 | 18,600 | 600 | 19,000 |
| 45-message move, FETCH / total commands | 3 / 46 | 23 / 66 | 3 / 70 | 23 / 102 |
| Subject rule, FETCH / total commands | 3 / 59 | 93 / 137 | 3 / 71 | 23 / 103 |

Warm tests at both 240 and 6,200 messages restore exactly three candidate
FETCHes and `3*C` fetched DB/raw rows. They allow 85 commands versus measured
70/71 (about 20% margin), less than 15 seconds for the 45-message fixture
(12.406 measured), less than 18 seconds for subject rules (15.220), and less
than 25 seconds for the ordinary 200-message fixture (20.460 maximum).
UID-set enumeration and local index processing remain mailbox-sized; these
bounds concern expensive fetched metadata and total modeled protocol cost.

Servers without certifiable change tracking retain all original Option A
assertions, including the formulas below. A certified cold build fetches every
raw identity in bounded 1,000-row batches, then freshly fetches candidates;
ordinary candidate batches and the action cap remain 200. Its fetched-row and
FETCH bounds use the ordinary Option A formula including two candidate copies,
with at most six additional certification commands per folder. That extra
work is explicit: at 240 messages, certified cold rules fetch 1,120 rows rather
than Option A's 720; at 6,200, 19,000 rather than 18,600. Certified cold subject
rules remain under 60 seconds and the 45-message fixture under 50 seconds.
Warm reuse is available only after a complete certified build succeeds.

For the fixed overlapping-folder fixtures, let `M` be the physical mailbox
size and `C` the candidate count (at most 200). Ordinary DB-row cost is bounded
by `ceil(3.5*M) + 2*C`; rules use `ceil(3.5*M)`. FETCH bounds are
`max(8, ceil(3.45*M/1000 + 4))` for ordinary cleanup and
`max(8, ceil(3.45*M/200))` for rules. Total commands are bounded by those
values plus 50 or 52 respectively. At 6,200 physical messages this allows
26/107 FETCHes and 22,100/21,700 DB rows (ordinary/rules), versus measured
23/93 FETCHes and 19,000/18,600 rows: roughly 13–17% margin. The same formulas
are asserted at 240 messages, and DB-row growth between sizes is also bounded.
The previous ordinary 90-command cap and candidate-only raw-row caps still
pass and remain in place. These formulas describe the fixture's fixed folder
layout; production scans each relevant mailbox view within the call budget.

The 50,000-message slow-peer probe returns complete ordinary-preview tokens
for 3, 45 and 200 matches in 16.027, 16.408 and 17.812 modeled seconds, with no
HEADER searches. Its cheaper envelope-row calibration differs from the
45-message and subject-rule fixtures above. The 59.020-second rule fixture
fits both the unchanged 120-second call budget and 96-second approval
headroom cutoff; larger audits still refuse if evidence cannot finish in time.
Every command remains bounded by the remaining I/O budget, with the response
reserve and close-on-timeout behavior described above.

A separate warm variant of the 50,000-message probe measures
5.727/6.108/7.512 seconds for 3/45/200 candidates, 57 commands, and
9/135/600 fetched ENVELOPE/raw rows. The original cold probe keeps its
at-least-50,000-ENVELOPE-row assertion unchanged. The warm variant prepares
folders separately with bounded 300-second calls: 321.1 modeled seconds and
100,000 raw rows in total. This preparation is not a single normal preview;
a cold full raw build of that fixture can exceed the default 120-second budget
and must refuse. There is no automatic background warmer, and these results
do not claim warm speed on servers lacking certifiable CONDSTORE evidence.

Raw Message-ID checks now require exact, unique canonical agreement on every
phase. Both formerly accepted duplicate-field audit cases refuse, alongside
missing, unrelated, unparseable and invalid UTF-8 fields.

These are synthetic measurements of client commands and fetched candidates.
They include a 50,000-message fake that charges mailbox-wide decryption cost for
`HEADER` searches and cheaper database metadata reads for envelope discovery.
They do not establish a live server's cost: complete metadata and raw-header
audits can still exceed a budget and must refuse safely.
Real mailbox latency still needs an operator test after review. Broad
`search_mail` header scans stop at the uncached candidate cap and report partial
results; explicit `since`/`before` bounds can reduce that work in large folders.

`PMM_REAL_BATCH` can optionally point to a
local JSON list of 27 Message-IDs; only the socket simulation uses those IDs, and
the test reports counts and timing without printing them. Optional comparison
and real-file cases skip when their input paths are not supplied.
