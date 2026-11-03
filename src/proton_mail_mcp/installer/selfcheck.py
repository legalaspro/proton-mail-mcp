"""Prove a running MCP service answers and reaches Bridge; standard library only.

The token goes only into the Authorization header of a request to 127.0.0.1, which
follows no redirect and no proxy. Errors name status codes, never values, and nothing
the server says about the mailbox is kept or shown: a tool error is shown only when it is
one of the server's fixed texts about Bridge.
"""

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .. import bridge_password

SERVER_NAME = "proton-mail-mcp"
# One cheap read-only tool: LIST and EXAMINE, with no header or body fetched.
PROBE_TOOL = "count_mail"
PROBE_ARGUMENTS = {"folder": "INBOX"}
_PROTOCOL = "2025-06-18"
_TIMEOUT = 40.0
_MAX_REPLY_BYTES = 1024 * 1024
# Bridge can still be starting when its port first opens.
_BRIDGE_ATTEMPTS = 4
_BRIDGE_PAUSE = 5.0
# FastMCP's lead before a tool error's own text.
_TOOL_ERROR = f"Error executing tool {PROBE_TOOL}: "
# The Bridge address in the server's not-running and STARTTLS texts.
_ADDRESS = re.compile(
    r" on (?:\[(?P<ip6>[0-9A-Fa-f:.]+)\]|(?P<host>[A-Za-z0-9.-]+)):(?P<port>\d{1,5})[.:]"
)


