"""Fold approval-wide progress from durable per-chunk execution evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .config import APPROVAL_MAX_CEILING
from .mail import MailError

RUN_EVENTS = {"run_start", "run_resume", "run_progress", "run_end"}


def run_event(event: str, run_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "action": event,
        "run_id": run_id,
        "timestamp": datetime.now(UTC).isoformat(),
        **fields,
    }


def replay_runs(
    records: list[dict[str, Any]], chunks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """A missing call end is interrupted, even when its last chunk completed."""
    runs: dict[str, dict[str, Any]] = {}
    for event in records:
        if event.get("action") not in RUN_EVENTS:
            continue
        run_id = event.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise MailError("Cleanup run journal has invalid run identity")
        if event["action"] == "run_start":
            if (
                run_id in runs
                or type(event.get("approved_total")) is not int
                # Bounded approvals continue past one 200-message chunk.
                or not 1 <= event["approved_total"] <= APPROVAL_MAX_CEILING
                or not isinstance(event.get("token_digest"), str)
            ):
                raise MailError("Cleanup run journal has invalid approval")
            runs[run_id] = {
                **event,
                "action": "execute_run",
                "status": "interrupted",
                "blocked": 0,
            }
        else:
            run = runs.get(run_id)
            if run is None:
                raise MailError("Cleanup run journal has missing start")
            if event["action"] == "run_end":
                run["status"] = "ended"
            elif event["action"] == "run_resume":
                run["status"] = "interrupted"
            blocked = event.get("blocked", run["blocked"])
            if type(blocked) is not int or not 0 <= blocked <= run["approved_total"]:
                raise MailError("Cleanup run journal has invalid blocked count")
            run["blocked"] = blocked
            if "stop_reason" in event:
                run["stop_reason"] = event["stop_reason"]
            run["updated_at"] = event["timestamp"]
    for run in runs.values():
        matching = [chunk for chunk in chunks if chunk.get("run_id") == run["run_id"]]
        done = uncertain = 0
        indexes = set()
        for chunk in matching:
            index = chunk.get("chunk_index")
            if (
                chunk.get("approved_total") != run["approved_total"]
                or type(index) is not int
                or index < 0
                or index in indexes
            ):
                raise MailError("Cleanup run journal has invalid chunk identity")
            indexes.add(index)
            done += sum(row.get("status") == "done" for row in chunk["messages"])
            uncertain += sum(
                row.get("status") not in {"done", "not_attempted"} for row in chunk["messages"]
            )
        remaining = run["approved_total"] - done - uncertain
        if remaining < 0 or run["blocked"] > remaining:
            raise MailError("Cleanup run journal exceeds its approved set")
        if run["status"] == "ended":
            run["status"] = (
                "completed"
                if done == run["approved_total"]
                and not any(chunk.get("partial") for chunk in matching)
                else "partial"
            )
        run.update(
            done=done,
            uncertain=uncertain,
            remaining=remaining,
            remaining_unchanged=remaining,
            log_ids=[chunk["log_id"] for chunk in matching],
            next_chunk_index=max(indexes, default=-1) + 1,
            partial=run["status"] != "completed",
        )
    return list(runs.values())
