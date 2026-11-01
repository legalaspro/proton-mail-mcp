# MCP Apps views

Clients that support the MCP Apps extension (`io.modelcontextprotocol/ui`,
SEP-1865, stable specification `2026-01-26` in the Model Context Protocol
`ext-apps` repository) can render
some tool results as interactive cards instead of plain text. This server ships
those cards as `ui://` resources. Clients without MCP Apps support ignore them
and receive the ordinary text result of the same tools.

| Tool | View resource | What it shows | Mail access |
| --- | --- | --- | --- |
| `show_thread(message_id, include_thread=True)` | `ui://proton-mail/thread.html` | A read-only thread: headers, sanitized HTML or text bodies, each attachment's name, type and encoded size. | Bounded `read_thread` read |
| `compose_reply(message_id=None, draft_id=None, reply_all=False, to=None, cc=None, subject="", body="", attachments=None, from_address="")` | `ui://proton-mail/reply.html` | A reply or new-draft composer that saves unsent drafts through `create_draft`. | One bounded message read, none for a new draft; the view appends drafts |
| `review_send(draft_id)` (send enabled only) | `ui://proton-mail/review-send.html` | The exact `preview_send` preview of one draft, without a token. | Reads the draft like `preview_send` |

`review_send` exists only when sending is enabled (`PROTON_MCP_ENABLE_SEND=1`).
Views are on by default. `PROTON_MCP_APPS_ENABLED=0` removes the view tools and
their resources and restores the previous tool list exactly; restart the server
after changing it. Only the exact values `0` and `1` are accepted.

## How a view works

1. A view tool's `tools/list` entry carries
   `_meta.ui = {"resourceUri": "ui://proton-mail/…", "visibility": ["model"]}`
   (plus the older flat `_meta["ui/resourceUri"]` key that earlier hosts read).
2. The host reads that resource with `resources/read`. It is one complete HTML
   document with MIME type `text/html;profile=mcp-app`. Styles and scripts
   are inlined from the installed package; the document contains no
   URL and loads nothing at runtime: no CDN, font, image or analytics request.
3. The host renders it in a sandboxed iframe. The view and the host talk JSON-RPC
   2.0 over `postMessage` (`ui/initialize`, `ui/notifications/tool-result`,
   `ui/notifications/size-changed` and so on). The view trusts messages only from
   its parent window.
4. Each view tool returns one text block (`content`) for the model and for plain
   clients. Its view reads what it renders from the result's `_meta`, under the
   key `proton-mail-mcp/view`; the result has no `structuredContent`. The host
   passes the whole result to the view in `ui/notifications/tool-result`.

## Security model

- **No remote content.** The resource declares an empty CSP domain list
  (`_meta.ui.csp` with no `connectDomains`, `resourceDomains`, `frameDomains` or
  `baseUriDomains`), so hosts keep their restrictive default policy. The document
  also carries its own policy: `default-src 'none'`, inline scripts and styles
  only, `img-src data:`, `connect-src 'none'`, `object-src 'none'`,
  `base-uri 'none'` and `form-action 'none'`.
- **Only message markup stays out of model context.** The sanitized HTML
  documents that the thread view renders travel only in the tool result's
  `_meta`, the protocol metadata that the MCP Apps specification marks as not
  intended for model context, and the `content` text never contains message
  markup. That is the only isolation the server promises. `content` is ordinary
  tool output that the model reads; so that clients without views get the same
  result, it carries a thread's headers and text bodies, the composer's reply
  fields including the quoted original text, and the send review's preview
  including the draft text. View tools return no `structuredContent`: it is
  ordinary MCP tool output, which clients may give to the model. A client that
  ignores the specification could still show `_meta` to a model; the server
  cannot prevent that.
- **No markup from data.** View scripts create DOM nodes and assign
  `textContent`; they never use `innerHTML`, `document.write`, `eval` or similar
  sinks, and they never use storage, cookies or network APIs.
- **Least privilege; no tokens in views.** Views may call only `create_draft`
  (appends one unsent draft and returns its Message-ID). Every other tool is marked
  `_meta.ui.visibility = ["model"]`, including the view tools `show_thread`,
  `compose_reply` and `review_send`: hosts still render their views when the
  assistant calls them, but MCP Apps hosts must refuse a view's `tools/call` for
  them. So a view can neither obtain nor redeem an approval token, change mail
  other than by saving a new draft, nor read mail itself: it shows only the tool
  result its host delivered. Bearer tokens, Bridge passwords and keys never
  reach a view: the host proxies view requests over its existing authenticated
  MCP connection.
- **No mail text speaks as the user.** Views post chat messages (`ui/message`)
  only from fixed templates filled with server-generated values: a thread
  message's position and hexadecimal `view_ref`, a draft Message-ID that passes a
  strict plain-identifier check, or a hexadecimal hash. Subjects, names, bodies
  and sender-chosen Message-IDs are never quoted.
