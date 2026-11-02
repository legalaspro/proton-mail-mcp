"""`proton-mail-mcp connect`: a local stdio MCP server that relays to the installed service.

Clients that start local servers as commands run this instead of holding the bearer token.
It reads the token when it starts, from the credential store setup filled (or a token file),
adds it to each request to http://127.0.0.1:PORT/mcp, and relays every JSON-RPC message in
both directions as the JSON it is: client capabilities, _meta and ui:// resources pass
unchanged, so MCP Apps views reach a client that renders them. The token never reaches
stdout, stderr, argv or a message. stdout carries only the protocol; diagnostics go to
stderr, which clients keep in their logs, so it never names the account either.
"""

import argparse
import http.client
import json
import os
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, BinaryIO, TextIO

from . import credential_store, localfs
from .config import default_credential_store
from .service_entry import EnvFileError, read_env_file

PROG = "proton-mail-mcp connect"
HOST = "127.0.0.1"
PATH = "/mcp"
DEFAULT_PORT = 8765
_CONNECT_SECONDS = 10.0
# A tool call may run for minutes before a JSON response's headers arrive.
_READ_SECONDS = 30 * 60.0
# initialize does no mail work, and the client waits on it before anything else.
_INITIALIZE_SECONDS = 60.0
_MAX_TOKEN_FILE_BYTES = 4096
# Reconnects of the stream for server-initiated messages, and the wait between them.
_LISTEN_ATTEMPTS = 3
_LISTEN_DELAY = 1.0
# How long a closed stdin waits for answers still on their way.
_DRAIN_SECONDS = 10.0
_JSON = "application/json"
_SSE = "text/event-stream"
# JSON-RPC codes: the standard parse error, and this relay's own, in the server range.
PARSE_ERROR = -32700
UNREACHABLE = -32000
REFUSED_TOKEN = -32001
NO_TOKEN = -32002
BAD_ANSWER = -32003
# The id of an initialize this relay replays after the service restarted; never relayed.
_REPLAY_ID = "proton-mail-mcp-connect-replay"


class TokenError(RuntimeError):
    """The token cannot be had; the text says why and what to do, never the value."""


class _Unreachable(Exception):
    pass


class _Unanswered(_Unreachable):
    """The service took the connection, then said nothing in time."""


def default_env_file(environ: dict[str, str]) -> str:
    """The env file setup writes on this computer, for this user."""
    from .installer.detect import detect_host
    from .installer.plan import paths_for

    return paths_for(detect_host(), dict(environ), os.path.expanduser("~")).env_file


