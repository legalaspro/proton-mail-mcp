# Local header search index

The server automatically uses a private, account-specific SQLite index when
available. It accelerates `search_mail` header matching. A complete index also
removes the default 30-day window for All Mail searches. The mailbox remains the
source of message membership, flags and full-message text searches. Indexed
INTERNALDATE values accelerate chronological paging.
`read_thread` uses fresh cached Message-ID/reply links and canonical proofs; rebuild an older index once to populate reply links, otherwise expansion retains the live header-search fallback.

## Tools

| Tool | Behavior |
| --- | --- |
| [`reindex`](tools.md#reindex) | Changes only the local index, never mail. `op="preview"` (no other argument): All Mail header coverage, missing count, estimated rebuild duration, `log_id`, and a single-use token valid for five minutes; during a rebuild, returns `state="running"` and its `run_id` without another inventory or token. `op="execute"` (`token` only): redeems that token to start one background header rebuild of All Mail and LIST-vetted real folders; returns `log_id` and `run_id`. Existing searches keep using the old index. A running rebuild refuses another start with `"already running"`, `state="running"` and its `run_id`. `op="cancel"` (`run_id` only): requests cooperative cancellation of that active rebuild; discards its candidate and preserves the old index. Any other combination refuses. Every call, including a preview, is audited. |
| [`reindex_status`](tools.md#reindex_status) | Current indexed All Mail count/max UID, last observed All Mail inventory, incremental sync times, automatic recovery progress (`state="rebuilding"` only while queued or running), and manual `last_full_run` counters. Progress includes `indexed_count`, `target`, `percent` and observed-rate `eta_seconds` (`null` until enough rate samples). While the optional [background sync](search-index.md#background-all-mail-sync) is enabled, also its `background_sync` block. Does not start work or contact Bridge. |

## What is indexed

Each entry belongs to one Bridge account/endpoint and one
`(folder, UIDVALIDITY, UID)` handle. Message-ID is not an identity fallback. A
message visible in multiple mailboxes has an entry for each observed handle.

The index stores complete decoded Subject, From, To and Cc header content,
including address names and addresses, plus message dates and membership metadata
when observed. It stores no snippet or body text. Building or warming the index
does not fetch bodies. A `read_thread` with thread expansion first catches All Mail up
to its current epoch, as a search does, because both its expansion and its canonical
lookup need that proof and new mail makes it stale; this is optional work that keeps
the search reserve and fetches only what arrived. Its other reads write nothing. Its
content reads use live metadata, and its canonical lookup uses the index for folders
with a current proof and live metadata for the others, under the same call deadline
and without requiring a complete index. The separate seed
lookup can verify a read-only index hint. Missing hints use one live Message-ID
search per large folder or one UID-range search per small folder, all under a shared
12-second fallback cap within the 15-second thread budget. Folder selection,
UIDNEXT discovery and the search share this cap; fetched raw headers then verify
matches within the remaining thread budget. Offline regressions simulate seed
searches up to 10.5 seconds, including restoration of the content budget. This cap
has not been timed against a live mailbox and does not guarantee that every
Bridge mailbox scan finishes within it. A capped fallback returns `partial=true`
with an incomplete-index lookup reason, without `timed_out` or `error`, plus the
shared rebuild note and structured status described below. A seed and thread
resolved fully through live IMAP do not carry an index-completeness warning.
`compose_reply` preserves those details in a retryable error without composer
fields when the requested message or draft could not be located within the cap.

Header candidate selection uses SQLite `instr` on casefolded decoded headers.
It evaluates case-insensitive substrings across the full indexed folder before
fetching current flags and page data or capping uncached header work. This includes
short terms, sender/recipient addresses or bare domains, subject exclusions and
nested header-only `and`/`or`/`not` queries. SQL wildcard characters are literal
substring characters. Unknown entries and text predicates remain candidates
whenever cached evidence cannot rule them out. Without text or unread predicates,
complete header and date coverage lets SQLite count matches and select the
requested chronological page; Bridge fetches display data only for that page.
A high-match recipient query therefore
does not fetch dates and flags for tens of thousands of matches on every page.
Matches retain the existing ordering, cursors and live unread handling.

Bridge's stored messages remain authoritative for `text`, `exclude_text` and
`count_mail`, whether Bridge compares them or, in a
[large folder's small date window](search.md), this server does. The header index
cannot prove that a full-message text match is absent. Counts
also use Bridge's raw-header matching, which can differ from the decoded headers
used by `search_mail`. Exact IMAP counts therefore omit index-completeness warnings
even while the header index is rebuilding.

`TEXT` searches the entire raw message, including headers outside the displayed
summary; a match need not appear in its subject or visible snippet. The index
does not substitute a subject/from/snippet search for this full-message meaning.
Full-text searches can therefore still take longer than indexed header searches.
A date window does not shorten Bridge's own scan, which reads the whole folder.

## All Mail coverage and results

For a mailbox carrying the IMAP `\All` attribute, the client checks the selected
UIDVALIDITY and compares indexed header UIDs with an authoritative `ALL` inventory.
A certified inventory is saved with its `MESSAGES`, `UIDNEXT` and `UIDVALIDITY`.
When SELECT matches that epoch, search reuses the indexed inventory, including
after restart, avoiding another Bridge `ALL` search or catch-up.
A matching row count alone is insufficient: a removal and arrival can preserve
the count while changing membership. A complete cache searches all dates by
default, including when a text predicate must use Bridge. Header predicates use
the complete decoded header index to skip known nonmatches. Indexed dates
determine the page before live flags and page data are fetched.
Completed searches return exact totals and `partial=false`; normal deadline,
missing metadata and canonical-folder resolution guards still apply.

When All Mail grows under the same UIDVALIDITY, bounded incremental catch-up
searches the new UID range from the saved UIDNEXT to the selected UIDNEXT minus
one. It fetches only missing headers and INTERNALDATE in batches, using read-only
EXAMINE and `BODY.PEEK`; no bodies are fetched or messages marked seen. Completed
batches survive interruption and later calls resume without fetching them again.
Coverage, observed count/maximum UID, `all_mail_observed_at` and `last_catchup_at`
advance together only after all headers are cached and the live epoch is checked
again. An unchanged certified epoch needs no inventory or header fetch.
The tail UID search is authoritative within that bounded range. The coverage
transaction removes cached tail headers that were expunged after an earlier
partial fetch. If an older message was expunged while a large backlog remains,
catch-up still caches up to the cap of missing tail headers without certifying.
Later calls can then reach the full `ALL` reconciliation needed to certify
membership again.
UIDVALIDITY changes, shrinkage and inconsistent membership retain the existing
inventory/fallback path; they do not start a full rebuild. This also runs when
searching INBOX or another folder, restoring that folder's selection afterward.
Catch-up decisions log only static reasons and numeric counts/UIDs, with no mail
content, mailbox names or exception text. Fallback and error transitions use
WARNING so the server's default log level includes them; unchanged epochs stay
at DEBUG/INFO, and unbuilt or disabled caches do not emit catch-up warnings.

`PROTON_MCP_SEARCH_INDEX_CATCHUP_MAX_MESSAGES` bounds the missing-header fetches
(default `2000`, integer from `0` to `10000`). `0` disables catch-up fetches while
retaining membership checks for an apparently complete cache. Growing epochs
can make progress across calls up to this cap per attempt. Larger historical
gaps or unsafe/missing headers leave incomplete coverage; unbounded All Mail
searches retain the 30-day partial path until coverage is complete. Catch-up
reserves time for the ordinary search path. An actual catch-up I/O deadline
closes the interrupted connection and serves the fallback on a new connection
under the original call deadline. The response describes any result limitation
through `partial` and `partial_reason`; a timeout in optional maintenance alone
does not make a completed result partial.

Catch-up may start when more than one second of optional command budget remains,
even if less than the usual socket timeout is available. Each command's socket
and watchdog deadline shrink to that available budget, preserving the ordinary
search and response reserves. Previously the 20-second search cap, 10-second
socket timeout and reserves admitted catch-up only during roughly the first
three seconds, including connection setup, LIST and EXAMINE. A fake-only
`search_mail` regression advances EXAMINE by four seconds and proves that this
budget branch formerly stalled a one-message gap but now completes catch-up.
This reproduces the budget-gate branch; it does not establish whether the live
stall took that branch or an inventory timeout. WARNING logs now distinguish
`reason=budget`, `reason=timeout` and other fallback transitions for diagnosis.

`partial` is the single completeness signal for `search_mail`. With `partial=false`,
follow `next_cursor` until it is null to collect the complete answer for the
requested folder, filters and dates within the search's UID snapshot. Explicitly
malformed INTERNALDATE values are omitted from matches and totals. Every
`partial=true` response has one `partial_reason`:

| Reason | Meaning |
| --- | --- |
| `date_window` | The default 30-day All Mail window limits the search, and a cursor remains. |
| `candidate_cap` | More uncached headers remain beyond the bounded scan. |
| `text_scan_limited` | A locally read date window ran out of time, or a message on this or an earlier page could not be read, stalled, or is longer than the part read. |
| `metadata_missing` | Required message data is unavailable or unusable, including failed metadata fetches or snippet reads. |
| `timed_out` | The requested search exceeded its deadline. |
| `unresolved_handles` | Matching page rows lack a unique canonical handle. |
| `unresolved_handles_skipped` | Bounded canonical-handle retries were exhausted for a row. |
| `canonical_unresolved` | A changed or failed canonical lookup interrupted the page. |
| `no_resumable_cursor` | Continuation is exhausted or cannot fit its bounded cursor. |

`total_is_exact` is independent: it describes the count, and remains true when
unresolved canonical handles drop page rows from an exact matching total.
`next_cursor` continues or retries the same snapshot. Dropped unresolved rows
replace `date_window` or `candidate_cap` with `unresolved_handles`, while other
existing failure reasons are retained. `candidate_cap` and `text_scan_limited` also
replace `date_window`, and `applied_since` still reports the window. Every terminal
partial response with a null cursor uses `no_resumable_cursor`, except a finished
window with an undecided message, which keeps `text_scan_limited`; `applied_since`,
`timed_out` and `error` preserve date-window and timeout context when that replaces
the original limitation. A complete response omits `partial_reason`.
Index health and maintenance are visible through `reindex(op="preview")`,
`reindex_status` and the server log.

Migration: replace `search_index`, `search_index_details` and `retryable` reads
with `partial` / `partial_reason` for completeness and `next_cursor` for continuation.
Match `canonical_unresolved` instead of `canonical_lookup_failed` in `partial_reason`.

Coverage proof persists in a local metadata table; local invalidations clear
certification in the same transaction as their header/UID changes. Proof reads
verify the complete-header count in the same SQLite snapshot, so older writers
cannot leave certification valid after losing header rows. Other folders use
their own UID inventories and can return complete results even when All Mail
coverage is incomplete.

Any tool's complete `ALL` inventory removes the rows it no longer lists. When its
count and UIDNEXT match the SELECT it was read under and every UID it lists has known
headers, the same step certifies the folder again, at that epoch. A search that caches
the last missing headers certifies the folder before it returns. Until then, including
with a header the index cannot store, the folder stays uncertified, with its ordinary
live inventory. While SELECT matches a certified epoch, a real-folder or label search
with no date, unread or `text` key uses the cached UID inventory as All Mail does, and
sends no `ALL`, on later pages too. It stands down during a rebuild.

Search, count and read tools use one shared helper to add `index_note` and
`index_status` whenever incomplete index coverage limits their result. A typical
note is plain text:

```text
search index rebuilding: 250/1000 messages indexed (25%); results may be incomplete
```

The accompanying fields are:

```json
{
  "index_status": {
    "state": "rebuilding",
    "indexed_count": 250,
    "target": 1000,
    "percent": 25.0,
    "eta_seconds": null
  }
}
```

`target` is the affected mailbox's latest observed message total, or `null`
before that total is known. Search uses its selected folder's count and target.
A limited seed read uses its live seed-folder observation. `percent` is
`100 * indexed_count / target`, clamped to 0–100; it is `null` for an unknown
target and 100 for an empty target. `eta_seconds` is `null` for these
result-specific scopes without an observed rate. `state="rebuilding"` requires
a queued or running worker. Inactive incomplete coverage reports `"incomplete"`,
`"disabled"`, `"unavailable"` or `"failed"`, with matching note text. Both reply
fields are omitted
when coverage is complete or the result does not depend on the index. In
particular, exact IMAP counts and reads fully resolved through live IMAP omit
them. These progress annotations do not
replace `partial` and `partial_reason` as the search completeness signals.

Missing folder headers can reuse complete All Mail search headers after a read-only
ENVELOPE/INTERNALDATE inventory maps unique Message-IDs between the two folders.
This copies cached search fields onto the target folder's own UIDVALIDITY and
UID; it does not substitute All Mail UIDs or claim canonical identity from a
Message-ID. Repeated, invalid or missing Message-IDs remain uncached and use the
bounded header-fetch fallback. Thus an older All-Mail-only index can accelerate
INBOX and other folders without another full header reindex. A complete mapping
certifies the folder's header coverage; incomplete inventories and unsafe mappings
retain the ordinary capped search. Mapping pauses during a rebuild, shares the
configured catch-up message cap between source and target metadata fetches, and
reserves time for the requested search. Completed warming batches persist for
later calls. A changed mailbox or failed mapping falls back; only an interrupted
stream requires a new session within the same deadline.
If All Mail coverage is incomplete or the index is unavailable, unbounded All
Mail searches retain the 30-day window and report `applied_since`, `partial=true`
and `total_is_exact=false`. The initial reason is `date_window`, subject to the
precedence above. Explicit date bounds
still override that window. Cursors retain their original scope if the cache
warms or becomes unavailable between pages.

The 1,000-candidate cap bounds only the remaining uncached header fetches, after
all known header matches have been evaluated. A complete header index can return
older matches from any position in the folder and more than 1,000 matching rows
retain exact totals, `partial=false` and chronological `next_cursor` paging when
the scan finishes. Cached decoded headers satisfy local filters without another
literal fetch. Larger uncached sets report `partial=true`,
`partial_reason="candidate_cap"` and a lower-bound total, following the
[continuation rules](search.md#search-continuation).
The sampled messages are sorted by INTERNALDATE;
the sample may omit newer messages outside the cap, and continuation can return
those newer messages after an earlier sample. Narrow filters or date bounds for exact
chronological pages. Cached header filters with complete date coverage select each
page in SQLite, ordered by descending `(INTERNALDATE, UID)`. Only the selected page
needs live metadata. Existing caches without date metadata warm it through
INTERNALDATE fetches, preserving their decoded header rows. Full fingerprints
are fetched only by bounded mapping/catch-up or canonical lookup, rather than
for every ordinary header-search candidate. Plain,
date-scoped and unread searches retain chronological cursors and live flags,
including the All Mail default window. Plain window scans retain up to 1,000
uncached headers for timeout recovery without limiting the date/flag scan.

Every returned message has a non-null canonical `folder`, `uidvalidity` and `uid`.
Real-folder searches use that folder's own handle. View searches, including All
Mail and labels, retain the found view handle as `alias` only after proving the
unique real-folder copy using envelope, date and size metadata. For All Mail
results and threads, complete indexed fingerprints can establish this proof. Live
All Mail must match its certified inventory, because only it lists every message once:
one copy there rules out a twin. A real folder needs a proof only if it holds a cached
copy of a wanted fingerprint, and no folder gives a handle from its index unless its
certified inventory matches live STATUS. Other folders are not read at all, and a row
in an unproven folder can be stale, so it only says where to look first. STATUS checks
before and after the lookup guard every proof used. A changed holder can re-certify
its proof with a reserved-budget UID inventory when its current headers are already
cached, including after expunges or an arrival observed by an ordinary read. This step
fetches no message metadata and pauses during a background rebuild.

A message no proven folder holds is looked for live, in the folders that fail their
proof and no others, stopping when each wanted message has a copy. Changed folders first
confirm their cached rows of that message by `UID FETCH`. Only a message still without a
copy then reads the UIDs that arrived since a folder's saved proof, found with a `UID`
range, since that proof covers every older UID. All Mail's single copy makes a live copy
with the same fingerprint the message, so a confirmed row settles it without reading the
arrivals. A read does not renew a real folder's proof, so those arrivals keep adding up
until an undated header search of that folder or a rebuild certifies it again.

The `ON`-day search and fingerprint fetch of the live resolver cost a database read per
message, more than the 3 seconds a thread's lookup has in a large folder. They run last,
only for a folder with no saved proof of its current UIDVALIDITY, or a proven or changed
folder with a row of a wanted date that has no fingerprint, as for a row cached before
plain search pages fetched the envelope and size, or by the light filter-search
fallback; such a row heals the next time a search page shows it. If the lookup runs out
of time, a thread keeps the handles the index proved and the lookup placed, and leaves
the others null with no warning. An All Mail search's scan stores the fingerprints it reads;
`read_thread`'s scan writes nothing, so it repeats until such a search or a rebuild warms
that row. Stopping at the first copy means a message listed in two real folders can
resolve to the proven or first-found one; it stays unresolved when the lookup sees both.

This avoids a same-day metadata scan on every page. An All Mail that stays stale after
catch-up, label views and missing or incomplete All Mail fingerprints use the whole live
resolver, All Mail and every real folder, under the same call deadline. A missing or ambiguous canonical
handle drops the unresolved page row with `partial=true`. Its
`unresolved_handles` reason replaces `date_window` or `candidate_cap`, while
other existing failure reasons are retained. A changed or failed lookup reports
`partial_reason="canonical_unresolved"` instead.
Resolved rows on either side are retained and excluded from later pages.
The matching `total` still describes the query; it can exceed the number of
displayed rows when canonical resolution is incomplete. Message-ID alone never
authorizes a canonical handle.

See the [search continuation rules](search.md#search-continuation) for bounded
cursors, retries, and the explicit partial/null-cursor response when continuation
is exhausted. Null certifies completion only when `partial=false`.

Search and count have a 20-second maximum call budget, including connection setup
and fallback searches, and a 10-second per-command timeout, each shortened by
configured limits. Socket timeouts and a watchdog interrupt both stalled and
slowly trickling responses. A timed-out search returns available results with
`partial=true`, `timed_out=true` and a clear `error`, including when setup or the
initial search times out. Its reason is `timed_out` for search work or
`canonical_unresolved` for canonical lookup; terminal continuation uses
`no_resumable_cursor`. A timed-out count returns `count=0`, `partial=true`,
`partial_reason="timed_out"`, `timed_out=true` and an `error`; that zero does not
certify an empty result. Interrupted connections are closed and later calls open
fresh connections.

Server logs use safe index fallback reasons such as `missing`,
`incomplete_headers`, `busy`, `schema_tables`, `corrupt`, `permission_denied`,
`unsafe_permissions`, `repository_path`, `incomplete_files`,
`database_error` and `storage_error`. They contain no paths or message content.

## Storage and updates

The database is in a `search-index` directory beside the configured action log:

```text
$XDG_STATE_HOME/proton-mail-mcp/search-index/<account-endpoint-digest>.sqlite3
```

Without `XDG_STATE_HOME`, the state root is `~/.local/state`. Setting
`PROTON_MCP_ACTION_LOG` relocates the index beside that log. The account and Bridge
endpoint are hashed into the filename to keep accounts separate. This database
contains message headers and metadata, so keep it in private local state,
outside the repository. Locations inside a Git checkout are refused; search
continues without the index.

Ordinary reads incrementally add or refresh observed messages. Moves, including
move-to-trash, remove the source handle; expunges remove their source entries.
Complete `ALL` inventories reconcile removals made by other mail clients. A
date-filtered or incomplete inventory does not discard entries outside its view.
A changed UIDVALIDITY clears that mailbox's old handles before its new UIDs are
used. These updates do not rebuild the full index on every call.

The optional [IMAP IDLE watcher](imap-idle.md) uses the same incremental index
API for new headers, expunges and UIDVALIDITY resets. It uses its own read-only
connections and fetches no bodies. Watching is disabled by default; ordinary
search and read calls continue to update the index independently. The optional
[background sync](#background-all-mail-sync) maintains an existing index without
a standing connection.

The v2 schema has no FTS5 dependency. Automatic startup recovery can create a
missing database and its directory; ordinary calls also create them when message
headers are first cached or explicitly rebuilt. Queries and invalidations of an
absent cache leave local state untouched. Corruption or a
storage error disables the accelerator and keeps the original
search path, with a diagnostic logged once per client instance. The diagnostic
names the safe fallback reason and does not include message content or credentials.
A concurrent operation holding the nonblocking lock is transient: initialization
and subsequent queries retry on the next use rather than disabling that client
permanently. Schema and permission checks remain mandatory. Search does not
require a working index.

Pre-v2 caches are rebuilt from scratch. Opening a cache whose SQLite
`user_version` is not `2`, or which still contains an FTS5 table, closes its
connection and discards the database plus its `-wal` and `-shm` files before
creating a fresh v2 schema. No FTS virtual table is dropped, so this also works
when SQLite has no FTS5 support. One log line reports that the old cache was
discarded without including its path. Old headers, dates and coverage proofs
are discarded; automatic startup recovery restores header coverage even when
periodic background sync is disabled. Ordinary reads also warm the cache, and the
`reindex(op="preview")` / `reindex(op="execute")` workflow remains available for a full
replacement rebuild. A current v2 cache is kept. Newly created indexes use the
same complete schema; ordinary queries and updates do not create tables lazily.

## Automatic startup recovery

`PROTON_MCP_INDEX_AUTOREBUILD_ENABLED` defaults to `1`. At server startup, an
independent worker checks for a discarded, empty or incomplete header cache and
recovers All Mail and LIST-vetted real folders. Set it to `0` and restart to disable
this worker. `PROTON_MCP_BACKGROUND_SYNC_ENABLED=0` disables only the periodic sync
described below; it does not prevent startup recovery.

Startup checks saved local coverage without reading credentials or contacting
Bridge when the cache is healthy. A pending folder pass survives restart even
after All Mail finishes. Missing or incomplete coverage queues recovery without
waiting for Bridge. The worker uses a dedicated,
preemptible IMAP connection, read-only EXAMINE and `BODY.PEEK` header/metadata
fetches. It fetches no bodies, changes no flags or mailbox membership, and gives
way to user requests. `PROTON_MCP_REINDEX_BATCH_SIZE` and
`PROTON_MCP_REINDEX_BATCH_SLEEP` bound its batches and pauses. Start, progress and finish are logged without mail content or credentials. Completed batches
are committed directly to the serving cache, so searches can use them immediately
and a restart resumes from saved headers rather than reading them all again.
Within each folder, the worker fetches the highest UIDs first so recent messages
become readable early. Each UID with missing or unstorable headers is attempted
at most three times per pass, including after transient failures reload its
inventory. The worker leaves that folder uncertified, warms
later folders and finishes with `state="failed"`, `last_error="headers_incomplete"`.
Missing All Mail, unavailable credentials, rejected login and invalid inventory
stop recovery with a fixed reason. Other mailbox failures stop after ten failed
attempts at the same folder and operation, with exponential backoff spanning
roughly four minutes. Successfully revisiting an earlier folder cannot reset that
budget, and foreground calls cannot shorten the backoff. After a terminal stop,
restore the underlying service or configuration, then restart the server to resume
recovery or run a manual `reindex(op="preview")` / `reindex(op="execute")` rebuild.
Fresh coverage still requires an authoritative inventory and current mailbox
epoch; a high maximum UID alone cannot prove completeness.

`reindex_status()` reports `source="auto"` with `state="rebuilding"` while
automatic recovery is queued or running. Inactive incomplete coverage reports
`"incomplete"`, `"disabled"`, `"unavailable"` or `"failed"` instead of inventing
activity or reusing an old success. Once automatic work stops, a failed or
cancelled manual rebuild retains its error, run ID and swap outcome.
`indexed_count` and `indexed_max_uid` describe the current serving All Mail cache;
`all_mail_count` and `all_mail_max_uid` describe the latest observed All Mail
inventory. Automatic recovery also exposes `max_uid` as an alias of
`all_mail_max_uid`. Automatic recovery and catch-up progress include `target`
(the observed All Mail message total), `percent` and `eta_seconds`. Percent is
`100 * indexed_count / target`, clamped to 0–100, and an empty target is 100%.
`target` and `percent` are `null` until an inventory is known. ETA is a rough
estimate from observed All Mail indexing progress; it remains `null` until enough
progress has been observed to measure a positive rate. Real-folder warming can
keep recovery `"rebuilding"` after All Mail reaches 100%, with ETA `null`. Active
recovery never reports a zero ETA. Incomplete nonempty coverage is capped at 99.9%,
so it cannot round up to 100%. `percent_done` remains available for compatibility.
`last_catchup_at` advances with committed recovery
batches and final coverage. Automatic recovery has no manual cancellation handle
or candidate swap: `run_id` is null and `swap_outcome` is `"not_attempted"`.
`last_full_run` retains the manual rebuild's counters. Status reads
local progress without opening an IMAP connection. Journal messages record recovery
start, progress and finish using static reasons and numeric counts, without mail
content, mailbox names or credentials.

This recovery fills missing live-cache headers without replacing the database.
The manual [header rebuild](#background-header-rebuild) below still requires its
preview token before replacing cached headers with a separate candidate.

## Background All Mail sync

`PROTON_MCP_BACKGROUND_SYNC_ENABLED=1` starts one read-only daemon thread that keeps
an already built All Mail index current between requests. This periodic worker is
off by default: off means it creates no thread, connection or file, and the
installer does not set the variable. Automatic startup recovery is controlled
separately. To turn periodic sync off, set it
to `0` and restart. While `PROTON_MCP_IMAP_IDLE_ENABLED=1` the watcher already
maintains the index, so this job stays inactive and reports `imap_idle_enabled`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROTON_MCP_BACKGROUND_SYNC_ENABLED` | `0` | Exact `1` or `0`. |
| `PROTON_MCP_BACKGROUND_SYNC_INTERVAL` | `900` | Seconds between wakes, measured after each tick, plus up to 10% jitter; from 300 to 3600. Missed ticks are dropped. |
| `PROTON_MCP_BACKGROUND_SYNC_MAX_COMMANDS` | `1500` | IMAP commands per UTC day, counted from `imaplib` tags; integer from 100 to 5000. A tick needs 16 spare at the default catch-up cap. An unchanged tick costs about 7, so a 300-second interval needs about 2000 a day. |

A wake sends no IMAP command, and reads no credential, while any of these holds:

- a tool call is active or ended under 30 seconds ago, which includes local ones,
  so polling `reindex_status` more often than that keeps ticks from running;
- no tool call has arrived since the server started or in the last 24 hours, so a
  boot, a crash loop or an unused server spends nothing;
- a rebuild is running, the daily budget has no room for another tick or the job is
  backing off;
- no index exists; automatic startup recovery handles that case separately;
- `PROTON_MCP_SEARCH_INDEX_CATCHUP_MAX_MESSAGES` is `0`.

Otherwise a tick opens one connection under a 20-second deadline and a 10-second
socket timeout, each shortened by configured limits, and reads STATUS for All Mail,
which it finds through LIST once per process and again after a failed tick. An
unchanged epoch ends there. A changed epoch uses the same read-only EXAMINE and
[bounded incremental catch-up](#all-mail-coverage-and-results) as a request, so only
new headers are fetched, with no bodies and no change to `\Seen`. A changed
UIDVALIDITY is left to the next request and sends no EXAMINE. A tick that leaves
its epoch uncertified without adding a known header marks that epoch `stuck`, which
stops repeat EXAMINEs until the epoch changes.

Tool calls never wait for a tick. Each one marks itself active and shuts down the
tick's socket without waiting, so the tick ends with `yielded`. Completed header
batches survive, the saved epoch stays put until catch-up is complete, and a
yielded tick causes no backoff. A cancelled call stays counted until its worker
really ends. New mail arriving during a call is out of scope for that call and is
picked up on the next request, with this job on or off. Cached rows never
authorize a mailbox change. The existing stall guard bounds repeated calls that
cannot make progress.

| Limit | Value |
| --- | --- |
| Commands per tick | At most 16 at the default catch-up cap: setup, LIST, STATUS, EXAMINE, tail SEARCH, `ALL` or a second STATUS, up to 4 FETCHes, LOGOUT |
| Commands per day | `PROTON_MCP_BACKGROUND_SYNC_MAX_COMMANDS` |
| Header rows per day | 5000. A tick runs only if a full catch-up still fits, so at the default cap of 2000 ticks stop after 3000 rows, and a cap above 5000 reports `budget` on every wake. A tick whose `ALL` read fails partway counts only its net new headers |
| Backoff | Doubles from twice the interval on a failure or a change tick over 5 seconds, up to 6 hours; a fast change tick resets it |

While the job is active, the All Mail raw identity audit of `preview_rules` stays in
memory for 24 hours after its last write, instead of 10 minutes, and each wake frees
expired audits. Audit rows hold message identities only, and are never written to
disk. A later preview still revalidates the audit against its current SELECT and
fetches matches afresh. A full All Mail audit can hold tens of MiB for that time.

While the job is active, a `preview_rules` call that finds a finished All Mail audit
and a mailbox that only grew between requests reads just the new UIDs, instead of
enumerating every UID range again. Each such call sends one UID SEARCH of the new
range that is left, and the rows it FETCHes must match that list, so an old expunge
cannot cancel out a row the peer skipped. The count check after that walk catches
an expunge hidden by those arrivals between requests. Either mismatch enumerates
once with UID-only reads, which reads no saved headers again; that continues in
the next call if this one's time runs out. A count that still disagrees refuses,
as before.

With this job on or off, an unfinished All Mail audit keeps its next UID when
mailbox growth between requests increases EXISTS and UIDNEXT by the same amount:
no previously walked row has left, so new UIDs wait at the end of the range on the
next request. A partial reply reports its position and rewind count in the
existing `reason` text as `all_mail_audit=X/Y rewinds=N`, where `X` is the next UID
and `Y` is that call's UIDNEXT. No reply fields are added. Fewer
messages, the same count with a higher UIDNEXT, or any other unequal delta still
restart the full UID walk, reusing saved headers. Hidden duplicate identities
still refuse approval.

While enabled, `reindex_status()` adds a local `background_sync` object: `active`,
`last_outcome`, `last_tick_at`, `last_skip_reason`, `commands_today`, `rows_today`,
`max_commands_per_day` and `max_rows_per_day`. `last_outcome` is the last tick's
result: `unchanged`, `caught_up`, `incomplete`, `stuck`, `yielded`, `failed`,
`uidvalidity_changed`, `no_all_mail` or `no_epoch`. `last_skip_reason` is why the
latest wake sent nothing, or null if it ticked: `request_active`,
`no_recent_request`, `backoff`, `catchup_disabled`, `reindex_running`, `budget`,
`no_index` or `imap_idle_enabled`. The job's own log lines carry these codes, with
WARNING only when it enters or leaves a problem state. The catch-up it reuses keeps
its existing WARNING lines, with numeric counts, for expunges and interrupted reads.

These costs remain. A request that meets a background index write can wait on the
index lock or take its fallback path for that call. Closing a socket need not stop
work Bridge already started. Each tick reads the credential store once, so a locked
store could prompt, which is why ticks need a recent request and back off on
failure. EXAMINE's cost on a large mailbox has not been measured, so check it
before enabling this on one, and raise the interval if it takes seconds.

This job does not change `apply_rules`, source discovery or the first audit after a
restart or rule edit. It does not change physical-copy checks either: their folder
audits, and any batch size narrowed for them, still expire after 10 minutes.

## Background header rebuild

Use the MCP tools to rebuild All Mail and the real folders' header indexes without
stopping the server. All Mail is identified by its IMAP `\All` attribute, including
localized names. Real folders, such as INBOX, Archive, Sent and Trash, are vetted
from LIST attributes; label views are not included. Each folder's headers are read
under that folder's own UIDVALIDITY and UID. The rebuild does not fetch bodies or
change mail. An older All-Mail-only cache can populate folder search headers from
live membership metadata; a rebuild also warms all real-folder copies directly.

1. Call `reindex(op="preview")`. It reports current indexed coverage against All Mail,
   the missing header count, a rough estimated duration, a `log_id`, and a preview
   token that expires after 300 seconds. `eta_is_estimate=true` labels that estimate,
   which includes All Mail and real-folder copies and assumes a fixed per-batch
   cost plus the configured pause; actual Bridge latency can differ. Indexed,
   All Mail and missing counts continue to describe All Mail, not the sum of copies.
   Preview does not start the rebuild. During an active run it instead returns
   `state="running"` and that run's `run_id`, without another inventory or token.
2. Call `reindex(op="execute", token=token)` before the token expires. A token is required,
   single-use, and valid for execution until consumed or expired. It is not bound
   to the preview's mailbox inventory. The call returns promptly with `log_id`
   and `run_id`; a background daemon worker performs the rebuild.
3. Poll `reindex_status()` for the current or last run. `state` is `idle`,
   `rebuilding`, `running`, `succeeded`, `failed`, `cancelled`, `incomplete`,
   `disabled` or `unavailable`. Automatic startup recovery uses `source="auto"`
   and reports `"rebuilding"` only while queued or running. A failed or cancelled
   manual run remains visible after automatic recovery stops.
   The response includes `indexed_count`, `percent_done`, `eta_seconds`,
   `last_error`, `run_id`, and the swap outcome and reason. Automatic recovery and
   catch-up also include `target` and `percent`. `target` is the latest observed
   All Mail message total. `percent` uses serving coverage and is unknown (`null`)
   until a target is known, or 100 for an empty target.
   Automatic recovery and catch-up ETA use observed All Mail progress, remaining
   `null` until a positive rate can be measured. Manual rebuild progress in the
   existing `percent_done` field and its ETA account for header reads in All Mail
   and the real folders.
   `last_full_run` groups the current or last full rebuild's All Mail counters:
   `old_count`, `new_count` and `all_mail_count`. It is `null` before a full run.
   Its `all_mail_count` is the count at that full rebuild; the top-level
   `all_mail_count` is the live count from the latest locally observed inventory.
   Run counters are restored from the account's action log after restart and
   retain that run's values, including after incremental catch-up, failure or
   cancellation. A run cut off by a restart reads `failed`, with counters from the serving
   index.
   In every state, top-level `indexed_count` and `indexed_max_uid` read the
   serving index's current All Mail headers, excluding duplicate real-folder
   rows. `all_mail_count`, `all_mail_max_uid` (also exposed as `max_uid` during automatic recovery) and
   `all_mail_observed_at` describe the latest locally observed All Mail inventory.
   The maximum is the highest
   member UID, rather than `UIDNEXT - 1`, which can include expunged UIDs.
   `last_catchup_at` records the last successfully completed incremental sync or
   committed automatic recovery batch; unchanged coverage checks do not reset it.
   `last_expunge_reconciliation_at`
   records the last authoritative inventory reconciliation, even if nothing was
   removed. Incremental tail catch-up does not set that timestamp; legacy caches
   without a recorded `ALL` reconciliation retain a null value. All timestamps
   are UTC ISO 8601 strings and survive restart.
   UID maxima are numeric strings, with `"0"` for an empty mailbox. Unavailable
   fields are `null`; older caches lack inventory maxima and timestamps until
   refreshed. Status reads local metadata only and never contacts Bridge.
4. To stop an active run, call `reindex(op="cancel", run_id=run_id)` with that run's ID.
   Cancellation is cooperative: the worker stops at a safe boundary and discards
   the candidate. Poll status until it reaches `cancelled`.

There is one active manual rebuild per account and no queue of manual requests.
An execute request while a run is active refuses with `"already running"`,
`state="running"` and the current `run_id`.
The preview, execute and cancel tools use the existing append-only action and
audit log; their local index changes cannot be undone with `undo_action`.
Reindex execution requires its preview token and does not ask for a mailbox-change
confirmation form.

The worker uses a dedicated IMAP connection so it does not occupy the normal
request connection. It selects All Mail and real folders read-only with EXAMINE
and fetches headers with `BODY.PEEK`. It never issues STORE, EXPUNGE, MOVE or other
mailbox mutations.
Batches retain normal Bridge timeout bounds. Rate limits let ordinary calls use
Bridge between batches:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROTON_MCP_REINDEX_BATCH_SIZE` | `100` | UIDs per header fetch batch; integer from 1 to 200. |
| `PROTON_MCP_REINDEX_BATCH_SLEEP` | `0.25` | Seconds between batches; from 0.01 to 60. |

The worker builds a separate private SQLite candidate in the live index's
directory. Searches keep using the old database throughout the build. Each real
folder is checked again for UIDVALIDITY changes and current `ALL` membership;
removed handles are reconciled. An error in any folder or cancellation discards
the candidate and preserves the old index. Before swapping, the worker checks
current All Mail membership again and counts candidate rows with known headers
for that inventory and UIDVALIDITY. Old or unrelated rows
cannot satisfy coverage. Only coverage of at least 99% permits an atomic
`os.replace`, with index connection handling coordinated so readers never see a
partially built database. A lower result discards the candidate and reports the
refusal reason in status. Cancellation or a rebuild error also leaves the old
index intact; failures set `last_error`.
If a reader holds the index lock at swap time, the worker retries for up to five
seconds (bounded by the Bridge timeout). Cancellation interrupts that wait;
continued contention keeps the old index with `swap_reason="swap_busy"`.

The 99% swap guard can leave incomplete coverage until every current All Mail
UID has known headers. Normal catch-up can fill a small remaining gap. Headers
containing NUL are never marked known, so such messages can leave coverage gaps
even after a successful rebuild. A successful swap replaces the entire database
with the newly indexed All Mail and real-folder headers. Label-view headers are discarded;
ordinary reads populate them again. A header rebuild does
not certify body completeness. Missing or unsafe headers and arrivals during a
real-folder scan can still leave that folder incomplete. Search first reuses safely
mapped All Mail headers and retains the uncached candidate cap for any remaining
unknown rows.

The manual candidate worker is local to the running server process, so a server
restart does not resume that candidate rebuild. Automatic startup recovery can
resume from headers committed to the serving cache. Status preserves the manual
run history separately from active automatic recovery. At startup,
stale `<live>.reindex-*` candidates and
their lock files are swept under each candidate's exclusive nonblocking lock;
held locks are skipped. This also reclaims leftovers from a failed discard.
Execute audit records still marked running without a result receive an
`interrupted` result. Execute records identify their account index; records without
that account tag are ignored. A recovered
result reports `swap_outcome="unknown"`, since a crash can happen after replacement
and before the outcome is logged. The private account run lock remains on disk
so concurrent processes keep using the same lock inode.
