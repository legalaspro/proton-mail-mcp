# Results, refusals and limits

How to read what the tools return: what each field and refusal means, what to do next, and the fixed
limits. Each tool's arguments are in the [tool reference](tools.md).

## Results and identifiers

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

### Identifiers

| Name | Form | Use |
| --- | --- | --- |
| `message_id`, `draft_id` | RFC Message-ID, such as `<a@example.test>` | Names a message or draft |
| `folder`, `uidvalidity`, `uid` | Canonical real-folder handle | Null when unresolved. A UID means nothing without its folder and UIDVALIDITY |
| `alias` | `folder`, `uidvalidity`, `uid` | The view that was read, such as All Mail or a label |
| `canonical_reason` | `no_single_copy`, `lookup_unfinished`, `lookup_skipped` | `read_thread` and `show_thread`, only when the canonical fields are null. See [Canonical message handles](search.md#canonical-message-handles) |
| `log_id` | 32 lowercase hexadecimal characters | One logged action; pass to `undo_action` |
| `log_ids` | List of `log_id` | Always a list on batch tools, even empty. Undo each separately |
| `run_id` | Random ID | Groups one approval's chunks. Not an undo capability |
| `audit_id` | Random ID | One write call's audit records |
| `action_hash` | SHA-256 | Binds a token to its exact action |
| `draft_hash` | SHA-256 | Binds a send to its exact message and envelope |
| `view_ref` | `msg-` and 12 hexadecimal digits | A thread view's reference to a message in the text result |

## What each tool returns

| Tool | Returns |
| --- | --- |
| `search_mail` | `messages`, `next_cursor`, `total`, `partial`, `total_is_exact`; when they apply `partial_reason`, `applied_since`, `skipped_uids`, `timed_out`, `error`, `hint`, `index_note`, `index_status` |
| `count_mail` | `folder`, `count`; on timeout also `partial`, `timed_out`, `partial_reason`, `error` |
| `read_thread` | `security_notice`, `messages`, `partial`; when they apply `partial_reason`, `timed_out`, `error`, `size_note`, `index_note`, `index_status` |
| `list_attachments` | `attachments` (`index`, `filename`, `type`, `size`), `size_note` |
| `read_attachment` | `kind` (`pdf`, `text` or `image`), `index`, `filename`, `type`, `size`, `decoded_size`; text with `next_offset` and `truncated`; a PDF adds `pages`, `pages_read`, `pages_failed`; an image comes as image content with `mime_type` |
| `list_from_addresses` | `default`, `allowed`, `default_allowed`, `send_enabled`, `note` |
| `list_rules` | `rules`, `built_in_count`, `saved_count`, `max_saved_rules`, `apply` |
| `reindex_status` | `state`, progress, counts, `run_id`, `swap_outcome`, `last_full_run` |
| `list_actions` | `actions`, `truncated`; once records exist, `audit`, `audit_truncated`, `runs`, `runs_truncated` |
| `preview_action` | `count`, `samples`, `blocked`, `reason`, `token`, `expires_at`, `action_hash`, `action`, `target` (`unsubscribe`: `targets`, with a token), `partial`, `total_is_exact`, `excluded_*`, `personal_provider_bypass_count`; mailbox cleanup adds `mark_read`, `estimated_calls`, `approval_max_messages`, `folder_creation`; moves, archive, draft deletion and unsubscribe add `log_id` |
| `preview_rules` | Per-rule counts and samples, `total_matches`, `batch_count`, `remaining`, `partial`, `total_is_exact`, `reason`, `token`, `expires_at`, `action_hash`, `next_cursor`, `excluded_*` |
| `preview_create_folder` | `name`, `exists`, `token`, `expires_at`, `action_hash`; an existing folder returns `exists: true` and no token |
| `preview_add_rule` | Normalized rule, `rule_sha256`, `script`, `script_sha256`, `apply`, `token`, `expires_at`, `action_hash`; `matches` on request |
| `preview_sieve_filter` | Normalized filter, `fileinto`, `script`, `script_sha256`, `apply`, `token`, `expires_at`, `action_hash` |
| `preview_send` | `draft_id`, `from`, `from_alias`, `to`, `cc`, `bcc`, `subject`, `attachments`, `draft_hash`, `caps`, `approval`, `token`, `expires_at`, `action_hash`; on typed OK also `summary`, `next_steps` |
| `show_thread` | `read_thread`'s shape as text, plus `view_ref` per message |
| `compose_reply` | Prefilled fields as text. It never sends, and writes nothing |
| `review_send` | `preview_send`'s preview plus `body_preview`, and **no token** |
| `create_draft` | The new draft's Message-ID |
| `forward_message` | `draft_id`, `subject`, `attachments`, `not_forwarded`, `size_note`, `warnings`, `next_steps` |
| `save_attachment` | `saved`, `filename`, `index`, `type`, `size`, `sha256` |
| `undo_action` | This attempt's `log_id`, `count`, `messages`, `missing`, `partial`; `deferred`, `unresolved` and `error` when they apply |
| `move`, `set_read_state`, `label`, `apply_rules` | The run result below; `move` adds `target`, and `folder_creation` when the approval creates the folder |
| `delete_draft` | `log_id`, `count`, `messages`, `partial`, `token_consumed`, `undo_supported: false` |
| `unsubscribe` | `log_id`, `count`, `messages` (per-target outcomes), `status`, `partial`, `token_consumed`; mailto targets (send on) add `drafts`, each with `send_preview` or `send_preview_error` |
| `create_folder` | `log_id`, `name`, `created`, `exists`, `partial`, `token_consumed`; `error` when interrupted |
| `add_rule` | `log_id`, `name`, `folder`, `rule_sha256`, `backend`, `added`, `attempted`, `partial`, `token_consumed` |
| `create_sieve_filter` | Always refuses |
| `reindex` | `op="preview"`: coverage, `log_id`, `token`; during a run `state="running"` and `run_id`. `op="execute"`: `log_id`, `run_id`, `state`, `token_consumed`. `op="cancel"`: `log_id`, `run_id`, `state`, `cancel_requested` |
| `send_draft` | `log_id`, `message_id`, `draft_hash`, `result`, `sent`, `uncertain`, `token_consumed`, `remaining_daily_sends` |

The run result is `log_ids`, `run_id`, `count`, `done`, `uncertain`, `run_done`, `run_uncertain`,
`approved_total`, `remaining`, `partial`, `stop_reason`, `token_consumed`, `resume`,
`continuation_token`, `expires_at`, `available`, `continuation_reason`, `message_ids`, `blocked_ids`,
`missing_ids`, `mismatched_ids`, and `error` when a call stops early. `limit` is 1 to 200 and caps
this call. A value above the approved total refuses and keeps the token. `verbose=true` adds
`messages` and replaces the three `_ids` lists with full `blocked`, `missing` and `mismatched` rows.

## Refused or failed calls

A refused or failed call is an MCP error result: `isError: true` with one text block. A preview that
cannot approve anything is a normal result with no `token`, so check for one.

| Text | Meaning | Next step |
| --- | --- | --- |
| `Error executing tool <tool>: <n> validation error(s) for <tool>Arguments` | Bad or unknown argument; the rest names the field. Every tool refuses unknown arguments, with values hidden from the error | Fix the arguments |
| `Error executing tool <tool>: Validation error: <reason>` | The server refused | Read the reason. `request a new preview`, `request a fresh preview` or `preview again` mean a new preview and approval |
| `Error executing tool <tool>: Validation error: User confirmation was not accepted...` | No accepted form; the token is retired | New preview |
| `Error executing tool <tool>: Mailbox operation failed; check Bridge configuration and availability` | Bridge or the backend failed; details are hidden | Check Bridge. Retry reads. For writes, check `list_actions` first |
| `Error executing tool <tool>: Draft preview failed; check Bridge configuration` | From `preview_send` or `review_send`: Bridge failed, or `draft_id` names no single complete saved draft | Check the `draft_id` and Bridge, then preview again |
| `Error executing tool send_draft: Validation error: Guarded send failed; no automatic retry is permitted` | Bridge or the journal failed during the send; the message may have gone out | Check `list_actions` and Sent. There is no automatic retry |
| `Error executing tool send_draft: Send failed; inspect the action log before retrying` | The failure could not be journaled either; the message may have gone out | Check `list_actions` and Sent. There is no automatic retry |
| `Action log is busy or unavailable; no change was made; retry` | The audit record could not be written, so the write did not start | Retry |

`compose_reply` also returns `isError: true`, with `partial_reason`, when its lookup could not
finish. That does not mean the message is missing, so retry.

At the HTTP level:

| Status | Body | Cause |
| --- | --- | --- |
| `401` | `{"error":"Unauthorized"}` | Missing or wrong bearer token |
| `421` | `Invalid Host header` | Host not allowed; set `PROTON_MCP_PUBLIC_HOST` for Tailscale Serve |
| `403` | `Invalid Origin header` | Origin not allowed |

## What refuses

| Attempt | Result |
| --- | --- |
| `create_sieve_filter` | Always refuses, before any form, with no changes |
| Token with the wrong tool, or a `target`, `state` or `op` it does not pin | Refuses without consuming it, before any form |
| Any change after the preview: identity, sender, read state when the approval changes it, membership, links, rules or draft bytes | Refuses; preview again. A `move`, `set_read_state` or `label` continuation instead reports the row in `blocked_ids` and leaves it untouched. An `apply_rules` continuation refuses and keeps its token; preview again |
| Form declined, cancelled, unanswered or failed, or a client with no form | Retires the token, with no changes |
| `send_draft` with a token from another draft | `This token came from another draft; request a new preview` |
| `send_draft` on a `typed_ok` token without `draft_id` | `A typed OK send needs draft_id; request a new preview` |
| A draft already sent | `This exact draft was already sent`; edit it or create a new draft |
| A draft that may have been sent (`uncertain: true`, or a journal reservation with no result) | `This exact draft may already have been sent; check Sent, then edit it or create a new draft` |
| A preview of more than 200 messages without a bounded `limit`, or an `unsubscribe` preview above its cap | Returns the count and `reason`, with no token. `delete_draft` refuses: `Draft deletion batches must contain at most 200 messages` |
| A `limit` above `approval_max_messages` on a preview | Refuses |
| A `limit` above the approved total when redeeming | `Execute limit cannot widen the approved batch`; the token stays valid |
| `limit` or nonempty `params` on `delete_draft`; `limit` on `unsubscribe` | Refuses |
| A cleanup `query` with `text` | Refuses |
| Any protected sender in a cleanup batch | `preview_action` returns `blocked` rows, each with its `reason`, and no token. `preview_rules` skips those rows instead |
| `preview_rules` on any folder but INBOX | `blocked`, with no token: only INBOX is accepted |
| `mark_read` or `mark_unread` on Drafts or Sent, also by special-use flag | `blocked`, with its `reason`, and no `token` |
| A folder move to anything but `Folders/` | Refuses; use `Trash` or `Archive` through their own actions |
| `preview_send` without a certificate pin, unless `PROTON_BRIDGE_SMTP_ALLOW_UNVERIFIED=1` | `SMTP requires a configured Bridge certificate pin` |
| A draft whose From is not on `PROTON_MCP_SEND_FROM` | `Draft From is not an exact allowlisted address` |
| A write when the audit log cannot record it | Refuses before anything changes |
| `undo_action` on a preview, reindex, draft deletion, unsubscribe, send, folder, rule or filter record | `Cleanup log ID was not found or cannot be undone` |
| `list_actions` with a `since` or `before` that is neither `YYYY-MM-DD` nor has a timezone | `Action lookup requires YYYY-MM-DD dates or timezone-aware ISO timestamps` |
| `compose_reply` with `reply_all` but no `message_id` | `reply_all applies only to message_id` |

No tool empties Trash, deletes or renames a mailbox, sends without a draft, or edits, reorders or
removes a saved rule.

## Results that are not errors

These results succeed but need a second look, so check their fields:

| Field | Where | Meaning |
| --- | --- | --- |
| `partial: true`, `partial_reason` | `search_mail`, `count_mail`, `read_thread`, `show_thread` | Not complete. Search reasons: `date_window`, `candidate_cap`, `text_scan_limited`, `metadata_missing`, `timed_out`, `unresolved_handles`, `unresolved_handles_skipped`, `canonical_unresolved`, `no_resumable_cursor` |
| `timed_out: true`, `error` | Same | Deadline reached; available data is kept. `count=0` here is not a completed count |
| `next_cursor` | `search_mail`, `preview_rules` | Pass as `cursor` with identical filters or options. A `preview_rules` cursor lasts ten minutes, a reply spends it, and it can come back null |
| `hint` | `search_mail` | With `no_resumable_cursor`: narrow the search, or ask to reindex. A locally read date window only needs narrowing |
| `index_note`, `index_status` | Search and read | Incomplete index coverage limits this result |
| `partial: true` beside a `token` | `preview_action` | Only some canonical fields stayed null; the token is unchanged |
| `partial: true` without a `token` | `preview_rules` | Continue with `next_cursor`; if null, start a fresh preview |
| `blocked`, no `token` | Previews | Nothing can be approved; narrow the selection or preview again |
| `excluded_count`, `excluded_ids`, `excluded_truncated`, `excluded_reasons` | `preview_action`, `preview_rules` | Rows left out of the token |
| `resume: true`, `continuation_token` | Run results | Budget ended. Call again with the same tool and selector |
| `available: false`, `continuation_reason` | Run results | No continuation. Inspect the outcome, then preview the remainder |
| `error_code: "first_chunk_unavailable"` | Run results | Nothing fit this call. `apply_rules` returns it with a continuation. On the other tools there is none: with `token_consumed: false` the supplied token stays valid until it expires, and otherwise preview again |
| `uncertain` greater than 0, `mismatched_ids` | Run results | The command's outcome is not confirmed. Inspect before acting again |
| `partial: true`, `deferred` | `undo_action` | "Undo time budget reached": call again with the same original `log_id` |
| `uncertain: true` | `send_draft` | The server may have accepted the message. Check Sent |
| `result` | `send_draft` | `sent`, `partial` (some recipients refused) or `smtp_error` |
| `status: "pending_send"` | `unsubscribe` | A draft was prepared. It needs its own send approval |
| `partial: true` | `create_folder`, `unsubscribe`, `delete_draft` | Interrupted. Check `list_actions` and the Proton app before retrying |
| `partial: true`, `attempted`, `added` | `add_rule` | Interrupted call; `attempted: false` means nothing was written |
| `folder_creation` | `move` | `created`, `exists`, `ready`, `uncertain` |
| `error: "already running"`, `state: "running"`, `run_id` | `reindex` | One rebuild can run at a time |

Audit `outcome` values in `list_actions`: `started`, `completed`, `partial`, `pending_send`,
`refused`, `failed`, `invalid_arguments`, `cancelled`, `confirmation_declined`,
`confirmation_cancelled`, `confirmation_timeout`, `confirmation_unavailable`, `confirmation_error`.
A `started` record with no outcome is running or interrupted. A declined `send_draft` form is audited
as `refused`, and its send journal records the result, such as `confirmation_declined`.

## Limits

| Limit | Value |
| --- | --- |
| Search and count call | 20 s; 10 s per IMAP command |
| Thread read | 15 s; 10 s per IMAP content command, 12 s shared by a fallback seed lookup; 32,768 header bytes and 65,536 text bytes per message |
| `preview_rules` call | 20 s; `limit` 1 to 200 |
| Cleanup approval | 200 messages, or up to `approval_max_messages` (500 by default, at most 1000) with a bounded `limit` |
| One command or undoable chunk | 200 messages; 50 for label actions |
| Draft deletion, unsubscribe | 200 messages; 50 messages |
| `read_attachment` | 10 MiB decoded, images 5 MiB, 65,536 characters per call |
| `save_attachment` | 25 MiB decoded, only inside `PROTON_MCP_ATTACHMENTS_DIR` |
| Files on one draft | 20 files, 7 MiB decoded in total |
| One draft with attachments | 10 MiB |
| Send | 10 recipients per message, 20 sends per rolling 24 hours, by default |
| Saved rules | 20 |
| `list_actions` | `limit` 1 to 200 per list |