class SelfCheckError(RuntimeError):
    """The named check failed; the text holds no secret and no mail data."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect would carry the Authorization header away from this host.
        return None


def _default_open(request: urllib.request.Request, timeout: float):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())
    return opener.open(request, timeout=timeout)


class _Client:
    """A minimal MCP Streamable HTTP client: JSON replies and SSE reply streams."""

    def __init__(self, port: int, token: str | None, open_url: Callable) -> None:
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.token = token
        self.open_url = open_url
        self.session: str | None = None
        self.protocol: str | None = None
        self.next_id = 0

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.protocol:
            headers["Mcp-Protocol-Version"] = self.protocol
        return headers

    def send(self, body: dict[str, Any]) -> tuple[int, dict[str, Any] | None, Any]:
        """(status, the JSON-RPC reply if any, response headers); never raises for a status."""
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.url, data, self._headers(), method="POST")
        try:
            with self.open_url(request, _TIMEOUT) as response:
                reply = _reply(response, body.get("id"))
                return response.status, reply, response.headers
        except urllib.error.HTTPError as error:
            error.close()
            return error.code, None, error.headers
        except (OSError, ValueError, http.client.HTTPException):
            raise SelfCheckError(f"no usable answer from {self.url}") from None

    def call(self, method: str, params: dict[str, Any]) -> tuple[int, dict[str, Any] | None, Any]:
        self.next_id += 1
        return self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})


def _reply(response: Any, request_id: Any) -> dict[str, Any] | None:
    """The reply to one request, from a JSON body or the first match on an SSE stream.

    Stops reading at that match, so a stream the server keeps open cannot hold it up.
    """
    if request_id is None:
        return None
    kind = response.headers.get("Content-Type", "") or ""
    if "text/event-stream" not in kind:
        data = response.read(_MAX_REPLY_BYTES + 1)
        if len(data) > _MAX_REPLY_BYTES:
            raise ValueError("reply too large")
        message = json.loads(data) if data.strip() else None
        return message if isinstance(message, dict) else None
    seen = 0
    for line in response:
        seen += len(line)
        if seen > _MAX_REPLY_BYTES:
            raise ValueError("reply too large")
        if not line.startswith(b"data:"):
            continue
        message = json.loads(line[5:])
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    return None


def _initialize_params() -> dict[str, Any]:
    return {
        "protocolVersion": _PROTOCOL,
        "capabilities": {},
        "clientInfo": {"name": "proton-mail-mcp-setup", "version": "1"},
    }


def _tool_result(reply: dict[str, Any] | None) -> dict[str, Any] | None:
    """The tool's dict result; None for an MCP error, a tool error or an unreadable one."""
    result = (reply or {}).get("result")
    if not isinstance(result, dict) or result.get("isError"):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    try:
        parsed = json.loads(result["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _bridge_problem(reply: dict[str, Any] | None, store: str | None) -> tuple[str | None, bool]:
    """What the server's tool error says about Bridge, and whether no retry can change it.

    The server's words are shown only when they are one of its fixed texts for this store or
    the Bridge address they name; otherwise only their lead is compared.
    """
    result = (reply or {}).get("result")
    try:
        text = result["content"][0]["text"] if result.get("isError") else ""
    except (AttributeError, KeyError, IndexError, TypeError):
        return None, False
    if not isinstance(text, str):
        return None, False
    text = text.removeprefix(_TOOL_ERROR)
    cannot = "the MCP server runs, but it cannot read INBOX."
    if text == bridge_password.missing(store, empty=True):
        # The store answered, with an empty entry: waiting changes nothing. A locked store
        # can be unlocked at its prompt meanwhile, and a Bridge still loading the account
        # refuses its login, so those are asked again.
        return f"{cannot} {text}", True
    if text in (bridge_password.missing(store), bridge_password.rejected(store)):
        return f"{cannot} {text}", False
    found = _ADDRESS.search(text)
    if found:
        host, port = found["ip6"] or found["host"], int(found["port"])
        if text == bridge_password.not_running(host, port):
            return bridge_password.logged_not_running(host, port), False
        if text == bridge_password.tls_failed(host, port):
            return f"{cannot} {text}", False
    # Another version's words: rebuilt from this one's fixed text.
    if bridge_password.REJECTED in text:
        return f"{cannot} {bridge_password.rejected(store)}", False
    if bridge_password.AUTH_FAILED in text:
        return f"{cannot} {bridge_password.missing(store)}", False
    return None, False


def check_service(
    port: int,
    token: str,
    say: Callable[[str], None],
    *,
    store: str | None = None,
    open_url: Callable = _default_open,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Raise SelfCheckError at the first check that fails.

    Without the token the server must answer 401, so it enforces it; with the stored
    token it must initialize as this server; then one read-only tool must read INBOX
    through Bridge, which proves the Bridge login and the stored password. `store` is the
    credential store setup used, which the server's texts about the password name.
    """
    status, _reply_body, _headers = _Client(port, None, open_url).call(
        "initialize", _initialize_params()
    )
    if status != 401:
        raise SelfCheckError(
            f"127.0.0.1:{port} answered {status} without the bearer token, so it is not this "
            "MCP server enforcing its token"
        )
    client = _Client(port, token, open_url)
    status, reply, headers = client.call("initialize", _initialize_params())
    if status == 401:
        raise SelfCheckError(
            "the MCP server refused the stored token, so it runs with another one; "
            "restart the service"
        )
    result = (reply or {}).get("result")
    info = result.get("serverInfo") if isinstance(result, dict) else None
    if status != 200 or not isinstance(info, dict) or info.get("name") != SERVER_NAME:
        raise SelfCheckError(f"127.0.0.1:{port} did not initialize as {SERVER_NAME} ({status})")
    client.session = headers.get("Mcp-Session-Id")
    protocol = result.get("protocolVersion")
    client.protocol = protocol if isinstance(protocol, str) else None
    client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    say(f"    ok: the MCP server on 127.0.0.1:{port} enforces and accepts the stored token")
    problem = None
    for attempt in range(_BRIDGE_ATTEMPTS):
        if attempt:
            sleep(_BRIDGE_PAUSE)
        status, reply, _headers = client.call(
            "tools/call", {"name": PROBE_TOOL, "arguments": PROBE_ARGUMENTS}
        )
        found = _tool_result(reply) if status == 200 else None
        if found is not None and not found.get("partial") and not found.get("timed_out"):
            say("    ok: the MCP server reads INBOX through Bridge with the stored password")
            return
        problem, final = _bridge_problem(reply, store) if status == 200 else (None, False)
        if final:
            break
    if problem:
        raise SelfCheckError(problem)
    raise SelfCheckError(
        "the MCP server runs, but it cannot read INBOX through Bridge: Bridge is not "
        "running or not signed in, or the Bridge username or password is wrong"
    )
