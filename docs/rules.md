# Folder rules, Sieve filters and saved rules

**Beta: folder rules.** The rules engine is not fully reliable yet; preview first
and check the results. This applies to `preview_add_rule`, `add_rule`, `list_rules`,
`preview_rules` and `apply_rules`.

Each tool's arguments are in the [tool reference](tools.md).

| Tool | Behavior |
| --- | --- |
| [`preview_rules`](tools.md#preview_rules) | Read-only per-rule counts, samples, protected skips and `excluded_*` fields, plus a single-use token for up to `limit` reviewed matches. Calls are capped at 20 seconds; pass `next_cursor` as `cursor` to continue unfinished review or browse the next batch. `order` accepts `oldest` or `newest`. Packaged rules come first, then [saved custom rules](#saved-custom-rules). |
| [`apply_rules`](tools.md#apply_rules) | Manually applies the approved moves and read-state option, plus each saved rule's own `mark_read`. Returns `log_ids` for `undo_action`. |
| [`preview_add_rule`](tools.md#preview_add_rule) | Read-only: validates one custom folder rule with the Sieve filter model and returns the normalized rule, `rule_sha256`, the informational Sieve `script`, the `apply` status, optional `matches` (a bounded, read-only INBOX count with samples) and a single-use token. See [Saved custom rules](#saved-custom-rules). |
| [`add_rule`](tools.md#add_rule) | Appends exactly the previewed rule to the saved rules store after the user's confirmation in the client's form; never replaces, edits, reorders or removes rules. Mail moves only when rules run. |
| [`list_rules`](tools.md#list_rules) | Read-only: every folder rule in the order rules run, `built_in` (the packaged file) then `saved`, with its target. No mailbox access. |
| [`preview_create_folder`](tools.md#preview_create_folder) / [`create_folder`](tools.md#create_folder) | Preview an exact `Folders/` name, then create it after approval and the user's confirmation in the client's form. An existing folder is a successful no-op without issuing or consuming a token. |
| [`preview_sieve_filter`](tools.md#preview_sieve_filter) | Read-only: validates one Proton Sieve filter for new mail (1 to 10 `from`/`to`/`subject` conditions, an existing real `Folders/` target, optional mark read) and returns the exact script, `script_sha256`, the `apply` backend status and a single-use token bound to that script. See [Sieve filters](#sieve-filters). |
| [`create_sieve_filter`](tools.md#create_sieve_filter) | Adds exactly the previewed script as one new filter through the apply backend, after the user's confirmation in the client's form; never replaces, edits or removes filters. **It always refuses before any form, without changes**: Proton Mail Bridge offers no supported apply path. |

## Manual connector folder rules

**Beta: folder rules.** The rules engine is not fully reliable yet; preview first
and check the results.

Rules live in the reviewed repository file
[`folder_rules.json`](../src/proton_mail_mcp/folder_rules.json), initially empty.
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
`next_cursor` still returned. A new preview chain first reviews one match, then up to 4,
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
entirely of protected rows cannot authorize moves. After the user approves
the preview, call `apply_rules(token)`.
These are connector rules, not Proton filters. The existing move path handles chunking, journal-before-move,
timeouts and sender guards. Apply returns `log_ids`, empty when no chunk ran; pass each to
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
after ten idle minutes or server restart, and the server keeps four and evicts the oldest. A
reply spends its cursor. A call that
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
same-UID content-change trade-off described under [Guarded cleanup](cleanup.md#guarded-cleanup):
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
client and mailbox for ten minutes, or for 24 hours after its last write for the All Mail
audit while [background sync](search-index.md#background-all-mail-sync) is active, with at
most 128 cached mailboxes. Each reuse
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

The optional Linux timer is **off by default**.
Nothing installs or enables it automatically, and no MCP tool can start it. It runs
`proton-mail-mcp-rules-run` for INBOX every five minutes, with a default cap of 50
moves (`PROTON_MCP_RULES_MAX_MOVES=1..200`). Interval and cap are defaults you can
change. The CLI accepts only INBOX (case-insensitive) and uses the existing Bridge
credentials, certificate settings and protected policy;
it requires loopback IMAP and makes no HTTP or SMTP requests.

To turn it on, copy the two [example units](../examples/systemd/) into
`~/.config/systemd/user/`, adjust the service's `ExecStart` to the installed CLI,
and create a private (`0600`) `~/.config/proton-mail-mcp/rules.env` with
`PROTON_BRIDGE_USER`, `PROTON_BRIDGE_CERT`, and `PROTON_MCP_CREDENTIAL_STORE` with any
`pass` location settings, plus the server's `PROTON_MCP_ACTION_LOG`,
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
password, not the Bridge password). This server holds only the Bridge password, reaches
Proton only through the Bridge and never signs in to the Proton API, so its only apply
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
([`sieve.py`](../src/proton_mail_mcp/sieve.py)) then reads the script back and refuses
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

[`sieve.py`](../src/proton_mail_mcp/sieve.py) keeps script generation and the apply
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
[`folder_rules.json`](../src/proton_mail_mcp/folder_rules.json) rules, through the same
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
To text. As for packaged rules, a message whose To header the server's
envelope cannot parse safely (group syntax, encoded display names, non-ASCII or malformed
addresses) never matches a `to` condition. `from` conditions read the raw From header, so
such a sender can match, and the batch then refuses with `Rules batch has unresolved
messages`. Valid UTF-8 display names match normally. Without complete indexed headers, each
saved rule retains its IMAP search narrowing:
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
`UID SEARCH` of INBOX and a metadata fetch (envelope and identity headers) of at most the 200
newest candidates:
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
`$XDG_STATE_HOME/proton-mail-mcp/saved-rules.json`, with `~/.local/state` when
`XDG_STATE_HOME` is unset) or at `PROTON_MCP_SAVED_RULES`,
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
interface. `saved_rules.rules_backend(settings)` builds the backend, always the local store
backend (`local_rules`, [`rules_store.py`](../src/proton_mail_mcp/rules_store.py)). It
implements `status()`, `filter_names()` and `create_filter(name, script)` and reads the
rule back from the exact reviewed script, so only a rendered rule can be stored. A
supported Sieve backend could be selected there later to create Proton filters
instead. Today the Sieve backend stays `unavailable` and refuses, `create_sieve_filter`
is unchanged, and no setting selects another backend. Saving rules uses no Proton
account API and no network beyond the loopback Bridge.