- Existing gates are unchanged: `PROTON_MCP_CONFIRM_MUTATIONS=1` remains the
  default, and every token-gated tool keeps its preview, token and host
  confirmation flow.

## Verify in a client

1. Restart the server so the view tools and resources are registered. Views are
   on unless `PROTON_MCP_APPS_ENABLED=0` is set.
2. In an MCP Apps client, reconnect or re-add the connector so it refreshes
   `tools/list` and `resources/list`. Clients cache tool lists; a stale connector
   may show no `show_thread`.
3. Ask the assistant to show a specific message or thread. It calls
   `show_thread(message_id)` and a thread card appears in the conversation.
   This performs the bounded, read-only mail access described below.
4. Expand a message and switch between formatted and plain text. Optional:
   switch the client between light and dark themes; the card follows the host
   theme.

If only text appears, the client did not render the view. Check its MCP Apps
settings or logs and reconnect the connector. Clients without view support still
receive the thread text. If a card appears empty while the text result contains
messages, check that the host delivers the result's `_meta`, where the view's
data travels.

## Read thread view

`show_thread(message_id, include_thread=True)` shows mail the user wants to see;
`read_thread` remains the tool for reading without a view. It performs the same
bounded read as `read_thread`: at most 15 seconds overall and 10 seconds per IMAP
command, 32768 header bytes and 65536 body bytes per message across at most 16
partial `BODY.PEEK` requests. Nothing is marked read, nothing in the mailbox
changes and attachments are never downloaded. The one difference is that each
message's `text/html` alternative is preferred over `text/plain`.

- **Text result.** The same shape as `read_thread` (`messages`, `partial`, and
  `timed_out`, `error` and `partial_reason` when they apply, and `size_note` when
  attachments are listed), plus each message's
  `view_ref`. HTML bodies become the text `read_thread` extracts from HTML, so
  clients without views still get the whole thread. The sanitized HTML
  documents are only in the view data in `_meta`, never in the text the model
  reads.
- **View.** One row per message with avatar, sender, recipients, date and an
  attachment marker. The requested message and the newest one start expanded;
  others expand on click, or with Enter or Space on their focused row. Opening
  or closing a message, or switching it between formatted and plain text,
  changes only that message, so the keyboard focus stays on its row or button.
  Attachments show name, type and size only. The size is marked, as in
  `39 KB encoded`: it is the transfer-encoded byte count from `BODYSTRUCTURE`, because
  nothing is downloaded, and base64 is about a third larger than the file's decoded
  bytes. **Details**
  shows the Message-ID and the canonical folder, UID and UIDVALIDITY. Banners
  report partial or timed-out reads, truncated bodies and blocked content.
- **Bounded payload.** Only the requested message and the 20 newest messages carry
  rendered HTML; older ones show their text. Plain-text bodies are shown as text
  with clickable `http(s)` links.

### How message HTML is rendered

Three independent layers keep message content inert:

1. **Server-side sanitizer** (`email_html.py`). It parses the HTML and
   re-serializes only allowlisted elements and attributes with escaped text; no
   source markup is passed through. Scripts, event handlers, `javascript:`,
   `vbscript:` and `data:` links, forms and inputs, frames, objects, embeds,
   media, SVG, MathML, `meta`, `base` and `link` elements and comments are
   removed. Remote and relative images become placeholders sized like the
   original; tracking pixels (2 px or smaller) vanish; `cid:` inline images are
   not fetched. Style attributes keep only safe properties without `url()`,
   escapes or expressions, and no outline; `<style>` blocks that load or escape
   anything are dropped whole, and in the others rules for focused elements
   (`:focus`, `:focus-visible`, `:focus-within`) match nothing. Only
   `data:image/png|gif|jpeg|webp|bmp` images remain.
   Links keep their text but lose `href`; the target moves to an inert
   `data-pmm-href` attribute and becomes the link's title, replacing any title
   the sender set, and the link becomes focusable (`tabindex="0"`,
   `role="link"`). Elements inside a link lose their titles, so no tooltip can
   pose as the destination.
2. **Sandboxed frame.** The sanitized document is shown in a nested
   `<iframe sandbox="allow-same-origin">` without script permission, so the
   browser runs nothing from a message even if a sanitizer bug let something
   through. It also cannot submit forms, open popups or navigate the host page,
   and its links have no `href` to follow. Same-origin access only lets the view
   measure the frame and handle link clicks. The view fits the frame to its
   content once per change, up to 30000 pixels; content sized by the frame
   itself, such as `height:100vh`, scrolls inside the frame instead of growing
   it. The frame
   document carries its own policy, `default-src 'none'; img-src data:;
   style-src 'unsafe-inline'; font-src data:`, and also inherits the view's.
