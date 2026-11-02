#!/usr/bin/env bash
# Build the introspection image and prove what a registry such as Glama sees: over stdio,
# initialize, notifications/initialized and tools/list answer, and a tool call refuses. The
# container runs as a non-root user with no network, a read-only filesystem and no capabilities,
# so it reaches no Bridge and writes nothing. Needs docker and python3; it touches only its own
# image and container, and uploads nothing.
#
#   scripts/docker_smoke.sh [--no-build]
#
# IMAGE names the image (default proton-mail-mcp-introspect:smoke); TIMEOUT is the seconds to
# wait for each answer (default 60).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
IMAGE=${IMAGE:-proton-mail-mcp-introspect:smoke}
TIMEOUT=${TIMEOUT:-60}
CONTAINER="proton-mail-mcp-smoke-$$"

if [[ ${1:-} != --no-build ]]; then
    docker build --tag "$IMAGE" "$ROOT"
fi

user=$(docker run --rm --network none "$IMAGE" id -u)
if [[ $user == 0 ]]; then
    echo "FAIL: the image runs as root" >&2
    exit 1
fi
echo "runs as uid $user"

trap 'docker rm --force "$CONTAINER" >/dev/null 2>&1 || true' EXIT
coproc SERVER {
    exec docker run --rm --interactive --name "$CONTAINER" --network none --read-only \
        --cap-drop ALL --security-opt no-new-privileges "$IMAGE"
}
to_server=${SERVER[1]}
from_server=${SERVER[0]}

# One line per message, as MCP's stdio transport frames them; stdin stays open until each
# answer arrives, as a client's does.
send() {
    printf '%s\n' "$1" >&"$to_server"
}
receive() {
    if ! IFS= read -r -t "$TIMEOUT" "$1" <&"$from_server"; then
        echo "FAIL: no answer within ${TIMEOUT}s" >&2
        exit 1
    fi
}
initialized='' tools='' called=''

send '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"docker-smoke","version":"1"}}}'
receive initialized
send '{"jsonrpc":"2.0","method":"notifications/initialized"}'
send '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
receive tools
send '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_mail","arguments":{"text":"invoice"}}}'
receive called
eval "exec $to_server>&-"
wait "$SERVER_PID"

printf '%s\n' "$initialized" "$tools" "$called" | python3 -c '
import json
import sys

initialized, tools, called = (json.loads(line) for line in sys.stdin)
assert [initialized["id"], tools["id"], called["id"]] == [1, 2, 3], "answers out of order"
server = initialized["result"]["serverInfo"]
name, version = server["name"], server["version"]
protocol = initialized["result"]["protocolVersion"]
assert name == "proton-mail-mcp", server
print(f"initialize: {name} {version}, protocol {protocol}")
names = [tool["name"] for tool in tools["result"]["tools"]]
assert names, "tools/list returned no tools"
listed = ", ".join(names)
print(f"tools/list: {len(names)} tools: {listed}")
result = called["result"]
assert result["isError"] is True, result
refusal = result["content"][0]["text"]
print(f"tools/call search_mail refused: {refusal}")
print(f"PASS: {len(names)} tools listed")
'
