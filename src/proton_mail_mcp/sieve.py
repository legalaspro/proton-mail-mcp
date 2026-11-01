"""Render one reviewed Proton Sieve filter exactly, and prove any script matches it.

Pure code: no mail, network or log access. The guarded flow in sieve_filters.py
(and the saved rules planned as backlog P-E) build a SieveFilter, show its
render_script() text in the preview, bind that text in the confirmation token and
hand exactly that text to a backend. verify_script() parses a script and
refuses it unless it is precisely the script for its filter: no other command,
test, extension, target or flag can reach a backend.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .cleanup_folders import _real_folder_name
from .mail import MailValidationError

MAX_CONDITIONS = 10
MAX_VALUE_LENGTH = 500
MAX_NAME_LENGTH = 100
MAX_SCRIPT_BYTES = 64 * 1024
# from and to match the address part, as Proton's filter editor writes them.
_TESTS = {"from": ("address", "From"), "to": ("address", "To"), "subject": ("header", "Subject")}
_OPERATORS = ("contains", "is")
_MATCHES = {"all": "allof", "any": "anyof"}
# Proton's editor writes these requirements, its spam guard and this comparator
# into every filter it generates (WebClients packages/sieve, toSieveTree.helpers.ts).
_REQUIRE = (
    "include",
    "environment",
    "variables",
    "relational",
    "comparator-i;ascii-numeric",
    "spamtest",
)
_ACTION_REQUIRE = ("fileinto", "imap4flags")
_COMPARATOR = "i;unicode-casemap"
_SEEN = "\\Seen"
# A fileinto name equal to a Proton system mailbox could file into that mailbox.
_SYSTEM_NAMES = frozenset(
    {
        "inbox",
        "draft",
        "drafts",
        "sent",
        "starred",
        "archive",
        "spam",
        "trash",
        "all mail",
        "almost all mail",
        "all drafts",
        "all sent",
        "scheduled",
        "snoozed",
        "outbox",
    }
)
# Controls (CR, LF, NUL, tab), format characters (bidi overrides, zero-width),
# surrogates, private use, unassigned code points and line/paragraph separators.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# Checked in the Bridge, gluon and go-proton-api sources; see docs/rules.md, Sieve filters.
UNAVAILABLE_REASON = (
    "Proton Mail Bridge offers mail clients only IMAP and SMTP, with no ManageSieve, "
    "Sieve or filter interface, and this server has no other apply path"
)


@dataclass(frozen=True)
class Condition:
    field: str  # from, to or subject
    operator: str  # contains or is
    value: str


@dataclass(frozen=True)
class SieveFilter:
    name: str
    folder: str  # canonical real folder, e.g. Folders/GitHub
    conditions: tuple[Condition, ...]
    match: str = "all"
    mark_read: bool = False

    def params(self) -> dict[str, Any]:
        """The canonical request form: what previews show and tokens bind."""
        return {
            "name": self.name,
            "folder": self.folder,
            "conditions": [
                {"field": row.field, row.operator: row.value} for row in self.conditions
            ],
            "match": self.match,
            "mark_read": self.mark_read,
        }


def _text(value: Any, what: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or value != value.strip()
        or any(unicodedata.category(char) in _REFUSED_CATEGORIES for char in value)
    ):
        raise MailValidationError(
            f"{what} must be 1 to {limit} characters of single-line text without "
            "surrounding spaces, control or invisible characters"
        )
    if "${" in value:
        # Proton's spam guard requires "variables": Sieve would expand ${...}.
        raise MailValidationError(f"{what} must not contain ${{")
    return value


def sieve_folder(folder: Any) -> str:
    """The fileinto path for a canonical Folders/ name: Folders/Work/A files into Work/A."""
    if not isinstance(folder, str) or not folder.startswith("Folders/"):
        raise MailValidationError(
            "Sieve filters file only into a real folder: an existing canonical Folders/ name"
        )
    if not _real_folder_name(folder):
        raise MailValidationError("Sieve filter folder must be a valid path under Folders/")
    path = _text(folder.removeprefix("Folders/"), "Sieve filter folder", 492)
    if path.split("/", 1)[0].casefold() in _SYSTEM_NAMES:
        raise MailValidationError(
            "Sieve filter folder must not be named like a Proton system folder"
        )
    return path


def filter_spec(
    name: Any, folder: Any, conditions: Any, match: Any = "all", mark_read: Any = False
) -> SieveFilter:
    """Validate one requested filter; every refusal happens before any mailbox access."""
    name = _text(name, "Filter name", MAX_NAME_LENGTH)
    sieve_folder(folder)
    if not isinstance(match, str) or match not in _MATCHES:
        raise MailValidationError("Filter match must be all or any")
    if type(mark_read) is not bool:
        raise MailValidationError("mark_read must be a boolean")
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= MAX_CONDITIONS:
        raise MailValidationError(f"A filter needs 1 to {MAX_CONDITIONS} conditions")
    parsed = []
    for condition in conditions:
        if (
            not isinstance(condition, dict)
            or set(condition) - {"field", *_OPERATORS}
            or len(condition) != 2
            or not isinstance(condition.get("field"), str)
            or condition["field"] not in _TESTS
        ):
            raise MailValidationError(
                'Each condition is {"field": "from", "to" or "subject", "contains" or "is": text}'
            )
        operator = next(key for key in _OPERATORS if key in condition)
        value = _text(condition[operator], "Filter condition values", MAX_VALUE_LENGTH)
        parsed.append(Condition(condition["field"], operator, value))
    if len(set(parsed)) != len(parsed):
        raise MailValidationError("Filter conditions must not repeat")
    return SieveFilter(name, folder, tuple(parsed), match, mark_read)


def _quoted(value: str) -> str:
    # RFC 5228 quoted string: only backslash and double quote need escapes.
    # Validation already refused CR, LF and every other control character.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _quoted_list(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_quoted(value) for value in values) + "]"


def render_script(spec: SieveFilter) -> str:
    """The exact script for one validated filter, verified by parsing it back."""
    if filter_spec(**spec.params()) != spec:
        raise MailValidationError("Sieve filter is not in its validated form")
    tests = []
    for row in spec.conditions:
        test, header = _TESTS[row.field]
        part = ":all " if test == "address" else ""
        tests.append(
            f"{test} {part}:comparator {_quoted(_COMPARATOR)} :{row.operator} "
            f"{_quoted(header)} {_quoted(row.value)}"
        )
    # Flag first: fileinto then files the message with \Seen under either
    # reading of RFC 5232 (flags taken at fileinto, or when actions run).
    actions = [f"addflag {_quoted(_SEEN)};"] if spec.mark_read is True else []
    actions.append(f"fileinto {_quoted(sieve_folder(spec.folder))};")
    lines = [
        f"require {_quoted_list(_REQUIRE)};",
        f"require {_quoted_list(_ACTION_REQUIRE)};",
        "",
        "# Never run on spam, like the filters Proton's editor generates.",
        'if allof (environment :matches "vnd.proton.spam-threshold" "*",',
        '          spamtest :value "ge" :comparator "i;ascii-numeric" "${1}") {',
        "    return;",
        "}",
        "",
        f"if {_MATCHES[spec.match]} (",
        ",\n".join("    " + test for test in tests),
        ") {",
        *("    " + action for action in actions),
        "}",
        "",
    ]
    script = "\n".join(lines)
    verify_script(script, spec)
    return script


def script_sha256(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


_LEXEMES = re.compile(
    r"""
    (?P<space>[ \t\r\n]+)
  | (?P<comment>\#[^\r\n]*|/\*.*?\*/)
  | (?P<string>"(?:[^"\\]|\\.)*")
  | (?P<tag>:[A-Za-z_][A-Za-z0-9_]*)
  | (?P<identifier>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<number>[0-9]+[KMG]?)
  | (?P<special>[;{}()\[\],])
    """,
    re.VERBOSE | re.DOTALL,
)
_MAX_DEPTH = 6


class _SieveSyntaxError(Exception):
    pass


class _Parser:
    """RFC 5228 grammar without multi-line strings; nodes are plain tuples."""

    def __init__(self, script: str):
        self.tokens = []
        position = 0
        while position < len(script):
            lexeme = _LEXEMES.match(script, position)
            if lexeme is None:
                raise _SieveSyntaxError
            position = lexeme.end()
            if lexeme.lastgroup == "string":
                value = re.sub(r"\\(.)", r"\1", lexeme.group()[1:-1], flags=re.DOTALL)
                self.tokens.append(("string", value))
            elif lexeme.lastgroup not in {"space", "comment"}:
                self.tokens.append((lexeme.lastgroup, lexeme.group()))
        self.index = 0

    def _peek(self) -> tuple[str | None, str | None]:
        return self.tokens[self.index] if self.index < len(self.tokens) else (None, None)

    def _take(self, kind: str, value: str | None = None) -> str:
        found, text = self._peek()
        if found != kind or (value is not None and text != value):
            raise _SieveSyntaxError
        self.index += 1
        return text

    def script(self) -> tuple:
        commands = self._commands(0)
        if self.index != len(self.tokens):
            raise _SieveSyntaxError
        return commands

    def _commands(self, depth: int) -> tuple:
        if depth > _MAX_DEPTH:
            raise _SieveSyntaxError
        commands = []
        while self._peek()[0] is not None and self._peek() != ("special", "}"):
            name = self._take("identifier")
            arguments, test = self._arguments(depth)
            block = None
            if self._peek() == ("special", "{"):
                self._take("special", "{")
                block = self._commands(depth + 1)
                self._take("special", "}")
            else:
                self._take("special", ";")
            commands.append(("command", name, arguments, test, block))
        return tuple(commands)

    def _arguments(self, depth: int) -> tuple[tuple, Any]:
        arguments = []
        while True:
            kind, text = self._peek()
            if kind in {"tag", "string", "number"}:
                self.index += 1
                arguments.append((kind, text))
            elif (kind, text) == ("special", "["):
                self.index += 1
                values = [self._take("string")]
                while self._peek() == ("special", ","):
                    self.index += 1
                    values.append(self._take("string"))
                self._take("special", "]")
                arguments.append(("list", tuple(values)))
            else:
                break
        if self._peek() == ("special", "("):
            self.index += 1
            tests = [self._test(depth + 1)]
            while self._peek() == ("special", ","):
                self.index += 1
                tests.append(self._test(depth + 1))
            self._take("special", ")")
            return tuple(arguments), tuple(tests)
        if self._peek()[0] == "identifier":
            return tuple(arguments), self._test(depth + 1)
        return tuple(arguments), None

    def _test(self, depth: int) -> tuple:
        if depth > _MAX_DEPTH:
            raise _SieveSyntaxError
        name = self._take("identifier")
        arguments, tests = self._arguments(depth)
        return ("test", name, arguments, tests)


def _command(name: str, *arguments: tuple, test: Any = None, block: Any = None) -> tuple:
    return ("command", name, arguments, test, block)


def _test(name: str, *arguments: tuple, tests: Any = None) -> tuple:
    return ("test", name, arguments, tests)


def _expected(spec: SieveFilter) -> tuple:
    """Build the filter's syntax tree directly from its fields, never from text."""

    def string(value: str) -> tuple:
        return ("string", value)

    def tag(name: str) -> tuple:
        return ("tag", ":" + name)

    conditions = []
    for row in spec.conditions:
        test, header = _TESTS[row.field]
        part = (tag("all"),) if test == "address" else ()
        conditions.append(
            _test(
                test,
                *part,
                tag("comparator"),
                string(_COMPARATOR),
                tag(row.operator),
                string(header),
                string(row.value),
            )
        )
    actions = (_command("addflag", string(_SEEN)),) if spec.mark_read is True else ()
    actions += (_command("fileinto", string(sieve_folder(spec.folder))),)
    spam = _test(
        "allof",
        tests=(
            _test("environment", tag("matches"), string("vnd.proton.spam-threshold"), string("*")),
            _test(
                "spamtest",
                tag("value"),
                string("ge"),
                tag("comparator"),
                string("i;ascii-numeric"),
                string("${1}"),
            ),
        ),
    )
    return (
        _command("require", ("list", _REQUIRE)),
        _command("require", ("list", _ACTION_REQUIRE)),
        _command("if", test=spam, block=(_command("return"),)),
        _command("if", test=_test(_MATCHES[spec.match], tests=tuple(conditions)), block=actions),
    )


