"""Run recovery reports the approved total independently of client delivery."""

import hashlib
import json

import pytest
from action_records import execution_records as replay_records
from test_cleanup_chunks import approve
from test_cleanup_chunks import chunk_mailbox as chunk_mailbox

from proton_mail_mcp.action_log import ActionLog, LogTransaction, new_record
from proton_mail_mcp.mail import MailError
from proton_mail_mcp.run_journal import run_event


def append_chunk(log, context, *, statuses, partial=False, token="synthetic-token"):
    record = new_record(context["action"], {})
    record.update({key: context[key] for key in ("run_id", "chunk_index", "approved_total")})
    record.update(
        token_digest=hashlib.sha256(token.encode()).hexdigest(),
        messages=[{"status": status} for status in statuses],
        status="partial" if partial else "completed",
        partial=partial,
    )
    with log.transaction() as transaction:
        transaction.append(record)
        log.ensure_run(transaction, context)
        log.update_run(transaction, context["run_id"])
    return record


def context(*, total=165):
    return {
        "run_id": "a" * 32,
        "action": "move_to_trash",
        "token_digest": hashlib.sha256(b"synthetic-token").hexdigest(),
        "approved_total": total,
        "chunk_index": 0,
    }


def test_missing_run_end_reconciles_journaled_chunks_as_interrupted(tmp_path):
    log = ActionLog(tmp_path / "scratch.jsonl")
    approval = context()
    first = append_chunk(log, approval, statuses=["done"] * 36)
    run = log.get_run(approval["run_id"])
    assert run["status"] == "interrupted" and run["partial"]
    assert (run["done"], run["uncertain"], run["remaining"]) == (36, 0, 129)
    assert run["log_ids"] == [first["log_id"]]
    found = log.list_actions(token="synthetic-token")
    assert found["runs"][0]["status"] == "interrupted"
    assert found["actions"][0]["status"] == "partial"
    assert found["runs"][0]["done"] == 36
    assert found["runs"][0]["log_ids"] == [found["actions"][0]["log_id"]]
    by_chunk_time = log.list_actions(since=first["timestamp"], before=run["timestamp"])
    assert by_chunk_time["runs"][0]["run_id"] == run["run_id"]
    assert by_chunk_time["runs"][0]["done"] == 36
    raw = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert len(replay_records(raw)) == 1
    assert len(replay_records(raw, include_runs=True)) == 2


def test_run_end_is_never_completed_below_approval_and_all_chunk_ids_survive(tmp_path):
    log = ActionLog(tmp_path / "scratch.jsonl")
    approval = context()
    first = append_chunk(log, approval, statuses=["done"] * 36)
    run = log.finish_run(approval["run_id"], stop_reason="Call budget reached")
    assert run["status"] == "partial" and run["remaining_unchanged"] == 129
    approval.update(chunk_index=1, _call_started=False)
    second = append_chunk(log, approval, statuses=["done"] * 129, token="synthetic-next")
    assert log.get_run(approval["run_id"])["status"] == "interrupted"
    run = log.finish_run(approval["run_id"])
    assert run["status"] == "completed" and not run["partial"]
    assert run["done"] == 165 and run["remaining"] == 0
    for token in ("synthetic-token", "synthetic-next"):
        found = log.list_actions(token=token)
        assert found["runs"][0]["log_ids"] == [first["log_id"], second["log_id"]]
        assert len(found["actions"]) == 2
        assert all(row["status"] == "completed" for row in found["actions"])
        assert found["runs"][0]["status"] == "completed"
        assert token not in json.dumps(found)


def test_chunk_partial_cannot_be_hidden_by_run_completion(tmp_path):
    log = ActionLog(tmp_path / "scratch.jsonl")
    approval = context(total=2)
    append_chunk(log, approval, statuses=["done", "done"], partial=True)
    run = log.finish_run(approval["run_id"])
    assert run["done"] == 2 and run["status"] == "partial" and run["partial"]


def test_missing_chunk_result_preserves_uncertainty_in_run(tmp_path):
    log = ActionLog(tmp_path / "scratch.jsonl")
    approval = context(total=3)
    record = append_chunk(log, approval, statuses=["done", "uncertain", "not_attempted"])
    run = log.get_run(approval["run_id"])
    assert run["status"] == "interrupted"
    assert (run["done"], run["uncertain"], run["remaining"]) == (1, 1, 1)
    assert run["log_ids"] == [record["log_id"]]


