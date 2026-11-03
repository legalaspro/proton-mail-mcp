# ChatGPT Mac app

The ChatGPT desktop app reads its MCP servers from Codex's config, `~/.codex/config.toml`. It is
the one client setup connects directly over HTTP, so this entry holds the bearer token: in a live
test, ChatGPT rendered the MCP Apps thread view this way.

## The entry

Setup writes this table, with the real token where the placeholder is:

```toml
[mcp_servers.proton-mail]
url = "http://127.0.0.1:8765/mcp"
http_headers = { Authorization = "Bearer <token: run show-token --copy>" }
```

It goes in `~/.codex/config.toml` (on Windows `%USERPROFILE%\.codex\config.toml`), or in
`config.toml` under `$CODEX_HOME`. Because it holds the token, the file is written owner-only
(mode `0600`), and the previous file, kept as a dated `.bak`, may hold an earlier token.

## Set it up

In your own terminal:

```sh
uvx mcp-proton-mail setup --client chatgpt
```

After setup, `proton-mail-mcp configure-client --client chatgpt` adds it on its own, with your
launcher in front. It reads the token from the credential store only when it writes the file, and
never prints it. Every other table and key in the file stays; only the
`[mcp_servers.proton-mail]` table and its subtables are replaced, in place. Comments stay too,
except those inside the replaced table; comments at its end, such as a note above the next
table, stay. A file that defines `proton-mail` some other way, such as with dotted keys or an inline
table, is refused and left as it was, by `--dry-run` too.

Codex reads the same entry. `--client chatgpt --client codex` therefore writes only this one, and
a later `--client codex` keeps it. When the file holds a `proton-mail` entry that starts a
command, as `--client codex` writes it, ChatGPT's entry replaces it, and setup warns: Codex uses
the new one too, and the file then holds the token.

Then quit the ChatGPT app completely and open it again.

Setup's entry has no `tool_timeout_sec`. Codex's default is 60 seconds, shorter than the
server's call budget, so add `tool_timeout_sec = 150` to the table by hand (see
[Client timeouts](../clients.md#client-timeouts)). A later `configure-client` keeps it while the
rest of the entry is unchanged; one that replaces the entry keeps the old table in the dated
backup, so add it again then.

## Try it

Ask: "How many unread messages are in my inbox?" ChatGPT should call `count_mail`, which reads and
changes nothing.

## If it does not work

- **A new token.** `setup --replace-credentials` stores a new token; run
  `configure-client --client chatgpt` again, or the app gets `401`.
- ChatGPT on the web does not read this file, and a server in the cloud cannot reach `127.0.0.1`.
- [Troubleshooting](../clients.md#troubleshooting) has more.

How this was checked: on 2 Nov 2026 the ChatGPT Mac app, with this direct HTTP entry, called the
tools and rendered the thread view card. The file merge was checked by tests.
