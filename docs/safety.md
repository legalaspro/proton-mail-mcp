# How approvals work

Why an assistant can change your mail only with your approval, and what each change takes. The
[README](../README.md#how-it-keeps-your-mail-safe) has the short version.

The [server instructions](tools.md#server-instructions) and tool descriptions tell the assistant to
wait for your approval, but text cannot enforce that, so the server does not rely on it. A change
needs a single-use token bound to the exact previewed messages, and by default the server refuses
that token until you accepted the same change in your client's form. These weaken that check:

- A client that answers forms automatically.
- `PROTON_MCP_CONFIRM_MUTATIONS=0`, which lets the token alone authorize a mailbox change.
- The [typed OK](sending.md#typed-ok-when-the-client-has-no-form) for a send on a client with no
  form, where only the assistant reports what you said. `PROTON_MCP_SEND_TYPED_OK=0` turns it off.
- `PROTON_MCP_SEND_SELF_NO_FORM=1`, which sends a plain note to your own addresses on the token
  alone.

## What the tools can and cannot do

- Search and read use read-only mailboxes and `BODY.PEEK`; they do not mark messages as seen.
- Draft creation only appends a new message to Drafts with the `\Draft` flag.
- Draft deletion, cleanup, rules, saving custom rules, unsubscribe, folder creation and Sieve
  filter creation require a separate read-only preview, a single-use token bound to the exact
  previewed action set and, by default, the user's confirmation in the client's form.
- **Sieve filters can be previewed but not applied:** Proton Mail Bridge has no Sieve or filter
  interface, so `create_sieve_filter` always refuses without changes and no setting enables it;
  see [Sieve filters](rules.md#sieve-filters).
- **Sending is off by default; send tools are absent unless explicitly enabled.** Guarded send
  requires a saved draft and preview token, with user confirmation through the client's
  elicitation form unless the own-address-only token approval applies. A client whose
  `initialize` omits elicitation takes a [typed OK](sending.md#typed-ok-when-the-client-has-no-form)
  instead, which the server cannot verify, unless `PROTON_MCP_SEND_TYPED_OK=0` refuses it. So
  does a client the operator names in `PROTON_MCP_SEND_TYPED_OK_CLIENTS`, which is empty by
  default.
- Every write tool call is recorded in an append-only audit log. Permanent deletion is limited
  to approved messages in Drafts through `delete_draft`; it cannot be undone.
- There is no empty Trash or mailbox deletion/rename tool. Label removal also uses targeted
  expunge in its `Labels/` mailbox, as explained in
  [Bridge mutation rules](cleanup.md#bridge-mutation-rules).
- Read tools list attachment metadata; [attachment tools](drafts.md#attachments) read and save single
  attachments within size caps, and local files only in the one folder you allow. Guarded send
  reads complete drafts and attachments.
- Clients that support MCP Apps render [interactive views](mcp-apps.md) for some tools; every
  view tool still returns its ordinary text result for other clients. Views load nothing from
  the network and never hold approval tokens.

## What each change needs

Form means the user's confirmation in the client's form, unless the operator set
`PROTON_MCP_CONFIRM_MUTATIONS=0`.

| Change | Preview | Redeem | Form | Undo |
| --- | --- | --- | --- | --- |
| Move to Trash | `preview_action(action="move_to_trash")` | `move(target="Trash", token)` | Yes | `undo_action` |
| Archive | `preview_action(action="archive")` | `move(target="Archive", token)` | Yes | `undo_action` |
| Move to a folder | `preview_action(action="move_to_folder", params={"folder": ...})` | `move(target=<that folder>, token)` | Yes | `undo_action`; a folder the approval created stays |
| Mark read, unread | `preview_action(action="mark_read")`, `mark_unread` | `set_read_state(state="read", token)`, `"unread"` | Yes | `undo_action` |
| Add, remove a label | `preview_action(action="add_label", params={"label": ...})`, `remove_label` | `label(op="add", token)`, `"remove"` | Yes | `undo_action` |
| Folder rules | `preview_rules` | `apply_rules(token)` | Yes | `undo_action` per `log_ids` entry |
| Delete drafts | `preview_action(action="delete_draft")` | `delete_draft(token)` | Yes | None |
| Unsubscribe | `preview_action(action="unsubscribe")` | `unsubscribe(token)` | Yes | None |
| Create a folder | `preview_create_folder` | `create_folder(token)` | Yes | None; remove it in the Proton app |
| Save a rule | `preview_add_rule` | `add_rule(token)` | Yes | None; edit the store by hand |
| Sieve filter | `preview_sieve_filter` | `create_sieve_filter(token)` refuses | Never reached | None |
| Rebuild the index | `reindex(op="preview")` | `reindex(op="execute", token)` | No | None |
| Send a draft | `preview_send` | `send_draft(token, draft_id)` | See below | None |

A continuation token belongs to an approval the user already confirmed, so it runs without another
form.

## Send approval

| `approval` | When | What happens |
| --- | --- | --- |
| `host_confirmation` | Default | `send_draft(token)` shows the host form, and the user accepts it. Decline, cancel, timeout or failure refuses, and never falls back |
| `token` | `PROTON_MCP_SEND_SELF_NO_FORM=1`, no attachment, no `Fwd:` or `Fw:` subject, and every recipient is [the account's address or on `PROTON_MCP_SEND_FROM`](sending.md#own-address-only-token-approval) | `send_draft(token)` shows no form |
| `typed_ok` | `PROTON_MCP_SEND_TYPED_OK` is on, and the client omitted `elicitation` or is listed in `PROTON_MCP_SEND_TYPED_OK_CLIENTS` | The assistant shows `summary` unchanged in a code block, and calls `send_draft(token, draft_id)` only after the user typed an explicit OK in chat |

There is no free-form send. A send starts from a saved draft: from `create_draft`,
`forward_message` or a mailto `unsubscribe`. See [Sending mail](sending.md).

## Tokens

| Token | Lifetime | Notes |
| --- | --- | --- |
| Cleanup, rules, unsubscribe, draft deletion, folder, rule | `PROTON_MCP_CLEANUP_TOKEN_TTL`: 600 s, at most 1800 | Single use. Bound to the exact previewed set |
| Cleanup and rules, after the first successful claim | Fixed 30-minute resume deadline, never renewed | `expires_at` reports it. Redeem `continuation_token` with the same tool and `target`, `state` or `op` |
| `move_to_folder`, after a lost response | Same resume deadline | The original token or latest continuation retries the same untouched remainder |
| Send | `PROTON_MCP_SEND_TOKEN_TTL`: 300 s, at most 600 | One live token per draft; a newer preview retires it. Single use, including after a declined form |
| Reindex | 300 s | Single use. Not bound to the mailbox inventory |

All tokens live in memory: a restart invalidates them. Lookup never returns one.

## Confirmation and approval binding

Every mutation token is bound to its exact preview. `approval_hash` in
[`guarded.py`](../src/proton_mail_mcp/guarded.py) is one SHA-256 over the action,
its target and params, the selector and the approved message rows: identity,
sender and membership data, read state where applicable, the unsubscribe target
and details, the exact Sieve script, and for send the exact draft bytes and SMTP
envelope. Every preview that issues a token, except `reindex(op="preview")`, also returns
this `action_hash`. The
token store keeps only the token's digest beside the stored approval and recomputes
the hash on every lookup, so a token cannot redeem any other action set. Before any change,
execution re-derives the rows from the mailbox (or re-reads the draft) and refuses
when anything differs: identity, sender headers, read state where the action uses it,
membership, unsubscribe
links, folder rules or draft bytes. A `move`, `set_read_state` or `label` continuation
instead leaves a changed row untouched and reports it as blocked; an `apply_rules`
continuation refuses and keeps its token. Unstarted approvals expire according to
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
exact rule and `rule_sha256` for saved rules. `create_sieve_filter` refuses before any form
while no apply backend exists, as on Bridge.
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

Mailbox and saved-rule tools that redeem a token first ask the user to confirm the exact
stored change in the client's form, unless the operator disabled that; see
[Confirmation and approval binding](#confirmation-and-approval-binding). Every
write tool call is audited; see [Audit trail](RECOVERY.md#audit-trail). The local-only
`reindex(op="execute")` uses its preview token without a mailbox-change confirmation.
