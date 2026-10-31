"""Saved custom folder rules: preview, one bound token, host confirmation, journaled append.

preview_add_rule validates one rule with the Sieve filter model (sieve.py), checks
its target folder as Sieve filter creation does, refuses a name an existing saved
rule already uses, and binds a message-less token to the exact rule, its digest,
its script and the store target. add_rule revalidates all of it, journals the
intent, claims the token and hands exactly that script to the rules backend:
rules_store.LocalRulesBackend by default, which appends the rule to the local
store. The rules engine (folder_rules) applies saved rules like packaged ones.
"""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from typing import Any

from .action_log import new_record, token_digest
from .cleanup_lookup import display_row, fetch_rows
from .folder_rules import FolderRulesService, SavedRule
from .guarded import MAX_BATCH, CleanupService, approval_hash
from .mail import MailError, MailValidationError
from .rules_source import require_rules_source
from .rules_store import MAX_SAVED_RULES, LocalRulesBackend, name_key, rule_sha256, store_path
from .search import _uidvalidity
from .sieve import (
    SieveFilter,
    backend_status,
    filter_spec,
    render_script,
    script_sha256,
    verify_script,
)
from .sieve_filters import _require_target

ACTION = "add_rule"
_SPEC_KEYS = ("name", "folder", "conditions", "match", "mark_read")
_GRANT_KEYS = frozenset({*_SPEC_KEYS, "rule_sha256", "script", "script_sha256", "backend", "store"})
# The optional look at current matches: one search, the newest candidates by UID.
MATCH_SCAN = MAX_BATCH
MATCH_SAMPLES = 5


def rules_backend(settings: Any) -> Any:
    """add_rule's backend: the local saved rules store."""
    return LocalRulesBackend(store_path(settings))


def require_rules_backend(backend: Any) -> None:
    """Refuse before any confirmation, journal or mailbox work unless saving can work."""
    status = backend_status(backend)
    if not status["available"]:
        raise MailValidationError(f"Rules cannot be saved: {status['reason']}; nothing was changed")


def _bound_rule(grant: Any) -> tuple[SieveFilter, str]:
    """Rebuild a token's rule and require its stored script and digests to be exactly its own."""
    params = grant.params
    if grant.messages or grant.selector or set(params) != _GRANT_KEYS:
        raise MailValidationError("Preview token does not match saving a rule")
    spec = filter_spec(**{key: params[key] for key in _SPEC_KEYS})
    script = params["script"]
    verify_script(script, spec)
    if (
        script != render_script(spec)
        or params["script_sha256"] != script_sha256(script)
        or params["rule_sha256"] != rule_sha256(spec)
    ):
        raise MailValidationError("Saved rule does not match its preview; preview again")
    return spec, script


def _interruption(added: bool, attempted: bool) -> str:
    """What an unfinished save left behind: a rule, possibly a rule, or none."""
    if added:
        return "Rule was saved, but its outcome could not be journaled; inspect the action log"
    if attempted:
        return (
            "Saving the rule had an uncertain outcome; check list_rules and the action log "
            "before trying again"
        )
    return (
        "Saving stopped before the rules backend was called, so no rule was saved; "
        "inspect the action log"
    )


