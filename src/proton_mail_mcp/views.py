"""View models for the MCP Apps tools: what a view renders and what the model reads."""

from __future__ import annotations

import hashlib
import re
from email.utils import getaddresses
from typing import Any

from .attachments import ATTACHED_SIZE_NOTE, ENCODED_SIZE_NOTE, visible_name, visible_type
from .email_html import email_document, sanitize_email_html
from .mail import MailValidationError, _addresses, _HtmlText, _message_id
from .send_address import FORWARD_SUBJECT, is_bare_address
from .threading import MAX_THREAD_BODY_BYTES, MAX_THREAD_HEADER_BYTES, MAX_THREAD_SECONDS

# Messages whose HTML a thread view renders; older ones show their text.
MAX_HTML_MESSAGES = 20
# A Message-ID that a view may quote in a chat message: one dot-atom, no prose.
_PLAIN_ID = re.compile(r"<[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,200}@[A-Za-z0-9.-]{1,200}>")


def html_text(markup: str) -> str:
    """The text read_thread extracts from an HTML body."""
    parser = _HtmlText()
    parser.feed(markup)
    return "".join(parser.parts).strip()


def plain_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _PLAIN_ID.fullmatch(value) else None


def view_ref(item: dict[str, Any]) -> str:
    """A short reference a view may quote for a message: hexadecimal, never mail text."""
    identity = item.get("message_id") or "/".join(
        str(item.get(key)) for key in ("folder", "uidvalidity", "uid")
    )
    return "msg-" + hashlib.sha256(str(identity).encode("utf-8", "replace")).hexdigest()[:12]


def _header(item: dict[str, Any], name: str) -> str:
    headers = item.get("headers")
    if not isinstance(headers, dict):
        return ""
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name.lower():
            return str(value)
    return ""


def _body(item: dict[str, Any], markup: str | None, render_html: bool) -> dict[str, Any]:
    text = item.get("text") or ""
    if markup is None:
        return {"kind": "text", "text": text}
    if not render_html:
        return {"kind": "text", "text": text, "html_omitted": True}
    sanitized = sanitize_email_html(markup, truncated=bool(item.get("truncated")))
    if sanitized["html"] is None:
        return {"kind": "text", "text": text, "html_unavailable": True}
    return {
        "kind": "html",
        "document": email_document(sanitized["html"]),
        "text": text,
        "remote_blocked": sanitized["remote_blocked"],
        "removed_active": sanitized["removed_active"],
        "links": sanitized["links"],
    }


def thread_size_note(result: dict[str, Any]) -> dict[str, str]:
    """What a read_thread result says about its attachment sizes, when it lists any."""
    listed = any(item.get("attachments") for item in result.get("messages") or [])
    return {"size_note": ENCODED_SIZE_NOTE} if listed else {}


