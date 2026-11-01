# Getting started

A first session: install the server, read some mail, save a draft, approve a change and undo it.
Nothing here sends mail. The draft stays in Drafts, and the only change to existing mail is one you
reverse at the end.

You need a paid Proton plan, because Bridge requires one, and an MCP client on the same computer.

## 1. Install and connect

Follow the three steps under [Install](../README.md#install) in the README. They set up Bridge and
the server, print your bearer token and connect your client. To check the connection, ask your
assistant which Proton Mail tools it has. It should name tools such as `search_mail`.

## 2. Read some mail

Ask your assistant:

> How many unread messages are in my inbox? Show me the newest one.

It calls `count_mail`, `search_mail`, then `read_thread` or `show_thread`. If your client supports
[MCP Apps](mcp-apps.md), you see the thread as a view in the chat.

Open the Proton app and look at that message: it is still unread. Search and read open mailboxes
read-only and never mark mail as seen.

## 3. Save a draft

Ask:

> Draft a short reply to that message saying thanks, but don't send it.

The assistant calls `create_draft`, or opens `compose_reply` for you to edit and save the reply.
Look in Drafts in the Proton app: once saved, the draft is there, unsent. Sending is off by default,
so there is no send tool at all yet.

## 4. Approve a change

Pick a sender whose mail you are happy to archive, then ask:

> Archive the messages in my inbox from news@example.test.

This takes two steps:

1. The assistant calls `preview_action`, which changes nothing, and shows you how many messages
   match, with samples. Check them, then tell it to go ahead.
2. The assistant calls `move` with the token from that preview. Your client then shows a form with
   the exact change. Accept it, and the messages move to Archive.

If your client refuses with `this client did not show a form`, it cannot confirm mailbox changes.
See [Forms and typed OK](clients.md#forms-and-typed-ok).

## 5. Undo it

Ask:

> Undo that.

The assistant calls `undo_action` with each `log_id` in the move's `log_ids`, and the messages
return to your inbox. Each logged change can be undone once. Sending, draft deletion and unsubscribe cannot be
undone, and the [approvals page](safety.md#what-each-change-needs) says what each change takes.

## Where to go next

- [Send mail](sending.md), after turning sending on.
- [Protected senders](configuration.md#protected-senders), if there are senders cleanup must never
  touch. No list ships with the project.
- [Recover after a lost response](RECOVERY.md).
- [All documentation](README.md).
