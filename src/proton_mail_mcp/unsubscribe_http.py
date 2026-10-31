"""Discover HTTPS/mailto unsubscribe targets and constrain HTTPS execution."""

import http.client
import ipaddress
import math
import queue
import re
import socket
import ssl
import threading
import time
import unicodedata
from contextlib import suppress
from email import policy
from email.errors import HeaderParseError
from email.header import decode_header
from email.parser import HeaderParser
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urlsplit

from .send_address import parse_addresses

# Reviewed ownership boundaries, not a guessed public-suffix list. Unknown sender
# domains require an exact hostname match. Add boundaries only through code review;
# private suffixes (for example github.io) must never be listed here.
REVIEWED_SENDER_DOMAINS = ("github.com", "proton.me", "example.com")

# Map a reviewed sender domain to that sender's reviewed ESP host/domain boundaries.
# Empty by default: a shared ESP is not trusted for every sender automatically.
ESP_DOMAINS_BY_SENDER: dict[str, tuple[str, ...]] = {}
MAX_TIMEOUT = 5.0
ONE_CLICK = "List-Unsubscribe=One-Click"
_LINK_LABEL = re.compile(r"unsubscribe|opt[\s_-]?out", re.IGNORECASE)
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_ENCODED_WORD = re.compile(r"=\?[^?\s]+\?[bBqQ]\?[^?\s]*\?=")
_MAILTO_QCHAR = re.compile(r"(?:[A-Za-z0-9._~!$'()*+,:;@-]|%[0-9a-fA-F]{2})*\Z")
# RFC 6068 requires these extra literals to be escaped. Accept common sender
# forms only where delimiters stay unambiguous: =/ in To, /? in field values.
_MAILTO_TO = re.compile(r"(?:[A-Za-z0-9._~!$'()*+,/:;@=-]|%[0-9a-fA-F]{2})*\Z")
_MAILTO_VALUE = re.compile(r"(?:[A-Za-z0-9._~!$'()*+,/:;?@-]|%[0-9a-fA-F]{2})*\Z")


class UnsubscribeRefused(ValueError):
    """A candidate fails the unsubscribe policy; diagnostics contain no URLs."""


class UnsubscribeHTTPError(RuntimeError):
    """The request failed; response bodies and remote diagnostics are withheld."""


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.href: str | None = None
        self.label: list[str] = []

    def _finish(self):
        if self.href and _LINK_LABEL.search(unquote(self.href) + " " + "".join(self.label)):
            self.links.append(self.href)
        self.href, self.label = None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._finish()
            self.href = dict(attrs).get("href")

    def handle_data(self, data):
        if self.href is not None:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == "a":
            self._finish()

    def close(self):
        super().close()
        self._finish()


def _header_urls(value: str) -> list[str]:
    """Read raw URI tokens; comments and encoded-words cannot supply syntax."""
    urls = []
    bare = []
    comment_depth = uri_depth = 0
    escaped = False
    start = 0

    def finish_bare():
        uri = "".join(bare).strip(" \t\r\n")
        bare.clear()
        if re.match(r"(?:https|mailto):", uri, re.IGNORECASE) and not _ENCODED_WORD.search(uri):
            urls.append(uri)

    # Keep offsets intact and encoded-words opaque, even if Q text contains
    # literal delimiters. URI syntax never needs RFC 2047 decoding.
    syntax = _ENCODED_WORD.sub(lambda match: " " * len(match[0]), value)
    for index, char in enumerate(syntax):
        if comment_depth:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "(":
                comment_depth += 1
            elif char == ")":
                comment_depth -= 1
        elif uri_depth:
            if char == "<":
                uri_depth += 1
            elif char == ">":
                uri_depth -= 1
                if not uri_depth:
                    # RFC 2369 ignores whitespace inside angle brackets. Remove
                    # legal header folds, but leave stray CR/LF for URL validation.
                    uri = re.sub(r"\r?\n[ \t]+", "", value[start:index])
                    uri = re.sub(r"[ \t]", "", uri)
                    if uri and "<" not in uri and not _ENCODED_WORD.search(uri):
                        urls.append(uri)
        elif char == "(":
            if bare and bare[-1].isspace():
                finish_bare()
            else:
                # Do not turn an ambiguous bare URI into an executable prefix.
                bare.clear()
            comment_depth = 1
        elif char == "<":
            bare.clear()
            uri_depth = 1
            start = index + 1
        elif char == "," and (
            not "".join(bare).strip()
            or re.match(r"\s*(?:<|https:|mailto:)", syntax[index + 1 :], re.IGNORECASE)
        ):
            # A mailto recipient list or URI query may itself contain commas.
            finish_bare()
        else:
            bare.append(value[index])
    finish_bare()
    return urls


