"""`proton-mail-mcp connect`: stdio relayed to a local HTTP server, the token never shown."""

import io
import json
import os
import queue
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from proton_mail_mcp import cli, connect, credential_store
from proton_mail_mcp.connect import (
    NO_TOKEN,
    PARSE_ERROR,
    REFUSED_TOKEN,
    UNREACHABLE,
    Relay,
    TokenError,
)

# Taken before conftest's fixture blocks connections: these tests reach 127.0.0.1 only.
REAL_CONNECT = socket.socket.connect
TOKEN = "sentinel-token-" + "q" * 40
OLD_TOKEN = "sentinel-older-" + "w" * 40
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {
            "extensions": {
                "io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}
            }
        },
        "clientInfo": {"name": "Example Client", "version": "1.0"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
INIT_RESULT = {
    "protocolVersion": "2025-11-25",
    "capabilities": {"tools": {}},
    "serverInfo": {"name": "proton-mail-mcp", "version": "1.30.0"},
}
TOOL = {
    "name": "read_thread",
    "inputSchema": {"type": "object"},
    "_meta": {"ui": {"resourceUri": "ui://proton-mail/thread.html", "visibility": ["model"]}},
}


@pytest.fixture(autouse=True)
def loopback(forbid_network, monkeypatch):
    """http.client connects through socket.create_connection, here to 127.0.0.1 only."""

    def create_connection(address, timeout=None, source_address=None, **kwargs):
        host, port = address
        assert host == "127.0.0.1", host
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            REAL_CONNECT(sock, (host, port))
        except OSError:
            sock.close()
            raise
        return sock

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(connect, "_LISTEN_DELAY", 0.01)
    monkeypatch.setattr(connect, "_DRAIN_SECONDS", 5.0)


class Stdin:
    def __init__(self):
        self.lines = queue.Queue()

    def send(self, message):
        self.lines.put(json.dumps(message).encode("utf-8") + b"\n")

    def raw(self, data: bytes):
        self.lines.put(data)

    def close(self):
        self.lines.put(None)

    def __iter__(self):
        while (line := self.lines.get()) is not None:
            yield line


class Stdout:
    def __init__(self):
        self.raw = bytearray()
        self.lines = queue.Queue()

    def write(self, data: bytes):
        self.raw += data
        for line in data.splitlines():
            self.lines.put(json.loads(line))

    def flush(self):
        pass

    def next(self, timeout=10.0):
        return self.lines.get(timeout=timeout)

    def empty_for(self, seconds=0.3):
        try:
            extra = self.lines.get(timeout=seconds)
        except queue.Empty:
            return True
        pytest.fail(f"unexpected message {extra}")


class Fake:
    """A Streamable HTTP server whose answers each test scripts."""

    def __init__(self, answer):
        self.answer = answer
        self.seen = []
        self.events = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                fake.handle(self, "POST")

            def do_GET(self):
                fake.handle(self, "GET")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def event(self, name):
        return self.events.setdefault(name, threading.Event())

    def handle(self, handler, method):
        length = int(handler.headers.get("Content-Length") or 0)
        body = json.loads(handler.rfile.read(length)) if length else None
        self.seen.append((method, dict(handler.headers), body))
        self.answer(self, handler, method, handler.headers, body)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def reply_json(handler, status, message, headers=()):
    data = json.dumps(message).encode("utf-8") if message is not None else b""
    handler.send_response(status)
    if message is not None:
        handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    for name, value in headers:
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(data)


def reply_sse(handler, messages, headers=()):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    for name, value in headers:
        handler.send_header(name, value)
    handler.end_headers()
    for message in messages:
        if callable(message):
            message()
            continue
        # Data split over two lines, which a reader must join with a line break.
        text = json.dumps(message, indent=1)
        handler.wfile.write(b": keepalive\n")
        for line in text.split("\n"):
            handler.wfile.write(f"data: {line}\n".encode())
        handler.wfile.write(b"\n")
        handler.wfile.flush()


def result(request, value):
    return {"jsonrpc": "2.0", "id": request["id"], "result": value}


def stateless(fake, handler, method, headers, body):
    if headers.get("Authorization") != f"Bearer {TOKEN}":
        return reply_json(handler, 401, {"error": "Unauthorized"})
    if "id" not in body:
        return reply_json(handler, 202, None)
    if body["method"] == "initialize":
        return reply_json(handler, 200, result(body, INIT_RESULT))
    if body["method"] == "tools/list":
        return reply_json(handler, 200, result(body, {"tools": [TOOL]}))
    return reply_json(handler, 200, result(body, {"echo": body.get("params")}))


class Rig:
    def __init__(self, answer, *, read_token=lambda: TOKEN, port=None, **options):
        self.fake = Fake(answer) if answer else None
        self.stdin, self.stdout, self.stderr = Stdin(), Stdout(), io.StringIO()
        self.relay = Relay(
            port or self.fake.port,
            read_token,
            stdin=self.stdin,
            stdout=self.stdout,
            stderr=self.stderr,
            **options,
        )
        self.code = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self.code = self.relay.run()

    def ask(self, message, timeout=10.0):
        self.stdin.send(message)
        return self.stdout.next(timeout)

    def finish(self):
        self.stdin.close()
        self.thread.join(10)
        assert not self.thread.is_alive()
        if self.fake:
            self.fake.close()
        assert self.code == 0
        # The token never reaches the client's stdout or its logs.
        for secret in (TOKEN, OLD_TOKEN):
            assert secret.encode() not in bytes(self.stdout.raw)
            assert secret not in self.stderr.getvalue()


def test_json_answers_pass_unchanged_with_the_token_and_protocol_headers():
    rig = Rig(stateless)
    assert rig.ask(INIT) == result(INIT, INIT_RESULT)
    rig.stdin.send(INITIALIZED)
    listed = rig.ask({"jsonrpc": "2.0", "id": "two", "method": "tools/list"})
    # _meta and the ui:// link arrive as the server sent them.
    assert listed == {"jsonrpc": "2.0", "id": "two", "result": {"tools": [TOOL]}}
    rig.stdout.empty_for()
    rig.finish()
    posts = [(headers, body) for method, headers, body in rig.fake.seen if method == "POST"]
    assert [body for _headers, body in posts] == [
        INIT,
        INITIALIZED,
        {"jsonrpc": "2.0", "id": "two", "method": "tools/list"},
    ]
    first, _initialized, later = (headers for headers, _body in posts)
    assert first["Authorization"] == f"Bearer {TOKEN}"
    assert first["Accept"] == "application/json, text/event-stream"
    assert first["Content-Type"] == "application/json"
    assert "MCP-Protocol-Version" not in first and "Mcp-Session-Id" not in first
    assert later["MCP-Protocol-Version"] == "2025-11-25"
    # A stateless server gives no session, so no stream for server messages is opened.
    assert not [method for method, _h, _b in rig.fake.seen if method == "GET"]
    assert rig.stderr.getvalue() == ""


def stateful(fake, handler, method, headers, body):
    session = fake.__dict__.setdefault("session", "session-1")
    if method == "GET":
        fake.event("get").set()
        if len([seen for seen in fake.seen if seen[0] == "GET"]) > 1:
            return reply_json(handler, 405, None)
        assert headers["Mcp-Session-Id"] == session
        changed = {"jsonrpc": "2.0", "method": "notifications/resources/list_changed"}
        return reply_sse(handler, [changed])
    if body.get("method") == "initialize":
        return reply_sse(handler, [result(body, INIT_RESULT)], [("Mcp-Session-Id", session)])
    if headers.get("Mcp-Session-Id") != session:
        return reply_json(handler, 404, {"error": "no session"})
    if "method" not in body:
        fake.answers = [*getattr(fake, "answers", []), body]
        fake.event("answered").set()
        return reply_json(handler, 202, None)
    if "id" not in body:
        return reply_json(handler, 202, None)
    if body["method"] == "tools/call":
        elicit = {"jsonrpc": "2.0", "id": 70, "method": "elicitation/create", "params": {"x": 1}}
        return reply_sse(
            handler,
            [elicit, lambda: fake.event("answered").wait(10), result(body, {"sent": True})],
        )
    return reply_json(handler, 200, result(body, {"ok": True}))


def test_sse_carries_a_server_request_whose_answer_goes_back_mid_call():
    rig = Rig(stateful)
    assert rig.ask(INIT)["result"] == INIT_RESULT
    rig.stdin.send(INITIALIZED)
    # The GET stream brings what the server sends unasked.
    assert rig.stdout.next() == {"jsonrpc": "2.0", "method": "notifications/resources/list_changed"}
    rig.stdin.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "a"}})
    elicit = rig.stdout.next()
    assert elicit["method"] == "elicitation/create" and elicit["id"] == 70
    answer = {"jsonrpc": "2.0", "id": 70, "result": {"action": "accept"}}
    rig.stdin.send(answer)
    assert rig.stdout.next() == {"jsonrpc": "2.0", "id": 5, "result": {"sent": True}}
    rig.finish()
    assert rig.fake.answers == [answer]
    sessions = [headers.get("Mcp-Session-Id") for method, headers, _b in rig.fake.seen]
    assert sessions[0] is None and set(sessions[1:]) == {"session-1"}


