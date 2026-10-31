"""Two-step mailbox cleanup with short-lived capabilities for untouched approved rows."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

from . import mail
from .action_log import ActionLog, new_record, token_digest
from .canonical import mailbox_roles, resolve_samples, row_identity
from .cleanup_folders import _move_folders
from .cleanup_imap import _ACTIONS as _IMAP_ACTIONS
from .cleanup_imap import _BATCHED_ACTIONS as _IMAP_BATCHED_ACTIONS
from .cleanup_imap import command_group_counts
from .cleanup_lookup import (
    PreviewExclusions,
    _IncompleteIdentityInventory,
    _inventory_state,
    _verify_inventory,
    display_row,
    display_value,
    fetch_rows,
    folder_inventory,
    identity_context,
    matching_rows,
    message_fingerprint,
)
from .config import APPROVAL_MAX_CEILING
from .mail import (
    CallDeadlineExceeded,
    MailError,
    MailValidationError,
    _Deadline,
    _imap_date,
    _message_id,
    _quote,
)
from .mail import _message_id as _cleanup_message_id
from .protected import ProtectedPolicy
from .rules_source import ReadStateSourceError, require_read_state_source
from .search import _uidvalidity
from .sender_matching import sender_matches

MAX_BATCH = 200
# One message's own evidence: a new rules batch drops it, the approval stays strict.
_ROW_REFUSALS = frozenset({"inconsistent_sender_headers", "ambiguous_label_membership"})
# A bounded approval may cover more than MAX_BATCH messages through server-side
# continuations, up to the configured PROTON_MCP_APPROVAL_MAX_MESSAGES and never
# above this ceiling. Every IMAP command and journal chunk keeps MAX_BATCH.
MAX_APPROVAL = APPROVAL_MAX_CEILING
# Bound total pending approved rows to the pre-existing worst case (1,000
# grants of 200 rows), however large single bounded approvals are.
MAX_PENDING_ROWS = 1000 * MAX_BATCH
# Starting an approved batch gives its untouched remainder one fixed window.
# Claims of later continuations never slide this deadline forward.
RESUME_TOKEN_TTL_SECONDS = 1800
# Per-message label actions (add_label, remove_label). Planning allowance: twice
# the 1 s/MOVE slow-peer calibration in the chunk regression, including final UID
# checks. This is not a live Bridge measurement or latency guarantee; the socket
# watchdog and partial journal remain essential.
EXECUTE_MUTATION_SECONDS_PER_MESSAGE = 2.0
EXECUTE_CHUNK_SIZE = 50
# Moves and read-state changes send one UID STORE and/or one UID MOVE per source
# folder for a chunk of up to 200 UIDs. Each such command keeps the two-second
# allowance of a former single-UID command (including its first UID and the
# group's writable SELECT, final FETCH and outcome checks); every further UID
# adds 0.05 seconds. A one-UID chunk thus plans exactly as before. Observed chunk
# costs can raise both allowances; neither is a live Bridge measurement.
BATCHED_ACTIONS = frozenset(_IMAP_BATCHED_ACTIONS)
EXECUTE_BATCH_SIZE = MAX_BATCH
EXECUTE_BATCH_SECONDS = 2.0
EXECUTE_BATCH_SECONDS_PER_MESSAGE = 0.05
EXECUTE_LOG_RESERVE_SECONDS = 1.0
# A guarded confirmation gets at most 48 s, including its existing response
# reserve (10%, capped at 5 s). Thus mailbox I/O stops by 43.2 s at this ceiling,
# leaving 4.8 s for the response inside the ceiling and 2 s below the
# 50 s client-facing bound. Keep the 1 s journal and 2/2.05 s per-row allowances.
# 48 s also preserves the existing configured 48 s chunk-capacity contract.
# The configured budget and response ceiling both retain their actual reserves.
EXECUTE_RESPONSE_BUDGET_S = 48.0
# Removal also refreshes the surviving All Mail handle during read-only preflight;
# its complete inventory is already cached by resolution.
REMOVE_LABEL_PREFLIGHT_SECONDS_PER_MESSAGE = 0.05
ACTIONS = frozenset(_IMAP_ACTIONS)
_QUERY_KEYS = frozenset({"folder", "from_", "to", "subject", "text", "since", "before"})
# Approvals that bind a name, script or rule and never any message rows.
_NO_MESSAGE_ACTIONS = frozenset({"create_folder", "create_sieve_filter", "add_rule"})


def _safe_name(value: str) -> str:
    """Quote untrusted diagnostic names, including controls, with bounded output."""
    return json.dumps(value[:64] + ("..." if len(value) > 64 else ""), ensure_ascii=True)


class _MoveSourceError(MailValidationError):
    """A preview can report a refused move source without issuing a capability."""


class _ImplicitSourceChanged(MailValidationError):
    """A source-discovery certificate no longer proves absence of other copies."""


class _ResponseBudgetReached(MailValidationError):
    """Yield at a chunk boundary without changing configured admission policy."""


class _BoundVerificationRefused(MailValidationError):
    """Bound resolution found invalid evidence; isolate it on a continuation."""


class _ContinuationSourceChanged(MailValidationError):
    """A continuation cannot cheaply prove that source discovery is unchanged."""


def _address_criteria(name: str, value: str) -> tuple[Any, ...]:
    """SEARCH keys that every From or To header containing the value satisfies.

    Address rendering can remove quotes/comments or insert spaces. Search a
    stable word, including escaped/encoded raw headers whose reconstructed word
    may not be literal. A value without such a word cannot narrow the search.
    """
    word = max(re.findall(r"[a-z0-9]+", value.casefold()), key=len, default="")
    if not word:
        return ()
    return ("OR", "OR", name, _quote(word), name, _quote("\\"), name, _quote("=?"))


def _digest(value: Any) -> bytes:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).digest()


def _fingerprint(messages: list[dict[str, Any]]) -> bytes:
    # Includes both stable identity and sender/membership data. A policy-relevant
    # header, mailbox membership or personal-provider allowlist change requires
    # a fresh preview, including the exact bypass evidence shown for each row.
    return _digest(
        sorted(
            messages,
            key=lambda row: tuple(
                row.get(key, "") for key in ("folder", "uidvalidity", "uid", "message_id")
            ),
        )
    )


def approval_hash(
    action: str, params: dict[str, Any], selector: dict[str, Any], messages: list[dict[str, Any]]
) -> str:
    """Bind one exact action set: action, targets and params, selector and message rows.

    Every confirmation token (cleanup, rules, unsubscribe, folder and Sieve
    filter creation, and send) stores this digest. Row lists use the execution
    fingerprint, so any identity, sender, flag, target, script or draft change
    yields a different hash.
    """

    def rows(value: Any) -> Any:
        if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
            return {"rows": _fingerprint(value).hex()}
        return value

    canonical = {
        "action": action,
        "params": params,
        "selector": {key: rows(value) for key, value in selector.items()},
        "messages": _fingerprint(messages).hex(),
    }
    return hashlib.sha256(b"proton-mail-mcp:approval:v1\0" + _digest(canonical)).hexdigest()


def _handle(row: dict[str, Any]) -> tuple[str, str, str]:
    return tuple(row[key] for key in ("folder", "uidvalidity", "uid"))


def _batch_command_seconds(action: str, params: dict[str, Any]) -> float:
    """Planned seconds for one batched command group: its STORE and/or MOVE."""
    commands = 2 if action == "move_to_folder" and params.get("mark_read") else 1
    return commands * EXECUTE_BATCH_SECONDS


def _approval_cap(selector: Any) -> int:
    """Rows one approval may bind: 200, or the bound its preview stored in the selector."""
    bound = selector.get("max_messages") if isinstance(selector, dict) else None
    if bound is None:
        return MAX_BATCH
    if type(bound) is not int or not MAX_BATCH < bound <= MAX_APPROVAL:
        raise MailValidationError("Preview token has an invalid approval bound")
    return bound


@dataclass(repr=False)
class _Grant:
    action: str
    params: dict[str, Any]
    selector: dict[str, Any]
    messages: list[dict[str, Any]]
    expires: float
    expires_at: str
    fingerprint: bytes = field(repr=False)
    verification_seconds: float = 0.0
    inventory_seconds: float = 0.0
    mutation_seconds: float = field(default_factory=lambda: EXECUTE_MUTATION_SECONDS_PER_MESSAGE)
    # Observed batched chunk allowances, carried to continuations.
    batch_row_seconds: float = field(default_factory=lambda: EXECUTE_BATCH_SECONDS_PER_MESSAGE)
    batch_chunk_seconds: float = field(default_factory=lambda: EXECUTE_BATCH_SECONDS)
    bounded_recovery: bool = False
    run_id: str | None = None
    run_token_digest: str | None = None
    approved_total: int = 0
    run_done: int = 0
    run_uncertain: int = 0
    blocked: list[dict[str, Any]] = field(default_factory=list)
    chunk_index: int = 0
    implicit_source_progress: dict[str, str] = field(default_factory=dict)
    token_digests: tuple[bytes, ...] = field(default_factory=tuple, repr=False)
    # The previewed approval; continuations keep it for confirmation and audit.
    action_hash: str = ""
    # Digest of this grant's current, possibly narrowed, action set.
    binding: str = field(default="", repr=False)
    # Continuations inherit the user's confirmation of their approval.
    confirmed: bool = True
    resume_started: bool = False


class TokenStore:
    """Keep digests only; atomically claim every token alias for one approved remainder."""

    def __init__(
        self,
        ttl: float = 600,
        *,
        clock: Callable[[], float] = time.monotonic,
        confirmation_required: bool = False,
    ):
        if isinstance(ttl, bool) or not math.isfinite(ttl) or not 0 < ttl <= 1800:
            raise MailValidationError(
                "Cleanup token lifetime must be positive and at most 1800 seconds"
            )
        self.ttl = ttl
        self.clock = clock
        # The MCP server enables this for the tokens it hands to clients: no
        # lookup can then redeem an approval before a host form confirmed it.
        self.confirmation_required = confirmation_required
        self._grants: dict[bytes, _Grant] = {}
        # Bounded digest/expiry tombstones distinguish expiry after automatic
        # pruning or a call that outlives its approval. They retain no approval
        # and can never authorize a retry.
        self._retired: dict[bytes, float] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        action: str,
        params: dict[str, str],
        selector: dict[str, Any],
        messages: list[dict[str, Any]],
        *,
        sole: bool = False,
    ) -> tuple[str, str]:
        """Issue an approval; sole retires every live one for this action and selector."""
        if action in _NO_MESSAGE_ACTIONS:
            if messages:
                raise MailValidationError(
                    "Folder or filter creation cannot authorize message changes"
                )
        elif not messages or len(messages) > _approval_cap(selector):
            raise MailValidationError(
                f"Cleanup batches must contain between 1 and {_approval_cap(selector)} messages"
            )
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).digest()
        now = self.clock()
        expires_at = datetime.fromtimestamp(time.time() + self.ttl, timezone.utc).isoformat()
        binding = approval_hash(action, params, selector, messages)
        grant = _Grant(
            action,
            copy.deepcopy(params),
            copy.deepcopy(selector),
            copy.deepcopy(messages),
            now + self.ttl,
            expires_at,
            _fingerprint(messages),
            token_digests=(digest,),
            action_hash=binding,
            binding=binding,
            confirmed=not self.confirmation_required,
        )
        with self._lock:
            self._prune_expired(self.clock())
            if sole:
                for key, live in list(self._grants.items()):
                    if live.action == action and live.selector == selector:
                        self._retire(key, self._grants.pop(key).expires)
            # Bound memory without invalidating a still-valid approval silently.
            if len(self._grants) >= 1000:
                raise MailValidationError("Too many pending cleanup previews; wait for expiry")
            # Continuation aliases share one grant; count each approval's rows once.
            pending = {id(item): len(item.messages) for item in self._grants.values()}
            if sum(pending.values()) + len(messages) > MAX_PENDING_ROWS:
                raise MailValidationError("Too many pending approved messages; wait for expiry")
            self._grants[digest] = grant
        return token, expires_at

    def claim(
        self,
        token: str,
        action: str,
        *,
        params: dict[str, str] | None = None,
        ids: list[str] | None = None,
    ) -> _Grant:
        grant = self._lookup(token, action, params=params, ids=ids, consume=True)
        if action in ACTIONS | {"apply_rules"} and not grant.resume_started:
            # Only a valid, confirmed claim starts the batch lifetime. Read-only
            # attempts and expired previews cannot obtain a fresh window.
            grant.expires = self.clock() + RESUME_TOKEN_TTL_SECONDS
            grant.expires_at = datetime.fromtimestamp(
                time.time() + RESUME_TOKEN_TTL_SECONDS, timezone.utc
            ).isoformat()
            grant.resume_started = True
        return grant

    def record_verification_cost(
        self, token: str, seconds: float, *, bounded_recovery: bool = False
    ) -> None:
        digest = hashlib.sha256(token.encode("ascii")).digest()
        with self._lock:
            grant = self._grants[digest]
            grant.bounded_recovery = bounded_recovery
            if (
                bounded_recovery
                and grant.action in {"move_to_folder", "move_to_trash", "archive"}
                and all("label_states" in row for row in grant.messages)
            ):
                # Certified confirmation fetches exact bound handles. Preview
                # discovery is one-off work, not a per-row verification sample.
                # Learn this cost from the first actual bounded verification.
                return
            grant.verification_seconds = max(0.0, seconds) / len(grant.messages)
            grant.inventory_seconds = max(0.0, seconds)

    def continue_grant(self, grant: _Grant, messages: list[dict[str, Any]]) -> str | None:
        """Publish a durable remainder under a new token and its prior digest aliases.

        A lost response cannot strand the client: the token it already holds
        names this same untouched remainder. Claim removes every alias at once;
        only a proven partial result may restore them, never completed rows.
        """
        if not messages:
            return None
        original = {_handle(row): row for row in grant.messages}
        if len({_handle(row) for row in messages}) != len(messages) or any(
            original.get(_handle(row)) != row for row in messages
        ):
            raise MailValidationError("Continuation exceeds the approved message set")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).digest()
        # The remainder keeps its approval's action hash and gets its own
        # binding, so a lookup still proves it is exactly this verified subset.
        continuation = replace(
            copy.deepcopy(grant),
            messages=copy.deepcopy(messages),
            fingerprint=_fingerprint(messages),
            binding=approval_hash(grant.action, grant.params, grant.selector, messages),
            token_digests=(
                (*grant.token_digests, digest) if grant.action == "move_to_folder" else (digest,)
            ),
        )
        with self._lock:
            self._prune_expired(self.clock())
            if (
                self.clock() >= grant.expires
                or len(self._grants) + len(continuation.token_digests) > 1000
            ):
                return None
            for alias in continuation.token_digests:
                self._grants[alias] = continuation
        return token

    def inspect(
        self,
        token: str,
        action: str,
        *,
        params: dict[str, str] | None = None,
        ids: list[str] | None = None,
    ) -> _Grant:
        """Validate an approval without consuming it during read-only checks."""
        return self._lookup(token, action, params=params, ids=ids, consume=False)

    def action_hash(self, token: Any) -> str | None:
        """Name a live token's approval for audit only: never prunes, consumes or refuses."""
        if not isinstance(token, str) or not token.isascii() or len(token) > 256:
            return None
        candidate = hashlib.sha256(token.encode("ascii")).digest()
        with self._lock:
            match = next((key for key in self._grants if hmac.compare_digest(key, candidate)), None)
            return self._grants[match].action_hash if match is not None else None

    def describe(self, token: str, action: str) -> _Grant:
        """Copy an approval for its confirmation summary; never consumes or confirms it."""
        return self._lookup(token, action, params=None, ids=None, consume=False, confirmed=False)

    def confirm(self, token: str, action: str, action_hash: str) -> None:
        """Record the user's host-form acceptance of exactly the summarized approval."""
        grant = self.describe(token, action)
        if not hmac.compare_digest(grant.action_hash, action_hash):
            raise MailValidationError("Preview token changed during confirmation; preview again")
        with self._lock:
            for alias in grant.token_digests:
                stored = self._grants.get(alias)
                if stored is not None and hmac.compare_digest(stored.action_hash, action_hash):
                    stored.confirmed = True

    def revoke(self, token: str, action: str) -> None:
        """Retire an approval whose confirmation was declined, failed or cancelled."""
        grant = self.describe(token, action)
        with self._lock:
            for alias in grant.token_digests:
                if self._grants.pop(alias, None) is not None:
                    self._retire(alias, grant.expires)

    def continuation_state(self, run_id: str) -> dict[str, Any]:
        """Expose live recovery metadata without recovering a capability secret."""
        with self._lock:
            now = self.clock()
            for grant in self._grants.values():
                if grant.run_id == run_id and grant.expires > now:
                    return {
                        "available": True,
                        "expires_at": grant.expires_at,
                        "remaining": len(grant.messages),
                    }
        return {"available": False, "expires_at": None, "remaining": 0}

    def _prune_expired(self, now: float) -> None:
        """Remove stale digests while the caller holds the store lock."""
        for key in [key for key, grant in self._grants.items() if grant.expires <= now]:
            self._retire(key, self._grants.pop(key).expires)

    def _retire(self, digest: bytes, expires: float) -> None:
        self._retired[digest] = expires
        if len(self._retired) > 1000:
            del self._retired[next(iter(self._retired))]

    def prune_expired(self) -> None:
        """Release approvals that expired during a long cleanup or undo call."""
        with self._lock:
            self._prune_expired(self.clock())

    def _lookup(
        self,
        token: str,
        action: str,
        *,
        params: dict[str, str] | None,
        ids: list[str] | None,
        consume: bool,
        confirmed: bool = True,
    ) -> _Grant:
        candidate = (
            hashlib.sha256(token.encode("ascii")).digest()
            if isinstance(token, str) and token.isascii() and len(token) <= 256
            else None
        )
        with self._lock:
            now = self.clock()
            match = (
                next((key for key in self._grants if hmac.compare_digest(key, candidate)), None)
                if candidate is not None
                else None
            )
            # Remember the requested grant before pruning so its first expired
            # lookup retains the actionable expiry error before retirement.
            grant = self._grants.get(match)
            self._prune_expired(now)
            if match is None:
                if candidate is not None and any(
                    hmac.compare_digest(key, candidate) and expires <= now
                    for key, expires in self._retired.items()
                ):
                    raise MailValidationError(
                        "Invalid or already used preview token; expired; request a new preview"
                    )
                raise MailValidationError("Invalid or already used preview token")
            if now >= grant.expires:
                raise MailValidationError("Preview token expired; request a new preview")
            if confirmed and not grant.confirmed:
                # Refuse before consuming: the model alone can never redeem it.
                raise MailValidationError(
                    "Preview token needs the user's confirmation in the client form; "
                    "call the action again to request it"
                )
            if consume:
                # The original preview and every previously returned continuation
                # are one capability for the current remainder, never independent
                # approvals. Hide all of them until durable progress is published.
                for alias in grant.token_digests:
                    self._grants.pop(alias, None)
                    self._retire(alias, grant.expires)
            else:
                grant = copy.deepcopy(grant)
            now = self.clock()
            if now >= grant.expires:
                self._prune_expired(now)
                raise MailValidationError("Preview token expired; request a new preview")
        if grant.action != action:
            raise MailValidationError("Preview token does not match the requested action")
        if not hmac.compare_digest(
            grant.binding,
            approval_hash(grant.action, grant.params, grant.selector, grant.messages),
        ):
            raise MailValidationError(
                "Preview token does not match its approved action set; request a new preview"
            )
        if params is not None and params != grant.params:
            raise MailValidationError("Preview token does not match the requested params")
        if ids is not None:
            requested_ids = sorted(_cleanup_message_id(identifier) for identifier in ids)
            if requested_ids != sorted(row["message_id"] for row in grant.messages):
                raise MailValidationError("Preview token does not match the requested message set")
        return grant


