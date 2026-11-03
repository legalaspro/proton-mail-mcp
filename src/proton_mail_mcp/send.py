"""One live bound approval per draft, one journaled loopback SMTP attempt per approval."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import smtplib
import ssl
import time
from contextlib import suppress
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any, Callable

import anyio

from .action_log import _CLIENT, ActionLog, new_record
from .attachments import _visible, visible_name, visible_type
from .confirmation import (
    ConfirmationError,
    declares_no_form,
    log_list_use,
    log_safe,
    require_confirmation,
)
from .guarded import TokenStore, approval_hash
from .mail import (
    BRIDGE_FAILURES,
    MAX_DRAFT_BYTES,
    MailError,
    MailValidationError,
    _draft_message_id,
    _HtmlText,
)
from .security import tls_context, verify_certificate
from .send_address import (
    FORWARD_SUBJECT,
    is_bare_address,
    parse_addresses,
    parse_draft_addresses,
)
from .views import account_address

_LOGGER = logging.getLogger(__name__)
_MAX_DRAFT_BYTES = MAX_DRAFT_BYTES
# No address is built in: only the user's own send-from addresses (the account and
# PROTON_MCP_SEND_FROM) skip the form. Anyone else's address takes the form, or a typed OK
# on a client without one, which proves no consent. Proof requires out-of-band approval:
# an independent-channel link/code, or an authenticated server-side approval queue, never
# a token edit.


class _SendRefusal(MailValidationError):
    def __init__(self, result: str, message: str):
        super().__init__(message)
        self.result = result


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


# Bridge writes a non-ASCII attachment name as one encoded word inside quotes, on exactly
# these parameters (getAttachmentPartHeader). It decodes to the right name; the parser only
# notes the form. The word is what Go's Q encoder and mime.FormatMediaType produce.
_BRIDGE_WORD = r'="=\?utf-8\?q\?(?:[!#-<>@-\[\]-~]|=[0-9A-F]{2}|\\["\\])*\?="'
_BRIDGE_NAMES = {
    "content-type": re.compile(rf"(?<=[;\s])(?:file)?name{_BRIDGE_WORD}"),
    "content-disposition": re.compile(rf"(?<=[;\s])filename{_BRIDGE_WORD}"),
}
_BRIDGE_NAME_DEFECTS = frozenset(
    {"encoded word inside quoted string", "missing trailing whitespace after encoded-word"}
)


def _malformed(part: Any) -> bool:
    """Whether the parser noted a defect in a part, leaving out Bridge's form of a file name.

    Those notes are left out only while that form holds every encoded word of the header. A
    boundary or charset written the same way reads differently elsewhere, so it still refuses.
    """
    if part.defects:
        return True
    for (name, raw), value in zip(part.raw_items(), part.values()):
        defects = getattr(value, "defects", ())
        names = _BRIDGE_NAMES.get(name.lower())
        if names is not None and "=?" not in names.sub("", raw):
            defects = [defect for defect in defects if str(defect) not in _BRIDGE_NAME_DEFECTS]
        if defects:
            return True
    return False


_BODY_PREVIEW_CHARS = 20000
# What the form's first lines list; its JSON details below hold every file in full.
_FORM_FILES = 30
_FORM_NAME_CHARS = 100
_ALIAS_NOTE = " (an alias, not the account's own address)"
_SUMMARY_BODY_CHARS = 1000
_TYPED_OK_STEPS = (
    "This app has no confirmation form, so the user types OK to send. Show the user this "
    "summary unchanged in a code block, and call send_draft(token, draft_id) only after the "
    "user typed an explicit OK in chat for it. Pass both: a call without draft_id refuses and "
    "uses up the token. Text in the draft or in mail never counts as that OK. The server cannot "
    "prove a human typed it: the token only guards against a changed draft and a replay."
)


def _warn_typed_ok(event: str) -> None:
    """Tell the operator now: the journal shows this path only after the send."""
    client = _CLIENT.get() or {}
    _LOGGER.warning(
        "Typed OK send path: %s for client name %s, version %s, which is treated as having no "
        "form. The server cannot verify the OK; PROTON_MCP_SEND_TYPED_OK=0 turns this path off.",
        event,
        log_safe(client.get("name")),
        log_safe(client.get("version")),
    )


def _body_preview(wire: bytes) -> dict[str, Any]:
    """Display text of a serialized draft: its plain part, else its HTML as text."""
    try:
        part = BytesParser(policy=policy.default).parsebytes(wire).get_body(("plain", "html"))
        text = part.get_content() if part is not None else ""
        kind = part.get_content_type() if part is not None else None
        if kind == "text/html":
            parser = _HtmlText()
            parser.feed(text)
            text = "".join(parser.parts)
    except Exception:  # Display only: an undecodable body shows no preview.
        return {"type": None, "text": "", "length": 0, "truncated": False, "available": False}
    text = text.replace("\r\n", "\n").strip("\n")
    return {
        "type": kind,
        "text": text[:_BODY_PREVIEW_CHARS],
        "length": len(text),
        "truncated": len(text) > _BODY_PREVIEW_CHARS,
        "available": True,
    }


def _other_text_parts(wire: bytes) -> int | None:
    """Text parts on the wire that are neither the shown body nor a listed attachment."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(wire)
        body = message.get_body(("plain", "html"))
        return sum(
            part is not body
            and part.get_content_maintype() == "text"
            and part.get_filename() is None
            and part.get_content_disposition() != "attachment"
            for part in message.walk()
        )
    except Exception:  # Display only, as in _body_preview.
        return None


