"""Optional operator sender block list; none ships, and a bad configured file fails closed."""

from __future__ import annotations

import hashlib
import json
import re
from email import policy
from email.parser import HeaderParser
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from .config import ConfigError

_FIELDS = {
    "addresses",
    "domains",
    "domain_patterns",
    "keywords",
    "personal_domains",
    "personal_provider_allowlist",
}


class ProtectedPolicy:
    """Every visible sender address must parse, and every address is checked."""

    def __init__(self, path: str | None = None):
        try:
            defaults = json.loads(Path(__file__).with_name("protected_default.json").read_text())
            local = {} if path is None else json.loads(Path(path).read_text(encoding="utf-8"))
            for data in (defaults, local):
                if not isinstance(data, dict) or set(data) - _FIELDS - {
                    "mode",
                    "protect_personal_domains",
                }:
                    raise ValueError
                if data.get("mode", "extend") not in {"extend", "replace"}:
                    raise ValueError
                if type(data.get("protect_personal_domains", True)) is not bool:
                    raise ValueError
                for name in _FIELDS:
                    values = data.get(name, [])
                    if not isinstance(values, list) or any(
                        not isinstance(value, str)
                        or not value.strip()
                        or any(ord(char) < 32 or ord(char) == 127 for char in value)
                        for value in values
                    ):
                        raise ValueError
                # Validate even entries discarded by replace mode: invalid policy fails closed.
                for value in data.get("personal_provider_allowlist", []):
                    self._allowlist_entry(value)
            base = {} if local.get("mode") == "replace" else defaults
            self.rules = {
                name: tuple(
                    dict.fromkeys(
                        self._allowlist_entry(item)
                        if name == "personal_provider_allowlist"
                        else item.strip().casefold()
                        for item in [*base.get(name, []), *local.get(name, [])]
                    )
                )
                for name in _FIELDS
            }
            # Retain every legacy spelling and add canonical matches. Otherwise an
            # IDNA allowlist could expose a protection that only matched Unicode.
            for name in ("addresses", "domains", "personal_domains"):
                self.rules[name] = tuple(
                    dict.fromkeys(
                        [
                            *self.rules[name],
                            *(
                                self._configured_identity(name, item)
                                for item in [*base.get(name, []), *local.get(name, [])]
                            ),
                        ]
                    )
                )
            self.protect_personal = local.get(
                "protect_personal_domains", defaults.get("protect_personal_domains", True)
            )
        except (OSError, ValueError, TypeError):
            # Local policy can contain personal addresses. Never include its contents/path.
            raise ConfigError("Cannot load a valid protected sender configuration") from None

    @staticmethod
    def _domain_matches(domain: str, configured: str) -> bool:
        configured = configured.removeprefix("*.").rstrip(".")
        return domain == configured or domain.endswith("." + configured)

    @staticmethod
    def _configured_identity(name: str, value: str) -> str:
        value = value.strip()
        local, separator, domain = value.rpartition("@") if name == "addresses" else ("", "", value)
        if name == "addresses" and not separator:
            return value.casefold()
        try:
            domain = domain.rstrip(".").encode("idna").decode("ascii")
        except UnicodeError:
            # Existing policy entries have never required strict mailbox syntax.
            return value.casefold()
        return f"{local}{separator}{domain}".casefold()

    @staticmethod
    def _complete(address: Any) -> tuple[str, str]:
        if not address.username or not address.domain:
            raise ValueError
        domain = address.domain.rstrip(".").encode("idna").decode("ascii").casefold()
        if "." not in domain or domain.startswith("["):
            raise ValueError
        return f"{address.username}@{domain}".casefold(), domain

    @classmethod
    def _allowlist_entry(cls, value: str) -> str:
        # A single bare address only, never a display name, group, comment or glob.
        value = value.strip()
        if value.count("@") != 1 or any(
            char.isspace() or char in '*?[]<>()\\,;:"' for char in value
        ):
            raise ValueError
        try:
            header = HeaderParser(policy=policy.default).parsestr(f"From: {value}\n\n")["From"]
        except (IndexError, AttributeError):
            # Older stdlib parsers can fail internally on malformed addresses.
            raise ValueError from None
        if header.defects or len(header.addresses) != 1:
            raise ValueError
        complete, domain = cls._complete(header.addresses[0])
        if any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
            for label in domain.split(".")
        ):
            raise ValueError
        return complete

    def check(self, message: dict[str, Any]) -> list[str]:
        """Return fixed reasons without copying potentially private header values."""
        return self.evaluate(message)[0]

    def allowlist_digest(self) -> str:
        """Bind cleanup confirmation to the effective normalized allowlist."""
        entries = json.dumps(
            sorted(self.rules["personal_provider_allowlist"]), separators=(",", ":")
        )
        return hashlib.sha256(entries.encode("utf-8")).hexdigest()

    def evaluate(self, message: dict[str, Any]) -> tuple[list[str], list[dict[str, str]]]:
        """Check every address, retaining only header names and digests for bypasses."""
        reasons = []
        bypasses = []
        if message.get("sender_invalid"):
            reasons.append("Unavailable, unsupported or ambiguous sender headers")
        for key, label in (("from", "From"), ("sender", "Sender"), ("reply_to", "Reply-To")):
            value = message.get(key)
            if value in (None, ""):
                if (key == "from" or key in message) and not message.get("sender_invalid"):
                    reasons.append(f"Missing or unparseable {label} sender")
                continue
            try:
                if not isinstance(value, str) or any(
                    ord(char) < 32 or ord(char) == 127 for char in value
                ):
                    raise ValueError
                parsed = HeaderParser(policy=policy.default).parsestr(f"{label}: {value}\n\n")
                header = parsed[label]
                addresses = header.addresses
                if header.defects or not addresses:
                    raise ValueError
                for address in addresses:
                    complete, domain = self._complete(address)
                    if complete in self.rules["addresses"]:
                        reasons.append(f"Protected personal address in {label}")
                    if any(self._domain_matches(domain, item) for item in self.rules["domains"]):
                        reasons.append(f"Protected sender domain in {label}")
                    if any(fnmatchcase(domain, item) for item in self.rules["domain_patterns"]):
                        reasons.append(
                            f"Protected financial, insurance or government domain in {label}"
                        )
                    if self.protect_personal and any(
                        self._domain_matches(domain, item)
                        for item in self.rules["personal_domains"]
                    ):
                        if complete in self.rules["personal_provider_allowlist"]:
                            bypass = {
                                "header": label,
                                "entry_sha256": hashlib.sha256(
                                    complete.encode("utf-8")
                                ).hexdigest(),
                            }
                            if bypass not in bypasses:
                                bypasses.append(bypass)
                        else:
                            reasons.append(f"Personal mail provider in {label}")
                    if any(
                        item in address.display_name.casefold() for item in self.rules["keywords"]
                    ):
                        reasons.append(f"Protected sender display name in {label}")
            except (ValueError, TypeError, AttributeError, IndexError, UnicodeError):
                reasons.append(f"Unparseable {label} sender")
        return list(dict.fromkeys(reasons)), bypasses
