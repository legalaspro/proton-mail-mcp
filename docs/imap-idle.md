# IMAP IDLE search index updates

The optional watcher observes mailbox membership changes and keeps the search
index current between searches. It never sends, moves, deletes, marks or
otherwise acts on mail. Use `search_mail(since=...)` to read recent messages.

## Enable and configure

Watching is off by default. Add these settings to the server's environment or
local LaunchAgent configuration and restart it:

```sh
export PROTON_MCP_IMAP_IDLE_ENABLED=1
export PROTON_MCP_IMAP_IDLE_FOLDERS='["INBOX", "Folders/Factures, été"]'
```

`PROTON_MCP_IMAP_IDLE_ENABLED` accepts exactly `0` or `1`. The folders setting is
a JSON array of 1–8 distinct, nonempty IMAP mailbox names, defaulting to
`["INBOX"]`. Commas and Unicode are supported. Control characters and duplicate
names are refused; INBOX is case-insensitive. Other folder names retain their
case. Use mailbox names as Bridge reports them, including `Folders/` or
`Labels/` prefixes where applicable.

Each watched folder has its own read-only connection and background thread.
Those connections use the same Bridge endpoint, credentials and TLS settings as
ordinary reads. They never hold a tool connection while waiting for events.
The server starts watching once per application lifetime and closes the workers
on shutdown; individual MCP requests and sessions do not start more watchers.
Setting the switch back to `0` and restarting disables watching.

The watcher checks CAPABILITY before using IMAP IDLE (RFC 2177), leaves IDLE to
process changes, and reissues it at 28 minutes, before the 29-minute server
limit. If IDLE is unavailable, it logs the fallback once and uses NOOP/inventory
checks every `PROTON_MCP_IMAP_IDLE_POLL_INTERVAL` seconds (default 60, allowed
30–300). Dropped connections retry with exponential backoff bounded from 1 to
60 seconds. Tool calls remain available during reconnects.

## Search index updates

New messages' headers and INTERNALDATE are added through the incremental index
API using read-only `BODY.PEEK`; the watcher fetches no bodies. Expunged UIDs are
removed, and UIDVALIDITY changes clear stale folder entries. The initial baseline
does not rebuild historical headers. Reconnects retain the previous inventory
and fetch headers for newly observed UIDs.

When an existing All Mail index has coverage metadata, startup and observed
membership changes also run bounded All Mail catch-up. A certified unchanged
All Mail epoch needs no inventory or header fetch. Otherwise an authoritative
`ALL` inventory reconciles expunges and missing UIDs, including UIDs beyond the
indexed maximum, and missing headers are fetched up to
`PROTON_MCP_SEARCH_INDEX_CATCHUP_MAX_MESSAGES` (default `2000`). Complete coverage
is certified again only after every current All Mail UID has known headers.
Large gaps, unsafe headers or an interrupted catch-up leave the cache stale for
ordinary search or a [background rebuild](search-index.md#background-header-rebuild)
to repair. A watcher for another folder temporarily selects All Mail and then
restores its watched mailbox before resuming observation.

Catch-up runs when a watcher wakes for its own folder. To observe changes across
the whole account promptly, include the Bridge mailbox carrying `\\All` in
`PROTON_MCP_IMAP_IDLE_FOLDERS`; an INBOX-only watcher does not wake for changes
confined to Archive or another folder. Searches also perform bounded catch-up, so
watching remains optional.

The index remains an optional cache. If index storage is unavailable, searches
retain their existing Bridge-backed behavior.
