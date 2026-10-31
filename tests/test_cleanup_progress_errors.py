"""Malformed server replies must not hide already journaled cleanup outcomes."""

import json
from dataclasses import replace

import pytest
from action_records import execution_records as replay_records
from resume_assertions import ResumeAudit, preserve_failure_boundary
from starlette.testclient import TestClient
from test_cleanup_chunks import (
    approve,
    assert_progress,
    moved,
    mutations,
    records,
)
from test_cleanup_chunks import chunk_mailbox as chunk_mailbox

from proton_mail_mcp.action_log import LogTransaction
from proton_mail_mcp.mail import MailError, MailValidationError
from proton_mail_mcp.server import create_app

PRIVATE_DIAGNOSTIC = "PRIVATE_SERVER_DIAGNOSTIC"


def execute(case, token, *, mcp):
    if not mcp:
        return case.service.execute("move_to_trash", token)
    # Stateless JSON routing; host confirmation has its own tests.
    settings = replace(case.mailbox.settings, confirm_mutations=False)
    with TestClient(
        create_app(settings, mail_client=case.mailbox), base_url="http://127.0.0.1"
    ) as http:
        response = http.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {settings.token}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "move",
                    "arguments": {"target": "Trash", "token": token, "verbose": True},
                },
            },
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert not result["isError"], result
    assert PRIVATE_DIAGNOSTIC not in json.dumps(result)
    return result["structuredContent"]


def assert_consumed_progress(case, token, result, *, done, remaining, uncertain=0, audit=None):
    assert_progress(
        result,
        done=done,
        remaining=remaining,
        uncertain=uncertain,
        error_done=audit.results[-1]["done"] if audit else None,
    )
    assert result["log_ids"] == [record["log_id"] for record in records(case)]
    assert "log_id" not in result
    assert result["token_consumed"] is True
    assert case.claims == [True] * (len(audit.results) if audit else 1)
    assert "token consumed" in result["error"]
    assert "request a new preview" in result["error"]
    assert PRIVATE_DIAGNOSTIC not in json.dumps(result)
    if case.log.exists():
        assert PRIVATE_DIAGNOSTIC not in case.log.read_text()
    with pytest.raises(MailValidationError, match="already used"):
        case.service.tokens.inspect(token, "move_to_trash")


