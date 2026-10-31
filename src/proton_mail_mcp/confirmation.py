"""Require a fresh host-mediated user decision, outside tool arguments."""

import json
import logging
import re
import weakref
from collections import Counter
from collections.abc import Collection
from contextlib import suppress
from typing import Any

import anyio
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_REQUEST, METHOD_NOT_FOUND, ErrorData
from pydantic import BaseModel, ConfigDict, Field

from .mail import MailValidationError

_LOGGER = logging.getLogger(__name__)
# The server's root logger stays at WARNING. These lines must still reach the service log.
_LOGGER.setLevel(logging.INFO)
# A quoted local part may hold spaces. Masking runs before the length cut, so a cut cannot hide
# the @, and trying only the start of each run keeps a long name linear.
_ADDRESS = re.compile(r'"[^"]*"@\S*|(?<![^\s@])[^\s@]+@\S*')
_LOGGED_SESSIONS: weakref.WeakSet[Any] = weakref.WeakSet()
_TARGETS = {"move_to_trash": "Trash", "archive": "Archive"}
# Why a form that was due never gave a person's answer. A decline or cancel needs no reason.
_NO_ANSWER = {
    "confirmation_timeout": "the client's form got no answer in time",
    "confirmation_unavailable": "this client did not show a form",
    "confirmation_error": "the client's form failed",
}
_LOG_REASONS = {
    "confirmation_timeout": "timeout",
    "confirmation_unavailable": "unsupported",
    "confirmation_declined": "decline",
    "confirmation_cancelled": "cancel",
    "confirmation_error": "error",
}


class ConfirmationError(MailValidationError):
    def __init__(self, result: str):
        self.result = result
        _LOGGER.info("Confirmation not accepted: reason %s", _LOG_REASONS.get(result, "error"))
        reason = _NO_ANSWER.get(result)
        super().__init__(
            f"User confirmation was not accepted: {reason}. Nothing was changed, and no other "
            "kind of confirmation applies; request a fresh preview to try again"
            if reason
            else "User confirmation was not accepted; request a fresh preview"
        )


class _Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    confirm: bool = Field(strict=True, description="I approve this exact action")


def log_safe(value: Any) -> str:
    """One self-reported clientInfo value as a JSON string, with no address in it."""
    return json.dumps(_ADDRESS.sub("[address]", value)[:100] if isinstance(value, str) else None)


def _client_label(ctx: Any) -> str:
    """The self-reported clientInfo as one log-safe line, with no address in it."""
    name = version = None
    with suppress(Exception):
        info = ctx.session.client_params.clientInfo
        name, version = info.name, info.version
    return f"name {log_safe(name)}, version {log_safe(version)}"


def log_session_client(ctx: Any) -> None:
    """Log who this session's client says it is, once. Never fails the call it runs in."""
    with suppress(Exception):  # Stateless HTTP has no initialized session.
        session = ctx.session
        if session.client_params is None or session in _LOGGED_SESSIONS:
            return
        _LOGGED_SESSIONS.add(session)
        declared = session.client_params.capabilities.elicitation is not None
        _LOGGER.info("MCP client %s, elicitation declared: %s", _client_label(ctx), declared)


def _listed(ctx: Any, no_form_clients: Collection[str]) -> bool:
    """Whether the operator listed this session's self-reported name; unknown counts as no."""
    if not no_form_clients:
        return False
    try:
        return ctx.session.client_params.clientInfo.name.strip().casefold() in no_form_clients
    except Exception:
        return False


def form_available(ctx: Any, no_form_clients: Collection[str] = ()) -> bool:
    """Whether this session's client can show a form; unknown counts as no.

    The MCP specification reads an empty elicitation capability as form support. A client
    the operator listed as having no form never gets one, whatever it declares.
    """
    if _listed(ctx, no_form_clients):
        return False
    try:
        params = ctx.session.client_params
        capability = params.capabilities.elicitation if params is not None else None
        return capability is not None and (capability.form is not None or capability.url is None)
    except Exception:
        return False


