# Connect an MCP client

The server speaks MCP over Streamable HTTP. Every client needs the same two things:

| Setting | Value |
| --- | --- |
| URL | `http://127.0.0.1:8765/mcp` |
| Header | `Authorization: Bearer <token>`, on every request |
| Session | Stateful with SSE: initialize a session first. Only with both `PROTON_MCP_ENABLE_SEND` and `PROTON_MCP_CONFIRM_MUTATIONS` at `0` is it stateless |

Replace `<token>` with the output of `show-token`, run with the same command you installed with:
`proton-mail-mcp show-token`, or until the first release
`uvx --from <wheel> proton-mail-mcp show-token` (see the note in
[Quick start](install.md#quick-start)). Keep it in your client's secret storage where it has one,
and out of version control and logs. A missing or wrong token returns HTTP `401`.

The client must run where it can reach that address, which means on the same computer as the
server. To reach the server from another device on your tailnet, use the HTTPS address from
[Tailscale Serve](install.md#expose-through-tailscale-serve) as the URL; the header stays the
same.

| Client | How it connects | How this page was checked |
| --- | --- | --- |
| [Claude Code](#claude-code) | Directly over HTTP | Connected to this server with Claude Code 2.1.302, on 1 Nov 2026. |
| [Claude Desktop](#claude-desktop) | Through a local stdio bridge | From Anthropic's and the bridge's docs; not run. |
| [Codex CLI, IDE extension and app](#codex-cli-ide-extension-and-app) | Directly over HTTP | Codex CLI 0.162.1 accepted this config, on 1 Nov 2026; tool calls not run. |
| [ChatGPT Mac app](#chatgpt-mac-app) | Directly over HTTP, with the same config as Codex | From OpenAI's docs; not run. |
| [Grok Bot and other clients](#grok-bot-and-other-clients) | Generic MCP over HTTP | The `curl` check ran against this server, on 1 Nov 2026; Grok Bot itself not run. |

Vendors change these settings often, so trust their current docs if they differ from this page.

## Claude Code

```sh
claude mcp add --transport http proton-mail http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer <token>"
claude mcp list
```

`claude mcp list` should report the server as connected. The command above leaves the token in
your shell history. To keep it out of files and history, put this in a project's `.mcp.json`
and set `PROTON_MCP_TOKEN` in your environment:

```json
{
  "mcpServers": {
    "proton-mail": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer ${PROTON_MCP_TOKEN}" }
    }
  }
}
```

## Claude Desktop

Claude Desktop runs local MCP servers as commands from its config file. Its custom connectors
connect from Anthropic's cloud, so they cannot reach `127.0.0.1`. Use a local stdio bridge such
as [`mcp-remote`](https://github.com/geelen/mcp-remote), a third-party package that needs
Node.js.

In Claude Desktop, open **Settings**, then **Developer**, then **Edit Config**. That opens
`claude_desktop_config.json`, at `~/Library/Application Support/Claude/` on macOS. Add:

```json
{
  "mcpServers": {
    "proton-mail": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://127.0.0.1:8765/mcp",
        "--header",
        "Authorization:${PROTON_MCP_AUTH}"
      ],
      "env": { "PROTON_MCP_AUTH": "Bearer <token>" }
    }
  }
}
```

Quit and reopen Claude Desktop. The token sits in this file, so keep the file private. There
are no spaces around the `:` in the header on purpose: `mcp-remote`'s docs give this form as
the workaround for clients that do not escape spaces in `args`.

## Codex CLI, IDE extension and app

These share one config, by default `~/.codex/config.toml`. Add the server with the CLI:

```sh
codex mcp add proton-mail --url http://127.0.0.1:8765/mcp \
  --bearer-token-env-var PROTON_MCP_TOKEN
```

Or edit the file directly:

```toml
[mcp_servers.proton-mail]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "PROTON_MCP_TOKEN"
tool_timeout_sec = 150
```

`PROTON_MCP_TOKEN` must be set in the environment Codex runs in. `tool_timeout_sec` matters here:
Codex's default is 60 seconds, shorter than this server's call budget. See
[Client timeouts](#client-timeouts).

## ChatGPT Mac app

OpenAI's docs say the ChatGPT desktop app, Codex CLI and IDE extension share the same
`config.toml`, so the [Codex config](#codex-cli-ide-extension-and-app) above also applies here.
In the app, open **Settings**, then **MCP servers**, then **Add server**. Enter a name, choose
**Streamable HTTP**, enter the URL, save, and select **Restart**.

An app you start from the Dock may not see variables exported in your shell. In that case, set
the header in `~/.codex/config.toml` instead, and keep the file private:

```toml
[mcp_servers.proton-mail]
url = "http://127.0.0.1:8765/mcp"
http_headers = { Authorization = "Bearer <token>" }
tool_timeout_sec = 150
```

This is for the desktop app on your Mac. OpenAI's docs say ChatGPT on the web does not read local
Codex configuration, and that local MCP servers may not be available in the cloud.

## Grok Bot and other clients

Grok Bot, like any client that speaks MCP over Streamable HTTP and can send a custom header,
needs only the URL and header from the top of this page. Many clients take a shape like this,
but key names vary, so check your client's docs:

```json
{
  "mcpServers": {
    "proton-mail": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

A client that runs in someone else's cloud cannot reach `127.0.0.1` on your computer.

Confirmation forms have not been verified on Grok Bot. Check them before relying on it: see
[Forms and typed OK](#forms-and-typed-ok).

To check the URL and token without any client, send `initialize` with `curl`. A working server
answers `200` with `serverInfo`; a wrong token gets `401`:

```sh
curl -sS -i http://127.0.0.1:8765/mcp \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl-check","version":"1"}}}'
```

The server keeps the sessions that forms need whenever sending or confirmation is on, which is
the default, so a client must initialize a session and read Server-Sent Events.

## Forms and typed OK

What a client declares in `initialize` decides how you approve changes:

| Your client | Mailbox changes | Sends, when enabled |
| --- | --- | --- |
| Declares `elicitation` and shows forms | Form | Form |
| Omits `elicitation` | Refused: `this client did not show a form` (audit outcome `confirmation_unavailable`) | [Typed OK](sending.md#typed-ok-when-the-client-has-no-form), unless `PROTON_MCP_SEND_TYPED_OK=0` refuses it |
| Declares `elicitation` but never shows the form | Refused | Refused; the operator can list it in `PROTON_MCP_SEND_TYPED_OK_CLIENTS` to get the typed OK |
| Listed in `PROTON_MCP_SEND_TYPED_OK_CLIENTS`, with the typed OK on | Refused at once | Typed OK |

A host that answers forms automatically defeats this check and is unsuitable. To let mailbox
changes run on a host without forms, the operator can deliberately set
[`PROTON_MCP_CONFIRM_MUTATIONS=0`](safety.md#confirmation-and-approval-binding); the token, which
passes through the assistant, then authorizes the change by itself.

To see what your client declares, read the service log after its first tool call. It records, at
INFO, a line such as:

```text
MCP client name "example-client", version "1.0", elicitation declared: True
```

`PROTON_MCP_SEND_TYPED_OK_CLIENTS` matches that name. It is self-reported and unauthenticated.

## Client timeouts

Keep your client's tool timeout above the server's budget. The server's default call budget is
120 seconds, and `undo_action` can use all of it, so use at least 150 seconds for that default.
A confirmation form may also stay unanswered for up to `PROTON_MCP_CONFIRM_TIMEOUT` (120 seconds
by default). If a client gives up first, the call may still be running: use `list_actions` to find
out what happened before you retry. See
[Call budgets](configuration.md#call-budgets-and-token-lifetimes) and
[Recovery](RECOVERY.md).
