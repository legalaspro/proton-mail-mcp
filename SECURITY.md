# Security policy

This server reads, moves and can send mail with your Proton Mail Bridge credentials, so a flaw in
its approval gates matters. Report one privately, never in a public issue.

## Supported versions

Fixes go to `main` and the latest release on [PyPI](https://pypi.org/project/mcp-proton-mail/).

## Reporting a vulnerability

Use [Report a vulnerability](https://github.com/legalaspro/proton-mail-mcp/security/advisories/new)
on GitHub, or email [dmitri@manajev.com](mailto:dmitri@manajev.com). Do not open a public issue,
pull request or discussion about it.

Include:

- the version (`proton-mail-mcp --version`) or commit, your OS, and the MCP client and its version;
- the steps that show the problem, and what you expected instead.

Do not include real mail, email addresses, subjects, tokens or passwords. Use reserved names such
as `example.test` in any example.

## In scope

- Changing mail without a preview, a single-use token, a recheck of the previewed rows or a
  confirmation.
- Sending without a saved draft and an approval.
- Instructions in mail or attachment content that lead to a change.
- The server listening anywhere but `127.0.0.1`.
- A token, password or other credential reaching a log or the audit log.
- Flaws in the installer (`setup`, `configure-client`, `uninstall`, `show-token`, `detect`) or in
  the `connect` relay.

## Out of scope

- Proton Mail Bridge itself: report it to Proton.
- A client that answers confirmation forms on its own, which is a documented limit (see
  [How approvals work](docs/safety.md#confirmation-and-approval-binding)).
- Mail that arrives during a request, which that request does not handle (see
  [AGENTS.md](AGENTS.md#mail-arriving-during-a-request)).

## What happens next

You get an answer within 7 days. The fix is disclosed in coordination with you, in a GitHub
security advisory that credits you unless you ask not to be named.