def verify_script(script: Any, spec: SieveFilter) -> None:
    """Refuse a script unless it parses to exactly this filter's commands and values."""
    try:
        if not isinstance(script, str) or len(script.encode("utf-8")) > MAX_SCRIPT_BYTES:
            raise ValueError
    except (ValueError, UnicodeError):
        raise MailValidationError("Sieve script is not valid text; preview again") from None
    try:
        parsed = _Parser(script).script()
    except _SieveSyntaxError:
        raise MailValidationError("Sieve script has invalid syntax; preview again") from None
    if parsed != _expected(spec):
        raise MailValidationError("Sieve script does not match its reviewed filter; preview again")


_HEADER_FIELDS = {header: field for field, (_, header) in _TESTS.items()}


def parse_filter(name: Any, script: Any) -> SieveFilter:
    """The one validated filter whose rendered script is exactly this script.

    A backend that keeps filters in another form (saved rules) reads the reviewed
    script back with this, so it can only ever store what the person approved.
    """
    name = _text(name, "Filter name", MAX_NAME_LENGTH)
    try:
        if not isinstance(script, str) or len(script.encode("utf-8")) > MAX_SCRIPT_BYTES:
            raise ValueError
        *_, (_, _, _, test, block) = _Parser(script).script()
        _, combinator, _, tests = test
        conditions = []
        for _, _, arguments, _ in tests:
            *_, (_, operator), (_, header), (_, value) = arguments
            conditions.append({"field": _HEADER_FIELDS[header], operator[1:]: value})
        *flags, (_, _, ((_, path),), _, _) = block
        match = {test: match for match, test in _MATCHES.items()}[combinator]
        spec = filter_spec(name, "Folders/" + path, conditions, match, bool(flags))
    except (ValueError, TypeError, KeyError, UnicodeError, _SieveSyntaxError):
        raise MailValidationError("Sieve script is not a reviewed filter; preview again") from None
    # Every other script fails here: the filter must render to it byte for byte.
    if render_script(spec) != script:
        raise MailValidationError("Sieve script is not a reviewed filter; preview again")
    return spec


