"""Shared, local progress reporting for replies using incomplete indexes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def progress_fields(
    indexed_count: int | None,
    target: int | None,
    *,
    eta_seconds: float | None = None,
    active: bool = False,
) -> dict[str, Any]:
    """Use message totals, never sparse UID maxima, for progress arithmetic."""
    indexed = max(0, indexed_count or 0)
    total = max(0, target) if target is not None else None
    if total is None:
        percent = None
        eta_seconds = None
    else:
        percent = min(99.9, round(100 * indexed / total, 1)) if indexed < total else 100.0
        if active and (indexed >= total or eta_seconds == 0):
            # All Mail coverage can finish before real-folder warming does.
            eta_seconds = None
        elif indexed >= total:
            eta_seconds = 0.0
    return {
        "indexed_count": indexed,
        "target": total,
        "percent": percent,
        "eta_seconds": eta_seconds,
    }


def recovery_state(client: Any, worker: dict[str, Any]) -> str:
    """Describe incomplete coverage without inventing background activity."""
    state = worker.get("state")
    if state in {"failed", "partial"}:
        manual = getattr(client, "reindex", None)
        if manual is None or getattr(client.index_autorebuild, "manual_run_id", None) == (
            manual.status().get("run_id")
        ):
            return state
        # A later manual run supersedes a worker's historical terminal outcome.
    # Storage may fail before the worker publishes its terminal outcome.
    if not getattr(client.search_index, "enabled", True):
        return "unavailable"
    if state in {"queued", "running"}:
        return "rebuilding"
    if state == "disabled":
        return "disabled"
    return "incomplete"


@dataclass
class RebuildRate:
    """Estimate from newly observed work, excluding headers cached before startup."""

    _first_count: int | None = None
    _first_at: float | None = None
    _last_count: int | None = None
    _target: int | None = None
    _rate: float | None = None

    def observe(self, indexed_count: int, target: int | None, now: float) -> dict[str, Any]:
        if self._last_count is None or indexed_count < self._last_count or target != self._target:
            self._first_count = indexed_count
            self._first_at = now
            self._rate = None
        elif (
            self._first_count is not None
            and self._first_at is not None
            and indexed_count > self._first_count
            and now > self._first_at
        ):
            self._rate = (indexed_count - self._first_count) / (now - self._first_at)
        self._last_count = indexed_count
        self._target = target
        eta = (
            round(max(0, target - indexed_count) / self._rate, 1)
            if target is not None and self._rate
            else None
        )
        return progress_fields(indexed_count, target, eta_seconds=eta)


def attach_index_status(
    client: Any,
    result: dict[str, Any],
    *,
    depends_on_index: bool,
    incomplete: bool,
    indexed_count: int | None = None,
    target: int | None = None,
) -> dict[str, Any]:
    """Annotate affected replies using only nonblocking local status snapshots."""
    if not depends_on_index or not incomplete:
        return result
    specific = indexed_count is not None or target is not None
    counts = (
        client.search_index.reporting_status() or {}
        if indexed_count is None or target is None
        else {}
    )
    worker = client.index_autorebuild.status()
    state = recovery_state(client, worker)
    active = state == "rebuilding"
    if indexed_count is None:
        indexed_count = counts.get("indexed_count")
        if indexed_count is None and active:
            indexed_count = worker.get("indexed_count")
    if target is None:
        target = counts.get("all_mail_count")
        if target is None and active:
            target = worker.get("target")
    # Folder-specific coverage has no corresponding worker rate estimate.
    eta = worker.get("eta_seconds") if active and not specific else None
    progress = progress_fields(
        indexed_count,
        target,
        eta_seconds=eta,
        active=active,
    )
    if not active:
        progress["eta_seconds"] = None
    result["index_status"] = {"state": state, **progress}
    if state in {"failed", "partial"} and worker.get("last_error"):
        result["index_status"]["last_error"] = worker["last_error"]
    indexed = progress["indexed_count"]
    if progress["target"] is None:
        detail = f"{indexed} messages indexed (target unknown)"
    else:
        detail = f"{indexed}/{progress['target']} messages indexed ({progress['percent']:g}%)"
    label = {
        "disabled": "search index incomplete (automatic recovery disabled)",
        "failed": "search index recovery failed",
        "partial": "search index recovery incomplete",
    }.get(state, f"search index {state}")
    result["index_note"] = f"{label}: {detail}; results may be incomplete"
    return result
