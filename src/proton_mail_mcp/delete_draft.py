"""Previewed, irreversible deletion of exact UIDs in the Drafts mailbox only."""

from __future__ import annotations

from copy import copy

from .action_log import header_digest, new_record, token_digest
from .cleanup_folders import _folder_attributes
from .cleanup_imap import _capabilities, _handle_identity, _uid_set
from .cleanup_lookup import fetch_rows, message_fingerprint
from .guarded import MAX_BATCH, _selector, approval_hash
from .mail import MailValidationError, _checked, _Deadline, _mailbox
from .protected import ProtectedPolicy
from .search import _uidvalidity
from .send_address import parse_draft_addresses

_REFUSAL = (
    "Draft deletion refused: every requested message must currently be in Drafts; "
    "no messages changed"
)
_CONFLICTING = frozenset(
    {
        r"\noselect",
        r"\nonexistent",
        r"\all",
        r"\flagged",
        r"\sent",
        r"\trash",
        r"\junk",
        r"\archive",
    }
)


class _OwnDraftPolicy:
    """Exempt only the authenticated author's provider, within verified Drafts."""

    def __init__(self, original, username, folder):
        self.original = original
        self.folder = folder
        try:
            self.author = ProtectedPolicy._allowlist_entry(username)
        except (ValueError, TypeError, AttributeError):
            self.author = None
        self.scoped = copy(original)
        self.scoped.rules = dict(original.rules)
        self.scoped.rules["personal_provider_allowlist"] = tuple(
            dict.fromkeys(
                [
                    *original.rules["personal_provider_allowlist"],
                    *([self.author] if self.author else []),
                ]
            )
        )

    def evaluate(self, row):
        try:
            authors = [
                ProtectedPolicy._allowlist_entry(address)
                for address in parse_draft_addresses(row.get("from"))
            ]
        except (ValueError, TypeError, AttributeError, IndexError, LookupError):
            authors = []
        policy = (
            self.scoped
            if self.author and row.get("folder") == self.folder and authors == [self.author]
            else self.original
        )
        return policy.evaluate(row)

    def check(self, row):
        return self.evaluate(row)[0]

    def allowlist_digest(self):
        return self.scoped.allowlist_digest()