def declares_no_form(ctx: Any, no_form_clients: Collection[str] = ()) -> bool:
    """Whether initialize left elicitation out or the operator listed it; unknown counts as no.

    Only this opens the typed OK. A client that declares elicitation, in any mode, stays on
    the form, which refuses a client that cannot show one, unless the operator listed it.
    """
    try:
        capability = ctx.session.client_params.capabilities.elicitation
    except Exception:
        return False
    return capability is None or _listed(ctx, no_form_clients)


def log_list_use(ctx: Any, no_form_clients: Collection[str]) -> None:
    """Log a typed OK that only the operator's list opened. Call once that approval is chosen."""
    with suppress(Exception):
        declared = ctx.session.client_params.capabilities.elicitation is not None
        if declared and _listed(ctx, no_form_clients):
            _LOGGER.info(
                "Client %s is on PROTON_MCP_SEND_TYPED_OK_CLIENTS: typed OK instead of the form",
                _client_label(ctx),
            )


def _says_no_form(error: ErrorData) -> bool:
    """How a host, not a person, says it has no form: no such method, or the SDK's "Elicitation
    not supported". A person's answer is always an action, and silence is a timeout.
    """
    return error.code == METHOD_NOT_FOUND or (
        error.code == INVALID_REQUEST and "not supported" in error.message.lower()
    )


async def require_confirmation(
    ctx, summary: str, *, timeout: float, no_form_clients: Collection[str] = ()
) -> None:
    """Trust only a host's form response; unsupported hosts and failures refuse."""
    if not form_available(ctx, no_form_clients):
        raise ConfirmationError("confirmation_unavailable")
    try:
        with anyio.fail_after(timeout):
            response = await ctx.elicit(message=summary, schema=_Confirmation)
    except TimeoutError:
        raise ConfirmationError("confirmation_timeout") from None
    except McpError as error:
        # This only names the refusal. The session keeps its capability, so it stays on the form.
        raise ConfirmationError(
            "confirmation_unavailable" if _says_no_form(error.error) else "confirmation_error"
        ) from None
    except Exception:
        raise ConfirmationError("confirmation_error") from None
    action = getattr(response, "action", None)
    if action == "decline":
        raise ConfirmationError("confirmation_declined")
    if action == "cancel":
        raise ConfirmationError("confirmation_cancelled")
    if action != "accept" or getattr(response, "data", None) is None:
        raise ConfirmationError("confirmation_error")
    if getattr(response.data, "confirm", None) is not True:
        raise ConfirmationError("confirmation_declined")


def describe_approval(cleanup: Any, action: str, token: str) -> dict[str, Any]:
    """Summarize the stored approval a token names; the model supplies only the token."""
    from .cleanup_lookup import display_row

    grant = cleanup.tokens.describe(token, action)
    summary: dict[str, Any] = {
        "action": action,
        "action_hash": grant.action_hash,
        "confirmed": grant.confirmed,
        "expires_at": grant.expires_at,
        "count": len(grant.messages),
        "approved_total": grant.approved_total or len(grant.messages),
    }
    if isinstance(grant.selector.get("max_messages"), int):
        # A bounded approval continues server-side, in chunks of at most 200.
        summary["approval_max_messages"] = grant.selector["max_messages"]
    params = grant.params
    if action == "create_folder":
        summary["name"] = params["name"]
    elif action == "delete_draft":
        summary["folder"] = params["folder"]
    elif action == "create_sieve_filter":
        # The person approves the exact stored script, not a description of it.
        for key in ("name", "folder", "match", "conditions", "mark_read", "backend"):
            summary[key] = params.get(key)
        summary["script_sha256"] = params.get("script_sha256")
        summary["script"] = params.get("script")
    elif action == "add_rule":
        # The person approves the exact rule to save; its Sieve script is informational.
        for key in ("name", "folder", "match", "conditions", "mark_read", "backend"):
            summary[key] = params.get(key)
        summary["rule_sha256"] = params.get("rule_sha256")
    elif action == "unsubscribe":
        from .unsubscribe import preview_target

        targets = Counter(
            (row["unsubscribe_target"]["method"], row["unsubscribe_target"]["host"])
            for row in grant.messages
        )
        summary["targets"] = [
            {"method": method, "host": host, "count": count}
            for (method, host), count in sorted(targets.items())
        ]
        drafts = [
            preview_target(row["unsubscribe_target"])
            for row in grant.messages
            if row["unsubscribe_target"]["method"] == "MAILTO"
        ]
        if drafts:
            summary["drafts"] = drafts
    elif action == "apply_rules":
        from .folder_rules import FolderRulesService

        rules, digest = FolderRulesService(cleanup).load()
        if digest != params.get("rules_digest"):
            raise MailValidationError("Folder rules changed after preview; preview again")
        counts = Counter(row.get("rule_index") for row in grant.messages)
        summary["folder"] = grant.selector.get("folder")
        summary["mark_read"] = params.get("mark_read") is True
        # Saved rules name themselves, their conditions and their own mark_read.
        summary["rules"] = [
            {"rule": index, **rule.report(), "target": rule.folder, "count": counts[index]}
            for index, rule in enumerate(rules, start=1)
            if counts[index]
        ]
    elif "label" in params:
        summary["target"] = "Labels/" + params["label"]
    elif action in {"mark_read", "mark_unread"}:
        summary["folders"] = sorted({row["folder"] for row in grant.messages})
    else:
        summary["target"] = _TARGETS.get(action, params.get("folder"))
        summary["mark_read"] = params.get("mark_read") is True
        if params.get("create_folder_if_missing") is True:
            # The same approval creates the destination when it is missing.
            summary["create_folder"] = params.get("folder")
    summary["samples"] = [
        {key: display_row(row).get(key) for key in ("from", "subject", "date", "folder")}
        for row in grant.messages[:5]
    ]
    return summary