3. **Host policy.** The view declares no CSP domains, so the host's restrictive
   default applies to the view and to the frame inside it.

Clicking a link, or focusing it with Tab and pressing Enter or Space, opens
nothing yet. The view first shows, below that message, where the link really
goes: the site's host name (in ASCII form for international names) or the mail
address, and the full URL exactly as it would be opened, never the link's text
or title. **Cancel** has the focus, so a held or repeated key cannot open the
link; Cancel or Escape closes the panel without opening anything. **Open link**
asks the host to open exactly that URL with `ui/open-link`; hosts may confirm
again, and a refusal is shown in the panel. A host without that capability gets
the same panel with the URL and **Close** only. Links in plain-text and
formatted bodies show a focus outline. In a formatted body the frame sets it
with `!important` in the document's first cascade layer on whatever has keyboard
focus, so message styles can neither remove, restyle nor delay it, and nothing
they style changes when the focus moves.
**Reply** and **Reply all** post a chat message (`ui/message`) asking the
assistant to open `compose_reply` for that message, such as "Draft a reply to
message 2 of the Proton Mail thread view (view_ref msg-…) with compose_reply."
**Forward** posts one asking for `forward_message`, in the same form, and asks the
assistant to ask you for the recipients and which attachments to include.
`view_ref` is `msg-` and 12 hexadecimal digits derived from the message's identity; each
message in the text result carries the same `view_ref`, so the assistant resolves
it to the Message-ID. A sender can choose a Message-ID, so it is never quoted;
neither is a subject, name or other text from the message. The thread view calls
no server tool.

## Draft reply view

`compose_reply` opens a composer. Pass `message_id` to reply to that message,
`draft_id` to reopen a saved draft for editing, or neither for a new draft. With
an id the tool reads only that one message with `read_thread`'s bounds
(`include_thread=false`). It changes nothing in any mode.

- **From.** The first row names the address the draft is written from, so it is on
  screen, like the attachments, before anything is saved. With more than one allowed
  address it is a choice of the account's own address and `PROTON_MCP_SEND_FROM`;
  **Reset** restores the first one. `from_address` starts from another allowed
  address, and anything else refuses.
- **Prefill.** To is the Reply-To address, else the sender; a reply to your own
  message goes to its original recipients. With `reply_all=true` Cc holds the
  other recipients; the view can toggle **Reply all**, and **Reset** restores the
  prefilled fields together with the original Reply all choice. This account's
  addresses (`PROTON_BRIDGE_USER` and `PROTON_MCP_SEND_FROM`) are never
  prefilled. Subjects get one `Re:` prefix and lose control characters.
  `in_reply_to` is the original Message-ID, and the body quotes the original
  text under an attribution line, noting when the quoted text was truncated.
  Each run of empty quote lines (only `>` and whitespace, such as `> >`) becomes
  its first line; quoted text is unchanged.
  Only plain ASCII addresses are prefilled; others are listed in a warning for
  the user to enter.
- **New draft.** With no id the composer opens blank, or prefilled with any of
  `to`, `cc`, `subject` and `body`; these are refused alongside an id. No mail is
  read. Recipients that `create_draft` would refuse return a validation error; the
  rest are prefilled as for a reply, without display names. Subjects lose control
  characters and the body stays exact. `create_draft` needs a recipient, so
  **Save draft** stays disabled until To has one. The draft has no In-Reply-To or
  References, and the composer offers no **Review & send…** for it.
- **Attachments.** The **Attached** row lists each file with its name, type and
  size and a checkbox, and only ticked files go with **Save**; **Reset** restores
  the first choice. A reopened draft's attachments start ticked, as do a saved
  draft's in a reply and files named in `attachments`. A reply to other mail offers
  the original's unticked. The view passes `attachments`, or `attachments_from` with
  `attachment_indexes`, to `create_draft`, which refuses rather than saving a draft
  without a file it cannot read; the error appears in the status line. A file from the
  allowed folder shows its decoded bytes with no mark. A saved draft's or message's file
  shows its encoded bytes, marked `encoded`, so one base64 PDF reads `29 KB` as a local
  file and `39 KB encoded` once its draft is reopened.
- **Editing a draft.** `draft_id` reloads that draft's To, Cc, Subject, text
  body, In-Reply-To, attachments and From. Saving creates a new draft with only
  these fields, as plain text (formatting is not kept); the original stays in Drafts.
  A From address that is not allowed cannot be kept: a warning says the new draft
  comes from the account's own address. `create_draft` writes no
  Bcc, so a draft with Bcc recipients gets them listed in `not_copied` and a
  warning that the new draft leaves them out. The composer shows that warning
  before anything is saved and repeats it next to **Save**; saving stays possible
  and the original draft keeps them. For `draft_id` the
  read also fetches the Bcc header with the message flags and keeps it only for
  a message flagged `\Draft`, so naming a sent message never reveals its hidden
  recipients. Bcc is not carried over: `create_draft`, which the model can call
  too, has no Bcc field, so it never puts hidden recipients on a draft that
  could later be sent from Proton's own apps, outside this server's send review.
