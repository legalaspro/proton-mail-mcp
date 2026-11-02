# Configuration

The server reads every setting from environment variables. Each table below lists a
setting's default and meaning. Settings are read at startup, so restart the server
after a change.

- **Running by hand:** export them before `proton-mail-mcp`.
- **Installed service:** put them in its settings file, listed under
  [What goes where](install.md#what-goes-where). Values there are literal, with no
  quotes or `$` expansion; only `PROTON_*` and `PMM_*` names are allowed, and any other
  name stops the service from starting. The file must not hold `PROTON_MCP_TOKEN` or `PROTON_BRIDGE_PASSWORD`.
- **Invalid values:** the server refuses to start, printing
  `Cannot start proton-mail-mcp:` and the reason, with exit status 2. `PROTON_BRIDGE_CERT`,
  `PROTON_MCP_ACTION_LOG`, `PROTON_MCP_SAVED_RULES`, `PROTON_MCP_IDENTITY_INDEX_DIR` and
  `PROTON_MCP_PROTECTED_CONFIG` are not checked at startup: a bad path shows on first use.
- **Switches:** a setting described as "exact `1` or `0`" accepts nothing else.

Contents:

- [Required settings and HTTP](#required-settings-and-http)
- [Bridge connection and credentials](#bridge-connection-and-credentials)
- [Call budgets and token lifetimes](#call-budgets-and-token-lifetimes)
- [Confirmation and views](#confirmation-and-views)
- [Local files](#local-files)
- [Search index and background work](#search-index-and-background-work)
- [Cleanup limits](#cleanup-limits)
- [Guarded send settings](#guarded-send-settings)
- [Other variables](#other-variables)
- [Bridge TLS](#bridge-tls)
- [Protected senders](#protected-senders)
- [Approved personal-provider sender allowlist](#approved-personal-provider-sender-allowlist)

## Required settings and HTTP

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROTON_MCP_TOKEN` | Required | Bearer token, at least 32 printable ASCII characters with no whitespace. |
| `PROTON_BRIDGE_USER` | Required | Bridge username and Keychain (or credential store) account. |
| `PROTON_MCP_HOST` | `127.0.0.1` | HTTP bind address. Only the literal `127.0.0.1` is accepted. |
| `PROTON_MCP_PORT` | `8765` | HTTP port; integer from 1 to 65535. |
| `PROTON_MCP_PUBLIC_HOST` | Unset | Exact Tailscale Serve hostname allowed by HTTP Host/Origin checks; no scheme, port, or path. Does not change the bind address. |

The installer's service launcher loads `PROTON_MCP_TOKEN` from the credential store, so
an installed service needs no token in its settings.

## Bridge connection and credentials

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROTON_BRIDGE_PASSWORD` | Unset | Password override for tests: when set, the credential store is not read. Prefer Keychain on macOS. The service launcher refuses it in the settings file. |
| `PROTON_MCP_CREDENTIAL_STORE` | `keychain` | Where the Bridge password is read when `PROTON_BRIDGE_PASSWORD` is unset: `keychain` (macOS), `libsecret` (`secret-tool`) or `pass` (Linux), or `wincred` (Windows Credential Manager, the default on Windows). Service `proton-mail-mcp`, account `PROTON_BRIDGE_USER`; see the [installer guide](install.md). |
| `PROTON_MCP_PASSWORD_STORE_DIR` | Unset | Absolute `pass` store directory, passed to `pass` as `PASSWORD_STORE_DIR`; unset uses the process environment or `~/.password-store`. The installer writes it for `pass`. |
| `PROTON_MCP_GNUPGHOME` | Unset | Absolute GnuPG home passed to `pass` as `GNUPGHOME`; unset uses the process environment or GnuPG's default. |
| `PROTON_BRIDGE_HOST` | `127.0.0.1` | Bridge IMAP hostname or address. |
| `PROTON_BRIDGE_IMAP_PORT` | `1143` | Bridge IMAP port, using STARTTLS; integer from 1 to 65535. |
| `PROTON_BRIDGE_CERT` | Unset | PEM Bridge certificate to pin. Required for send unless SMTP verification is explicitly disabled; optional for existing IMAP operations. |
| `PROTON_BRIDGE_DRAFTS_FOLDER` | `Drafts` | Exact IMAP mailbox name used for new drafts. |
| `PROTON_BRIDGE_TIMEOUT` | `30` | IMAP and SMTP socket timeout in seconds; greater than zero and at most 300. |

## Call budgets and token lifetimes

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROTON_MCP_CALL_TIMEOUT` | `120` | Mailbox call budget in seconds, including the upper limit for cleanup execution and undo; greater than zero and at most 300. Search and count cap this at 20 seconds, and thread reads at 15 seconds; those tools also cap each IMAP command at 10 seconds, except a thread read's fallback Message-ID lookup, which shares a 12-second limit. Each command's socket timeout and watchdog use the remaining budget, reserving time for connection cleanup and the response. Slowly trickling responses cannot extend a command or call deadline. |
| `PROTON_MCP_EXECUTE_TIMEOUT` | Call budget (`120`) | Additional cleanup execution and undo limit in seconds; greater than zero and at most 300. The effective budget is the smaller of this value and `PROTON_MCP_CALL_TIMEOUT`. |
| `PROTON_MCP_PREVIEW_TIMEOUT` | Call budget | Optional shorter cleanup preview budget, greater than zero and no greater than `PROTON_MCP_CALL_TIMEOUT`. Larger values are rejected at startup. `preview_rules` additionally caps each call at 20 seconds. |
| `PROTON_MCP_CLEANUP_TOKEN_TTL` | `600` | Preview lifetime in seconds; greater than zero and at most 1800 (30 minutes). Starting cleanup or `apply_rules` establishes a separate, fixed 30-minute resume deadline. |

Guarded cleanup confirmation (move, trash, archive, mark, label and manual rules)
uses `min(PROTON_MCP_CALL_TIMEOUT, PROTON_MCP_EXECUTE_TIMEOUT, 48 seconds)`.
`EXECUTE_RESPONSE_BUDGET_S = 48` includes the existing response reserve: 10% of
the effective budget, at most five seconds. The reserve formula is unchanged.
At the 48-second ceiling, mailbox I/O stops by 43.2 seconds, leaving 4.8
seconds for cleanup and serialization within the ceiling, two seconds
below the tested 50-second response bound. The one-second journal reserve and
the label actions' 2.0/2.05-second per-message mutation allowances remain
unchanged; moves and read-state changes are budgeted per command instead (see
[Batched moves and read-state changes](cleanup.md#batched-moves-and-read-state-changes)).
Choosing 48 preserves existing configured 48-second chunk capacity. Lower configured limits
still win. Keep the client's timeout above the server budget with transport margin.
Undo retains `min(PROTON_MCP_CALL_TIMEOUT, PROTON_MCP_EXECUTE_TIMEOUT)` (120
seconds by default); use at least 150 seconds for that default. Raising only
`PROTON_MCP_EXECUTE_TIMEOUT` cannot extend a call beyond `PROTON_MCP_CALL_TIMEOUT`.
A call stops with partial progress when the remaining work no longer fits. If
`resume` is true, call the same tool with the same `target`, `state` or `op`, passing
`continuation_token` as `token`, to continue the same approval. There is no background continuation. Each continuation contains only
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

## Confirmation and views

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROTON_MCP_CONFIRM_MUTATIONS` | `1` | Exact `1` or `0`. `1` requires the user's confirmation in the client's form before a cleanup, rules, rule-saving, unsubscribe, folder or Sieve filter token is redeemed; `0` is an explicit opt-out to token-only redemption for hosts without form elicitation. See [Confirmation and approval binding](safety.md#confirmation-and-approval-binding). |
| `PROTON_MCP_CONFIRM_TIMEOUT` | `120` | Seconds a confirmation form may stay unanswered; positive and at most 300. |
| `PROTON_MCP_APPS_ENABLED` | `1` | Exact `1` or `0`. `1` registers the [MCP Apps views](mcp-apps.md): `ui://` resources and their view tools. `0` restores the plain tool list. |
| `PROTON_MCP_TIMEZONE` | Unset | An IANA time zone name such as `Europe/Berlin`. Bridge writes `Date` headers in UTC; `read_thread`, `show_thread` and `search_mail` add each message's `date_local` in this zone, and `compose_reply` quotes the original's date in it. Unset means this computer's time zone, which `TZ` sets for a server started by hand. On Windows a name needs the `tzdata` package. Views show dates in the client's time zone. |

## Local files

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROTON_MCP_PROTECTED_CONFIG` | Unset | Path to a local JSON policy of [protected senders](#protected-senders), an optional operator block list, off by default. It checks cleanup senders, not a draft's recipients. Personal addresses belong in this uncommitted file. |
| `PROTON_MCP_ACTION_LOG` | User state directory | Optional absolute path for the append-only action and audit JSONL log; see [Action log, undo and interrupted calls](RECOVERY.md#action-log-undo-and-interrupted-calls). |
| `PROTON_MCP_SAVED_RULES` | `saved-rules.json` beside the action log | Optional path of the private [saved custom rules](rules.md#saved-custom-rules) store that `add_rule` appends to and the rules engine and timer read. |
| `PROTON_MCP_ATTACHMENTS_DIR` | `$XDG_DATA_HOME/proton-mail-mcp/attachments` if `XDG_DATA_HOME` is absolute, else `~/.local/share/proton-mail-mcp/attachments` | Absolute path of the one folder that [attachment tools](drafts.md#attachments) may use for local files. It must be a real directory, not a symbolic link, and not writable by everyone. The path must be normalized, with no `.` or `..` steps and no trailing slash. A folder you set by hand must exist already, and `setup` creates one it is given; the default one is created with mode `0700` on the first save, and an existing one keeps its mode. `setup` shows the path in use and can change it. |

## Search index and background work

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROTON_MCP_IDENTITY_INDEX_ENABLED` | `1` | Reuse a certified identity inventory when the server supplies complete CONDSTORE evidence; `0` retains full discovery scans. |
| `PROTON_MCP_IDENTITY_INDEX_DIR` | User state directory | Optional identity-index directory; defaults to `$XDG_STATE_HOME/proton-mail-mcp/identity-index` or `~/.local/state/proton-mail-mcp/identity-index`. |
| `PROTON_MCP_SEARCH_INDEX_CATCHUP_MAX_MESSAGES` | `2000` | Maximum missing All Mail headers to catch up during search, optional IDLE updates or background sync; also caps combined source/target metadata fetches for folder mapping. Integer from 0 to 10000; `0` disables these optional fetches and background sync. Catch-up preserves time for the requested search and reconciles expunges. |
| `PROTON_MCP_INDEX_AUTOREBUILD_ENABLED` | `1` | Exact `1` or `0`. Recover a discarded, empty or incomplete header index in the background at server startup; independent of `PROTON_MCP_BACKGROUND_SYNC_ENABLED`. Set `0` to disable [automatic recovery](search-index.md#automatic-startup-recovery). |
| `PROTON_MCP_REINDEX_BATCH_SIZE` | `100` | Header UIDs fetched per batch by automatic recovery and the manual background All Mail and real-folder search-index rebuild; integer from 1 to 200. |
| `PROTON_MCP_REINDEX_BATCH_SLEEP` | `0.25` | Seconds to pause between background rebuild batches; from 0.01 to 60. Manual rebuild batches keep the normal Bridge timeout; each automatic-recovery session is capped at 20 seconds, socket timeout included. |
| `PROTON_MCP_IMAP_IDLE_ENABLED` | `0` | Exact `1` or `0`. Enable the read-only [new-mail watcher](imap-idle.md) at server startup. |
| `PROTON_MCP_IMAP_IDLE_FOLDERS` | `["INBOX"]` | JSON array of 1–8 distinct, nonempty IMAP folder names. Commas and Unicode names are supported; control characters are refused. |
| `PROTON_MCP_IMAP_IDLE_POLL_INTERVAL` | `60` | Seconds between NOOP/inventory checks only when Bridge lacks IDLE; from 30 to 300. |
| `PROTON_MCP_BACKGROUND_SYNC_ENABLED` | `0` | Exact `1` or `0`. Enable the read-only periodic [background All Mail sync](search-index.md#background-all-mail-sync) at server startup. Off disables that periodic worker; automatic startup recovery is controlled separately. It stays inactive while `PROTON_MCP_IMAP_IDLE_ENABLED=1`. |
| `PROTON_MCP_BACKGROUND_SYNC_INTERVAL` | `900` | Seconds between its wakes, plus up to 10% jitter; from 300 to 3600. |
| `PROTON_MCP_BACKGROUND_SYNC_MAX_COMMANDS` | `1500` | Its IMAP commands per UTC day; integer from 100 to 5000. |

## Cleanup limits

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROTON_MCP_UNSUBSCRIBE_MAX_BATCH` | `50` | Unsubscribe batch limit; integer from 1 to 50. Cannot raise the hard cap. |
| `PROTON_MCP_RULES_MAX_MOVES` | `50` | Optional timer's per-run move cap; integer from 1 to 200. Does not enable the timer. |
| `PROTON_MCP_APPROVAL_MAX_MESSAGES` | `500` | Hard upper bound for one [bounded cleanup approval](cleanup.md#bounded-approvals), requested with a `preview_action` `limit` above 200; integer from 200 to 1000. Every `preview_action` preview except unsubscribe and draft deletion reports it as `approval_max_messages` and a bounded token binds it. `200` disables bounded approvals. |

## Guarded send settings

Sending is off by default. See [Guarded send](sending.md#guarded-send) for how it works.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROTON_MCP_ENABLE_SEND` | `0` | `1` registers the two send tools (plus the token-free `review_send` view tool when views are on) and enables HTTP sessions/SSE for elicitation. With `PROTON_MCP_SEND_TYPED_OK` on, the default, it also lets a client whose `initialize` omits elicitation, or that `PROTON_MCP_SEND_TYPED_OK_CLIENTS` lists, send to any recipient on a [typed OK](sending.md#typed-ok-when-the-client-has-no-form), which the server cannot verify. |
| `PROTON_MCP_SEND_TYPED_OK` | `1` | Exact `1` or `0`. `1` allows the [typed OK](sending.md#typed-ok-when-the-client-has-no-form) for a client whose `initialize` omits elicitation, or that `PROTON_MCP_SEND_TYPED_OK_CLIENTS` lists, and logs a WARNING for each `preview_send` and send on it. `0` ignores that list and refuses every send from a client whose `initialize` omits elicitation that the own-address path does not cover. A client that declares elicitation gets the form with `0`, and with `1` unless the list names it. |
| `PROTON_MCP_SEND_TYPED_OK_CLIENTS` | empty | Off by default; an operator choice for a host that declares elicitation but never shows the form. Comma list of `clientInfo.name` values, matched exactly but ignoring case and surrounding spaces; empty entries are ignored. In the env file, write names without quotes, as in `Example Client,Other`: values are read literally, so a quoted name keeps its quotes and never matches. A listed client is treated as having no form: sends take the [typed OK](sending.md#typed-ok-when-the-client-has-no-form) and mailbox changes that need a form refuse at once with `confirmation_unavailable`. `clientInfo.name` is self-reported and unauthenticated, the same trust level as the elicitation flag, so any client holding the server token can claim a listed name. `PROTON_MCP_SEND_TYPED_OK=0` overrides the list. |
| `PROTON_MCP_SEND_SELF_NO_FORM` | `0` | Exact `1` or `0`. `1` skips the form for a draft with no attachment or forward subject when every recipient is the account's own address or on `PROTON_MCP_SEND_FROM`. No other configurable recipient allowlist. Formerly `PMM_SEND_TOKEN_APPROVAL`. |
| `PROTON_MCP_SEND_FROM` | Empty | Exact ASCII addresses, the account's own and any [aliases](sending.md#sending-from-an-alias) to send from; an empty allowlist refuses all sends. With `PROTON_MCP_SEND_SELF_NO_FORM=1` they also receive mail with no form, so list only your own. |
| `PROTON_MCP_SEND_MAX_RECIPIENTS` | `10` | Total To/Cc/Bcc addresses per message; 1–50. |
| `PROTON_MCP_SEND_DAILY_CAP` | `20` | Rolling 24-hour send attempts reserved before SMTP; 1–200. |
| `PROTON_MCP_SEND_TOKEN_TTL` | `300` | Preview lifetime in seconds; positive, at most 600. |
| `PROTON_MCP_SEND_CONFIRM_TIMEOUT` | `120` | User form timeout in seconds; positive, at most 300. A timeout refuses the send with a clear error and never falls back. |
| `PROTON_BRIDGE_SMTP_HOST` | `127.0.0.1` | Only literal `127.0.0.1` or `::1`; no DNS or remote SMTP. |
| `PROTON_BRIDGE_SMTP_PORT` | `1025` | Bridge SMTP port; integer from 1 to 65535. |
| `PROTON_BRIDGE_SMTP_TLS` | `0` | STARTTLS; `1` selects implicit TLS. |
| `PROTON_BRIDGE_SMTP_ALLOW_UNVERIFIED` | `0` | `1` explicitly permits unverified loopback TLS when no pin is configured. |

## Other variables

| Variable | Used by | Meaning |
| --- | --- | --- |
| `XDG_STATE_HOME` | Server, installer | Root of the default action log and index locations; unset means `~/.local/state`. |
| `XDG_DATA_HOME` | Server, installer | Root of the default attachments folder when it is an absolute path; otherwise `~/.local/share`. |
| `PASSWORD_STORE_DIR` | `pass` store | Where `pass` keeps entries, when `PROTON_MCP_PASSWORD_STORE_DIR` is unset; unset means `~/.password-store`. |
| `GNUPGHOME` | `pass` store | GnuPG home for `pass`, when `PROTON_MCP_GNUPGHOME` is unset. |
| `PMM_INSTALLER_VENV` | Installer | Written to the settings file for a `--venv` path or one already saved, so reruns and `uninstall` find it. See [Reruns, changes and options](install.md#reruns-changes-and-options). |
| `PMM_INSTALLER_PREVIOUS_VENVS` | Installer | Written to the settings file: earlier virtual environments the installer made. See [Directories the installer made stay](install.md#directories-the-installer-made-stay). |
| `PMM_PYTHON` | `scripts/install.sh`, `scripts/install.ps1` | The Python interpreter to try first, after `-Python` in `install.ps1`. |
| `PMM_OS_RELEASE` | `scripts/install.sh` | The file read for the distribution when printing a Python install hint; default `/etc/os-release`. |
| `PMM_REAL_BATCH` | Tests | Optional local JSON list of 27 Message-IDs for one socket simulation. See [Development](development.md). |
| `PMM_SEND_TOKEN_APPROVAL` | Nothing | Former name of `PROTON_MCP_SEND_SELF_NO_FORM`. It is ignored, and startup and the installer warn while it is set. |

## Bridge TLS

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
the pin if Bridge regenerates its certificate. Protect the file and keep the
default loopback Bridge connection.

## Protected senders

Protected domains are an optional operator block list set via
`PROTON_MCP_PROTECTED_CONFIG`, off by default. The shipped
[`protected_default.json`](../src/proton_mail_mcp/protected_default.json) lists nothing, so
with the variable unset no address, domain or display name is protected. The list
applies to the senders of mail that cleanup, unsubscribe, draft deletion and rules would
act on, though [draft deletion](cleanup.md#guarded-draft-deletion) has a narrow exception for the
account's own drafts. It never applies to a draft's recipients: `create_draft`, `preview_send` and
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
any other, and the [optional timer](rules.md#manual-connector-folder-rules) moves whatever a rule matches.
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
[settings file](install.md#what-goes-where), with no quotes, or under
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

## Approved personal-provider sender allowlist

`personal_provider_allowlist` permits cleanup of an approved sender hosted on a
personal mail provider. The committed list is empty. The allowlist only matters
alongside `personal_domains`: it exempts a sender on a listed personal domain, and
has no effect without one. **Each entry lets cleanup act on that sender, so add only
senders you approve.** Add full addresses to the optional local JSON policy, for example:

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
[bounded approval](cleanup.md#bounded-approvals) is limited by its configured bound instead),
fresh identity and membership checks, execution headroom guard, All Mail requirement
for label removal, and existing mutation restrictions all still apply.

Preview samples that use an exception include `personal_provider_bypasses`, and
the top-level `personal_provider_bypass_count` counts messages with a
personal-provider reason suppressed, even if another rule still blocks them. Each
bypass identifies its header and the SHA-256 digest of the normalized full
allowlist entry. Move, archive, draft deletion and unsubscribe previews log their
outcome and counts without message content. Execute (except unsubscribe) and undo record
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