def read_token_file(path: str) -> str:
    """The token in an owner-only regular file, without the line break an editor adds. Opening
    never waits, as it would on a named pipe with no writer."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise TokenError(f"cannot read the token file {path}") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise TokenError(f"the token file {path} is not a regular file")
        refusal = localfs.acl_refusal(descriptor)
        if not localfs.owned(info) or not localfs.mode_private(info) or refusal:
            raise TokenError(
                f"the token file {path} must be yours and readable by you alone "
                f"(`chmod 600 {path}`)" + (f": it {refusal}" if refusal else "")
            )
        data = b""
        while chunk := os.read(descriptor, _MAX_TOKEN_FILE_BYTES + 1 - len(data)):
            data += chunk
            if len(data) > _MAX_TOKEN_FILE_BYTES:
                break
    except OSError:
        raise TokenError(f"cannot read the token file {path}") from None
    finally:
        os.close(descriptor)
    try:
        token = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        token = ""
    if len(data) > _MAX_TOKEN_FILE_BYTES or not token:
        raise TokenError(f"the token file {path} holds no token")
    return token


def token_reader(
    settings: dict[str, str], token_file: str | None, environ: dict[str, str]
) -> Callable[[], str]:
    """Where the token comes from: PROTON_MCP_TOKEN, then --token-file, then the store."""
    if environ.get("PROTON_MCP_TOKEN"):
        value = environ["PROTON_MCP_TOKEN"]
        return lambda: value
    if token_file:
        return lambda: read_token_file(token_file)
    store = settings.get("PROTON_MCP_CREDENTIAL_STORE") or default_credential_store()
    account = settings.get("PROTON_BRIDGE_USER", "")
    locations = credential_store.pass_locations(settings)

    def read() -> str:
        if not account:
            raise TokenError(
                "the env file names no Bridge account (PROTON_BRIDGE_USER): rerun "
                "`proton-mail-mcp setup`"
            )
        try:
            token = credential_store.lookup(
                store, credential_store.TOKEN_SERVICE, account, locations
            )
        except credential_store.CredentialStoreError:
            raise TokenError(_store_hint(store)) from None
        if not token:
            raise TokenError(
                f"the MCP token in {store} is empty: rerun `proton-mail-mcp setup` with "
                "--replace-credentials"
            )
        return token

    return read


def _store_hint(store: str) -> str:
    text = f"cannot read the MCP token from {store}"
    if store == "keychain":
        return (
            f"{text}: the login Keychain may be locked, or access was denied (choose Always "
            "Allow when macOS asks). If setup never ran here, run `proton-mail-mcp setup`"
        )
    if store == "libsecret":
        return (
            f"{text}: secret-tool needs an unlocked Secret Service (such as gnome-keyring) on "
            "this D-Bus session; a client started from another session sees none. Unlock it, "
            "use --token-file with an owner-only file, or run `proton-mail-mcp setup` first"
        )
    return f"{text}: unlock it, or run `proton-mail-mcp setup` first, which stores the token"


def _usable(token: str) -> bool:
    """An HTTP header value; http.client would quote a bad one in its error."""
    return token.isascii() and token.isprintable() and " " not in token


def _request_ids(message: Any) -> list[Any]:
    """The ids of the requests in a message or batch, which each need an answer."""
    items = message if isinstance(message, list) else [message]
    return [
        item["id"]
        for item in items
        if isinstance(item, dict) and "method" in item and item.get("id") is not None
    ]


def _method(message: Any) -> str:
    return message.get("method", "") if isinstance(message, dict) else ""


def events(response: Any) -> Iterator[tuple[str, str]]:
    """(event, data) from a text/event-stream, one per blank-line-ended event."""
    event, data = "message", []
    while True:
        raw = response.readline()
        if not raw:
            if data:
                yield event, "\n".join(data)
            return
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue
        field, _sep, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value or "message"
        elif field == "data":
            data.append(value)


class Relay:
    """Relay newline-delimited JSON-RPC between stdio and the service's Streamable HTTP."""

    def __init__(
        self,
        port: int,
        read_token: Callable[[], str],
        *,
        stdin: BinaryIO,
        stdout: BinaryIO,
        stderr: TextIO,
        token_file: str | None = None,
        from_environment: bool = False,
        setting_problem: str = "",
        connect: Callable[..., http.client.HTTPConnection] = http.client.HTTPConnection,
    ) -> None:
        self.port = port
        self.read_token = read_token
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.token_file = token_file
        self.from_environment = from_environment
        # A setting that keeps every request from reaching the service, said for each.
        self.setting_problem = setting_problem
        # Why the last token read failed, said for each request until one succeeds.
        self.token_problem = ""
        self.connect = connect
        self._token: str | None = None
        self._token_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._warn_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._replay_lock = threading.Lock()
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._initialize: dict[str, Any] | None = None
        self._initialized = False
        # Each new session gets its own listener; an older one sees the change and ends.
        self._generation = 0
        self._closed = threading.Event()
        self._workers: list[threading.Thread] = []
        self._last_warning = ""

    # stdio ---------------------------------------------------------------------

    def run(self) -> int:
        # Said once at the start, where the client's log shows it next to the launch.
        if self.setting_problem:
            self.warn(self.setting_problem)
        elif self.token() is None:
            self.warn(self.token_problem)
        for raw in self.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self.write(_error(None, PARSE_ERROR, "Parse error: each line must be JSON"))
                continue
            if not isinstance(message, (dict, list)):
                self.write(_error(None, PARSE_ERROR, "Parse error: expected a JSON-RPC object"))
                continue
            self.dispatch(message)
        self._closed.set()
        deadline = time.monotonic() + _DRAIN_SECONDS
        for worker in self._workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        return 0

    def dispatch(self, message: Any) -> None:
        """initialize, notifications and answers go in order; each other request in a
        thread of its own, so a long tool call never holds up an answer the server awaits."""
        method = _method(message)
        if method == "initialize":
            with self._state_lock:
                self._initialize = message
                self._initialized = False
                self.session_id = None
                self.protocol_version = None
                self._generation += 1
            self._guarded(message)
        elif _request_ids(message):
            self._workers = [worker for worker in self._workers if worker.is_alive()]
            worker = threading.Thread(target=self._guarded, args=(message,), daemon=True)
            self._workers.append(worker)
            worker.start()
        else:
            delivered = self._guarded(message)
            if method == "notifications/initialized" and delivered:
                with self._state_lock:
                    self._initialized = True
                    generation = self._generation
                    listen = self.session_id is not None
                if listen:
                    self._start_listener(generation)

    def write(self, message: Any) -> None:
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                self.stdout.write(data.encode("utf-8") + b"\n")
                self.stdout.flush()
            except (OSError, ValueError):
                # The client closed its end: nobody is left to answer.
                self._closed.set()

    def warn(self, text: str) -> None:
        """One stderr line per new problem; the same one again stays quiet."""
        with self._warn_lock:
            if text != self._last_warning:
                self._last_warning = text
                print(f"{PROG}: {text}", file=self.stderr, flush=True)

    # HTTP ----------------------------------------------------------------------

    def token(self, *, again: bool = False) -> str | None:
        """The token, read once; a failed read is tried again on the next request, so an
        unlocked store or a finished setup helps without restarting the client."""
        with self._token_lock:
            if self._token is not None and not again:
                return self._token
            try:
                token = self.read_token()
            except TokenError as error:
                self.token_problem = f"no MCP token: {error}"
                return None
            if not _usable(token):
                self.token_problem = (
                    "no MCP token: the stored one is not plain text; rerun `proton-mail-mcp "
                    "setup` with --replace-credentials"
                )
                return None
            self._token = token
            self.token_problem = ""
            return token

    def _headers(self, token: str, session: str | None, version: str | None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token}", "Accept": f"{_JSON}, {_SSE}"}
        if session:
            headers["Mcp-Session-Id"] = session
        if version:
            headers["MCP-Protocol-Version"] = version
        return headers

    def _open(
        self,
        method: str,
        body: bytes | None,
        headers: dict[str, str],
        wait: float = _READ_SECONDS,
    ):
        link = self.connect(HOST, self.port, timeout=_CONNECT_SECONDS)
        try:
            link.connect()
        except (OSError, http.client.HTTPException):
            link.close()
            raise _Unreachable from None
        try:
            link.sock.settimeout(wait)
            if body is not None:
                headers = {**headers, "Content-Type": _JSON}
            link.request(method, PATH, body=body, headers=headers)
            return link, link.getresponse()
        except TimeoutError:
            link.close()
            raise _Unanswered from None
        except (OSError, http.client.HTTPException):
            link.close()
            raise _Unreachable from None

    def _guarded(self, message: Any) -> bool:
        """Send one message; whatever goes wrong becomes an answer, never a crash."""
        try:
            return self.send(message, self.write)
        except Exception as error:  # The loop must survive any one message.
            # Only the type: a message from http.client could quote a header.
            self.warn(f"relaying failed ({type(error).__name__})")
            self._fail(message, BAD_ANSWER, "connect could not relay this message; retry it")
            return False

    def send(
        self, message: Any, sink: Callable[[Any], None], *, retry: bool = True, fresh: bool = True
    ) -> bool:
        """POST one client message and pass each message the server answers to sink.
        True when the service took it."""
        if self.setting_problem:
            self._fail(message, NO_TOKEN, self.setting_problem, sink)
            return False
        token = self.token()
        if token is None:
            self.warn(self.token_problem)
            self._fail(message, NO_TOKEN, self.token_problem, sink)
            return False
        with self._state_lock:
            session, version = self.session_id, self.protocol_version
        initialize = _method(message) == "initialize"
        if initialize:
            session = version = None
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        wait = _INITIALIZE_SECONDS if initialize else _READ_SECONDS
        try:
            headers = self._headers(token, session, version)
            link, response = self._open("POST", body, headers, wait)
        except _Unanswered:
            text = (
                f"proton-mail-mcp on {HOST}:{self.port} took the connection but did not answer "
                f"within {wait:.0f} seconds. Restart its service; its log says why"
            )
            self.warn(text)
            self._fail(message, UNREACHABLE, text, sink)
            return False
        except _Unreachable:
            self._fail(message, UNREACHABLE, self._unreachable(), sink)
            return False
        try:
            status = response.status
            if status == 401:
                response.read()
                if fresh and self.token(again=True) is not None:
                    return self.send(message, sink, retry=retry, fresh=False)
                self._fail(message, REFUSED_TOKEN, self._refused(), sink)
                return False
            if status == 404 and session and retry and not initialize:
                response.read()
                if self._replay(session):
                    return self.send(message, sink, retry=False, fresh=fresh)
                self._fail(
                    message,
                    UNREACHABLE,
                    "the proton-mail-mcp service restarted and did not take a new session; "
                    "restart the client",
                    sink,
                )
                return False
            if status not in {200, 202}:
                self._fail(message, BAD_ANSWER, self._http_error(response), sink)
                return False
            if initialize:
                with self._state_lock:
                    self.session_id = response.getheader("mcp-session-id") or None
            pending = _request_ids(message)
            if status == 202 or not pending:
                response.read()
                return True
            kind = (response.getheader("Content-Type") or "").lower()
            if kind.startswith(_JSON):
                self._relay(response.read().decode("utf-8", "replace"), sink, pending, initialize)
            elif kind.startswith(_SSE):
                for event, data in events(response):
                    if event == "message" and data:
                        self._relay(data, sink, pending, initialize)
                    if not pending:
                        break
            for left in pending:
                sink(_error(left, BAD_ANSWER, "the service ended its answer early; retry"))
            return True
        finally:
            link.close()

    def _relay(self, text: str, sink: Callable[[Any], None], pending: list, initialize: bool):
        try:
            data = json.loads(text)
        except ValueError:
            self.warn("the service sent a message that is not JSON; it was dropped")
            return
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            if "method" not in item and item.get("id") in pending:
                pending.remove(item["id"])
                result = item.get("result")
                if initialize and isinstance(result, dict):
                    version = result.get("protocolVersion")
                    with self._state_lock:
                        self.protocol_version = version if isinstance(version, str) else None
            sink(item)

    def _fail(
        self, message: Any, code: int, text: str, sink: Callable[[Any], None] | None = None
    ) -> None:
        ids = _request_ids(message)
        if not ids:
            # A notification has no answer to carry the reason; stderr says it, once.
            self.warn(text)
        for request_id in ids:
            (sink or self.write)(_error(request_id, code, text))

    def _unreachable(self) -> str:
        text = (
            f"proton-mail-mcp is not running on {HOST}:{self.port}. Start its service, or rerun "
            "`proton-mail-mcp setup`, whose self-check says what is wrong"
        )
        self.warn(text)
        return text

    def _refused(self) -> str:
        if self.from_environment:
            how = (
                " PROTON_MCP_TOKEN in connect's environment is not the service's token: remove "
                "it so connect reads the credential store"
            )
        elif self.token_file:
            how = (
                f" The token file {self.token_file} most likely holds an older token than the "
                "one the service uses: delete it, or drop --token-file so connect reads the "
                "current token from the credential store"
            )
        else:
            how = (
                " The credential store holds a token the running service does not use: restart "
                "the service, or rerun `proton-mail-mcp setup`"
            )
        text = "the proton-mail-mcp service refused the bearer token." + how
        self.warn(text)
        return text

    def _http_error(self, response: Any) -> str:
        status = response.status
        try:
            body = json.loads(response.read(64 * 1024).decode("utf-8"))
            said = body["error"]["message"]
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
            said = ""
        text = f"the service answered HTTP {status}"
        return f"{text}: {said}" if isinstance(said, str) and said else text

    def _replay(self, stale: str) -> bool:
        """After the service restarted, start a new session as the client did, once."""
        with self._replay_lock:
            with self._state_lock:
                if self.session_id != stale:
                    return self.session_id is not None
                message, initialized = self._initialize, self._initialized
                self.session_id = self.protocol_version = None
                self._generation += 1
                generation = self._generation
            if message is None:
                return False
            answers: list[Any] = []
            replayed = {**message, "id": _REPLAY_ID}
            if not self.send(replayed, answers.append, retry=False) or not any(
                isinstance(item, dict) and "result" in item for item in answers
            ):
                return False
            if initialized:
                self.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, _drop)
            with self._state_lock:
                restarted = self.session_id is not None
            if restarted and initialized:
                self._start_listener(generation)
            return restarted

    # Server-initiated messages -----------------------------------------------------

    def _start_listener(self, generation: int) -> None:
        threading.Thread(target=self._listen, args=(generation,), daemon=True).start()

    def _listen(self, generation: int) -> None:
        """The GET stream that carries what the server sends unasked; 405 means none."""
        attempts = 0
        while attempts < _LISTEN_ATTEMPTS and not self._closed.is_set():
            with self._state_lock:
                if generation != self._generation or not self.session_id:
                    return
                session, version = self.session_id, self.protocol_version
            token = self.token()
            if token is None:
                return
            headers = {**self._headers(token, session, version), "Accept": _SSE}
            try:
                link, response = self._open("GET", None, headers)
            except _Unreachable:
                attempts += 1
            else:
                try:
                    kind = (response.getheader("Content-Type") or "").lower()
                    if response.status in {404, 405}:
                        return
                    if response.status == 200 and kind.startswith(_SSE):
                        attempts = 0
                        for event, data in events(response):
                            if self._closed.is_set():
                                return
                            if event == "message" and data:
                                self._relay(data, self.write, [], False)
                    else:
                        attempts += 1
                except (OSError, http.client.HTTPException):
                    attempts += 1
                finally:
                    link.close()
            self._closed.wait(_LISTEN_DELAY * max(1, attempts))


