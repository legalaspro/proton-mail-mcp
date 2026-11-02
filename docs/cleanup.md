# Cleaning up mail

How moves, read state, labels, draft deletion and unsubscribe work, from preview to
redemption. Each tool's arguments are in the [tool reference](tools.md); what every
change needs is in [How approvals work](safety.md#what-each-change-needs).

| Tool | Behavior |
| --- | --- |
| [`preview_action`](tools.md#preview_action) | Read-only preview: `{count, samples, blocked, token, expires_at, action_hash, action, target}` (for unsubscribe, a `targets` list once a token is issued) plus completion information, `approval_max_messages` and `folder_creation` where applicable. Moves, archive, draft deletion and unsubscribe also return `log_id` for the preview record. Samples contain at most 10 messages with `from`, `subject`, `date`, the canonical `folder`, `uidvalidity` and `uid` (plus `alias` for a label or All Mail source), `message_id` (also `id` for compatibility). Supply exactly one of `query` or `ids`. A `limit` above 200 requests a [bounded approval](#bounded-approvals); `delete_draft` accepts no `limit` or nonempty `params`. `move_to_folder` accepts `params.create_folder_if_missing=true` to [create a missing destination in the same approval](#creating-the-destination-in-the-same-approval). Each `action` has one tool that redeems its token: `move` for `move_to_trash`, `archive` and `move_to_folder`; `set_read_state` for `mark_read` and `mark_unread`; `label` for `add_label` and `remove_label`; `delete_draft` and `unsubscribe` under their own names. |
| [`move`](tools.md#move) | Moves the approved batch to the destination its token pins, with the read-state option bound in the preview. `target` states that destination as the preview's `target` reports it: `"Trash"` or `"Archive"` (both must exist) or the exact `Folders/` name, which must exist unless the approval includes creating it, which happens first. Any other `target` refuses without consuming the token. |
| [`set_read_state`](tools.md#set_read_state) | `state="read"` redeems a `mark_read` preview and `state="unread"` a `mark_unread` preview, setting or clearing only `\Seen` for the previewed messages; undo restores each message's prior read state. The other `state` refuses without consuming the token. |
| [`label`](tools.md#label) | `op="add"` redeems an `add_label` preview, copying the approved batch to the existing `Labels/` mailbox bound in the preview. `op="remove"` redeems a `remove_label` preview, removing only the approved label membership and preserving the message in All Mail. The other `op` refuses without consuming the token. |
| [`delete_draft`](tools.md#delete_draft) | Permanently deletes only the approved messages still in Drafts, after rechecking the whole batch. Requires UIDPLUS; uses UID STORE `\Deleted` and UID EXPUNGE for only the targeted UIDs. Returns `log_id`, `count`, `messages`, `partial`, `token_consumed` and `undo_supported: false`; cannot be undone. |
| [`unsubscribe`](tools.md#unsubscribe) | Performs approved HTTPS unsubscribe requests or prepares unsent mailto drafts with separate send previews. HTTPS cannot be undone; mailto sending requires `send_draft` approval. |

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
`folder` defaults to `Labels/<label>` for `remove_label`, to the resolved Drafts folder for
`delete_draft` (which refuses any other folder), and to INBOX otherwise.
It selects the complete matching set, without search
pagination. Alternatively, pass `ids: ["<message@example.test>"]` using RFC
Message-ID header values, not IMAP UIDs. For moves and read-state changes, all
matching UIDs in one query source folder are approved together, including repeated
IDs in the default INBOX.
ID-only selectors and queries without an explicit folder still refuse IDs shared across source
folders; set `query.folder` to select one source explicitly.
Cleanup accepts one control-free token without whitespace, quotes, parentheses, commas or
semicolons, including opaque provider IDs; surrounding whitespace is refused. A quoted local part
or an obsolete form with comments or spaces does not fit, so handle that message by hand. A bare
`local@domain` ID is wrapped in `<...>` before lookup, preview, token binding and execution
checks, and other IDs stay as given. Message-ID matching is otherwise exact and case-sensitive; case variants
are distinct identifiers. Cleanup `text` queries are refused because
Bridge must decrypt message bodies to evaluate them. ID lookup uses ordinary
source folders; a query can explicitly select All Mail for label additions.
Folder moves still require an ordinary source folder.

### What a preview excludes

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

### Preview limits

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
ordering semantics; see [Manual connector folder rules](rules.md#manual-connector-folder-rules).
Initial execute revalidation still refuses a changed approved batch before any
claim or mutation. Continuations keep their existing approved-row checks and
never discover new candidates; a fresh preview creates a new approval.
`preview_rules` excludes the same way, with the same four fields, a message whose
copies have inconsistent sender headers or ambiguous label memberships; it keeps
every other refusal. `apply_rules` and folder-rules continuation stay whole-batch
strict on inconsistent sender headers and identity ambiguity.

### How previews and execution check messages

Source query previews pin `(UIDVALIDITY, UIDNEXT, EXISTS)` and exclude all UIDs
at or above that UIDNEXT. A cheap UID SEARCH verifies that the exact set below
the boundary still exists after fetching. Append-only growth is accepted only
with unchanged UIDVALIDITY and consistent count/UIDNEXT growth. A concurrent
move-out retries that folder inventory twice within the same preview deadline;
continued churn refuses with "mailbox is changing; retry" and no token.
UIDVALIDITY changes refuse immediately. A later visit to that source uses the
same low-UID proof; other folders' membership/identity inventories stay strict.

Execution refreshes the whole approval on the first call, and each later chunk's rows,
before mutating them (at most 200
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
above, including apply's reuse of a kept audit; the certified warm bounds
in [Development](development.md) are synthetic results for a server implementing the required extension,
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
it is not a latency guarantee. A first confirmation verifies the whole approved set and a
continuation the next bounded subset,
then checks its actual headroom before claiming the token. It uses one deadline
bounded by both configured call and execution timeouts and the 48-second
response ceiling, including setup and verification. `estimated_calls` uses this
same capped budget and unchanged reserves, conservatively planning one full
chunk per call. Chunks contain at most 200 rows for moves and read-state changes
and 50 rows for label actions, shrinking to fit the measured verification
allowance and the remaining mutation budget. Uncertified paths also reserve measured full inventory time
before another chunk. Certified moves with destination UIDNEXT
use observed bound-UID verification cost and start each call with no fixed inventory floor;
within a call, one chunk's share of the last verification is that floor. Before freezing a certified chunk, execution
also reserves its final pre-claim identity check; observed mutation costs can
increase the existing two-second planning floor without raising any timeout.

A certified continuation resumes stored untouched handles without rerunning the
selector or a full source inventory. Unchanged UIDVALIDITY/UIDNEXT certificates
use exact bound UID reads. Small source append ranges are checked against every
untouched approved identity, bounded by the chunk size. Changed validity, a large
append range or ambiguous identity refuses continued certification and requires
review; no mailbox-wide fallback spends the continuation budget.

The mutation allowance retains a two-second floor. The floor leaves margin for mutation and
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
the first chunk, `apply_rules` returns zero done with the exact untouched continuation and
`error_code: first_chunk_unavailable`, and `move`, `set_read_state` and `label` return the same
code with no continuation, while the supplied token stays valid until expiry; neither claims any message was changed. A
`move`, `set_read_state` or `label` continuation verifies
its exact remaining handles and reports changed rows as blocked. The rules path
also retains its whole reviewed-set preflight, including protected skips, before
any group mutates, so a row or rule that changed refuses an `apply_rules` continuation, which
keeps its token; a large rules verification may still refuse without progress.
Set the client's timeout above the configured server budget as described in
[Configuration](configuration.md#call-budgets-and-token-lifetimes); the server cannot infer an arbitrary shorter client deadline.

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
The retry must use the same tool and selector. Other cleanup actions retain single-use
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
after it. A row that changed before the command stays untouched; it is named
in `mismatched_ids` (`mismatched`, with its reason and any `uncertain` status, under
`verbose=true`) and becomes `blocked` for the continuation. After STORE, every UID's
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
copy, which includes every UID whose source or destination read failed, is named
in `mismatched_ids` (`mismatched`, with its reason and any `uncertain` status, under
`verbose=true`) and never counted as done. A command that the server refuses, or whose reply is lost, leaves every
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
`PROTON_MCP_APPROVAL_MAX_MESSAGES` (default 500, at most 1000). Every `preview_action`
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

## Guarded draft deletion

Call `preview_action(action="delete_draft", ids=[message_id])`, or select drafts
with `query={"folder": "Drafts", ...}`. Show the preview and obtain explicit
approval before calling `delete_draft(token)`. The confirmation form states that
the deletion is permanent, names the resolved Drafts mailbox and warns that it
cannot be undone. The preview returns a `log_id`; execution returns a separate
`log_id` and `undo_supported: false`.

Drafts resolves from the one special-use `\Drafts` mailbox, or from
`PROTON_BRIDGE_DRAFTS_FOLDER` only when no mailbox has that flag. Several `\Drafts` mailboxes,
or a resolved mailbox that is missing, unselectable or has a conflicting flag, refuse. The
preview refuses the
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
an ESP explicitly reviewed for that sender. A one-click POST has no sender-domain
check: any public HTTPS host in the header is accepted, and the preview and form show
it. Header HTTPS targets are preferred,
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
before calling `send_draft(send_preview.token, send_preview.draft_id)`. `draft_id` is
required when `send_preview.approval` is `typed_ok`: a call without it refuses and uses
up the token. The draft must match the discovered recipient list, subject and body
before a send token is issued; subsequent edits invalidate that token. A draft is
never counted as a completed unsubscribe.

The existing guarded-send rules still apply: `PROTON_MCP_SEND_SELF_NO_FORM=1` allows
token-only sending solely to the user's own addresses; third-party unsubscribe mail
requires host confirmation, or a [typed OK](sending.md#typed-ok-when-the-client-has-no-form) on a client
with no form. Send caps, token lifetime, TLS checks, and durable send
journaling are unchanged. Unsubscribe never submits SMTP itself. If a send preview
fails after draft creation, the response retains the draft ID and a clear error;
inspect that draft before requesting another send preview.
Mailto targets have no sender-domain ownership check; they rely on the two explicit
approvals and host confirmation, or a typed OK on a client with no form, for third-party
recipients.

The stdlib has no public-suffix database. To avoid guessing registrable domains,
[`unsubscribe_http.py`](../src/proton_mail_mcp/unsubscribe_http.py) contains a small
reviewed ownership list: `github.com`, `proton.me`, and the reserved example
domain `example.com`. Those senders may use sibling/subdomain links within that
boundary. Other senders' GET links require an exact hostname match; valid sibling links
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
