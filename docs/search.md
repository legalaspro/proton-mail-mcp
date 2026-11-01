# Searching and reading mail

How search, counts and thread reads behave. Each tool's arguments are in the
[tool reference](tools.md); the local index behind them is in
[Search index](search-index.md).

| Tool | Behavior |
| --- | --- |
| [`search_mail`](tools.md#search_mail) | Returns `{messages, next_cursor, total, partial, total_is_exact}` and `partial_reason` when partial. Results limited by incomplete index coverage also carry `index_note` and `index_status` progress annotations. See [Local header search index](search-index.md). Messages include Message-ID, the [canonical](#canonical-message-handles) `folder`, `uidvalidity` and `uid`, an `alias` when found through a view, sender, recipients, subject, date and `seen` (the IMAP `\Seen` flag). Dates use `YYYY-MM-DD`; `since` is inclusive and `before` exclusive. Limits above 100 are capped at 100. `unread_only`, `exclude_subject`, `exclude_text` and the `and`/`or`/`not` `query` are described in [Unread mail, exclusions, queries and counts](#unread-mail-exclusions-queries-and-counts). |
| [`count_mail`](tools.md#count_mail) | Read-only `{folder, count}` for the same filters, computed by IMAP `UID SEARCH` alone: no headers or bodies are fetched. Exact IMAP counts do not depend on index coverage and omit `index_note` and `index_status`. Bridge matches every condition, including raw `From`, `To` and `Subject` text; see [Unread mail, exclusions, queries and counts](#unread-mail-exclusions-queries-and-counts). |
| [`read_thread`](tools.md#read_thread) | Returns `{messages, partial}` with the canonical `folder`, `uidvalidity` and `uid` when resolved, an `alias` for the source mailbox handle read, headers, capped text bodies, attachment filename, type and size (encoded bytes, as the result's `size_note` says) and a per-message `truncated` flag. A read limited by incomplete index coverage includes `index_note` and `index_status`; a read fully resolved through live IMAP omits them. Set `include_thread=False` to read only the requested message and skip canonical lookup, leaving every message's canonical fields null, including direct real-folder reads. Prefers plain text, with stripped HTML as a fallback. |
| [`show_thread`](tools.md#show_thread) | The same bounded read as `read_thread`, preferring HTML parts, for showing mail to the user. Returns `read_thread`'s shape as text (HTML bodies converted to text, plus a `view_ref`, `msg-` and 12 hexadecimal digits, per message) and, for [MCP Apps](mcp-apps.md) clients, a read-only thread view with sanitized HTML in a sandboxed frame; remote images, styles and scripts are blocked. The text carries `size_note`, and the view marks each attachment's size `encoded`. |

## Searching mail

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
[Local header search index](search-index.md). `text`
still uses Bridge's server-side `TEXT` scan, with `SINCE`/`BEFORE` placed first;
Bridge may still scan the folder, so the deadline bounds the wait. Text criteria are
quoted/escaped or sent as UTF-8 literals.

For **All Mail** (identified by its IMAP LIST `\All` attribute, including localized
mailbox names), a complete header index searches all dates. Completed
searches have exact totals and `partial=false`, subject to the deadline and metadata
guards. More than 1,000
indexed header matches remain fully reachable through `next_cursor`; the candidate
cap bounds only remaining uncached header work. How coverage is proved, how small gaps
catch up and how other folders reuse these headers are in
[All Mail coverage and results](search-index.md#all-mail-coverage-and-results).

For All Mail without complete coverage, omitting both `since` and `before` applies a default
30-day `SINCE` window, reported as `applied_since`. These results have
`partial=true` and `total_is_exact=false` relative to the whole mailbox; `total`
counts matches gathered inside the window. Up to 1,000 uncached headers are retained
during the scan, so a later deadline can still return completed results. An old-only
mailbox can return an empty window. Supply either date bound to search a different period.

Search and read replies whose results depend on incomplete index coverage
include a shared `index_note` and `index_status`; complete coverage, exact IMAP counts
and reads fully resolved through live IMAP omit both fields. These progress annotations
do not replace `partial` and `partial_reason` as the search completeness signals. The
[index guide](search-index.md#all-mail-coverage-and-results) describes their fields.

`partial` is the single completeness signal. With `partial=false`, follow
`next_cursor` until it is null to collect the complete answer for the requested
folder, filters and dates within the search's UID snapshot. Every partial response
includes `partial_reason`; `total_is_exact` independently describes the count. The
[index guide](search-index.md#all-mail-coverage-and-results) lists every reason.
Index maintenance remains visible through `reindex(op="preview")`, `reindex_status` and
the server log.

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

### Search continuation

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

sends, after `LIST` and `EXAMINE`, exactly one `UID SEARCH` and no FETCH:

```text
UID SEARCH CHARSET UTF-8 SINCE 01-Oct-2026 UNSEEN FROM "alice" NOT SUBJECT "weekly" OR TO "team" NOT TEXT "draft"
```

Search cursors bind `unread_only`, the exclusions and the normalized query; the order
of keys in a query object does not matter. A search without these options keeps its
previous cursor key, so cursors issued before they existed continue to page.

`count_mail` accepts the same filters, `unread_only`, exclusions and `query` and
returns `{"folder": "INBOX", "count": 12}`. Beyond `LIST` and `EXAMINE` it sends only `UID SEARCH` and never
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

## Reading threads

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
Relatives then come from cached reply links when a folder's proof is current and every
row has them; otherwise they are searched by ASCII Message-ID, References and In-Reply-To
criteria, combined with OR in bounded chunks per frontier. Unicode IDs use
individual synchronizing literal searches for each header field, including when
mixed with ASCII IDs.
Thread headers are fetched in batches and Message-IDs are deduplicated. Headers
are retained before fetching MIME structure and bounded text for kept messages,
so a slow MIME fetch still returns the requested message's headers as partial.
Thread content reads skip synchronous search-index writes and index catch-up; canonical
lookup can re-certify a changed real folder. Canonical
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
most 0.25 seconds when that budget runs out. Each content IMAP command is capped
at 10 seconds, while fallback lookup commands, including `UID SEARCH HEADER Message-ID`, share
the 12-second lookup limit; a watchdog also interrupts
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

## Canonical message handles

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
  canonical fields only with `include_thread=true`: first in an indexed pass after the
  requested body, under the call deadline, then after reading the bodies,
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
no proven copy gets the live lookup below in the folders that fail their proof or may hold
rows without a fingerprint, and no others, stopping at its first copy. So a message listed in two real folders can
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
reports `partial=true`; threads and previews retain `null` canonical fields.
A deadline or mailbox error during resolution sets `partial=true`. Incomplete
metadata also prevents resolution: a
record missing from the FETCH response, or one without a well-formed `ENVELOPE`,
`INTERNALDATE` or `RFC822.SIZE`, for the view handle or for any copy that a
`UID SEARCH` returned. Such a record could belong to another copy of the message,
so the remaining copies never establish uniqueness.

Before a handle resolves, the whole live lookup opens the view, All Mail and every real
folder it read once more (`EXAMINE`); the narrower scan behind a certified All Mail
rereads nothing. Each must report the UIDVALIDITY, UIDNEXT
and message count it was scanned with. UIDNEXT never repeats, so this proves that
no identical message arrived, for example in INBOX after All Mail was scanned,
and that no copy left while the folders were read. A mailbox that changed, or one
that reports no UIDNEXT, leaves the handle unresolved with `partial=true`.
Without an All Mail view, no view handle resolves and no lookup runs: no other
mailbox lists each message exactly once, so a Starred or label message without a
real folder would be indistinguishable from an identical message in INBOX. Search
cursors still page through the searched mailbox's own UIDs.
