"""Previewed Proton Sieve filter creation: one exact script, one bound token, journaled."""

from __future__ import annotations

import hashlib
from typing import Any

from .action_log import new_record, token_digest
from .cleanup_folders import _folder_attributes, real_folders
from .guarded import CleanupService, approval_hash
from .mail import MailValidationError
from .sieve import (
    SieveFilter,
    UnavailableSieveBackend,
    backend_status,
    filter_spec,
    render_script,
    require_backend,
    script_sha256,
    sieve_folder,
    verify_script,
)

ACTION = "create_sieve_filter"
_SPEC_KEYS = ("name", "folder", "conditions", "match", "mark_read")
_GRANT_KEYS = frozenset({*_SPEC_KEYS, "script", "script_sha256", "backend"})
# A renamed system mailbox keeps its special-use attribute; never file into one.
# real_folders() already excludes the \All, \Flagged and \Noselect views.
_SPECIAL_USE = frozenset({"\\drafts", "\\sent", "\\junk", "\\trash", "\\archive"})


def _require_target(connection: Any, folder: str) -> None:
    """The target must be a listed real folder that no other mailbox name can shadow."""
    attributes = _folder_attributes(connection)
    if folder not in real_folders(attributes) or attributes[folder] & _SPECIAL_USE:
        raise MailValidationError(
            "Sieve filter folder must be an existing real folder; "
            'create it with preview_action(action="create_folder") first'
        )
    # fileinto names a folder or a label by its bare path. Refuse a label, or
    # another folder differing only in case, that the same path could reach.
    path = sieve_folder(folder).casefold()
    for name in attributes:
        prefix, _, rest = name.partition("/")
        if prefix in {"Folders", "Labels"} and rest.casefold() == path and name != folder:
            raise MailValidationError(
                "Another folder or label has the same name; the filter could not tell them apart"
            )


def _bound_filter(grant: Any) -> tuple[SieveFilter, str]:
    """Rebuild a token's filter and require its stored script to be exactly that filter's."""
    params = grant.params
    if grant.messages or grant.selector or set(params) != _GRANT_KEYS:
        raise MailValidationError("Preview token does not match Sieve filter creation")
    spec = filter_spec(**{key: params[key] for key in _SPEC_KEYS})
    script = params["script"]
    verify_script(script, spec)
    if script != render_script(spec) or params["script_sha256"] != script_sha256(script):
        raise MailValidationError("Sieve script does not match its preview; preview again")
    return spec, script


def _filter_id(value: Any) -> str | None:
    if isinstance(value, str) and value.isascii() and value.isprintable() and len(value) <= 200:
        return value or None
    return None


def _interruption(created: bool, attempted: bool) -> str:
    """What a partial creation left behind: a filter, possibly a filter, or none."""
    if created:
        return (
            "Sieve filter was created, but its outcome could not be journaled; "
            "inspect the action log"
        )
    if attempted:
        return (
            "Sieve filter creation outcome is uncertain; inspect the action log and the "
            "Proton filter list before trying again"
        )
    return (
        "Sieve filter creation stopped before the apply backend was called, so no filter "
        "was created; inspect the action log"
    )


class SieveFilterService:
    """Preview read-only; create through the pluggable backend only after every check."""

    def __init__(self, cleanup: CleanupService, backend: Any = None):
        self.cleanup = cleanup
        self.backend = backend if backend is not None else UnavailableSieveBackend()

    def _existing(self, name: str, timeout: float) -> None:
        names = self.backend.filter_names(timeout=timeout)
        if not isinstance(names, (set, frozenset, list, tuple)) or not all(
            isinstance(item, str) for item in names
        ):
            raise MailValidationError("Sieve apply backend returned invalid filter names")
        if name.casefold() in {item.casefold() for item in names}:
            raise MailValidationError(
                "A Sieve filter with this name already exists; existing filters are never "
                "replaced; choose another name"
            )

    def preview(self, spec: SieveFilter) -> dict[str, Any]:
        script = render_script(spec)
        cleanup = self.cleanup
        deadline = cleanup.client._deadline()
        with cleanup.client._session(deadline=deadline) as (connection, _):
            _require_target(connection, spec.folder)
            deadline.check(connection)
        status = backend_status(self.backend)
        if status["available"]:
            self._existing(spec.name, deadline.check())
        params = spec.params() | {
            "script": script,
            "script_sha256": script_sha256(script),
            "backend": status["backend"],
        }
        token, expires = cleanup.tokens.issue(ACTION, params, {}, [])
        return {
            "action": ACTION,
            **spec.params(),
            "fileinto": sieve_folder(spec.folder),
            "script": script,
            "script_sha256": params["script_sha256"],
            "apply": status,
            "token": token,
            "expires_at": expires,
            "action_hash": approval_hash(ACTION, params, {}, []),
        }

    def create(self, token: str) -> dict[str, Any]:
        cleanup = self.cleanup
        with cleanup._exclusive():
            grant = cleanup.tokens.inspect(token, ACTION)
            spec, script = _bound_filter(grant)
            require_backend(self.backend)
            if grant.params["backend"] != backend_status(self.backend)["backend"]:
                raise MailValidationError(
                    "Sieve apply backend changed after preview; preview again"
                )
            deadline = cleanup.client._deadline(execute=True)
            with cleanup.backend.log.transaction() as transaction:
                transaction.records()
                with cleanup.client._session(deadline=deadline) as (connection, _):
                    _require_target(connection, spec.folder)
                    deadline.check(connection)
                self._existing(spec.name, deadline.check())
                # The journal names the filter by digests: its name and condition
                # values can hold addresses or subject text.
                record = new_record(
                    ACTION,
                    {
                        "name_sha256": hashlib.sha256(spec.name.encode("utf-8")).hexdigest(),
                        "folder": spec.folder,
                        "match": spec.match,
                        "fields": [f"{row.field}:{row.operator}" for row in spec.conditions],
                        "mark_read": spec.mark_read,
                        "script_sha256": grant.params["script_sha256"],
                        "backend": grant.params["backend"],
                    },
                )
                record["token_digest"] = token_digest(token)
                record["status"] = "in_progress"
                entry = {"folder": spec.folder, "status": "not_attempted", "result": "pending"}
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

                claimed = attempted = False
                try:
                    deadline.check()
                    cleanup.tokens.claim(token, ACTION, params=grant.params)
                    claimed = True
                    entry.update(status="uncertain", result="uncertain")
                    progress()
                    # Journaled as uncertain, but only the backend call can leave a
                    # filter: a deadline expiring first leaves attempted false.
                    timeout = deadline.check()
                    attempted = True
                    created = self.backend.create_filter(spec.name, script, timeout=timeout)
                    entry.update(status="done", result="created")
                    if _filter_id(created) is not None:
                        entry["filter_id"] = _filter_id(created)
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
                result = {
                    "log_id": record["log_id"],
                    "name": spec.name,
                    "folder": spec.folder,
                    "script_sha256": grant.params["script_sha256"],
                    "backend": grant.params["backend"],
                    "created": entry["result"] == "created",
                    "attempted": attempted,
                    "filter_id": entry.get("filter_id"),
                    "partial": record["partial"],
                    "token_consumed": claimed,
                }
                if record["partial"]:
                    result["error"] = _interruption(result["created"], attempted)
                return result
