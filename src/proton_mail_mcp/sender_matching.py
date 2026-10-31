"""Shared sender queries, with separate strict evidence for guarded actions."""

from __future__ import annotations

import re
from email import policy
from email.parser import HeaderParser
from typing import Any

_SENDER_FIELDS = (("from", "From"), ("sender", "Sender"), ("reply_to", "Reply-To"))
_DISPLAY_ADDRESS = re.compile(r"[^\s<>(),;:\\\"]+@[^\s<>(),;:\\\"]+")


def _parsed_sender(value: Any) -> Any:
    if hasattr(value, "addresses"):
        return value
    return HeaderParser(policy=policy.default).parsestr(f"From: {value}\n\n")["From"]


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
        value = str(value) if decoded else str(_parsed_sender(value))
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
            header = _parsed_sender(raw)
            values[key] = str(header)
            addresses = header.addresses
            if (
                header.defects
                or not addresses
                or (key in {"from", "sender"} and len(addresses) != 1)
                or any(group.display_name is not None for group in header.groups)
                or any(not char.isprintable() for char in str(header))
            ):
                invalid = True
            for address in addresses:
                if not address.username or not address.domain:
                    invalid = True
                if any(
                    match.casefold() != address.addr_spec.casefold()
                    for match in _DISPLAY_ADDRESS.findall(address.display_name)
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
