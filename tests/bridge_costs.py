"""Shared Gluon work units and an explicitly synthetic timing calibration.

Gluon mailbox_fetch.go loads a DB row per fetched message and one whole literal
when any BODY section or RFC822 literal item is requested. mailbox_search.go
loads one literal per visited message for header/body search keys, regardless of
how many OR branches use it. Date/size SEARCH reads DB rows; ALL/flags/UID use the
mailbox snapshot. No field-specific discount is applied to header FETCH.

The watchdog start cost is a field-symptom calibration, not a Bridge measurement:
2.5 ms plus 0.25 ms per DB row reproduces the old 20k-row search timeout. Charging
actual starts also represents the previous per-line watchdog implementation.
The pessimistic DB cost is three times the default. ENVELOPE client parsing costs
100 us per row; this parse-only allowance excludes other preview client work.
The measured full client path uses 109-123 us per row at moderate load and more
under heavy load. Real Bridge timing remains unknown; this is not a live measurement.
"""

import re
import threading
import time
from dataclasses import dataclass, replace
from email import policy
from email.header import decode_header
from email.parser import BytesParser, HeaderParser
from functools import lru_cache


@dataclass(frozen=True)
class CostTable:
    literal_load: float = 0.003
    db_row: float = 0.00025
    command: float = 0.001
    watchdog_start: float = 0.0025
    envelope_client_row: float = 0.0001


DEFAULT_COSTS = CostTable()
PESSIMISTIC_COSTS = replace(DEFAULT_COSTS, db_row=0.00075)
ZERO_COSTS = CostTable(0, 0, 0, 0, 0)
LITERAL_SEARCH_KEYS = frozenset(
    {
        "HEADER",
        "FROM",
        "TO",
        "CC",
        "BCC",
        "SUBJECT",
        "BODY",
        "TEXT",
        "SENTBEFORE",
        "SENTON",
        "SENTSINCE",
    }
)
DB_SEARCH_KEYS = frozenset({"SINCE", "BEFORE", "ON", "LARGER", "SMALLER"})


class CommandClock:
    """Deterministic per-command latency without changing threading's real clock."""

    def __init__(self):
        self.elapsed = 0.0

    def monotonic(self):
        return self.elapsed

    def __getattr__(self, name):
        return getattr(time, name)


def literal_fetch(query):
    """RFC822.SIZE is DB metadata; RFC822/HEADER/TEXT and every BODY[] are not."""
    return bool(
        re.search(rb"BODY(?:\.PEEK)?\[|\bRFC822(?:\.HEADER|\.TEXT)?(?=[ )]|$)", query.upper())
    )


def search_keys(command):
    # Commands are synthetic, and quoted values are stripped before classifying
    # keys so a subject containing the word BODY does not affect instrumentation.
    clean = re.sub(rb'"(?:[^"\\]|\\.)*"', b'""', command.upper())
    return {word.decode("ascii") for word in re.findall(rb"[A-Z]+", clean)}


class WorkCounters:
    def __init__(self, clock=None, costs=DEFAULT_COSTS, *, forbid_literals=False):
        self.clock, self.costs = clock, costs
        self.forbid_literals = forbid_literals
        self.literal_loads_fetch = 0
        self.literal_loads_search = 0
        self.db_rows_fetch = 0
        self.db_rows_search = 0
        self.watchdog_starts = 0
        self.client_cpu_seconds = 0.0
        self.commands = {}

    @property
    def literal_loads(self):
        return self.literal_loads_fetch + self.literal_loads_search

    def charge(self, operation, command, rows):
        self.commands[operation] = self.commands.get(operation, 0) + 1
        cost = self.costs.command
        if operation == "FETCH":
            self.db_rows_fetch += rows
            cost += rows * self.costs.db_row
            if re.search(rb"\bENVELOPE\b", command.upper()):
                client_cpu = rows * self.costs.envelope_client_row
                self.client_cpu_seconds += client_cpu
                cost += client_cpu
            if literal_fetch(command):
                self.literal_loads_fetch += rows
                cost += rows * self.costs.literal_load
                if self.forbid_literals:
                    raise AssertionError("Cleanup attempted a literal-needing FETCH")
        elif operation == "SEARCH":
            keys = search_keys(command)
            if keys & LITERAL_SEARCH_KEYS:
                self.literal_loads_search += rows
                cost += rows * self.costs.literal_load
                if self.forbid_literals:
                    raise AssertionError("Cleanup attempted a literal-needing SEARCH")
            if keys & DB_SEARCH_KEYS:
                self.db_rows_search += rows
                cost += rows * self.costs.db_row
        if self.clock is not None:
            self.clock.elapsed += cost

    def summary(self):
        return {
            name: getattr(self, name)
            for name in (
                "literal_loads_fetch",
                "literal_loads_search",
                "db_rows_fetch",
                "db_rows_search",
                "watchdog_starts",
                "client_cpu_seconds",
            )
        } | {"literal_loads": self.literal_loads}


