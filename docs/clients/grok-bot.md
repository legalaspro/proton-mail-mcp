# Grok Bot and other agents

Grok Bot, or any agent that runs MCP servers from a JSON config on the same computer, starts
`proton-mail-mcp connect` as a stdio server. connect reads the bearer token itself when it
starts, so the agent's config holds no token. Setup does not know where such an agent keeps its
config, so it writes no file: it shows the entry for the agent to add.

## The entry

The paths are the macOS ones; on Linux and Windows they are those of your own install, as
[The connect command](../clients.md#the-connect-command) lists:

```json
{
  "mcpServers": {
    "proton-mail": {
      "command": "/Users/you/Library/Application Support/proton-mail-mcp/venv/bin/proton-mail-mcp",
      "args": [
        "connect",
        "--env-file",
        "/Users/you/Library/Application Support/proton-mail-mcp/proton-mail-mcp.env"
      ]
    }
  }
}
```

Key names vary between agents, so check yours; what it needs is the command and its arguments.

## Set it up

Install on the agent's own computer: that is the default, and the one this page covers. An agent
setting itself up follows [llms-install.md](../../llms-install.md), which hands the one step only
a person can do, the Bridge login, to that person.

```sh
uvx --from . proton-mail-mcp setup --client grok-bot
```

The plan ends with the entry above, with this computer's paths. With `--json` the same command and
arguments are in the document's `connector`, with the server's URL and no token. After setup,
`proton-mail-mcp configure-client --client grok-bot --print-config` prints the entry again and
changes nothing.

Add the entry to the agent's MCP servers, and restart it if it reads them only at start.

## Try it

Ask: "How many unread messages are in my inbox?" The agent should call `count_mail`, which reads
and changes nothing.

## Another computer

A connector that runs somewhere else, such as an agent's hosted service, cannot reach
`127.0.0.1` on this computer, and the server listens nowhere else. That is why connect runs
beside the agent instead. Serving the server to other devices over a tailnet, with
[Tailscale Serve](../install.md#expose-through-tailscale-serve), is a later, advanced option; it
needs the bearer token in the remote client and has not been set up for an agent.

## If it does not work

- Confirmation forms have not been checked with Grok Bot. Check them before relying on it: see
  [Forms and typed OK](../clients.md#forms-and-typed-ok).
- [Troubleshooting](../clients.md#troubleshooting) covers a service that is not running and a
  refused token.

How this was checked: the entry by tests, and connect against the real server in tests. Not yet
run with Grok Bot.