def extract(headers, htmls=()) -> dict[str, list[str]]:
    """Return original header values, their URIs, and unsubscribe HTML anchors."""

    def values(name):
        if hasattr(headers, "raw_items"):
            return [value for key, value in headers.raw_items() if key.lower() == name.lower()]
        result = []
        for key, value in headers.items():
            if key.lower() == name.lower():
                result.extend(value if isinstance(value, list) else [str(value)])
        return result

    unsubscribe = values("List-Unsubscribe")
    html_links = []
    for html in htmls:
        links = _Links()
        links.feed(html)
        links.close()
        html_links.extend(links.links)
    return {
        "list_unsubscribe": unsubscribe,
        "list_unsubscribe_post": values("List-Unsubscribe-Post"),
        "header_urls": list(
            dict.fromkeys(url for value in unsubscribe for url in _header_urls(value))
        ),
        "html_links": list(dict.fromkeys(html_links)),
    }


def _within(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _sender_domain(sender: str) -> str:
    if any(char in sender for char in "\r\n"):
        raise UnsubscribeRefused("A valid sender address is required")
    header = HeaderParser(policy=policy.default).parsestr("From: " + sender + "\n\n")["From"]
    if header.defects or len(header.addresses) != 1 or not header.addresses[0].username:
        raise UnsubscribeRefused("A single valid sender address is required")
    host = header.addresses[0].domain.lower()
    if "." not in host or not all(_HOST.fullmatch(label) for label in host.split(".")):
        raise UnsubscribeRefused("A valid sender domain is required")
    return next((domain for domain in REVIEWED_SENDER_DOMAINS if _within(host, domain)), host)


def _url_host(url: str) -> str:
    try:
        if (
            not url.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)
            or any(char in url for char in '<>"\\')
            or re.search(r"%(?![0-9a-fA-F]{2})", url)
        ):
            raise ValueError
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
            or len(host) > 253
            or "." not in host
            or not all(_HOST.fullmatch(label) for label in host.split("."))
        ):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return host
    except (ValueError, TypeError, AttributeError):
        pass
    raise UnsubscribeRefused("Unsubscribe requires HTTPS without credentials on a DNS host")


def parse_mailto(url: str) -> dict[str, Any]:
    """Decode RFC 6068 To/Subject/Body without sending or accepting extra headers."""

    def decode(value, characters=_MAILTO_QCHAR):
        if not characters.fullmatch(value):
            raise UnsubscribeRefused("Invalid mailto encoding")
        # Unlike form decoding, RFC 6068 preserves literal '+' characters.
        return unquote(value, encoding="utf-8", errors="strict")

    try:
        if not isinstance(url, str) or not url.isascii():
            raise UnsubscribeRefused("Invalid mailto URI")
        scheme, separator, remainder = url.partition(":")
        if scheme.lower() != "mailto" or not separator:
            raise UnsubscribeRefused("Invalid mailto scheme")
        if remainder.startswith("//"):
            raise UnsubscribeRefused("Mailto cannot contain an authority")
        raw_to, has_query, query = remainder.partition("?")
        recipients = parse_addresses(decode(raw_to, _MAILTO_TO)) if raw_to else []
        fields = {}
        for field in query.split("&") if has_query else ():
            name, equals, value = field.partition("=")
            name = decode(name).lower()
            if name not in ("to", "subject", "body"):
                raise UnsubscribeRefused("Mailto supports only to, subject and body fields")
            if not equals or name in fields:
                raise UnsubscribeRefused("Invalid or duplicate mailto field")
            fields[name] = decode(value, _MAILTO_VALUE)
        if "to" in fields:
            recipients.extend(parse_addresses(fields["to"]))
        if not recipients:
            raise UnsubscribeRefused("Mailto requires at least one recipient")
        subject = "".join(
            value.decode(charset or "ascii", errors="strict") if isinstance(value, bytes) else value
            for value, charset in decode_header(fields.get("subject", ""))
        )
        body = fields.get("body", "")
        if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in subject):
            raise UnsubscribeRefused("Mailto subject cannot contain control characters")
        if any(
            unicodedata.category(char) in {"Cc", "Cf", "Cs"} and char not in "\r\n\t"
            for char in body
        ) or re.search(r"(?<!\r)\n|\r(?!\n)", body):
            raise UnsubscribeRefused("Mailto body requires CRLF line breaks and no other controls")
        return {
            "method": "MAILTO",
            "url": url,
            "host": ", ".join(
                sorted({address.rsplit("@", 1)[1].lower() for address in recipients})
            ),
            "to": list(dict.fromkeys(recipients)),
            "subject": subject,
            "body": body,
        }
    except UnsubscribeRefused:
        raise
    except (ValueError, LookupError, HeaderParseError):
        raise UnsubscribeRefused("Invalid mailto recipients or encoding") from None


