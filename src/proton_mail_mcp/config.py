"""Validated environment configuration; secret values never enter representations."""

import json
import math
import ntpath
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import localfs
from .send_address import is_bare_address


class ConfigError(ValueError):
    """A configuration value is missing or invalid."""


# Code ceiling for PROTON_MCP_APPROVAL_MAX_MESSAGES: one confirmed approval never
# covers more messages, however it is configured. Each IMAP command and each
# undoable journal chunk still names at most 200 messages.
APPROVAL_MAX_CEILING = 1000
# Mirrors credential_store.STORES; config stays free of subprocess-backed imports.
CREDENTIAL_STORES = ("keychain", "libsecret", "pass", "wincred")
# Mirrors the setting names of credential_store.LOCATION_SETTINGS.
CREDENTIAL_LOCATION_KEYS = ("PROTON_MCP_PASSWORD_STORE_DIR", "PROTON_MCP_GNUPGHOME")


def valid_attachments_dir(value: str, flavor=os.path) -> bool:
    """A normalized absolute path below the filesystem root, with no trailing slash."""
    return (
        flavor.isabs(value)
        # "/." and "/srv/.." open the root, and a trailing slash follows a link.
        and flavor.normpath(value) == value
        and flavor.dirname(value) != value
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def windows_attachments_dir(env: Mapping[str, str]) -> str | None:
    """The default folder on Windows, in LOCALAPPDATA as the rest of this server's files are."""
    data = env.get("LOCALAPPDATA", "")
    if not ntpath.isabs(data):
        home = env.get("USERPROFILE") or env.get("HOME") or os.path.expanduser("~")
        data = ntpath.join(home, "AppData", "Local")
    path = ntpath.normpath(ntpath.join(data, "proton-mail-mcp", "attachments"))
    return path if valid_attachments_dir(path, ntpath) else None


def default_attachments_dir(env: Mapping[str, str]) -> str | None:
    """The private per-user folder used when PROTON_MCP_ATTACHMENTS_DIR is unset.

    None when there is no usable home, and attachment tools then refuse.
    """
    if sys.platform == "win32":
        return windows_attachments_dir(env)
    # The XDG specification ignores relative values.
    data = env.get("XDG_DATA_HOME", "")
    if not os.path.isabs(data):
        data = os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "share")
    path = os.path.normpath(os.path.join(data, "proton-mail-mcp", "attachments"))
    return path if valid_attachments_dir(path) else None


def default_credential_store() -> str:
    """Credential Manager on Windows; keychain everywhere else, as it always was."""
    return "wincred" if sys.platform == "win32" else "keychain"


def _flag(values: Mapping[str, str], name: str) -> bool:
    value = values.get(name, "0")
    if value not in {"0", "1"}:
        raise ConfigError(f"{name} must be 0 or 1")
    return value == "1"


