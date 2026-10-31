"""Shared source refusals for read-state changes, manual rules and the timer."""

from __future__ import annotations

import logging

from .cleanup_folders import _folder_attributes
from .config import ConfigError
from .mail import MailValidationError

_REFUSED_INBOX_ATTRIBUTES = frozenset(
    {
        r"\drafts",
        r"\sent",
        r"\junk",
        r"\trash",
        r"\all",
        r"\noselect",
        r"\nonexistent",
        r"\archive",
        r"\flagged",
    }
)
_REFUSAL = (
    "Rules source refused: INBOX must be selectable and have no conflicting special-use flags"
)
_READ_STATE_REFUSAL = "Read-state source refused: Drafts and Sent are not allowed"


class RulesSourceError(ConfigError, MailValidationError):
    """A source refusal is both invalid configuration and invalid tool input."""


class ReadStateSourceError(MailValidationError):
    """A protected source cannot receive an approval to change read state."""


def _source_flags(folder, attributes):
    if isinstance(folder, str) and folder.upper() == "INBOX":
        # INBOX aliases identify the same mailbox; other names are exact identities.
        return set().union(
            *(flags for name, flags in attributes.items() if name.upper() == "INBOX")
        )
    return attributes.get(folder, set())


def _protected_source(folder, attributes):
    # Normalize only to refuse a protected name, never to authorize selection.
    name = "/".join(part.strip().casefold() for part in folder.split("/"))
    return name.removeprefix("folders/") in {"drafts", "sent"} or bool(
        _source_flags(folder, attributes).intersection({r"\drafts", r"\sent"})
    )


def require_read_state_source(folder, connection=None):
    """Refuse protected names and special-use flags before a read-state write."""
    attributes = _folder_attributes(connection) if connection is not None else {}
    if not isinstance(folder, str) or _protected_source(folder, attributes):
        logging.getLogger(__name__).warning(_READ_STATE_REFUSAL)
        raise ReadStateSourceError(_READ_STATE_REFUSAL)


def require_rules_source(folder, connection=None):
    """Return the server's INBOX name after validating its LIST attributes.

    Only ASCII case is insignificant for INBOX. Whitespace, prefixes, Unicode
    lookalikes and every other mailbox remain outside this authorization.
    """
    allowed = isinstance(folder, str) and folder.isascii() and folder.upper() == "INBOX"
    if allowed and connection is not None:
        attributes = _folder_attributes(connection)
        inbox_flags = _source_flags(folder, attributes)
        names = [name for name in attributes if name.isascii() and name.upper() == "INBOX"]
        allowed = bool(names) and not inbox_flags.intersection(_REFUSED_INBOX_ATTRIBUTES)
        if allowed:
            folder = names[0]
    if not allowed:
        # No untrusted name, message data or configuration value reaches logs.
        logging.getLogger(__name__).warning(_REFUSAL)
        raise RulesSourceError(_REFUSAL)
    return folder
