# mcp-proton-mail plugin

A plugin that gives an agent the [proton-mail-mcp](https://github.com/legalaspro/proton-mail-mcp)
tools: search and read Proton Mail, write drafts and tidy the mailbox through
[Proton Mail Bridge](https://proton.me/mail/bridge), with a preview you approve before mail is
moved, deleted or sent. It is unofficial, not affiliated with Proton AG.

The plugin is only the connector. Its one MCP server, `proton-mail`, runs
`uvx mcp-proton-mail@1.30.0 connect`, which relays the agent's requests to the proton-mail-mcp
service on `127.0.0.1` that `proton-mail-mcp setup` installed on the same computer.

## Before you install it

1. Install [uv](https://docs.astral.sh/uv/). It downloads the `mcp-proton-mail` package from PyPI,
   and Python 3.12 when there is none.
2. Run the guided setup once, in your own terminal, from a clone of the repository:
   `uvx --from . proton-mail-mcp setup`. It installs or checks Proton Mail Bridge, stores the
   Bridge password and a new bearer token in your system's credential store, and starts the
   service. A paid Proton plan is needed, because Bridge requires one.

Use either this plugin or the entry `setup --client claude-code` writes, not both, or the agent
sees every tool twice.

## Install

In Claude Code:

```text
/plugin marketplace add legalaspro/proton-mail-mcp
/plugin install mcp-proton-mail@proton-mail-mcp
```

From a clone, `/plugin marketplace add ./` adds the same marketplace from your own files.

## Network and credentials

- **Network:** the relay connects only to the proton-mail-mcp service on `127.0.0.1`, and the
  service to Bridge, on `127.0.0.1` unless `PROTON_BRIDGE_HOST` names another host with a verified
  certificate. uv downloads the package from PyPI. The one other connection is an unsubscribe link
  from a message, and only after you approve that unsubscribe.
- **Credentials:** none in the plugin. The relay reads the service's bearer token from the system
  credential store each time it starts and never prints it; on macOS, Keychain may ask once.

## Privacy

proton-mail-mcp runs on your computer and nothing is hosted; there is no telemetry. Sending is off
until you turn it on, and then each send needs a preview and your approval. On a client without
forms the approval is an OK you type in chat, which the server cannot tell apart from one the model
writes. What the agent reads goes to your agent client and its model provider. The
[privacy policy](https://github.com/legalaspro/proton-mail-mcp#privacy-policy) has the details,
including the setting that sends to your own addresses with no approval.

## License

[Apache-2.0](https://github.com/legalaspro/proton-mail-mcp/blob/main/LICENSE).