def thread_view(result: dict[str, Any], requested: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a prefer_html read_thread result into model text and view data.

    The model text keeps read_thread's shape; HTML bodies become the text that
    read_thread extracts from HTML. Only sanitized HTML reaches the view.
    """
    requested = _message_id(requested)
    items = [dict(item) for item in result.get("messages") or []]
    markup: list[str | None] = []
    for item in items:
        # The model resolves a view's chat reference (view_ref) from this result.
        item["view_ref"] = view_ref(item)
        is_html = item.pop("body_type", None) == "text/html"
        markup.append((item.get("text") or "") if is_html else None)
        if is_html:
            item["text"] = html_text(item.get("text") or "")
    position = next(
        (index for index, item in enumerate(items) if item.get("message_id") == requested), None
    )
    # The requested message and the newest ones keep their formatting.
    newest = set(range(max(0, len(items) - MAX_HTML_MESSAGES), len(items)))
    if position is not None and position not in newest:
        newest.discard(min(newest))
        newest.add(position)
    messages = []
    totals = {"images": 0, "inline_images": 0, "styles": 0, "other": 0}
    for index, item in enumerate(items):
        body = _body(item, markup[index], index in newest)
        for key, count in body.get("remote_blocked", {}).items():
            totals[key] += count
        messages.append(
            {
                "index": index + 1,
                "view_ref": item["view_ref"],
                "message_id": item.get("message_id"),
                "requested": index == position,
                "folder": item.get("folder"),
                "uidvalidity": item.get("uidvalidity"),
                "uid": item.get("uid"),
                "alias": item.get("alias"),
                "from": item.get("from") or "",
                "to": item.get("to") or "",
                "cc": _header(item, "Cc"),
                "reply_to": _header(item, "Reply-To"),
                "date": item.get("date") or "",
                "subject": item.get("subject") or "",
                "attachments": [
                    {
                        "filename": visible_name(attachment["filename"])
                        if attachment.get("filename")
                        else None,
                        "type": visible_type(attachment.get("type")),
                        "size": attachment.get("size"),
                        "size_encoded": True,
                    }
                    for attachment in item.get("attachments") or []
                ],
                "truncated": bool(item.get("truncated")),
                "body": body,
            }
        )
    text = {
        **{key: value for key, value in result.items() if key != "messages"},
        **thread_size_note(result),
        "messages": items,
    }
    subject = (messages[position] if position is not None else (messages or [{}])[0]).get(
        "subject", ""
    )
    view = {
        "view": "thread",
        "requested_message_id": requested,
        "found": position is not None,
        "subject": subject,
        "partial": bool(result.get("partial")),
        "partial_reason": result.get("partial_reason"),
        "timed_out": bool(result.get("timed_out")),
        "error": result.get("error"),
        "remote_blocked": totals,
        "limits": {
            "body_bytes": MAX_THREAD_BODY_BYTES,
            "header_bytes": MAX_THREAD_HEADER_BYTES,
            "seconds": MAX_THREAD_SECONDS,
            "html_messages": MAX_HTML_MESSAGES,
        },
        "messages": messages,
    }
    return text, view


def _recipients(header: str) -> tuple[list[str], list[str]]:
    """Plain ASCII addresses in a header, and the entries that are not."""
    try:
        parsed = getaddresses([header]) if header else []
    except Exception:  # A malformed header prefills nothing.
        return [], [header]
    kept, skipped = [], []
    for name, address in parsed:
        if is_bare_address(address):
            kept.append(address)
        elif address or name:
            skipped.append(address or name)
    return kept, skipped


def account_address(user: str) -> str:
    """The From address create_draft writes for this account."""
    kept, _ = _recipients(user)
    return kept[0] if len(kept) == 1 else user


def _unique(addresses: list[str], exclude: set[str]) -> list[str]:
    seen = set(exclude)
    result = []
    for address in addresses:
        if address.lower() not in seen:
            seen.add(address.lower())
            result.append(address)
    return result


def _one_line(value: Any) -> str:
    # create_draft refuses control characters and line breaks in subjects.
    return re.sub(r"[\x00-\x1f\x7f\x85\u2028\u2029]+", " ", str(value or "")).strip()


def _reply_subject(subject: str) -> str:
    subject = _one_line(subject)
    return subject if re.match(r"(?i)re\s*(\[\d+\])?\s*:", subject) else f"Re: {subject}".strip()


# A quote line without text: only ">" and whitespace, such as ">" or "> >".
_EMPTY_QUOTE = re.compile(r">[>\s]*")


def _quoted(item: dict[str, Any]) -> str:
    lines = (item.get("text") or "").splitlines() or [""]
    attribution = (
        f"On {_one_line(item.get('date')) or 'an earlier date'}, "
        f"{_one_line(item.get('from')) or 'the sender'} wrote:"
    )
    kept: list[str] = []
    for line in lines:
        line = ">" + (" " + line if line else "")
        # A run of empty quote lines, whatever their depth, keeps only its first.
        if not (kept and _EMPTY_QUOTE.fullmatch(line) and _EMPTY_QUOTE.fullmatch(kept[-1])):
            kept.append(line)
    quoted = "\n".join(kept)
    if item.get("truncated"):
        quoted += "\n> [The original message is longer; only its beginning is quoted.]"
    return f"\n\n{attribution}\n{quoted}\n"


def _thread_id(value: Any) -> str | None:
    try:
        return _message_id(value)
    except MailValidationError:
        return None


def forward_fields(item: dict[str, Any], note: str) -> tuple[str, str]:
    """The subject and text of a draft that forwards one read message."""
    original = _one_line(item.get("subject"))
    subject = original if FORWARD_SUBJECT.match(original) else f"Fwd: {original}".strip()
    lines = [
        "---------- Forwarded message ---------",
        f"From: {_one_line(item.get('from'))}",
        f"Date: {_one_line(item.get('date'))}",
        f"Subject: {original}",
        f"To: {_one_line(item.get('to'))}",
    ]
    cc = _one_line(_header(item, "Cc"))
    if cc:
        lines.append(f"Cc: {cc}")
    text = (item.get("text") or "").replace("\r\n", "\n")
    if item.get("truncated"):
        text += "\n\n[The original message is longer; only its beginning is included.]"
    return subject, f"{note}\n\n" * bool(note) + "\n".join(lines) + f"\n\n{text}\n"


# A group without members, such as "undisclosed-recipients:;", names nobody.
_EMPTY_GROUP = re.compile(r"[^:]*:\s*;?")


def _hidden(header: str) -> list[str]:
    """A draft's Bcc entries. A header that names someone but cannot be parsed is listed whole."""
    kept, skipped = _recipients(header)
    header = _one_line(header)
    if not kept and not skipped and header and not _EMPTY_GROUP.fullmatch(header):
        return [header[:200]]
    return kept + skipped


def _listed(entries: list[str]) -> str:
    shown = ", ".join(_one_line(entry)[:200] for entry in entries[:10])
    return shown + (f" and {len(entries) - 10} more" if len(entries) > 10 else "")


def _not_copied_warning(not_copied: dict[str, list[Any]]) -> str | None:
    if not not_copied["bcc"]:
        return None
    return (
        "Saving this edit creates a new draft without the original's "
        f"Bcc recipients ({_listed(not_copied['bcc'])}). The original draft keeps them."
    )


def _is_draft(item: dict[str, Any]) -> bool:
    return "\\draft" in {str(flag).lower() for flag in item.get("flags") or []}


def _local_rows(local: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"source": "local", **row, "checked": True} for row in local]