class ChargedThreading:
    """Preserve real timers and charge each actual watchdog start uniformly."""

    def __init__(self, work):
        self.work = work

    def __getattr__(self, name):
        return getattr(threading, name)

    def Timer(self, *args, **kwargs):
        timer = threading.Timer(*args, **kwargs)
        original = timer.start

        def start():
            self.work.watchdog_starts += 1
            if self.work.clock is not None:
                self.work.clock.elapsed += self.work.costs.watchdog_start
            return original()

        timer.start = start
        return timer


def _nstring(value):
    """Gluon params.go addString: empty is NIL, otherwise Go strconv.Quote."""
    if value is None or value == "":
        return b"NIL"
    escapes = {
        "\a": r"\a",
        "\b": r"\b",
        "\f": r"\f",
        "\n": r"\n",
        "\r": r"\r",
        "\t": r"\t",
        "\v": r"\v",
        "\\": r"\\",
        '"': r"\"",
    }
    result = ['"']
    for char in str(value):
        code = ord(char)
        if char in escapes:
            result.append(escapes[char])
        elif 0xDC80 <= code <= 0xDCFF:
            result.append(f"\\x{code - 0xDC00:02x}")
        elif code < 32 or code == 127:
            result.append(f"\\x{code:02x}")
        elif char.isprintable():
            result.append(char)
        elif code < 65536:
            result.append(f"\\u{code:04x}")
        else:
            result.append(f"\\U{code:08x}")
    result.append('"')
    return "".join(result).encode("utf-8")


@lru_cache(maxsize=100_000)
def _addresses(value):
    # Large fixtures have distinct identities but mostly share address fields.
    # Cache each immutable field value without replacing any parser behavior.
    if value is None or not value:
        return b"NIL"
    try:
        # Python's header parser tolerates unknown charsets. Gluon returns
        # a parse error, so require every encoded word to decode strictly.
        for part, charset in decode_header(value):
            if isinstance(part, bytes):
                part.decode(charset or "ascii", errors="strict")
        header = HeaderParser(policy=policy.default).parsestr(f"To: {value}\n\n")["To"]
        if header.defects or not header.addresses:
            return b"NIL"
        result = []
        for address in header.addresses:
            # Quoted local parts are unquoted before Gluon splits them.
            complete = f"{address.username}@{address.domain}"
            parts = complete.split("@")
            local, domain = parts if len(parts) == 2 else (None, None)
            result.append(
                b"("
                + b" ".join(
                    (_nstring(address.display_name), b"NIL", _nstring(local), _nstring(domain))
                )
                + b")"
            )
        return b"(" + b"".join(result) + b")"
    except (LookupError, UnicodeError, ValueError, AttributeError, IndexError):
        return b"NIL"


@lru_cache(maxsize=100_000)
def envelope_from_headers(raw):
    """Model the Gluon envelope rules used by the synthetic mailbox fixtures.

    envelope.go copies From only for absent Sender/Reply-To; parser errors
    yield NIL. rfc822/header.go exposes only the first header. Address groups
    flatten (rfc5322/address.go), encoded names decode (rfc5322/atom.go), and
    params.go splits an address at every @ before applying strconv.Quote.
    """
    parsed = BytesParser(policy=policy.compat32).parsebytes(raw)
    first = {}
    for name, value in parsed.raw_items():
        # Preserve undecoded header values and unfold continuation lines.
        value = value.encode("utf-8", errors="surrogateescape").decode(
            "utf-8", errors="surrogateescape"
        )
        first.setdefault(name.lower(), re.sub(r"\r?\n[ \t]+", " ", value))

    def addresses(field):
        return _addresses(first.get(field))

    sender = addresses("from")
    return (
        b"("
        + b" ".join(
            (
                _nstring(first.get("date")),
                _nstring(first.get("subject")),
                sender,
                addresses("sender") if "sender" in first else sender,
                addresses("reply-to") if "reply-to" in first else sender,
                addresses("to"),
                addresses("cc"),
                addresses("bcc"),
                _nstring(first.get("in-reply-to")),
                _nstring(first.get("message-id")),
            )
        )
        + b")"
    )
