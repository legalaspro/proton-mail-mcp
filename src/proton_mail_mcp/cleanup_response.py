"""Compact public cleanup responses without changing internal or durable evidence."""

from typing import Any


def cleanup_response(result: dict[str, Any], *, verbose: bool = False) -> dict[str, Any]:
    """Keep journal rows internal unless the caller explicitly requests details."""
    response = dict(result)
    response["message_ids"] = [row["message_id"] for row in result.get("messages", [])]
    response["available"] = bool(result.get("continuation_token"))
    if not response["available"] and result.get("remaining", 0):
        response.setdefault(
            "continuation_reason",
            result.get("stop_reason") or "Continuation unavailable; request a new preview",
        )
    if not verbose:
        response.pop("messages", None)
        for key in ("blocked", "missing", "mismatched"):
            rows = response.pop(key, [])
            response[key + "_ids"] = [row["message_id"] for row in rows if "message_id" in row]
    return response