@pytest.mark.parametrize("mcp", [False, True], ids=["service", "mcp"])
@pytest.mark.parametrize("failure", ["list", "fetch_preparation", "fetch_inside_chunk"])
def test_malformed_reply_after_first_chunk_returns_undoable_progress(
    chunk_mailbox, monkeypatch, failure, mcp
):
    case = chunk_mailbox()
    token = approve(case)
    preserve_failure_boundary(case, monkeypatch)
    audit = ResumeAudit(case, token)
    failures, writable = [], []
    original_list, original_uid = case.fake.list, case.fake.uid
    original_select = case.fake.select
    done = 50

    def malformed_list():
        status, data = original_list()
        if len(case.fake.folders["Trash"]) >= 50:
            failures.append("list")
            # Invalid modified UTF-7 raises ValueError inside _decode_folder.
            data = [*data, f'(\\HasNoChildren) "/" "Folders/{PRIVATE_DIAGNOSTIC}&A-"'.encode()]
        return status, data

    original_verify = case.service._verified_chunk
    preparing = []

    def verify(*args, **kwargs):
        if len(case.fake.folders["Trash"]) >= 50:
            preparing.append(True)
        return original_verify(*args, **kwargs)

    def select(folder, *, readonly):
        if not readonly and len(case.fake.folders["Trash"]) >= 50:
            writable.append(folder)
        return original_select(folder, readonly=readonly)

    def malformed_fetch(command, *args):
        # Preparation: the next chunk's verification. Inside the chunk: the final
        # batch FETCH after its journal record (the chunk's second writable SELECT).
        reached = len(writable) >= 2 if failure == "fetch_inside_chunk" else preparing
        if command == "FETCH" and len(case.fake.folders["Trash"]) >= done and reached:
            failures.append("fetch")
            # Invalid literal length raises ValueError inside _ImapValues.value.
            return "OK", [
                (f"1 (UID {done + 1} ENVELOPE {{{PRIVATE_DIAGNOSTIC}}}".encode(), b"x"),
                b")",
            ]
        return original_uid(command, *args)

    with monkeypatch.context() as patch:
        if failure == "list":
            patch.setattr(case.fake, "list", malformed_list)
        else:
            patch.setattr(case.fake, "uid", malformed_fetch)
            patch.setattr(case.fake, "select", select)
            patch.setattr(case.service, "_verified_chunk", verify)
        audit.finish(
            token,
            invoke=lambda current: execute(case, current, mcp=mcp),
            stop=lambda result: bool(failures),
        )
        result = audit.combined()

    assert_consumed_progress(case, token, result, done=done, remaining=184 - done, audit=audit)
    assert len(case.fake.folders["Trash"]) == len(moved(case)) == done
    assert "unlogged" not in result
    logged = records(case)
    assert len(logged) >= (2 if failure == "fetch_inside_chunk" else 1)
    if failure == "fetch_inside_chunk":
        # The journaled second chunk failed its final check: nothing in it moved.
        assert logged[-1]["partial"] is True
        assert {entry["status"] for entry in logged[-1]["messages"]} == {"not_attempted"}
        assert result["error"].startswith("Cleanup interrupted during execute verification")
    else:
        assert result["error"].startswith("Cleanup interrupted during chunk preparation;")
    # Every returned ID remains usable for undo after the malformed reply ends.
    prior = case.log.read_bytes()
    assert audit.undo_done() == done
    assert case.log.read_bytes().startswith(prior)
    assert len(case.fake.folders["INBOX"]) == 184 and not case.fake.folders["Trash"]


@pytest.mark.parametrize("exception", [RecursionError, MailValidationError])
def test_unexpected_chunk_preparation_error_returns_safe_progress(
    chunk_mailbox, monkeypatch, exception
):
    case = chunk_mailbox()
    token = approve(case)
    preserve_failure_boundary(case, monkeypatch)
    audit = ResumeAudit(case, token)
    failures = []
    original = case.service._resolve

    def resolve(*args, **kwargs):
        if len(case.fake.folders["Trash"]) >= 50:
            failures.append(True)
            raise exception(PRIVATE_DIAGNOSTIC)
        return original(*args, **kwargs)

    monkeypatch.setattr(case.service, "_resolve", resolve)
    audit.finish(token, stop=lambda result: bool(failures))
    result = audit.combined()
    assert_consumed_progress(case, token, result, done=50, remaining=134, audit=audit)
    assert result["error"].startswith("Cleanup interrupted during chunk preparation;")
    assert len(moved(case)) == 50
    assert sum(len(record["messages"]) for record in records(case)) == 50
    assert audit.undo_done() == 50
    assert len(case.fake.folders["INBOX"]) == 184 and not case.fake.folders["Trash"]


@pytest.mark.parametrize("exception", [ValueError, RecursionError])
def test_unexpected_mutation_error_returns_partial_journal(chunk_mailbox, monkeypatch, exception):
    case = chunk_mailbox()
    token = approve(case)
    preserve_failure_boundary(case, monkeypatch)
    audit = ResumeAudit(case, token)
    original = case.fake.uid

    def uid(command, *args):
        # The second chunk's batched MOVE fails after its intents are durable.
        if command == "MOVE" and len(case.fake.folders["Trash"]) == 50:
            raise exception(PRIVATE_DIAGNOSTIC)
        return original(command, *args)

    monkeypatch.setattr(case.fake, "uid", uid)
    audit.finish(token, stop=lambda result: result["uncertain"] > 0)
    result = audit.combined()
    assert_consumed_progress(case, token, result, done=50, remaining=84, uncertain=50, audit=audit)
    assert len(moved(case)) == 50 and len(records(case)) >= 2
    assert records(case)[-1]["partial"] is True
    assert [entry["status"] for entry in records(case)[-1]["messages"]] == ["uncertain"] * 50
    assert {entry["stage"] for entry in records(case)[-1]["messages"]} == {"move"}
    monkeypatch.setattr(case.fake, "uid", original)
    assert audit.undo_done() == 50
    assert len(case.fake.folders["INBOX"]) == 184 and not case.fake.folders["Trash"]