class SieveUnavailable(MailValidationError):
    """No supported path can apply Sieve filters; nothing was changed."""

    def __init__(self, reason: str = UNAVAILABLE_REASON):
        super().__init__(f"Sieve filters cannot be applied: {reason}; nothing was changed")


class UnavailableSieveBackend:
    """The only shipped backend: Bridge offers no way to apply Sieve, so every apply refuses."""

    name = "unavailable"

    def status(self) -> dict[str, Any]:
        return {"available": False, "reason": UNAVAILABLE_REASON}

    def filter_names(self, *, timeout: float) -> set[str]:
        raise SieveUnavailable()

    def create_filter(self, name: str, script: str, *, timeout: float) -> str | None:
        raise SieveUnavailable()


def backend_status(backend: Any) -> dict[str, Any]:
    """A backend's status in one safe shape; anything unexpected reads unavailable."""
    name = getattr(backend, "name", None)
    try:
        status = backend.status()
        available = isinstance(status, dict) and status.get("available") is True
        reason = status.get("reason") if isinstance(status, dict) else None
    except Exception:
        available, reason = False, None
    if not isinstance(name, str) or not name.isprintable() or not 1 <= len(name) <= 64:
        available, name = False, "invalid"
    if not isinstance(reason, str) or not reason.isprintable():
        reason = None if available else "the Sieve apply backend is unavailable"
    return {"backend": name, "available": available, "reason": reason and reason[:200]}


def require_backend(backend: Any) -> None:
    """Refuse before any confirmation, journal or mailbox work unless applying can work."""
    status = backend_status(backend)
    if not status["available"]:
        raise SieveUnavailable(status["reason"])
