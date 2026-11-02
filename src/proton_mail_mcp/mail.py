"""Read mail, append drafts, and expose the guarded cleanup coordinator."""

from __future__ import annotations

import base64
import hashlib
import imaplib
import quopri
import re
import socket
import ssl
import threading
import time
import unicodedata
from contextlib import contextmanager, suppress
from datetime import date, datetime
from email import policy
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, getaddresses, make_msgid
from html.parser import HTMLParser
from typing import Any, Callable, Iterator

from .security import (
    CredentialError,
    bridge_endpoint,
    get_credentials,
    tls_context,
    verify_certificate,
)

MAX_SEARCH_RESULTS = 100
MAX_SNIPPET_BYTES = 4096
MAX_THREAD_MESSAGES = 100
MAX_THREAD_IDS = 200
MAX_THREAD_FOLDERS = 100
MAX_THREAD_CANDIDATES = 1000
MAX_MESSAGE_ID_BYTES = 4096
# The largest draft that guarded send reads, and so the largest with attachments.
MAX_DRAFT_BYTES = 10 * 1024 * 1024
_ID = re.compile(r"<[^<>\s]+>")
_IMAP_QUOTED = re.compile(rb'([^"\\]*(?:\\.[^"\\]*)*)"', re.DOTALL)
_IMAP_SPACE = re.compile(rb"[ \r\n\t]*")
_IMAP_ATOM = re.compile(rb"[^ ()\r\n\t]*")
_IMAP_ESCAPE = re.compile(
    rb"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|[0-7]{3}|.)", re.DOTALL
)
_IMAP_ESCAPES = {
    b'"': b'"',
    b"\\": b"\\",
    b"a": b"\a",
    b"b": b"\b",
    b"f": b"\f",
    b"n": b"\n",
    b"r": b"\r",
    b"t": b"\t",
    b"v": b"\v",
}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class MailError(RuntimeError):
    """A mailbox failure whose message contains no server or credential details."""


class MailValidationError(ValueError):
    """Invalid tool input with a fixed message safe to expose to the caller."""


class MailAuthenticationError(MailError):
    """Unavailable credentials or a rejected login, with no server diagnostics."""


class CallDeadlineExceeded(MailError):
    """The bounded mailbox call ran out of time; retain any available results."""


class BridgeUnavailable(MailError):
    """Nothing answers on Bridge's IMAP address; the text names only that address."""

    def __init__(self, host: str, port: int):
        address = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        super().__init__(
            f"Proton Mail Bridge is not running: nothing answers on {address}. Start Bridge "
            "(its app, its service, or `bridge --noninteractive`), then retry"
        )


class _ConnectTimedOut(ConnectionError):
    """The connect to Bridge got no answer within the whole socket timeout."""


def _imap_unescape(match: re.Match) -> bytes:
    value = match.group(1)
    if value in _IMAP_ESCAPES:
        return _IMAP_ESCAPES[value]
    try:
        if value[:1] == b"x" and len(value) == 3:
            return bytes([int(value[1:], 16)])
        if value[:1] in (b"u", b"U") and len(value) in (5, 9):
            return chr(int(value[1:], 16)).encode("utf-8")
        if len(value) == 3 and all(char in b"01234567" for char in value):
            return bytes([int(value, 8)])
    except (ValueError, UnicodeError):
        pass
    raise MailError("Mailbox returned invalid message metadata")


class _Deadline:
    def __init__(self, seconds: float, socket_timeout: float):
        started = time.monotonic()
        self.end = started + seconds
        # Leave time to close an interrupted stream and serialize a refusal
        # before the caller's budget expires. Keep end as the configured budget
        # for the existing execution/headroom accounting.
        self.response_reserve = min(5.0, max(0.0, seconds) * 0.1)
        self.socket_timeout = socket_timeout
        self._guard_state = threading.local()
        self._stage = "connection setup"
        self._stage_started = started
        self._stage_seconds: dict[str, float] = {}

    @property
    def stage(self) -> str:
        return self._stage

    @stage.setter
    def stage(self, value: str) -> None:
        now = time.monotonic()
        self._stage_seconds[self._stage] = (
            self._stage_seconds.get(self._stage, 0.0) + now - self._stage_started
        )
        self._stage, self._stage_started = value, now

    def diagnostic(self) -> str:
        """Report elapsed stages without adding fields to mailbox responses."""
        seconds = dict(self._stage_seconds)
        seconds[self.stage] = seconds.get(self.stage, 0.0) + time.monotonic() - self._stage_started
        timings = ", ".join(
            f"{stage}: {round(elapsed * 1000)}" for stage, elapsed in seconds.items()
        )
        return f"stage={self.stage}; stage_ms=({timings})"

    @property
    def io_end(self) -> float:
        return self.end - self.response_reserve

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.io_end

    def check(self, connection: Any = None) -> float:
        remaining = self.io_end - time.monotonic()
        if remaining <= 0:
            raise CallDeadlineExceeded("Mailbox call deadline reached")
        timeout = min(self.socket_timeout, remaining)
        sock = getattr(connection, "sock", None)
        if sock is not None:
            sock.settimeout(timeout)
        return timeout

    @contextmanager
    def guard(self, connection: Any) -> Iterator[None]:
        """Interrupt buffered reads even when a peer keeps sending small chunks."""
        timeout = self.check(connection)
        if getattr(self._guard_state, "connection", None) is connection:
            # A timed command already owns a watchdog for this socket. Reusing
            # it avoids starting a thread for every line/literal in a bulk FETCH.
            # Constructor reads still create their own watchdogs, as before.
            yield
            self.check(connection)
            return
        previous = getattr(self._guard_state, "connection", None)
        self._guard_state.connection = connection
        lock = threading.Lock()
        active = True
        interrupted = False

        def interrupt() -> None:
            nonlocal interrupted
            with lock:
                if active:
                    interrupted = True
                    sock = getattr(connection, "sock", None)
                    if sock is not None:
                        with suppress(OSError):
                            sock.shutdown(socket.SHUT_RDWR)

        timer = None
        if getattr(connection, "sock", None) is not None:
            # A peer can keep a buffered read alive by trickling bytes faster
            # than the socket timeout. Bound the whole command as well.
            timer = threading.Timer(timeout, interrupt)
            timer.daemon = True
            timer.start()
        try:
            try:
                yield
                with lock:
                    # Completion and watchdog interruption must be atomic: a
                    # late callback cannot close a successfully read stream.
                    active = False
                    if interrupted:
                        raise CallDeadlineExceeded("Mailbox call timed out")
            except Exception as error:
                if isinstance(error, TimeoutError) or interrupted or self.expired:
                    # A timed-out IMAP stream cannot be reused, including when
                    # the socket timeout is shorter than the overall budget.
                    self.end = min(self.end, time.monotonic())
                    raise CallDeadlineExceeded("Mailbox call timed out") from None
                raise
            self.check()
        finally:
            self._guard_state.connection = previous
            with lock:
                active = False
            if timer is not None:
                timer.cancel()


class _DeadlineTLSContext:
    """Make the TLS socket available to the watchdog before its handshake."""

    def __init__(self, context: Any, connection: Any, deadline: _Deadline):
        self.context, self.connection, self.deadline = context, connection, deadline

    def wrap_socket(self, sock: Any, **kwargs: Any) -> Any:
        wrapped = self.context.wrap_socket(sock, do_handshake_on_connect=False, **kwargs)
        self.connection.sock = wrapped
        with self.deadline.guard(self.connection):
            wrapped.do_handshake()
        return wrapped