class SavedRuleService:
    """Preview read-only; save through the rules backend only after every check."""

    def __init__(self, cleanup: CleanupService):
        self.cleanup = cleanup
        self.backend = rules_backend(cleanup.settings)

    def _store(self) -> str | None:
        """The store target tokens bind: the local store's real target digest."""
        store = getattr(self.backend, "store_id", None)
        return store if isinstance(store, str) else None

    def _bind(self, store: Any) -> Any:
        """Keep one save in the store its token bound, refusing first if that changed."""
        bind = getattr(self.backend, "bind", None)
        if callable(bind):
            return bind(store)
        if store != self._store():
            raise MailValidationError("Saved rules store changed after preview; preview again")
        return nullcontext()

    def _free(self, name: str, timeout: float) -> None:
        names = self.backend.filter_names(timeout=timeout)
        if not isinstance(names, (set, frozenset, list, tuple)) or not all(
            isinstance(item, str) for item in names
        ):
            raise MailValidationError("Rules backend returned invalid rule names")
        if name_key(name) in {name_key(item) for item in names}:
            raise MailValidationError(
                "A saved rule with this name already exists; rules are never replaced; "
                "choose another name"
            )

    def _matches(self, connection: Any, deadline: Any, spec: SieveFilter) -> dict[str, Any]:
        """Current INBOX mail the rule matches: read-only, one search, bounded fetch."""
        client = self.cleanup.client
        rule = SavedRule(spec)
        result: dict[str, Any] = {"folder": "INBOX", "count": 0, "scanned": 0}
        result |= {"candidates": 0, "total_is_exact": True, "samples": []}
        try:
            source = require_rules_source("INBOX", connection)
            state = client._select(connection, source)
            validity = _uidvalidity(connection)
            uids = []
            if state["exists"]:
                criteria, literal = rule.criteria()
                found = client._search(connection, criteria or ["ALL"], literal=literal)
                uids = sorted(set(found), key=int)
            scanned = uids[-MATCH_SCAN:]
            rows = fetch_rows(
                client, connection, deadline, source, scanned, validity, complete=False
            )
            matched = sorted(
                (row for row in rows if rule.matches(row)),
                key=lambda row: int(row["uid"]),
                reverse=True,
            )
        except (MailError, MailValidationError):
            # Informational only: a slow or changing mailbox never blocks the preview.
            result.update(partial=True, total_is_exact=False)
            return result
        result.update(
            folder=source,
            count=len(matched),
            scanned=len(rows),
            candidates=len(uids),
            total_is_exact=len(scanned) == len(uids),
            samples=[
                {key: display_row(row)[key] for key in ("from", "subject", "date")}
                | {"folder": source, "uidvalidity": validity, "uid": row["uid"]}
                for row in matched[:MATCH_SAMPLES]
            ],
        )
        return result

    def preview(self, spec: SieveFilter, *, include_matches: bool = False) -> dict[str, Any]:
        script = render_script(spec)
        require_rules_backend(self.backend)
        # The target this token binds, resolved before anything reads the store.
        store = self._store()
        cleanup = self.cleanup
        deadline = cleanup.client._deadline()
        self._free(spec.name, deadline.check())
        matches = None
        with cleanup.client._session(deadline=deadline) as (connection, _):
            _require_target(connection, spec.folder)
            deadline.check(connection)
            if include_matches:
                matches = self._matches(connection, deadline, spec)
        status = backend_status(self.backend)
        params = spec.params() | {
            "rule_sha256": rule_sha256(spec),
            "script": script,
            "script_sha256": script_sha256(script),
            "backend": status["backend"],
            "store": store,
        }
        token, expires = cleanup.tokens.issue(ACTION, params, {}, [])
        result = {
            "action": ACTION,
            **spec.params(),
            "rule_sha256": params["rule_sha256"],
            # Informational: the same rule as a Proton filter, for its own settings.
            "script": script,
            "script_sha256": params["script_sha256"],
            "apply": status,
            "token": token,
            "expires_at": expires,
            "action_hash": approval_hash(ACTION, params, {}, []),
        }
        if matches is not None:
            result["matches"] = matches
        return result

    def ready(self) -> None:
        require_rules_backend(self.backend)

    def add(self, token: str) -> dict[str, Any]:
        cleanup = self.cleanup
        with cleanup._exclusive():
            grant = cleanup.tokens.inspect(token, ACTION)
            spec, script = _bound_rule(grant)
            require_rules_backend(self.backend)
            if grant.params["backend"] != backend_status(self.backend)["backend"]:
                raise MailValidationError("Saved rules store changed after preview; preview again")
            deadline = cleanup.client._deadline(execute=True)
            # Refuses before the journal unless the store is still the bound one,
            # then holds it: the name check and the save below use only that store.
            with (
                self._bind(grant.params["store"]),
                cleanup.backend.log.transaction() as transaction,
            ):
                transaction.records()
                with cleanup.client._session(deadline=deadline) as (connection, _):
                    _require_target(connection, spec.folder)
                    deadline.check(connection)
                self._free(spec.name, deadline.check())
                # Digests only: rule names and condition values can hold
                # addresses or subject text.
                record = new_record(
                    ACTION,
                    {
                        "name_sha256": hashlib.sha256(spec.name.encode("utf-8")).hexdigest(),
                        "folder": spec.folder,
                        "match": spec.match,
                        "fields": [f"{row.field}:{row.operator}" for row in spec.conditions],
                        "mark_read": spec.mark_read,
                        "rule_sha256": grant.params["rule_sha256"],
                        "script_sha256": grant.params["script_sha256"],
                        "backend": grant.params["backend"],
                        "store": grant.params["store"],
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
                    # Journaled as uncertain, but only the backend call can save
                    # the rule: a deadline expiring first leaves attempted false.
                    timeout = deadline.check()
                    attempted = True
                    self.backend.create_filter(spec.name, script, timeout=timeout)
                    entry.update(status="done", result="added")
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
                    "rule_sha256": grant.params["rule_sha256"],
                    "backend": grant.params["backend"],
                    "added": entry["result"] == "added",
                    "attempted": attempted,
                    "partial": record["partial"],
                    "token_consumed": claimed,
                }
                if record["partial"]:
                    result["error"] = _interruption(result["added"], attempted)
                return result

    def list_rules(self) -> dict[str, Any]:
        """Every rule the engine applies, in order; read-only, no mailbox access."""
        rules, _ = FolderRulesService(self.cleanup).load()
        listed = []
        for index, rule in enumerate(rules, start=1):
            entry = {"rule": index, "kind": "saved" if isinstance(rule, SavedRule) else "built_in"}
            entry |= rule.report() | {"target": rule.folder}
            if isinstance(rule, SavedRule):
                entry["rule_sha256"] = rule_sha256(rule.spec)
            listed.append(entry)
        saved = sum(entry["kind"] == "saved" for entry in listed)
        return {
            "rules": listed,
            "built_in_count": len(listed) - saved,
            "saved_count": saved,
            "max_saved_rules": MAX_SAVED_RULES,
            "apply": backend_status(self.backend),
        }
