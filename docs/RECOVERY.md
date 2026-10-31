# Recover a cleanup action after a lost response

Current mailbox commands use socket timeouts bounded by the remaining call
budget and a watchdog for responses that trickle in. They stop 10% of the budget
(at most five seconds) early to allow connection cleanup and a response. A timed
out connection is closed, never reused. A preview timeout issues no token;
guarded execution also has a 48-second ceiling, including the unchanged response
reserve. At that ceiling, the existing 10% reserve is 4.8 seconds and mailbox I/O ends
by 43.2 seconds. Chunk admission
retains its one-second journal reserve and the label actions' 2.0/2.05-second
per-message allowances. Moves and read-state changes send one `UID MOVE` or
`UID STORE` per source folder and chunk of at most 200 UIDs, budgeted at 2.0
seconds per command plus 0.05 seconds per further UID; normal budget stops happen
between physical chunks. Execution uses the remaining safe budget, including a
smaller trailing chunk when it fits; it no longer stops at 80% of the configured
timeout. The preview's 80% gate, execution deadlines, reserves and final frozen
chunk checks remain in place. Execution returns journaled
partial progress with a run ID, done/remaining/uncertain counts, stop reason and
continuation when available. The default MCP response includes message IDs and
counts; request `verbose=true` for full per-message details. A read-only attempt
that cannot fit its first chunk returns the explicit `first_chunk_unavailable`
error and its still-valid token when retry is possible. Remaining rows never
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

The previous engineer added per-chunk intents and outcomes before mailbox
mutations. Approval-wide records now link those chunks, so a completed chunk
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

Run records share the private append-only journal and fsync ordering. `run_start`
precedes the first mutation, `run_progress` follows each chunk, and `run_end`
closes the call. Subsequent calls append `run_resume`. Only a complete approval
with no partial chunk can read as `completed`; otherwise it is `partial` or
`interrupted`. Earlier entries are never rewritten. No run-level undo or new
permanent-delete path is added.

New records exist before MOVE and retain per-message intents. A batched
`UID MOVE` or `UID STORE` writes every message's intent before its one command;
afterwards each UID is verified: a moved UID must be gone from its source and have
a confirmed new copy in the destination (`COPYUID` only proposes destination UIDs;
another client may have moved the message elsewhere first), and a `\Seen` change
is read back. A UID that could not be verified stays `uncertain` and is listed
in the response's `mismatched`. When a move approval also creates its destination
folder, a `create_folder` record carrying the run's token digest and ID precedes
`CREATE`. `list_actions` lists it with that run (by the original token or a used
continuation token, or with `action="move_to_folder"`), never as one of its chunks,
and the call's `audit` entry shows `folder_created` and `folder_creation_uncertain`.
An uncertain creation needs a look at the folder list before a new preview. Without an
established destination UID/UIDVALIDITY, undo searches all exposed folders,
without MOVE-source allowlist exclusions, for the logged Message-ID, header
digest, size and internal date. It restores only when exactly one physical copy
matches and that copy is in the recorded Trash destination. Labels and All Mail
are virtual views, not extra copies. Zero or multiple matches, a match outside
Trash, or any ambiguity refuses the entry with a clear reason and leaves it
unresolved for manual handling. Recorded destination handles retain their strict
checks; legacy records without size/date require that path.

Absence of a legacy log entry does not establish that no mail moved. Do not infer
a recovery set from the time window alone.

An incomplete final journal line does not hide earlier complete records. Lookup
leaves it untouched; the next writer preserves its bytes and appends a NUL,
SHA-256 checksum of the fragment, and newline before writing another record.
This explicit seal can itself be interrupted and sealed on the next attempt.
Repaired journals are not ordinary JSONL; use `list_actions` or the `ActionLog`
journal reader.
Other malformed complete lines or invalid seals refuse recovery. An incomplete
undo outcome leaves its durable claim unmatched and blocks another undo attempt;
a torn claim cannot have authorized a MOVE.

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
tokens, token digests and token IDs are not part of its output.