def test_a_restarted_service_gets_the_same_initialize_again_and_the_call_is_retried():
    rig = Rig(stateful)
    assert rig.ask(INIT)["result"] == INIT_RESULT
    rig.stdin.send(INITIALIZED)
    rig.stdout.next()  # list_changed from the GET stream
    rig.fake.session = "session-2"
    assert rig.ask({"jsonrpc": "2.0", "id": 9, "method": "ping"}) == result({"id": 9}, {"ok": True})
    rig.stdout.empty_for()
    rig.finish()
    initializes = [
        body for _m, _h, body in rig.fake.seen if body and body.get("method") == "initialize"
    ]
    assert len(initializes) == 2
    replayed = initializes[1]
    assert replayed["params"] == INIT["params"] and replayed["id"] != INIT["id"]
    bodies = [body for method, _h, body in rig.fake.seen if method == "POST"]
    assert bodies[-2:] == [INITIALIZED, {"jsonrpc": "2.0", "id": 9, "method": "ping"}]


def test_a_refused_token_is_read_again_once():
    tokens = iter([OLD_TOKEN, TOKEN])
    rig = Rig(stateless, read_token=lambda: next(tokens))
    assert rig.ask(INIT) == result(INIT, INIT_RESULT)
    rig.finish()
    auth = [headers["Authorization"] for _m, headers, _b in rig.fake.seen]
    assert auth == [f"Bearer {OLD_TOKEN}", f"Bearer {TOKEN}"]


