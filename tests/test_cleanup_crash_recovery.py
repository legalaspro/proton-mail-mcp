"""Torn journal writes cannot hide recovery IDs or widen undo's moved set."""

import json
import os

import pytest
from action_records import execution_records as replay_records
from test_cleanup_chunks import approve, mutations
from test_cleanup_chunks import chunk_mailbox as chunk_mailbox
from test_cleanup_chunks import moved as moved_uids
from test_cleanup_undo_retry import expire_before_second_undo
from test_mail import message

from proton_mail_mcp.action_log import LogTransaction
from proton_mail_mcp.mail import MailValidationError


class ProcessKilled(BaseException):
    """Bypass normal error handling after persisting a partial append."""


def tear_append(monkeypatch, matches, *, missing_newline=False):
    append = LogTransaction.append
    killed = False

    def tear(transaction, record):
        nonlocal killed
        if killed:
            raise ProcessKilled()
        if matches(record):
            payload = (json.dumps(record) + "\n").encode()
            fragment = payload[:-1] if missing_newline else payload[: len(payload) // 2]
            os.write(transaction.descriptor, fragment)
            os.fsync(transaction.descriptor)
            killed = True
            raise ProcessKilled()
        return append(transaction, record)

    monkeypatch.setattr(LogTransaction, "append", tear)
    return append


@pytest.mark.parametrize("boundary", ["progress", "result"])
def test_torn_post_move_append_preserves_lookup_and_exact_undo(
    chunk_mailbox, monkeypatch, boundary
):
    case = chunk_mailbox(count=2)
    originals = dict(case.fake.folders["INBOX"])
    unrelated = message("<unapproved-trash@example.test>")
    case.fake.folders["Trash"]["901"] = unrelated
    token = approve(case)

    def matches(record):
        if boundary == "result":
            return record["action"] == "execute_result"
        return record["action"] == "execute_progress" and record["message"]["status"] == "done"

    append = tear_append(monkeypatch, matches)
    with pytest.raises(ProcessKilled):
        case.mailbox.move_to_trash(token)
    monkeypatch.setattr(LogTransaction, "append", append)
    # One batched MOVE moved both; a torn first outcome leaves both intents.
    moved = 2
    assert len(mutations(case)) == 1 and len(moved_uids(case)) == moved
    assert len(case.fake.folders["Trash"]) == moved + 1
    durable = case.log.read_bytes()
    assert not durable.endswith(b"\n")
    log_id = next(
        row["log_id"]
        for row in map(json.loads, durable.splitlines())
        if row.get("action") == "move_to_trash"
    )

    actions = replay_records(case.mailbox.list_actions(token=token)["actions"])
    assert len(actions) == 1 and actions[0]["log_id"] == log_id
    assert actions[0]["status"] == "in_progress"
    assert actions[0]["done"] == (2 if boundary == "result" else 0)
    assert actions[0]["uncertain"] == (0 if boundary == "result" else 2)
    assert actions[0]["not_attempted"] == 0
    assert case.log.read_bytes() == durable  # Lookup must remain read-only.
    with pytest.raises(MailValidationError, match="already used"):
        case.mailbox.move_to_trash(token)

    result = case.mailbox.undo_action(log_id)
    assert result["count"] == moved and not result["partial"]
    assert {id(item) for item in case.fake.folders["INBOX"].values()} == {
        id(item) for item in originals.values()
    }
    assert case.fake.folders["Trash"] == {"901": unrelated}
    assert len(mutations(case)) == 1 + moved  # Undo restores each message by itself.
    assert all(call[0] == "MOVE" for call in mutations(case))
    assert case.log.read_bytes().startswith(durable)
    assert replay_records(case.mailbox.list_actions(token=token)["actions"]) == actions
    with pytest.raises(MailValidationError, match="already been undone"):
        case.mailbox.undo_action(log_id)
    assert len(mutations(case)) == 1 + moved


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("missing_newline", [False, True])
def test_torn_undo_outcome_never_allows_replay(
    chunk_mailbox, monkeypatch, partial, missing_newline
):
    case = chunk_mailbox(count=2, move_seconds=0.1)
    token = approve(case)
    log_id = case.mailbox.move_to_trash(token)["log_ids"][0]
    if partial:
        expire_before_second_undo(case, monkeypatch)
    append = tear_append(
        monkeypatch,
        lambda record: record["action"] == "undo_action",
        missing_newline=missing_newline,
    )
    with pytest.raises(ProcessKilled):
        case.mailbox.undo_action(log_id)
    monkeypatch.setattr(LogTransaction, "append", append)
    moved = 1 if partial else 2
    assert len(mutations(case)) == 1 + moved  # One batched forward MOVE.
    assert len(case.fake.folders["Trash"]) == 2 - moved
    durable = case.log.read_bytes()
    assert not durable.endswith(b"\n")
    actions = replay_records(case.mailbox.list_actions(token=token)["actions"])
    assert len(actions) == 1 and actions[0]["log_id"] == log_id
    # A complete claim remains unmatched: the torn outcome cannot declare an
    # entry done or untouched, even when those fields precede the torn bytes.
    with pytest.raises(MailValidationError, match="unresolved journal outcomes"):
        case.mailbox.undo_action(log_id)
    assert case.log.read_bytes() == durable
    assert len(mutations(case)) == 1 + moved


def test_torn_undo_claim_never_reaches_move(chunk_mailbox, monkeypatch):
    case = chunk_mailbox(count=2)
    token = approve(case)
    log_id = case.mailbox.move_to_trash(token)["log_ids"][0]
    originals = {id(item) for item in case.fake.folders["Trash"].values()}
    append = tear_append(monkeypatch, lambda record: record["action"] == "undo_claim")
    with pytest.raises(ProcessKilled):
        case.mailbox.undo_action(log_id)
    monkeypatch.setattr(LogTransaction, "append", append)
    assert len(mutations(case)) == 1  # Only the batched forward MOVE.
    assert not case.fake.folders["INBOX"]
    assert {id(item) for item in case.fake.folders["Trash"].values()} == originals
    durable = case.log.read_bytes()
    actions = replay_records(case.mailbox.list_actions(token=token)["actions"])
    assert len(actions) == 1 and actions[0]["log_id"] == log_id

    # No undo could start before the claim was durable. A new, complete claim
    # permits the first actual restore without trusting any torn record fields.
    result = case.mailbox.undo_action(log_id)
    assert result["count"] == 2 and not result["partial"]
    assert len(mutations(case)) == 3 and not case.fake.folders["Trash"]
    assert {id(item) for item in case.fake.folders["INBOX"].values()} == originals
    assert case.log.read_bytes().startswith(durable)
    with pytest.raises(MailValidationError, match="already been undone"):
        case.mailbox.undo_action(log_id)
    assert len(mutations(case)) == 3
