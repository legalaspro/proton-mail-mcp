# Glama

[Glama](https://glama.ai/mcp/servers) lists MCP servers and checks each one by building it in a
container and starting it. These are the build settings that work for this server, as entered in
its Glama listing; [`glama.json`](../glama.json) names the maintainer.

## The build

Glama builds from source on `main`, not from the PyPI wheel. The wheel's server needs Bridge
credentials to start; `proton-mail-mcp introspect` answers `initialize` and `tools/list` with no
credentials or Bridge, and refuses every tool call.

| Setting | Value |
| --- | --- |
| Python | 3.12 |
| Pinned commit | Empty, so it builds `main` |
| Build steps | Below |
| Command | Below |

Build steps:

```json
[
  "python -m venv /opt/venv",
  "/opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check --constraint docker/constraints.txt ."
]
```

Command:

```json
["mcp-proxy", "--", "/opt/venv/bin/proton-mail-mcp", "introspect"]
```

- Plain `pip` is not on Glama's image, so the steps make a virtual environment and use its `pip`.
- [`docker/constraints.txt`](../docker/constraints.txt) pins each runtime package, as the
  repository's `Dockerfile` does ([Docker image](install.md#docker-image)).
- `scripts/docker_smoke.sh` checks the same `introspect` answers locally, from that `Dockerfile`.

## Releases

The maintainer, Dmitri Manajev, made the Glama release with these settings on 3 November 2026.