def _params(action: str, params: dict[str, Any] | None) -> dict[str, Any]:
    if action not in ACTIONS:
        raise MailValidationError("Unsupported cleanup action")
    params = {} if params is None else params
    required = (
        {"folder"}
        if action == "move_to_folder"
        else ({"label"} if action in {"add_label", "remove_label"} else set())
    )
    optional = {"mark_read", "create_folder_if_missing"} if action == "move_to_folder" else set()
    if (
        not isinstance(params, dict)
        or not required <= set(params)
        or set(params) - required - optional
    ):
        raise MailValidationError("Cleanup params do not match the selected action")
    result = {}
    for key, value in params.items():
        if key == "mark_read":
            if type(value) is not bool:
                raise MailValidationError("mark_read must be a boolean")
            result[key] = value
            continue
        if key == "create_folder_if_missing":
            if type(value) is not bool:
                raise MailValidationError("create_folder_if_missing must be a boolean")
            if value:
                # The preview keeps this only when it shows the creation.
                result[key] = True
            continue
        _quote(value)
        if not value or value != value.strip():
            raise MailValidationError(
                "Cleanup target must be nonempty text without surrounding spaces"
            )
        if key == "label" and value.startswith("Labels/"):
            value = value.removeprefix("Labels/")
        if not value or any(part in {"", ".", ".."} for part in value.split("/")):
            raise MailValidationError("Cleanup target contains an invalid path component")
        if key == "folder" and not value.startswith("Folders/"):
            raise MailValidationError("Move targets must be existing mailboxes under Folders/")
        result[key] = value
    if action == "move_to_folder":
        result.setdefault("mark_read", False)
        if result.get("create_folder_if_missing"):
            from .create_folder import _folder_name

            # A folder this approval may create must meet create_folder's rules.
            _folder_name(result["folder"])
    return result


def _batch_limit(limit: int | None) -> int | None:
    if limit is not None and (type(limit) is not int or not 1 <= limit <= MAX_BATCH):
        raise MailValidationError("limit must be a positive integer at most 200")
    return limit


def _preview_limit(limit: int | None, bound: int) -> int | None:
    """A preview limit above 200 asks for one bounded approval of at most bound rows."""
    if limit is not None and (type(limit) is not int or not 1 <= limit <= bound):
        raise MailValidationError(
            f"limit must be a positive integer at most {bound} (PROTON_MCP_APPROVAL_MAX_MESSAGES)"
        )
    return limit


def _selector(query: dict[str, Any] | None, ids: list[str] | None) -> dict[str, Any]:
    if (query is None) == (ids is None):
        raise MailValidationError("Provide exactly one of query or ids")
    if ids is not None:
        if not isinstance(ids, list) or not ids:
            raise MailValidationError("ids must be a nonempty list of Message-ID headers")
        normalized = [_message_id(value) for value in ids]
        if len(set(normalized)) != len(normalized):
            raise MailValidationError("Duplicate message IDs are not allowed")
        return {"ids": sorted(normalized)}
    supported = ", ".join(sorted(_QUERY_KEYS | {"from"}))
    if not isinstance(query, dict):
        raise MailValidationError("Cleanup query must be an object; supported fields: " + supported)
    bad = [key for key in query if key not in _QUERY_KEYS | {"from"}]
    if bad:
        names = [
            _safe_name(key if isinstance(key, str) else type(key).__name__) for key in bad[:10]
        ]
        suffix = f" (and {len(bad) - 10} more)" if len(bad) > 10 else ""
        raise MailValidationError(
            "Unsupported cleanup query fields: "
            + ", ".join(names)
            + suffix
            + "; supported fields: "
            + supported
        )
    query = dict(query)
    if "from" in query:
        alias = query.pop("from")
        if "from_" in query and query["from_"] != alias:
            raise MailValidationError(
                "Cleanup query from and from_ must agree when both are supplied"
            )
        query["from_"] = alias
    normalized = {}
    for key, value in query.items():
        if value is not None:
            try:
                _quote(value)
            except UnicodeEncodeError:
                raise MailValidationError("Cleanup query requires valid UTF-8 text") from None
            if key in {"since", "before"}:
                _imap_date(value)
            normalized[key] = value
    return {"query": normalized}