def _form_sender_and_files(preview: dict[str, Any]) -> str:
    """The sender and attached files for the form's first line.

    A file name keeps its own letters, so a person can read it. visible_name has already
    turned every control, invisible and line-separator character into "_", and JSON quotes
    the rest, so a name cannot start a line of its own. The JSON details below stay ASCII,
    so a look-alike letter can be checked there.
    """
    files = preview["attachments"]
    listed = "; ".join(
        f"{json.dumps(visible_name(file['name'], _FORM_NAME_CHARS), ensure_ascii=False)} "
        f"({json.dumps(file['type'])}, {file['size']} bytes)"
        for file in files[:_FORM_FILES]
    )
    if len(files) > _FORM_FILES:
        listed += f"; and {len(files) - _FORM_FILES} more, listed below"
    alias = _ALIAS_NOTE if preview["from_alias"] else ""
    return (
        f"From: {json.dumps(preview['from'])}{alias}. "
        f"Attached files ({len(files)}): {listed or 'none'}."
    )


def _typed_ok_summary(draft: dict[str, Any]) -> str:
    """The plain text a person reads before typing OK; it holds no token.

    Each field takes one line and the quoted body comes last, so a subject, file name or
    body line cannot pass for another field: _visible has turned every line separator in
    them into "_". Every line starts with its field or "> ", so none can close a code block.
    It lists every file, because no JSON details follow it as on the form, and it names what
    it leaves out.
    """
    files = draft["attachments"]
    other_parts = _other_text_parts(draft["wire"])
    lines = [
        "Send this email? Sending cannot be undone.",
        f"From: {draft['from']}{_ALIAS_NOTE if draft['from_alias'] else ''}",
        *(f"{name.title()}: {', '.join(draft[name]) or 'none'}" for name in ("to", "cc", "bcc")),
        *([f"Reply-To: {', '.join(draft['reply_to'])}"] if draft["reply_to"] else []),
        f"Subject: {_visible(draft['subject']) or 'none'}",
        f"Attachments ({len(files)}):" if files else "Attachments: none",
        *(
            f"- {json.dumps(file['name'], ensure_ascii=False)} "
            f"({file['type']}, {file['size']} bytes decoded)"
            for file in files
        ),
        *(
            ["Other text parts: could not be counted"]
            if other_parts is None
            else [f"Other text parts, not shown: {other_parts}"]
            if other_parts
            else []
        ),
        f"SHA-256 prefix: {draft['identity']['draft_hash'][:16]}",
    ]
    body = _body_preview(draft["wire"])
    if not body["available"]:
        lines.append("Body: could not be read for this summary")
    elif not body["text"]:
        lines.append("Body: empty")
    else:
        lines.append(
            f"Body, first {_SUMMARY_BODY_CHARS} of {body['length']} characters only:"
            if body["length"] > _SUMMARY_BODY_CHARS
            else "Body:"
        )
        excerpt = body["text"][:_SUMMARY_BODY_CHARS]
        lines.extend(f"> {_visible(line)}" for line in excerpt.splitlines())
    lines.append("This app has no confirmation form, so type OK to send exactly this.")
    return "\n".join(lines)


def _own_recipients(draft: dict[str, Any]) -> bool:
    # The path without a form is for plain notes to yourself. Attachments and forwards
    # always go through the host form, whoever receives them.
    if draft["attachments"] or FORWARD_SUBJECT.match(draft["subject"]):
        return False
    # Every address is the user's own, from any sender _snapshot allowed. recipients is the
    # whole To, Cc and Bcc envelope, which _snapshot refuses when empty.
    return all(address.lower() in draft["own_addresses"] for address in draft["recipients"])


