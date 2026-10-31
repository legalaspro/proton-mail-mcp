"""Canonical, selectable real folders eligible for guarded moves."""

from __future__ import annotations

from typing import Any

from .mail import _checked, _decode_folder, _ImapValues, _string

_SYSTEM_FOLDERS = frozenset({"INBOX", "Archive", "Sent", "Drafts", "Spam", "Trash"})
_VIEW_ATTRIBUTES = frozenset({"\\flagged", "\\all", "\\noselect"})


def _real_folder_name(folder: str) -> bool:
    # INBOX alone is case-insensitive in IMAP. Other mailbox identities,
    # including the ASCII Folders/ namespace prefix, remain exact.
    if folder in _SYSTEM_FOLDERS or (folder.isascii() and folder.upper() == "INBOX"):
        return True
    if not folder.startswith("Folders/"):
        return False
    if len(folder) > 500 or any(
        char in "\"'\\*%" or ord(char) < 32 or 127 <= ord(char) <= 159 for char in folder
    ):
        return False
    return all(part not in {"", ".", ".."} and part == part.strip() for part in folder.split("/"))


def _folder_attributes(connection: Any) -> dict[str, set[str]]:
    """Preserve all LIST attributes, including conflicting repeated entries."""
    attributes: dict[str, set[str]] = {}
    for item in _checked(connection.list()):
        if item is None:
            continue
        if isinstance(item, tuple):
            item = item[0] + b"\r\n" + item[1]
        parser = _ImapValues(item)
        flags, _, name = parser.value(), parser.value(), parser.value()
        folder = _decode_folder(name)
        # Merge repeated LIST responses so a conflicting attribute cannot be
        # hidden by an earlier, apparently ordinary response for the same name.
        attributes.setdefault(folder, set()).update(
            {_string(flag).casefold() for flag in flags}
            if isinstance(flags, list)
            else {"\\noselect"}
        )
    return attributes


def real_folders(attributes: dict[str, set[str]]) -> set[str]:
    """Real folders by name and LIST attributes: move sources and canonical locations."""
    return {
        folder
        for folder, flags in attributes.items()
        if _real_folder_name(folder) and not flags.intersection(_VIEW_ATTRIBUTES)
    }


def _move_folders(connection: Any) -> set[str]:
    """Use LIST attributes as well as names; a view never becomes a move source."""
    return real_folders(_folder_attributes(connection))