def test_a_stale_token_file_is_named_and_the_store_suggested():
    rig = Rig(stateless, read_token=lambda: OLD_TOKEN, token_file="/tmp/example-token")
    answer = rig.ask(INIT)
    rig.finish()
    assert answer["id"] == 1 and answer["error"]["code"] == REFUSED_TOKEN
    text = answer["error"]["message"]
    assert "refused the bearer token" in text
    assert "/tmp/example-token most likely holds an older token" in text
    assert "drop --token-file" in text
    assert "refused the bearer token" in rig.stderr.getvalue()


def test_a_service_that_is_not_running_is_said_for_each_request():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    rig = Rig(None, port=port)
    answer = rig.ask(INIT)
    assert answer["error"]["code"] == UNREACHABLE
    assert f"not running on 127.0.0.1:{port}" in answer["error"]["message"]
    rig.stdin.send(INITIALIZED)
    assert rig.ask({"jsonrpc": "2.0", "id": 2, "method": "ping"})["id"] == 2
    rig.finish()
    # Said once, not once per message.
    assert rig.stderr.getvalue().count("not running") == 1


def test_a_service_that_takes_the_connection_but_never_answers_initialize_is_said(monkeypatch):
    monkeypatch.setattr(connect, "_INITIALIZE_SECONDS", 0.3)
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()
    try:
        rig = Rig(None, port=silent.getsockname()[1])
        answer = rig.ask(INIT)
        rig.finish()
    finally:
        silent.close()
    assert answer["error"]["code"] == UNREACHABLE
    assert "took the connection but did not answer" in answer["error"]["message"]


def test_a_bad_line_gets_a_parse_error_and_the_relay_goes_on():
    rig = Rig(stateless)
    rig.stdin.raw(b"{not json\n")
    assert rig.stdout.next() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": PARSE_ERROR, "message": "Parse error: each line must be JSON"},
    }
    rig.stdin.raw(b"\xff\xfe\n")
    assert rig.stdout.next()["error"]["code"] == PARSE_ERROR
    rig.stdin.raw(b"\n")
    assert rig.ask(INIT) == result(INIT, INIT_RESULT)
    rig.finish()


def test_without_a_token_every_request_says_why_and_nothing_is_sent():
    def locked():
        raise TokenError("cannot read the MCP token from keychain: example reason")

    rig = Rig(stateless, read_token=locked)
    answer = rig.ask(INIT)
    assert answer["error"]["code"] == NO_TOKEN
    assert "example reason" in answer["error"]["message"]
    rig.stdin.send(INITIALIZED)
    rig.stdout.empty_for()
    rig.finish()
    assert rig.fake.seen == []
    assert rig.stderr.getvalue().count("example reason") == 1


def test_a_setting_problem_answers_every_request():
    rig = Rig(stateless, setting_problem="/x/proton-mail-mcp.env does not exist: run setup")
    assert "does not exist" in rig.ask(INIT)["error"]["message"]
    rig.finish()
    assert rig.fake.seen == []


