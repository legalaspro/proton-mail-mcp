"""Shared sender queries, with separate strict evidence for guarded actions."""

from __future__ import annotations

import re
from collections.abc import Callable
from email import policy
from email.parser import HeaderParser
from functools import lru_cache, wraps
from typing import Any

_SENDER_FIELDS = (("from", "From"), ("sender", "Sender"), ("reply_to", "Reply-To"))
_DISPLAY_ADDRESS = re.compile(r"[^\s<>(),;:\\\"]+@[^\s<>(),;:\\\"]+")


# What a parse cache may hold in a long-lived server. Mail chooses these strings, so a long one
# is parsed every time rather than kept. tests/test_sender_parse_cache.py limits the worst case.
CACHE_ENTRIES = 16384
CACHE_VALUE_CHARS = 256


def bounded_cache(function: Callable[..., Any]) -> Callable[..., Any]:
    """An LRU cache that skips any call with a string argument above the length limit."""
    cached = lru_cache(maxsize=CACHE_ENTRIES)(function)

    @wraps(function)
    def call(*args: Any) -> Any:
        for arg in args:
            if isinstance(arg, str) and len(arg) > CACHE_VALUE_CHARS:
                return function(*args)
        return cached(*args)

    call.cache_info = cached.cache_info  # type: ignore[attr-defined]
    call.cache_clear = cached.cache_clear  # type: ignore[attr-defined]
    return call


@bounded_cache
def _parsed_text(value: str) -> tuple[str, tuple[tuple[bool, str, str], ...], bool]:
    """The header's text, its addresses, and whether it has defects or a named group.

    Each address is (has both a username and a domain, addr_spec, display name). Inventories
    read the same senders on every revalidation, and a whole header object weighs about 19 KB.
    """
    header = HeaderParser(policy=policy.default).parsestr(f"From: {value}\n\n")["From"]
    addresses = tuple(
        (bool(address.username and address.domain), address.addr_spec, address.display_name)
        for address in header.addresses
    )
    flagged = bool(header.defects) or any(group.display_name is not None for group in header.groups)
    return str(header), addresses, flagged


def _sender_text(value: Any) -> str:
    return str(value) if hasattr(value, "addresses") else _parsed_text(f"{value}")[0]


def sender_matches(value: Any, query: str, *, decoded: bool = False) -> bool:
    """Match decoded display names and addresses equally in search and preview.

    Matching is discovery, not authorization. Read-only search may report an
    ambiguous sender; guarded actions separately require strict sender evidence.
    Preview snapshots already contain decoded text, so never parse them twice:
    a literal encoded-word spelling in a display name must remain literal.
    """
    if value is None:
        return False
    try:
        value = str(value) if decoded else _sender_text(value)
    except (ValueError, TypeError, AttributeError, IndexError):
        value = str(value)
    return query.casefold() in value.casefold()


def sender_snapshot(headers: Any) -> tuple[dict[str, str], bool]:
    """Retain parsed raw senders and reject ambiguity hidden by IMAP ENVELOPE.

    A destructive capability requires exactly one From mailbox and at most one
    Sender mailbox. Duplicate fields, groups, malformed headers, and display
    names impersonating a different mailbox are never authorization evidence.
    Absent Sender/Reply-To copy From, as Gluon does, preserving snapshot digests.
    """
    values: dict[str, str] = {}
    invalid = False
    for key, label in _SENDER_FIELDS:
        fields = (
            [value for name, value in headers.raw_items() if name.casefold() == label.casefold()]
            if headers is not None
            else []
        )
        if not fields:
            values[key] = values.get("from", "")
            invalid |= key == "from"
            continue
        invalid |= len(fields) != 1
        try:
            # BytesParser preserves raw RFC 6532 UTF-8 through surrogateescape;
            # decode strictly before parsing so a valid international display
            # name does not inherit an UndecodableBytesDefect from the byte API.
            raw = fields[0].encode("utf-8", "surrogateescape").decode("utf-8", "strict")
            raw = re.sub(r"\r?\n[ \t]+", " ", raw)
            if any(not char.isprintable() and char != "\t" for char in raw):
                invalid = True
            text, addresses, flagged = _parsed_text(raw)
            values[key] = text
            if (
                flagged
                or not addresses
                or (key in {"from", "sender"} and len(addresses) != 1)
                or any(not char.isprintable() for char in text)
            ):
                invalid = True
            for complete, addr_spec, display_name in addresses:
                if not complete:
                    invalid = True
                if any(
                    match.casefold() != addr_spec.casefold()
                    for match in _DISPLAY_ADDRESS.findall(display_name)
                ):
                    invalid = True
        except (ValueError, TypeError, AttributeError, IndexError, UnicodeError):
            # Invalid octets are still discoverable through the same visible
            # replacement-decoded text as search. Preserve that text while
            # refusing authorization, rather than silently losing the match.
            try:
                values[key] = str(headers.get(label, ""))
            except (ValueError, TypeError, AttributeError, IndexError):
                values[key] = ""
            invalid = True
    return values, invalid
