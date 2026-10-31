"""Undo preparation remains retryable when reads fail, with no mail changed."""

import json
from pathlib import Path

import pytest
from action_records import execution_records as replay_records
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations

from proton_mail_mcp.mail import CallDeadlineExceeded, MailError, MailValidationError


@pytest.fixture
def cleanup_batch(tmp_path):
    simulations = []

    def build(count=3, *, copyuid=True):
        sim = BridgeSim(copyuid=copyuid)
        simulations.append(sim)
        messages = [sim.add(Msg(hdr(f"<prepare-{index}@shop.example>"))) for index in range(count)]
        client = make_client(sim, tmp_path / str(len(simulations)))
        preview = client.preview_action("move_to_trash", query={"folder": "INBOX"})
        if not copyuid:
            # Hidden from verification, no moved copy is confirmed in Trash: the
            # moves stay uncertain without destination UIDs, so undo takes the
            # header-digest fallback.
            sim.fetch_omit.update(("Trash", message.pid) for message in messages)
        completed = client.move_to_trash(preview["token"])
        sim.fetch_omit.clear()
        if copyuid:
            assert completed["count"] == count and not completed["partial"], completed
        else:
            assert completed["uncertain"] == count and completed["count"] == 0, completed
            assert all(row["uid_after"] is None for row in completed["messages"])
        return sim, client, messages, completed

    yield build
    for sim in simulations:
        sim.close()
        assert not sim.errors
        assert not sim.violations
        assert all(not worker.is_alive() for _, _, worker in sim.peers)


def assert_restored(client, messages, completed):
    restored = client.undo_action(completed["log_ids"][0])
    assert restored["count"] == len(messages), restored
    assert restored["partial"] is False and restored["missing"] == [], restored
    assert all(message.folder == "INBOX" for message in messages)
    records = replay_records(
        [
            json.loads(line)
            for line in Path(client.settings.action_log_path).read_text().splitlines()
        ]
    )
    assert [record["action"] for record in records] == [
        "move_to_trash",
        "undo_claim",
        "undo_action",
    ]


@pytest.mark.parametrize("copyuid", [True, False], ids=["bound_uid", "digest_fallback"])
@pytest.mark.parametrize("failure", ["NO", "BAD", "disconnect"])
def test_undo_preparation_fetch_failure_does_not_claim_and_retry_restores_all(
    cleanup_batch, monkeypatch, copyuid, failure
):
    sim, client, messages, completed = cleanup_batch(copyuid=copyuid)
    before = mutations(sim)
    log = Path(client.settings.action_log_path)
    original_log = log.read_bytes()
    fetches = 0
    # Legacy digest lookup inventories Trash once for all rows; handle-bound
    # lookup instead fetches each approved UID independently.
    failure_fetch = 2 if copyuid else 1
    handle = sim.handle

    def fail_second_fetch(sid, selected, operation, command):
        nonlocal fetches
        if operation == "FETCH" and selected == "Trash":
            fetches += 1
            if fetches == failure_fetch and failure == "disconnect":
                return "drop"
        return None

    def failed_response(tag, operation, uid_op, words, session, command):
        if operation == "FETCH" and session.selected == "Trash" and fetches == failure_fetch:
            return tag + b" " + failure.encode() + b" injected transient failure\r\n"
        return handle(tag, operation, uid_op, words, session, command)

    sim.hooks.append(fail_second_fetch)
    with monkeypatch.context() as patch:
        patch.setattr(sim, "handle", failed_response)
        with pytest.raises(MailError):
            client.undo_action(completed["log_ids"][0])
    sim.hooks.clear()
    assert fetches == failure_fetch
    assert log.read_bytes() == original_log  # No durable claim or outcome was appended.
    assert mutations(sim) == before
    assert all(message.folder == "Trash" for message in messages)
    assert_restored(client, messages, completed)