def test_torn_run_end_reads_interrupted_without_rewriting(tmp_path):
    log = ActionLog(tmp_path / "scratch.jsonl")
    approval = context(total=2)
    append_chunk(log, approval, statuses=["done", "done"])
    prefix = (
        log.path.read_bytes()
        + json.dumps(run_event("run_end", approval["run_id"], status="completed")).encode()
    )
    log.path.write_bytes(prefix)
    assert log.list_actions()["runs"][0]["status"] == "interrupted"
    assert log.path.read_bytes() == prefix


def test_duplicate_run_metadata_preserves_chunk_undo_but_refuses_forward_execution(chunk_mailbox):
    case = chunk_mailbox(count=15, timeout=24, move_seconds=2)
    # A per-call limit leaves a durable partial run (one batched chunk fits all 15).
    first = case.service.execute("move_to_trash", approve(case), limit=10)
    assert first["done"] == 10 and first["resume"]
    log = case.service.backend.log
    raw = [json.loads(line) for line in log.path.read_text().splitlines()]
    duplicate = replay_records(raw)[0]
    duplicate["log_id"] = "f" * 32
    with log.path.open("a") as stream:
        stream.write(json.dumps(duplicate) + "\n")
    with log.transaction(readonly=True) as transaction:
        assert len(replay_records(transaction.records())) == 2
        with pytest.raises(MailError, match="invalid chunk identity"):
            transaction.records(include_runs=True)
    with pytest.raises(MailError, match="invalid chunk identity"):
        log.list_actions()
    with pytest.raises(MailError, match="invalid chunk identity"):
        case.service.execute("move_to_trash", first["continuation_token"])
    restored = case.service.undo_action(first["log_ids"][0])
    assert restored["count"] == 10 and not restored["partial"]
    assert len(case.fake.folders["INBOX"]) == 15 and not case.fake.folders["Trash"]
    token = approve(case)
    moves = sum(call[0] == "MOVE" for call in case.fake.calls)
    with pytest.raises(MailError, match="invalid chunk identity"):
        case.service.execute("move_to_trash", token)
    assert sum(call[0] == "MOVE" for call in case.fake.calls) == moves


def test_run_start_is_durable_before_first_move(chunk_mailbox, monkeypatch):
    case = chunk_mailbox(count=2)
    token = approve(case)
    original = case.fake.uid
    checked = []

    def inspect_before_move(command, *args):
        if command == "MOVE":
            raw = [json.loads(line) for line in case.log.read_text().splitlines()]
            starts = [row for row in raw if row["action"] == "run_start"]
            assert len(starts) == 1 and starts[0]["approved_total"] == 2
            assert starts[0]["token_digest"] == hashlib.sha256(token.encode()).hexdigest()
            checked.append(True)
        return original(command, *args)

    monkeypatch.setattr(case.fake, "uid", inspect_before_move)
    result = case.service.execute("move_to_trash", token)
    assert result["done"] == 2 and len(checked) == 1  # One batched MOVE.


def test_failed_run_start_prevents_first_move(chunk_mailbox, monkeypatch):
    case = chunk_mailbox(count=2)
    token = approve(case)
    original = LogTransaction.append

    def fail_start(transaction, record):
        if record["action"] == "run_start":
            raise OSError("Synthetic run start write failure")
        return original(transaction, record)

    monkeypatch.setattr(LogTransaction, "append", fail_start)
    result = case.service.execute("move_to_trash", token)
    assert result["done"] == 0
    assert not [call for call in case.fake.calls if call[0] == "MOVE"]
    assert result["partial"]


def test_partial_run_summary_retains_approved_total(chunk_mailbox):
    case = chunk_mailbox(count=165, timeout=76, move_seconds=2)
    token = approve(case)
    # The server reaches its return even if the caller has already disconnected.
    case.service.execute("move_to_trash", token, limit=100)
    lookup = case.service.backend.log.list_actions(token=token)
    chunks = replay_records(lookup["actions"])
    assert chunks and 0 < sum(chunk["done"] for chunk in chunks) < 165
    assert "runs" in lookup, (
        "approved=165 chunk_done="
        + str(sum(chunk["done"] for chunk in chunks))
        + " chunk_status="
        + chunks[0]["status"]
        + " chunk_partial="
        + str(chunks[0]["partial"])
        + " run_record=absent"
    )
    run = lookup["runs"][0]
    assert run["approved_total"] == 165
    assert run["done"] == sum(chunk["done"] for chunk in chunks)
    assert run["remaining"] == 165 - run["done"]
    assert run["status"] == "partial" and run["partial"]
    assert set(run["log_ids"]) == {chunk["log_id"] for chunk in chunks}
    assert all(chunk["status"] == "partial" and chunk["partial"] for chunk in chunks)
    assert token not in json.dumps(lookup)
