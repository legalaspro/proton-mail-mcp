"""Gluon's UID SEARCH grammar and matching, ported for offline tests.

The grammar follows gluon imap/command/search.go (dev 2561acf) on RFC 3501
tokens: one SP between keys, "(" and ")" directly around a key list, quoted
strings that escape only backslash and double quote, literals as {N} CRLF
octets, two-digit date days, and nesting limited to depth 64. Gluon itself
accepts 8-bit octets in quoted strings; this port refuses them, and CR, LF and
NUL, because the client must send non-ASCII values only as literals.

Matching follows gluon internal/state/mailbox_search.go: FROM, TO, CC, BCC,
SUBJECT and HEADER compare the first raw (undecoded) header value, TEXT the whole
raw message and BODY its body, lowercased; SINCE, BEFORE and ON compare the UTC
day of INTERNALDATE; SEEN and UNSEEN read the flags; a key list is an AND.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from test_mail import FakeImap, internaldate

MAX_DEPTH = 64
_FLAG_KEYS = {
    "ANSWERED": ("\\answered", True),
    "UNANSWERED": ("\\answered", False),
    "DELETED": ("\\deleted", True),
    "UNDELETED": ("\\deleted", False),
    "DRAFT": ("\\draft", True),
    "UNDRAFT": ("\\draft", False),
    "FLAGGED": ("\\flagged", True),
    "UNFLAGGED": ("\\flagged", False),
    "SEEN": ("\\seen", True),
    "UNSEEN": ("\\seen", False),
}
_STRING_KEYS = {"FROM", "TO", "CC", "BCC", "SUBJECT", "TEXT", "BODY"}
_DATE_KEYS = {"SINCE", "BEFORE", "ON"}
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DATE = re.compile(rb"(\d{2})-([A-Za-z]{3})-(\d{4})")
_ASTRING_SPECIALS = b' (){%*"\\'


class SearchSyntaxError(ValueError):
    """The command is not a SEARCH that gluon (with 7-bit quoted strings) accepts."""


class _Parser:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def peek(self) -> bytes:
        return self.data[self.pos : self.pos + 1]

    def take(self, expected: bytes) -> None:
        if not self.data.startswith(expected, self.pos):
            raise SearchSyntaxError(f"expected {expected!r} at offset {self.pos}")
        self.pos += len(expected)

    def word(self) -> str:
        start = self.pos
        while self.pos < len(self.data) and chr(self.data[self.pos]).isascii():
            if not chr(self.data[self.pos]).isalpha():
                break
            self.pos += 1
        if start == self.pos:
            raise SearchSyntaxError(f"expected a search key at offset {start}")
        return self.data[start : self.pos].decode("ascii").upper()

    def astring(self) -> bytes:
        if self.peek() == b'"':
            return self.quoted()
        if self.peek() == b"{":
            return self.literal()
        start = self.pos
        while (
            self.pos < len(self.data)
            and 32 < self.data[self.pos] < 127
            and self.data[self.pos] not in _ASTRING_SPECIALS
        ):
            self.pos += 1
        if start == self.pos:
            raise SearchSyntaxError(f"expected an astring at offset {start}")
        return self.data[start : self.pos]

    def quoted(self) -> bytes:
        self.take(b'"')
        out = bytearray()
        while True:
            if self.pos >= len(self.data):
                raise SearchSyntaxError("unterminated quoted string")
            char = self.data[self.pos]
            if char == ord('"'):
                self.pos += 1
                return bytes(out)
            if char == ord("\\"):
                escaped = self.data[self.pos + 1 : self.pos + 2]
                if escaped not in (b'"', b"\\"):
                    raise SearchSyntaxError('only \\\\ and \\" may be escaped')
                out += escaped
                self.pos += 2
                continue
            if char in (0, 10, 13) or char > 127:
                raise SearchSyntaxError("quoted strings carry 7-bit text without CR or LF")
            out.append(char)
            self.pos += 1

    def literal(self) -> bytes:
        self.take(b"{")
        start = self.pos
        while self.peek().isdigit():
            self.pos += 1
        if start == self.pos:
            raise SearchSyntaxError("literal without length")
        size = int(self.data[start : self.pos])
        self.take(b"}\r\n")
        value = self.data[self.pos : self.pos + size]
        if size < 1 or len(value) != size:
            raise SearchSyntaxError("truncated literal")
        self.pos += size
        return value

    def date(self) -> date:
        quoted = self.peek() == b'"'
        if quoted:
            self.pos += 1
        match = _DATE.match(self.data, self.pos)
        if match is None or match.group(2).decode().lower() not in _MONTHS:
            raise SearchSyntaxError(f"expected dd-Mon-yyyy at offset {self.pos}")
        self.pos = match.end()
        if quoted:
            self.take(b'"')
        day, month, year = match.groups()
        return date(int(year), _MONTHS.index(month.decode().lower()) + 1, int(day))

    def sequence(self) -> str:
        start = self.pos
        while self.pos < len(self.data) and self.data[self.pos] in b"0123456789:*,":
            self.pos += 1
        if start == self.pos:
            raise SearchSyntaxError(f"expected a sequence set at offset {start}")
        return self.data[start : self.pos].decode("ascii")

    def key(self, depth: int) -> tuple:
        if depth > MAX_DEPTH:
            raise SearchSyntaxError("maximum search key nesting depth exceeded")
        if self.peek() == b"(":
            self.pos += 1
            keys = [self.key(depth + 1)]
            while self.peek() == b" ":
                self.pos += 1
                keys.append(self.key(depth + 1))
            self.take(b")")
            return ("AND", tuple(keys))
        if self.peek().isdigit() or self.peek() == b"*":
            return ("SEQ", self.sequence())
        word = self.word()
        if word == "ALL":
            return ("ALL",)
        if word in _FLAG_KEYS:
            return (word,)
        if word == "NOT":
            self.take(b" ")
            return ("NOT", self.key(depth + 1))
        if word == "OR":
            self.take(b" ")
            left = self.key(depth + 1)
            self.take(b" ")
            return ("OR", left, self.key(depth + 1))
        if word in _STRING_KEYS:
            self.take(b" ")
            return (word, self.astring().decode("utf-8"))
        if word in _DATE_KEYS:
            self.take(b" ")
            return (word, self.date())
        if word == "HEADER":
            self.take(b" ")
            name = self.astring().decode("ascii")
            self.take(b" ")
            return ("HEADER", name, self.astring().decode("utf-8"))
        if word == "UID":
            self.take(b" ")
            return ("UID", self.sequence())
        raise SearchSyntaxError(f"unknown search key {word}")


def parse_search(command: bytes) -> tuple[str | None, list[tuple]]:
    """Parse ``SEARCH [CHARSET astring] 1*(SP search-key)``; return charset and keys."""
    parser = _Parser(command)
    parser.take(b"SEARCH ")
    charset = None
    if command[parser.pos : parser.pos + 8].upper() == b"CHARSET ":
        parser.pos += 8
        charset = parser.astring().decode("ascii")
        parser.take(b" ")
    keys = [parser.key(0)]
    while parser.pos < len(command):
        parser.take(b" ")
        keys.append(parser.key(0))
    return charset, keys


def depth(key: tuple) -> int:
    """Gluon's nesting depth of one parsed key: NOT, OR and lists add a level."""
    if key[0] == "NOT":
        return 1 + depth(key[1])
    if key[0] == "OR":
        return 1 + max(depth(key[1]), depth(key[2]))
    if key[0] == "AND":
        return 1 + max(map(depth, key[1]))
    return 0


