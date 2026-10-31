"""Single-use unsubscribe approvals for HTTPS or an unsent, guarded-send draft."""

from __future__ import annotations

import hashlib
from email import policy
from email.parser import BytesParser, HeaderParser
from typing import Any

from . import mail
from . import unsubscribe_http as http
from .action_log import ActionLog, header_digest, new_record, token_digest
from .canonical import mailbox_roles, resolve_samples
from .cleanup_folders import _move_folders
from .cleanup_lookup import PreviewExclusions, raw_message_id
from .guarded import _digest, _selector, approval_hash
from .mail import (
    CallDeadlineExceeded,
    MailError,
    MailValidationError,
    _checked,
    _Deadline,
    _literal,
    _mime_parts,
    _structure,
    _text_body,
)

# Bound fallback discovery independently of the much smaller display/snippet
# budgets. Newsletter unsubscribe links commonly appear after long HTML bodies.
MAX_HTML_BYTES = 1024 * 1024


class _UnsubscribeIdentityChanged(MailValidationError):
    """A new preview can exclude this identity; execution still refuses it."""


class UnsubscribeService:
    def __init__(self, cleanup):
        self.cleanup = cleanup
        self.client = cleanup.client
        self.settings = cleanup.settings

    def _details(self, connection, deadline, row):
        deadline.check(connection)
        self.client._select(connection, row["folder"])
        raw_headers = self.client._raw_headers(connection, row["uid"])
        headers = BytesParser(policy=policy.default).parsebytes(raw_headers)

        def refuse_identity(message):
            # A policy failure remains a whole-preview refusal even when the
            # same freshly fetched headers also contradict the source identity.
            sender_headers = {
                key: str(headers[name])
                for key, name in (("from", "From"), ("sender", "Sender"), ("reply_to", "Reply-To"))
                if name in headers
            }
            protected = self.cleanup.policy.check(sender_headers) or any(
                len(headers.get_all(name, [])) > 1 for name in ("From", "Sender", "Reply-To")
            )
            error = MailValidationError if protected else _UnsubscribeIdentityChanged
            raise error(message) from None

        try:
            identifier = raw_message_id(headers)
        except MailValidationError:
            identifier = None
        if identifier != row["message_id"]:
            refuse_identity("Unsubscribe message identity changed; preview again")
        try:
            actual = headers["From"]
            expected = HeaderParser(policy=policy.default).parsestr(f"From: {row['from']}\n\n")[
                "From"
            ]
            if (
                actual is None
                or actual.defects
                or expected.defects
                or actual.addresses != expected.addresses
            ):
                raise ValueError
        except (AttributeError, IndexError, ValueError):
            refuse_identity("Unsubscribe sender identity changed; preview again")
        details = http.extract(headers)
        try:
            self._select_target(row, details)
            header_usable = True
        except http.UnsubscribeRefused:
            header_usable = False
        partial = False
        # Approval discovery needs no body when the independently fetched headers
        # provide a permitted method.
        if not header_usable:
            deadline.check(connection)
            structure = _structure(_checked(connection.uid("FETCH", row["uid"], "(BODYSTRUCTURE)")))
            htmls, remaining = [], MAX_HTML_BYTES
            for part in _mime_parts(structure):
                if part["attachment"] or part["type"] != "text/html":
                    continue
                if part["size"] > remaining:
                    partial = True
                if remaining == 0:
                    break
                deadline.check(connection)
                raw = _literal(
                    _checked(
                        connection.uid(
                            "FETCH", row["uid"], f"(BODY.PEEK[{part['section']}]<0.{remaining}>)"
                        )
                    )
                )[:remaining]
                remaining -= len(raw)
                htmls.append(_text_body(raw, part, partial=True, strip_html=False))
            details = http.extract(headers, htmls)
        # Bind exact fetched bytes, including every List-Unsubscribe/Post field
        # in order. Parsed raw_items() still loses colon whitespace and line ends.
        details["headers_digest"] = hashlib.sha256(raw_headers).hexdigest()
        # Bind fetched headers and any scanned body links to the approval. A usable
        # header target deliberately leaves the unread body outside that binding.
        details["partial"] = partial
        sender_headers = {
            key: str(headers[name])
            for key, name in (("from", "From"), ("sender", "Sender"), ("reply_to", "Reply-To"))
            if name in headers
        }
        details["sender_protected"] = bool(self.cleanup.policy.check(sender_headers)) or any(
            len(headers.get_all(name, [])) > 1 for name in ("From", "Sender", "Reply-To")
        )
        return details

    def _select_target(self, row, details):
        return http.select_target(
            row["from"],
            details,
            enable_send=self.settings.enable_send,
            send_max_recipients=self.settings.send_max_recipients,
        )

    def _rows(self, connection, deadline, selector, *, exclusions=None):
        folders = self.client._folders(connection)
        return self.cleanup._resolve(
            connection, deadline, selector, "unsubscribe", {}, folders, exclusions=exclusions
        )

    def _prepare(self, connection, deadline, rows, real, *, exclusions=None):
        """Refuse protected or unusable targets; ``real`` renders the blocked samples."""
        blocked = self.cleanup._blocked(rows, real)
        if blocked or len(rows) > self.settings.unsubscribe_max_batch:
            return blocked
        for row in rows:
            try:
                details = self._details(connection, deadline, row)
            except _UnsubscribeIdentityChanged:
                if exclusions is None:
                    raise
                exclusions.add(row["message_id"], "unsubscribe_identity_changed")
                continue
            try:
                if details["sender_protected"]:
                    raise http.UnsubscribeRefused(
                        "Unsubscribe message has protected sender headers"
                    )
                try:
                    row["unsubscribe_target"] = self._select_target(row, details)
                except http.UnsubscribeRefused:
                    if details["partial"]:
                        raise http.UnsubscribeRefused(
                            "No permitted unsubscribe method found in headers or bounded HTML "
                            f"scan ({MAX_HTML_BYTES} bytes; body was truncated)"
                        ) from None
                    raise
                row["unsubscribe_details_digest"] = _digest(details).hex()
            except http.UnsubscribeRefused as error:
                blocked.append({**self.cleanup._sample(row, real), "reason": str(error)})
        return blocked

    def preview(self, query=None, ids=None, params=None) -> dict[str, Any]:
        if params not in (None, {}):
            raise MailValidationError("Unsubscribe does not accept params")
        selector = _selector(query, ids)
        exclusions = PreviewExclusions()
        result = {
            **exclusions.response(),
            "action": "unsubscribe",
            "count": 0,
            "samples": [],
            "blocked": [],
            "token": None,
            "expires_at": None,
            "action_hash": None,
            "partial": False,
            "total_is_exact": True,
            "personal_provider_bypass_count": 0,
        }
        budget = self.settings.preview_timeout or self.settings.call_timeout
        started = mail.time.monotonic()
        deadline = _Deadline(budget, self.settings.timeout)
        try:
            with self.client._session(deadline=deadline) as (connection, _):
                rows = self._rows(connection, deadline, selector, exclusions=exclusions)
                # Samples take real folders and All Mail from LIST, as search does.
                _, real, all_mail = mailbox_roles(connection)
                result["blocked"] = self._prepare(
                    connection, deadline, rows, real, exclusions=exclusions
                )
                rows = [
                    row
                    for row in rows
                    if row["message_id"] not in exclusions.reasons
                    or self.cleanup.policy.check(row)
                    or row.get("membership_protection")
                ]
                result.update(
                    **exclusions.response(),
                    count=len(rows),
                    samples=[self.cleanup._sample(row, real) for row in rows[:10]],
                    personal_provider_bypass_count=sum(
                        bool(row.get("personal_provider_bypasses")) for row in rows
                    ),
                )
                if not rows and exclusions.reasons:
                    result["reason"] = "All candidates were excluded; no cleanup token issued"
                elif len(rows) > self.settings.unsubscribe_max_batch:
                    result["reason"] = (
                        "Unsubscribe batch exceeds the cap of "
                        f"{self.settings.unsubscribe_max_batch}"
                    )
                elif rows and not result["blocked"]:
                    deadline.check(connection)
                    clean_selector = selector
                    if exclusions.reasons:
                        clean_ids = sorted(row["message_id"] for row in rows)
                        clean_selector = (
                            {**selector, "candidate_ids": clean_ids}
                            if "query" in selector
                            else {"ids": clean_ids}
                        )
                    result["token"], result["expires_at"] = self.cleanup.tokens.issue(
                        "unsubscribe", {}, clean_selector, rows
                    )
                    result["action_hash"] = approval_hash("unsubscribe", {}, clean_selector, rows)
                    # Mailto content must be visible before approving a draft.
                    # HTTP query secrets still stay out of the preview.
                    result["targets"] = [preview_target(row["unsubscribe_target"]) for row in rows]
                # Display only: name a label or All Mail source by its real folder.
                if resolve_samples(
                    self.client,
                    connection,
                    deadline,
                    result["samples"] + result["blocked"],
                    real=real,
                    all_mail=all_mail,
                    started=started,
                    budget=budget,
                ):
                    result["partial"] = True
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
                reason="Unsubscribe preview timed out",
            )
        return self.cleanup._log_preview("unsubscribe", result)

    def execute(self, token: str) -> dict[str, Any]:
        with self.cleanup._exclusive():
            grant = self.cleanup.tokens.inspect(token, "unsubscribe")
            if not 1 <= len(grant.messages) <= self.settings.unsubscribe_max_batch:
                raise MailValidationError("Unsubscribe batch exceeds the configured cap")
            deadline = self.client._deadline(execute=True)
            with self.client._session(deadline=deadline) as (connection, _):
                rows = self._rows(connection, deadline, grant.selector)
                if self._prepare(connection, deadline, rows, _move_folders(connection)):
                    raise MailValidationError("Unsubscribe contains protected or refused targets")
                self.cleanup._check_execution_rows(rows, grant.messages, grant.fingerprint)
            # Close the mailbox session before making any HTTP request. One shared
            # execution budget covers the read-only revalidation and every request.
            deadline.check()
            return self._perform(rows, token, deadline)

    def _perform(self, rows, token, deadline):
        drafts = []
        record = new_record("unsubscribe", {})
        record.update(
            status="in_progress",
            token_digest=token_digest(token),
            messages=[
                {
                    "message_id_digest": _digest(row["message_id"]).hex(),
                    "header_digest": header_digest(row),
                    "url_digest": _digest(row["unsubscribe_target"]["url"]).hex(),
                    "method": row["unsubscribe_target"]["method"],
                    "status": "not_attempted",
                }
                for row in rows
            ],
        )
        with ActionLog(self.settings.action_log_path).transaction() as transaction:
            transaction.records()
            deadline.check()
            transaction.append(record)
            self.cleanup.tokens.claim(token, "unsubscribe")
            for index, row in enumerate(rows):
                entry = record["messages"][index]
                try:
                    timeout = min(5.0, deadline.check())
                except CallDeadlineExceeded:
                    break
                entry["status"] = "uncertain"
                progress = {
                    **new_record("execute_progress", {}),
                    "action_log_id": record["log_id"],
                    "index": index,
                    "message": entry,
                }
                # Durable intent precedes HTTP or APPEND, including ambiguous timeouts.
                transaction.append(progress)
                try:
                    target = row["unsubscribe_target"]
                    if target["method"] == "MAILTO":
                        # This never sends: send_draft needs its own preview token
                        # and retains the existing host/own-address approval.
                        draft_id = self.client.create_draft(
                            to=target["to"],
                            subject=target["subject"],
                            body=target["body"],
                            _deadline=deadline,
                        )
                        entry["draft_id"] = draft_id
                        entry["status"] = "pending_send"
                        drafts.append(
                            {
                                "draft_id": draft_id,
                                **{key: target[key] for key in ("method", "to", "subject", "body")},
                            }
                        )
                    else:
                        entry["http_status"] = http.perform(target, timeout=timeout)
                        entry["status"] = "done"
                except (
                    http.UnsubscribeRefused,
                    http.UnsubscribeHTTPError,
                    MailError,
                    MailValidationError,
                ):
                    # A failed exchange may have changed the subscription or saved
                    # a draft. Never retry automatically or expose remote diagnostics.
                    transaction.append(progress)
                    break
                transaction.append(progress)
            done = sum(entry["status"] == "done" for entry in record["messages"])
            partial = done + len(drafts) != len(rows)
            status = "partial" if partial else "pending_send" if drafts else "completed"
            transaction.append(
                {
                    **new_record("execute_result", {}),
                    "action_log_id": record["log_id"],
                    "partial": partial,
                    "status": status,
                }
            )
        return {
            "log_id": record["log_id"],
            "count": done,
            "partial": partial,
            "status": status,
            "token_consumed": True,
            "messages": record["messages"],
            **(
                {
                    "drafts": drafts,
                    "pending_send": len(drafts),
                    "reason": (
                        "Mailto drafts are unsent; review each send preview before send_draft"
                    ),
                }
                if drafts
                else {}
            ),
        }


def preview_target(target):
    """Show exactly what a mailto approval prepares, never an HTTP query secret."""
    result = {key: target[key] for key in ("method", "host")}
    if target["method"] == "MAILTO":
        result.update(
            **{key: target[key] for key in ("to", "subject", "body")},
            requires_send_approval=True,
        )
    return result