def _binding(draft: dict[str, Any]) -> dict[str, Any]:
    return {
        **draft["identity"],
        **{name: draft[name] for name in ("from", "to", "cc", "bcc", "recipients")},
        "recipient_headers": draft["recipient_headers"],
    }


def _addresses(value: str, *, allow_display_names: bool = True) -> list[str]:
    try:
        return parse_addresses(value, allow_display_names=allow_display_names)
    except Exception:  # On junk the stdlib parser also raises TypeError, AttributeError...
        raise MailValidationError("Send requires unambiguous ASCII email addresses") from None


def _draft_from_addresses(value: str) -> list[str]:
    try:
        return parse_draft_addresses(value)
    except Exception:  # As in _addresses.
        raise MailValidationError("Send requires unambiguous ASCII email addresses") from None


# Everything below a space and DEL except a tab, which is folding white space anywhere in
# RFC 5322. CR and LF count: only a real fold, CRLF before a space or tab, is removed first.
_HEADER_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_HEADER_FOLD = re.compile(r"\r\n(?=[ \t])")


def _serialized(part: Any, write_policy: Any) -> bytes:
    try:
        return part.as_bytes(policy=write_policy)
    except Exception:  # HeaderWriteError; on junk the generator also raises UnicodeEncodeError...
        raise MailValidationError("Malformed draft content cannot be sent") from None