def approval_text(summary: dict[str, Any]) -> str:
    """Render the exact approval for the host form, including its hash prefix."""
    action, count = summary["action"], summary["count"]
    marking = ", marking them read" if summary.get("mark_read") else ""
    if not marking and any(rule.get("mark_read") for rule in summary.get("rules", [])):
        marking = ", marking those of mark_read rules read"
    target = json.dumps(summary.get("target"), ensure_ascii=True)
    sentence = {
        "delete_draft": (
            f"Permanently delete {count} message(s) from Drafts "
            f"({json.dumps(summary.get('folder'), ensure_ascii=True)}); cannot be undone."
        ),
        "mark_read": f"Mark {count} message(s) as read.",
        "mark_unread": f"Mark {count} message(s) as unread.",
        "add_label": f"Add the label {target} to {count} message(s).",
        "remove_label": f"Remove the label {target} from {count} message(s).",
        "apply_rules": (
            f"Move {count} message(s) from "
            f"{json.dumps(summary.get('folder'), ensure_ascii=True)} by the reviewed "
            f"folder rules{marking}."
        ),
        "unsubscribe": (
            f"Prepare {len(summary['drafts'])} unsent mailto draft(s) for separate send approval; "
            "perform any approved HTTPS unsubscribe requests. HTTPS requests cannot be undone."
            if summary.get("drafts")
            else f"Send {count} unsubscribe request(s) over HTTPS; this cannot be undone."
        ),
        "create_folder": (
            f"Create the folder {json.dumps(summary.get('name'), ensure_ascii=True)}."
        ),
        "create_sieve_filter": (
            f"Create the Sieve filter {json.dumps(summary.get('name'), ensure_ascii=True)}, "
            "which files matching new mail into "
            f"{json.dumps(summary.get('folder'), ensure_ascii=True)}{marking}."
        ),
        "add_rule": (
            f"Save the folder rule {json.dumps(summary.get('name'), ensure_ascii=True)}: "
            "when rules run, matching INBOX mail moves to "
            f"{json.dumps(summary.get('folder'), ensure_ascii=True)}"
            + (" (apply_rules also marks it read)." if summary.get("mark_read") else ".")
        ),
    }.get(action, f"Move {count} message(s) to {target}{marking}.")
    if summary.get("create_folder"):
        sentence = f"Create the folder {target} if it is missing, then move {count} message(s) "
        sentence += f"to it{marking}."
    details = {key: value for key, value in summary.items() if key != "confirmed"}
    return (
        f"Approve exactly this mailbox change? {sentence} "
        f"Approval hash prefix: {summary['action_hash'][:16]}\n"
        + json.dumps(details, ensure_ascii=True, sort_keys=True)
    )
