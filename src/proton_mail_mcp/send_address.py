"""Conservative addr-specs with separate configuration and draft-header parsing."""

import base64
import re
import unicodedata
from email import policy
from email.header import decode_header
from email.utils import getaddresses

_ADDRESS = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+"
)

# A forward's subject, as forward_message writes it and other mail clients do.
FORWARD_SUBJECT = re.compile(r"(?i)\s*fwd?\s*:")


def is_bare_address(value: object) -> bool:
    return isinstance(value, str) and _ADDRESS.fullmatch(value) is not None


def parse_addresses(value: str, *, allow_display_names: bool = False) -> list[str]:
    """Parse conservative ASCII mailboxes without hiding another address."""
    if (
        not isinstance(value, str)
        or not value.isascii()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(char in value for char in "():;\\")
        or "=?" in value
        or value.rstrip().endswith(",")
    ):
        raise ValueError("Invalid address header")
    try:
        parsed = getaddresses([value], strict=True)
    except TypeError:  # Python versions before strict address parsing was added.
        parsed = getaddresses([value])
    header = policy.SMTP.header_factory("To", value)
    if (
        not parsed
        or header.defects
        or any(group.display_name is not None for group in header.groups)
        or parsed != [(address.display_name, address.addr_spec) for address in header.addresses]
    ):
        raise ValueError("Invalid address header")
    for display, address in parsed:
        # An identical address label adds no recipient information. Any other
        # '@'-bearing display could hide a different recipient and still refuses.
        if (
            not is_bare_address(address)
            or any(char in display for char in "<>,")
            or ("@" in display and display != address)
        ):
            raise ValueError("Invalid address header")
        if not allow_display_names and (display or any(char in value for char in '<>"')):
            raise ValueError("Invalid address header")
    return [address for _, address in parsed]


_ENCODED_WORD = re.compile(r"=\?([^?\s]+)\?([bBqQ])\?([^?\s]*)\?=")


def _check_display_name(value: str, *, address: str | None = None) -> None:
    if (
        any(char in value for char in '<>,;:()"\\')
        or ("@" in value and value != address)
        or "=?" in value
        or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)
    ):
        raise ValueError("Invalid display name")


def _check_encoded_name(value: str) -> str:
    # Inspect literal text as well as decoded text; the header parser tolerates
    # some malformed Q escapes and decoders may otherwise replace bad bytes.
    if any(char in value for char in '@<>,;:()"\\'):
        raise ValueError("Invalid display name")
    _check_display_name(_ENCODED_WORD.sub("", value))
    for word in _ENCODED_WORD.finditer(value):
        encoding, payload = word.group(2, 3)
        if encoding.lower() == "b":
            base64.b64decode(payload, validate=True)
        elif re.search(r"=(?![0-9a-fA-F]{2})", payload):
            raise ValueError("Invalid encoded display name")
    decoded = []
    for text, charset in decode_header(value):
        if isinstance(text, bytes):
            text = text.decode(charset or "ascii", errors="strict")
        decoded.append(text)
    name = "".join(decoded)
    _check_display_name(name)
    return name


def parse_draft_addresses(value: str) -> list[str]:
    """Validate stored From mailboxes without broadening recipient/config parsing."""
    if not isinstance(value, str) or not value.isascii():
        raise ValueError("Invalid address header")
    # Only syntactic folding is removed for validation. Stored bytes are kept
    # separately for transport; a bare CR/LF or any remaining control refuses.
    value = re.sub(r"\r\n[ \t]+", " ", value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Invalid address header")
    if any(char in value for char in "():;\\"):
        raise ValueError("Invalid address header")
    header = policy.SMTP.header_factory("To", value)
    if header.defects or any(group.display_name is not None for group in header.groups):
        raise ValueError("Invalid address header")
    entries = value.split(",")
    if len(entries) != len(header.addresses):
        raise ValueError("Invalid address header")
    addresses = []
    for entry, parsed in zip(entries, header.addresses):
        entry = entry.strip()
        if is_bare_address(entry):
            address = entry
        else:
            match = re.fullmatch(r"(.+?)\s*<([^<>]+)>", entry)
            if match is None:
                raise ValueError("Invalid address header")
            name, address = match.groups()
            name = name.strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1]
            if not name or not parsed.display_name.strip() or not is_bare_address(address):
                raise ValueError("Invalid address header")
            _check_encoded_name(name)
        _check_display_name(parsed.display_name)
        if "=?" in address or parsed.addr_spec != address:
            raise ValueError("Invalid address header")
        addresses.append(address)
    if not addresses:
        raise ValueError("Invalid address header")
    return addresses
