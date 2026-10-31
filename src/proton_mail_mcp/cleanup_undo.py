"""Replay durable undo outcomes without authorizing another mailbox identity."""

from __future__ import annotations

import re

from .mail import MailValidationError


def remaining_undo(original: dict, records: list[dict]) -> tuple[dict, list[dict]]:
    """Retry only entries durably reported as never attempted.

    A claim without its outcome may have changed mail. Missing identities and
    uncertain multi-step operations also require inspection, not another MOVE.
    Older journals lack the bindings needed to retry safely and fail closed.
    An attempt whose preparation ran out of time claims only a nonempty prefix
    of the pending entries; the unclaimed rest stays pending, unchanged.
    """
    pending = {
        index: dict(entry, original_index=index)
        for index, entry in enumerate(original["messages"])
        if entry["status"] != "not_attempted"
    }
    unresolved = {}
    history = [record for record in records if record.get("undo_of") == original["log_id"]]

    def invalid():
        raise MailValidationError("Undo has unresolved journal outcomes; inspect before retrying")

    if len(history) % 2:
        invalid()
    for offset in range(0, len(history), 2):
        claim, outcome = history[offset : offset + 2]
        if (
            claim.get("action") != "undo_claim"
            or outcome.get("action") != "undo_action"
            or not isinstance(claim.get("outcome_log_id"), str)
            or claim["outcome_log_id"] != outcome.get("log_id")
        ):
            invalid()
        if outcome.get("undo_version") != 1:
            if outcome.get("partial") is False:
                raise MailValidationError("Cleanup action has already been undone")
            invalid()
        indices = claim.get("original_indices")
        if (
            not isinstance(indices, list)
            or any(type(index) is not int for index in indices)
            or (pending and not indices)
            or indices != list(pending)[: len(indices)]
            or not isinstance(outcome.get("messages"), list)
        ):
            invalid()
        claimed = set(indices)
        entries = {}
        for entry in outcome["messages"]:
            if not isinstance(entry, dict):
                invalid()
            index = entry.get("original_index")
            if type(index) is not int or index not in claimed or index in entries:
                invalid()
            previous = pending[index]
            sources = {previous["destination_folder"]}
            if original["action"] == "remove_label" or "seen_before" in previous:
                sources.add(previous["source_folder"])
            target = (
                previous["destination_folder"]
                if original["action"] == "add_label"
                else previous["source_folder"]
            )
            if (
                entry.get("message_id") != previous["message_id"]
                or entry.get("source_folder") not in sources
                or entry.get("folder") != entry["source_folder"]
                or entry.get("destination_folder") != target
                or any(
                    not isinstance(entry.get(key), str)
                    or not re.fullmatch(r"[1-9][0-9]*", entry[key])
                    for key in ("uid_before", "uidvalidity_before")
                )
                or not isinstance(entry.get("header_digest"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["header_digest"])
                or entry.get("status")
                not in {"done", "not_attempted", "uncertain", "moved", "flagged"}
            ):
                invalid()
            entries[index] = entry
        next_pending = {}
        for index, previous in pending.items():
            if index not in claimed:
                # Never examined by this attempt; nothing about it changed.
                next_pending[index] = previous
                continue
            entry = entries.get(index)
            status = entry["status"] if entry else "missing"
            if status == "not_attempted":
                # Keep the first attempt's selected handle, including when the
                # original execute record had only a header-digest fallback.
                next_pending[index] = dict(previous, undo_retry_handle=entry)
            elif status != "done":
                unresolved[index] = {
                    "original_index": index,
                    "message_id": previous["message_id"],
                    "status": status,
                    "outcome_log_id": outcome["log_id"],
                }
        pending = next_pending
    if history and not pending:
        if unresolved:
            raise MailValidationError("Undo has unresolved messages; inspect logged outcomes")
        raise MailValidationError("Cleanup action has already been undone")
    return dict(original, messages=list(pending.values())), list(unresolved.values())