def _drop(_message: Any) -> None:
    pass


def _error(request_id: Any, code: int, text: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": text}}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Serve MCP on stdin and stdout for a client that starts local servers as commands, "
            "relaying to the proton-mail-mcp service with the stored token, which is never "
            "printed."
        ),
    )
    parser.add_argument(
        "--env-file", help="the settings setup wrote (default: the one on this computer)"
    )
    parser.add_argument(
        "--port", type=int, help="the service's port (default: PROTON_MCP_PORT, else 8765)"
    )
    parser.add_argument(
        "--token-file",
        help="read the bearer token from this owner-only file instead of the credential store",
    )
    return parser


def configure(
    args: argparse.Namespace, environ: dict[str, str]
) -> tuple[int, Callable[[], str], str]:
    """The port, the token reader and any problem that stops every request. The env file is
    read only when something comes from it, and only if it is the user's own: the port it
    names is where the token goes."""
    from_store = not (environ.get("PROTON_MCP_TOKEN") or args.token_file)
    settings: dict[str, str] = {}
    problem = ""
    path = args.env_file or default_env_file(environ)
    if args.port is None or from_store:
        if os.path.exists(path):
            try:
                settings = read_env_file(path)
            except EnvFileError as error:
                problem = f"{path}: {error}"
        elif from_store:
            problem = (
                f"{path} does not exist: run `proton-mail-mcp setup` first, which writes it "
                "and stores the token"
            )
    port = args.port
    saved = settings.get("PROTON_MCP_PORT", "")
    if port is None and saved:
        if saved.isdigit() and 1 <= int(saved) <= 65535:
            port = int(saved)
        else:
            problem = problem or f"{path}: PROTON_MCP_PORT must be a port from 1 to 65535"
    return port or DEFAULT_PORT, token_reader(settings, args.token_file, environ), problem


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    environ = dict(os.environ)
    port, read_token, problem = configure(args, environ)
    relay = Relay(
        port,
        read_token,
        stdin=sys.stdin.buffer,
        stdout=sys.stdout.buffer,
        stderr=sys.stderr,
        token_file=args.token_file,
        from_environment=bool(environ.get("PROTON_MCP_TOKEN")),
        setting_problem=problem,
    )
    try:
        return relay.run()
    except KeyboardInterrupt:
        return 130
