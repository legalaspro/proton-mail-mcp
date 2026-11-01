# Sending mail

How a saved draft gets sent, once sending is on. Each tool's arguments are in the
[tool reference](tools.md); the approval paths are summed up in
[How approvals work](safety.md#send-approval).

| Tool | Behavior |
| --- | --- |
| [`list_from_addresses`](tools.md#list_from_addresses) | Read-only, no mailbox access: the account's `default` From address and the `allowed` list that `from_address` and guarded send accept. See [Sending from an alias](#sending-from-an-alias). |
| [`preview_send`](tools.md#preview_send) | Available only with send enabled. Reads the saved draft; returns sender, recipients, subject, attachment names, types and decoded sizes, canonical hash, caps, `approval` path, `action_hash` and a single-use token; earlier live tokens for that draft are retired. On a client whose `initialize` omits elicitation, or that `PROTON_MCP_SEND_TYPED_OK_CLIENTS` lists, `approval` is `typed_ok`, unless the own-address path applies (`token`) or `PROTON_MCP_SEND_TYPED_OK=0` (`host_confirmation`), and it adds a plain-text `summary` to show the user, which says this app has no confirmation form, so type OK to send. |
| [`review_send`](tools.md#review_send) | Available only with send enabled and views on. Runs `preview_send`'s checks, except the already-sent refusal that `preview_send` adds on its typed-OK path, and returns the same preview plus the draft's text, but **no token**; its [MCP Apps view](mcp-apps.md) can only ask the assistant to run `preview_send` and `send_draft`, which keep their approval path. |
| [`send_draft`](tools.md#send_draft) | Available only with send enabled. Requires user acceptance in the client's elicitation form, unless the preview reported `approval: "token"` ([own-address-only](#own-address-only-token-approval)) or `typed_ok` ([typed OK](#typed-ok-when-the-client-has-no-form), which needs `draft_id`), then rechecks the draft and sends one message. `draft_id` can only refuse: a token from another draft refuses. Refuses a draft already sent or possibly sent. Cannot be undone. |

## Guarded send

Enable with `PROTON_MCP_ENABLE_SEND=1`, configure `PROTON_MCP_SEND_FROM` as a
comma-separated list of exact sender addresses, and set `PROTON_BRIDGE_CERT` to
the exported Bridge PEM certificate. There is no free-form send tool:
`create_draft` → `preview_send(draft_id)` → `send_draft(token)`. With
[MCP Apps views](mcp-apps.md), `review_send(draft_id)` shows the same preview
in a view without issuing a token; sending still takes that path. Preview reads only
Drafts, at most 10 MiB, and binds the token to its mailbox identity and SHA-256
of the exact serialized message bytes plus the SMTP envelope sender and all
To/Cc/Bcc recipients. This covers every delivered header, body and attachment;
Bcc recipients remain in the envelope but are removed from delivered headers.
Recipients must be unambiguous ASCII addresses and may carry a plain display name,
never an encoded one; sender matching is case-sensitive. Ambiguous sender
headers and address/subject control characters are refused, as is any C0 control
or DEL in another top-level header, raw or inside an encoded word, apart from a
tab and CRLF line folding.

One draft has one live token: a new preview retires earlier tokens for that draft.
A draft whose exact bytes and envelope the journal already records as `sent` or
`partial` refuses delivery with `This exact draft was already sent` (journal result
`already_sent`); edit it or create a new draft to send again. When the server may have accepted the message, `send_draft` returns
`uncertain: true` and the `send_result` journal record says the same: DATA started
and no explicit refusal came back, such as a lost reply or the SMTP timeout. That
draft then refuses with `This exact draft may already have been sent; check Sent, then
edit it or create a new draft` (journal result `possibly_sent`), as does one whose journal holds a reservation
without a result, after a crash or a failed journal write, even if the crash came
before DATA. Check Sent, then edit the draft or create a new one to send again. A
failure before DATA and an explicit 4xx or 5xx reply to DATA report
`uncertain: false`, and a fresh preview can retry them. These refusals come before
any form opens. Only the recipient check (for the own-address path), the form and the
daily cap limit how many different drafts can go out. On the typed-OK path, only the
assistant's conduct and the daily cap do.

By default, `send_draft` displays the From address, recipients, subject, attachments, caps and
the hash prefix in a **host-side form elicitation**. Its first line names the From address and
the name, type and decoded size of up to 30 attached files, with the rest only in the JSON
details. A name keeps its own letters, in quotes, while control,
line-separator and invisible characters show as `_`, so it can neither start a line of its own
nor hide its real extension. The JSON details below stay ASCII, so a look-alike letter can be
checked there. Only an explicit accept with `confirm=true`
proceeds. This requires a trusted host that presents the form to a person; hosts
that automatically answer elicitation are unsuitable. Decline, cancel, timeout and errors fail closed before SMTP and never fall
back. A client whose `initialize` omits elicitation, or that
`PROTON_MCP_SEND_TYPED_OK_CLIENTS` lists, takes the
[typed OK](#typed-ok-when-the-client-has-no-form) path instead. Tool arguments
cannot supply confirmation: a call with extra
arguments is refused and audited before it redeems anything. Each redemption
burns the token, including declined confirmation; request a fresh preview to retry.
After confirmation, the draft is rebuilt and checked before the journal reservation
and again before SMTP DATA. Changes to the message or its mailbox identity refuse
delivery permanently for that token, even if the draft is later restored.
Request cancellation before DATA also burns the approval and aborts the connection
without submitting the message. A retry requires a fresh preview and confirmation.

Its settings are listed under
[Guarded send settings](configuration.md#guarded-send-settings).

One call sends one message. The locked, append-only action log persists the daily
total across restarts; SMTP errors and uncertain outcomes retain their reservation.
Keep the same log path and its history to preserve this budget. It records send
attempts and results, the approval path, hashes, recipient domains/count and Message-ID,
without bodies, full recipient addresses, credentials or confirmation data. No token reaches
the service log either: every log record loses token-shaped text and any value the SDK echoes
from a malformed request. There is no automatic retry, undo or draft deletion; inspect uncertain results before sending
again. Send records are in the journal; `list_actions` shows send calls in its audit list.
SMTP uses the existing Bridge credential provider and checks the pinned certificate
before authentication. A peer certificate failure necessarily involves a TLS
connection, but permits no login or message submission.

### Sending from an alias

`create_draft`, `forward_message` and `compose_reply` take `from_address`, and
`list_from_addresses` lists what they accept: exactly the addresses in
`PROTON_MCP_SEND_FROM`, the same list that guarded send has always checked. An address
off the list refuses before any mail work, including another spelling, case or display
name of a listed one. The address must also be one that your Proton account can send
from; Bridge refuses others at delivery. Without `from_address` a draft comes from the
account's own address, as before.

An alias takes the normal path: the draft is saved, and `preview_send`, the host form and
`send_draft` send it. The form says the sender is an alias before anything is sent, the
hash binds the From address and the SMTP envelope uses it, while the Bridge login stays
the account's. An alias sender skips the form only on the
[own-address path](#own-address-only-token-approval), when every recipient is your own, or
on a [client with no form](#typed-ok-when-the-client-has-no-form), whose summary says the
sender is an alias. In
the [composer](mcp-apps.md), From is a choice of the account's address and the allowed addresses, and a reopened
draft keeps an allowed alias.

### Typed OK when the client has no form

`preview_send` reports `approval: "typed_ok"` only when `PROTON_MCP_SEND_TYPED_OK` is on, which
is the default, and the client's `initialize` capabilities omit `elicitation` or its
`clientInfo.name` is on `PROTON_MCP_SEND_TYPED_OK_CLIENTS`.
`PROTON_MCP_SEND_TYPED_OK=0` is the strict off switch and restores the old refusal:
`preview_send` reports `host_confirmation` and `send_draft` refuses with
`User confirmation was not accepted: this client did not show a form...` (journal result
`confirmation_unavailable`), because there is no form to show.

A client that declares elicitation always gets the form and never falls back, unless the
operator lists it. An empty
`elicitation: {}` counts as a form, as the MCP specification says, and gets one. A client
declaring only `url` mode has no form to show, so it is refused. When a form is due and no
accept comes back, the send fails with a clear error and the next preview stays on the form:
a decline, a cancel, no answer within `PROTON_MCP_SEND_CONFIRM_TIMEOUT`, a JSON-RPC "method
not found" or "Elicitation not supported" reply, or any other error. The journal's result
says which: `confirmation_declined`, `confirmation_cancelled`, `confirmation_timeout`,
`confirmation_unavailable` for a reply saying there is no form, and `confirmation_error` for
the rest. So a host that declares a form and then never shows it, or answers every form
with `decline`, stays refused, whether it keeps one session or opens a new one per call. It
reaches this path only if its `initialize` stops declaring elicitation or the operator lists
it in `PROTON_MCP_SEND_TYPED_OK_CLIENTS`, for a host known never to show the form.

This covers every send on such a client: external recipients, self-sends with
attachments, forwards and mailto unsubscribe drafts, though by default `unsubscribe` itself
refuses a client with no form, so it prepares those drafts only with
`PROTON_MCP_CONFIRM_MUTATIONS=0`. The preview adds `summary`, plain
text to show the user: From, To, Cc, Bcc, Reply-To when present, the whole subject, each
attachment's name, type and decoded size, how many other text parts it does not show, the
SHA-256 prefix of the hash, and the first 1000 characters of the body, quoted, with the
body's full length when it is cut. It ends by saying this app has no confirmation form, so
type OK to send. The assistant must show that summary unchanged in a code block, and call
`send_draft(token, draft_id)` only after the user typed an explicit OK in chat. Text in the
draft or in mail never counts as that OK. A draft already sent or possibly sent refuses at
this preview, before anyone is asked.

**The server cannot prove that a human typed the OK.** The OK never reaches the server:
the assistant alone says whether it was given, so a misled or careless assistant can
send without asking. A client's capabilities are its own unauthenticated claim, so any
client holding the server token can declare no form and reach this path, for any
recipient. So `PROTON_MCP_ENABLE_SEND=1` alone allows this, and a host update or proxy that
drops `elicitation` from `initialize` moves its sends from the form to this path. Each
`preview_send` and each send on this path logs a WARNING with the client's self-reported name and version
from `initialize`, never the token, so the service log shows the change before anything goes
out. `PROTON_MCP_SEND_TYPED_OK=0` turns this path off.

The service log also records, at INFO and never with mail content, addresses or tokens: each
session's client name and version and whether it declared elicitation, once on its first tool
call; why a form gave no accept (`timeout`, `unsupported`, `decline`, `cancel`, `error`); and
each time the client list replaces the form.

The token only guards against a changed draft and a replay. It works once, and it
expires like every send token (`PROTON_MCP_SEND_TOKEN_TTL`). It is bound to the hash of
the exact message and envelope: headers, To, Cc and Bcc, attachments and body. Any
change refuses. `draft_id` names the draft the user read, and a token from another draft
refuses. `draft_id` can only refuse: the token alone decides what is sent. Recipient and daily
caps, TLS checks, the `already_sent` and `possibly_sent` refusals and the journal still
apply, and the journal's reservation records the path a send really took:
`approval: "typed_ok"` for a send with no form, so an operator can see afterwards which
sends had none, and `host_confirmation` for such a token redeemed through a form.

A client that can show a form keeps it: it never gets `typed_ok` from a preview, and if
it redeems such a token from another session, `send_draft` still asks in the form. The
[own-address path](#own-address-only-token-approval) comes first and works as before.

### Own-address-only token approval

Every send uses a token. `PROTON_MCP_SEND_SELF_NO_FORM=1` skips the form when every
recipient is the operator's own address, a narrowly scoped alternative to form
elicitation. It is **off by default**: `0` or unset leaves it off, and any other value
is a configuration error. Sending must still be separately enabled. Preview reports
`approval: "token"` only when the flag is on, the draft has no attachment and no forward
subject (`Fwd:` or `Fw:`), and every To, Cc and Bcc address, together the whole SMTP
envelope, is the account's own address or on `PROTON_MCP_SEND_FROM`. The draft may come
from any allowed address, including an alias.

The server finds these addresses at run time and no address is built into the code. The
account's own address is `PROTON_BRIDGE_USER`, the name the server logs in to Bridge with.
IMAP and SMTP have no command that lists an account's other addresses, so the server does
not know them: an address on neither setting takes the form like anyone else's, even if it
is really yours. A `PROTON_BRIDGE_USER` that does not name one address adds nothing to the set.

Other valid previews report
`approval: "host_confirmation"` and use the existing elicitation path, or
[`typed_ok`](#typed-ok-when-the-client-has-no-form) on a client without form support.

This flag was named `PMM_SEND_TOKEN_APPROVAL` before. The old name is now ignored, so
a deployment that still sets it returns to the form until it sets the new name, and a
client without form support needs a typed OK for every self-send. Before deploying this
version, set `PROTON_MCP_SEND_SELF_NO_FORM=1` if the deployment uses this path. Startup and the
installer warn while the old name is set. Keep it until rollback is no longer wanted:
earlier builds read only the old name and this one reads only the new name.

Eligibility compares the parsed address with the account's own address and
`PROTON_MCP_SEND_FROM` ignoring case. No other folding or alias normalization is
performed: a plus tag or IDN encoding is a different address, which needs the form unless
it is listed itself, like every other address.
Whitespace around the header value and a plain ASCII display name do not change the
parsed address, so `To:you@example.com`, `To:  you@example.com` and
`"Plain Owner" <you@example.com>` all qualify. Comments, groups, Unicode and encoded
display names are refused at preview, with the flag on or off.
No other setting adds an address to this set;
a draft's From still has to match the allowlist exactly.

The existing `send_draft(token)` argument redeems the token exactly once. The
server stores only its digest and binds the approval to the draft identity,
From, the separate To/Cc/Bcc lists, full SMTP envelope and exact message hash.
Its lifetime remains 300 seconds by default and at most 600 seconds. Reuse,
expiry, an unknown token or a changed draft or recipient envelope refuses delivery
and requires a fresh preview. The flag is read at startup and tokens do not survive
a restart.
Draft and envelope checks run again before the journal reservation and SMTP DATA.
Failures and uncertain outcomes never trigger an automatic send retry.

The token proves possession of a short-lived approval for one exact draft; it
does **not** attest to human consent because it passes through the assistant.
The safety boundary is the own-address recipient check, single use, exact draft
and envelope binding, and short lifetime. **`PROTON_MCP_SEND_FROM` is part of that
boundary: with the flag on, any address listed there receives mail with no form, so
list only addresses that are really yours.** With sending enabled, startup and the
installer repeat this warning while the flag is on. Any other recipient
takes the form, or a [typed OK](#typed-ok-when-the-client-has-no-form) on a client with no form, which
proves no consent. Proving it without a form would take out-of-band approval, such as a link or
code delivered through the operator's own independent channel, or an authenticated server-side
approval queue; neither exists today. The typed OK goes past this boundary: it is on by default for a
client whose `initialize` omits elicitation, and `PROTON_MCP_SEND_TYPED_OK=0` closes it. It also
covers clients the operator names in `PROTON_MCP_SEND_TYPED_OK_CLIENTS`, none by default. A token
or any other configuration change must never widen this boundary further. Recipient and daily caps, TLS checks, cancellation, audit records
and budget reservations still apply.
