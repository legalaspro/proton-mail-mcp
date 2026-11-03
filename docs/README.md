# Documentation

Pages are grouped by what you need right now, following [Diátaxis](https://diataxis.fr/).

## Learn

- [Getting started](getting-started.md): a first session, from install to approving and undoing a
  change.

## Do a task

- [Install](install.md): the installer in full, its `--json` output for agents, manual install,
  launchd, Tailscale Serve, uninstall.
- [Connect an MCP client](clients.md): the `connect` command, the installer writing a client's
  config, and generic MCP over HTTP. Each client has its own page:
  [Claude Desktop](clients/claude-desktop.md), [Claude Code](clients/claude-code.md),
  [ChatGPT](clients/chatgpt.md), [Codex](clients/codex.md), [Gemini CLI](clients/gemini.md),
  [Cursor](clients/cursor.md) and [Grok Bot and other agents](clients/grok-bot.md).
- [Installing with an AI agent](../llms-install.md): the steps an agent follows, with the one
  hand-off to you.
- [Windows](windows.md): native Windows support, what was and was not checked, and WSL2.
- [IMAP IDLE](imap-idle.md): turn on the optional new-mail watcher.
- [Recovery](RECOVERY.md): recover from a lost response, use the action log and undo, read the audit
  trail.

## Look something up

- [Tool reference](tools.md): every tool and argument, generated from the server, in the words the
  model reads.
- [Results, refusals and limits](results.md): what a result or refusal means and what to do next,
  and the fixed limits.
- [Configuration](configuration.md): every environment variable, Bridge TLS, protected senders.

## Understand how it works

- [How approvals work](safety.md): previews, tokens and forms, and what each change needs.
- [Privacy policy](../PRIVACY.md): what it reads, what it keeps where and how to delete it, and
  every network connection it makes.
- [Searching and reading mail](search.md): search, counts, threads and message handles.
- [Drafts and attachments](drafts.md): drafts, replies, forwards and the allowed folder.
- [Sending mail](sending.md): the guarded send path, aliases and the typed OK.
- [Cleaning up mail](cleanup.md): moves, read state, labels, draft deletion and unsubscribe.
- [Folder rules, Sieve filters and saved rules](rules.md).
- [Search index](search-index.md): index coverage, rebuilds and background sync.
- [MCP Apps views](mcp-apps.md): the thread view, the composer and the send review.

## Change the code

- [AGENTS.md](../AGENTS.md): commands, layout, conventions and safety rules, for coding agents and
  people alike.
- [Contributing](../CONTRIBUTING.md) and [Development and offline checks](development.md).
- [Security policy](../SECURITY.md): how to report a vulnerability privately.
- [CI](ci.md): what GitHub Actions runs, where and when, and how a release reaches PyPI.
- [Glama](glama.md): the build settings Glama's listing uses to start the server without Bridge.
- [Changelog](../CHANGELOG.md).

## Where each kind of content lives

Each fact has one home, chosen by who reads it and when.

| Reader | Where they read it | What belongs there |
| --- | --- | --- |
| A model using the server | [Server instructions](tools.md#server-instructions), tool descriptions, argument descriptions, results and refusal text, in [`server.py`](../src/proton_mail_mcp/server.py) and [`tool_docs.py`](../src/proton_mail_mcp/tool_docs.py) | How to use the tools: order, approval, what to do next. A model sees only this, and no page here |
| A person or model reading about the tools | [Tool reference](tools.md), generated from the same text | Nothing by hand: run `make docs` |
| A person using the server | [README](../README.md) and the pages above | What it does, how to set it up, how it behaves in full |
| An AI assistant reading about the project | [`llms.txt`](../llms.txt) | An index of these same pages, with no text of its own |
| A coding agent or contributor | [AGENTS.md](../AGENTS.md) | Commands, layout, conventions and safety rules |