class DeleteDraftService:
    def __init__(self, cleanup):
        self.cleanup = cleanup
        self.client = cleanup.client

    def _scoped_cleanup(self, folder, username):
        # Share tokens, the journal and the mutation lock, while keeping this
        # action's sender exception out of every other cleanup tool. Resolver
        # membership checks and all execute rechecks use the same scoped policy.
        cleanup = copy(self.cleanup)
        cleanup.policy = _OwnDraftPolicy(cleanup.policy, username, folder)
        return cleanup

    def _drafts(self, connection):
        attributes = _folder_attributes(connection)
        special = [name for name, flags in attributes.items() if r"\drafts" in flags]
        if len(special) > 1:
            raise MailValidationError("Drafts mailbox is ambiguous; no messages changed")
        folder = special[0] if special else self.cleanup.settings.drafts_folder
        if folder not in attributes or attributes[folder].intersection(_CONFLICTING):
            raise MailValidationError(
                "Drafts mailbox must exist and be selectable without conflicting flags"
            )
        _mailbox(folder)
        if "UIDPLUS" not in _capabilities(connection):
            raise MailValidationError("Draft deletion requires UIDPLUS for scoped UID EXPUNGE")
        return folder

    def preview(self, query=None, ids=None, params=None):
        if params not in (None, {}):
            raise MailValidationError("Draft deletion does not accept params")
        selector = _selector(query, ids)
        cleanup = self.cleanup
        deadline = _Deadline(
            cleanup.settings.preview_timeout or cleanup.settings.call_timeout,
            cleanup.settings.timeout,
        )
        with self.client._session(deadline=deadline) as (connection, credentials):
            folder = self._drafts(connection)
            cleanup = self._scoped_cleanup(folder, credentials.username)
            if "query" in selector:
                if selector["query"].get("folder", folder) != folder:
                    raise MailValidationError(_REFUSAL)
                selector["query"]["folder"] = folder
            else:
                # Resolve all requested IDs strictly within Drafts, including renamed
                # special-use mailboxes outside the ordinary real-folder names.
                selector = {"query": {"folder": folder}, "candidate_ids": selector["ids"]}
            rows = cleanup._resolve(connection, deadline, selector, "delete_draft", {}, [folder])
            if "candidate_ids" in selector and {row["message_id"] for row in rows} != set(
                selector["candidate_ids"]
            ):
                raise MailValidationError(_REFUSAL)
            if len(rows) > MAX_BATCH:
                raise MailValidationError(
                    "Draft deletion batches must contain at most 200 messages"
                )
            result = {
                "action": "delete_draft",
                "target": folder,
                "count": len(rows),
                "samples": [cleanup._sample(row, {folder}) for row in rows[:10]],
                "blocked": cleanup._blocked(rows, {folder}),
                "token": None,
                "expires_at": None,
                "action_hash": None,
                "partial": False,
                "total_is_exact": True,
                "excluded_count": 0,
                "personal_provider_bypass_count": sum(
                    bool(row.get("personal_provider_bypasses")) for row in rows
                ),
                "undo_supported": False,
            }
            if rows and not result["blocked"]:
                deadline.check(connection)
                params = {"folder": folder}
                result["token"], result["expires_at"] = cleanup.tokens.issue(
                    "delete_draft", params, selector, rows
                )
                result["action_hash"] = approval_hash("delete_draft", params, selector, rows)
        return cleanup._log_preview("delete_draft", result)

    def execute(self, token):
        cleanup = self.cleanup
        with cleanup._exclusive():
            grant = cleanup.tokens.inspect(token, "delete_draft")
            if not 1 <= len(grant.messages) <= MAX_BATCH:
                raise MailValidationError("Draft deletion batches must contain 1 to 200 messages")
            deadline = self.client._deadline(execute=True)
            with cleanup.backend.log.transaction() as transaction:
                transaction.records()
                with self.client._session(deadline=deadline) as (connection, credentials):
                    folder = self._drafts(connection)
                    cleanup = self._scoped_cleanup(folder, credentials.username)
                    if grant.params != {"folder": folder} or any(
                        row["folder"] != folder for row in grant.messages
                    ):
                        raise MailValidationError(_REFUSAL)
                    rows = cleanup._resolve(
                        connection,
                        deadline,
                        grant.selector,
                        "delete_draft",
                        {},
                        [folder],
                        bound_rows=grant.messages,
                    )
                    cleanup._check_execution_rows(rows, grant.messages, grant.fingerprint)
                    uids = [row["uid"] for row in rows]
                    uid_set = _uid_set(uids)
                    # Recheck every approved handle after writable SELECT, before
                    # claiming the token or setting any flag in the entire batch.
                    _checked(connection.select(_mailbox(folder), readonly=False))
                    validity = _uidvalidity(connection)
                    if any(row["uidvalidity"] != validity for row in rows):
                        raise MailValidationError("Drafts UIDVALIDITY changed; no messages changed")
                    fresh = fetch_rows(self.client, connection, deadline, folder, uids, validity)
                    if sorted(map(_handle_identity, fresh)) != sorted(map(_handle_identity, rows)):
                        raise MailValidationError(_REFUSAL)
                    previous = {row["uid"]: row for row in rows}
                    if any(
                        cleanup._block_reasons(row)
                        or message_fingerprint(row) != message_fingerprint(previous[row["uid"]])
                        or header_digest(row) != header_digest(previous[row["uid"]])
                        or row.get("internaldate") != previous[row["uid"]].get("internaldate")
                        for row in fresh
                    ):
                        raise MailValidationError(
                            "Draft changed after preview; no messages changed"
                        )
                    record = new_record("delete_draft", {"folder": folder})
                    record.update(
                        token_digest=token_digest(token),
                        status="in_progress",
                        messages=[
                            {
                                **cleanup.backend._row_record(row, ""),
                                "personal_provider_bypasses": row.get(
                                    "personal_provider_bypasses", []
                                ),
                            }
                            for row in rows
                        ],
                    )
                    transaction.append(record)
                    deadline.check(connection)
                    cleanup.tokens.claim(token, "delete_draft", params={"folder": folder})
                    return self._delete(transaction, connection, deadline, record, uids, uid_set)

    def _delete(self, transaction, connection, deadline, record, uids, uid_set):
        def progress(status, stage):
            for index, entry in enumerate(record["messages"]):
                entry.update(status=status, stage=stage)
                transaction.append(
                    {
                        "action": "execute_progress",
                        "action_log_id": record["log_id"],
                        "index": index,
                        "message": entry,
                    }
                )

        try:
            progress("uncertain", "draft_flag")
            deadline.check(connection)
            _checked(connection.uid("STORE", uid_set, "+FLAGS.SILENT", r"(\Deleted)"))
            progress("uncertain", "draft_expunge")
            deadline.check(connection)
            _checked(connection.uid("EXPUNGE", uid_set))
            remaining = self.client._metadata(
                connection,
                uids,
                deadline=deadline,
                headers=False,
                bodystructure=False,
                internaldate=False,
            )
            if _uidvalidity(connection) != record["messages"][0]["uidvalidity_before"] or remaining:
                raise MailValidationError("Draft deletion could not be verified")
            self.client.search_index.remove(
                record["params"]["folder"], record["messages"][0]["uidvalidity_before"], uids
            )
            progress("done", "draft_expunge")
        except Exception:
            record["partial"] = True
            record["error"] = (
                "Draft deletion interrupted; inspect the action log and Proton app before retrying"
            )
        try:
            transaction.append(
                {
                    "action": "execute_result",
                    "action_log_id": record["log_id"],
                    "partial": record["partial"],
                    **({"error": record["error"]} if record["partial"] else {}),
                }
            )
        except Exception:
            record["partial"] = True
            record["error"] = "Draft deletion outcome could not be logged; inspect the Proton app"
        return {
            "action": "delete_draft",
            "log_id": record["log_id"],
            "count": sum(entry["status"] == "done" for entry in record["messages"]),
            "messages": record["messages"],
            "partial": record["partial"],
            "token_consumed": True,
            "undo_supported": False,
            **({"error": record["error"]} if record["partial"] else {}),
        }