@pytest.mark.parametrize("copyuid", [True, False], ids=["bound_uid", "digest_fallback"])
def test_undo_preparation_deadline_does_not_claim_and_retry_restores_eight(
    cleanup_batch, monkeypatch, copyuid
):
    sim, client, messages, completed = cleanup_batch(count=8, copyuid=copyuid)
    before = mutations(sim)
    log = Path(client.settings.action_log_path)
    original_log = log.read_bytes()
    deadline = client._deadline()
    fetches = 0
    failure_fetch = 4 if copyuid else 1

    def expire_during_preparation(sid, selected, operation, command):
        nonlocal fetches
        if operation == "FETCH" and selected == "Trash":
            fetches += 1
            if fetches == failure_fetch:
                deadline.end = 0  # Expire a real deadline after some rows were prepared.
        return None

    sim.hooks.append(expire_during_preparation)
    with monkeypatch.context() as patch:
        patch.setattr(client, "_deadline", lambda **_: deadline)
        with pytest.raises(CallDeadlineExceeded):
            client.undo_action(completed["log_ids"][0])
    sim.hooks.clear()
    assert fetches == failure_fetch
    assert log.read_bytes() == original_log
    assert mutations(sim) == before
    assert all(message.folder == "Trash" for message in messages)
    assert_restored(client, messages, completed)


def test_undo_deadline_after_preparation_does_not_claim(cleanup_batch, monkeypatch):
    sim, client, messages, completed = cleanup_batch(count=8)
    before = mutations(sim)
    log = Path(client.settings.action_log_path)
    original_log = log.read_bytes()
    mutator = client.cleanup.backend
    prepare = mutator._prepare_undo

    def expire_after_preparation(connection, deadline, original):
        prepared = prepare(connection, deadline, original)
        deadline.end = 0
        return prepared

    with monkeypatch.context() as patch:
        patch.setattr(mutator, "_prepare_undo", expire_after_preparation)
        with pytest.raises(CallDeadlineExceeded):
            client.undo_action(completed["log_ids"][0])
    assert log.read_bytes() == original_log
    assert mutations(sim) == before
    assert_restored(client, messages, completed)


def test_undo_digest_mismatch_still_reports_missing_without_mutation(cleanup_batch):
    sim, client, messages, completed = cleanup_batch(count=1, copyuid=False)
    messages[0].headers = hdr(messages[0].message_id(), subject="Changed identity evidence")
    before = mutations(sim)
    result = client.undo_action(completed["log_ids"][0])
    assert result["count"] == 0 and result["partial"] is True, result
    assert result["missing"] == [
        {
            "message_id": messages[0].message_id(),
            "folder": "Trash",
            "reason": (
                'found 0 matching physical copies; manual handling required during undo in "Trash"'
            ),
        }
    ]
    assert "error" not in result
    assert messages[0].folder == "Trash" and mutations(sim) == before


@pytest.mark.parametrize("failure", ["disconnect", "fetch_no", "deadline"])
def test_undo_io_failure_after_claim_reports_interruption(cleanup_batch, monkeypatch, failure):
    sim, client, messages, completed = cleanup_batch()
    before = mutations(sim)
    writable = False
    deadline = client._deadline()

    def interrupt_after_claim(sid, selected, operation, command):
        nonlocal writable
        if operation == "SELECT" and command == 'SELECT "Trash"':
            writable = True
            if failure == "disconnect":
                return "drop"
            if failure == "deadline":
                deadline.end = 0
        elif writable and operation == "FETCH" and selected == "Trash":
            return "NO", "injected transient failure"
        return None

    sim.hooks.append(interrupt_after_claim)
    with monkeypatch.context() as patch:
        patch.setattr(client, "_deadline", lambda **_: deadline)
        result = client.undo_action(completed["log_ids"][0])
    sim.hooks.clear()
    assert writable
    assert result["count"] == 0 and result["partial"] is True, result
    assert result["missing"] == []
    assert result["error"] == (
        'Cleanup interrupted during undo verification in "Trash"; '
        "inspect logged outcomes before another action"
    )
    assert all(entry["status"] == "not_attempted" for entry in result["messages"])
    assert mutations(sim) == before
    assert all(message.folder == "Trash" for message in messages)
    records = replay_records(
        [
            json.loads(line)
            for line in Path(client.settings.action_log_path).read_text().splitlines()
        ]
    )
    assert [record["action"] for record in records] == [
        "move_to_trash",
        "undo_claim",
        "undo_action",
    ]
    retry = client.undo_action(completed["log_ids"][0])
    assert retry["count"] == len(messages) and retry["partial"] is False
    assert all(message.folder == "INBOX" for message in messages)
    with pytest.raises(MailValidationError, match="already been undone"):
        client.undo_action(completed["log_ids"][0])