def _idle_folders(value: str) -> tuple[str, ...]:
    try:
        folders = json.loads(value)
    except (TypeError, ValueError):
        raise ConfigError(
            "PROTON_MCP_IMAP_IDLE_FOLDERS must be a JSON array of folder names"
        ) from None
    if not isinstance(folders, list):
        raise ConfigError("PROTON_MCP_IMAP_IDLE_FOLDERS must be a JSON array of folder names")
    return tuple(folders)


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    bridge_user: str
    host: str = "127.0.0.1"
    port: int = 8765
    imap_host: str = "127.0.0.1"
    imap_port: int = 1143
    imap_cert_path: str | None = None
    drafts_folder: str = "Drafts"
    timeout: float = 30.0
    call_timeout: float = 120.0
    execute_timeout: float = 120.0
    preview_timeout: float | None = None
    public_host: str | None = None
    cleanup_token_ttl: float = 600.0
    protected_config_path: str | None = None
    action_log_path: str | None = None
    # Saved custom rules (add_rule); unset means saved-rules.json beside the action log.
    saved_rules_path: str | None = None
    # The only folder attachment tools read or write; None means they refuse.
    attachments_dir: str | None = None
    # It is the per-user default, not a folder the operator named: the first save creates it.
    attachments_dir_default: bool = False
    identity_index_enabled: bool = True
    identity_index_dir: str | None = None
    search_index_catchup_max_messages: int = 2000
    # Startup recovery is independent of optional periodic background sync.
    index_autorebuild_enabled: bool = True
    reindex_batch_size: int = 100
    reindex_batch_sleep: float = 0.25
    imap_idle_enabled: bool = False
    imap_idle_folders: tuple[str, ...] = ("INBOX",)
    imap_idle_poll_interval: float = 60.0
    # Opt-in standing job: off means no thread, connection or file exists.
    background_sync_enabled: bool = False
    background_sync_interval: float = 900.0
    background_sync_max_commands: int = 1500
    unsubscribe_max_batch: int = 50
    rules_max_moves: int = 50
    # Hard upper bound for one cleanup approval that continues server-side past
    # 200 messages; 200 keeps every approval within a single chunk's cap.
    approval_max_messages: int = 500
    # Cleanup, rules, unsubscribe and folder tokens need a host form confirmation.
    confirm_mutations: bool = True
    confirm_timeout: float = 120.0
    # MCP Apps views (ui:// resources and their view tools); 0 restores the plain tool list.
    apps_enabled: bool = True
    # The folder rule and Sieve filter tools (beta); off leaves them out of the tool list.
    enable_rules: bool = False
    # IANA zone for the local times in results; None means this computer's time zone.
    time_zone: str | None = None
    enable_send: bool = False
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_tls: bool = False
    smtp_allow_unverified: bool = False
    send_from_allowlist: tuple[str, ...] = field(default=(), repr=False)
    send_max_recipients: int = 10
    send_daily_cap: int = 20
    send_token_ttl: float = 300.0
    send_self_no_form: bool = False
    send_typed_ok: bool = True
    # clientInfo names, trimmed and casefolded, that get the typed OK despite declaring a form.
    send_typed_ok_clients: tuple[str, ...] = ()
    send_confirm_timeout: float = 120.0
    # Where the Bridge password lives when PROTON_BRIDGE_PASSWORD is unset.
    credential_store: str = field(default_factory=default_credential_store)
    # (setting, absolute path) pairs that locate a pass store and its GnuPG home.
    credential_locations: tuple[tuple[str, str], ...] = ()

    @property
    def no_form_clients(self) -> tuple[str, ...]:
        """Client names to treat as having no form; none with the typed OK switched off."""
        return self.send_typed_ok_clients if self.send_typed_ok is True else ()

    @property
    def display_zone(self) -> ZoneInfo | None:
        """The zone of date_local and quoted dates; None is this computer's local time zone."""
        return ZoneInfo(self.time_zone) if self.time_zone else None

    @property
    def background_sync_active(self) -> bool:
        # An enabled IDLE watcher already maintains the index on its own connections.
        return self.background_sync_enabled and not self.imap_idle_enabled

    def __post_init__(self) -> None:
        if type(self.imap_idle_enabled) is not bool:
            raise ConfigError("PROTON_MCP_IMAP_IDLE_ENABLED must be boolean")
        if (
            not isinstance(self.imap_idle_folders, tuple)
            or not 1 <= len(self.imap_idle_folders) <= 8
            or any(
                not isinstance(folder, str)
                or not folder
                or any(
                    ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
                    for char in folder
                )
                for folder in self.imap_idle_folders
            )
        ):
            raise ConfigError("PROTON_MCP_IMAP_IDLE_FOLDERS requires 1 to 8 safe folder names")
        # INBOX is case-insensitive in IMAP; other mailbox names need not be.
        folder_keys = [
            "INBOX" if name.upper() == "INBOX" else name for name in self.imap_idle_folders
        ]
        if len(set(folder_keys)) != len(folder_keys):
            raise ConfigError("PROTON_MCP_IMAP_IDLE_FOLDERS requires distinct folder names")
        if (
            isinstance(self.imap_idle_poll_interval, bool)
            or not isinstance(self.imap_idle_poll_interval, (int, float))
            or not math.isfinite(self.imap_idle_poll_interval)
            or not 30 <= self.imap_idle_poll_interval <= 300
        ):
            raise ConfigError("PROTON_MCP_IMAP_IDLE_POLL_INTERVAL must be between 30 and 300")
        if type(self.background_sync_enabled) is not bool:
            raise ConfigError("PROTON_MCP_BACKGROUND_SYNC_ENABLED must be boolean")
        if (
            isinstance(self.background_sync_interval, bool)
            or not isinstance(self.background_sync_interval, (int, float))
            or not math.isfinite(self.background_sync_interval)
            or not 300 <= self.background_sync_interval <= 3600
        ):
            raise ConfigError("PROTON_MCP_BACKGROUND_SYNC_INTERVAL must be between 300 and 3600")
        if (
            type(self.background_sync_max_commands) is not int
            or not 100 <= self.background_sync_max_commands <= 5000
        ):
            raise ConfigError(
                "PROTON_MCP_BACKGROUND_SYNC_MAX_COMMANDS must be between 100 and 5000"
            )
        if type(self.identity_index_enabled) is not bool:
            raise ConfigError("PROTON_MCP_IDENTITY_INDEX_ENABLED must be boolean")
        if type(self.index_autorebuild_enabled) is not bool:
            raise ConfigError("PROTON_MCP_INDEX_AUTOREBUILD_ENABLED must be boolean")
        if (
            type(self.search_index_catchup_max_messages) is not int
            or not 0 <= self.search_index_catchup_max_messages <= 10000
        ):
            raise ConfigError(
                "PROTON_MCP_SEARCH_INDEX_CATCHUP_MAX_MESSAGES must be between 0 and 10000"
            )
        if type(self.reindex_batch_size) is not int or not 1 <= self.reindex_batch_size <= 200:
            raise ConfigError("PROTON_MCP_REINDEX_BATCH_SIZE must be between 1 and 200")
        if (
            isinstance(self.reindex_batch_sleep, bool)
            or not isinstance(self.reindex_batch_sleep, (int, float))
            or not math.isfinite(self.reindex_batch_sleep)
            or not 0.01 <= self.reindex_batch_sleep <= 60
        ):
            raise ConfigError("PROTON_MCP_REINDEX_BATCH_SLEEP must be between 0.01 and 60")
        if type(self.rules_max_moves) is not int or not 1 <= self.rules_max_moves <= 200:
            raise ConfigError("PROTON_MCP_RULES_MAX_MOVES must be between 1 and 200")
        if (
            type(self.approval_max_messages) is not int
            or not 200 <= self.approval_max_messages <= APPROVAL_MAX_CEILING
        ):
            raise ConfigError(
                f"PROTON_MCP_APPROVAL_MAX_MESSAGES must be between 200 and {APPROVAL_MAX_CEILING}"
            )
        for name in (
            "enable_send",
            "smtp_tls",
            "smtp_allow_unverified",
            "send_self_no_form",
            "send_typed_ok",
        ):
            if type(getattr(self, name)) is not bool:
                raise ConfigError("Send switches must be boolean")
        if type(self.confirm_mutations) is not bool:
            raise ConfigError("PROTON_MCP_CONFIRM_MUTATIONS must be boolean")
        if type(self.apps_enabled) is not bool:
            raise ConfigError("PROTON_MCP_APPS_ENABLED must be boolean")
        if type(self.enable_rules) is not bool:
            raise ConfigError("PROTON_MCP_ENABLE_RULES must be boolean")
        if self.time_zone is not None:
            try:
                ZoneInfo(self.time_zone)
            except (TypeError, ValueError, ZoneInfoNotFoundError):
                raise ConfigError(
                    "PROTON_MCP_TIMEZONE must be an IANA time zone name such as Europe/Berlin"
                ) from None
        if (
            isinstance(self.confirm_timeout, bool)
            or not isinstance(self.confirm_timeout, (int, float))
            or not math.isfinite(self.confirm_timeout)
            or not 0 < self.confirm_timeout <= 300
        ):
            raise ConfigError("PROTON_MCP_CONFIRM_TIMEOUT must be positive and at most 300")
        if self.smtp_host not in {"127.0.0.1", "::1"}:
            raise ConfigError("SMTP requires the literal loopback address 127.0.0.1 or ::1")
        for name, maximum in (("send_max_recipients", 50), ("send_daily_cap", 200)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ConfigError(f"{name} must be an integer between 1 and {maximum}")
        for name, maximum in (("send_token_ttl", 600), ("send_confirm_timeout", 300)):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= maximum:
                raise ConfigError(f"{name} must be positive and at most {maximum} seconds")
        if not isinstance(self.send_from_allowlist, tuple) or any(
            not is_bare_address(address) for address in self.send_from_allowlist
        ):
            raise ConfigError("Send From allowlist requires exact ASCII addresses")
        if not isinstance(self.send_typed_ok_clients, tuple):
            raise ConfigError("PROTON_MCP_SEND_TYPED_OK_CLIENTS must be a list of client names")
        if type(self.unsubscribe_max_batch) is not int or not 1 <= self.unsubscribe_max_batch <= 50:
            raise ConfigError("PROTON_MCP_UNSUBSCRIBE_MAX_BATCH must be between 1 and 50")
        if (
            not isinstance(self.token, str)
            or len(self.token) < 32
            or not self.token.isascii()
            or any(ord(char) <= 32 or ord(char) >= 127 for char in self.token)
        ):
            raise ConfigError(
                "PROTON_MCP_TOKEN must contain at least 32 printable ASCII characters"
            )
        if self.host != "127.0.0.1":
            raise ConfigError("PROTON_MCP_HOST must be 127.0.0.1")
        if self.credential_store not in CREDENTIAL_STORES:
            raise ConfigError(
                "PROTON_MCP_CREDENTIAL_STORE must be keychain, libsecret, pass or wincred"
            )
        for key, value in self.credential_locations:
            if (
                key not in CREDENTIAL_LOCATION_KEYS
                or not os.path.isabs(value)
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                raise ConfigError(f"{key} must be an absolute path")
        if self.attachments_dir is not None and not valid_attachments_dir(self.attachments_dir):
            raise ConfigError(
                "PROTON_MCP_ATTACHMENTS_DIR must be a normalized absolute path below the "
                "filesystem root, with no trailing slash"
            )
        # Windows stat shows no owner or mode bits, so this reads the real access rules, of
        # the default state folder too when any path falls back to it: LOCALAPPDATA proves nothing.
        if sys.platform == "win32":
            defaulted = None in (self.action_log_path, self.identity_index_dir)
            for name, value in (
                ("PROTON_MCP_ACTION_LOG", self.action_log_path),
                ("PROTON_MCP_SAVED_RULES", self.saved_rules_path),
                ("PROTON_MCP_IDENTITY_INDEX_DIR", self.identity_index_dir),
                ("PROTON_MCP_ATTACHMENTS_DIR", self.attachments_dir),
                ("The default state folder", localfs.state_home() if defaulted else None),
            ):
                refusal = localfs.path_refusal(value) if value else None
                if refusal:
                    raise ConfigError(f"{name} {refusal}")
        for port in (self.port, self.imap_port, self.smtp_port):
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                raise ConfigError("Ports must be integers between 1 and 65535")
        for value in (self.bridge_user, self.imap_host, self.drafts_folder):
            if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ConfigError("Bridge account, host, and drafts folder must be nonempty text")
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 300:
            raise ConfigError("PROTON_BRIDGE_TIMEOUT must be greater than zero and at most 300")
        if (
            isinstance(self.call_timeout, bool)
            or not math.isfinite(self.call_timeout)
            or not 0 < self.call_timeout <= 300
        ):
            raise ConfigError("PROTON_MCP_CALL_TIMEOUT must be greater than zero and at most 300")
        if (
            isinstance(self.execute_timeout, bool)
            or not math.isfinite(self.execute_timeout)
            or not 0 < self.execute_timeout <= 300
        ):
            raise ConfigError("PROTON_MCP_EXECUTE_TIMEOUT must be positive and at most 300")
        if self.preview_timeout is not None and (
            isinstance(self.preview_timeout, bool)
            or not math.isfinite(self.preview_timeout)
            or not 0 < self.preview_timeout <= 300
        ):
            raise ConfigError("PROTON_MCP_PREVIEW_TIMEOUT must be positive and at most 300")
        if self.preview_timeout is not None and self.preview_timeout > self.call_timeout:
            raise ConfigError("PROTON_MCP_PREVIEW_TIMEOUT must not exceed PROTON_MCP_CALL_TIMEOUT")
        if (
            isinstance(self.cleanup_token_ttl, bool)
            or not math.isfinite(self.cleanup_token_ttl)
            or not 0 < self.cleanup_token_ttl <= 1800
        ):
            raise ConfigError("PROTON_MCP_CLEANUP_TOKEN_TTL must be positive and at most 1800")
        if self.public_host and not re.fullmatch(
            r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", self.public_host
        ):
            raise ConfigError(
                "PROTON_MCP_PUBLIC_HOST must be a hostname without scheme, port, or path"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if env is None else env
        try:
            return cls(
                token=values.get("PROTON_MCP_TOKEN", ""),
                bridge_user=values.get("PROTON_BRIDGE_USER", ""),
                host=values.get("PROTON_MCP_HOST", "127.0.0.1"),
                port=int(values.get("PROTON_MCP_PORT", "8765")),
                imap_host=values.get("PROTON_BRIDGE_HOST", "127.0.0.1"),
                imap_port=int(values.get("PROTON_BRIDGE_IMAP_PORT", "1143")),
                imap_cert_path=values.get("PROTON_BRIDGE_CERT") or None,
                drafts_folder=values.get("PROTON_BRIDGE_DRAFTS_FOLDER", "Drafts"),
                timeout=float(values.get("PROTON_BRIDGE_TIMEOUT", "30")),
                call_timeout=float(values.get("PROTON_MCP_CALL_TIMEOUT", "120")),
                execute_timeout=float(
                    values.get(
                        "PROTON_MCP_EXECUTE_TIMEOUT", values.get("PROTON_MCP_CALL_TIMEOUT", "120")
                    )
                ),
                preview_timeout=(
                    float(values["PROTON_MCP_PREVIEW_TIMEOUT"])
                    if "PROTON_MCP_PREVIEW_TIMEOUT" in values
                    else None
                ),
                public_host=values.get("PROTON_MCP_PUBLIC_HOST") or None,
                cleanup_token_ttl=float(values.get("PROTON_MCP_CLEANUP_TOKEN_TTL", "600")),
                protected_config_path=values.get("PROTON_MCP_PROTECTED_CONFIG") or None,
                action_log_path=values.get("PROTON_MCP_ACTION_LOG") or None,
                saved_rules_path=values.get("PROTON_MCP_SAVED_RULES") or None,
                attachments_dir=(
                    values.get("PROTON_MCP_ATTACHMENTS_DIR") or default_attachments_dir(values)
                ),
                attachments_dir_default=not values.get("PROTON_MCP_ATTACHMENTS_DIR"),
                identity_index_enabled=_flag(
                    {"PROTON_MCP_IDENTITY_INDEX_ENABLED": "1", **values},
                    "PROTON_MCP_IDENTITY_INDEX_ENABLED",
                ),
                identity_index_dir=values.get("PROTON_MCP_IDENTITY_INDEX_DIR") or None,
                search_index_catchup_max_messages=int(
                    values.get("PROTON_MCP_SEARCH_INDEX_CATCHUP_MAX_MESSAGES", "2000")
                ),
                index_autorebuild_enabled=_flag(
                    {"PROTON_MCP_INDEX_AUTOREBUILD_ENABLED": "1", **values},
                    "PROTON_MCP_INDEX_AUTOREBUILD_ENABLED",
                ),
                reindex_batch_size=int(values.get("PROTON_MCP_REINDEX_BATCH_SIZE", "100")),
                reindex_batch_sleep=float(values.get("PROTON_MCP_REINDEX_BATCH_SLEEP", "0.25")),
                imap_idle_enabled=_flag(values, "PROTON_MCP_IMAP_IDLE_ENABLED"),
                imap_idle_folders=_idle_folders(
                    values.get("PROTON_MCP_IMAP_IDLE_FOLDERS", '["INBOX"]')
                ),
                imap_idle_poll_interval=float(
                    values.get("PROTON_MCP_IMAP_IDLE_POLL_INTERVAL", "60")
                ),
                background_sync_enabled=_flag(values, "PROTON_MCP_BACKGROUND_SYNC_ENABLED"),
                background_sync_interval=float(
                    values.get("PROTON_MCP_BACKGROUND_SYNC_INTERVAL", "900")
                ),
                background_sync_max_commands=int(
                    values.get("PROTON_MCP_BACKGROUND_SYNC_MAX_COMMANDS", "1500")
                ),
                unsubscribe_max_batch=int(values.get("PROTON_MCP_UNSUBSCRIBE_MAX_BATCH", "50")),
                rules_max_moves=int(values.get("PROTON_MCP_RULES_MAX_MOVES", "50")),
                approval_max_messages=int(values.get("PROTON_MCP_APPROVAL_MAX_MESSAGES", "500")),
                confirm_mutations=_flag(
                    {"PROTON_MCP_CONFIRM_MUTATIONS": "1", **values},
                    "PROTON_MCP_CONFIRM_MUTATIONS",
                ),
                confirm_timeout=float(values.get("PROTON_MCP_CONFIRM_TIMEOUT", "120")),
                apps_enabled=_flag(
                    {"PROTON_MCP_APPS_ENABLED": "1", **values}, "PROTON_MCP_APPS_ENABLED"
                ),
                enable_rules=_flag(values, "PROTON_MCP_ENABLE_RULES"),
                time_zone=values.get("PROTON_MCP_TIMEZONE") or None,
                enable_send=_flag(values, "PROTON_MCP_ENABLE_SEND"),
                smtp_host=values.get("PROTON_BRIDGE_SMTP_HOST", "127.0.0.1"),
                smtp_port=int(values.get("PROTON_BRIDGE_SMTP_PORT", "1025")),
                smtp_tls=_flag(values, "PROTON_BRIDGE_SMTP_TLS"),
                smtp_allow_unverified=_flag(values, "PROTON_BRIDGE_SMTP_ALLOW_UNVERIFIED"),
                send_from_allowlist=tuple(
                    entry.strip()
                    for entry in values.get("PROTON_MCP_SEND_FROM", "").split(",")
                    if entry.strip()
                ),
                send_max_recipients=int(values.get("PROTON_MCP_SEND_MAX_RECIPIENTS", "10")),
                send_daily_cap=int(values.get("PROTON_MCP_SEND_DAILY_CAP", "20")),
                send_token_ttl=float(values.get("PROTON_MCP_SEND_TOKEN_TTL", "300")),
                send_self_no_form=_flag(values, "PROTON_MCP_SEND_SELF_NO_FORM"),
                send_typed_ok=_flag(
                    {"PROTON_MCP_SEND_TYPED_OK": "1", **values}, "PROTON_MCP_SEND_TYPED_OK"
                ),
                send_typed_ok_clients=tuple(
                    entry.strip().casefold()
                    for entry in values.get("PROTON_MCP_SEND_TYPED_OK_CLIENTS", "").split(",")
                    if entry.strip()
                ),
                send_confirm_timeout=float(values.get("PROTON_MCP_SEND_CONFIRM_TIMEOUT", "120")),
                credential_store=values.get(
                    "PROTON_MCP_CREDENTIAL_STORE", default_credential_store()
                ),
                credential_locations=tuple(
                    (key, values[key]) for key in CREDENTIAL_LOCATION_KEYS if values.get(key)
                ),
            )
        except ConfigError:
            raise
        except (TypeError, ValueError):
            raise ConfigError("Invalid numeric environment configuration") from None
