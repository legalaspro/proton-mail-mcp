"""Token-gated background header rebuilds that preserve the serving index."""

from __future__ import annotations

import math
import os
import secrets
import threading
import time
from collections import OrderedDict
from typing import Any

from . import canonical
from .action_log import ActionLog, new_record, token_digest
from .mail import CallDeadlineExceeded, MailError, MailValidationError
from .search import _coverage_status, _uidvalidity
from .search_index import _ERRORS, _Unavailable

_TOKEN_TTL = 300
_MAX_TOKENS = 128
_SWAP_WAIT_SECONDS = 5.0


class _Stopped(Exception):
    pass


class _ReindexFailure(MailError):
    """A fixed reason code, never a server exception or indexed message."""


class ReindexService:
    def __init__(self, client: Any):
        self.client = client
        self.log = ActionLog(client.settings.action_log_path)
        # State reads never wait for an IMAP command, log append or SQLite swap.
        self._lock = threading.Lock()
        self._control = threading.Lock()
        self._guard_lock = threading.Lock()
        self._guard: int | None = None
        self._guard_users = 0
        self._tokens: OrderedDict[str, float] = OrderedDict()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0
        self._processed = 0
        self._work_total = 0
        self._state: dict[str, Any] = {
            "state": "idle",
            "percent_done": 0.0,
            "eta_seconds": None,
            "last_error": None,
            "old_count": 0,
            "new_count": 0,
            "all_mail_count": 0,
            "run_id": None,
            "swap_outcome": "not_attempted",
            "swap_reason": None,
        }
        self._recover_interrupted()
        self._restore_status()

    def _acquire_guard(self) -> None:
        with self._guard_lock:
            if self._guard is None:
                self._guard = self.client.search_index.acquire_reindex_guard()
            self._guard_users += 1

    def _release_guard(self) -> None:
        with self._guard_lock:
            self._guard_users -= 1
            if self._guard_users == 0:
                os.close(self._guard)
                self._guard = None

    def _pending(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        completed = {
            row.get("action_log_id") for row in records if row.get("action") == "reindex_result"
        }
        index = self.client.search_index
        pending = []
        for row in records:
            run_id = row.get("run_id")
            if (
                row.get("action") != "reindex_execute"
                or row.get("state") != "running"
                or row.get("log_id") in completed
                or not isinstance(run_id, str)
                or len(run_id) != 32
                or any(char not in "0123456789abcdef" for char in run_id)
                or row.get("index_key") != index.path.name
            ):
                continue
            pending.append(row)
        return pending

    def _recover_interrupted(self) -> None:
        """Best-effort startup recovery, excluded by a peer's account run lease."""
        index = self.client.search_index
        guard = None
        try:
            try:
                with self.log.transaction(readonly=True) as transaction:
                    pending = self._pending(transaction.records())
            except MailError:
                pending = []
            if not pending and not index.path.parent.exists():
                return
            guard = index.acquire_reindex_guard()
            _, busy = index.sweep_candidates()
            if not pending:
                return
            with self.log.transaction() as transaction:
                for intent in self._pending(transaction.records()):
                    if intent["run_id"] in busy:
                        continue
                    record = new_record("reindex_result", {})
                    record.update(
                        action_log_id=intent["log_id"],
                        run_id=intent["run_id"],
                        index_key=index.path.name,
                        client=intent.get("client"),
                        state="interrupted",
                        last_error="interrupted",
                        swap_outcome="unknown",
                        swap_reason="interrupted",
                    )
                    transaction.append(record)
        except (*_ERRORS, MailError):
            # Recovery must never prevent ordinary read-only mail access.
            pass
        finally:
            if guard is not None:
                os.close(guard)

    def _restore_status(self) -> None:
        """Recover the last account run, preserving that run's counter meaning."""
        try:
            with self.log.transaction(readonly=True) as transaction:
                records = transaction.records()
        except MailError:
            return
        results = {
            row.get("action_log_id"): row
            for row in records
            if row.get("action") == "reindex_result"
        }
        # Append order of terminal records can differ from run start order when
        # an older worker is still recording its result as the next run starts.
        for intent in reversed(records):
            if not self._pending([intent]):
                continue
            result = results.get(intent.get("log_id"))
            if result is None or result.get("run_id") != intent["run_id"]:
                continue
            state = result.get("state")
            if state not in {"succeeded", "failed", "cancelled", "interrupted"}:
                continue
            for key in self._state:
                if key in result:
                    self._state[key] = result[key]
            if state == "interrupted":
                self._state.update(state="failed", run_id=intent["run_id"])
            # Interrupted recovery records lack counters. The live
            # cache remains useful evidence, without fabricating candidate work.
            counts = self.client.search_index.reporting_counts()
            if counts is not None:
                for key in ("old_count", "new_count"):
                    if key not in result:
                        self._state[key] = counts["indexed_count"]
                if "all_mail_count" not in result:
                    self._state["all_mail_count"] = counts["all_mail_count"]
            return

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state["state"] == "running"

    def status(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._state)
            if result["state"] == "running":
                total = self._work_total
                result["percent_done"] = min(
                    99.9, round(100 * self._processed / total, 1) if total else 0.0
                )
                elapsed = time.monotonic() - self._started
                if self._processed and elapsed > 0:
                    result["eta_seconds"] = round(
                        max(0, total - self._processed) * elapsed / self._processed, 1
                    )
        full_run = {key: result.pop(key) for key in ("old_count", "new_count", "all_mail_count")}
        result["last_full_run"] = full_run if result["run_id"] is not None else None
        result.update(
            indexed_count=None,
            indexed_max_uid=None,
            all_mail_count=None,
            all_mail_max_uid=None,
            all_mail_observed_at=None,
            last_catchup_at=None,
            last_expunge_reconciliation_at=None,
        )
        counts = self.client.search_index.reporting_status()
        if counts is not None:
            result.update(counts)
        return result

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def _estimate(self, count: int) -> float:
        batches = math.ceil(count / self.client.settings.reindex_batch_size)
        return round(batches * (1.0 + self.client.settings.reindex_batch_sleep), 1)

    @staticmethod
    def _all_mail(connection: Any) -> str:
        attributes, _, _ = canonical.mailbox_roles(connection)
        choices = [
            folder
            for folder, flags in attributes.items()
            if "\\all" in flags and "\\noselect" not in flags
        ]
        if len(choices) != 1:
            raise MailValidationError("Reindex requires one unambiguous All Mail mailbox")
        return choices[0]

    def _prune_tokens(self, now: float) -> None:
        for digest, expiry in list(self._tokens.items()):
            if expiry <= now:
                self._tokens.pop(digest, None)
        while len(self._tokens) >= _MAX_TOKENS:
            self._tokens.popitem(last=False)

    def preview(self) -> dict[str, Any]:
        with self._control:
            with self._lock:
                if self._state["state"] == "running":
                    return {
                        "state": "running",
                        "run_id": self._state["run_id"],
                        "error": "already running",
                    }
            return self._preview()

    def _preview(self) -> dict[str, Any]:
        with self.client._session() as (connection, _):
            # Preview may inspect coverage but must never invalidate the old DB.
            connection._search_index = None
            folder = self._all_mail(connection)
            inventory = self.client._select(connection, folder)
            validity = _uidvalidity(connection)
            uids = self.client._search(connection, ["ALL"])
            if len(uids) != len(set(uids)) or len(uids) != inventory["exists"]:
                raise MailValidationError("All Mail inventory changed; preview the reindex again")
            _, real, _ = canonical.mailbox_roles(connection)
            work_count = len(uids) + sum(
                _coverage_status(connection, name)[1] for name in sorted(real)
            )
        known = self.client.search_index.known_uids(folder, validity) or set()
        indexed = len(known.intersection(uids))
        token = secrets.token_urlsafe(32)
        digest = token_digest(token)
        record = new_record("reindex_preview", {})
        record.update(
            token_digest=digest,
            indexed_count=indexed,
            all_mail_count=len(uids),
            missing_count=len(uids) - indexed,
            eta_seconds=self._estimate(work_count),
            eta_is_estimate=True,
            state="previewed",
        )
        self.log.audit(record)
        with self._lock:
            now = time.monotonic()
            self._prune_tokens(now)
            self._tokens[digest] = now + _TOKEN_TTL
        return {
            "log_id": record["log_id"],
            "token": token,
            "ttl_seconds": _TOKEN_TTL,
            "indexed_count": indexed,
            "all_mail_count": len(uids),
            "missing_count": len(uids) - indexed,
            "eta_seconds": record["eta_seconds"],
            "eta_is_estimate": True,
        }

    def execute(self, token: str) -> dict[str, Any]:
        with self._control:
            with self._lock:
                active = self._state["state"] == "running"
                current_run = self._state["run_id"]
            if active:
                record = new_record("reindex_execute", {})
                record.update(state="refused", run_id=current_run, error="already running")
                self.log.audit(record)
                return {
                    "error": "already running",
                    "state": "running",
                    "run_id": current_run,
                    "log_id": record["log_id"],
                    "token_consumed": False,
                }
            if not isinstance(token, str) or not token.isascii() or len(token) != 43:
                raise MailValidationError("Reindex requires a valid, unused preview token")
            digest = token_digest(token)
            with self._lock:
                expiry = self._tokens.get(digest)
            if expiry is None or expiry <= time.monotonic():
                raise MailValidationError(
                    "Reindex preview token is invalid, expired or already used"
                )
            run_id = secrets.token_hex(16)
            try:
                self._acquire_guard()
            except BlockingIOError:
                with self.log.transaction(readonly=True) as transaction:
                    pending = self._pending(transaction.records())
                return {
                    "error": "already running",
                    "state": "running",
                    "run_id": pending[-1]["run_id"] if pending else None,
                    "token_consumed": False,
                }
            record = new_record("reindex_execute", {})
            record.update(
                run_id=run_id,
                state="running",
                token_digest=digest,
                index_key=self.client.search_index.path.name,
            )
            try:
                self.log.audit(record)
            except Exception:
                self._release_guard()
                raise
            with self._lock:
                self._tokens.pop(digest, None)
                self._cancel = threading.Event()
                self._started = time.monotonic()
                self._processed = 0
                self._work_total = 0
                self._state.update(
                    state="running",
                    percent_done=0.0,
                    eta_seconds=None,
                    last_error=None,
                    old_count=0,
                    new_count=0,
                    all_mail_count=0,
                    run_id=run_id,
                    swap_outcome="pending",
                    swap_reason=None,
                )
            self._thread = threading.Thread(
                target=self._worker, args=(run_id, record), name="search-index-rebuild", daemon=True
            )
            try:
                self._thread.start()
            except Exception:
                self._update(
                    state="failed",
                    last_error="worker_start_failed",
                    swap_outcome="kept_old",
                    swap_reason="worker_start_failed",
                )
                self._terminal_record(record)
                self._release_guard()
                raise MailValidationError("Reindex worker could not be started") from None
            return {
                "log_id": record["log_id"],
                "run_id": run_id,
                "state": "running",
                "token_consumed": True,
            }

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self._control:
            with self._lock:
                active = self._state["state"] == "running"
                current_run = self._state["run_id"]
            if not isinstance(run_id, str) or run_id != current_run or not active:
                raise MailValidationError("Reindex run is not running")
            record = new_record("reindex_cancel", {})
            record.update(run_id=run_id, state="running", cancel_requested=True)
            self.log.audit(record)
            # The worker checks this before each batch and under _control before
            # replacement. An in-flight read finishes within its ordinary timeout.
            self._cancel.set()
            return {
                "log_id": record["log_id"],
                "run_id": run_id,
                "state": "running",
                "cancel_requested": True,
            }

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise _Stopped

    def _terminal_record(
        self, intent: dict[str, Any], snapshot: dict[str, Any] | None = None
    ) -> None:
        record = new_record("reindex_result", {})
        record.update(snapshot if snapshot is not None else self.status())
        # Keep the existing action-log format and restoration of full-run
        # counters, independently of the current serving index's counters.
        full_run = record.pop("last_full_run")
        if full_run is not None:
            record.update(full_run)
        record.update(
            action_log_id=intent["log_id"],
            client=intent["client"],
            index_key=self.client.search_index.path.name,
        )
        # Failure to append the outcome must never undo a completed replacement.
        try:
            self.log.audit(record)
        except Exception:
            with self._lock:
                if self._state["run_id"] == intent["run_id"]:
                    self._state["last_error"] = "outcome_log_unavailable"

    def _worker(self, run_id: str, intent: dict[str, Any]) -> None:
        candidate = None
        terminal = "failed"
        reason = "rebuild_failed"
        snapshot = None
        try:
            self._check_cancel()
            candidate = self.client.search_index.create_candidate(self.client.settings, run_id)
            if not candidate.enabled or not candidate._initialized:
                raise _ReindexFailure("candidate_unavailable")
            deadline = self.client._deadline()

            def rearm() -> None:
                self._check_cancel()
                deadline.end = time.monotonic() + self.client.settings.call_timeout

            with self.client._session(deadline=deadline) as (connection, _):
                connection._search_index = candidate
                rearm()
                folder = self._all_mail(connection)
                rearm()
                _, real, _ = canonical.mailbox_roles(connection)
                folder_counts = {}
                for name in sorted(real, key=lambda name: (name.upper() != "INBOX", name)):
                    rearm()
                    folder_counts[name] = _coverage_status(connection, name)[1]
                rearm()
                inventory = self.client._select(connection, folder)
                validity = _uidvalidity(connection)
                rearm()
                uids = self.client._search(connection, ["ALL"])
                if len(uids) != len(set(uids)) or len(uids) != inventory["exists"]:
                    raise _ReindexFailure("incomplete_inventory")
                old_known = self.client.search_index.known_uids(folder, validity) or set()
                with self._lock:
                    self._work_total = len(uids) + sum(folder_counts.values())
                self._update(
                    old_count=len(old_known.intersection(uids)),
                    all_mail_count=len(uids),
                    eta_seconds=self._estimate(self._work_total),
                )

                def warm(name: str, epoch: str, members: list[str]) -> None:
                    iterator = iter(
                        self.client._index_header_batches(
                            connection,
                            members,
                            deadline=deadline,
                            batch_size=self.client.settings.reindex_batch_size,
                        )
                    )
                    batches = math.ceil(len(members) / self.client.settings.reindex_batch_size)
                    indexed = 0
                    for batch in range(batches):
                        rearm()
                        next(iterator)
                        if not candidate.enabled or candidate.reason is not None:
                            raise _ReindexFailure("candidate_write_failed")
                        start = batch * self.client.settings.reindex_batch_size
                        batch_uids = members[
                            start : start + self.client.settings.reindex_batch_size
                        ]
                        count = candidate.known_count(name, epoch, uids=batch_uids)
                        if count is None:
                            raise _ReindexFailure("candidate_read_failed")
                        indexed += count
                        with self._lock:
                            self._processed += len(batch_uids)
                            self._state["percent_done"] = min(
                                99.9, round(100 * self._processed / self._work_total, 1)
                            )
                            if name == folder:
                                self._state["new_count"] = indexed
                        if batch + 1 < batches and self._cancel.wait(
                            self.client.settings.reindex_batch_sleep
                        ):
                            raise _Stopped

                warm(folder, validity, uids)
                # Every mailbox has its own UID space. Warm real-folder handles
                # directly, so rebuilding All Mail also accelerates default
                # INBOX searches without guessing cross-folder message identity.
                for name, expected_count in folder_counts.items():
                    rearm()
                    state = self.client._select(connection, name)
                    epoch = _uidvalidity(connection)
                    rearm()
                    members = self.client._search(connection, ["ALL"])
                    if len(members) != len(set(members)) or len(members) != state["exists"]:
                        raise _ReindexFailure("incomplete_inventory")
                    with self._lock:
                        self._work_total += len(members) - expected_count
                    if (
                        members
                        and self._processed
                        and self._cancel.wait(self.client.settings.reindex_batch_sleep)
                    ):
                        raise _Stopped
                    warm(name, epoch, members)
                    rearm()
                    state = self.client._select(connection, name)
                    if _uidvalidity(connection) != epoch:
                        raise _ReindexFailure("uidvalidity_changed")
                    rearm()
                    current_members = self.client._search(connection, ["ALL"])
                    if (
                        _uidvalidity(connection) != epoch
                        or len(current_members) != len(set(current_members))
                        or len(current_members) != state["exists"]
                    ):
                        raise _ReindexFailure("incomplete_inventory")
                    candidate.reconcile(name, epoch, current_members)
                    candidate.record_coverage(name, epoch, state, current_members, account=False)
                    if not candidate.enabled or candidate.reason is not None:
                        raise _ReindexFailure("candidate_write_failed")
                # Compare with a fresh epoch and UID inventory, never a stale
                # starting count or rows for messages expunged during the run.
                rearm()
                inventory = self.client._select(connection, folder)
                if _uidvalidity(connection) != validity:
                    raise _ReindexFailure("uidvalidity_changed")
                rearm()
                current = self.client._search(connection, ["ALL"])
                if _uidvalidity(connection) != validity:
                    raise _ReindexFailure("uidvalidity_changed")
                if len(current) != len(set(current)) or len(current) != inventory["exists"]:
                    raise _ReindexFailure("incomplete_inventory")
                candidate.reconcile(folder, validity, current)
                known = candidate.known_uids(folder, validity)
                if known is None or not candidate.enabled or candidate.reason is not None:
                    raise _ReindexFailure("candidate_read_failed")
                new_count = len(known.intersection(current))
                candidate.record_coverage(folder, validity, inventory, current, known=known)
                self._update(new_count=new_count, all_mail_count=len(current))
                rearm()
            self._check_cancel()
            if new_count * 100 < len(current) * 99:
                raise _ReindexFailure("coverage_below_99_percent")
            swap_deadline = time.monotonic() + min(self.client.settings.timeout, _SWAP_WAIT_SECONDS)
            while True:
                with self._control:
                    self._check_cancel()
                    try:
                        self.client.search_index.replace_with(candidate)
                    except BlockingIOError:
                        pass
                    else:
                        terminal, reason = "succeeded", None
                        # Publish completion before releasing the cancel/swap lock.
                        self._update(
                            state=terminal,
                            percent_done=100.0,
                            eta_seconds=0.0,
                            swap_outcome="swapped",
                            swap_reason=None,
                        )
                        snapshot = self.status()
                        break
                remaining = swap_deadline - time.monotonic()
                if remaining <= 0:
                    raise _ReindexFailure("swap_busy")
                if self._cancel.wait(min(0.05, remaining)):
                    raise _Stopped
        except Exception as error:
            if self._cancel.is_set() or isinstance(error, _Stopped):
                terminal, reason = "cancelled", "cancelled"
            elif isinstance(error, _ReindexFailure):
                reason = error.args[0]
            elif isinstance(error, CallDeadlineExceeded):
                reason = "imap_timeout"
            elif isinstance(error, BlockingIOError):
                reason = "index_busy"
            elif isinstance(error, _Unavailable):
                # SearchIndex's reasons are fixed internal codes, never paths.
                reason = error.args[0]
        finally:
            if candidate is not None:
                try:
                    candidate.discard()
                except FileNotFoundError:
                    pass
                except Exception:
                    if terminal != "succeeded":
                        reason = "candidate_cleanup_failed"
            if terminal != "succeeded":
                with self._control:
                    self._update(
                        state=terminal,
                        eta_seconds=None,
                        last_error=None if terminal == "cancelled" else reason,
                        swap_outcome="kept_old",
                        swap_reason=reason,
                    )
                    snapshot = self.status()
            try:
                self._terminal_record(intent, snapshot)
            finally:
                if candidate is not None:
                    candidate.release_candidate()
                self._release_guard()