def _message_rows(item: dict[str, Any], *, keep: bool) -> list[dict[str, Any]]:
    """A message's attachments as the composer lists them: index, name, type and size."""
    return [
        {
            "source": "message",
            "index": index,
            "name": visible_name(attachment.get("filename") or "(unnamed)"),
            "type": visible_type(attachment.get("type")),
            "size": attachment.get("size"),
            "size_encoded": True,
            "checked": keep,
        }
        for index, attachment in enumerate(item.get("attachments") or [], start=1)
    ]


def _attachment_fields(
    local: list[dict[str, Any]], rows: list[dict[str, Any]], source: str
) -> dict[str, Any]:
    """The create_draft arguments that carry the attachments a composer starts with."""
    fields: dict[str, Any] = {}
    if local:
        fields["attachments"] = [row["name"] for row in local]
    kept = [row["index"] for row in rows if row["checked"]]
    if kept:
        fields.update(attachments_from=source, attachment_indexes=kept)
    return fields


def _from_fields(
    sender: str | None, allowed: tuple[str, ...], selected: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A composer's From: what it shows, and the create_draft argument for a chosen alias."""
    if not sender:
        return {}, {}
    choices = list(dict.fromkeys([sender, *allowed]))
    identity = {
        "from": selected or sender,
        "default_from": sender,
        **({"from_choices": choices} if len(choices) > 1 else {}),
    }
    return identity, {"from_address": selected} if selected and selected != sender else {}


def from_addresses(settings: Any) -> dict[str, Any]:
    """The From addresses a draft can use; nothing is read from Bridge."""
    default = account_address(settings.bridge_user)
    allowed = list(settings.send_from_allowlist)
    return {
        "default": default,
        "allowed": allowed,
        "default_allowed": default in allowed,
        "send_enabled": settings.enable_send,
        "note": (
            "Pass one of allowed as from_address to create_draft, forward_message or "
            "compose_reply; any other address refuses. Without from_address a draft comes "
            "from default. The operator sets this list in PROTON_MCP_SEND_FROM."
        ),
    }


# The listed files that draft holds go with it; a listed file whose checked is false only
# joins by its index in attachment_indexes, beside attachments_from.
_ATTACHED_NOTE = (
    " attached lists the attachments of this draft. Those with checked true are in the draft "
    "fields; to include another one, add its index to attachment_indexes and set "
    "attachments_from to attachments_source. In a view the user ticks them."
)


def reply_view(
    result: dict[str, Any],
    requested: str,
    *,
    own: set[str],
    reply_all: bool = False,
    edit: bool = False,
    send_enabled: bool = False,
    local: list[dict[str, Any]] | None = None,
    sender: str | None = None,
    allowed: tuple[str, ...] = (),
    from_address: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prefill a reply to one read message, or reopen a saved draft for editing."""
    requested = _message_id(requested)
    item = next(
        (item for item in result.get("messages") or [] if item.get("message_id") == requested),
        None,
    )
    if item is None:
        if result.get("partial") or result.get("timed_out"):
            kind = "Draft" if edit else "Message"
            text = {
                "mode": "edit" if edit else "reply",
                "partial": True,
                "error": f"{kind} lookup incomplete; retry the read",
                **{
                    key: result[key]
                    for key in ("partial_reason", "index_note", "index_status", "timed_out")
                    if key in result
                },
            }
            # No draft fields means the existing reply view cannot save a blank
            # composer. The tool returns this as an error with its read details.
            return text, {"view": "reply", "lookup_incomplete": True, **text}
        raise MailValidationError(
            "Draft not found" if edit else "Message not found; check its Message-ID"
        )
    own = {address.lower() for address in own}
    warnings = []
    senders, skipped_from = _recipients(item.get("from") or "")
    reply_to, skipped_reply = _recipients(_header(item, "Reply-To"))
    recipients, skipped_to = _recipients(item.get("to") or "")
    copied, skipped_cc = _recipients(_header(item, "Cc"))
    not_copied: dict[str, list[Any]] | None = None
    if edit:
        to, cc = recipients, copied
        all_cc: list[str] = []
        subject = _one_line(item.get("subject"))
        body = item.get("text") or ""
        in_reply_to = _thread_id(_header(item, "In-Reply-To").strip() or None)
        skipped = skipped_to + skipped_cc
        if item.get("truncated"):
            warnings.append("The draft is longer than the read limit; only its beginning loaded.")
        # create_draft writes no Bcc, so a draft saved from these fields lacks it. The read
        # keeps a Bcc header only for messages flagged as drafts.
        not_copied = {"bcc": _hidden(_header(item, "Bcc"))}
        left_out = _not_copied_warning(not_copied)
        if left_out:
            warnings.append(left_out)
        # A draft from an allowed alias stays from it; any other address cannot.
        written_from = senders[0] if len(senders) == 1 else None
        if not from_address and sender and written_from and written_from != sender:
            if written_from in allowed:
                from_address = written_from
            else:
                warnings.append(
                    f"This draft is from {written_from}, which is not an allowed From address. "
                    f"Saving writes it from {sender}."
                )
    else:
        if senders and all(address.lower() in own for address in senders):
            # A reply to your own message goes to its original recipients.
            to = _unique(recipients, own)
            all_cc = _unique(copied, own | {address.lower() for address in to})
        else:
            to = _unique(reply_to or senders, own)
            all_cc = _unique(recipients + copied, own | {address.lower() for address in to})
        cc = all_cc if reply_all else []
        subject = _reply_subject(item.get("subject") or "")
        body = _quoted(item)
        # The read matched this validated identity, so create_draft accepts it.
        in_reply_to = requested
        skipped = skipped_from + skipped_reply + skipped_to + skipped_cc
    if skipped:
        warnings.append(
            "Some recipients are not plain ASCII addresses and were not prefilled: "
            + ", ".join(_one_line(entry)[:200] for entry in skipped[:10])
        )
    if result.get("partial") or result.get("timed_out"):
        warnings.append(
            # A read that stopped early may not have listed the draft's attachments.
            "The draft could not be read completely, so some of its text or attachments may "
            "be missing here."
            if edit
            else "The original message could not be read completely."
        )
    # An edit keeps a draft's attachments, and so does a reply to a saved draft. A reply to
    # other mail only offers the original's attachments.
    message_rows = _message_rows(item, keep=edit or _is_draft(item))
    attached = _local_rows(local or []) + message_rows
    identity, from_field = _from_fields(sender, allowed, from_address)
    draft = {
        "to": to,
        "cc": cc,
        "subject": subject,
        "body": body,
        "in_reply_to": in_reply_to,
        **_attachment_fields(local or [], message_rows, requested),
        **from_field,
    }
    original = {key: item.get(key) for key in ("message_id", "from", "to", "date", "subject")}
    note = (
        "Unsent. In an MCP Apps view the user edits these fields and saves them with "
        "create_draft; nothing is sent from the view. Without a view, show the fields "
        "to the user and call create_draft only after they agree."
    )
    if edit:
        note += (
            " Saving an edit creates a new draft with only these fields, as plain text; the "
            "original stays in Drafts."
        )
    if not_copied and any(not_copied.values()):
        note += (
            " The new draft leaves out the Bcc recipients listed in not_copied; the user must "
            "know this before saving."
        )
    if attached:
        note += _ATTACHED_NOTE
    listed = (
        {"attached": attached, **({"attachments_source": requested} if message_rows else {})}
        if attached
        else {}
    )
    text = {
        "mode": "edit" if edit else "reply",
        **identity,
        "draft": draft,
        "original": original,
        "warnings": warnings,
        **listed,
        **({"size_note": ATTACHED_SIZE_NOTE} if attached else {}),
        **({"not_copied": not_copied} if not_copied is not None else {}),
        "note": note,
    }
    view = {
        "view": "reply",
        "mode": text["mode"],
        "reply_all": bool(reply_all) and not edit,
        "reply_all_cc": all_cc,
        **identity,
        "draft": draft,
        "original": original,
        "warnings": warnings,
        **listed,
        **({"not_copied": not_copied} if not_copied is not None else {}),
        "send_enabled": send_enabled,
    }
    return text, view


def new_draft_view(
    to: str | list[str] | None,
    cc: str | list[str] | None,
    subject: str,
    body: str,
    local: list[dict[str, Any]] | None = None,
    sender: str | None = None,
    allowed: tuple[str, ...] = (),
    from_address: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prefill a new draft with the fields given: no original, no read, no write."""
    # What create_draft would refuse is refused now, so no recipient silently disappears.
    (to, skipped_to), (cc, skipped_cc) = (_recipients(_addresses(field)) for field in (to, cc))
    warnings = []
    if skipped_to or skipped_cc:
        warnings.append(
            "Some recipients are not plain ASCII addresses and were not prefilled: "
            + _listed(skipped_to + skipped_cc)
        )
    identity, from_field = _from_fields(sender, allowed, from_address)
    draft = {
        "to": to,
        "cc": cc,
        "subject": _one_line(subject),
        "body": body,
        "in_reply_to": None,
        **_attachment_fields(local or [], [], ""),
        **from_field,
    }
    listed = {"attached": _local_rows(local)} if local else {}
    text = {
        "mode": "new",
        **identity,
        "draft": draft,
        "warnings": warnings,
        **listed,
        **({"size_note": ATTACHED_SIZE_NOTE} if local else {}),
        "note": (
            "Unsent and not saved yet. In an MCP Apps view the user edits these fields and "
            "saves them with create_draft, which needs at least one recipient; nothing is sent "
            "from the view. Without a view, show the fields to the user and call create_draft "
            "only after they agree."
        ),
    }
    # Draft only: send_enabled stays False, so the view offers no send review.
    view = {
        "view": "reply",
        "mode": "new",
        "reply_all": False,
        "reply_all_cc": [],
        **identity,
        "draft": draft,
        "warnings": warnings,
        **listed,
        "send_enabled": False,
    }
    return text, view


# Never passed to a view, whatever a preview contains.
_SECRET_FIELDS = frozenset({"token", "expires_at"})


def review_view(preview: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """A send review: preview_send's preview without any redeemable field."""
    shown = {key: value for key, value in preview.items() if key not in _SECRET_FIELDS}
    confirmation = (
        "the user then confirms the exact draft in the host's form"
        if shown.get("approval") == "host_confirmation"
        else "this client has no form, so first show that preview's summary, wait for the user's "
        "typed OK in chat, and name the draft in draft_id"
        if shown.get("approval") == "typed_ok"
        else "own-address token approval sends without a separate form"
    )
    text = {
        **shown,
        "next_steps": (
            "This review issues no token, so nothing can be sent with it. To send after "
            "the user approves this exact preview, call preview_send(draft_id) for a fresh "
            "single-use token, check that its draft_hash equals this draft_hash, then call "
            f"send_draft(token); {confirmation}. If the hash differs, show the new preview."
        ),
    }
    view = {
        "view": "review_send",
        "preview": shown,
        "quotable_draft_id": plain_id(shown.get("draft_id")),
    }
    return text, view