@dataclass
class Mail:
    uid: int
    sequence: int
    headers: bytes
    body: bytes
    internaldate: datetime
    flags: set[str] = field(default_factory=set)

    @classmethod
    def from_fake(cls, uid: str, sequence: int, item) -> "Mail":
        stamp = datetime.strptime(internaldate(item), "%d-%b-%Y %H:%M:%S %z")
        return cls(
            int(uid),
            sequence,
            item.headers,
            b"".join(item.sections.values()),
            stamp,
            {flag.lower() for flag in getattr(item, "flags", ())},
        )

    def header(self, name: str) -> str:
        """gluon rfc822.Header.Get: the first field's raw value, unfolded."""
        value = None
        for line in self.headers.split(b"\r\n"):
            if value is not None:
                if line[:1] in (b" ", b"\t"):
                    value += b" " + line.strip()
                    continue
                break
            key, separator, rest = line.partition(b":")
            if separator and key.strip().lower() == name.lower().encode("ascii"):
                value = rest.strip()
        return (value or b"").decode("utf-8", "replace")


def _in_set(spec: str, number: int, top: int) -> bool:
    for part in spec.split(","):
        low, _, high = part.partition(":")
        low_value = top if low == "*" else int(low)
        high_value = low_value if not high else top if high == "*" else int(high)
        if min(low_value, high_value) <= number <= max(low_value, high_value):
            return True
    return False