@pytest.mark.parametrize("exception", [ValueError, RecursionError])
@pytest.mark.parametrize("failing_chunk", [1, 2])
@pytest.mark.parametrize("stage", ["progress", "result"])
def test_unexpected_journal_error_keeps_durable_recovery_id(
    chunk_mailbox, monkeypatch, exception, failing_chunk, stage
):
    case = chunk_mailbox()
    token = approve(case)
    preserve_failure_boundary(case, monkeypatch)
    audit = ResumeAudit(case, token, durable_intent=True)
    original = LogTransaction.append
    chunks, failures = 0, []

    def append(transaction, record):
        nonlocal chunks
        if record["action"] == "move_to_trash":
            chunks += 1
        failure = (
            record["action"] == "execute_result"
            if stage == "result"
            else record["action"] == "execute_progress" and record["message"]["status"] == "done"
        )
        # The batched chunk's MOVE has completed when its first outcome is written.
        if chunks == failing_chunk and failure and not failures:
            failures.append(True)
            raise exception(PRIVATE_DIAGNOSTIC)
        return original(transaction, record)

    monkeypatch.setattr(LogTransaction, "append", append)
    audit.finish(token, stop=lambda result: bool(failures))
    result = audit.combined()
    done = failing_chunk * 50
    assert_consumed_progress(case, token, result, done=done, remaining=184 - done, audit=audit)
    assert "unlogged" not in result
    if stage == "progress" and failing_chunk == 1:
        assert len(records(case)) == 1
    else:
        assert len(records(case)) >= failing_chunk
    assert len(moved(case)) == done
    found = replay_records(case.service.backend.log.list_actions(token=token)["actions"])
    assert result["log_ids"] == [row["log_id"] for row in reversed(found)]
    # A failed outcome append leaves the pre-command intent available for exact undo.
    monkeypatch.setattr(LogTransaction, "append", original)
    assert audit.undo_done() == done
    assert len(case.fake.folders["INBOX"]) == 184 and not case.fake.folders["Trash"]


@pytest.mark.parametrize("exception", [ValueError, RecursionError])
def test_initial_journal_error_prevents_mutation_and_keeps_approval(
    chunk_mailbox, monkeypatch, exception
):
    case = chunk_mailbox()
    token = approve(case)
    original = LogTransaction.append

    def append(transaction, record):
        if record["action"] == "move_to_trash":
            raise exception(PRIVATE_DIAGNOSTIC)
        return original(transaction, record)

    monkeypatch.setattr(LogTransaction, "append", append)
    with pytest.raises(MailError, match="^Mailbox operation failed$"):
        case.service.execute("move_to_trash", token)
    assert not records(case) and not mutations(case) and not case.claims
    case.service.tokens.inspect(token, "move_to_trash")


@pytest.mark.parametrize("stage", ["resolution", "preflight"])
def test_unexpected_error_before_first_claim_keeps_approval(chunk_mailbox, monkeypatch, stage):
    case = chunk_mailbox(count=2)
    token = approve(case)

    def fail(*args, **kwargs):
        raise RecursionError(PRIVATE_DIAGNOSTIC)

    with monkeypatch.context() as patch:
        target, name = (
            (case.service, "_resolve")
            if stage == "resolution"
            else (case.service.backend, "_preflight")
        )
        patch.setattr(target, name, fail)
        with pytest.raises(MailError, match="^Mailbox operation failed$"):
            case.service.execute("move_to_trash", token)
    assert not mutations(case) and not case.claims and not records(case)
    case.service.tokens.inspect(token, "move_to_trash")
    assert_progress(case.service.execute("move_to_trash", token), done=2, remaining=0)
