"""Previewed creation of real folders; existing names leave approvals unconsumed."""

from __future__ import annotations

from .action_log import new_record, token_digest
from .cleanup_folders import _real_folder_name
from .guarded import CleanupService, approval_hash
from .mail import MailValidationError, _checked, _decode_folder, _ImapValues, _mailbox


def _folder_name(name: str) -> str:
    if not isinstance(name, str) or not name.startswith("Folders/") or not _real_folder_name(name):
        raise MailValidationError(
            "Folder names must be valid paths under Folders/ (at most 500 characters)"
        )
    try:
        _mailbox(name)
    except UnicodeError:
        raise MailValidationError("Folder names must contain valid Unicode") from None
    return name


def _exists(connection, name: str) -> bool:
    # Include nonselectable parents: any existing mailbox is a creation no-op.
    names = set()
    for item in _checked(connection.list()):
        if item is None:
            continue
        if isinstance(item, tuple):
            item = item[0] + b"\r\n" + item[1]
        parser = _ImapValues(item)
        parser.value()
        parser.value()
        names.add(_decode_folder(parser.value()))
    return name in names


def create_approved_folder(cleanup, connection, deadline, name, context, *, authorize):
    """Create a move approval's missing destination before its first move.

    The caller has verified the approved moves. Like create_folder, the intent
    record precedes the claim (authorize: final headroom check and token claim)
    and an uncertain progress line precedes CREATE. The run starts before the
    folder exists, so the creation is recoverable with the run's token digest.
    It names that run as its parent_run_id, never run_id: it is not a chunk.
    An existing folder is a no-op. Never deletes or renames.
    """
    if _exists(connection, name):
        return {
            "name": name,
            "created": False,
            "exists": True,
            "ready": True,
            "uncertain": False,
        }
    log = cleanup.backend.log
    with log.transaction() as transaction:
        transaction.records(include_runs=True)
        record = new_record("create_folder", {"name": name})
        record["token_digest"] = context["token_digest"]
        record["parent_run_id"] = context["run_id"]
        record["status"] = "in_progress"
        entry = {"folder": name, "status": "not_attempted", "result": "pending"}
        record["messages"] = [entry]
        transaction.append(record)

        def progress():
            transaction.append(
                {
                    "action": "execute_progress",
                    "action_log_id": record["log_id"],
                    "index": 0,
                    "message": entry,
                }
            )

        def finish():
            try:
                transaction.append(
                    {
                        "action": "execute_result",
                        "action_log_id": record["log_id"],
                        "partial": record["partial"],
                    }
                )
            except Exception:
                record["partial"] = True

        try:
            authorize()
        except Exception:
            # Refused before any change: the token stays as authorize left it.
            record["partial"] = True
            finish()
            raise
        try:
            log.ensure_run(transaction, context)
            entry.update(status="uncertain", result="uncertain")
            progress()
            deadline.check(connection)
            _checked(connection.create(_mailbox(name)))
            entry.update(status="done", result="created")
            progress()
        except Exception:
            record["partial"] = True
        finish()
    ready = entry["status"] == "done"
    if ready:
        try:
            # The move needs a selectable folder, as validate_target checks.
            ready = name in cleanup.client._folders(connection)
        except Exception:
            ready = False
    return {
        "name": name,
        "log_id": record["log_id"],
        "created": entry["result"] == "created",
        "exists": False,
        "ready": ready,
        # CREATE was sent without a confirmed reply: inspect the folder list.
        "uncertain": entry["status"] == "uncertain",
    }


class CreateFolderService:
    def __init__(self, cleanup: CleanupService):
        self.cleanup = cleanup

    def preview_create_folder(self, name: str) -> dict:
        name = _folder_name(name)
        cleanup = self.cleanup
        deadline = cleanup.client._deadline()
        with cleanup.client._session(deadline=deadline) as (connection, _):
            exists = _exists(connection, name)
            deadline.check(connection)
        token, expires, action_hash = (None, None, None)
        if not exists:
            token, expires = cleanup.tokens.issue("create_folder", {"name": name}, {}, [])
            action_hash = approval_hash("create_folder", {"name": name}, {}, [])
        return {
            "action": "create_folder",
            "name": name,
            "exists": exists,
            "token": token,
            "expires_at": expires,
            "action_hash": action_hash,
        }

    def create_folder(self, token: str) -> dict:
        cleanup = self.cleanup
        with cleanup._exclusive():
            grant = cleanup.tokens.inspect(token, "create_folder")
            name = _folder_name(grant.params.get("name"))
            if grant.params != {"name": name} or grant.messages or grant.selector:
                raise MailValidationError("Preview token does not match folder creation")
            deadline = cleanup.client._deadline(execute=True)
            with cleanup.backend.log.transaction() as transaction:
                transaction.records()
                with cleanup.client._session(deadline=deadline) as (connection, _):
                    exists = _exists(connection, name)
                    record = new_record("create_folder", {"name": name})
                    record["token_digest"] = token_digest(token)
                    record["status"] = "completed" if exists else "in_progress"
                    entry = {"folder": name, "status": "done" if exists else "not_attempted"}
                    entry["result"] = "exists" if exists else "pending"
                    record["messages"] = [entry]
                    transaction.append(record)
                    if exists:
                        return self._result(record, exists=True, consumed=False)

                    def progress():
                        transaction.append(
                            {
                                "action": "execute_progress",
                                "action_log_id": record["log_id"],
                                "index": 0,
                                "message": entry,
                            }
                        )

                    claimed = False
                    try:
                        deadline.check(connection)
                        cleanup.tokens.claim(token, "create_folder", params={"name": name})
                        claimed = True
                        entry.update(status="uncertain", result="uncertain")
                        progress()
                        deadline.check(connection)
                        _checked(connection.create(_mailbox(name)))
                        entry.update(status="done", result="created")
                        progress()
                    except Exception:
                        record["partial"] = True
                    try:
                        transaction.append(
                            {
                                "action": "execute_result",
                                "action_log_id": record["log_id"],
                                "partial": record["partial"],
                            }
                        )
                    except Exception:
                        record["partial"] = True
                    return self._result(record, exists=False, consumed=claimed)

    @staticmethod
    def _result(record: dict, *, exists: bool, consumed: bool) -> dict:
        result = {
            "log_id": record["log_id"],
            "name": record["params"]["name"],
            "exists": exists,
            "created": record["messages"][0]["result"] == "created",
            "partial": record["partial"],
            "token_consumed": consumed,
        }
        if record["partial"]:
            result["error"] = "Folder creation interrupted; inspect the action log and Proton app"
        return result