def select_target(
    sender: str,
    extracted: dict[str, list[str]],
    *,
    enable_send: bool = True,
    send_max_recipients: int | None = None,
) -> dict[str, Any]:
    """Prefer header HTTPS, then header mailto, then permitted body HTTPS links."""
    domain = _sender_domain(sender)
    header_urls = extracted.get("header_urls", [])
    # Ambiguous/duplicate Post fields fail closed rather than enabling one-click.
    post_fields = extracted.get("list_unsubscribe_post", [])
    one_click = len(post_fields) == 1 and bool(
        re.fullmatch(
            r"[ \t]*List-Unsubscribe[ \t]*=[ \t]*One-Click[ \t]*",
            re.sub(r"\r?\n[ \t]+", " ", post_fields[0]),
        )
    )
    allowed = ESP_DOMAINS_BY_SENDER.get(domain, ())

    def https_target(url, *, header):
        try:
            host = _url_host(url)
        except UnsubscribeRefused:
            return None
        method = "POST" if one_click and header else "GET"
        # For an unreviewed domain, only the exact sender host is established.
        same_sender = host == domain or (
            domain in REVIEWED_SENDER_DOMAINS and _within(host, domain)
        )
        if method == "POST" or same_sender or any(_within(host, esp) for esp in allowed):
            return {"method": method, "url": url, "host": host}
        return None

    for url in header_urls:
        target = https_target(url, header=True)
        if target:
            return target
    mailto_error = None
    for url in header_urls:
        if url.lower().startswith("mailto:"):
            try:
                target = parse_mailto(url)
                if enable_send is not True:
                    raise UnsubscribeRefused(
                        "Guarded send is disabled; mailto unsubscribe requires send approval"
                    )
                if send_max_recipients is not None and len(target["to"]) > send_max_recipients:
                    raise UnsubscribeRefused(
                        "Mailto unsubscribe exceeds the configured recipient cap"
                    )
                return target
            except UnsubscribeRefused as error:
                mailto_error = str(error)
    for url in extracted.get("html_links", []):
        target = https_target(url, header=False)
        if target:
            return target
    reason = "No permitted unsubscribe method found (HTTPS or mailto)"
    if mailto_error:
        reason += ": " + mailto_error
    raise UnsubscribeRefused(reason)


def _resolve(host: str, deadline: float):
    """Bound the caller's DNS wait and reject every non-public answer."""
    result: queue.Queue = queue.Queue(maxsize=1)

    def lookup():
        try:
            result.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except OSError:
            result.put(None)

    worker = threading.Thread(target=lookup, daemon=True)
    worker.start()
    try:
        answers = result.get(timeout=max(0, deadline - time.monotonic()))
    except queue.Empty:
        raise UnsubscribeHTTPError("Unsubscribe DNS lookup timed out") from None
    if not answers:
        raise UnsubscribeHTTPError("Unsubscribe DNS lookup failed")
    for family, _, _, _, sockaddr in answers:
        address = ipaddress.ip_address(sockaddr[0])
        if (
            family not in (socket.AF_INET, socket.AF_INET6)
            or not address.is_global
            or address.is_multicast
            or address.is_reserved
            or getattr(address, "is_site_local", False)
            or getattr(address, "ipv4_mapped", None) is not None
            or getattr(address, "sixtofour", None) is not None
            or getattr(address, "teredo", None) is not None
        ):
            raise UnsubscribeHTTPError("Unsubscribe endpoint must resolve only to public addresses")
    return answers[0]


def perform(target: dict[str, str], timeout: float = MAX_TIMEOUT) -> int:
    """Send one request, with verified TLS, no redirects/proxy/cookies, and no body read."""
    host = _url_host(target["url"])
    method = target["method"]
    if target.get("host") != host or method not in ("GET", "POST"):
        raise UnsubscribeRefused("Invalid unsubscribe target")
    if not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT:
        raise UnsubscribeRefused("Unsubscribe timeout must be positive and at most five seconds")
    deadline = time.monotonic() + timeout
    family, socktype, proto, _, sockaddr = _resolve(host, deadline)
    https_connection = http.client.HTTPSConnection(host, timeout=timeout)
    lock = threading.Lock()

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError
        return value

    def interrupt():
        with lock:
            if https_connection.sock is not None:
                with suppress(OSError):
                    https_connection.sock.shutdown(socket.SHUT_RDWR)

    timer = threading.Timer(max(0, deadline - time.monotonic()), interrupt)
    timer.daemon = True
    try:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        https_connection.sock = socket.socket(family, socktype, proto)
        timer.start()
        https_connection.sock.settimeout(remaining())
        https_connection.sock.connect(sockaddr)  # Numeric address: no second DNS lookup.
        with lock:
            https_connection.sock = context.wrap_socket(
                https_connection.sock, server_hostname=host, do_handshake_on_connect=False
            )
        https_connection.sock.settimeout(remaining())
        https_connection.sock.do_handshake()
        parsed = urlsplit(target["url"])
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        body = ONE_CLICK.encode("ascii") if method == "POST" else None
        headers = {"Connection": "close"}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        https_connection.sock.settimeout(remaining())
        https_connection.request(method, path, body=body, headers=headers)
        response = https_connection.getresponse()
        remaining()
        status = response.status
        response.close()
        if 300 <= status < 400:
            raise UnsubscribeHTTPError("Unsubscribe redirects are refused")
        if not 200 <= status < 300:
            raise UnsubscribeHTTPError("Unsubscribe endpoint returned a non-success status")
        return status
    except (OSError, http.client.HTTPException, ValueError):
        raise UnsubscribeHTTPError("Unsubscribe HTTPS request failed or timed out") from None
    finally:
        timer.cancel()
        with lock:
            https_connection.close()
