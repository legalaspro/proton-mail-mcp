# Recovery, action log and audit trail

How to recover from a lost response or an interrupted call, and what the journal behind
that recovery records.

- [Recover a cleanup action after a lost response](#recover-a-cleanup-action-after-a-lost-response)
- [Action log, undo and interrupted calls](#action-log-undo-and-interrupted-calls)
- [Audit trail](#audit-trail)
- [Location and rotation](#location-and-rotation)

## Tools

| Tool | Behavior |
| --- | --- |
| [`list_actions`](tools.md#list_actions) | Read-only lookup of action, preview and reindex log IDs, linked runs and audited write calls, filtered by confirmation token, timestamp, tool (`action`: a write tool or `preview_action`, or one exact action of a merged tool such as `archive`; `move`, `set_read_state`, `label` and `reindex` find all of theirs) or MCP client name. Reindex records include run metadata and cannot be undone. Pending unsubscribe drafts include `draft_ids` for recovery. No source message IDs, recipients, senders, subjects, bodies, token digests or token IDs are returned. |
| [`undo_action`](tools.md#undo_action) | Reverses a logged action once, logs that undo, and reports missing messages. It takes no token; each call is audited. A large chunk can take several calls: repeat while it returns `partial: true` with "Undo time budget reached" (see [Action log, undo and interrupted calls](RECOVERY.md#action-log-undo-and-interrupted-calls)). |

## Recover a cleanup action after a lost response

Current mailbox commands use socket timeouts bounded by the remaining call
budget and a watchdog for responses that trickle in. They stop 10% of the budget
(at most five seconds) early to allow connection cleanup and a response. A timed
out connection is closed, never reused. A preview timeout issues no token;
guarded execution also has a 48-second ceiling, including the unchanged response
reserve. At that ceiling, the existing 10% reserve is 4.8 seconds and mailbox I/O ends
by 43.2 seconds. Chunk admission
retains its one-second journal reserve and the label actions' 2.0/2.05-second
per-message allowances. Moves and read-state changes send one `UID STORE` and/or
`UID MOVE` per source folder and chunk of at most 200 UIDs, with identical copies in
successive commands, budgeted at 2.0
seconds per command plus 0.05 seconds per further UID; normal budget stops happen
between physical chunks. Execution uses the remaining safe budget, including a
smaller trailing chunk when it fits; it no longer stops at 80% of the configured
timeout. The preview's 80% gate, execution deadlines, reserves and final frozen
chunk checks remain in place. Execution returns journaled
partial progress with a run ID, done/remaining/uncertain counts, stop reason and
continuation when available. The default MCP response includes message IDs and
counts; request `verbose=true` for full per-message details. A read-only attempt
that cannot fit its first chunk returns the explicit `first_chunk_unavailable`
error and leaves its token valid when retry is possible: `apply_rules` returns it as
`continuation_token`, while `move`, `set_read_state` and `label` return null, so retry with the
token you hold. Remaining rows never
silently lose their continuation: expiry or a hard failure reports
`available=false` with `continuation_reason`. If the client's own timeout is
shorter than the server budget, use `list_actions` to establish the outcome before
retrying. Identity checks use raw FETCH evidence rather than `SEARCH HEADER
Message-ID`. The rules-preview raw fallback audits All Mail with UIDNEXT-derived
ranges of at most 200 UIDs, including on servers without CONDSTORE,
checking the deadline before each range and saving progress after each completed
batch. Resume an unfinished audit with `next_cursor`; it cannot issue a token
until membership is verified. Certified-index STATUS and UID-set checks remain
unchanged and may issue `UID SEARCH ALL`. At confirmation, `apply_rules` can reuse
the kept All Mail audit when SELECT supplies UIDNEXT, after fresh scope, expiry,
checksum and allocation checks, and reads known matching occurrences fresh.
This path does not repeat a full All Mail audit or `UID SEARCH ALL`. If an
unrelated row gains a second raw Message-ID matching an approved message while
its UID, EXISTS and UIDNEXT stay unchanged, apply does not re-read that row and
would move the approved message despite the hidden duplicate. Proton
Bridge/Gluon assigns a new UID on edit, so this is unlikely in practice and is
an accepted trade-off. Without SELECT UIDNEXT, apply retains the full raw reread.
Missing or unusable audit evidence requires new discovery, which can still
refuse cleanly on timeout.

Per-chunk intents are written before mailbox
mutations, outcomes after. Approval-wide records now link those chunks, so a completed chunk
cannot stand in for a larger unfinished approval. A synchronous worker may still be active if its client stops waiting before the
48-second ceiling. No mutation continues in the background after the execute
call returns; journaling does not depend on delivery of its response.

Action-log IDs are 32-character lowercase hexadecimal strings. Cleanup execution
returns chunk IDs only in `log_ids`, always a list, including when empty or holding
one ID. `list_actions` exposes `log_id` on each `actions` entry and `log_ids` on
each `runs` entry. Iterate those IDs for recovery; replace the former batch
response's singular `log_id` with `log_ids[0]` only when exactly one chunk exists.
Preview tokens, `run_id` and `audit_id` are separate identifiers and cannot be
passed to `undo_action`.

1. Call `list_actions(token="<supplied confirmation token>")`, or use a narrow
   `since`/`before` UTC time window for older records. A busy journal means the
   worker may still be active; retry lookup after it releases the lock.
2. Read `runs`: `approved_total`, `done`, `uncertain`, `remaining`, `blocked`,
   `status` and `log_ids`. A missing run end is `interrupted`, never completed.
   Counts are reconstructed from chunk outcomes even if a checkpoint was lost.
   `continuation` reports whether an unexpired in-memory continuation exists,
   its fixed resume deadline and eligible remaining count. It contains no secret.
   `remaining` means unchanged by this run and includes blocked rows. For old
   unlinked records, inspect all relevant chunk IDs and their counts.
3. If the response was received with `resume: true`, call the same tool with the
   same `target`, `state` or `op`, passing `continuation_token` as `token`. It
   cannot widen the approval. Unstarted previews retain their configured
   lifetime (ten minutes by default, at most thirty).
   The first successful claim for cleanup or `apply_rules` establishes a fixed
   30-minute resume window, reported in the response's `expires_at`. A batch can
   continue after its preview expiry; later claims and continuations never renew
   that resume deadline. For a `move_to_folder` approval, a lost response can also be
   recovered by calling `move` with the same target and the original preview token or the
   latest continuation already received. These tokens refer only to the same
   unfinished run's untouched, unblocked remainder. They never re-run the
   selector, include new arrivals, or retry done/uncertain rows. All token aliases
   are claimed atomically; an overlapping call is refused while execution is
   active. Every remaining row still receives identity, UIDVALIDITY, fingerprint,
   membership and protection checks. A changed row is blocked and reported.
   Certified resumes use bound UID reads and source allocation certificates;
   a small append delta is checked within the chunk budget. Unprovable source
   changes require a new preview instead of a full continuation inventory.
   Optional execute `limit` caps rows attempted in this call; it cannot widen
   the original approval. A bounded approval (preview `limit` above 200, at most
   `PROTON_MCP_APPROVAL_MAX_MESSAGES`) continues the same way, in chunks of at most
   200; its continuations name only the untouched remainder of the bound set. For a separately reviewed small batch, request
   `preview_action(..., limit=30)` before approving.
   Completion refuses every token for that run. A different action, account,
   target or parameters, a tampered token, and the resume deadline also refuse.
   Other cleanup actions retain single-use continuations; their lost continuation
   requires a fresh preview. Restart or the resume deadline requires a fresh
   preview for all actions because the original in-memory approval can no longer
   be proven.
   Lookup never returns a secret token; only digests are stored.
   A fresh preview must be reviewed as a new approval of the current remainder;
   it does not extend the old approval or authorize previously unapproved rows.
   Fresh cleanup previews exclude item-level identity or membership failures and
   report `excluded_count`, capped `excluded_ids`, `excluded_truncated`, and
   `excluded_reasons`. Their token and optional limit cover only the clean set;
   an all-excluded preview issues no token. Protected-sender failures still refuse
   approval. Folder-rule previews exclude and report only messages with
   inconsistent sender copies or ambiguous label memberships, and otherwise keep
   whole-batch validation, as `apply_rules` does throughout. A rules preview call
   that fails keeps its cursor: retry with it, rather than starting again.
4. To reverse confirmed changes, call `undo_action(log_id="<selected chunk>")`
   for each run chunk. A run ID is a grouping key, not an undo capability. Undo
   retains its identity/protection checks and existing uncertain-outcome rules.
   Undo restores a chunk message by message; for a chunk of up to 200 messages it
   may return `partial: true` with "Undo time budget reached". Call `undo_action`
   with the same log ID again to restore the rest. `deferred` counts messages that
   attempt did not examine; it did not claim them, and they stay retryable.
   Inspect uncertain entries before another mutation; never infer success from
   a disconnected client or a completed subset alone.

Every write tool call also leaves `audit` records in the same journal: one with
`outcome: "started"` before the tool runs and one outcome afterwards.
`list_actions` returns them in `audit` (filter with `action`, `client_name`,
`token`, `since` and `before`). A call that still reads `started` is running or was
interrupted; check its runs and chunks as above. A continuation of an approval the
user already confirmed needs no new confirmation form. A declined, cancelled or
unanswered form retires that preview token without changing mail; request a new
preview and confirmation.

New records exist before MOVE and retain per-message intents. A batched
`UID MOVE` or `UID STORE` writes every message's intent before its one command;
afterwards each UID is verified: a moved UID must be gone from its source and have
a confirmed new copy in the destination (`COPYUID` only proposes destination UIDs;
another client may have moved the message elsewhere first), and a `\Seen` change
is read back. A UID that could not be verified stays `uncertain` and is listed
in the response's `mismatched_ids` (`mismatched`, with reasons, under `verbose=true`). When a move approval also creates its destination
folder, a `create_folder` record carrying the run's token digest and ID precedes
`CREATE`. `list_actions` lists it with that run (by the original token or a used
continuation token, or with `action="move_to_folder"`), never as one of its chunks,
and the call's `audit` entry shows `folder_created` and `folder_creation_uncertain`.
An uncertain creation needs a look at the folder list before a new preview. How undo
finds a message without an established destination UID/UIDVALIDITY is described under
[Action log, undo and interrupted calls](#action-log-undo-and-interrupted-calls).

Absence of a legacy log entry does not establish that no mail moved. Do not infer
a recovery set from the time window alone.

An operator can run this read-only check with the installed environment's Python,
the explicitly selected action-log path, and UTC bounds. It imports only the log
reader, does not load credentials or connect to Bridge, and prints lookup metadata
only. Do not source an environment file or print the raw journal. **This command was
not run against production.**

```sh
python -c 'import json,sys; from proton_mail_mcp.action_log import ActionLog; print(json.dumps(ActionLog(sys.argv[1]).list_actions(since=sys.argv[2], before=sys.argv[3])))' /absolute/path/to/actions.jsonl 2026-10-18T00:00:00Z 2026-10-19T00:00:00Z
```

Replace the example path and dates with the configured log location and the actual
incident window. Lookup is read-only and refuses a busy or invalid log; retry a
busy log after execution finishes. Sender, subject, message IDs, confirmation
tokens, token digests and token IDs are not part of its output, except `draft_ids`, the
Message-IDs of unsubscribe drafts still `pending_send`.

## Action log, undo and interrupted calls

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
lookup leaves the fragment untouched, and the next writer preserves its bytes and appends
a NUL, its SHA-256 checksum and a newline before writing another record. This explicit
seal can itself be interrupted and sealed on the next attempt. Repaired journals
are therefore not ordinary JSONL: use `list_actions` or the `ActionLog` journal
reader. Other malformed complete lines or invalid repair seals refuse recovery. An
incomplete undo outcome leaves its durable claim unmatched and blocks another undo
attempt; a torn claim cannot have authorized a MOVE.

Each approval also has a durable `run_start` before its first mutation, followed
by `run_progress` after each chunk and `run_end` when the call returns. Resuming
appends `run_resume`. These records contain a random run ID, action, token digest
(never the token), approved total, chunk IDs and done/uncertain/remaining counts.
Each chunk carries `run_id`, `chunk_index` and `approved_total`. Recovery derives
counts from per-message chunk evidence, so a crash after a chunk outcome but
before a checkpoint still has correct counts. A start/resume without an end reads
as `interrupted`, even when the last chunk completed. A run is `completed` only
when all approved rows are done and no chunk reports a partial outcome. Run records share
the same fsync ordering, and no run-level undo or new permanent-delete path is added.

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
stops before claiming leaves its token valid; `apply_rules` returns it as
`continuation_token`, while `move`, `set_read_state` and `label` return null, so retry with
it. If no first chunk fits, `error_code=first_chunk_unavailable` makes
the refusal explicit. A nonterminal budget stop retains a usable continuation;
hard failures or expiry report `available=false` and `continuation_reason`.
`blocked_ids` and `missing_ids` identify unchanged problem rows; `verbose=true`
replaces the `_ids` lists with full rows. `stop_reason` gives the interruption reason, or `null`
on completion.

`list_actions(token=...)` finds the whole linked run and its chunks from the
original token or a continuation that wrote a chunk, including after restart.
A continuation that only blocks changed rows returns the run ID; use the original
token or time bounds for its lookup. Time
bounds are timezone-aware ISO timestamps (`since` inclusive, `before`
exclusive) or plain `YYYY-MM-DD` dates in the server's local timezone. A date covers
that whole day in either bound: `since` starts when the day starts and `before` ends
when it ends, so one date in both finds that day. This differs from `search_mail`,
whose `before` leaves its date out. `actions`/`truncated` retain their existing fields
and chunk recovery IDs. A completed chunk in a partial run is shown as partial.
`runs`/`runs_truncated` expose the run's status, approved total, done/uncertain/remaining/blocked
counts and chunk IDs through `log_ids`, linking each chunk to its approval-wide progress. A run
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
identities, parameters or token digests are added to lookup output, except `draft_ids` for
unsubscribe drafts still `pending_send`.

Lookup opens the journal read-only and does not create/chmod it or change mail
or tokens. A busy log refuses promptly; retry after the operation finishes.
See [the recovery steps](#recover-a-cleanup-action-after-a-lost-response) for lost responses and interrupted runs.

A failed initial append prevents mutations. A later disk write/fsync failure stops
execution; the durable intent may leave the last message uncertain. No journal can
make an IMAP command and a local disk append atomic. Keep the log on reliable local
storage and inspect unresolved outcomes before attempting further cleanup.

`undo_action(log_id)` verifies the recorded destination UID and UIDVALIDITY,
including the All Mail recovery identity for removed labels, and checks the
Message-ID and the recorded fingerprint before reversing a folder move, Seen
change or label change. A changed mailbox identity, absent UID or different
fingerprint is reported as
missing with `partial=true`; a logged UID never falls back to a Message-ID search. A failed
fetch before the claim fails the call and journals nothing; after the claim, the attempt
returns `partial=true` and an interruption `error`. Both stay retryable.

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

## Audit trail

The same journal records every write tool call; there is no second log. Each MCP
call to `create_draft`, `forward_message`, `save_attachment`, `send_draft`, `delete_draft`,
`move`, `set_read_state`, `label`, `apply_rules`, `unsubscribe`, `create_folder`,
`create_sieve_filter`, `add_rule`, `reindex` or
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

`outcome` is `completed`, `partial`, `pending_send`, `refused` (with the server's own `reason`),
`failed` (backend error, without details), `invalid_arguments`, `cancelled`, or a
confirmation result such as `confirmation_declined`, `confirmation_unavailable` or
`confirmation_timeout`. A declined `send_draft` form reads `refused` here; its send record
names the confirmation result. A `started` record without an outcome means the call is
still running or was interrupted; inspect its run and chunk records. `client` is
the MCP client's self-reported `clientInfo` name and version (not authenticated)
plus the transport; stateless HTTP leaves name and version null. `token_id` is the
first 16 hex digits of the token's SHA-256, matching the `token_digest` prefix of
run and chunk records; tokens themselves are never written. `create_draft`
records only recipient domains, the recipient count, SHA-256 digests of subject
and body, and the new draft's Message-ID, plus `attachment_count` and `from_domain` when used.
Recipients are counted as the draft's
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

## Location and rotation

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
from zero, so rotate at least 24 hours after the last send. A draft already or possibly sent
is also no longer refused. Inspect an archive
read-only with the `ActionLog` reader shown in the
[recovery steps](#recover-a-cleanup-action-after-a-lost-response), pointed at the archived path.