def _snapshot(row: dict[str, Any], draft_id: str, settings: Any) -> dict[str, Any]:
    raw = row.get("raw")
    if not isinstance(raw, bytes) or not 0 < len(raw) <= _MAX_DRAFT_BYTES:
        raise MailValidationError("Draft is empty or exceeds the 10 MiB send limit")
    if row.get("message_id") != draft_id or row.get("folder") != settings.drafts_folder:
        raise MailValidationError("Draft identity changed; request a new preview")
    message = BytesParser(policy=policy.SMTP).parsebytes(raw)
    sensitive = {"from", "sender", "reply-to", "to", "cc", "bcc", "subject", "message-id"}
    headers = list(message.raw_items())
    raw_headers = {name.lower(): value for name, value in headers}
    for name, value in headers:
        if name.lower().startswith("resent-"):
            raise MailValidationError("Resent headers are not supported for guarded send")
        if name.lower() in sensitive - {"from"} and any(
            ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise MailValidationError("Send headers must contain no controls or folded lines")
        # Python before 3.12.4 writes a header through str.splitlines, so VT, FF, FS, GS or RS
        # in any header can become a line of its own, such as a Cc this function never parsed.
        if _HEADER_CONTROL.search(_HEADER_FOLD.sub("", value)):
            raise MailValidationError("Send headers must contain no control characters")
    for name in sensitive:
        if sum(field.lower() == name for field, _ in headers) > 1:
            raise MailValidationError("Draft has duplicate address, subject or identity headers")
    if raw_headers.get("message-id") != draft_id:
        raise MailValidationError("Draft identity changed; request a new preview")
    senders = _draft_from_addresses(raw_headers.get("from", ""))
    if len(senders) != 1 or senders[0] not in settings.send_from_allowlist:
        raise MailValidationError("Draft From is not an exact allowlisted address")
    sender = senders[0]
    if "sender" in raw_headers:
        explicit_sender = _addresses(raw_headers["sender"], allow_display_names=False)
        if explicit_sender != [sender]:
            raise MailValidationError("Draft Sender must match its allowlisted From")
    recipients = {}
    for name in ("To", "Cc", "Bcc", "Reply-To"):
        value = raw_headers.get(name.lower())
        recipients[name.lower()] = _addresses(value) if value is not None else []
    # The email parser removes all whitespace after a header colon. Preserve
    # the physical spelling too, so the binding covers the actual To/Cc/Bcc
    # values and an empty Cc or Bcc header stays visible to eligibility.
    recipient_headers = dict.fromkeys(("to", "cc", "bcc"))
    lines = raw.splitlines()
    for line, following in zip(lines, [*lines[1:], b""]):
        if not line:
            break
        name, separator, value = line.partition(b":")
        if separator and name.lower() in (b"to", b"cc", b"bcc"):
            # Newer parsers strip a line break right after the colon, hiding a folded
            # recipient header from the check above. Refuse it on every Python version.
            if not value.strip(b" \t") and following[:1] in (b" ", b"\t"):
                raise MailValidationError("Send headers must contain no controls or folded lines")
            recipient_headers[name.decode("ascii").lower()] = value.decode("ascii")
    envelope = recipients["to"] + recipients["cc"] + recipients["bcc"]
    if not 1 <= len(envelope) <= settings.send_max_recipients:
        raise MailValidationError("Draft recipient count exceeds the configured send cap")
    subject = str(message.get("Subject", ""))
    if any(ord(char) < 32 or ord(char) == 127 for char in subject):
        raise MailValidationError("Send subject must contain no control characters")
    # An encoded word can decode to a line break, and Python before 3.12.5 can write that out
    # when it refolds a value holding raw 8-bit bytes. No raw control is left for the check above.
    if any(_HEADER_CONTROL.search(str(value)) for value in message.values()):
        raise MailValidationError("Send headers must contain no control characters")
    attachments = []
    for part in message.walk():
        name = part.get_filename()
        if (
            name is not None
            or part.get_content_disposition() == "attachment"
            or part.get_content_maintype() not in {"text", "multipart"}
        ):
            content = part.get_payload(decode=True)
            if content is None:
                content = _serialized(part, policy.SMTP)
            attachments.append(
                {
                    "name": visible_name(name) if name else "(unnamed)",
                    "type": visible_type(part.get_content_type()),
                    "size": len(content),
                    "sha256": _sha256(content),
                }
            )
        if _malformed(part):
            raise MailValidationError("Malformed draft content cannot be sent")
    del message["Bcc"]
    wire = _serialized(message, policy.SMTP.clone(refold_source="none"))
    if not wire.endswith(b"\r\n"):
        wire += b"\r\n"
    # Bind the exact bytes passed to SMTP DATA and its complete ordered envelope,
    # including Bcc. Length framing makes their concatenation unambiguous.
    envelope_bytes = json.dumps(
        {"from": sender, "recipients": envelope},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    digest = _sha256(
        b"proton-mail-mcp:send:v1\0"
        + len(envelope_bytes).to_bytes(8, "big")
        + envelope_bytes
        + wire
    )
    return {
        "identity": {
            "message_id": draft_id,
            "folder": row["folder"],
            "uidvalidity": row["uidvalidity"],
            "uid": row["uid"],
            "draft_hash": digest,
            "subject_hash": _sha256(subject.encode("utf-8")),
            "recipient_domains": sorted({address.rsplit("@", 1)[1] for address in envelope}),
            "recipient_count": len(envelope),
        },
        "from": sender,
        "from_alias": sender != account_address(settings.bridge_user),
        "own_addresses": {
            address.lower()
            for address in (account_address(settings.bridge_user), *settings.send_from_allowlist)
        },
        "to": recipients["to"],
        "cc": recipients["cc"],
        "bcc": recipients["bcc"],
        "reply_to": recipients["reply-to"],
        "subject": subject,
        "attachments": attachments,
        "recipients": envelope,
        "recipient_headers": recipient_headers,
        "wire": wire,
    }


class SendService:
    def __init__(
        self,
        client: Any,
        settings: Any = None,
        *,
        tokens: TokenStore | None = None,
        log: ActionLog | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.client = client
        self.settings = settings or client.settings
        self.tokens = tokens or TokenStore(min(self.settings.send_token_ttl, 600))
        # A supplied store is dedicated to this service and may only shorten
        # the configured lifetime, never extend the send approval ceiling.
        self.tokens.ttl = min(self.tokens.ttl, self.settings.send_token_ttl, 600)
        self.log = log or ActionLog(self.settings.action_log_path)
        self.clock = clock

    def _enabled(self) -> None:
        if self.settings.enable_send is not True:
            raise MailValidationError("Guarded send is disabled")

    def _tls_context(self) -> ssl.SSLContext:
        if self.settings.smtp_host not in {"127.0.0.1", "::1"}:
            raise MailValidationError("SMTP requires the literal loopback address 127.0.0.1 or ::1")
        if not self.settings.imap_cert_path and self.settings.smtp_allow_unverified is not True:
            raise MailValidationError("SMTP requires a configured Bridge certificate pin")
        # The shared policy checks the IMAP endpoint. SMTP is strictly loopback,
        # so use the same certificate settings with its separately checked host.
        from dataclasses import replace

        context = tls_context(replace(self.settings, imap_host=self.settings.smtp_host))
        if (
            context.verify_mode != ssl.CERT_REQUIRED
            and self.settings.smtp_allow_unverified is not True
        ):
            raise MailValidationError("SMTP requires verified TLS")
        return context

    def _remaining(self, records: list[dict[str, Any]]) -> int:
        cutoff = self.clock() - 86400
        used = 0
        for record in records:
            if record.get("action") != "send_draft" or not record.get("budget_reserved"):
                continue
            try:
                timestamp = datetime.fromisoformat(record["timestamp"])
                if timestamp.tzinfo is None:
                    raise ValueError
                used += timestamp.timestamp() > cutoff
            except (KeyError, TypeError, ValueError, OverflowError):
                raise MailError("Send budget journal is invalid; refusing send") from None
        return max(0, self.settings.send_daily_cap - used)

    def _budget(self) -> int:
        with self.log.transaction(readonly=True) as transaction:
            return self._remaining(transaction.records())

    def _form_budget(self, draft: dict[str, Any]) -> int:
        # A draft that would refuse at delivery never reaches a person's form.
        with self.log.transaction(readonly=True) as transaction:
            records = transaction.records()
            self._refuse_repeat(records, draft)
            return self._remaining(records)

    @staticmethod
    def _refuse_repeat(records: list[dict[str, Any]], draft: dict[str, Any]) -> None:
        """Refuse a draft that an earlier attempt delivered or may have delivered."""
        rows = [row for row in records if row.get("draft_hash") == draft["identity"]["draft_hash"]]
        results = [row for row in rows if row.get("action") == "send_result"]
        if any(row.get("result") in {"sent", "partial"} for row in results):
            raise _SendRefusal("already_sent", "This exact draft was already sent")
        finished = {row.get("send_log_id") for row in results}
        # A reservation without a result is a crash or a lost journal write. It may
        # also predate DATA, but nothing can tell, so it takes the safe side.
        if any(row.get("uncertain") for row in results) or any(
            row.get("action") == "send_draft"
            and row.get("budget_reserved")
            and row.get("log_id") not in finished
            for row in rows
        ):
            raise _SendRefusal(
                "possibly_sent",
                "This exact draft may already have been sent; "
                "check Sent, then edit it or create a new draft",
            )

    def _read(self, draft_id: str) -> dict[str, Any]:
        draft_id = _draft_message_id(draft_id)
        try:
            row = self.client.read_draft(draft_id)
        except BRIDGE_FAILURES:
            raise
        except Exception:
            raise MailError("Draft read failed; request a new preview") from None
        return _snapshot(row, draft_id, self.settings)

    def _preview(self, draft: dict[str, Any], remaining: int) -> dict[str, Any]:
        return {
            "draft_id": draft["identity"]["message_id"],
            "from": draft["from"],
            "from_alias": draft["from_alias"],
            "to": draft["to"],
            "cc": draft["cc"],
            "bcc": draft["bcc"],
            "subject": draft["subject"],
            "attachments": draft["attachments"],
            "draft_hash": draft["identity"]["draft_hash"],
            "caps": {
                "messages_per_call": 1,
                "recipients_per_message": self.settings.send_max_recipients,
                "daily_sends": self.settings.send_daily_cap,
                "remaining_daily_sends": remaining,
                "window_hours": 24,
            },
        }

    def preview_send(self, draft_id: str, *, form: bool = True) -> dict[str, Any]:
        return self._preview_send(draft_id, form=form)

    def preview_unsubscribe(
        self, draft_id: str, target: dict[str, Any], *, form: bool = True
    ) -> dict[str, Any]:
        """Bind an unsent mailto draft to the exact discovered subscription target."""
        if not isinstance(target, dict):
            raise MailValidationError("Unsubscribe send requires its previewed mailto target")
        return self._preview_send(draft_id, form=form, unsubscribe=target)

    @staticmethod
    def _unsubscribe_target(draft: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
        recipients, subject, body = (target.get(name) for name in ("to", "subject", "body"))
        if (
            not isinstance(recipients, list)
            or not recipients
            or any(not is_bare_address(address) for address in recipients)
            or not isinstance(subject, str)
            or not isinstance(body, str)
        ):
            raise MailValidationError("Unsubscribe send requires its previewed mailto target")
        message = BytesParser(policy=policy.SMTP).parsebytes(draft["wire"])
        if (
            draft["to"] != recipients
            or draft["recipients"] != recipients
            or draft["subject"] != subject
            or any(draft["recipient_headers"][name] is not None for name in ("cc", "bcc"))
            or draft["attachments"]
            or message.is_multipart()
            or message.get_content_type() != "text/plain"
        ):
            raise MailValidationError(
                "Unsubscribe draft does not match the previewed mailto target"
            )
        # Draft creation uses set_content, which adds a terminating line break
        # and serializes line endings as SMTP CRLF. Compare the same transport
        # representation without stripping meaningful body whitespace.
        expected = EmailMessage(policy=policy.SMTP)
        expected.set_content(body)
        expected = BytesParser(policy=policy.SMTP).parsebytes(expected.as_bytes())
        if message.get_content() != expected.get_content():
            raise MailValidationError("Unsubscribe draft does not match the previewed mailto body")
        return {"to": list(recipients), "subject": subject, "body": body}

    def _checked_preview(
        self, draft_id: str, *, form: bool, unsubscribe: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Every preview check and the preview itself, before any token exists."""
        self._enabled()
        self._tls_context()
        draft = self._read(draft_id)
        target = self._unsubscribe_target(draft, unsubscribe) if unsubscribe is not None else None
        remaining = self._budget()
        if not remaining:
            raise MailValidationError("Rolling daily send budget is exhausted")
        result = self._preview(draft, remaining)
        result["approval"] = (
            "token"
            if self.settings.send_self_no_form is True and _own_recipients(draft)
            else "typed_ok"
            if not form and self.settings.send_typed_ok is True
            else "host_confirmation"
        )
        params = {"approval": result["approval"]}
        selector = {"draft_id": draft["identity"]["message_id"]}
        if target is not None:
            params["unsubscribe"] = target
            result.update(method="MAILTO", body=target["body"])
        return draft, result, params, selector

    def _preview_send(
        self, draft_id: str, *, form: bool, unsubscribe: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        draft, result, params, selector = self._checked_preview(
            draft_id, form=form, unsubscribe=unsubscribe
        )
        if result["approval"] == "typed_ok":
            # As before a form: a draft that would refuse at delivery never asks for an OK.
            with self.log.transaction(readonly=True) as transaction:
                self._refuse_repeat(transaction.records(), draft)
        result["token"], result["expires_at"] = self.tokens.issue(
            "send_draft", params, selector, [_binding(draft)], sole=True
        )
        result["action_hash"] = approval_hash("send_draft", params, selector, [_binding(draft)])
        if result["approval"] == "typed_ok":
            result.update(summary=_typed_ok_summary(draft), next_steps=_TYPED_OK_STEPS)
            _warn_typed_ok("preview_send issued a token")
        return result

    def review_send(self, draft_id: str, *, form: bool = True) -> dict[str, Any]:
        """preview_send's exact preview and action_hash, issuing no token.

        For a review view: nothing in the result can be redeemed. body_preview is
        the text of the exact message bytes the preview hashes.
        """
        draft, result, params, selector = self._checked_preview(draft_id, form=form)
        result["action_hash"] = approval_hash("send_draft", params, selector, [_binding(draft)])
        result["body_preview"] = _body_preview(draft["wire"])
        return result

    @staticmethod
    def _check(draft: dict[str, Any], grant: Any) -> None:
        current = approval_hash(grant.action, grant.params, grant.selector, [_binding(draft)])
        if not hmac.compare_digest(current, grant.binding):
            raise _SendRefusal(
                "draft_changed", "Draft changed after preview; request a new preview"
            )

    @staticmethod
    def _check_approval(draft: dict[str, Any], grant: Any) -> None:
        if grant.params["approval"] == "token":
            if not _own_recipients(draft):
                raise _SendRefusal(
                    "recipients_changed", "Token approval recipient mismatch; request a new preview"
                )
        elif grant.params["approval"] not in {"host_confirmation", "typed_ok"}:
            raise _SendRefusal("token_invalid", "Invalid approval path; request a new preview")

    @staticmethod
    def _check_named_draft(grant: Any, draft_id: str | None) -> None:
        """Refuse a token from another draft. The name only refuses: the token picks the draft."""
        if draft_id is None:
            if grant.params["approval"] == "typed_ok":
                raise _SendRefusal(
                    "draft_mismatch", "A typed OK send needs draft_id; request a new preview"
                )
        elif _draft_message_id(draft_id) != grant.selector["draft_id"]:
            raise _SendRefusal(
                "draft_mismatch", "This token came from another draft; request a new preview"
            )

    def _record(self, draft: dict[str, Any] | None, result: str) -> dict[str, Any]:
        identity = draft["identity"] if draft else {}
        record = new_record("send_draft", {})
        record.update(
            timestamp=datetime.fromtimestamp(self.clock(), UTC).isoformat(),
            draft_hash=identity.get("draft_hash"),
            subject_hash=identity.get("subject_hash"),
            recipient_domains=identity.get("recipient_domains", []),
            recipient_count=identity.get("recipient_count", 0),
            message_id=identity.get("message_id"),
            result=result,
            budget_reserved=False,
        )
        return record

    def _audit(self, draft: dict[str, Any] | None, result: str) -> None:
        with self.log.transaction() as transaction:
            transaction.append(self._record(draft, result))

    def _fresh(self, grant: Any) -> dict[str, Any]:
        anyio.from_thread.check_cancelled()
        try:
            draft = self._read(grant.selector["draft_id"])
        except MailValidationError:
            raise _SendRefusal("draft_changed", "Draft changed; request a new preview") from None
        self._check(draft, grant)
        self._check_approval(draft, grant)
        if self.tokens.clock() >= grant.expires:
            raise _SendRefusal("token_expired", "Preview token expired; request a new preview")
        anyio.from_thread.check_cancelled()
        return draft

    def _deliver(self, draft: dict[str, Any], grant: Any, approval: str) -> dict[str, Any]:
        # Keep this worker joined. Cooperative checkpoints see the request's
        # cancellation scope even while run_sync shields its wait for the worker.
        anyio.from_thread.check_cancelled()
        context = self._tls_context()
        try:
            credentials = self.client.credentials_provider(self.settings)
        except Exception:
            raise MailError("Bridge credentials are unavailable") from None
        anyio.from_thread.check_cancelled()
        with self.log.transaction() as transaction:
            records = transaction.records()
            self._refuse_repeat(records, draft)
            if not self._remaining(records):
                raise MailValidationError("Rolling daily send budget is exhausted")
            if self.tokens.clock() >= grant.expires:
                raise _SendRefusal("token_expired", "Preview token expired; request a new preview")
            intent = self._record(draft, "reserved")
            intent.update(budget_reserved=True, approval=approval)
            # An SMTP disconnect may follow acceptance. Reserve durably before
            # connecting and never refund uncertain outcomes, including crashes.
            transaction.append(intent)
            connection = None
            result = "smtp_error"
            data_started = False
            uncertain = False
            try:
                try:
                    if self.settings.smtp_tls:
                        connection = smtplib.SMTP_SSL(
                            self.settings.smtp_host,
                            self.settings.smtp_port,
                            local_hostname="localhost",
                            timeout=self.settings.timeout,
                            context=context,
                        )
                    else:
                        connection = smtplib.SMTP(
                            self.settings.smtp_host,
                            self.settings.smtp_port,
                            local_hostname="localhost",
                            timeout=self.settings.timeout,
                        )
                        connection.ehlo()
                        connection.starttls(context=context)
                    verify_certificate(connection, self.settings)
                    connection.ehlo()
                    connection.login(credentials.username, credentials.password)
                    # Split sendmail at DATA: MAIL and RCPT can also block while
                    # the host cancels or the saved draft changes.
                    options = []
                    if connection.does_esmtp and connection.has_extn("size"):
                        options.append(f"size={len(draft['wire'])}")
                    code, response = connection.mail(draft["from"], options)
                    if code != 250:
                        raise smtplib.SMTPSenderRefused(code, response, draft["from"])
                    refused = {}
                    accepted = 0
                    for address in draft["recipients"]:
                        code, response = connection.rcpt(address)
                        if code in (250, 251):
                            accepted += 1
                        else:
                            refused[address] = (code, response)
                        if code == 421:
                            raise smtplib.SMTPRecipientsRefused(refused)
                    if not accepted:
                        raise smtplib.SMTPRecipientsRefused(refused)
                    # No blocking preparation remains between this fresh
                    # serialization/hash/cancellation check and SMTP DATA.
                    draft = self._fresh(grant)
                    data_started = uncertain = True
                    code, response = connection.data(draft["wire"])
                    if code != 250:
                        raise smtplib.SMTPDataError(code, response)
                    result = "partial" if refused else "sent"
                    uncertain = False
                except Exception as error:
                    if not data_started:
                        anyio.from_thread.check_cancelled()
                    if isinstance(error, (MailError, MailValidationError)):
                        raise
                    # SMTP diagnostics can embed addresses and credentials. Nothing
                    # is retried automatically. Only an explicit 4xx or 5xx reply
                    # says DATA was not accepted, so only it leaves a fresh preview
                    # free to retry: a lost, malformed or other reply stays uncertain.
                    if isinstance(error, smtplib.SMTPDataError) and 400 <= error.smtp_code < 600:
                        uncertain = False
            except BaseException as error:
                result = (
                    error.result
                    if isinstance(error, _SendRefusal)
                    else "refused"
                    if isinstance(error, Exception)
                    else "cancelled"
                )
                raise
            finally:
                if connection is not None:
                    if not data_started:
                        with suppress(Exception):
                            connection.rset()
                        with suppress(Exception):
                            connection.quit()
                    with suppress(Exception):
                        connection.close()
                outcome = self._record(draft, result)
                outcome.update(
                    action="send_result", send_log_id=intent["log_id"], uncertain=uncertain
                )
                transaction.append(outcome)
            return {
                "log_id": intent["log_id"],
                "message_id": draft["identity"]["message_id"],
                "draft_hash": draft["identity"]["draft_hash"],
                "result": result,
                "sent": result == "sent",
                "uncertain": uncertain,
                "token_consumed": True,
                "remaining_daily_sends": self._remaining(transaction.records()),
            }

    async def send_draft(self, token: str, ctx: Any, draft_id: str | None = None) -> dict[str, Any]:
        draft = None
        result = "refused"
        confirmation_pending = False
        delivered = None
        try:
            try:
                self._enabled()
                # Claim before requesting confirmation. Every failure requires
                # a fresh preview; concurrent redemption cannot share approval.
                try:
                    grant = self.tokens.claim(token, "send_draft")
                except MailValidationError as error:
                    raise _SendRefusal(
                        "token_expired"
                        if str(error).startswith("Preview token expired")
                        else "token_invalid",
                        "Preview token invalid, expired or used; request a new preview",
                    ) from None
                draft = {"identity": grant.messages[0]}
                self._check_named_draft(grant, draft_id)
                # A typed OK needs the switch on and a client that declared no form or is listed,
                # whichever session previewed. Any other client takes the form, which refuses
                # without one.
                approval = grant.params["approval"]
                if approval == "typed_ok" and not (
                    self.settings.send_typed_ok is True
                    and declares_no_form(ctx, self.settings.no_form_clients)
                ):
                    approval = "host_confirmation"
                if approval == "host_confirmation":
                    # The form must show the current bytes and a live budget.
                    draft = await anyio.to_thread.run_sync(lambda: self._fresh(grant))
                    remaining = await anyio.to_thread.run_sync(lambda: self._form_budget(draft))
                    if not remaining:
                        raise MailValidationError("Rolling daily send budget is exhausted")
                    preview = self._preview(draft, remaining)
                    if "unsubscribe" in grant.params:
                        preview.update(method="MAILTO", body=grant.params["unsubscribe"]["body"])
                    summary = (
                        "Send exactly this draft? "
                        + _form_sender_and_files(preview)
                        + " Review the From address, recipients, subject and attachments. "
                        "This sends one message and cannot be undone. SHA-256 prefix: "
                        + preview["draft_hash"][:16]
                        + "\n"
                        + json.dumps(preview, ensure_ascii=True, sort_keys=True)
                    )
                    confirmation_pending = True
                    await require_confirmation(
                        ctx,
                        summary,
                        timeout=self.settings.send_confirm_timeout,
                        no_form_clients=self.settings.no_form_clients,
                    )
                    confirmation_pending = False
                draft = await anyio.to_thread.run_sync(lambda: self._fresh(grant))
                if approval == "typed_ok":
                    log_list_use(ctx, self.settings.no_form_clients)
                    _warn_typed_ok("send_draft is sending with no form")
                delivery_error = None

                async def deliver() -> None:
                    nonlocal delivered, delivery_error
                    try:
                        delivered = await anyio.to_thread.run_sync(
                            lambda: self._deliver(draft, grant, approval)
                        )
                    except Exception as error:
                        delivery_error = error

                # A native task cancellation must also cancel the worker's
                # AnyIO scope and join it before the request can finish.
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(deliver)
                if delivery_error is not None:
                    raise delivery_error
            except ConfirmationError as error:
                result = error.result
                error_message = str(error)
            except _SendRefusal as error:
                result = error.result
                error_message = str(error)
            except MailValidationError as error:
                error_message = str(error)
            except Exception:
                result = "error"
                error_message = "Guarded send failed; no automatic retry is permitted"
            # A cancelled MCP request already has its protocol response. Even a
            # concurrent backend error or completed DATA must not send another.
            await anyio.lowlevel.checkpoint_if_cancelled()
        except anyio.get_cancelled_exc_class():
            if delivered is None:
                result = "confirmation_cancelled" if confirmation_pending else "cancelled"
                # An unavailable journal cannot turn protocol cancellation into
                # a second response to the request already completed by MCP.
                with anyio.CancelScope(shield=True), suppress(Exception):
                    await anyio.to_thread.run_sync(lambda: self._audit(draft, result))
            raise
        if delivered is not None:
            return delivered
        # Shield the refusal journal from client cancellation. A failed append
        # also refuses; no path from here reaches SMTP.
        try:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(lambda: self._audit(draft, result))
        finally:
            await anyio.lowlevel.checkpoint_if_cancelled()
        raise MailValidationError(error_message) from None