def evaluate(key: tuple, mail: Mail, top_uid: int = 0, top_sequence: int = 0) -> bool:
    kind = key[0]
    if kind == "ALL":
        return True
    if kind in _FLAG_KEYS:
        flag, present = _FLAG_KEYS[kind]
        return (flag in mail.flags) is present
    if kind == "NOT":
        return not evaluate(key[1], mail, top_uid, top_sequence)
    if kind == "OR":
        return evaluate(key[1], mail, top_uid, top_sequence) or evaluate(
            key[2], mail, top_uid, top_sequence
        )
    if kind == "AND":
        return all(evaluate(child, mail, top_uid, top_sequence) for child in key[1])
    if kind == "TEXT":
        raw = (mail.headers + b"\r\n" + mail.body).decode("utf-8", "replace")
        return key[1].lower() in raw.lower()
    if kind == "BODY":
        return key[1].lower() in mail.body.decode("utf-8", "replace").lower()
    if kind in _STRING_KEYS:
        return key[1].lower() in mail.header(kind.title()).lower()
    if kind == "HEADER":
        return key[2].lower() in mail.header(key[1]).lower()
    day = mail.internaldate.astimezone(timezone.utc).date()
    if kind == "SINCE":
        return day >= key[1]
    if kind == "BEFORE":
        return day < key[1]
    if kind == "ON":
        return day == key[1]
    if kind == "UID":
        return _in_set(key[1], mail.uid, top_uid)
    if kind == "SEQ":
        return _in_set(key[1], mail.sequence, top_sequence)
    raise AssertionError(f"unhandled key {key!r}")


def command_bytes(args, literal: bytes | None) -> bytes:
    """The octets imaplib sends for ``uid("SEARCH", *args)``, literal inline."""
    raw = b"SEARCH " + b" ".join(
        arg if isinstance(arg, bytes) else arg.encode("ascii") for arg in args
    )
    if literal is not None:
        raw += b" {%d}\r\n" % len(literal) + literal
    return raw


def search(command: bytes, mailbox: dict) -> list[str]:
    """UIDs of ``mailbox`` (uid -> FakeMessage) that gluon would return."""
    charset, keys = parse_search(command)
    if charset is not None and charset.upper() not in ("UTF-8", "US-ASCII"):
        raise SearchSyntaxError("unsupported charset")
    ordered = sorted(mailbox, key=int)
    mails = [Mail.from_fake(uid, index, mailbox[uid]) for index, uid in enumerate(ordered, 1)]
    top = int(ordered[-1]) if ordered else 0
    return [
        str(mail.uid)
        for mail in mails
        if all(evaluate(key, mail, top, len(ordered)) for key in keys)
    ]


class GluonSearchImap(FakeImap):
    """FakeImap whose UID SEARCH is parsed and matched as gluon does.

    ``searches`` records each command's octets; a command gluon would reject is
    answered BAD and recorded in ``rejected``.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.searches = []
        self.rejected = []

    def uid(self, command, *args):
        if command != "SEARCH":
            return super().uid(command, *args)
        self.calls.append((command, *args))
        literal, self.literal = self.literal, None
        if literal is not None:
            self.search_literals.append(literal)
        raw = command_bytes(args, literal)
        self.searches.append(raw)
        try:
            found = search(raw, self.folders[self.current])
        except SearchSyntaxError as error:
            self.rejected.append((raw, str(error)))
            return "BAD", [b"invalid SEARCH"]
        return "OK", [" ".join(found).encode()]
