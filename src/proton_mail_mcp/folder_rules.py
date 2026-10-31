"""Manual, reviewed folder rules using the existing guarded move and undo path."""

from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import suppress
from dataclasses import dataclass, replace
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from . import mail
from .action_log import token_digest
from .cleanup_folders import _move_folders, _real_folder_name
from .cleanup_lookup import (
    PreviewExclusions,
    _IncompleteIdentityInventory,
    _PreviewInventoryGrew,
    display_value,
)
from .config import ConfigError
from .guarded import (
    MAX_BATCH,
    CleanupService,
    _address_criteria,
    _batch_limit,
    _fingerprint,
    _handle,
    approval_hash,
)
from .mail import CallDeadlineExceeded, MailError, MailValidationError, _Deadline, _quote
from .rules_preview import MAX_PREVIEWS, PREVIEW_SECONDS, discover, source_fingerprint
from .rules_source import RulesSourceError, require_rules_source
from .rules_store import _unique_object, load_saved, store_path
from .sieve import Condition, SieveFilter

RULES_PATH = Path(__file__).with_name("folder_rules.json")
MAX_RULES = 20
MAX_STALLED_PREVIEWS = 6
# A new cursor chain's first strict review. Fixed folder checks dominate a few
# rows, so small match sets need no second review.
CALIBRATION_ROWS = 4


class _UnresolvedRows(MailValidationError):
    """A rules review holds a message whose identity or sender cannot be verified."""


class _BatchNarrowed(CallDeadlineExceeded):
    """Not a deadline: ends a call whose batch held a message it cannot verify."""


@dataclass(frozen=True)
class FolderRule:
    field: str
    contains: str
    folder: str
    # Packaged rules never mark read themselves; preview_rules(mark_read=True) can.
    mark_read = False

    def report(self) -> dict[str, str]:
        return {"field": self.field, "contains": self.contains}


def _addresses(value: Any) -> list[str]:
    """The addr-specs of a rendered envelope address list; unparseable text has none."""
    if not isinstance(value, str) or not value:
        return []
    try:
        return [address for _, address in getaddresses([value]) if address]
    except Exception:
        return []


def _condition_keys(condition: Condition) -> tuple[list[Any], bytes | None]:
    if condition.field != "subject":
        return list(_address_criteria(condition.field.upper(), condition.value)), None
    if condition.value.isascii():
        return ["SUBJECT", _quote(condition.value)], None
    # imaplib sends this literal after the command's last key.
    return ["SUBJECT"], condition.value.encode("utf-8")


@dataclass(frozen=True)
class SavedRule:
    """A saved custom rule (rules_store), matched the way its Sieve script reads.

    from and to compare each address (local@domain) of the From or To header,
    subject the decoded Subject; contains is a substring, is the whole value,
    both case-insensitive. match all needs every condition, any one of them.
    """

    spec: SieveFilter

    @property
    def folder(self) -> str:
        return self.spec.folder

    @property
    def mark_read(self) -> bool:
        return self.spec.mark_read

    def report(self) -> dict[str, Any]:
        params = self.spec.params()
        return {key: params[key] for key in ("name", "conditions", "match", "mark_read")}

    def criteria(self) -> tuple[list[Any], bytes | None]:
        """UID SEARCH keys that every matching message satisfies, as for packaged rules.

        from and to narrow by their longest word (or a quoted or encoded header),
        subject by its whole value; the conditions then decide locally. A command
        carries one literal at most, so only one non-ASCII value can narrow it.
        """
        keys = [_condition_keys(condition) for condition in self.spec.conditions]
        if self.spec.match == "all":
            # Every condition must hold, so the keys of any of them narrow safely.
            criteria = [key for tokens, literal in keys if literal is None for key in tokens]
            wide = next((item for item in keys if item[1] is not None), None)
            return (criteria + wide[0], wide[1]) if wide else (criteria, None)
        # One OR chain narrows only if every condition has keys; its literal goes last.
        if any(not tokens for tokens, _ in keys) or sum(item[1] is not None for item in keys) > 1:
            return [], None
        keys.sort(key=lambda item: item[1] is not None)
        criteria = [key for tokens, _ in keys[:-1] for key in ("OR", *tokens)]
        return criteria + keys[-1][0], keys[-1][1]

    def matches(self, row: dict[str, Any]) -> bool:
        def holds(condition: Condition) -> bool:
            if condition.field == "subject":
                values = [display_value(row, "subject")]
            else:
                values = _addresses(row.get(condition.field))
            needle = condition.value.casefold()
            if condition.operator == "is":
                return any(value.casefold() == needle for value in values)
            return any(needle in value.casefold() for value in values)

        results = (holds(condition) for condition in self.spec.conditions)
        return all(results) if self.spec.match == "all" else any(results)