- **Saving.** **Save draft** calls the existing `create_draft` tool through the
  host (`tools/call`). It appends one unsent draft with `\Draft`, In-Reply-To and
  References, is audited like every write call, and returns the new Message-ID,
  which the view shows and passes to the model with `ui/update-model-context`.
  Saving again after an edit creates another draft and lists the earlier
  versions, which are never deleted automatically. Save stays available while
  the fields differ from the last saved version; the fields remain editable
  during a save, and edits made meanwhile stay unsaved (the view says so) until
  saved again. A host that does not let views call tools (no `serverTools`
  capability) gets no Save: the button stays disabled, the view says the draft
  cannot be saved from there, and it never sends `tools/call`. **Ask to delete
  earlier versions…** only posts a chat message asking for a `preview_action`
  `delete_draft` preview, which keeps its token and host confirmation.
- **No send.** The composer cannot send: it calls no tool except `create_draft`,
  and every send tool is hidden from views. With send enabled, for a reply or an
  edited draft, **Review & send…** posts a chat message asking the assistant to
  open `review_send` for the saved draft.
- **Text result.** `from`, `default_from`, `from_choices`, `attached` and the prefilled fields as JSON, quoted
  text included, so clients without views can show them and call `create_draft`
  after the user agrees. The model reads them like any tool result. With files listed,
  `size_note` says that a row with `size_encoded: true` holds encoded bytes and any
  other row decoded bytes.

## Review send view

`review_send(draft_id)` exists only with sending enabled. It runs every
`preview_send` check (send enabled, verified SMTP TLS, a complete draft of at most
10 MiB in Drafts, an exact allowlisted From, recipient caps, the rolling daily
budget) and returns the same preview from the same code path: From, To, Cc, Bcc,
subject, attachment names, types, decoded sizes and SHA-256 digests, `draft_hash`, caps, the
`approval` path and `action_hash`. The view always has a From row, which marks an
alias, and an **Attached** row, which says **None** when nothing is attached. Its sizes
are the files' decoded bytes, the numbers the send form shows, so they carry no `encoded`
mark. A forward from `forward_message`
is a draft like any other and appears in this same view. It adds `body_preview`, the text of the exact
message bytes that `draft_hash` covers (at most 20000 characters; HTML drafts are
shown as text). It **issues no token**: there is no `token` or `expires_at`, the
token store is not touched and nothing is journaled, so nothing in the result or
the view can be redeemed.

Sending keeps the existing gate, except that a client whose `initialize` omits elicitation,
or that `PROTON_MCP_SEND_TYPED_OK_CLIENTS` names,
takes a [typed OK](sending.md#typed-ok-when-the-client-has-no-form) instead of the form,
unless the operator set `PROTON_MCP_SEND_TYPED_OK=0`:

1. The user presses **Send…** in the view. The view posts a chat message
   (`ui/message`) asking the assistant to send that draft: call `preview_send`,
   check that its `draft_hash` starts with the 16 hexadecimal characters shown,
   and call `send_draft` with that preview's token. The message quotes only the
   plain Message-ID and the hexadecimal hash. On a client with no form it asks for
   the summary first, and that press does not stand in for the typed OK below.
2. The assistant calls `preview_send`, which issues the usual short-lived,
   single-use token to the model; `preview_send` and `send_draft` are hidden from
   views, so the token never reaches a view.
3. `send_draft(token)` asks the user to confirm the exact draft in the host's
   form (with the same SHA-256 prefix), whose first line names the From address and
   each attached file's name, type and decoded size, and says when the sender is an alias.
   Only a plain note to the user's own addresses can skip the form, also from an
   alias, if the operator enabled the own-address-only token approval: never a draft
   with an attachment, a forward subject or any other recipient. Declining,
   cancelling or any change to the draft refuses and burns the token. The draft is
   checked again before SMTP. On a client whose `initialize` omits elicitation, or that
   `PROTON_MCP_SEND_TYPED_OK_CLIENTS` lists, the assistant shows the preview's `summary` and
   calls `send_draft` only after the user types OK in chat, which
   [the server cannot prove](sending.md#typed-ok-when-the-client-has-no-form).

**Edit draft** and **Refresh…** post chat messages asking the assistant to
reopen the draft in `compose_reply` or to run `review_send` again; like every
other mail read, `review_send` is model-only, so the view calls no server tool.
When the host cannot post chat messages, the view tells the user to ask the
assistant instead.