class CleanupService:
    """Resolve previews read-only, then refresh only their bound handles before applying."""

    def __init__(
        self,
        client: Any,
        settings: Any = None,
        *,
        backend: Any = None,
        policy: Any = None,
        tokens: TokenStore | None = None,
    ):
        from .cleanup_imap import CleanupMutator

        self.client = client
        self.settings = settings or client.settings
        self.policy = policy or ProtectedPolicy(self.settings.protected_config_path)
        self.tokens = tokens or TokenStore(self.settings.cleanup_token_ttl)
        self.backend = backend or CleanupMutator(client, self.settings, self.policy)
        # Serialize cleanup mutations and undo in this process, preventing two
        # independently approved batches racing against the same local state.
        self._mutation_lock = threading.Lock()

    def _block_reasons(self, row: dict[str, Any]) -> list[str]:
        reasons = [
            reason + " during " + identity_context("source", row["folder"])
            for reason in self.policy.check(row)
        ] + row.get("membership_protection", [])
        if row.get("identity_invalid"):
            reasons.append(
                "Missing or ambiguous Message-ID header during "
                + identity_context("source", row["folder"])
            )
        return reasons

    def _approval_bound(self) -> int:
        """The configured hard upper bound for one approval (200 disables continuations)."""
        bound = getattr(self.settings, "approval_max_messages", MAX_BATCH)
        if type(bound) is not int or not MAX_BATCH <= bound <= MAX_APPROVAL:
            raise MailValidationError("Invalid PROTON_MCP_APPROVAL_MAX_MESSAGES configuration")
        return bound

    def _grant_cap(self, grant: _Grant) -> int:
        """A token's own bound; a lower configured bound refuses rather than narrows."""
        cap = _approval_cap(grant.selector)
        if cap > self._approval_bound():
            raise MailValidationError(
                "Preview token exceeds the configured approval bound; request a new preview"
            )
        return cap

    def _folder_creation(self, action, params, connection):
        """Show the destination's state; bind its creation only while it is missing."""
        from .create_folder import _exists

        name = params["folder"]
        if _exists(connection, name):
            # Nothing to create: an ordinary move into the existing folder.
            params = {
                key: value for key, value in params.items() if key != "create_folder_if_missing"
            }
            self.backend.validate_target(action, params, connection)
            return params, {"name": name, "exists": True, "create": False}
        return params, {"name": name, "exists": False, "create": True}

    def _destination_to_create(self, grant, connection):
        """The approved destination to create before the first move, or None if it exists."""
        if grant.params.get("create_folder_if_missing") is not True:
            return None
        from .create_folder import _exists, _folder_name

        name = _folder_name(grant.params.get("folder"))
        if grant.action != "move_to_folder" or _exists(connection, name):
            return None
        if grant.run_id is not None:
            # Creation happens once, before an approval's first move; a later
            # disappearance is unexpected and needs review, not re-creation.
            raise MailValidationError(
                "Approved destination folder no longer exists; request a new preview"
            )
        return name

    def _blocked(self, messages: list[dict[str, Any]], real: set[str]) -> list[dict[str, Any]]:
        blocked = []
        for row in messages:
            reasons = self._block_reasons(row)
            if reasons:
                blocked.append(
                    {
                        **self._sample(row, real),
                        "reason": "; ".join(reasons),
                        "unresolved": bool(
                            row.get("identity_invalid")
                            or row.get("sender_invalid")
                            or row.get("membership_unresolved")
                        ),
                    }
                )
        return blocked

    @staticmethod
    def _sample(row: dict[str, Any], real: set[str]) -> dict[str, Any]:
        row = display_row(row)
        return {
            **{key: row[key] for key in ("from", "subject", "date")},
            # The same canonical (folder, UIDVALIDITY, UID) as search and threads,
            # from the same LIST-vetted real folders (``real``). A label or All
            # Mail source handle is the alias; previews resolve it as search does.
            **row_identity(row, real),
            "id": row["message_id"],
            "message_id": row["message_id"],
            **(
                {"personal_provider_bypasses": copy.deepcopy(row["personal_provider_bypasses"])}
                if row.get("personal_provider_bypasses")
                else {}
            ),
        }

    def _fetch(
        self, connection: Any, deadline: Any, folder: str, uids: list[str], validity: str
    ) -> list[dict[str, Any]]:
        return fetch_rows(self.client, connection, deadline, folder, uids, validity)

    def _folder_rows(
        self,
        connection: Any,
        deadline: Any,
        folder: str,
        identifiers: list[str],
        *,
        stage: str,
        exclusions: PreviewExclusions | None = None,
    ) -> list[dict[str, Any]]:
        return matching_rows(
            folder_inventory(
                self.client,
                connection,
                deadline,
                folder,
                stage=stage,
                identifiers=identifiers,
                exclusions=exclusions,
            ),
            identifiers,
            stage=stage,
            folder=folder,
            raw_identity=getattr(connection, "_cleanup_raw_identity", False),
            exclusions=exclusions,
        )

    def _resolve(self, connection, deadline, *args, **kwargs):
        try:
            return self._resolve_rows(connection, deadline, *args, **kwargs)
        except MailValidationError as error:
            if isinstance(error, _ContinuationSourceChanged):
                raise
            if kwargs.get("bound_rows") is not None:
                raise _BoundVerificationRefused(str(error)) from None
            raise

    def _resolve_bound(self, connection, deadline, expected, action, params, folders):
        """Refresh exact approved handles without mailbox-wide identity discovery."""
        connection._cleanup_inventory = {}
        connection._cleanup_seen = action in {"mark_read", "mark_unread"} or params.get(
            "mark_read", False
        )
        allowed = _move_folders(connection)
        source_states = expected[0].get("implicit_source_states")
        if source_states is not None:
            if {state["folder"] for state in source_states} != allowed:
                if getattr(connection, "_cleanup_resume_messages", None) is not None:
                    raise _ContinuationSourceChanged(
                        "Move source folders changed; request a new preview"
                    )
                raise _ImplicitSourceChanged("Move source folders changed after preview")
            progress = dict(getattr(connection, "_cleanup_implicit_source_progress", {}))
            for previous in source_states:
                state = self.client._select(connection, previous["folder"])
                boundary = progress.get(previous["folder"], previous["uidnext"])
                if (
                    _uidvalidity(connection) != previous["uidvalidity"]
                    or state["uidnext"] is None
                    or state["uidnext"] != boundary
                ):
                    pending = getattr(connection, "_cleanup_resume_messages", None)
                    if pending is not None:
                        self._verify_source_appends(
                            connection, deadline, previous, state, boundary, pending, len(expected)
                        )
                        progress[previous["folder"]] = state["uidnext"]
                        continue
                    raise _ImplicitSourceChanged("Move source discovery changed after preview")
            connection._cleanup_implicit_source_progress = progress
        labels = expected[0]["label_states"]
        if {state["folder"] for state in labels} != {
            folder for folder in folders if folder.startswith("Labels/")
        }:
            raise MailValidationError("Label membership changed after preview; preview again")
        for previous in labels:
            state = self.client._select(connection, previous["folder"])
            current = {**state, "uidvalidity": _uidvalidity(connection)}
            if current != previous or current["uidnext"] is None:
                raise MailValidationError("Label membership changed after preview; preview again")
        if any(row["folder"] not in allowed for row in expected):
            raise _MoveSourceError("Move sources must be allowed real folders")
        handles = {
            _handle(member): member for row in expected for member in [row, *row["memberships"]]
        }
        fetched = {}
        for folder in dict.fromkeys(handle[0] for handle in handles):
            requested = [member for handle, member in handles.items() if handle[0] == folder]
            if connection._cleanup_seen and any(row["folder"] == folder for row in expected):
                require_read_state_source(folder, connection)
            self.client._select(connection, folder)
            validity = _uidvalidity(connection)
            if {row["uidvalidity"] for row in requested} != {validity}:
                raise MailValidationError(
                    "Mailbox UIDVALIDITY changed after preview during "
                    + identity_context("execute", folder)
                )
            # Whole-approval validation precedes the first mutation, but each
            # wire read remains bounded just like a mutation chunk. Do not
            # repeat source discovery or selection certificates per read batch.
            for offset in range(0, len(requested), EXECUTE_CHUNK_SIZE):
                current = fetch_rows(
                    self.client,
                    connection,
                    deadline,
                    folder,
                    [row["uid"] for row in requested[offset : offset + EXECUTE_CHUNK_SIZE]],
                    validity,
                    stage="execute",
                )
                fetched.update((_handle(row), row) for row in current)
        result = []
        allowlist_digest = self.policy.allowlist_digest()
        for approved in expected:
            row = copy.deepcopy(fetched[_handle(approved)])
            _, row["personal_provider_bypasses"] = self.policy.evaluate(row)
            row["personal_provider_allowlist_digest"] = allowlist_digest
            for member in approved["memberships"]:
                current = fetched[_handle(member)]
                if current.get("identity_invalid") or current["message_id"] != member["message_id"]:
                    # fetch_rows requires one exact raw Message-ID matching its
                    # envelope. Preserve that refusal category on the bound path.
                    raise MailValidationError(
                        "Bound membership Message-ID identity changed during "
                        + identity_context("execute", member["folder"])
                    )
                if (
                    message_fingerprint(current) != message_fingerprint(approved)
                    or self.policy.check(current)
                    or any(
                        current.get(key) != approved.get(key)
                        for key in ("from", "sender", "reply_to", "sender_invalid")
                    )
                ):
                    raise MailValidationError(
                        "Bound membership changed or is protected during "
                        + identity_context("execute", member["folder"])
                    )
            # These memberships have just been checked by their exact handles.
            # Label selection certificates also refuse new or removed labels.
            row["memberships"] = copy.deepcopy(approved["memberships"])
            row["folders"] = list(approved["folders"])
            row["label_states"] = copy.deepcopy(labels)
            if source_states is not None:
                row["implicit_source_states"] = copy.deepcopy(source_states)
            result.append(row)
        return result

    def _verify_source_appends(
        self, connection, deadline, previous, state, boundary, pending, chunk_size
    ):
        """Certify a bounded append delta without repeating source discovery.

        Previously approved source handles remain immutable. A newly allocated
        UID can only be ignored after its raw identity excludes every untouched
        approved row. Large gaps, changed validity and ambiguous evidence refuse
        the continuation rather than launching a mailbox-wide inventory.
        """
        validity = _uidvalidity(connection)
        next_uid = state["uidnext"]
        if (
            validity != previous["uidvalidity"]
            or next_uid is None
            or not 0 < int(next_uid) - int(boundary) <= chunk_size
        ):
            raise _ContinuationSourceChanged(
                "Move source discovery changed beyond the continuation chunk; request a new preview"
            )
        try:
            arrived = fetch_rows(
                self.client,
                connection,
                deadline,
                previous["folder"],
                [str(uid) for uid in range(int(boundary), int(next_uid))],
                validity,
                stage="continuation source append check",
            )
        except MailValidationError:
            # Missing metadata cannot distinguish an expunged allocation gap
            # from an unverified copy, so neither may renew the certificate.
            raise _ContinuationSourceChanged(
                "Move source append identity could not be verified; request a new preview"
            ) from None
        approved_ids = {row["message_id"] for row in pending}
        if any(row.get("identity_invalid") or row["message_id"] in approved_ids for row in arrived):
            raise _ContinuationSourceChanged(
                "Move source identity is ambiguous after preview; request a new preview"
            )

    @staticmethod
    def _advance_implicit_sources(connection, chunk, result):
        """Account only for our proven consecutive destination appends.

        Original preview certificates remain immutable. A gap, stale mapping or
        uncertain outcome cannot authorize a new source-discovery certificate;
        the next check must independently verify the changed source evidence.
        """
        if not chunk or "implicit_source_states" not in chunk[0]:
            return
        states = {state["folder"]: state for state in chunk[0]["implicit_source_states"]}
        progress = dict(getattr(connection, "_cleanup_implicit_source_progress", {}))
        for entry in result["messages"]:
            target = entry.get("destination_folder")
            state = states.get(target)
            if state is None:
                continue
            next_uid = progress.get(target, state["uidnext"])
            if (
                entry.get("status") != "done"
                or entry.get("uidvalidity_after") != state["uidvalidity"]
                or entry.get("uid_after") != next_uid
            ):
                break
            progress[target] = str(int(next_uid) + 1)
        connection._cleanup_implicit_source_progress = progress

    def _resolve_rows(
        self,
        connection: Any,
        deadline: Any,
        selector: dict[str, Any],
        action: str,
        params: dict[str, str],
        folders: list[str],
        *,
        exclude_ids: set[str] | None = None,
        source_uids: set[str] | None = None,
        exclude_handles: set[tuple[str, str, str]] | None = None,
        bound_rows: list[dict] | None = None,
        source_rows: list[dict] | None = None,
        exclusions: PreviewExclusions | None = None,
        row_refusals: PreviewExclusions | None = None,
    ) -> list[dict[str, Any]]:
        # Only callers building a new preview opt in. Execute and apply_rules
        # retain the default strict behavior, including their bound-row revalidation.
        # A rules preview passes row_refusals: _ROW_REFUSALS drop one message only.
        if exclusions is not None and (bound_rows is not None or source_rows is not None):
            raise MailValidationError("Preview exclusions cannot revalidate approved rows")

        def refuse(identifier, reason, message):
            if row_refusals is not None and reason in _ROW_REFUSALS:
                row_refusals.add(identifier, reason)
                return
            if exclusions is None:
                raise MailValidationError(message)
            exclusions.add(identifier, reason)

        implicit_fallback = False
        if (
            bound_rows
            and action in {"move_to_folder", "move_to_trash", "archive"}
            and not getattr(connection, "_cleanup_raw_identity", False)
            and all("label_states" in row for row in bound_rows)
        ):
            try:
                return self._resolve_bound(
                    connection, deadline, bound_rows, action, params, folders
                )
            except _ImplicitSourceChanged:
                # Changed folder allocation can introduce a same-identity copy.
                # Keep full discovery and its original ambiguity refusals.
                implicit_fallback = True
        # Rules may grow a reviewed batch within one preview session. Reuse its
        # identity inventories only while fresh selection checks still match.
        # If they change, preview_rules retains the last completed batch and
        # resumes with new per-call caches; membership checks stay strict.
        # preview_rules initializes this cache on each new per-call proxy.
        if getattr(connection, "_rules_preview_state", None) is None:
            connection._cleanup_inventory = {}
        connection._cleanup_seen = action in {"mark_read", "mark_unread"} or params.get(
            "mark_read", False
        )
        deadline.stage = "source folder listing"
        label_folder = "Labels/" + params["label"] if action == "remove_label" else None
        is_move = action in {
            "archive",
            "move_to_trash",
            "move_to_folder",
            "mark_read",
            "mark_unread",
        }
        move_folders = (
            _move_folders(connection)
            if is_move or ("ids" in selector and not label_folder)
            else set()
        )
        # A rules preview can first count a backlog, then validate one exact
        # batch. Its source handles are pinned but memberships are not approved
        # yet, unlike bound_rows at confirmation.
        pinned_rows = bound_rows if bound_rows is not None else source_rows
        if pinned_rows is not None:
            rows = []
            for folder in dict.fromkeys(row["folder"] for row in pinned_rows):
                deadline.stage = "source lookup in " + _safe_name(folder)
                if connection._cleanup_seen:
                    require_read_state_source(folder, connection)
                requested = [row for row in pinned_rows if row["folder"] == folder]
                self.client._select(connection, folder)
                validity = _uidvalidity(connection)
                if {row["uidvalidity"] for row in requested} != {validity}:
                    raise MailValidationError(
                        "Mailbox UIDVALIDITY changed after preview during "
                        + identity_context("execute", folder)
                    )
                try:
                    fetched = self._fetch(
                        connection, deadline, folder, [row["uid"] for row in requested], validity
                    )
                except _IncompleteIdentityInventory:
                    raise MailValidationError(
                        "Bound messages not found or changed after preview during "
                        + identity_context("execute", folder)
                    ) from None
                by_handle = {_handle(row): row for row in requested}
                for row in fetched:
                    if "rule_index" in by_handle[_handle(row)]:
                        row["rule_index"] = by_handle[_handle(row)]["rule_index"]
                rows.extend(fetched)
        elif "query" in selector:
            query = selector["query"]
            folder = query.get("folder", label_folder or "INBOX")
            if connection._cleanup_seen:
                require_read_state_source(folder, connection)
            if label_folder and folder != label_folder:
                raise MailValidationError("remove_label query must select its Labels/ mailbox")
            if is_move and folder not in move_folders:
                raise _MoveSourceError("Move sources must be allowed real folders")
            if folder not in folders:
                raise MailValidationError("Cleanup source folder does not exist")
            deadline.stage = "source query in " + _safe_name(folder)
            if "text" in query:
                raise MailValidationError(
                    "Cleanup text queries require message bodies; use IDs or envelope fields"
                )
            criteria = []
            for name, key in (("SINCE", "since"), ("BEFORE", "before")):
                if key in query:
                    criteria.extend((name, _imap_date(query[key])))
            # Apply From only through the shared local matcher, as search_mail
            # does. A server's lowercase-only FROM comparison can omit decoded
            # casefold matches such as a raw UTF-8 Straße queried as STRASSE.
            if "to" in query:
                criteria.extend(_address_criteria("TO", query["to"]))
            literal = None
            if "subject" in query:
                # Always narrow by the complete subject term. Servers with
                # weaker Unicode matching may omit candidates; the client
                # casefold check below remains authoritative for returned rows.
                subject = query["subject"]
                criteria.append("SUBJECT")
                if subject.isascii():
                    criteria.append(_quote(subject))
                else:
                    # imaplib appends this literal after all other criteria.
                    try:
                        literal = subject.encode("utf-8")
                    except UnicodeEncodeError:
                        raise MailValidationError(
                            "Cleanup subject requires valid UTF-8 text"
                        ) from None
            rule = query.get("rule")
            if rule is not None:
                # A saved folder rule (internal: public queries cannot name one)
                # narrows the search with its own keys; its conditions decide below.
                if set(query) != {"folder", "rule"}:
                    raise MailValidationError("A saved rule query names only its source folder")
                keys, literal = rule.criteria()
                criteria.extend(keys)
            rows = folder_inventory(
                self.client, connection, deadline, folder, criteria=criteria, literal=literal
            )
            if source_uids is not None:
                rows = [row for row in rows if row["uid"] in source_uids]
            fields = (("to", "to"), ("subject", "subject"))
            rows = [
                row
                for row in rows
                if (
                    "from_" not in query
                    or sender_matches(row["from"], query["from_"], decoded=True)
                )
                and all(
                    query[key].casefold() in display_value(row, field).casefold()
                    for key, field in fields
                    if key in query
                )
                and ("since" not in query or row["internaldate_day"] >= query["since"])
                and ("before" not in query or row["internaldate_day"] < query["before"])
                and (rule is None or rule.matches(row))
            ]
        else:
            identifiers = selector["ids"]
            sources = (
                [label_folder]
                if label_folder
                else [folder for folder in folders if folder in move_folders]
            )
            rows = []
            for folder in sources:
                deadline.stage = "source lookup in " + _safe_name(folder)
                rows.extend(
                    self._folder_rows(
                        connection,
                        deadline,
                        folder,
                        identifiers,
                        stage="source",
                        exclusions=exclusions,
                    )
                )
            if len({row["message_id"] for row in rows}) < len(identifiers):
                for identifier in sorted(set(identifiers) - {row["message_id"] for row in rows}):
                    refuse(
                        identifier,
                        "missing_source",
                        "Some requested messages were not found in a source folder",
                    )
            if connection._cleanup_seen:
                for folder in dict.fromkeys(row["folder"] for row in rows):
                    require_read_state_source(folder, connection)
        # Strict callers keep their original selection order. New previews apply
        # the limit after exclusions below. Confirmation uses stored handles.
        if exclusions is None and pinned_rows is None and selector.get("limit") is not None:
            rows = sorted(
                rows,
                key=lambda row: (row["folder"], int(row["uidvalidity"]), int(row["uid"])),
            )[: selector["limit"]]
        # Rule planning partitions matching physical UIDs, including repeated IDs.
        if "candidate_ids" in selector:
            # An unsubscribe preview can bind a clean subset while retaining
            # its original source query. This internal constraint is not an
            # accepted public query field and cannot widen the approval.
            rows = [row for row in rows if row["message_id"] in selector["candidate_ids"]]
        if exclude_ids:
            rows = [row for row in rows if row["message_id"] not in exclude_ids]
        if exclude_handles:
            rows = [row for row in rows if _handle(row) not in exclude_handles]
        allowlist_digest = self.policy.allowlist_digest()
        for row in rows:
            _, row["personal_provider_bypasses"] = self.policy.evaluate(row)
            row["personal_provider_allowlist_digest"] = allowlist_digest
        identifiers = [row["message_id"] for row in rows]
        if exclusions is None and (
            len(rows) > _approval_cap(selector) or any(row.get("identity_invalid") for row in rows)
        ):
            return rows
        if exclusions is not None:
            for row in rows:
                if row.get("identity_invalid"):
                    exclusions.add(row["message_id"], "invalid_identity")
        single_folder = is_move and "query" in selector
        explicit_source = "folder" in selector.get("query", {})
        if len(set(identifiers)) != len(identifiers) and (
            not single_folder
            or len({row["folder"] for row in rows}) != 1
            or action in {"add_label", "remove_label"}
        ):
            for identifier in dict.fromkeys(identifiers):
                if identifiers.count(identifier) > 1:
                    refuse(
                        identifier,
                        "ambiguous_source",
                        "Message-ID is ambiguous during "
                        + identity_context("source", rows[0]["folder"])
                        + "; narrow the query to one source folder",
                    )
        if is_move and any(row["folder"] not in move_folders for row in rows):
            raise _MoveSourceError("Move sources must be allowed real folders")
        if not rows:
            return rows
        memberships = {_handle(row): [] for row in rows}
        bound_memberships_complete = True
        incomplete_ids: set[str] = set()
        sources_by_id: dict[str, list[dict]] = {}
        for row in rows:
            sources_by_id.setdefault(row["message_id"], []).append(row)
        approved = {_handle(row): row for row in bound_rows or []}
        physical_candidates = {}
        if (
            single_folder
            and bound_rows is None
            and action in {"archive", "move_to_trash", "move_to_folder"}
        ):
            # A physical copy outside the move allowlist can still make a
            # label's ownership ambiguous. Unknown folders count too.
            physical_folders = [
                folder
                for folder in folders
                if not folder.startswith("Labels/")
                and folder not in {"All Mail", "Starred", "Scheduled"}
            ]
            preview_state = getattr(connection, "_rules_preview_state", None)
            checks = None
            if preview_state is not None:
                # The opaque, bounded rules cursor already binds the rules and
                # options. Keep only this exact batch's completed folder checks.
                scope = (_fingerprint(rows), tuple(sorted(physical_folders)))
                saved = preview_state.get("physical_copies", {})
                if saved.get("scope") != scope:
                    saved = preview_state["physical_copies"] = {"scope": scope, "folders": {}}
                checks = saved["folders"]
            fingerprints = {message_fingerprint(row) for row in rows}
            for folder in physical_folders:
                deadline.stage = "physical copy check in " + _safe_name(folder)
                cached = checks.get(folder) if checks is not None else None
                if cached is not None:
                    self.client._select(connection, folder)
                    validity = _uidvalidity(connection)
                    snapshot = (
                        _inventory_state(connection, validity, "physical copy check", folder),
                        validity,
                    )
                    if snapshot != cached["snapshot"]:
                        checks.pop(folder)
                        cached = None
                    else:
                        # Membership must detect changes after this selection,
                        # and still needs fresh candidate headers and flags.
                        connection._cleanup_inventory.setdefault(
                            (folder, "selection_state"), snapshot
                        )
                        key = (folder, validity, ("ids", *sorted(set(identifiers))))
                        if key not in connection._cleanup_inventory:
                            candidates = fetch_rows(
                                self.client,
                                connection,
                                deadline,
                                folder,
                                cached["uids"],
                                validity,
                                stage="physical copy check",
                                raw_fields=True,
                            )
                            _verify_inventory(
                                self.client,
                                connection,
                                snapshot[0],
                                validity,
                                "physical copy check",
                                folder,
                            )
                            connection._cleanup_inventory[key] = candidates
                if cached is None:
                    counts = {}
                    candidates = self._folder_rows(
                        connection,
                        deadline,
                        folder,
                        identifiers,
                        stage="physical copy check",
                        exclusions=exclusions,
                    )
                    for candidate in candidates:
                        fingerprint = message_fingerprint(candidate)
                        if fingerprint in fingerprints:
                            counts[fingerprint] = counts.get(fingerprint, 0) + 1
                    if checks is not None:
                        snapshot = connection._cleanup_inventory[(folder, "selection_state")]
                        if snapshot[0]["uidnext"] is not None:
                            # Unknown allocation state cannot certify reuse. Save
                            # nothing from an interrupted folder, only full checks.
                            checks[folder] = {
                                "snapshot": snapshot,
                                "counts": counts,
                                "uids": [row["uid"] for row in candidates],
                            }
                            preview_state["progress"] = preview_state.get("progress", 0) + 1
                else:
                    counts = cached["counts"]
                for fingerprint, count in counts.items():
                    physical_candidates[fingerprint] = (
                        physical_candidates.get(fingerprint, 0) + count
                    )
        for folder in folders:
            deadline.stage = "membership check in " + _safe_name(folder)
            found = self._folder_rows(
                connection, deadline, folder, identifiers, stage="membership", exclusions=exclusions
            )
            if getattr(connection, "_cleanup_raw_identity", False) and any(
                row.get("identity_invalid") for row in found
            ):
                for source in rows:
                    source["identity_invalid"] = True
                return rows
            seen = set()
            for row in found:
                identifier = row["message_id"]
                if identifier in seen and not single_folder:
                    refuse(
                        identifier,
                        "ambiguous_membership",
                        "Message-ID is ambiguous during " + identity_context("membership", folder),
                    )
                seen.add(identifier)
                sources = sources_by_id[identifier]
                if (
                    not explicit_source
                    and folder in move_folders
                    and any(source["folder"] != folder for source in sources)
                ):
                    refuse(
                        identifier,
                        "ambiguous_membership",
                        "Message-ID is ambiguous during "
                        + identity_context("membership", folder)
                        + "; narrow the query to one source folder",
                    )
                protection = [
                    reason + " during " + identity_context("membership", folder)
                    for reason in self.policy.check(row)
                ]
                if row.get("identity_invalid"):
                    if exclusions is not None:
                        exclusions.add(identifier, "invalid_identity")
                    if exclusions is None or protection:
                        protection.append(
                            "Missing or ambiguous Message-ID header during "
                            + identity_context("membership", folder)
                        )
                for source in sources:
                    if row.get("identity_invalid") or row.get("sender_invalid"):
                        source["membership_unresolved"] = True
                    if protection:
                        source.setdefault("membership_protection", []).extend(protection)
                    if row["folder"] == source["folder"] and _handle(row) != _handle(source):
                        bound_memberships_complete = False
                        incomplete_ids.add(identifier)
                        continue
                    sender_keys = ("from", "sender", "reply_to", "sender_invalid")
                    if (
                        not protection
                        and not self.policy.check(source)
                        and any(row.get(key) != source.get(key) for key in sender_keys)
                    ):
                        refuse(
                            identifier,
                            "inconsistent_sender_headers",
                            "Message-ID copies have inconsistent sender headers during "
                            + identity_context("membership", folder),
                        )
                    if message_fingerprint(row) != message_fingerprint(source):
                        bound_memberships_complete = False
                        incomplete_ids.add(identifier)
                        if single_folder:
                            continue
                    if (
                        folder.startswith("Labels/")
                        and physical_candidates.get(message_fingerprint(source), 0) > 1
                    ):
                        refuse(
                            identifier,
                            "ambiguous_label_membership",
                            "Identical duplicate messages have ambiguous label memberships during "
                            + identity_context("membership", folder),
                        )
                    membership = {
                        key: row[key] for key in ("message_id", "folder", "uidvalidity", "uid")
                    }
                    if (
                        bound_rows is not None
                        and (
                            folder in {"All Mail", "Starred", "Scheduled"}
                            or (single_folder and folder in move_folders)
                        )
                        and membership not in approved[_handle(source)].get("memberships", [])
                    ):
                        continue
                    memberships[_handle(source)].append(membership)
        if row_refusals is not None:
            # Even a protected row goes: kept for the blocked list, it would stay among
            # the reviewed rows, and apply's strict review would refuse the whole token.
            rows = [row for row in rows if row["message_id"] not in row_refusals.reasons]
        for row in rows:
            records = memberships[_handle(row)]
            identity = {key: row[key] for key in ("message_id", "folder", "uidvalidity", "uid")}
            if identity not in records:
                refuse(
                    row["message_id"],
                    "source_membership_changed",
                    "Message set changed during " + identity_context("membership", row["folder"]),
                )
            row["memberships"] = sorted(records, key=_handle)
            row["folders"] = sorted(record["folder"] for record in records)
            if action == "add_label" and "Labels/" + params["label"] in row["folders"]:
                refuse(
                    row["message_id"],
                    "label_already_present",
                    "Message already has the requested label",
                )
            if action == "remove_label" and "All Mail" not in row["folders"]:
                refuse(
                    row["message_id"],
                    "missing_all_mail_copy",
                    "Label removal requires an existing All Mail copy",
                )
            if implicit_fallback and bound_memberships_complete:
                # Full discovery above has freshly checked identity, protection
                # and memberships. Preserve only the immutable optimization
                # metadata for the final approval comparison; dropping it would
                # falsely block unchanged rows after an unrelated arrival. Do
                # not renew its UIDNEXT: later checks must still take discovery.
                previous = approved[_handle(row)]
                certificate_keys = {"label_states", "implicit_source_states"}
                evidence = {
                    key: value for key, value in previous.items() if key not in certificate_keys
                }
                if row == evidence and not self._block_reasons(row):
                    for key in certificate_keys:
                        row[key] = copy.deepcopy(previous[key])
        if exclusions is not None:
            # Policy refusals still block the entire preview, even when that
            # same identity also failed an item-level consistency check.
            rows = [
                row
                for row in rows
                if row["message_id"] not in exclusions.reasons
                or self.policy.check(row)
                or row.get("membership_protection")
            ]
            if selector.get("limit") is not None:
                rows = sorted(
                    rows,
                    key=lambda row: (row["folder"], int(row["uidvalidity"]), int(row["uid"])),
                )[: selector["limit"]]
            bound_memberships_complete = not any(
                row["message_id"] in incomplete_ids for row in rows
            )
        # A same-ID copy excluded from memberships still influenced protection
        # discovery. Retain that discovery unless every such copy is bound.
        if (
            action in {"move_to_folder", "move_to_trash", "archive"}
            and bound_rows is None
            and bound_memberships_complete
            and not getattr(connection, "_cleanup_raw_identity", False)
            # Some servers remove labels on trash. Those own mutations would
            # invalidate a global label-state certificate for the next chunk;
            # retain complete discovery for that approval instead.
            and (
                action != "move_to_trash"
                or not any(
                    member["folder"].startswith("Labels/")
                    for row in rows
                    for member in row["memberships"]
                )
            )
        ):
            label_states = []
            source_states = []
            for folder in folders:
                is_label = folder.startswith("Labels/")
                if is_label or (not explicit_source and folder in move_folders):
                    state, validity = connection._cleanup_inventory[(folder, "selection_state")]
                    if state["uidnext"] is None:
                        break  # Without this certificate retain complete discovery.
                    if is_label:
                        label_states.append({**state, "uidvalidity": validity})
                    else:
                        # Source removals are checked by exact UID below. Any
                        # append could introduce another physical identity copy.
                        source_states.append(
                            {"folder": folder, "uidvalidity": validity, "uidnext": state["uidnext"]}
                        )
            else:
                for row in rows:
                    row["label_states"] = copy.deepcopy(label_states)
                    if not explicit_source:
                        row["implicit_source_states"] = copy.deepcopy(source_states)
        return rows

    def _preview_has_headroom(
        self,
        started: float,
        action: str,
        count: int = 1,
        *,
        extra: float = 0.0,
        params: dict[str, Any] | None = None,
    ) -> bool:
        """Estimate confirmation from this resolution; retain room for a safe chunk.

        Preview and confirmation have separate deadlines. Confirmation verifies
        bounded subsets, so require room for one row rather than the whole set.
        Keep the preview's own 80% gate and the journal/response allowances.
        A batched action's one-row chunk carries its per-folder command allowance;
        extra reserves an approved folder creation.
        """
        elapsed = mail.time.monotonic() - started
        preview_budget = self.settings.preview_timeout or self.settings.call_timeout
        execute_budget = min(self.settings.call_timeout, self.settings.execute_timeout)
        minimum = (
            EXECUTE_LOG_RESERVE_SECONDS
            + extra
            + (
                _batch_command_seconds(action, params or {})
                if action in BATCHED_ACTIONS
                else EXECUTE_MUTATION_SECONDS_PER_MESSAGE
            )
        )
        if action == "remove_label":
            minimum += REMOVE_LABEL_PREFLIGHT_SECONDS_PER_MESSAGE
        verification = elapsed / max(1, count)
        return (
            elapsed <= preview_budget * 0.8
            and verification <= execute_budget * 0.8
            and execute_budget - min(5.0, execute_budget * 0.1) - verification >= minimum
        )

    def _estimated_calls(
        self,
        started: float,
        action: str,
        count: int,
        *,
        certified: bool = False,
        rows: list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
    ) -> int:
        configured = min(self.settings.call_timeout, self.settings.execute_timeout)
        budget = min(configured, EXECUTE_RESPONSE_BUDGET_S)
        available = budget - min(5.0, budget * 0.1) - EXECUTE_LOG_RESERVE_SECONDS
        elapsed = 0.0 if certified else mail.time.monotonic() - started
        # Uncertified confirmation retains full discovery, including recovery
        # preparation. Certified moves use bound handles and a destination append
        # boundary instead. Plan only one full chunk per call in either case.
        if action in BATCHED_ACTIONS:
            # Chunks of up to 200 UIDs: each command group (per source folder and
            # per identical copy) includes its first UID; further UIDs add 0.05 s.
            group = _batch_command_seconds(action, params or {})
            verification = elapsed / max(1, count)
            room = available - elapsed

            def chunk_seconds(size, needed):
                extra = (size - needed) * EXECUTE_BATCH_SECONDS_PER_MESSAGE
                return needed * group + extra + size * verification

            if rows is None:
                per_row = EXECUTE_BATCH_SECONDS_PER_MESSAGE + verification
                fitted = 1 + math.floor((room - chunk_seconds(1, 1)) / per_row)
                return max(1, math.ceil(count / max(1, min(EXECUTE_BATCH_SIZE, fitted))))
            calls, offset = 0, 0
            while offset < len(rows):
                groups = command_group_counts(action, rows[offset : offset + EXECUTE_BATCH_SIZE])
                fitted = sum(
                    1
                    for size, needed in enumerate(groups, start=1)
                    if chunk_seconds(size, needed) <= room
                )
                offset += max(1, fitted)
                calls += 1
            return max(1, calls)
        cost = EXECUTE_MUTATION_SECONDS_PER_MESSAGE + (
            REMOVE_LABEL_PREFLIGHT_SECONDS_PER_MESSAGE if action == "remove_label" else 0
        )
        rows_per_call = max(
            1,
            min(
                EXECUTE_CHUNK_SIZE,
                math.floor((available - elapsed) / (cost + elapsed / max(1, count))),
            ),
        )
        return max(1, math.ceil(count / rows_per_call))

    def _cap_execute_deadline(self, deadline, started):
        """Cap the whole call before setup; keep configured admission independent.

        Reapplying this at a group/chunk boundary cannot renew the call budget.
        Apply the existing 10% (at most 5 s) response reserve to that budget.
        """
        if not hasattr(deadline, "cleanup_admission_end"):
            deadline.cleanup_admission_end = deadline.end
            deadline.cleanup_admission_reserve = deadline.response_reserve
        deadline.end = min(deadline.end, started + EXECUTE_RESPONSE_BUDGET_S)
        deadline.response_reserve = min(deadline.response_reserve, EXECUTE_RESPONSE_BUDGET_S * 0.1)
        return deadline

    def preview_action(
        self,
        action: str,
        query: dict[str, Any] | None = None,
        ids: list[str] | None = None,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params = _params(action, params)
        selector = _selector(query, ids)
        bound = self._approval_bound()
        if _preview_limit(limit, bound) is not None:
            selector["limit"] = limit
            if limit > MAX_BATCH:
                # One bounded approval: the token binds this hard bound too, and
                # server-side continuations cover it in chunks of at most 200.
                selector["max_messages"] = bound
        exclusions = PreviewExclusions()
        result: dict[str, Any] = {
            **exclusions.response(),
            "count": 0,
            "samples": [],
            "blocked": [],
            "token": None,
            "expires_at": None,
            "action_hash": None,
            "action": action,
            "target": None,
            "partial": False,
            "total_is_exact": True,
            "personal_provider_bypass_count": 0,
            "mark_read": params.get("mark_read", action == "mark_read"),
            "estimated_calls": 0,
            "approval_max_messages": bound,
            "folder_creation": None,
        }
        preview_started = mail.time.monotonic()
        deadline = (
            _Deadline(self.settings.preview_timeout, self.settings.timeout)
            if self.settings.preview_timeout is not None
            else self.client._deadline()
        )
        deadline.stage = "connection setup"
        try:
            with self.client._session(deadline=deadline) as (connection, _):
                deadline.stage = "target validation and folder listing"
                if params.get("create_folder_if_missing"):
                    result["target"] = self.backend.validate_target(
                        action, params, connection, missing_ok=True
                    )
                    params, result["folder_creation"] = self._folder_creation(
                        action, params, connection
                    )
                else:
                    result["target"] = self.backend.validate_target(action, params, connection)
                creates = bool(result["folder_creation"] and result["folder_creation"]["create"])
                folders = self.client._folders(connection)
                # Samples take real folders and All Mail from LIST, as search does.
                _, real, all_mail = mailbox_roles(connection)
                rows = self._resolve(
                    connection, deadline, selector, action, params, folders, exclusions=exclusions
                )
                if creates and "folder" not in selector.get("query", {}):
                    # Creating the destination adds a real folder, which an implicit
                    # source certificate cannot account for: confirmation must keep
                    # full identity discovery for every chunk instead.
                    for row in rows:
                        row.pop("label_states", None)
                        row.pop("implicit_source_states", None)
                result.update(
                    **exclusions.response(),
                    count=len(rows),
                    samples=[self._sample(row, real) for row in rows[:10]],
                    blocked=self._blocked(rows, real),
                    personal_provider_bypass_count=sum(
                        bool(row["personal_provider_bypasses"]) for row in rows
                    ),
                )
                if not rows and exclusions.reasons:
                    result["reason"] = "All candidates were excluded; no cleanup token issued"
                elif len(rows) > _approval_cap(selector):
                    result["reason"] = "Batch exceeds the hard cap of 200 messages" + (
                        f"; set limit (at most {bound}) for one bounded approval"
                        if bound > MAX_BATCH
                        else ""
                    )
                elif rows and not result["blocked"]:
                    deadline.stage = "final approval check"
                    deadline.check(connection)
                    target_state = getattr(connection, "_cleanup_inventory", {}).get(
                        (result["target"], "selection_state")
                    )
                    bounded_recovery = bool(
                        action in {"move_to_folder", "move_to_trash", "archive"}
                        and all("label_states" in row for row in rows)
                        and target_state is not None
                        and target_state[0]["uidnext"] is not None
                    )
                    result["estimated_calls"] = self._estimated_calls(
                        preview_started,
                        action,
                        len(rows),
                        certified=bounded_recovery,
                        rows=rows,
                        params=params,
                    )
                    if not self._preview_has_headroom(
                        preview_started,
                        action,
                        len(rows),
                        extra=EXECUTE_BATCH_SECONDS if creates else 0.0,
                        params=params,
                    ):
                        result["reason"] = (
                            "Preview exceeded 80% of its preview or execute budget, or has "
                            "insufficient execute headroom; retry when Bridge is idle or "
                            "raise PROTON_MCP_CALL_TIMEOUT, PROTON_MCP_PREVIEW_TIMEOUT and "
                            "PROTON_MCP_EXECUTE_TIMEOUT together with the client timeout"
                        )
                    else:
                        result["token"], result["expires_at"] = self.tokens.issue(
                            action, params, selector, rows
                        )
                        result["action_hash"] = approval_hash(action, params, selector, rows)
                        self.tokens.record_verification_cost(
                            result["token"],
                            mail.time.monotonic() - preview_started,
                            bounded_recovery=bounded_recovery,
                        )
                # Display only, so after approval and its cost: name label and All
                # Mail sources by the real folder that search reports for them.
                if resolve_samples(
                    self.client,
                    connection,
                    deadline,
                    result["samples"] + result["blocked"],
                    real=real,
                    all_mail=all_mail,
                    started=preview_started,
                    budget=self.settings.preview_timeout or self.settings.call_timeout,
                ):
                    result["partial"] = True
        except ReadStateSourceError as error:
            result.update(reason=str(error), blocked=[{"reason": str(error)}])
        except _MoveSourceError as error:
            result["reason"] = str(error)
        except MailError as error:
            if not isinstance(error, CallDeadlineExceeded) and not deadline.expired:
                raise
            result.update(
                **exclusions.response(),
                partial=True,
                total_is_exact=False,
                token=None,
                expires_at=None,
                action_hash=None,
                reason="Preview deadline reached during " + deadline.stage + "; preview again",
            )
        if action in {"move_to_trash", "archive", "move_to_folder"}:
            return self._log_preview(action, result)
        return result

    def _log_preview(self, action: str, result: dict[str, Any]) -> dict[str, Any]:
        record = new_record("preview_action", {"action": action})
        record.update(
            count=result["count"],
            blocked_count=len(result["blocked"]),
            excluded_count=result["excluded_count"],
            partial=result["partial"],
            total_is_exact=result["total_is_exact"],
            action_hash=result["action_hash"],
            status=(
                "previewed"
                if result["token"]
                else "partial"
                if result["partial"]
                else "blocked"
                if result["blocked"] or result.get("reason")
                else "empty"
            ),
        )
        if result["token"]:
            record["token_digest"] = token_digest(result["token"])
        try:
            with ActionLog(self.settings.action_log_path).transaction() as transaction:
                transaction.append(record)
        except BaseException:
            # An unlogged approval must never survive a failed preview response.
            if result["token"]:
                with suppress(MailValidationError):
                    self.tokens.revoke(result["token"], action)
            raise
        result["log_id"] = record["log_id"]
        return result

    @contextmanager
    def _exclusive(self):
        if not self._mutation_lock.acquire(blocking=False):
            raise MailValidationError(
                "Another cleanup operation is running; retry after it finishes"
            )
        try:
            yield
        finally:
            try:
                self.tokens.prune_expired()
            finally:
                self._mutation_lock.release()

    def execute(
        self,
        action: str,
        token: str,
        *,
        params: dict[str, str] | None = None,
        ids: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        _batch_limit(limit)
        with self._exclusive():
            grant = self.tokens.inspect(token, action, params=params, ids=ids)
            cap = self._grant_cap(grant)
            if not grant.messages or len(grant.messages) > cap:
                raise MailValidationError(
                    f"Cleanup batches must contain between 1 and {cap} messages"
                )
            if limit is not None and limit > (grant.approved_total or len(grant.messages)):
                raise MailValidationError("Execute limit cannot widen the approved batch")
            execute_started = mail.time.monotonic()
            deadline = self._cap_execute_deadline(
                self.client._deadline(execute=True), execute_started
            )
            if grant.run_id is not None:
                run = self.backend.log.get_run(grant.run_id)
                if (
                    run is None
                    or run["done"] != grant.run_done
                    or run["uncertain"] != grant.run_uncertain
                    or run["approved_total"] != grant.approved_total
                    or run["status"] == "completed"
                ):
                    raise MailValidationError("Continuation does not match durable run progress")
                # A refused pre-claim attempt can still have a journaled empty
                # chunk. Allocate its successor from durable evidence on retry.
                grant = replace(grant, chunk_index=run["next_chunk_index"])
            with self.client._session(deadline=deadline) as (connection, _):
                create_folder = self._destination_to_create(grant, connection)
                if create_folder is None:
                    self.backend.validate_target(action, grant.params, connection)
                connection._cleanup_bounded_recovery = grant.bounded_recovery
                connection._cleanup_implicit_source_progress = dict(grant.implicit_source_progress)
                result = self._execute_chunks(
                    connection,
                    deadline,
                    execute_started,
                    grant,
                    None,
                    token,
                    params,
                    ids,
                    limit=limit,
                    create_folder=create_folder,
                )
                if grant.params.get("create_folder_if_missing") is True:
                    # Nothing was created: the destination already existed, or the
                    # call stopped before its creation.
                    result.setdefault(
                        "folder_creation",
                        {
                            "name": grant.params["folder"],
                            "created": False,
                            "exists": create_folder is None,
                            "ready": create_folder is None,
                            "uncertain": False,
                        },
                    )
                return result

    def _check_execution_rows(self, rows, expected, fingerprint, *, cap=MAX_BATCH):
        if len(rows) > cap:
            raise MailValidationError(f"Cleanup batch exceeds the hard cap of {cap} messages")
        # Name the bound source being executed. A sample's canonical folder can
        # differ from it, or be null, for a label or All Mail source.
        blocked = next((row for row in rows if self._block_reasons(row)), None)
        if blocked is not None:
            raise MailValidationError(
                "Batch contains protected senders or ambiguous identity during "
                + identity_context("execute", blocked["folder"])
            )
        if not hmac.compare_digest(_fingerprint(rows), fingerprint):
            previous = {_handle(row): row for row in expected}
            if any(
                "seen" in previous.get(_handle(row), {})
                and row.get("seen") != previous[_handle(row)]["seen"]
                for row in rows
            ):
                raise MailValidationError("Message read state changed after preview; preview again")
            changed = next((row for row in expected if row not in rows), expected[0])
            raise MailValidationError(
                "Message set changed after preview during "
                + identity_context("execute", changed["folder"])
                + "; request a new preview"
            )

    def _verified_chunk(self, connection, deadline, grant, expected, *, continuing=False):
        """Refresh approved handles only; isolate changed rows on a continuation."""

        def resolve(bound):
            return self._resolve(
                connection,
                deadline,
                grant.selector,
                grant.action,
                grant.params,
                self.client._folders(connection),
                bound_rows=bound,
            )

        def blocked_row(approved, reason=None):
            return {
                **{key: approved[key] for key in ("message_id", "folder", "uid", "uidvalidity")},
                "reason": reason or "Approved row changed or is protected; request a new preview",
                "status": "blocked",
            }

        try:
            resolved = resolve(expected)
        except _ContinuationSourceChanged as error:
            return [], [blocked_row(row, str(error)) for row in expected]
        except _BoundVerificationRefused:
            if not continuing:
                raise
            resolved = None
        if not continuing:
            self._check_execution_rows(
                resolved, expected, _fingerprint(expected), cap=_approval_cap(grant.selector)
            )
            return resolved, []
        if resolved is None:
            # A missing bound source UID must not trigger one whole membership
            # inventory per remaining row. This source-only probe can exclude
            # vanished handles; it never authorizes a mutation by itself.
            found = set()
            for folder in dict.fromkeys(row["folder"] for row in expected):
                requested = [row for row in expected if row["folder"] == folder]
                self.client._select(connection, folder)
                validity = _uidvalidity(connection)
                if {row["uidvalidity"] for row in requested} != {validity}:
                    continue
                fetched = fetch_rows(
                    self.client,
                    connection,
                    deadline,
                    folder,
                    [row["uid"] for row in requested],
                    validity,
                    complete=False,
                    stage="execute",
                )
                found.update(_handle(row) for row in fetched)
            missing = [row for row in expected if _handle(row) not in found]
            if missing:
                present = [row for row in expected if _handle(row) in found]
                rows, blocked = (
                    self._verified_chunk(connection, deadline, grant, present, continuing=True)
                    if present
                    else ([], [])
                )
                return rows, [blocked_row(row) for row in missing] + blocked

        # A successful batch resolution already includes complete membership
        # evidence. Compare each row against its approval without repeating that
        # expensive inventory for every changed fingerprint or protection bit.
        by_handle = {_handle(row): row for row in resolved or []}
        rows, blocked = [], []
        for approved in expected:
            deadline.check(connection)
            try:
                if resolved is None:
                    current = resolve([approved])
                else:
                    current = (
                        [by_handle[_handle(approved)]] if _handle(approved) in by_handle else []
                    )
                self._check_execution_rows(current, [approved], _fingerprint([approved]))
                rows.extend(current)
            except MailValidationError:
                blocked.append(blocked_row(approved))
        return rows, blocked

    def _execute_chunks(
        self,
        connection,
        deadline,
        started,
        grant,
        rows,
        token,
        params,
        ids,
        *,
        claim_token=None,
        before_refresh=None,
        run_context=None,
        limit=None,
        create_folder=None,
    ) -> dict[str, Any]:
        self._cap_execute_deadline(deadline, started)
        # Rules supply their own claim callback and retain their original
        # chunk sizing, recovery, and error presentation.
        moves_execute = claim_token is None
        results = []
        completed_handles: set[tuple[str, str, str]] = set()
        blocked = list(grant.blocked)
        claimed = False
        stop_reason = None
        unlogged = []
        continuing = grant.run_id is not None
        connection._cleanup_resume_messages = None
        approved_total = grant.approved_total or len(grant.messages)
        context = (
            run_context
            if run_context is not None
            else {
                "run_id": grant.run_id or secrets.token_hex(16),
                "action": grant.action,
                "token_digest": grant.run_token_digest or token_digest(token),
                "approved_total": approved_total,
                "chunk_index": grant.chunk_index,
            }
        )
        # Moves and read-state changes cost one command group per source folder
        # plus a small per-UID allowance; label actions keep per-message costs.
        batched = grant.action in BATCHED_ACTIONS
        max_chunk = EXECUTE_BATCH_SIZE if batched else EXECUTE_CHUNK_SIZE
        if batched:
            per_message = max(EXECUTE_BATCH_SECONDS_PER_MESSAGE, grant.batch_row_seconds)
            per_group = max(
                _batch_command_seconds(grant.action, grant.params), grant.batch_chunk_seconds
            )
        else:
            per_message = max(EXECUTE_MUTATION_SECONDS_PER_MESSAGE, grant.mutation_seconds) + (
                REMOVE_LABEL_PREFLIGHT_SECONDS_PER_MESSAGE if grant.action == "remove_label" else 0
            )
            per_group = 0.0
        verification_cost = grant.verification_seconds
        # Certified reads scale with the chunk. The last whole chunk's observed
        # cost must not become a fixed inventory charge on the next call.
        certified = grant.bounded_recovery and all("label_states" in row for row in grant.messages)
        verification_floor = 0.0 if certified else grant.inventory_seconds
        attempted = 0
        folder_created = None

        def claim_approval():
            nonlocal claimed
            current = (
                self.tokens.claim(token, grant.action, params=params, ids=ids)
                if claim_token is None
                else claim_token()
            )
            # Execution inspected a snapshot before preflight. Carry the first
            # claim's fixed resume deadline into its remainder and response.
            grant.expires = current.expires
            grant.expires_at = current.expires_at
            grant.resume_started = current.resume_started
            claimed = True

        def groups(prefix):
            """Command groups (per source folder, per identical copy) the backend sends."""
            return (command_group_counts(grant.action, prefix) or [0])[-1] if batched else 0

        def cost(prefix, extra=0.0):
            if not batched:
                return len(prefix) * per_message + extra
            # Each command group's allowance includes its first UID.
            needed = groups(prefix)
            return needed * per_group + (len(prefix) - needed) * per_message + extra

        def capacity(prefix, *, verification=False, reserve=0.0):
            """Rows of this prefix whose mutation (and next verification) still fit."""
            available = (
                deadline.io_end - mail.time.monotonic() - EXECUTE_LOG_RESERVE_SECONDS - reserve
            )
            if not batched:
                estimate = available / (per_message + (verification_cost if verification else 0))
                if verification:
                    # Membership discovery has a mailbox-wide component: shrinking
                    # the candidate set does not make that work proportional. Never
                    # launch another inventory on only a per-row estimate.
                    estimate = min(estimate, (available - verification_floor) / per_message)
                return max(0, math.floor(estimate))
            fitted = 0
            for count in command_group_counts(grant.action, prefix[:max_chunk]):
                fixed = count * per_group
                rows_cost = (fitted + 1 - count) * per_message
                if fixed + rows_cost + (fitted + 1) * (
                    verification_cost if verification else 0
                ) > available or (
                    verification and fixed + rows_cost + verification_floor > available
                ):
                    break
                fitted += 1
            return fitted

        def check_admission(seconds):
            nonlocal stop_reason
            configured_available = (
                deadline.cleanup_admission_end
                - deadline.cleanup_admission_reserve
                - mail.time.monotonic()
                - EXECUTE_LOG_RESERVE_SECONDS
            )
            # The remaining mutation, journal and response allowances determine
            # admission. An elapsed percentage must not discard a safe prefix.
            if configured_available < seconds:
                stop_reason = "Insufficient execute headroom"
                raise MailValidationError(
                    stop_reason
                    + (
                        "; no messages were changed; preview token remains valid until expiry; "
                        "retry when Bridge is idle or raise PROTON_MCP_CALL_TIMEOUT and "
                        "PROTON_MCP_EXECUTE_TIMEOUT together with the client timeout"
                        if not claimed
                        else ""
                    )
                )

        def check_headroom(prefix, extra=0.0):
            nonlocal stop_reason
            check_admission(cost(prefix, extra))
            if capacity(prefix, reserve=extra) < len(prefix):
                stop_reason = "Execute response budget reached"
                raise _ResponseBudgetReached(stop_reason)

        def overflow_seconds(prefix):
            """Mutation seconds for one row more than now fits in the response budget."""
            fitted = capacity(prefix)
            if not batched:
                return (fitted + 1) * per_message
            return cost(prefix[:fitted]) + (per_message if fitted else per_group)

        while len(completed_handles) < len(grant.messages):
            if limit is not None and attempted >= limit:
                stop_reason = "Execute limit reached"
                break
            chunk = []
            chunk_started = False
            pending = [row for row in grant.messages if _handle(row) not in completed_handles]
            try:
                check_headroom(pending[:1])
                expected = pending
                # Rows the caller just verified need no further verification this round.
                fit = capacity(expected, verification=rows is None or bool(results))
                size = min(max_chunk, fit, len(expected))
                if rows is None and not results and moves_execute:
                    # Preview discovery is not a measurement of this call's
                    # verification. Always attempt the first read-only check
                    # within the real deadline, even if that stale estimate
                    # leaves no predicted capacity. Initial confirmation checks
                    # the whole approval before any chunk can claim or mutate.
                    size = len(expected) if not continuing else max(1, size)
                if limit is not None and (continuing or results or not moves_execute):
                    size = min(size, limit - attempted)
                if size == 0:
                    stop_reason = "Insufficient execute headroom for verification"
                    if not results:
                        # Configured exhaustion refuses before claiming; response
                        # exhaustion yields below, as for one row beyond capacity.
                        check_admission(overflow_seconds(expected))
                        stop_reason = "Execute response budget reached"
                        raise _ResponseBudgetReached(stop_reason)
                    break
                expected = expected[:size]
                if rows is None or results:
                    if continuing and certified:
                        connection._cleanup_resume_messages = [
                            row for row in grant.messages if _handle(row) not in completed_handles
                        ]
                    if results and before_refresh is not None:
                        before_refresh()
                    verification_started = mail.time.monotonic()
                    rows, newly_blocked = self._verified_chunk(
                        connection,
                        deadline,
                        grant,
                        expected,
                        continuing=continuing,
                    )
                    if moves_execute:
                        verification_elapsed = mail.time.monotonic() - verification_started
                        # Even bound reads can encounter a fixed server delay;
                        # reserve the observed work before another verification.
                        if not results:
                            # Replace preview/prior-call estimates with observed
                            # work in this call; they must not impose a permanent
                            # inventory floor on an otherwise idle confirmation.
                            verification_cost = verification_elapsed / len(expected)
                            # Certified bound reads scale with rows: a bounded
                            # approval's whole first check is not the fixed cost
                            # of verifying one later chunk.
                            verification_floor = verification_elapsed * (
                                min(1.0, max_chunk / len(expected))
                                if batched and certified
                                else 1.0
                            )
                        else:
                            verification_cost = max(
                                verification_cost, verification_elapsed / len(expected)
                            )
                            verification_floor = max(verification_floor, verification_elapsed)
                    else:
                        verification_cost = max(
                            verification_cost,
                            (mail.time.monotonic() - verification_started) / len(expected),
                        )
                        verification_floor = max(
                            verification_floor, mail.time.monotonic() - verification_started
                        )
                    blocked.extend(newly_blocked)
                    completed_handles.update(_handle(row) for row in newly_blocked)
                else:
                    rows = rows[:size]
                if not rows:
                    rows = None
                    continue
                if not claimed and self.tokens.clock() >= grant.expires:
                    if claim_token is None:
                        self.tokens.inspect(token, grant.action, params=params, ids=ids)
                    else:
                        claim_approval()
                # An approved folder creation precedes the first chunk; keep its room.
                creation = EXECUTE_BATCH_SECONDS if create_folder is not None else 0.0
                if creation and capacity(rows[:1], reserve=creation) < 1:
                    # Not even one move fits beside the creation in this call.
                    chunk = []
                else:
                    check_headroom(rows[:1], creation)
                    chunk = rows[: min(max_chunk, capacity(rows, reserve=creation))]
                    # Initial moves verification covers the whole approval, so its
                    # mutation prefix still needs the caller's smaller call limit.
                    if moves_execute and limit is not None:
                        chunk = chunk[: limit - attempted]
                if create_folder is not None:
                    # Then the creation alone is this call's progress, and the
                    # continuation moves the approved rows.
                    creation_only = not chunk

                    def claim_for_folder():
                        nonlocal claimed, stop_reason
                        deadline.check(connection)
                        if creation_only:
                            # Room for the creation and the journal alone.
                            check_admission(creation)
                            if (
                                deadline.io_end - mail.time.monotonic()
                                < creation + EXECUTE_LOG_RESERVE_SECONDS
                            ):
                                stop_reason = "Execute response budget reached"
                                raise _ResponseBudgetReached(stop_reason)
                        else:
                            # Room for the creation and then this verified chunk; the
                            # journal intent took time, so narrow rather than refuse.
                            check_headroom(chunk[:1], creation)
                            chunk[:] = chunk[: max(1, capacity(chunk, reserve=creation))]
                            check_headroom(chunk, creation)
                        if not claimed:
                            claim_approval()

                    # The approval also creates its destination: once its moves
                    # verified, claim the token, create the folder (journaled),
                    # then move. Nothing is created before this point.
                    from .create_folder import create_approved_folder

                    folder_created = create_approved_folder(
                        self,
                        connection,
                        deadline,
                        create_folder,
                        context,
                        authorize=claim_for_folder,
                    )
                    create_folder = None
                    if not folder_created["ready"]:
                        stop_reason = (
                            "Approved destination folder was created but is not ready for "
                            "moves; inspect the folder list"
                            if folder_created["created"]
                            else "Approved destination folder could not be confirmed created; "
                            "inspect the action log and folder list"
                            if folder_created["uncertain"]
                            else "Approved destination folder could not be created; "
                            "inspect the action log"
                        )
                        break
                    if creation_only:
                        stop_reason = "Execute response budget reached"
                        break

                def before_chunk():
                    nonlocal stop_reason
                    # Read-only preflight may have reduced capacity. Narrow the
                    # verified prefix before the backend freezes its journal.
                    check_headroom(chunk[:1])
                    # Leave room for the first per-UID check after journal
                    # freeze. Preflight measures the same bounded SELECT/FETCH
                    # work without charging discovery or resolution overhead.
                    claim_reserve = (
                        getattr(deadline, "cleanup_claim_verification_seconds", 0.0)
                        if certified or moves_execute
                        else 0.0
                    )
                    if batched:
                        # A batched final check re-reads every row of the chunk,
                        # after the chunk record is journaled: reserve it twice so
                        # the unresizable claim gate does not refuse a full chunk.
                        claim_reserve *= 2
                    frozen_capacity = capacity(chunk, reserve=claim_reserve)
                    if frozen_capacity < 1:
                        stop_reason = "Execute response budget reached"
                        raise _ResponseBudgetReached(
                            "Insufficient execute headroom for final identity verification"
                        )
                    chunk[:] = chunk[:frozen_capacity]
                    check_headroom(chunk)

                def claim_before_mutation():
                    nonlocal claimed, chunk_started
                    deadline.check(connection)
                    # The journal and operations already describe this exact
                    # chunk. This final gate must never resize it.
                    check_headroom(chunk)
                    if not claimed:
                        claim_approval()
                    chunk_started = True

                self.backend.run_context = context
                mutation_started = mail.time.monotonic()
                try:
                    result = self.backend.apply(
                        connection,
                        deadline,
                        grant.action,
                        grant.params,
                        chunk,
                        before_chunk=before_chunk,
                        before_mutation=claim_before_mutation,
                        token_digest=token_digest(token),
                    )
                finally:
                    self.backend.run_context = None
                attempted += len(chunk)
                if result["count"] and batched:
                    # Attribute observed time beyond the further-UID allowance to
                    # the command groups, retaining both original floors.
                    needed = max(1, groups(chunk))
                    elapsed = mail.time.monotonic() - mutation_started
                    per_group = max(
                        per_group, (elapsed - (len(chunk) - needed) * per_message) / needed
                    )
                elif result["count"]:
                    # Include preparation and final per-UID checks in the
                    # observed per-row allowance, retaining the original floor.
                    per_message = max(
                        per_message,
                        (mail.time.monotonic() - mutation_started) / result["count"],
                    )
                self._advance_implicit_sources(connection, chunk, result)
                results.append(result)
                context["chunk_index"] += 1
                for row, entry in zip(chunk, result["messages"]):
                    if entry["status"] != "not_attempted":
                        completed_handles.add(_handle(row))
                if result["partial"] or result["count"] != len(chunk):
                    stop_reason = stop_reason or result.get("error", "Cleanup interrupted")
                    break
                completed_handles.update(_handle(row) for row in chunk)
                rows = None
            except Exception as error:
                # Before any claim, refusing leaves the approval untouched. After an
                # approved folder creation claimed it, report like any progress.
                if not results and not chunk_started and not claimed:
                    if isinstance(error, CallDeadlineExceeded):
                        # Configured exhaustion still refuses before claiming;
                        # response-only exhaustion follows the caller's recovery
                        # policy below.
                        check_admission(cost(pending[:1]))
                        response_io_end = (
                            min(deadline.cleanup_admission_end, started + EXECUTE_RESPONSE_BUDGET_S)
                            - deadline.response_reserve
                        )
                        if (
                            deadline.cleanup_admission_end <= started + EXECUTE_RESPONSE_BUDGET_S
                            or mail.time.monotonic() < response_io_end
                        ):
                            raise
                    if isinstance(error, (_ResponseBudgetReached, CallDeadlineExceeded)):
                        if moves_execute:
                            # Do not claim or create an empty moves run just to
                            # rotate the same approval forever.
                            stop_reason = (
                                stop_reason or "Execute response budget reached during verification"
                            )
                        else:
                            log = getattr(self.backend, "log", None)
                            if log is None:
                                raise
                            # Rules preserve the unchanged approval durably
                            # before claiming it and returning their partial run.
                            self.tokens.inspect(token, context["action"])
                            with log.transaction() as transaction:
                                log.ensure_run(transaction, context)
                            claim_approval()
                            stop_reason = "Execute response budget reached"
                        break
                    raise
                if chunk_started:
                    unlogged = [
                        {"message_id": row["message_id"], "status": "uncertain"} for row in chunk
                    ]
                    completed_handles.update(_handle(row) for row in chunk)
                    stop_reason = "Cleanup outcome could not be logged; inspect the active chunk"
                else:
                    stop_reason = stop_reason or "Cleanup interrupted during chunk preparation"
                break

        if continuing and not claimed and len(blocked) > len(grant.blocked):
            claim_approval()
        messages = [entry for result in results for entry in result["messages"]] + unlogged
        done = sum(result["count"] for result in results)
        uncertain = sum(entry["status"] not in {"done", "not_attempted"} for entry in messages)
        run_done = grant.run_done + done
        run_uncertain = grant.run_uncertain + uncertain
        remaining = approved_total - run_done - run_uncertain
        log_ids = [result["log_id"] for result in results]
        partial = run_done != approved_total or any(result["partial"] for result in results)
        if blocked:
            stop_reason = stop_reason or "Approved rows changed; blocked rows remain unchanged"
        durable = True
        log = getattr(self.backend, "log", None)
        if log is not None and run_context is None:
            try:
                if context.get("_call_started") or (continuing and (claimed or not moves_execute)):
                    run = log.finish_run(
                        context["run_id"], stop_reason=stop_reason, blocked=len(blocked)
                    )
                    durable = run["done"] == run_done and run["uncertain"] == run_uncertain
                    partial = partial or run["partial"]
                elif claimed:
                    durable = False
            except Exception:
                durable = False
                stop_reason = "Run outcome could not be logged; inspect the journal"
        if not durable:
            partial = True
        continuation = None
        pending = [row for row in grant.messages if _handle(row) not in completed_handles]
        made_progress = (
            done > 0
            or uncertain > 0
            or len(blocked) > len(grant.blocked)
            # A created destination is durable progress of this approval.
            or bool(folder_created and folder_created["created"])
        )
        if (
            claimed
            and partial
            and durable
            and not unlogged
            and claim_token is None
            and made_progress
        ):
            next_grant = replace(
                grant,
                run_id=context["run_id"],
                approved_total=approved_total,
                run_token_digest=context["token_digest"],
                run_done=run_done,
                run_uncertain=run_uncertain,
                blocked=blocked,
                chunk_index=context["chunk_index"],
                verification_seconds=verification_cost,
                inventory_seconds=0.0 if certified else verification_floor,
                mutation_seconds=grant.mutation_seconds
                if batched
                else per_message
                - (
                    REMOVE_LABEL_PREFLIGHT_SECONDS_PER_MESSAGE
                    if grant.action == "remove_label"
                    else 0
                ),
                batch_row_seconds=per_message if batched else grant.batch_row_seconds,
                batch_chunk_seconds=per_group if batched else grant.batch_chunk_seconds,
                implicit_source_progress=dict(
                    getattr(connection, "_cleanup_implicit_source_progress", {})
                ),
            )
            continuation = self.tokens.continue_grant(next_grant, pending)
        unavailable_reason = None
        if remaining and continuation is None:
            if moves_execute:
                unavailable_reason = (
                    "Run outcome could not be durably verified; inspect the journal"
                    if not durable or unlogged
                    else "Preview token expired; request a new preview"
                    if self.tokens.clock() >= grant.expires
                    else (
                        "No progress was recorded; inspect logged outcomes "
                        "and request a new preview"
                    )
                    if not made_progress and claimed
                    else (
                        "No chunk started; preview token remains valid until expiry; "
                        "retry when Bridge is idle"
                    )
                    if not made_progress and not claimed
                    else "Approved rows changed or are protected; request a new preview"
                    if not pending
                    else "Continuation could not be retained; request a new preview"
                )
            else:
                unavailable_reason = (
                    "Run outcome could not be durably verified; inspect the journal"
                    if not durable or unlogged
                    else "Approved rows changed or are protected; request a new preview"
                    if not pending
                    else "Preview token expired; request a new preview"
                    if self.tokens.clock() >= grant.expires
                    else "Continuation could not be retained; request a new preview"
                )
        response = {
            "log_ids": log_ids,
            "run_id": context["run_id"],
            "approved_total": approved_total,
            "run_done": run_done,
            "run_uncertain": run_uncertain,
            "count": done,
            "done": done,
            "remaining": remaining,
            "uncertain": uncertain,
            "partial": partial,
            "stop_reason": stop_reason,
            "token_consumed": claimed,
            "messages": messages,
            "blocked": blocked,
            "resume": continuation is not None,
            "continuation_token": continuation,
            "available": continuation is not None,
            "continuation_reason": unavailable_reason,
            "expires_at": grant.expires_at,
            "missing": [entry for result in results for entry in result.get("missing", [])],
            # Rows a batched chunk left untouched or could not verify, with reasons.
            "mismatched": [entry for result in results for entry in result.get("mismatched", [])],
        }
        if folder_created is not None:
            response["folder_creation"] = folder_created
        if stop_reason:
            response["error"] = (
                stop_reason
                + f"; {done} done, {remaining} remaining unchanged, {uncertain} uncertain; "
                + (
                    "token consumed; "
                    + (
                        "use the continuation token before expiry, or request a new preview "
                        if continuation is not None
                        else "inspect logged outcomes and request a new preview "
                    )
                    + "for any remaining messages"
                    if claimed
                    else (
                        "preview token remains valid until expiry; retry when Bridge is idle"
                        if self.tokens.clock() < grant.expires
                        else "preview token expired; request a new preview"
                    )
                    if moves_execute
                    else "preview token remains valid until expiry"
                )
            )
        if unlogged:
            response["unlogged"] = len(unlogged)
        if (not made_progress if moves_execute else done == 0 and uncertain == 0) and pending:
            response["error_code"] = "first_chunk_unavailable"
            response["error"] = "No approved chunk could complete within this call; " + (
                response.get("error")
                or (unavailable_reason if moves_execute else "retry the continuation before expiry")
            )
        return response

    def undo_action(self, log_id: str) -> dict[str, Any]:
        with self._exclusive():
            return self.backend.undo_action(log_id)