def load_rules(path: Path = RULES_PATH) -> tuple[tuple[FolderRule, ...], str]:
    """Reload reviewed configuration, refusing missing, malformed or oversized files."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError
        data = json.loads(raw, object_pairs_hook=_unique_object)
        if isinstance(data, dict) and {"source", "source_folder", "folder"}.intersection(data):
            require_rules_source(None)
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "rules"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or not isinstance(data["rules"], list)
            or len(data["rules"]) > MAX_RULES
        ):
            raise ValueError
        rules = []
        for rule in data["rules"]:
            if isinstance(rule, dict) and {"source", "source_folder"}.intersection(rule):
                # Source overrides are not part of the reviewed rules format.
                # Use the same refusal as all other rules entry points, even
                # when an override happens to spell an otherwise allowed name.
                require_rules_source(None)
            if not isinstance(rule, dict) or set(rule) != {"field", "contains", "folder"}:
                raise ValueError
            if any(
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > 500
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                for value in rule.values()
            ):
                raise ValueError
            if (
                rule["field"] not in {"from", "to", "subject"}
                or not rule["folder"].startswith("Folders/")
                or not _real_folder_name(rule["folder"])
            ):
                raise ValueError
            rules.append(FolderRule(**rule))
        return tuple(rules), hashlib.sha256(raw).hexdigest()
    except RulesSourceError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise ConfigError("Cannot load valid connector folder rules") from None


def engine_rules(path: Path = RULES_PATH, saved_path: Path | None = None) -> tuple[tuple, str]:
    """Packaged rules, then saved custom rules in the order they were added.

    One digest names both files, so saving a rule invalidates outstanding rule
    previews. Without saved rules it stays the packaged file's own digest.
    """
    rules, digest = load_rules(path)
    saved, saved_digest = load_saved(saved_path) if saved_path is not None else ((), None)
    if not saved:
        return rules, digest
    combined = hashlib.sha256(f"{digest}\0{saved_digest}".encode("ascii")).hexdigest()
    return (*rules, *(SavedRule(spec) for spec in saved)), combined


def _review_rows(
    costs: dict,
    window: float,
    limit: int,
    *,
    explore: bool = False,
    probe: bool = False,
    lower_bounds: dict | None = None,
) -> int:
    """Rows whose strict review is expected to fit the remaining window.

    Warm reviews repeat folder listing and target checks, then read fresh
    headers per row. Exclude the singleton that populates per-call inventories.
    With completed samples of two sizes,
    fit both costs from the smallest and largest, and allow half the time left
    after the fixed part for rows. One sample cannot separate them: charge it
    wholly per row within half the window. explore doubles that sample instead
    when the result is smaller, so a fixed cost above half the window cannot
    pin later batches at its size. On a later call, probe the limit when the
    sample fits the remaining window: fixed costs may leave room for only one
    warm review per call. The secured singleton and overrun caps bound this
    probe if row costs instead dominate.
    """
    if not costs:
        rows = min(CALIBRATION_ROWS, limit)
    elif len(costs) > 1:
        low, high = min(costs), max(costs)
        per_row = max(0.0, (costs[high] - costs[low]) / (high - low))
        fixed = max(0.0, costs[low] - low * per_row)
        if per_row:
            rows = (window - fixed) / (2 * per_row)
        else:
            rows = limit if fixed < window else 1
    else:
        ((sampled, seconds),) = costs.items()
        rows = sampled * window / (2 * seconds) if seconds > 0 else limit
        if explore:
            rows = max(rows, limit if probe and seconds < window else 2 * sampled)
    # An interrupted review is a censored sample, not a completed timing to
    # fit. Do not retry its size in a window shorter than its observed cost.
    for sampled, seconds in (lower_bounds or {}).items():
        if seconds >= window:
            rows = min(rows, max(1, sampled // 2))
    return max(1, min(limit, int(rows)))


class FolderRulesService:
    """First matching rule wins; optional Seen changes require the same approval."""

    def __init__(
        self, cleanup: CleanupService, path: Path = RULES_PATH, *, saved_path: Path | None = None
    ):
        self.cleanup = cleanup
        self.path = path
        self.saved_path = (
            Path(saved_path) if saved_path is not None else store_path(cleanup.settings)
        )
        # Let preview show source refusals in its blocked list. Other malformed
        # configuration, including an invalid saved rules store, fails immediately.
        with suppress(RulesSourceError):
            self.load()

    def load(self) -> tuple[tuple, str]:
        """Reload the packaged and saved rules and their digest, refusing invalid files."""
        return engine_rules(self.path, self.saved_path)

    def _plan(
        self,
        connection,
        deadline,
        rules,
        folder,
        *,
        source_uids=None,
        mark_read=False,
        rule_mark_read=True,
        bound_rows=None,
        source_rows=None,
        row_refusals=None,
    ):
        folder = require_rules_source(folder, connection)
        cleanup = self.cleanup
        # Keep discovery, confirmation and the shared per-UID move checks on
        # the same raw identity rule for this entire rules connection.
        connection._cleanup_raw_identity = True
        connection._rules_reviewed = []
        folders = cleanup.client._folders(connection)
        real_folders = _move_folders(connection)
        if folder not in real_folders:
            raise MailValidationError("Rules require an existing real source folder")
        for rule in rules:
            if rule.folder not in real_folders:
                raise MailValidationError("Rules require existing real destination folders")
            cleanup.backend.validate_target("move_to_folder", {"folder": rule.folder}, connection)
        selected = set()
        groups, reports = [], []
        preview_rows = None
        if (
            source_rows is not None
            and getattr(connection, "_rules_preview_state", None) is not None
        ):
            # Review this one bounded source set together. Repeating the same
            # cross-folder audit per rule can itself exceed every preview call,
            # even after all raw inventories have finished. Targets were checked
            # above; this shared membership check needs no move target.
            preview_rows = (
                cleanup._resolve(
                    connection,
                    deadline,
                    {"query": {"folder": folder}},
                    "move_to_folder",
                    {
                        "mark_read": mark_read
                        or (
                            rule_mark_read
                            and any(
                                rule.mark_read
                                for index, rule in enumerate(rules, start=1)
                                if any(row["rule_index"] == index for row in source_rows)
                            )
                        ),
                    },
                    folders,
                    source_rows=source_rows,
                    row_refusals=row_refusals,
                )
                if source_rows
                else []
            )
        for index, rule in enumerate(rules, start=1):
            if isinstance(rule, SavedRule):
                query = {"folder": folder, "rule": rule}
            else:
                query = {
                    "folder": folder,
                    "from_" if rule.field == "from" else rule.field: rule.contains,
                }
            rows = (
                [row for row in preview_rows if row["rule_index"] == index]
                if preview_rows is not None
                else cleanup._resolve(
                    connection,
                    deadline,
                    {"query": query},
                    "move_to_folder",
                    # A saved rule's own mark_read applies too, except with
                    # rule_mark_read=False: the timer never changes Seen.
                    {
                        "folder": rule.folder,
                        "mark_read": mark_read or (rule_mark_read and rule.mark_read),
                    },
                    folders,
                    exclude_handles=selected,
                    source_uids=source_uids,
                    bound_rows=(
                        [row for row in bound_rows if row.get("rule_index") == index]
                        if bound_rows is not None
                        else None
                    ),
                    source_rows=(
                        [row for row in source_rows if row.get("rule_index") == index]
                        if source_rows is not None
                        else None
                    ),
                )
            )
            if preview_rows is not None and not (mark_read or (rule_mark_read and rule.mark_read)):
                for row in rows:
                    row.pop("seen", None)
            for row in rows:
                require_rules_source(row.get("folder"))
                if "source_folder" in row:
                    require_rules_source(row["source_folder"])
                row["rule_index"] = index
            # Protected matches are still part of the reviewed safety evidence.
            # Bind them too so confirmation can refresh exact UIDs without
            # rediscovering the selector or auditing unrelated folder contents.
            connection._rules_reviewed.extend(rows)
            blocked = cleanup._blocked(rows, real_folders)
            if any(row["unresolved"] for row in blocked):
                raise _UnresolvedRows(
                    "Rules batch has unresolved messages; cannot verify protected memberships"
                )
            selected.update(_handle(row) for row in rows)
            blocked_ids = {row["message_id"] for row in blocked}
            eligible = [
                row
                for row in rows
                if row["message_id"] not in blocked_ids and folder != rule.folder
            ]
            groups.append(eligible)
            reports.append(
                {
                    "rule": index,
                    **rule.report(),
                    "target": rule.folder,
                    "matched_count": len(rows),
                    "count": len(eligible),
                    "personal_provider_bypass_count": sum(
                        bool(row.get("personal_provider_bypasses")) for row in eligible
                    ),
                    "samples": [cleanup._sample(row, real_folders) for row in eligible[:10]],
                    "blocked": blocked,
                    "already_in_folder": len(rows) if folder == rule.folder else 0,
                }
            )
        return groups, reports

    @staticmethod
    def _batch_options(limit, order):
        if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
            raise MailValidationError("Rules batch limit must be between 1 and 200")
        if order not in ("oldest", "newest"):
            raise MailValidationError("Rules batch order must be oldest or newest")

    def preview_rules(
        self,
        folder: str = "INBOX",
        mark_read: bool = False,
        limit: int = MAX_BATCH,
        order: str = "oldest",
        cursor: str | None = None,
    ) -> dict:
        cleanup = self.cleanup
        if type(mark_read) is not bool:
            raise MailValidationError("mark_read must be a boolean")
        self._batch_options(limit, order)
        result = {
            "action": "apply_rules",
            "folder": folder,
            "count": 0,
            "matched_count": 0,
            "rules": [],
            "blocked": [],
            "token": None,
            "expires_at": None,
            "action_hash": None,
            "partial": False,
            "total_is_exact": True,
            "mark_read": mark_read,
            "limit": limit,
            "order": order,
            "total_matches": 0,
            "batch_count": 0,
            "remaining": 0,
            "estimated_calls": 0,
            "next_cursor": None,
            **PreviewExclusions().response(),
        }
        try:
            require_rules_source(folder)
            rules, digest = self.load()
            for rule in rules:
                try:
                    json.dumps(rule.report(), ensure_ascii=False).encode("utf-8")
                except UnicodeEncodeError:
                    raise MailValidationError("Rules require valid UTF-8 text") from None
        except RulesSourceError as error:
            result.update(blocked=[{"reason": str(error)}], reason=str(error))
            return result
        started = mail.time.monotonic()
        options = (folder.casefold(), digest, mark_read, limit, order)
        states = getattr(cleanup, "_rules_preview_states", None)
        if states is None:
            states = cleanup._rules_preview_states = {}
        for key, saved in list(states.items()):
            if saved["expires"] <= started:
                states.pop(key, None)
        if cursor is not None:
            if not isinstance(cursor, str) or cursor not in states:
                raise MailValidationError(
                    "Invalid, expired or evicted rules preview cursor; preview again"
                )
            if states[cursor]["options"] != options:
                raise MailValidationError("Rules preview options changed; preview again")
            state = states.pop(cursor)
        else:
            state = {"options": options, "served": set()}
        # Expire idle cursors, rather than scans that keep making progress.
        state["expires"] = started + 600
        exclusions = PreviewExclusions(state.setdefault("excluded", {}))

        def keep_cursor():
            # A refused call hands out no new cursor, so the caller's one stays valid.
            if cursor is not None:
                while len(states) >= MAX_PREVIEWS:
                    states.pop(next(iter(states)))
                states[cursor] = state

        progress = state.get("progress", 0)
        stalled = False
        audit_before = state.get("audit_seconds", 0)
        deadline = _Deadline(
            min(
                PREVIEW_SECONDS,
                cleanup.settings.preview_timeout or cleanup.settings.call_timeout,
            ),
            cleanup.settings.timeout,
        )
        deadline.stage = "connection setup"
        try:
            with cleanup.client._session(deadline=deadline) as (connection, _):
                folder = require_rules_source(folder, connection)
                # Keep this state on the per-call proxy, including with reused
                # test connections. Execution must never inherit preview caches.
                object.__setattr__(connection, "_rules_preview_state", state)
                object.__setattr__(connection, "_cleanup_inventory", {})
                deadline.stage = "source discovery"
                candidates = discover(
                    cleanup.client, connection, deadline, rules, folder, state, order
                )
                live = {row["uid"] for row in candidates}
                state["served"].intersection_update(live)
                # A refused message that has left the source needs no more reporting.
                present = {row["message_id"] for row in candidates}
                for identifier in set(exclusions.reasons) - present:
                    del exclusions.reasons[identifier]
                pending = [
                    row
                    for row in candidates
                    if row["uid"] not in state["served"]
                    and row["message_id"] not in exclusions.reasons
                ]
                review_limit = max(1, limit // state.get("batch_scale", 1))
                # Only completed warm reviews are comparable across sizes.
                # The singleton pays the per-call inventory setup cost.
                costs = state.setdefault("review_costs", {})
                lower_bounds = state.setdefault("review_lower_bounds", {})
                warm = {}

                def review(size, *, singleton=False):
                    rows = pending[:size]
                    fingerprint = source_fingerprint(rows)
                    audited = state.get("audit_seconds", 0)
                    review_started = mail.time.monotonic()
                    deadline.stage = "rules folder validation"
                    if not singleton:
                        # Cold audit reads can authorize the singleton, but a
                        # warm timing sample must pay for the same candidate
                        # reads as later calls. Keep the identity inventories.
                        inventory = connection._cleanup_inventory
                        for key in list(inventory):
                            if len(key) == 3 and key[2][0] == "raw_identity_fresh":
                                inventory.pop(key)
                    # Only the singleton may spend the response reserve. Its
                    # reply can follow the normal I/O cutoff; speculative
                    # reviews retain time for closing and serializing a reply.
                    reserve = deadline.response_reserve
                    deadline.rules_response_reserve = reserve
                    # Bind all folder/target validation within this review to
                    # one complete LIST response. The next review gets a fresh
                    # listing; SELECT membership checks remain fresh per visit.
                    list_folders = connection.list
                    listing = None

                    def review_listing(*args, **kwargs):
                        nonlocal listing
                        if args or kwargs:
                            return list_folders(*args, **kwargs)
                        if listing is None:
                            listing = list_folders()
                        return listing

                    object.__setattr__(connection, "list", review_listing)
                    try:
                        if singleton:
                            deadline.response_reserve = 0
                        plan = self._plan(
                            connection,
                            deadline,
                            rules,
                            folder,
                            mark_read=mark_read,
                            source_rows=rows,
                            row_refusals=exclusions,
                        )
                    except CallDeadlineExceeded:
                        if rows and not singleton:
                            seconds = mail.time.monotonic() - review_started
                            seconds -= state.get("audit_seconds", 0) - audited
                            lower_bounds[len(rows)] = max(lower_bounds.get(len(rows), 0), seconds)
                            state["review_cap"] = max(1, len(rows) // 2)
                            state["review_cap_seconds"] = seconds
                            state["review_overruns"] = state.get("review_overruns", 0) + 1
                        raise
                    finally:
                        object.__delattr__(connection, "list")
                        deadline.response_reserve = reserve
                        # Refused rows leave later reviews, however this one ended.
                        pending[:] = [
                            row for row in pending if row["message_id"] not in exclusions.reasons
                        ]
                    # Raw folder audits run once per chain and are then reused.
                    seconds = mail.time.monotonic() - review_started
                    if singleton and rows:
                        # Fixed folder checks depend on the mailbox, not the chain.
                        cleanup._rules_warm_review = seconds - (
                            state.get("audit_seconds", 0) - audited
                        )
                    if any(row["message_id"] in exclusions.reasons for row in rows):
                        # Refused rows leave this batch and its evidence too.
                        rows = [row for row in rows if row["message_id"] not in exclusions.reasons]
                        fingerprint = source_fingerprint(rows)
                    # After a held batch, a warm one-row review is the first timing sample.
                    if (
                        singleton
                        and state.get("batch_held")
                        and state.get("audit_seconds", 0) == audited
                    ):
                        warm[1] = seconds
                    if rows and not singleton:
                        costs[len(rows)] = seconds - (state.get("audit_seconds", 0) - audited)
                        lower_bounds.pop(len(rows), None)
                        state["review_overruns"] = 0
                        # A completed smaller sample permits refitting on the
                        # next call. Keep censored timings to constrain retries
                        # when its available window has not improved.
                        state.pop("review_cap", None)
                    return rows, fingerprint, plan

                # Secure one complete item before attempting an all-or-nothing
                # calibration or fitted batch, then size it from the time left.
                # A review costing half that time or more leaves no room for a
                # second one, so the call's one review takes a whole batch of
                # one rule.
                size = 1
                window = deadline.io_end - mail.time.monotonic()
                warm_cost = getattr(cleanup, "_rules_warm_review", None)
                if warm_cost is not None and window / 2 <= warm_cost < window:
                    # An overrun in a call's leftover window says nothing about a whole call.
                    if window >= 2 * state.get("review_cap_seconds", float("inf")):
                        state.pop("review_cap", None)
                    size = min(len(pending), review_limit, state.get("review_cap", review_limit))
                    for sampled, seconds in lower_bounds.items():
                        if seconds >= window:
                            size = min(size, sampled // 2)
                    size = max(1, size)
                    if size > 1:
                        # Apply reviews once per rule, so at this cost a token holds one rule.
                        first = pending[0]["rule_index"]
                        pending.sort(key=lambda row: row["rule_index"] != first)
                        size = min(size, sum(row["rule_index"] == first for row in pending))
                try:
                    reviewed, fingerprint, (groups, result["rules"]) = review(
                        size, singleton=size == 1
                    )
                except CallDeadlineExceeded:
                    if size > 1 and state.get("audit_seconds", 0) == audit_before:
                        # The halved cap is the progress, so a short overrun must not
                        # lift it again; the stall guard bounds singletons.
                        state.pop("review_cap_seconds", None)
                        state["progress"] = state.get("progress", 0) + 1
                    raise
                except _UnresolvedRows:
                    if size == 1:
                        raise
                    # Keep serving the rows before the one that cannot be verified.
                    state["review_cap"] = size // 2
                    state.pop("review_cap_seconds", None)
                    state["progress"] = state.get("progress", 0) + 1
                    deadline.stage = "narrowing a batch with an unresolved message"
                    raise _BatchNarrowed("A row in this batch cannot be verified") from None
                if size > 1 and reviewed:
                    # One slow sample must not pin a mailbox at one rule per approval once it is
                    # fast again, and this path skips the one-row review that would correct it.
                    cleanup._rules_warm_review = min(warm_cost, costs[len(reviewed)])
                batch, secured, overran = connection._rules_reviewed, mail.time.monotonic(), False
                fitted = len(costs) > 1
                expand = size == 1
                attempted = False
                while (
                    expand
                    and not overran
                    and not deadline.expired
                    and len(reviewed) < min(len(pending), review_limit)
                ):
                    window = deadline.io_end - mail.time.monotonic()
                    # A cold audit can leave only a fraction of a warm review
                    # window. Retry calibration when that window has doubled;
                    # equal-window repeated overruns must still shrink.
                    if window >= 2 * state.get("review_cap_seconds", float("inf")):
                        state.pop("review_cap", None)
                    size = _review_rows(
                        costs or warm,
                        window,
                        min(review_limit, state.get("review_cap", review_limit)),
                        explore=not fitted
                        and state.get("batch_scale", 1) == 1
                        and (
                            not attempted
                            or (len(costs) == 1 and window >= 2 * next(iter(costs.values())))
                        ),
                        probe=not attempted,
                        lower_bounds=lower_bounds,
                    )
                    if size <= len(reviewed):
                        break
                    attempted = True
                    try:
                        grown = review(size)
                    except (CallDeadlineExceeded, _IncompleteIdentityInventory) as error:
                        # Keep the batch already reviewed. Time its approval
                        # without the larger review that missed the deadline.
                        started += mail.time.monotonic() - secured
                        overran = True
                        if isinstance(error, _IncompleteIdentityInventory):
                            result.update(
                                partial=True,
                                total_is_exact=False,
                                reason="Mailbox membership changed; kept the completed rules batch",
                            )
                        break
                    reviewed, fingerprint, (groups, result["rules"]) = grown
                    batch, secured = connection._rules_reviewed, mail.time.monotonic()
                    expand = not fitted or len(pending) < review_limit
                    fitted = len(costs) > 1
                if source_fingerprint(batch) != fingerprint:
                    raise MailValidationError("Rule messages changed while selecting the batch")
                reviewed = batch
                # This call's one-time audit, not the mailbox, limited the batch. A token now
                # would cost another approval and apply, so hand over to the next call, once.
                # An overrun in the leftover window says nothing about that call.
                if (
                    len(reviewed) <= CALIBRATION_ROWS
                    and len(reviewed) < min(len(pending), review_limit)
                    and state.get("audit_seconds", 0) > audit_before
                    and not state.get("batch_held")
                ):
                    state["batch_held"] = True
                    state.pop("review_cap", None)
                    lower_bounds.clear()
                    result["rules"] = []
                    deadline.stage = "the call that finished the rules audit"
                    raise CallDeadlineExceeded("Rules audit used this call's budget")
                for index, report in enumerate(result["rules"], start=1):
                    report["matched_count"] = sum(row["rule_index"] == index for row in candidates)
                rows = [row for group in groups for row in group]
                result["count"] = len(rows)
                result["matched_count"] = sum(rule["matched_count"] for rule in result["rules"])
                result["total_matches"] = result["matched_count"]
                result["batch_count"] = len(reviewed)
                result["remaining"] = len(pending) - result["batch_count"]
                if result["remaining"] and not result.get("reason"):
                    result["reason"] = (
                        f"Showing {len(reviewed)} of {result['total_matches']} matches "
                        "within the batch size and time budget; more runs are needed"
                    )
                if rows:
                    # The complete plan above is the approval evidence. A
                    # deadline reached after it must not discard that progress;
                    # no more mailbox I/O is needed to issue its token.
                    # A batched rules move with mark_read sends a STORE and a MOVE.
                    move = {
                        "mark_read": mark_read
                        or any(rule.mark_read for rule, group in zip(rules, groups) if group)
                    }
                    result["estimated_calls"] = cleanup._estimated_calls(
                        started, "move_to_folder", len(rows), params=move
                    )
                    # Unlike direct cleanup, rules must refresh the complete
                    # reviewed set before moving any rule's first message.
                    if not cleanup._preview_has_headroom(started, "move_to_folder", params=move):
                        result["reason"] = "Insufficient execute headroom; preview rules again"
                    else:
                        params = {
                            "rules_digest": digest,
                            "mark_read": mark_read,
                            "limit": limit,
                            "order": order,
                        }
                        selector = {"folder": folder, "reviewed_rows": reviewed}
                        result["token"], result["expires_at"] = cleanup.tokens.issue(
                            "apply_rules", params, selector, rows
                        )
                        result["action_hash"] = approval_hash("apply_rules", params, selector, rows)
                        cleanup.tokens.record_verification_cost(
                            result["token"], mail.time.monotonic() - started
                        )
                if not rows or result["token"]:
                    state["served"].update(row["uid"] for row in reviewed)
                    state["stalled"] = 0
        except RulesSourceError as error:
            result.update(blocked=[{"reason": str(error)}], reason=str(error))
        except (MailError, _PreviewInventoryGrew) as error:
            if not isinstance(error, (CallDeadlineExceeded, _PreviewInventoryGrew)) and not (
                deadline.expired
            ):
                keep_cursor()
                raise
            result.update(
                partial=True,
                total_is_exact=False,
                token=None,
                expires_at=None,
                action_hash=None,
                reason=(
                    "Rules preview membership changed during "
                    if isinstance(error, _PreviewInventoryGrew)
                    else "Rules preview deadline reached during "
                )
                + deadline.stage,
            )
            if isinstance(error, _BatchNarrowed):
                result["reason"] = (
                    "A message in this batch cannot be verified; the next call reviews fewer"
                )
            matches = state.get("source", {}).get("matches", [])
            result["matched_count"] = result["total_matches"] = len(matches)
            result["remaining"] = sum(
                row["uid"] not in state["served"] and row["message_id"] not in exclusions.reasons
                for row in matches
            )
        except MailValidationError:
            keep_cursor()
            raise
        if (result["partial"] and not result["token"]) or (result["count"] and not result["token"]):
            # A completed batch is durable progress; a repeated failed command
            # is not. Keep productive scans batched across ordinary deadlines;
            # shrink only stalled calls, and bound retries for indivisible
            # steps or a review that never has execute headroom. A stall also
            # discards review timings, so the next call calibrates again.
            if state.get("progress", 0) > progress:
                state["stalled"] = 0
            else:
                state["batch_scale"] = min(1024, state.get("batch_scale", 1) * 4)
                state["stalled"] = state.get("stalled", 0) + 1
                state["review_costs"] = {}
            stalled = state["stalled"] >= MAX_STALLED_PREVIEWS
            if stalled:
                result["reason"] = (
                    "Rules preview cannot make progress within the call budget; preview again "
                    "after mailbox operations become faster"
                )
        if not stalled and (
            result["partial"] or result["remaining"] or (result["count"] and not result["token"])
        ):
            if state["expires"] > mail.time.monotonic():
                while len(states) >= MAX_PREVIEWS:
                    states.pop(next(iter(states)))
                result["next_cursor"] = secrets.token_urlsafe(32)
                states[result["next_cursor"]] = state
        if result["partial"] and not stalled:
            result["reason"] += (
                "; continue preview_rules with next_cursor"
                if result["next_cursor"]
                else "; saved preview expired; start a fresh preview"
            )
        audit = state.get("inventories", {}).get("All Mail")
        if result["partial"] and audit and audit["next_uid"] < int(audit["snapshot"]["uidnext"]):
            result["reason"] += (
                f"; all_mail_audit={audit['next_uid']}/{audit['snapshot']['uidnext']}"
                f" rewinds={state.get('all_mail_rewinds', 0)}"
            )
        if exclusions.reasons and not (result["batch_count"] or result.get("reason")):
            result["reason"] = "All remaining matches were excluded; no rules token issued"
        if limit == 1 or result["partial"] or result["remaining"]:
            result["reason"] = (
                result.get("reason", "Rules preview completed") + "; " + deadline.diagnostic()
            )
        result.update(exclusions.response())
        return result

    def apply_rules(self, token: str, *, limit: int | None = None) -> dict:
        call_limit = _batch_limit(limit)
        cleanup = self.cleanup
        with cleanup._exclusive():
            grant = cleanup.tokens.inspect(token, "apply_rules")
            if call_limit is not None and call_limit > (
                grant.approved_total or len(grant.messages)
            ):
                raise MailValidationError("Execute limit cannot widen the approved batch")
            require_rules_source(grant.selector.get("folder"))
            reviewed = grant.selector.get("reviewed_rows")
            if (
                set(grant.selector) != {"folder", "reviewed_rows"}
                or not isinstance(reviewed, list)
                or not 0 < len(reviewed) <= MAX_BATCH
            ):
                raise MailValidationError(
                    "Rules batch has unresolved messages; cannot verify protected memberships; "
                    "preview again"
                )
            for row in [*grant.messages, *reviewed]:
                require_rules_source(row.get("folder"))
                if "source_folder" in row:
                    require_rules_source(row["source_folder"])
                if type(row.get("rule_index")) is not int:
                    raise MailValidationError(
                        "Rules batch has unresolved messages; cannot verify protected memberships"
                    )
            mark_read = grant.params.get("mark_read")
            if type(mark_read) is not bool:
                raise MailValidationError("Preview token has an invalid mark_read flag")
            limit, order = grant.params.get("limit"), grant.params.get("order")
            self._batch_options(limit, order)
            if len(reviewed) > limit:
                raise MailValidationError("Preview token exceeds its rules batch limit")
            rules, digest = self.load()
            if grant.params != {
                "rules_digest": digest,
                "mark_read": mark_read,
                "limit": limit,
                "order": order,
            }:
                raise MailValidationError("Folder rules changed after preview; preview again")
            if any(
                not 1 <= row["rule_index"] <= len(rules) for row in [*grant.messages, *reviewed]
            ):
                raise MailValidationError("Preview token has an invalid rule identity")
            approved_total = grant.approved_total or len(grant.messages)
            context = {
                "run_id": grant.run_id or secrets.token_hex(16),
                "action": "apply_rules",
                "token_digest": grant.run_token_digest or token_digest(token),
                "approved_total": approved_total,
                "chunk_index": grant.chunk_index,
            }
            if grant.run_id is not None:
                run = cleanup.backend.log.get_run(grant.run_id)
                if (
                    run is None
                    or run["done"] != grant.run_done
                    or run["uncertain"] != grant.run_uncertain
                    or run["approved_total"] != approved_total
                    or run["status"] == "completed"
                ):
                    raise MailValidationError("Continuation does not match durable run progress")
                context["chunk_index"] = run["next_chunk_index"]
            started = mail.time.monotonic()
            deadline = cleanup._cap_execute_deadline(
                cleanup.client._deadline(execute=True), started
            )
            claimed = False

            def claim_token():
                nonlocal claimed, grant
                if not claimed:
                    grant = cleanup.tokens.claim(token, "apply_rules")
                    claimed = True
                return grant

            results = []
            stop_reason = None
            blocked = list(grant.blocked)
            with cleanup.client._session(deadline=deadline) as (connection, _):
                # Reuse only raw occurrence discovery. Every resolution still
                # clears candidate caches, revalidates the audit's allocation,
                # and reads matching headers, policy evidence and flags fresh.
                # Keep preview behavior off this mutating connection.
                object.__setattr__(
                    connection,
                    "_rules_apply_state",
                    {"options": (grant.selector["folder"].casefold(), digest, mark_read)},
                )
                # Rules retain their stricter whole-review preflight: ambiguity
                # in a later rule must prevent any earlier rule from mutating.
                # A continuation narrows this review to pending and skipped rows.
                groups, reports = self._plan(
                    connection,
                    deadline,
                    rules,
                    grant.selector["folder"],
                    mark_read=grant.params["mark_read"],
                    bound_rows=reviewed,
                )
                if sum(report["matched_count"] for report in reports) > MAX_BATCH:
                    raise MailValidationError("Rules batch exceeds the hard cap of 200 matches")
                rows = [row for group in groups for row in group]
                cleanup._check_execution_rows(rows, grant.messages, grant.fingerprint)
                if _fingerprint(connection._rules_reviewed) != _fingerprint(reviewed):
                    raise MailValidationError("Reviewed rule messages changed after preview")
                approved_handles = {_handle(row) for row in grant.messages}
                skipped = [row for row in reviewed if _handle(row) not in approved_handles]

                def verify_skipped():
                    # Source policy can drift even when every reviewed row was eligible.
                    require_rules_source(grant.selector["folder"], connection)
                    if skipped:
                        self._plan(
                            connection,
                            deadline,
                            rules,
                            grant.selector["folder"],
                            mark_read=mark_read,
                            bound_rows=skipped,
                        )
                        if _fingerprint(connection._rules_reviewed) != _fingerprint(skipped):
                            raise MailValidationError(
                                "Reviewed rule messages changed after preview"
                            )

                for rule, group in zip(rules, groups):
                    if not group:
                        continue
                    attempted_count = sum(
                        result["done"] + result["uncertain"] for result in results
                    )
                    if call_limit is not None and attempted_count >= call_limit:
                        stop_reason = "Execute call limit reached"
                        break
                    move_grant = replace(
                        grant,
                        action="move_to_folder",
                        params={
                            "folder": rule.folder,
                            "mark_read": grant.params["mark_read"] or rule.mark_read,
                        },
                        selector={"query": {"folder": grant.selector["folder"]}},
                        messages=group,
                        fingerprint=_fingerprint(group),
                        approved_total=len(group),
                        run_done=0,
                        run_uncertain=0,
                        blocked=[],
                    )
                    try:
                        verify_skipped()
                        if results:
                            group = cleanup._resolve(
                                connection,
                                deadline,
                                move_grant.selector,
                                move_grant.action,
                                move_grant.params,
                                cleanup.client._folders(connection),
                                bound_rows=move_grant.messages,
                            )
                            cleanup._check_execution_rows(
                                group, move_grant.messages, move_grant.fingerprint
                            )
                        result = cleanup._execute_chunks(
                            connection,
                            deadline,
                            started,
                            move_grant,
                            group,
                            token,
                            None,
                            None,
                            claim_token=claim_token,
                            before_refresh=verify_skipped,
                            run_context=context,
                            limit=None if call_limit is None else call_limit - attempted_count,
                        )
                    except Exception:
                        if not results:
                            raise
                        stop_reason = (
                            "Rules interrupted during preparation; inspect logged outcomes"
                        )
                        break
                    results.append(result)
                    blocked.extend(result.get("blocked", []))
                    if result["partial"]:
                        stop_reason = result.get(
                            "error", "Rules interrupted; inspect logged outcomes"
                        )
                        break
            log_ids = [log_id for result in results for log_id in result["log_ids"]]
            done = sum(result["done"] for result in results)
            uncertain = sum(result["uncertain"] for result in results)
            run_done = grant.run_done + done
            run_uncertain = grant.run_uncertain + uncertain
            messages = [row for result in results for row in result["messages"]]
            unlogged = sum(result.get("unlogged", 0) for result in results)
            partial = run_done != approved_total or any(result["partial"] for result in results)
            durable = not unlogged
            try:
                if context.get("_call_started") or grant.run_id is not None:
                    run = cleanup.backend.log.finish_run(
                        context["run_id"], stop_reason=stop_reason, blocked=len(blocked)
                    )
                    partial = partial or run["partial"]
                    durable = (
                        durable and run["done"] == run_done and run["uncertain"] == run_uncertain
                    )
                elif claimed:
                    durable = False
            except Exception:
                durable = False
            if not durable:
                partial = True
                stop_reason = "Rules run outcome could not be logged; inspect the journal"
            continuation = None
            if claimed and partial and durable:
                attempted = {
                    (row["folder"], row["uidvalidity_before"], row["uid_before"])
                    for row in messages
                    if row["status"] != "not_attempted"
                }
                attempted.update(_handle(row) for row in blocked)
                pending = [row for row in grant.messages if _handle(row) not in attempted]
                next_grant = replace(
                    grant,
                    selector={
                        **grant.selector,
                        "reviewed_rows": [row for row in reviewed if _handle(row) not in attempted],
                    },
                    run_id=context["run_id"],
                    run_token_digest=context["token_digest"],
                    approved_total=approved_total,
                    run_done=run_done,
                    run_uncertain=run_uncertain,
                    blocked=blocked,
                    chunk_index=context["chunk_index"],
                )
                continuation = cleanup.tokens.continue_grant(next_grant, pending)
            elif not claimed and partial and durable:
                # Final read-only verification can refuse the frozen chunk
                # before its claim callback. The held approval still describes
                # the untouched remainder; revalidate it before publishing it.
                try:
                    cleanup.tokens.inspect(token, "apply_rules")
                    continuation = token
                except MailValidationError:
                    pass
            remaining = approved_total - run_done - run_uncertain
            continuation_reason = None
            if remaining and continuation is None:
                continuation_reason = (
                    "Rules run outcome could not be durably verified; inspect the journal"
                    if not durable
                    else "Preview token expired; request a new preview"
                    if cleanup.tokens.clock() >= grant.expires
                    else stop_reason or "Continuation unavailable; request a new preview"
                )
            response = {
                "log_ids": log_ids,
                "count": done,
                "done": done,
                "run_id": context["run_id"],
                "approved_total": approved_total,
                "run_done": run_done,
                "run_uncertain": run_uncertain,
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
                "continuation_reason": continuation_reason,
                "expires_at": grant.expires_at,
                "missing": [row for result in results for row in result.get("missing", [])],
                # Rows a batched chunk left untouched or could not verify, with reasons.
                "mismatched": [row for result in results for row in result.get("mismatched", [])],
            }
            if stop_reason:
                response["error"] = stop_reason
            if unlogged:
                response["unlogged"] = unlogged
            if (
                done == 0
                and uncertain == 0
                and any(result.get("error_code") == "first_chunk_unavailable" for result in results)
            ):
                response["error_code"] = "first_chunk_unavailable"
            return response