class _DeadlineIMAP4(imaplib.IMAP4):
    """Bound greeting/capability reads that happen inside IMAP4 construction."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
        deadline: _Deadline,
        on_connect: Callable[[Any], None] | None = None,
    ):
        self._call_deadline = deadline
        self._on_connect = on_connect
        try:
            super().__init__(host, port, timeout=timeout)
        except Exception:
            with suppress(Exception):
                self.shutdown()
            raise

    def _create_socket(self, timeout: Any) -> socket.socket:
        try:
            return super()._create_socket(timeout)
        except TimeoutError:
            # With the whole socket timeout nothing answered there; a shorter one is only
            # the call's own budget running out.
            if timeout is not None and timeout >= self._call_deadline.socket_timeout:
                raise _ConnectTimedOut from None
            raise

    def open(self, host: str = "", port: int = imaplib.IMAP4_PORT, timeout: Any = None) -> None:
        super().open(host, port, timeout)
        # A watcher can interrupt setup before the greeting or TLS/login reads.
        if self._on_connect is not None:
            self._on_connect(self)

    def read(self, size: int) -> bytes:
        with self._call_deadline.guard(self):
            return super().read(size)

    def readline(self) -> bytes:
        with self._call_deadline.guard(self):
            return super().readline()

    def starttls(self, ssl_context: Any = None) -> Any:
        context = ssl_context or ssl.create_default_context()
        return super().starttls(_DeadlineTLSContext(context, self, self._call_deadline))


class _TimedConnection:
    """Per-session proxy: no shared deadline or IMAP state across tool calls."""

    def __init__(
        self,
        connection: Any,
        deadline: _Deadline,
    ):
        object.__setattr__(self, "connection", connection)
        object.__setattr__(self, "deadline", deadline)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self.connection, name)
        if not callable(value):
            return value

        def call(*args: Any, **kwargs: Any) -> Any:
            if name == "select":
                # Raw writable SELECTs in cleanup must not inherit another
                # mailbox's search-cache identity.
                self._search_selection = None
            with self.deadline.guard(self.connection):
                return value(*args, **kwargs)

        return call

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self.connection, name, value)


def _quote(value: str) -> bytes:
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise MailValidationError(
            "Mail search and header values must contain no control characters"
        )
    return ('"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"').encode("utf-8")


def _imap_date(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise MailValidationError("Dates must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise MailValidationError("Dates must use a valid YYYY-MM-DD date") from None
    return f"{parsed.day:02d}-{_MONTHS[parsed.month - 1]}-{parsed.year:04d}"


def _encode_folder(value: str) -> str:
    _quote(value)
    result: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            encoded = base64.b64encode("".join(pending).encode("utf-16-be")).decode("ascii")
            result.append("&" + encoded.rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for char in value:
        if 32 <= ord(char) <= 126:
            flush()
            result.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return "".join(result)


def _decode_folder(value: bytes) -> str:
    encoded = value.decode("ascii")

    def decode(match: re.Match[str]) -> str:
        payload = match.group(1)
        if not payload:
            return "&"
        payload = payload.replace(",", "/")
        payload += "=" * (-len(payload) % 4)
        return base64.b64decode(payload).decode("utf-16-be")

    return re.sub(r"&([^-]*)-", decode, encoded)


def _mailbox(folder: str) -> str:
    return _quote(_encode_folder(folder)).decode("ascii")


def _checked(response: tuple[Any, Any]) -> Any:
    status, data = response
    if status not in ("OK", b"OK"):
        raise MailError("Mailbox operation failed")
    return data


def _refresh_capabilities(connection: Any) -> None:
    """Store only capabilities advertised after successful authentication."""
    # imaplib records both untagged CAPABILITY and tagged OK [CAPABILITY ...]
    # here, but login() does not update its cached capabilities attribute.
    _, data = connection.response("CAPABILITY")
    if not data or data == [None]:
        data = _checked(connection.capability())
    if not isinstance(data, (list, tuple)) or not data:
        raise MailError("Mailbox returned invalid capabilities")
    latest = data[-1]
    if isinstance(latest, bytes):
        latest = latest.decode("ascii")
    if not isinstance(latest, str) or not latest.isascii() or not latest.strip():
        raise MailError("Mailbox returned invalid capabilities")
    # RFC 3501 atom-specials are forbidden inside capabilities. Spaces separate
    # atoms; controls or bracketed response prose must never grant MOVE.
    if any(ord(char) < 32 or ord(char) == 127 or char in '(){%*"\\]' for char in latest):
        raise MailError("Mailbox returned invalid capabilities")
    # Each advertisement replaces the previous set; never union pre-auth atoms.
    connection.capabilities = tuple(latest.upper().split())


def _literal(data: list[Any]) -> bytes:
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    raise MailError("Mailbox returned an incomplete message")


def _internaldate(value: Any) -> float | None:
    """Return a locale-independent sort key, or unknown for a malformed date."""
    if not isinstance(value, bytes):
        return None
    raw = b'INTERNALDATE "' + value + b'"'
    try:
        parsed = imaplib.Internaldate2tuple(raw)
        match = imaplib.InternalDate.fullmatch(raw)
        if parsed is None or match is None:
            return None
        # Internaldate2tuple normalizes out-of-range fields. Reject those rather
        # than assigning an invalid date a plausible but incorrect sort key.
        datetime(
            int(match["year"]),
            _MONTHS.index(match["mon"].decode("ascii")) + 1,
            int(match["day"]),
            int(match["hour"]),
            int(match["min"]),
            int(match["sec"]),
        )
        if int(match["zoneh"]) > 23 or int(match["zonem"]) > 59:
            return None
        return time.mktime(parsed)
    except (KeyError, ValueError, OverflowError, OSError):
        return None


class _ImapValues:
    """Parse IMAP values, including quoted strings and literal strings."""

    def __init__(self, raw: bytes):
        self.raw = raw
        self.position = 0

    def value(self) -> Any:
        raw = self.raw
        self.position = _IMAP_SPACE.match(raw, self.position).end()
        if self.position >= len(raw):
            raise MailError("Mailbox returned invalid message metadata")
        char = raw[self.position]
        self.position += 1
        if char == ord("("):
            values = []
            while True:
                self.position = _IMAP_SPACE.match(raw, self.position).end()
                if self.position >= len(raw):
                    raise MailError("Mailbox returned invalid message metadata")
                if raw[self.position] == ord(")"):
                    self.position += 1
                    return values
                values.append(self.value())
        if char == ord('"'):
            match = _IMAP_QUOTED.match(raw, self.position)
            if match is None:
                raise MailError("Mailbox returned invalid message metadata")
            self.position = match.end()
            value = match.group(1)
            if b"\\" not in value:
                return value
            # Gluon uses Go quoting. Preserve escaped controls and Unicode so
            # sender/identity validation sees the original, potentially unsafe value.
            return _IMAP_ESCAPE.sub(_imap_unescape, value)
        if char == ord("{"):
            end = raw.find(b"}", self.position)
            if end < 0:
                raise MailError("Mailbox returned invalid message metadata")
            length = int(raw[self.position : end].rstrip(b"+"))
            start = end + 1
            if raw[start : start + 2] != b"\r\n":
                raise MailError("Mailbox returned invalid message metadata")
            start += 2
            self.position = start + length
            if self.position > len(raw):
                raise MailError("Mailbox returned invalid message metadata")
            return raw[start : self.position]
        start = self.position - 1
        self.position = _IMAP_ATOM.match(raw, self.position).end()
        token = raw[start : self.position]
        return None if token.upper() == b"NIL" else token


def _structure(data: list[Any]) -> list[Any]:
    chunks = []
    for item in data:
        if isinstance(item, tuple):
            chunks.append(item[0] + b"\r\n" + item[1])
        elif isinstance(item, bytes):
            chunks.append(item)
    raw = b" ".join(chunks)
    match = re.search(rb"\bBODYSTRUCTURE\s+", raw, flags=re.IGNORECASE)
    if not match:
        raise MailError("Mailbox returned incomplete message metadata")
    result = _ImapValues(raw[match.end() :]).value()
    if not isinstance(result, list):
        raise MailError("Mailbox returned invalid message metadata")
    return result


def _string(value: Any) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else ""


def _parameters(value: Any) -> dict[str, str]:
    if not isinstance(value, list):
        return {}
    return {_string(value[i]).lower(): _string(value[i + 1]) for i in range(0, len(value) - 1, 2)}


def _filename(disposition: dict[str, str], content_type: dict[str, str]) -> str | None:
    for parameters, prefix in ((disposition, "filename"), (content_type, "name")):
        fields = []
        for name, value in parameters.items():
            if name == prefix or re.fullmatch(re.escape(prefix) + r"\*(?:\d+\*?)?", name):
                parameter = "filename" + name[len(prefix) :]
                fields.append(parameter + "=" + _quote(value).decode("utf-8"))
        if fields:
            header = EmailMessage(policy=policy.default)
            header["Content-Disposition"] = "attachment; " + "; ".join(fields)
            filename = header.get_filename()
            if filename is not None:
                return filename
    return None


def _mime_parts(
    structure: list[Any], section: str = "", inherited_attachment: bool = False
) -> list[dict[str, Any]]:
    if not structure:
        raise MailError("Mailbox returned invalid message metadata")
    if isinstance(structure[0], list):
        count = 0
        while count < len(structure) and isinstance(structure[count], list):
            count += 1
        params = _parameters(structure[count + 1] if len(structure) > count + 1 else None)
        disposition = structure[count + 2] if len(structure) > count + 2 else None
        disp_params = (
            _parameters(disposition[1])
            if isinstance(disposition, list) and len(disposition) > 1
            else {}
        )
        is_attachment = (
            inherited_attachment
            or bool(_filename(disp_params, params))
            or (isinstance(disposition, list) and _string(disposition[0]).lower() == "attachment")
        )
        result = []
        for index, part in enumerate(structure[:count], start=1):
            child_section = f"{section}.{index}" if section else str(index)
            result.extend(_mime_parts(part, child_section, is_attachment))
        return result
    if len(structure) < 7:
        raise MailError("Mailbox returned invalid message metadata")
    main_type, subtype = _string(structure[0]).lower(), _string(structure[1]).lower()
    params = _parameters(structure[2])
    extension_index = (
        9 if main_type == "text" else 11 if (main_type, subtype) == ("message", "rfc822") else 8
    )
    disposition = structure[extension_index] if len(structure) > extension_index else None
    disp_params = (
        _parameters(disposition[1])
        if isinstance(disposition, list) and len(disposition) > 1
        else {}
    )
    filename = _filename(disp_params, params)
    attached = (
        inherited_attachment
        or bool(filename)
        or main_type != "text"
        or (isinstance(disposition, list) and _string(disposition[0]).lower() == "attachment")
    )
    return [
        {
            "section": section or "1",
            "type": f"{main_type}/{subtype}",
            "charset": params.get("charset", "utf-8"),
            "encoding": _string(structure[5]).lower(),
            "size": int(structure[6]),
            "filename": filename,
            "attachment": attached,
        }
    ]


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head"):
            self.hidden += 1
        if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3") and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head") and self.hidden:
            self.hidden -= 1
        if tag in ("p", "div", "li", "tr") and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


# Marketing mail pads its preheader with long runs of invisible characters, often between
# spaces, which would fill the text a read returns. A run holding two or more of them, with the
# spaces among them, becomes one space; a single one, such as the joiner in an emoji sequence or
# a Persian zero-width non-joiner, stays.
_INVISIBLE = frozenset(
    "\u00ad\u034f\u115f\u1160\u180e\u200b\u200c\u200d"
    "\u2060\u2061\u2062\u2063\u2064\u2800\u3164\ufeff\uffa0"
)
_ANY_INVISIBLE = re.compile("[" + "".join(sorted(_INVISIBLE)) + "]")
_FILLER_RUN = re.compile("[" + "".join(sorted(_INVISIBLE)) + " \t\u00a0\u2007\u202f]+")


def _collapse_filler(text: str) -> str:
    def collapse(match: re.Match) -> str:
        run = match.group()
        return " " if sum(char in _INVISIBLE for char in run) >= 2 else run

    return _FILLER_RUN.sub(collapse, text) if _ANY_INVISIBLE.search(text) else text


def _text_body(
    raw: bytes, part: dict[str, Any], *, partial: bool = False, strip_html: bool = True
) -> str:
    if part["encoding"] == "base64":
        if partial:
            # A partial fetch may stop inside a folded base64 quartet.
            raw = re.sub(rb"\s+", b"", raw)
            raw = raw[: len(raw) // 4 * 4]
        raw = base64.b64decode(raw)
    elif part["encoding"] == "quoted-printable":
        raw = quopri.decodestring(raw)
    try:
        text = raw.decode(part["charset"], errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    if strip_html and part["type"] == "text/html":
        parser = _HtmlText()
        parser.feed(text)
        text = "".join(parser.parts)
    return text.strip()


def _ids(value: str) -> list[str]:
    # A single field may omit brackets; References may still contain a list.
    try:
        return [_message_id(value)]
    except MailValidationError:
        pass
    identifiers = []
    for identifier in _ID.findall(value):
        try:
            identifiers.append(_message_id(identifier))
        except MailValidationError:
            continue
    return identifiers


def _message_id(value: str) -> str:
    if not isinstance(value, str):
        raise MailValidationError("Message-ID must be text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise MailValidationError("Message-ID requires valid UTF-8 text") from None
    if (
        not value
        or len(value) > MAX_MESSAGE_ID_BYTES
        or any(
            char.isspace() or char in "\"'(),;" or unicodedata.category(char) in {"Cc", "Cf", "Cs"}
            for char in value
        )
        or not re.fullmatch(r"<[^<>]+>|[^<>]+", value)
    ):
        raise MailValidationError("Message-ID must be one bounded, control-free token")
    if size > MAX_MESSAGE_ID_BYTES:
        raise MailValidationError("Message-ID exceeds the size limit")
    _quote(value)
    # Retain the existing bare RFC alias. Opaque provider IDs are already exact
    # identities and must never gain brackets, change case, or lose characters.
    if size + 2 <= MAX_MESSAGE_ID_BYTES and re.fullmatch(r"[^<>\s@]+@[^<>\s@]+", value):
        value = "<" + value + ">"
    return value


def _message_id_search(value: str) -> str:
    """HEADER search is a substring prefilter; callers compare exact identities."""
    return value[1:-1] if value.startswith("<") and value.endswith(">") else value


def _raw_header_value(headers: EmailMessage) -> str:
    # Headerregistry can truncate non-RFC IDs (for example <a@b@c>). Use the
    # server's complete field, recovering UTF-8 just as cleanup's raw checks do.
    for field, value in headers.raw_items():
        if field.casefold() == "message-id":
            return value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return ""


def _draft_message_id(value: str) -> str:
    # Draft input keeps its existing stricter rejection of controls, including
    # leading/trailing line breaks, before shared identity normalization.
    _quote(value)
    value = value.strip()
    if "<" not in value and ">" not in value:
        value = "<" + value + ">"
    if not re.fullmatch(r"<[^<>\s@]+@[^<>\s@]+>", value):
        raise MailValidationError("A complete RFC Message-ID is required")
    return value


# A Message-ID needs a domain. Without one, make_msgid looks up and discloses this
# machine's name, such as a reverse-DNS ip6.arpa name.
_MESSAGE_ID_FALLBACK_DOMAIN = "proton-mail-mcp.invalid"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_DOMAIN = re.compile(rf"(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})+")


def _message_id_domain(sender: str) -> str:
    """The domain of a draft's bare From address, never the machine's hostname."""
    domain = sender.rpartition("@")[2]
    return domain.lower() if _DOMAIN.fullmatch(domain) else _MESSAGE_ID_FALLBACK_DOMAIN


def _addresses(value: str | list[str] | None, required: bool = False) -> str:
    if value is None:
        value = ""
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise MailValidationError("Recipients must be email addresses")
        value = ", ".join(value)
    if not isinstance(value, str):
        raise MailValidationError("Recipients must be email addresses")
    _quote(value)
    if not value.strip():
        if required:
            raise MailValidationError("At least one recipient is required")
        return ""
    if value.rstrip().endswith(","):
        raise MailValidationError("Recipients must be valid email addresses")
    try:
        # Parser errors, including deeply nested comments that exhaust the
        # recursive parser, are invalid recipients, never backend failures.
        parsed = getaddresses([value])
        if not parsed:
            raise ValueError
        for _, address in parsed:
            validated = Address(addr_spec=address)
            if not validated.username or not validated.domain:
                raise ValueError
    except (ValueError, IndexError, HeaderParseError, RecursionError):
        raise MailValidationError("Recipients must be valid email addresses") from None
    return value


def check_from_address(settings: Any, address: Any) -> None:
    """A chosen From must be exactly on the allowed list; no case folding or display names."""
    if address not in settings.send_from_allowlist:
        raise MailValidationError(
            "From address is not on the allowed list; see list_from_addresses"
        )


def draft_audit(
    to: Any = None,
    cc: Any = None,
    subject: Any = None,
    body: Any = None,
    attachments: Any = None,
    attachment_indexes: Any = None,
    from_address: Any = None,
    **_: Any,
) -> dict[str, Any]:
    """Describe a draft for the audit log: recipient domains and hashes, never content.

    Recipients are parsed as create_draft writes its To and Cc headers. A list it
    would refuse has no count or domains (None). Never raises.
    """
    try:
        headers = [_addresses(to, required=True), _addresses(cc)]
        addresses = [
            address for header in headers if header for _, address in getaddresses([header])
        ]
        count = len(addresses)
        domains = sorted({address.rsplit("@", 1)[1].lower()[:253] for address in addresses})[:50]
    except Exception:
        # Describing a refused or unparseable attempt must not keep it unaudited.
        count = domains = None

    def digest(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()

    files = sum(
        len(value) for value in (attachments, attachment_indexes) if isinstance(value, list)
    )
    return {
        "recipient_domains": domains,
        "recipient_count": count,
        "subject_sha256": digest(subject),
        "body_sha256": digest(body),
        **({"attachment_count": files} if files else {}),
        **(
            {"from_domain": from_address.rsplit("@", 1)[-1].lower()[:253]}
            if from_address and isinstance(from_address, str)
            else {}
        ),
    }


class MailClient:
    def __init__(
        self,
        settings: Any,
        credentials_provider: Callable[..., Any] | None = None,
        imap_factory: Callable[..., Any] | None = None,
    ):
        from .search_index import SearchIndex

        self.settings = settings
        self.search_index = SearchIndex(settings)
        self.credentials_provider = credentials_provider or get_credentials
        self.imap_factory = imap_factory or _DeadlineIMAP4
        self._cleanup_service = None
        self._cleanup_lock = threading.Lock()
        from .reindex import ReindexService

        self._reindex_service = ReindexService(self)
        from .index_autorebuild import IndexAutoRebuild

        self.index_autorebuild = IndexAutoRebuild(self, settings)
        self._host_confirmation = False
        # None means sieve.UnavailableSieveBackend, the only shipped backend: Bridge
        # offers no way to apply Sieve filters, so creating one always refuses. No
        # setting selects another backend; only tests plug in offline fakes.
        self.sieve_backend = None

    @property
    def reindex(self) -> Any:
        return self._reindex_service

    def reindex_preview(self) -> dict[str, Any]:
        return self.reindex.preview()

    def reindex_execute(self, token: str) -> dict[str, Any]:
        return self.reindex.execute(token)

    def reindex_status(self) -> dict[str, Any]:
        from .index_status import progress_fields, recovery_state

        status = self.reindex.status()
        auto = self.index_autorebuild.status()
        recovery = recovery_state(self, auto)
        active = recovery == "rebuilding"
        unavailable = recovery == "unavailable"
        if status["state"] == "running":
            if status.get("eta_seconds") == 0:
                status["eta_seconds"] = None
            return status
        if not active and status["state"] in {"failed", "cancelled"}:
            # Cancellation and a kept-old failure are polling contracts. An
            # incomplete serving cache does not erase that manual run's result.
            return status

        indexed = status.get("indexed_count")
        target = status.get("all_mail_count")
        unknown = indexed is None or target is None
        behind = not unknown and indexed < target
        same_run = self.index_autorebuild.manual_run_id == status["run_id"]
        auto_terminal = auto.get("state") in {"failed", "partial"} and same_run
        auto_finished = auto.get("state") == "succeeded" and same_run and not unavailable
        if not (active or auto_terminal or behind or unknown or auto_finished or unavailable):
            return status

        # Keep full-run history separately from the serving cache's live state.
        # Inactivity changes coverage wording, not the preceding manual outcome.
        if active or auto_terminal or (auto_finished and not unknown and not behind):
            status.update(
                source="auto",
                run_id=None,
                swap_outcome="not_attempted",
                swap_reason=None,
                last_error=auto.get("last_error"),
            )
        if active:
            for key in (
                "indexed_count",
                "indexed_max_uid",
                "all_mail_count",
                "all_mail_max_uid",
                "all_mail_observed_at",
                "last_catchup_at",
                "last_expunge_reconciliation_at",
            ):
                if status[key] is None and auto.get(key) is not None:
                    status[key] = auto[key]
        status["max_uid"] = status["all_mail_max_uid"]
        target = status["all_mail_count"]
        if target is None and active:
            target = auto.get("target")
        eta = auto.get("eta_seconds") if active and auto.get("target") == target else None
        progress = progress_fields(status["indexed_count"], target, eta_seconds=eta, active=active)
        if status["indexed_count"] is None:
            progress["indexed_count"] = None
        if not active:
            progress["eta_seconds"] = None
        status["state"] = "succeeded" if auto_finished and not unknown and not behind else recovery
        status.update(progress)
        status["percent_done"] = progress["percent"]
        return status

    def reindex_cancel(self, run_id: str) -> dict[str, Any]:
        return self.reindex.cancel(run_id)

    @property
    def cleanup(self) -> Any:
        # One server-side token store per client, shared by concurrent MCP calls.
        # Lazy construction keeps existing read/draft calls independent of cleanup state.
        with self._cleanup_lock:
            if self._cleanup_service is None:
                from .guarded import CleanupService

                self._cleanup_service = CleanupService(self, self.settings)
                if self._host_confirmation:
                    self._cleanup_service.tokens.confirmation_required = True
            return self._cleanup_service

    def require_host_confirmation(self) -> None:
        """Make every token this client issues need a host-form confirmation first."""
        with self._cleanup_lock:
            self._host_confirmation = True
            if self._cleanup_service is not None:
                self._cleanup_service.tokens.confirmation_required = True

    def approval(self, action: str, token: str) -> dict[str, Any]:
        from .confirmation import describe_approval

        return describe_approval(self.cleanup, action, token)

    def approval_hash(self, token: str) -> str | None:
        return self.cleanup.tokens.action_hash(token)

    def check_move_target(self, action: str, token: str, target: str) -> None:
        """Refuse a stated move target that this token's approval does not pin."""
        grant = self.cleanup.tokens.describe(token, action)
        if action == "move_to_folder" and grant.params["folder"] != target:
            raise MailValidationError("Preview token does not match the requested move target")

    def confirm_approval(self, action: str, token: str, action_hash: str) -> None:
        self.cleanup.tokens.confirm(token, action, action_hash)

    def revoke_approval(self, action: str, token: str) -> None:
        self.cleanup.tokens.revoke(token, action)

    def preview_action(
        self,
        action: str,
        query: dict[str, Any] | None = None,
        ids: list[str] | None = None,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if action == "delete_draft":
            from .delete_draft import DeleteDraftService

            if limit is not None:
                raise MailValidationError("Draft deletion does not accept limit")
            return DeleteDraftService(self.cleanup).preview(query=query, ids=ids, params=params)
        if action == "unsubscribe":
            from .unsubscribe import UnsubscribeService

            if limit is not None:
                raise MailValidationError("limit is supported for mailbox cleanup actions only")
            return UnsubscribeService(self.cleanup).preview(query=query, ids=ids, params=params)
        return self.cleanup.preview_action(action, query=query, ids=ids, params=params, limit=limit)

    def unsubscribe(self, token: str) -> dict[str, Any]:
        from .unsubscribe import UnsubscribeService

        return UnsubscribeService(self.cleanup).execute(token)

    def preview_rules(
        self,
        folder: str = "INBOX",
        mark_read: bool = False,
        limit: int = 200,
        order: str = "oldest",
        cursor: str | None = None,
    ) -> dict[str, Any]:
        from .folder_rules import FolderRulesService

        return FolderRulesService(self.cleanup).preview_rules(
            folder, mark_read=mark_read, limit=limit, order=order, cursor=cursor
        )

    def preview_create_folder(self, name: str) -> dict[str, Any]:
        from .create_folder import CreateFolderService

        return CreateFolderService(self.cleanup).preview_create_folder(name)

    def create_folder(self, token: str) -> dict[str, Any]:
        from .create_folder import CreateFolderService

        return CreateFolderService(self.cleanup).create_folder(token)

    def preview_sieve_filter(
        self,
        name: str,
        folder: str,
        conditions: list[dict[str, Any]],
        match: str = "all",
        mark_read: bool = False,
    ) -> dict[str, Any]:
        from .sieve import filter_spec
        from .sieve_filters import SieveFilterService

        spec = filter_spec(name, folder, conditions, match=match, mark_read=mark_read)
        return SieveFilterService(self.cleanup, self.sieve_backend).preview(spec)

    def sieve_filter_ready(self) -> None:
        """Refuse at once when no backend can apply filters, before any confirmation."""
        from .sieve import UnavailableSieveBackend, require_backend

        backend = self.sieve_backend
        require_backend(UnavailableSieveBackend() if backend is None else backend)

    def create_sieve_filter(self, token: str) -> dict[str, Any]:
        from .sieve_filters import SieveFilterService

        return SieveFilterService(self.cleanup, self.sieve_backend).create(token)

    def preview_add_rule(
        self,
        name: str,
        folder: str,
        conditions: list[dict[str, Any]],
        match: str = "all",
        mark_read: bool = False,
        include_matches: bool = False,
    ) -> dict[str, Any]:
        from .saved_rules import SavedRuleService
        from .sieve import filter_spec

        if type(include_matches) is not bool:
            raise MailValidationError("include_matches must be a boolean")
        spec = filter_spec(name, folder, conditions, match=match, mark_read=mark_read)
        return SavedRuleService(self.cleanup).preview(spec, include_matches=include_matches)

    def add_rule_ready(self) -> None:
        """Refuse at once when no backend can save rules, before any confirmation."""
        from .saved_rules import require_rules_backend, rules_backend

        require_rules_backend(rules_backend(self.settings))

    def add_rule(self, token: str) -> dict[str, Any]:
        from .saved_rules import SavedRuleService

        return SavedRuleService(self.cleanup).add(token)

    def list_rules(self) -> dict[str, Any]:
        from .saved_rules import SavedRuleService

        return SavedRuleService(self.cleanup).list_rules()

    def apply_rules(self, token: str, *, limit: int | None = None) -> dict[str, Any]:
        from .folder_rules import FolderRulesService

        return FolderRulesService(self.cleanup).apply_rules(token, limit=limit)

    def move_to_trash(self, token: str, *, limit: int | None = None) -> dict[str, Any]:
        return self.cleanup.execute("move_to_trash", token, limit=limit)

    def delete_draft(self, token: str) -> dict[str, Any]:
        from .delete_draft import DeleteDraftService

        return DeleteDraftService(self.cleanup).execute(token)

    def archive(self, token: str, *, limit: int | None = None) -> dict[str, Any]:
        return self.cleanup.execute("archive", token, limit=limit)

    def move_to_folder(self, token: str, *, limit: int | None = None) -> dict[str, Any]:
        return self.cleanup.execute("move_to_folder", token, limit=limit)

    def mark_read(self, token: str, *, limit: int | None = None) -> dict[str, Any]:
        return self.cleanup.execute("mark_read", token, limit=limit)

    def mark_unread(self, token: str, *, limit: int | None = None) -> dict[str, Any]:
        return self.cleanup.execute("mark_unread", token, limit=limit)

    def add_label(self, token: str, *, limit: int | None = None) -> dict[str, Any]:
        return self.cleanup.execute("add_label", token, limit=limit)

    def remove_label(self, token: str, *, limit: int | None = None) -> dict[str, Any]:
        return self.cleanup.execute("remove_label", token, limit=limit)

    def undo_action(self, log_id: str) -> dict[str, Any]:
        return self.cleanup.undo_action(log_id)

    def list_actions(
        self,
        token: str | None = None,
        since: str | None = None,
        before: str | None = None,
        limit: int = 100,
        action: str | None = None,
        client: str | None = None,
    ) -> dict[str, Any]:
        from .action_log import ActionLog

        result = ActionLog(self.settings.action_log_path).list_actions(
            token, since, before, limit, action=action, client=client
        )
        # Recovery lookup must stay available without loading mutation policy.
        # An uninitialized service cannot hold an in-memory continuation.
        with self._cleanup_lock:
            cleanup = self._cleanup_service
        for run in result.get("runs", []):
            run["continuation"] = (
                cleanup.tokens.continuation_state(run["run_id"])
                if cleanup is not None
                else {"available": False, "expires_at": None, "remaining": 0}
            )
        return result

    def _deadline(self, *, execute: bool = False) -> _Deadline:
        seconds = self.settings.call_timeout
        if execute:
            seconds = min(seconds, self.settings.execute_timeout)
        return _Deadline(seconds, self.settings.timeout)

    @contextmanager
    def _session(
        self,
        deadline: _Deadline | None = None,
        *,
        on_connect: Callable[[Any], None] | None = None,
    ) -> Iterator[tuple[Any, Any]]:
        deadline = deadline or self._deadline()
        connection = None
        session_ready = False
        stream_broken = False
        try:
            context = tls_context(self.settings)
            host = bridge_endpoint(self.settings)
            try:
                credentials = self.credentials_provider(self.settings)
            except CredentialError:
                raise MailAuthenticationError("Mailbox authentication failed") from None
            timeout = deadline.check()
            options = (
                {"deadline": deadline, "on_connect": on_connect}
                if self.imap_factory is _DeadlineIMAP4
                else {}
            )
            try:
                connection = self.imap_factory(
                    host, self.settings.imap_port, timeout=timeout, **options
                )
            except (ConnectionRefusedError, _ConnectTimedOut):
                raise BridgeUnavailable(self.settings.imap_host, self.settings.imap_port) from None
            if on_connect is not None and self.imap_factory is not _DeadlineIMAP4:
                on_connect(connection)
            timed = _TimedConnection(connection, deadline)
            timed._search_index = self.search_index
            timed._search_selection = None
            _checked(timed.starttls(ssl_context=context))
            verify_certificate(connection, self.settings)
            # Discard greeting/STARTTLS responses so only the authentication
            # exchange can supply a cached post-login CAPABILITY response.
            timed.response("CAPABILITY")
            try:
                _checked(timed.login(credentials.username, credentials.password))
            except imaplib.IMAP4.abort:
                raise
            except (imaplib.IMAP4.error, MailError) as error:
                if isinstance(error, CallDeadlineExceeded):
                    raise
                raise MailAuthenticationError("Mailbox authentication failed") from None
            _refresh_capabilities(timed)
            session_ready = True
            yield timed, credentials
        except TimeoutError:
            stream_broken = True
            deadline.end = min(deadline.end, time.monotonic())
            raise CallDeadlineExceeded("Mailbox call timed out") from None
        except CallDeadlineExceeded:
            stream_broken = True
            if not session_ready:
                raise CallDeadlineExceeded("Mailbox connection timed out") from None
            raise
        except (MailError, MailValidationError):
            raise
        except (imaplib.IMAP4.abort, OSError):
            stream_broken = True
            raise MailError("Mailbox operation failed") from None
        except Exception:
            raise MailError("Mailbox operation failed") from None
        finally:
            if connection is not None:
                try:
                    if stream_broken:
                        raise MailError("Mailbox connection interrupted")
                    with deadline.guard(connection):
                        connection.logout()
                except Exception:
                    # An expired/failed connection must not keep Bridge working or wait
                    # for a second socket timeout during graceful logout.
                    with suppress(Exception):
                        connection.shutdown()

    @staticmethod
    def _select(connection: Any, folder: str) -> dict[str, Any]:
        data = _checked(connection.select(_mailbox(folder), readonly=True))
        if (
            not isinstance(data, (list, tuple))
            or len(data) != 1
            or not isinstance(data[0], bytes)
            or not re.fullmatch(rb"[0-9]+", data[0])
        ):
            raise MailError("Mailbox returned an invalid message count")
        # Retain SELECT evidence without consuming UIDVALIDITY: repeated identity
        # checks rely on imaplib's current selection responses remaining available.
        responses = getattr(connection, "untagged_responses", {})
        next_values = responses.get("UIDNEXT")
        uidnext = None
        if next_values is not None:
            if (
                not isinstance(next_values, (list, tuple))
                or len(next_values) != 1
                or not isinstance(next_values[0], bytes)
                or not re.fullmatch(rb"[1-9][0-9]*", next_values[0])
            ):
                raise MailError("Mailbox returned invalid UIDNEXT")
            uidnext = next_values[0].decode("ascii")
        state = {"folder": folder, "exists": int(data[0]), "uidnext": uidnext}
        connection._selected_inventory = state
        index = getattr(connection, "_search_index", None)
        connection._search_selection = None
        if index is not None and getattr(connection, "untagged_responses", None) is not None:
            from .search import _uidvalidity

            try:
                validity = _uidvalidity(connection)
            except MailError:
                # Never consume response("UIDVALIDITY") for optional indexing:
                # the search/thread caller still needs the selection evidence.
                pass
            else:
                connection._search_selection = (folder, validity)
                index.select(folder, validity)
        return state

    @staticmethod
    def _search(connection: Any, criteria: list[Any], literal: bytes | None = None) -> list[str]:
        if literal is not None:
            connection.literal = literal
        try:
            data = _checked(connection.uid("SEARCH", "CHARSET", "UTF-8", *criteria))
        finally:
            if literal is not None:
                connection.literal = None
        raw = b" ".join(item for item in data if isinstance(item, bytes))
        uids = raw.split()
        if any(not uid.isdigit() for uid in uids):
            raise MailError("Mailbox returned invalid message identifiers")
        result = [uid.decode("ascii") for uid in uids]
        selection = getattr(connection, "_search_selection", None)
        if selection is not None and criteria == ["ALL"] and literal is None:
            connection._search_index.reconcile(
                *selection, result, state=connection._selected_inventory
            )
        return result

    @classmethod
    def _header_search(cls, connection: Any, field: str, value: str) -> list[str]:
        quoted = _quote(value)
        if value.isascii():
            return cls._search(connection, ["HEADER", field, quoted])
        return cls._search(connection, ["HEADER", field], literal=value.encode("utf-8"))

    @staticmethod
    def _raw_headers(connection: Any, uid: str) -> bytes:
        return _literal(_checked(connection.uid("FETCH", uid, "(BODY.PEEK[HEADER])")))

    @classmethod
    def _headers(cls, connection: Any, uid: str) -> EmailMessage:
        headers = BytesParser(policy=policy.default).parsebytes(cls._raw_headers(connection, uid))
        selection = getattr(connection, "_search_selection", None)
        if selection is not None:
            connection._search_index.observe(*selection, {uid: {"headers": headers}})
        return headers

    @staticmethod
    def _metadata(
        connection: Any,
        uids: list[str],
        *,
        deadline: _Deadline,
        headers: bool = True,
        bodystructure: bool = True,
        internaldate: bool = True,
        identity_headers_only: bool = False,
        envelope: bool = False,
        size: bool = False,
        flags: bool = False,
        header_max_bytes: int | None = None,
        invalid_dates: set[str] | None = None,
        bcc: bool = False,
        raw_bytes: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Fetch a UID batch, associating literals by UID, never response sequence number.

        Optionally report UIDs with explicitly malformed INTERNALDATE values, or
        return the start of each raw message as ``raw``.
        """
        if not uids:
            return {}
        if any(not uid.isdigit() or int(uid) < 1 for uid in uids):
            raise MailError("Mailbox returned invalid message identifiers")
        deadline.check(connection)
        # A draft's Bcc, for the draft composer. FLAGS tell which messages are drafts.
        bcc = bcc and headers and not identity_headers_only
        fields = ["UID"]
        if raw_bytes is not None:
            fields.insert(0, f"BODY.PEEK[]<0.{raw_bytes}>")
        if internaldate:
            fields.append("INTERNALDATE")
        if size:
            fields.append("RFC822.SIZE")
        if flags or bcc:
            fields.append("FLAGS")
        if envelope:
            fields.append("ENVELOPE")
        if bodystructure:
            fields.append("BODYSTRUCTURE")
        if headers:
            names = (
                "MESSAGE-ID FROM SENDER REPLY-TO"
                if identity_headers_only
                else "MESSAGE-ID REFERENCES IN-REPLY-TO FROM SENDER REPLY-TO TO CC SUBJECT DATE"
            )
            if bcc:
                names += " BCC"
            partial = f"<0.{header_max_bytes}>" if header_max_bytes is not None else ""
            fields.append("BODY.PEEK[HEADER.FIELDS (" + names + ")]" + partial)
        data = _checked(connection.uid("FETCH", ",".join(uids), "(" + " ".join(fields) + ")"))
        # imaplib splits a response around each literal. Reassemble protocol values
        # before parsing so UID/structure may occur either before or after a literal.
        chunks = []
        for item in data:
            if isinstance(item, tuple):
                chunks.append(item[0] + b"\r\n" + item[1])
            elif isinstance(item, bytes):
                chunks.append(item)
        raw = b" ".join(chunks)
        parser = _ImapValues(raw)
        result: dict[str, dict[str, Any]] = {}
        malformed_dates: set[str] = set()
        requested = set(uids)
        record_start = re.compile(rb"\s*\d+\s+")
        while parser.position < len(raw):
            match = record_start.match(raw, parser.position)
            if not match:
                if not raw[parser.position :].strip():
                    break
                raise MailError("Mailbox returned invalid message metadata")
            parser.position = match.end()
            values = parser.value()
            if not isinstance(values, list):
                raise MailError("Mailbox returned invalid message metadata")
            items: dict[str, Any] = {}
            index = 0
            while index + 1 < len(values):
                key = _string(values[index]).upper()
                index += 1
                if key.startswith("BODY[") and "]" not in key:
                    # HEADER.FIELDS includes its own parenthesized field list and
                    # closing bracket, represented as separate IMAP values.
                    while index < len(values):
                        token = values[index]
                        if isinstance(token, bytes) and re.search(rb"](?:<\d+>)?$", token):
                            index += 1
                            break
                        index += 1
                    if index >= len(values):
                        raise MailError("Mailbox returned incomplete message metadata")
                items[key] = values[index]
                index += 1
            uid = _string(items.get("UID"))
            # imaplib retains unsolicited FETCH updates received during SEARCH
            # too. Flag updates may omit UID or refer to an unrelated message.
            if uid not in requested:
                continue
            record = result.setdefault(uid, {"structure": None})
            for key, value in items.items():
                if key.startswith("BODY[HEADER") and isinstance(value, bytes):
                    if header_max_bytes is not None:
                        # A partial FETCH does not identify EOF at exactly the
                        # requested size; conservatively keep cap hits unindexed.
                        record["headers_truncated"] = len(value) >= header_max_bytes
                        value = value[:header_max_bytes]
                        record["headers_fetched_bytes"] = len(value)
                    record["headers"] = BytesParser(policy=policy.default).parsebytes(value)
                elif key.startswith("BODY[]") and isinstance(value, bytes):
                    record["raw"] = value
                elif key == "ENVELOPE":
                    record["envelope"] = value
                elif (
                    key == "FLAGS"
                    and isinstance(value, list)
                    and all(isinstance(flag, bytes) for flag in value)
                ):
                    record["flags"] = [_string(flag) for flag in value]
                elif key == "BODYSTRUCTURE":
                    record["structure"] = value
                elif key == "RFC822.SIZE" and isinstance(value, bytes) and value.isdigit():
                    record["size"] = int(value)
                elif key == "INTERNALDATE":
                    stamp = _internaldate(value)
                    if stamp is None:
                        malformed_dates.add(uid)
                    else:
                        record["internaldate"] = stamp
                        # IMAP SINCE/BEFORE compare the stored calendar day,
                        # disregarding time and timezone, rather than UTC day.
                        match = imaplib.InternalDate.fullmatch(b'INTERNALDATE "' + value + b'"')
                        record["internaldate_day"] = "{:04d}-{:02d}-{:02d}".format(
                            int(match["year"]),
                            _MONTHS.index(match["mon"].decode("ascii")) + 1,
                            int(match["day"]),
                        )
        # A requested UID can have several records (for example FLAGS before or
        # after its metadata). Merge them first, then let callers treat any
        # incomplete UID like a message that disappeared during the batch.
        if internaldate and invalid_dates is not None:
            # A split/unsolicited record may also provide a valid date for the
            # same UID. Only reject dates still invalid after all records merge;
            # an absent INTERNALDATE remains a retryable omission.
            invalid_dates.update(
                uid for uid in malformed_dates if "internaldate" not in result[uid]
            )
        complete = {
            uid: record
            for uid, record in result.items()
            if (not headers or "headers" in record)
            and (not envelope or "envelope" in record)
            and (not bodystructure or isinstance(record["structure"], list))
            and (not internaldate or "internaldate" in record)
            and (not size or "size" in record)
            and (not flags or "flags" in record)
            and (raw_bytes is None or "raw" in record)
        }
        if bcc:
            for record in complete.values():
                if "\\draft" not in {flag.lower() for flag in record.get("flags", [])}:
                    # Other messages, such as sent copies, keep their hidden recipients hidden.
                    del record["headers"]["Bcc"]
        selection = getattr(connection, "_search_selection", None)
        if selection is not None and not identity_headers_only:
            connection._search_index.observe(
                *selection,
                {
                    uid: record
                    for uid, record in complete.items()
                    if not record.get("headers_truncated")
                },
            )
        return complete

    @staticmethod
    def _body(
        connection: Any,
        uid: str,
        *,
        max_bytes: int | None = None,
        structure: list[Any] | None = None,
        deadline: _Deadline | None = None,
        max_parts: int | None = None,
        details: dict[str, Any] | None = None,
        prefer_html: bool = False,
    ) -> tuple[str, list[dict[str, Any]]]:
        # prefer_html selects HTML alternatives and returns their markup unstripped,
        # for views that sanitize it; byte, part and deadline limits are unchanged.
        if deadline is not None:
            deadline.check(connection)
        if structure is None:
            structure = _structure(_checked(connection.uid("FETCH", uid, "(BODYSTRUCTURE)")))
        parts = _mime_parts(structure)
        attachments = [
            {"filename": part["filename"], "type": part["type"], "size": part["size"]}
            for part in parts
            if part["attachment"]
        ]
        candidates = [
            part
            for part in parts
            if not part["attachment"] and part["type"] in ("text/plain", "text/html")
        ]
        first = "text/html" if prefer_html else "text/plain"
        candidates.sort(key=lambda part: part["type"] != first)
        preferred_type = candidates[0]["type"] if candidates else None
        candidates = [part for part in candidates if part["type"] == preferred_type]
        body_size = sum(part["size"] for part in candidates)
        if details is not None and prefer_html:
            details["body_type"] = preferred_type
        if details is not None:
            details.update(
                attachments=attachments,
                body_size_bytes=body_size,
                body_fetched_bytes=0,
                body_limit_bytes=max_bytes,
                body_parts_fetched=0,
                body_parts_limit=max_parts,
                truncated=(max_bytes is not None and body_size > max_bytes)
                or (max_parts is not None and len(candidates) > max_parts),
            )
        if not candidates:
            return "", attachments
        bodies = []
        remaining = max_bytes
        for part in candidates[:max_parts]:
            if deadline is not None:
                deadline.check(connection)
            if remaining == 0:
                break
            partial = f"<0.{remaining}>" if remaining is not None else ""
            raw = _literal(
                _checked(connection.uid("FETCH", uid, f"(BODY.PEEK[{part['section']}]{partial})"))
            )
            if remaining is not None:
                raw = raw[:remaining]
                remaining -= len(raw)
            if details is not None:
                details["body_fetched_bytes"] += len(raw)
                details["body_parts_fetched"] += 1
            bodies.append(
                _text_body(raw, part, partial=max_bytes is not None, strip_html=not prefer_html)
            )
            if details is not None:
                details["text"] = "\n\n".join(bodies)[:max_bytes]
        body = "\n\n".join(bodies)
        return body, attachments

    @staticmethod
    def _summary(
        headers: EmailMessage, uid: str, folder: str, uidvalidity: str | None = None
    ) -> dict[str, Any]:
        identifier = _raw_header_value(headers)
        try:
            identifier = _message_id(identifier)
        except MailValidationError:
            pass  # Read-only results may still display malformed identity evidence.
        return {
            "message_id": identifier,
            "folder": folder,
            "uidvalidity": uidvalidity,
            "uid": uid,
            "from": str(headers.get("From", "")),
            "to": str(headers.get("To", "")),
            "subject": str(headers.get("Subject", "")),
            "date": str(headers.get("Date", "")),
        }

    @staticmethod
    def _folders(connection: Any) -> list[str]:
        data = _checked(connection.list())
        folders = []
        all_folders = []
        for item in data:
            if item is None:
                continue
            if isinstance(item, tuple):
                item = item[0] + b"\r\n" + item[1]
            parser = _ImapValues(item)
            flags, _, name = parser.value(), parser.value(), parser.value()
            if isinstance(flags, list) and any(
                _string(flag).lower() == "\\noselect" for flag in flags
            ):
                continue
            folder = _decode_folder(name)
            if folder not in folders and folder not in all_folders:
                # Keep an ordinary mailbox as the representative of duplicate messages.
                target = (
                    all_folders
                    if isinstance(flags, list)
                    and any(_string(flag).lower() == "\\all" for flag in flags)
                    else folders
                )
                target.append(folder)
        folders.extend(all_folders)
        if len(folders) > MAX_THREAD_FOLDERS:
            raise MailError("Mailbox has too many folders for bounded thread lookup")
        return folders

    def search_mail(self, **kwargs: Any) -> dict[str, Any]:
        from .search import search_mail

        return search_mail(self, **kwargs)

    def count_mail(
        self,
        from_: str | None = None,
        to: str | None = None,
        subject: str | None = None,
        text: str | None = None,
        since: str | None = None,
        before: str | None = None,
        folder: str = "INBOX",
        unread_only: bool = False,
        exclude_subject: str | None = None,
        exclude_text: str | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from .index_status import attach_index_status
        from .search import count_mail

        result = count_mail(
            self,
            from_=from_,
            to=to,
            subject=subject,
            text=text,
            since=since,
            before=before,
            folder=folder,
            unread_only=unread_only,
            exclude_subject=exclude_subject,
            exclude_text=exclude_text,
            query=query,
        )
        # Counts use only live IMAP inventory/search, including partial timeout
        # replies; an incomplete header cache cannot affect them.
        return attach_index_status(self, result, depends_on_index=False, incomplete=False)

    def read_thread(
        self,
        message_id: str,
        include_thread: bool = True,
        *,
        prefer_html: bool = False,
        include_bcc: bool = False,
        include_flags: bool = False,
    ) -> dict[str, Any]:
        from .threading import read_thread

        return read_thread(
            self,
            message_id,
            include_thread=include_thread,
            prefer_html=prefer_html,
            include_bcc=include_bcc,
            include_flags=include_flags,
        )

    def list_attachments(self, message_id: str) -> dict[str, Any]:
        from .attachments import list_attachments

        return list_attachments(self, message_id)

    def read_attachment(self, message_id: str, attachment: int, offset: int = 0) -> dict[str, Any]:
        from .attachments import read_attachment

        return read_attachment(self, message_id, attachment, offset)

    def save_attachment(
        self, message_id: str, attachment: int, filename: str = ""
    ) -> dict[str, Any]:
        from .attachments import save_attachment

        return save_attachment(self, message_id, attachment, filename)

    def forward_message(
        self,
        message_id: str,
        to: str | list[str],
        cc: str | list[str] | None = None,
        *,
        attachments: list[int],
        note: str = "",
        from_address: str = "",
    ) -> dict[str, Any]:
        from .attachments import forward_message

        return forward_message(
            self,
            message_id,
            to,
            cc,
            attachments=attachments,
            note=note,
            from_address=from_address,
        )

    def local_attachments(self, names: list[str]) -> list[dict[str, Any]]:
        from .attachments import local_attachments

        return local_attachments(self, names)

    def rebuild_search_index(self, folder: str = "INBOX") -> dict[str, Any]:
        """Explicitly reset the account cache and warm one folder's headers.

        This local maintenance operation uses the ordinary read deadline and
        never fetches bodies. Interrupted work remains a usable partial cache.
        """
        from . import canonical
        from .search import _METADATA_BATCH_SIZE
        from .search_index import SearchIndex

        _mailbox(folder)
        self.search_index = SearchIndex(self.settings, rebuild=True)
        result = {"folder": folder, "indexed": 0, "available": self.search_index.enabled}
        if not self.search_index.enabled:
            return result
        deadline = self._deadline()
        with self._session(deadline=deadline) as (connection, _):
            attributes = canonical.mailbox_roles(connection)[0]
            folder = canonical.listed_name(attributes, folder)
            result["folder"] = folder
            state = self._select(connection, folder)
            if getattr(connection, "_search_selection", None) is None:
                raise MailError("Mailbox did not provide a stable search index identity")
            validity = connection._search_selection[1]
            uids = self._search(connection, ["ALL"])
            for count in self._index_header_batches(
                connection, uids, deadline=deadline, batch_size=_METADATA_BATCH_SIZE
            ):
                if not self.search_index.enabled:
                    break
                result["indexed"] += count
            if "\\all" in attributes.get(folder, set()):
                self.search_index.record_coverage(folder, validity, state, uids)
        result["available"] = self.search_index.enabled
        return result

    def _index_header_batches(
        self,
        connection: Any,
        uids: list[str],
        *,
        deadline: _Deadline,
        batch_size: int,
    ) -> Iterator[int]:
        """Shared read-only header warming for explicit and background rebuilds."""
        for start in range(0, len(uids), batch_size):
            metadata = self._metadata(
                connection,
                uids[start : start + batch_size],
                deadline=deadline,
                headers=True,
                bodystructure=False,
                internaldate=True,
                envelope=True,
                size=True,
            )
            yield len(metadata)

    def read_draft(self, draft_id: str) -> dict[str, Any]:
        """Read one complete, bounded Drafts message without changing flags."""
        from .search import _uidvalidity

        draft_id = _draft_message_id(draft_id)
        if not draft_id.isascii() or len(draft_id) > 998:
            raise MailValidationError("Send requires an ASCII draft Message-ID")
        maximum = MAX_DRAFT_BYTES
        with self._session() as (connection, _):
            self._select(connection, self.settings.drafts_folder)
            validity = _uidvalidity(connection)
            uids = self._header_search(connection, "Message-ID", draft_id)
            if len(uids) != 1:
                raise MailValidationError("Draft is missing or ambiguous")
            uid = uids[0]
            data = _checked(
                connection.uid(
                    "FETCH", uid, f"(UID RFC822.SIZE FLAGS BODY.PEEK[]<0.{maximum + 1}>)"
                )
            )
            raw = b" ".join(
                item[0] + b"\r\n" + item[1] if isinstance(item, tuple) else item
                for item in data
                if isinstance(item, (tuple, bytes))
            )
            parser = _ImapValues(raw)
            found = []
            while parser.position < len(raw) and raw[parser.position :].strip():
                sequence = parser.value()
                fields = parser.value()
                if (
                    not isinstance(sequence, bytes)
                    or not sequence.isdigit()
                    or not isinstance(fields, list)
                    or len(fields) % 2
                ):
                    raise MailError("Mailbox returned an incomplete draft")
                values = {
                    _string(fields[i]).upper(): fields[i + 1] for i in range(0, len(fields), 2)
                }
                if values.get("UID") != uid.encode("ascii"):
                    continue
                content = values.get("BODY[]<0>", values.get("BODY[]"))
                if content is None:
                    continue  # An unsolicited flag-only response is not draft content.
                flags = values.get("FLAGS")
                size = values.get("RFC822.SIZE")
                if (
                    not isinstance(content, bytes)
                    or not isinstance(size, bytes)
                    or not size.isdigit()
                    or not 0 < len(content) == int(size) <= maximum
                    or not isinstance(flags, list)
                    or b"\\DRAFT" not in [flag.upper() for flag in flags if isinstance(flag, bytes)]
                    or b"\\DELETED" in [flag.upper() for flag in flags if isinstance(flag, bytes)]
                ):
                    raise MailValidationError("Send requires a complete draft of at most 10 MiB")
                headers = BytesParser(policy=policy.default).parsebytes(content, headersonly=True)
                if headers.get_all("Message-ID") != [draft_id]:
                    raise MailValidationError("Draft identity changed or is ambiguous")
                found.append(content)
            if len(found) != 1:
                raise MailValidationError("Draft is missing or ambiguous")
            return {
                "message_id": draft_id,
                "folder": self.settings.drafts_folder,
                "uidvalidity": validity,
                "uid": uid,
                "raw": found[0],
            }

    def create_draft(
        self,
        to: str | list[str],
        cc: str | list[str] | None = None,
        *,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
        attachments: list[str] | None = None,
        attachments_from: str = "",
        attachment_indexes: list[int] | None = None,
        from_address: str = "",
        _deadline: _Deadline | None = None,
        _files: list[dict[str, Any]] | None = None,
    ) -> str:
        if from_address:
            check_from_address(self.settings, from_address)
        recipient = _addresses(to, required=True)
        carbon = _addresses(cc)
        _quote(subject)
        if not isinstance(body, str):
            raise MailValidationError("Draft body must be text")
        if in_reply_to is not None:
            in_reply_to = _message_id(in_reply_to)
        _mailbox(self.settings.drafts_folder)
        deadline = _deadline or self._deadline()
        files = _files
        if files is None and (attachments or attachments_from or attachment_indexes):
            from .attachments import draft_files

            # Everything refused here refuses before any draft exists.
            files = draft_files(
                self, attachments, attachments_from, attachment_indexes, deadline=deadline
            )
        references: list[str] = []
        if in_reply_to is not None:
            from .threading import _thread_folders

            # Parent references enrich the draft but must leave time to APPEND.
            # Use a separate connection so an interrupted lookup cannot leave
            # the APPEND command on a broken IMAP stream.
            lookup_deadline = _Deadline(
                max(0, (deadline.end - time.monotonic()) / 2), self.settings.timeout
            )
            try:
                with self._session(deadline=lookup_deadline) as (connection, _):
                    folders, _ = _thread_folders(connection)
                    parent_found = False
                    candidate_count = 0
                    for folder in folders:
                        self._select(connection, folder)
                        candidates = self._header_search(
                            connection, "Message-ID", _message_id_search(in_reply_to)
                        )
                        candidate_count += len(candidates)
                        if candidate_count > MAX_THREAD_CANDIDATES:
                            raise MailError("Reply lookup exceeds the candidate safety limit")
                        for uid in candidates:
                            headers = self._headers(connection, uid)
                            try:
                                parent_id = _message_id(_raw_header_value(headers))
                            except MailValidationError:
                                continue
                            if in_reply_to == parent_id:
                                references = _ids(str(headers.get("References", "")))
                                parent_found = True
                                break
                        if parent_found:
                            break
            except MailError:
                # Includes a lookup deadline or an unavailable lookup session.
                # In-Reply-To and References still identify the supplied parent.
                references = []
            if len(set([*references, in_reply_to])) > MAX_THREAD_IDS:
                raise MailError("Reply exceeds the reference safety limit")
        with self._session(deadline=deadline) as (connection, credentials):
            from .send_address import parse_addresses

            try:
                senders = parse_addresses(credentials.username, allow_display_names=True)
                if len(senders) != 1:
                    raise ValueError
            except (ValueError, IndexError):
                raise MailValidationError(
                    "Draft From must be one unambiguous ASCII address"
                ) from None
            message = EmailMessage(policy=policy.SMTP)
            sender = from_address or senders[0]
            message["From"] = sender
            message["To"] = recipient
            if carbon:
                message["Cc"] = carbon
            message["Subject"] = subject
            message["Date"] = formatdate(usegmt=True)
            message["Message-ID"] = make_msgid(domain=_message_id_domain(sender))
            if in_reply_to:
                message["In-Reply-To"] = in_reply_to
                message["References"] = " ".join(dict.fromkeys([*references, in_reply_to]))
            message.set_content(body)
            if files:
                from .attachments import add_files

                add_files(message, files)
            # Guarded send rejects folded sensitive headers. Serialize
            # our own headers without introducing continuation lines.
            raw = message.as_bytes(policy=policy.SMTP.clone(max_line_length=0))
            if files and len(raw) > MAX_DRAFT_BYTES:
                raise MailValidationError("Draft and attachments exceed the 10 MiB send limit")
            _checked(
                connection.append(_mailbox(self.settings.drafts_folder), r"(\Draft)", None, raw)
            )
            return str(message["Message-ID"])