def test_the_service_error_text_is_passed_on():
    def bad(fake, handler, method, headers, body):
        reply_json(
            handler,
            400,
            {
                "jsonrpc": "2.0",
                "id": "server-error",
                "error": {"code": -32600, "message": "Bad Request: Missing session ID"},
            },
        )

    rig = Rig(bad)
    answer = rig.ask({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    rig.finish()
    assert answer["id"] == 3
    assert (
        answer["error"]["message"]
        == "the service answered HTTP 400: Bad Request: Missing session ID"
    )


def write_env(tmp_path, text, mode=0o600):
    path = tmp_path / "proton-mail-mcp.env"
    path.write_text(f"# Managed by proton-mail-mcp installer\n{text}", encoding="utf-8")
    path.chmod(mode)
    return path


def args(**values):
    return SimpleNamespace(**{"env_file": None, "port": None, "token_file": None, **values})


def test_the_store_and_port_come_from_the_env_file(tmp_path, monkeypatch):
    path = write_env(
        tmp_path,
        "PROTON_BRIDGE_USER=user@example.test\nPROTON_MCP_PORT=9876\n"
        "PROTON_MCP_CREDENTIAL_STORE=pass\nPROTON_MCP_PASSWORD_STORE_DIR=/x/store\n",
    )
    calls = []
    monkeypatch.setattr(
        credential_store, "lookup", lambda *a: calls.append(a) or TOKEN, raising=True
    )
    port, read, problem = connect.configure(args(env_file=str(path)), {})
    assert (port, problem) == (9876, "")
    assert read() == TOKEN
    assert calls == [
        ("pass", "proton-mail-mcp-token", "user@example.test", {"PASSWORD_STORE_DIR": "/x/store"})
    ]
    port, _read, _problem = connect.configure(args(env_file=str(path), port=1234), {})
    assert port == 1234


def test_a_missing_env_file_says_to_run_setup(tmp_path):
    port, _read, problem = connect.configure(args(env_file=str(tmp_path / "none.env")), {})
    assert port == connect.DEFAULT_PORT
    assert "does not exist: run `proton-mail-mcp setup` first" in problem


def test_the_environment_token_wins_and_needs_no_env_file(tmp_path):
    port, read, problem = connect.configure(
        args(env_file=str(tmp_path / "none.env")), {"PROTON_MCP_TOKEN": TOKEN}
    )
    assert (port, problem, read()) == (connect.DEFAULT_PORT, "", TOKEN)


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_an_env_file_others_may_change_is_refused(tmp_path):
    path = write_env(tmp_path, "PROTON_MCP_PORT=9876\n", mode=0o622)
    _port, _read, problem = connect.configure(args(env_file=str(path)), {})
    assert "not writable by group or others" in problem


def test_a_bad_port_in_the_env_file_is_a_problem(tmp_path):
    path = write_env(tmp_path, "PROTON_BRIDGE_USER=user@example.test\nPROTON_MCP_PORT=http\n")
    _port, _read, problem = connect.configure(args(env_file=str(path)), {})
    assert "PROTON_MCP_PORT must be a port" in problem


def test_store_failures_say_what_to_do_without_the_account(tmp_path, monkeypatch):
    path = write_env(
        tmp_path, "PROTON_BRIDGE_USER=user@example.test\nPROTON_MCP_CREDENTIAL_STORE=libsecret\n"
    )

    def fail(*_args):
        raise credential_store.CredentialStoreError("store output")

    monkeypatch.setattr(credential_store, "lookup", fail)
    _port, read, _problem = connect.configure(args(env_file=str(path)), {})
    with pytest.raises(TokenError) as caught:
        read()
    assert "D-Bus session" in str(caught.value) and "--token-file" in str(caught.value)
    assert "user@example.test" not in str(caught.value) and "store output" not in str(caught.value)


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_a_token_file_must_be_private_and_is_read_without_its_line_break(tmp_path):
    path = tmp_path / "token"
    path.write_text(f"{TOKEN}\n", encoding="utf-8")
    path.chmod(0o644)
    _port, read, _problem = connect.configure(
        args(env_file=str(tmp_path / "none.env"), token_file=str(path)), {}
    )
    with pytest.raises(TokenError) as caught:
        read()
    assert "readable by you alone" in str(caught.value) and TOKEN not in str(caught.value)
    path.chmod(0o600)
    assert read() == TOKEN
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(TokenError, match="holds no token"):
        read()


@pytest.mark.skipif(os.name != "posix", reason="named pipes")
def test_a_token_file_that_is_no_regular_file_is_refused_without_waiting(tmp_path):
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    for path in (tmp_path, pipe):
        _port, read, _problem = connect.configure(
            args(env_file=str(tmp_path / "none.env"), token_file=str(path)), {}
        )
        with pytest.raises(TokenError, match="not a regular file"):
            read()


def test_a_token_that_is_no_header_value_is_never_sent():
    rig = Rig(stateless, read_token=lambda: "bad token\nvalue")
    answer = rig.ask(INIT)
    rig.finish()
    assert answer["error"]["code"] == NO_TOKEN
    assert "not plain text" in answer["error"]["message"]
    assert "bad token" not in rig.stderr.getvalue() + bytes(rig.stdout.raw).decode()
    assert rig.fake.seen == []


def test_the_command_goes_to_connect_and_never_loads_the_server(monkeypatch):
    calls = []
    monkeypatch.setattr(connect, "main", lambda argv: calls.append(argv) or 0)
    monkeypatch.setitem(sys.modules, "proton_mail_mcp.server", None)
    assert cli.main(["connect", "--port", "9"]) == 0
    assert calls == [["--port", "9"]]


def test_help_names_connect(capsys):
    assert cli.main(["--help"]) == 0
    assert "\n  connect " in capsys.readouterr().out


def test_connect_rejects_a_port_out_of_range(capsys):
    with pytest.raises(SystemExit) as caught:
        connect.main(["--port", "70000"])
    assert caught.value.code == 2
    assert "--port must be between 1 and 65535" in capsys.readouterr().err


@pytest.fixture
def real_server(tmp_path):
    """The real app over a real socket, as the service serves it."""
    uvicorn = pytest.importorskip("uvicorn")
    from proton_mail_mcp.config import Settings
    from proton_mail_mcp.server import BearerAuth, create_mcp

    servers = []

    def start(env):
        settings = Settings.from_env(
            {
                "PROTON_MCP_TOKEN": TOKEN,
                "PROTON_BRIDGE_USER": "user@example.test",
                "PROTON_MCP_ACTION_LOG": str(tmp_path / "actions.jsonl"),
                **env,
            }
        )
        mcp = create_mcp(settings, mail_client=object())
        app = BearerAuth(mcp.streamable_http_app(), settings.token)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        config = uvicorn.Config(
            app, access_log=False, log_level="warning", ws="none", timeout_graceful_shutdown=1
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while not server.started:
            assert time.monotonic() < deadline and thread.is_alive()
            time.sleep(0.02)
        servers.append((server, thread, sock))
        return sock.getsockname()[1], mcp

    yield start
    for server, thread, sock in servers:
        server.should_exit = True
        thread.join(10)
        sock.close()


@pytest.mark.parametrize(
    "env",
    [{"PROTON_MCP_CONFIRM_MUTATIONS": "0"}, {}, {"PROTON_MCP_ENABLE_SEND": "1"}],
    ids=["stateless", "default", "send"],
)
def test_views_and_their_metadata_reach_the_client_unchanged(real_server, env):
    import asyncio

    from proton_mail_mcp import mcp_apps

    port, mcp = real_server(env)
    rig = Rig(None, port=port)
    started = rig.ask(INIT)
    assert started["result"]["serverInfo"]["name"] == "proton-mail-mcp"
    rig.stdin.send(INITIALIZED)
    tools = rig.ask({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    # Every tool, with its _meta and view link, exactly as the server lists it.
    expected = [
        tool.model_dump(by_alias=True, mode="json", exclude_none=True)
        for tool in asyncio.run(mcp.list_tools())
    ]
    assert tools == expected
    assert any(
        tool.get("_meta", {}).get("ui", {}).get("resourceUri") == mcp_apps.THREAD_URI
        for tool in tools
    )
    listed = rig.ask({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})["result"]
    uris = {resource["uri"]: resource for resource in listed["resources"]}
    assert uris[mcp_apps.THREAD_URI]["mimeType"] == mcp_apps.MIME_TYPE
    read = rig.ask(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/read",
            "params": {"uri": mcp_apps.THREAD_URI},
        }
    )
    assert read["result"]["contents"][0]["text"] == mcp_apps.view_html("thread")
    rig.finish()
    # Stateless JSON without confirmations; otherwise a session, with answers over SSE.
    stateless = env.get("PROTON_MCP_CONFIRM_MUTATIONS") == "0"
    assert (rig.relay.session_id is None) is stateless
