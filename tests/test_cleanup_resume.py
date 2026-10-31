"""Large guarded approvals continue safely through a slow local IMAP peer."""

import hashlib
import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from action_records import execution_records as replay_records
from bridge_costs import CommandClock, WorkCounters
from bridge_sim import Msg, hdr, mutations
from slow_cleanup import SlowCleanupPeer
from test_cleanup_mutations import uid_values

from proton_mail_mcp import guarded, mail
from proton_mail_mcp.mail import MailValidationError


@pytest.fixture
def slow_cleanup(tmp_path, monkeypatch):
    peers = []

    def forbid_network(*args, **kwargs):
        pytest.fail("Slow cleanup tests must use local socket pairs")

    monkeypatch.setattr(socket, "create_connection", forbid_network)

    def build(*, count=165, timeout=80, mutation_seconds=1.5, batch_size=1):
        # These approvals exercise multi-call continuations. One-UID batched
        # chunks (each still planned with its full command-group allowance) keep
        # a call's capacity well below the approval, as per-message moves did.
        monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", batch_size)
        peer = SlowCleanupPeer(mutation_seconds=mutation_seconds)
        peers.append(peer)
        monkeypatch.setattr(mail, "time", peer.clock)
        items = peer.populate(count)
        client = peer.client(tmp_path / str(len(peers)), timeout=timeout)
        client.cleanup.tokens.clock = peer.clock.monotonic
        return SimpleNamespace(
            peer=peer,
            client=client,
            service=client.cleanup,
            items=items,
            clock=peer.clock,
            path=Path(client.settings.action_log_path),
            initial_uids={str(peer.uid_of("INBOX", item.pid)) for item in items},
        )

    yield build
    for peer in peers:
        peer.close()
        assert not peer.errors and not peer.violations
        assert all(not worker.is_alive() for _, _, worker in peer.peers)


def approve(case, action="move_to_trash", params=None):
    preview = case.service.preview_action(
        action,
        query={"folder": "INBOX", "from_": "notifications@example.invalid"},
        params=params,
    )
    assert preview["token"] and preview["count"] == len(case.items)
    assert preview["blocked"] == [] and not mutations(case.peer)
    return preview


def records(case):
    return replay_records([json.loads(line) for line in case.path.read_text().splitlines()])


def continue_to_end(case, token, *, action="move_to_trash", first=None):
    results = [] if first is None else [first]
    used = set()
    if first is not None:
        token = first["continuation_token"]
    for _ in range(30):
        assert token and token not in used, "Continuation must rotate on every accepted call"
        used.add(token)
        started = case.clock.elapsed
        result = case.service.execute(action, token)
        budget = min(case.client.settings.call_timeout, case.client.settings.execute_timeout)
        assert case.clock.elapsed - started < budget - min(5, budget * 0.1)
        results.append(result)
        before = list(mutations(case.peer))
        with pytest.raises(MailValidationError, match="already used"):
            case.service.execute(action, token)
        assert mutations(case.peer) == before
        if not result["resume"]:
            assert result["continuation_token"] is None
            return results
        assert result["partial"] and result["continuation_token"] != token
        token = result["continuation_token"]
    pytest.fail("Bounded approval did not finish in 30 confirmation calls")


@pytest.mark.parametrize("count", [129, 200])
def test_large_slow_approval_finishes_across_calls_and_each_chunk_undoes(
    slow_cleanup, monkeypatch, count
):
    case = slow_cleanup(count=count)
    preview = approve(case)
    assert preview["estimated_calls"] >= 2
    outsider = case.peer.add(
        Msg(hdr("<later@example.invalid>", from_="notifications@example.invalid"))
    )
    outsider_uid = str(case.peer.uid_of("INBOX", outsider.pid))
    original_resolve = case.service._resolve
    verified_sizes = []

    def bounded_resolution(*args, **kwargs):
        approved = kwargs.get("bound_rows")
        assert approved is not None, "Confirmation must not rerun its preview selector"
        if not verified_sizes:
            assert len(approved) == count
            assert {row["uid"] for row in approved} == case.initial_uids
        else:
            assert 0 < len(approved) <= 50, "Read-only verification must be bounded per chunk"
        verified_sizes.append(len(approved))
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(case.service, "_resolve", bounded_resolution)
    results = continue_to_end(case, preview["token"])
    assert len(results) > 1 and verified_sizes
    assert sum(result["done"] for result in results) == count
    assert all(result["done"] > 0 for result in results)
    assert len({result["run_id"] for result in results}) == 1
    assert all(result["approved_total"] == count for result in results)
    assert results[-1]["run_done"] == count
    assert results[-1]["remaining"] == results[-1]["uncertain"] == 0
    assert results[-1]["partial"] is False
    logs = [log_id for result in results for log_id in result["log_ids"]]
    assert len(logs) == len(set(logs)) == len(records(case))
    assert {row["uid_before"] for record in records(case) for row in record["messages"]} == (
        case.initial_uids
    )
    assert all(record["approved_total"] == count for record in records(case))
    assert all(record["run_id"] == results[0]["run_id"] for record in records(case))
    assert all(item.folder == "Trash" for item in case.items)
    assert (
        outsider.folder == "INBOX" and str(case.peer.uid_of("INBOX", outsider.pid)) == outsider_uid
    )
    assert all(
        uid in case.initial_uids
        for _, _, _, command in mutations(case.peer)
        for uid in uid_values(command.split()[2])
    )
    # Undo each durable chunk through the existing path; each restores exactly
    # its journaled physical messages, including after several continuations.
    monkeypatch.setattr(case.service, "_resolve", original_resolve)
    case.peer.mutation_seconds = 0
    restored = set()
    for record in list(records(case)):
        expected = {row["message_id"] for row in record["messages"] if row["status"] == "done"}
        before = {item.message_id() for item in case.items if item.folder == "INBOX"}
        result = case.service.undo_action(record["log_id"])
        after = {item.message_id() for item in case.items if item.folder == "INBOX"}
        assert result["count"] == len(expected) and not result["partial"]
        assert after - before == expected
        restored |= expected
    assert restored == {item.message_id() for item in case.items}
    assert all(item.folder == "INBOX" for item in case.items)
    assert outsider.folder == "INBOX"
    assert not any("EXPUNGE" in command for _, _, _, command in case.peer.commands)


def test_disconnected_client_does_not_hide_partial_run(slow_cleanup):
    case = slow_cleanup()
    token = approve(case)["token"]
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    outcome = {}

    def hold_first_move(session, folder, operation, command):
        if operation == "MOVE" and not entered.is_set():
            entered.set()
            assert release.wait(10)

    case.peer.arrival_hook = hold_first_move

    def execute():
        try:
            outcome["result"] = case.service.execute("move_to_trash", token)
        except BaseException as error:
            outcome["error"] = error
        finally:
            finished.set()

    async def disconnected_client():
        async def wait_for_result():
            await anyio.to_thread.run_sync(execute, abandon_on_cancel=True)
            pytest.fail("Cancelled client received the worker result")

        async with anyio.create_task_group() as group:
            group.start_soon(wait_for_result)
            assert await anyio.to_thread.run_sync(entered.wait, 10)
            group.cancel_scope.cancel()

    try:
        anyio.run(disconnected_client)
    finally:
        release.set()
    assert finished.wait(20)
    assert "error" not in outcome
    result = outcome["result"]
    assert result["partial"] and result["resume"] and 0 < result["done"] < 165
    assert result["remaining"] == 165 - result["done"] and result["uncertain"] == 0
    summary = case.client.list_actions(token=token)
    run = next(run for run in summary["runs"] if run["run_id"] == result["run_id"])
    assert run["status"] == "partial" and run["done"] == result["done"]
    assert run["approved_total"] == 165 and run["remaining"] == result["remaining"]
    assert run["log_ids"] == result["log_ids"]
    assert all(
        record["status"] == "partial" and record["partial"]
        for record in replay_records(summary["actions"])
    )
    journal = case.path.read_text()
    assert token not in journal
    assert hashlib.sha256(token.encode("ascii")).hexdigest() in journal


@pytest.mark.parametrize("change", ["moved", "protected", "fingerprint", "seen"])
def test_changed_remaining_row_is_blocked_while_other_approved_rows_finish(slow_cleanup, change):
    case = slow_cleanup(count=18, timeout=24, mutation_seconds=2)
    action = "mark_read" if change == "seen" else "move_to_trash"
    token = approve(case, action)["token"]
    first = case.service.execute(action, token)
    assert first["resume"] and 0 < first["done"] < len(case.items)
    pending = next(
        item
        for item in case.items
        if (r"\Seen" not in item.flags if action == "mark_read" else item.folder == "INBOX")
    )
    pending_uid = str(case.peer.uid_of("INBOX", pending.pid))
    if change == "moved":
        pending.folder = "Archive"
        case.peer.sync()
    elif change == "protected":
        pending.headers = pending.headers.replace(
            b"notifications@example.invalid", b"person@gmail.com"
        )
    elif change == "fingerprint":
        pending.headers = pending.headers.replace(b"Subject: Hello", b"Subject: Changed")
    else:
        pending.flags.add(r"\Seen")
    before = len(mutations(case.peer))
    results = continue_to_end(case, first["continuation_token"], action=action)
    assert sum(result["done"] for result in results) + first["done"] == len(case.items) - 1
    assert results[-1]["remaining"] == 1 and results[-1]["partial"]
    assert not results[-1]["resume"]
    assert any(result["blocked"] for result in results)
    assert all(
        pending_uid not in uid_values(command.split()[2])
        for _, _, _, command in mutations(case.peer)[before:]
    )
    if action == "mark_read":
        assert all(r"\Seen" in item.flags for item in case.items)
    else:
        assert all(item.folder == "Trash" for item in case.items if item is not pending)
        assert pending.folder == ("Archive" if change == "moved" else "INBOX")


def test_continuation_is_bound_to_original_action_params_and_resume_expiry(slow_cleanup):
    case = slow_cleanup(count=30, timeout=24, mutation_seconds=2)
    params = {"folder": "Folders/Reviewed"}
    token = approve(case, "move_to_folder", params)["token"]
    original_expiry = case.service.tokens.inspect(token, "move_to_folder").expires
    first = case.service.execute("move_to_folder", token)
    continuation = first["continuation_token"]
    assert continuation and first["resume"]
    grant = case.service.tokens.inspect(continuation, "move_to_folder")
    assert grant.expires > original_expiry
    assert grant.expires_at == first["expires_at"]
    before = list(mutations(case.peer))
    with pytest.raises(MailValidationError, match="action"):
        case.service.execute("move_to_trash", continuation)
    with pytest.raises(MailValidationError, match="params"):
        case.service.execute("move_to_folder", continuation, params={"folder": "Archive"})
    with pytest.raises(MailValidationError, match="message set"):
        case.service.execute("move_to_folder", continuation, ids=["<outside@example.invalid>"])
    assert mutations(case.peer) == before
    case.clock.elapsed = grant.expires
    with pytest.raises(MailValidationError, match="expired"):
        case.service.execute("move_to_folder", continuation)
    assert mutations(case.peer) == before


def test_concurrent_continuation_refused_without_stealing_token(slow_cleanup):
    case = slow_cleanup(count=30, timeout=24, mutation_seconds=2)
    token = approve(case)["token"]
    first = case.service.execute("move_to_trash", token)
    continuation = first["continuation_token"]
    assert continuation
    entered, release = threading.Event(), threading.Event()
    outcome = {}

    def hold_first_move(session, folder, operation, command):
        if operation == "MOVE" and not entered.is_set():
            entered.set()
            assert release.wait(10)

    case.peer.arrival_hook = hold_first_move

    def execute():
        try:
            outcome["result"] = case.service.execute("move_to_trash", continuation)
        except BaseException as error:
            outcome["error"] = error

    worker = threading.Thread(target=execute)
    worker.start()
    try:
        assert entered.wait(10)
        before = len(mutations(case.peer))
        with pytest.raises(MailValidationError, match="Another cleanup operation"):
            case.service.execute("move_to_trash", continuation)
        assert len(mutations(case.peer)) == before
    finally:
        release.set()
        worker.join(timeout=20)
    assert not worker.is_alive() and "error" not in outcome
    assert outcome["result"]["done"] > 0
    with pytest.raises(MailValidationError, match="already used"):
        case.service.execute("move_to_trash", continuation)


def test_mutation_planning_floor_covers_slow_move_and_shared_bridge_metadata_cost():
    clock = CommandClock()
    counters = WorkCounters(clock)
    # Price the final UID metadata and raw identity check, mailbox selections,
    # and acknowledgement using the same work units as the Bridge model. Add
    # the separate 1 s slow-MOVE calibration, leaving the second second for
    # journal work and variability. Full inventory verification is additional.
    counters.charge("SELECT", b'SELECT "INBOX"', 0)
    counters.charge("FETCH", b"(UID ENVELOPE FLAGS INTERNALDATE RFC822.SIZE)", 1)
    counters.charge("FETCH", b"(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])", 1)
    counters.charge("MOVE", b'UID MOVE 1 "Trash"', 0)
    assert counters.literal_loads_fetch == 1 and counters.db_rows_fetch == 2
    assert 1 + clock.elapsed < guarded.EXECUTE_MUTATION_SECONDS_PER_MESSAGE


def test_slow_inventory_drift_stops_before_response_reserve_and_still_resumes(slow_cleanup):
    case = slow_cleanup(count=165, timeout=300, mutation_seconds=1)
    preview = approve(case)
    case.peer.bulk_row_seconds = 0.268
    results = continue_to_end(case, preview["token"])
    assert len(results) > 1
    assert sum(result["done"] for result in results) == 165
    assert results[-1]["remaining"] == 0 and not results[-1]["partial"]


def test_all_remaining_rows_blocked_consumes_continuation_and_journals_no_changes(slow_cleanup):
    case = slow_cleanup(count=18, timeout=24, mutation_seconds=2)
    token = approve(case)["token"]
    first = case.service.execute("move_to_trash", token)
    continuation = first["continuation_token"]
    assert continuation and first["resume"]
    pending = [item for item in case.items if item.folder == "INBOX"]
    for item in pending:
        item.headers = item.headers.replace(
            b"notifications@example.invalid", b"synthetic-blocked@gmail.com"
        )
    before = list(mutations(case.peer))
    result = case.service.execute("move_to_trash", continuation)
    assert result["done"] == 0 and result["run_done"] == first["done"]
    assert result["partial"] and result["token_consumed"] and not result["resume"]
    assert result["continuation_token"] is None
    assert result["remaining"] == len(result["blocked"]) == len(pending)
    assert mutations(case.peer) == before
    with pytest.raises(MailValidationError, match="already used"):
        case.service.execute("move_to_trash", continuation)
    run = case.client.list_actions(token=token)["runs"][0]
    assert run["status"] == "partial" and run["blocked"] == len(pending)
    assert run["done"] == first["done"] and run["remaining"] == len(pending)


def test_failed_run_end_returns_partial_without_continuation(slow_cleanup, monkeypatch):
    case = slow_cleanup(count=2, mutation_seconds=0)
    token = approve(case)["token"]

    def fail_finish(*args, **kwargs):
        raise OSError("Synthetic final run journal failure")

    monkeypatch.setattr(case.service.backend.log, "finish_run", fail_finish)
    result = case.service.execute("move_to_trash", token)
    assert result["done"] == 2 and result["remaining"] == 0
    assert result["partial"] and not result["resume"]
    assert result["continuation_token"] is None
    run = case.client.list_actions(token=token)["runs"][0]
    assert run["status"] == "interrupted" and run["partial"] and run["done"] == 2


def test_failed_done_journal_never_continues_uncertain_row(slow_cleanup, monkeypatch):
    from proton_mail_mcp.action_log import LogTransaction

    case = slow_cleanup(count=18, timeout=24, mutation_seconds=2)
    token = approve(case)["token"]
    original = LogTransaction.append

    def fail_done(transaction, record):
        if record["action"] == "execute_progress" and record["message"]["status"] == "done":
            raise OSError("Synthetic missing durable outcome")
        return original(transaction, record)

    monkeypatch.setattr(LogTransaction, "append", fail_done)
    result = case.service.execute("move_to_trash", token)
    assert result["partial"] and not result["resume"]
    assert result["continuation_token"] is None and result["done"] == 1
    run = case.client.list_actions(token=token)["runs"][0]
    assert run["done"] == 0 and run["uncertain"] == 1 and run["remaining"] == 17
    assert run["status"] == "partial"
    with pytest.raises(MailValidationError, match="already used"):
        case.service.execute("move_to_trash", token)


def test_unclaimed_continuation_retry_uses_next_durable_chunk_index(slow_cleanup, monkeypatch):
    from proton_mail_mcp import cleanup_imap
    from proton_mail_mcp.mail import CallDeadlineExceeded

    case = slow_cleanup(count=18, timeout=24, mutation_seconds=2)
    token = approve(case)["token"]
    first = case.service.execute("move_to_trash", token)
    continuation = first["continuation_token"]
    assert continuation and first["resume"]
    original_preflight = case.service.backend._preflight
    original_fetch = cleanup_imap.fetch_rows
    preflight_complete = False

    def preflight(*args, **kwargs):
        nonlocal preflight_complete
        result = original_preflight(*args, **kwargs)
        preflight_complete = True
        return result

    def fail_final_fetch(*args, **kwargs):
        if preflight_complete:
            raise CallDeadlineExceeded("Synthetic first UID check timeout")
        return original_fetch(*args, **kwargs)

    before = list(mutations(case.peer))
    with monkeypatch.context() as patch:
        patch.setattr(case.service.backend, "_preflight", preflight)
        patch.setattr(cleanup_imap, "fetch_rows", fail_final_fetch)
        interrupted = case.service.execute("move_to_trash", continuation)
    assert interrupted["done"] == 0 and interrupted["partial"]
    assert interrupted["token_consumed"] is False
    assert interrupted["log_ids"]
    assert all(row["status"] == "not_attempted" for row in interrupted["messages"])
    assert mutations(case.peer) == before
    case.service.tokens.inspect(continuation, "move_to_trash")
    retried = continue_to_end(case, continuation)
    assert first["done"] + sum(result["done"] for result in retried) == 18
    assert retried[-1]["run_done"] == 18 and retried[-1]["remaining"] == 0
    assert all(item.folder == "Trash" for item in case.items)
    chunks = records(case)
    indexes = [record["chunk_index"] for record in chunks]
    assert len(indexes) == len(set(indexes))
    run = case.client.list_actions(token=token)["runs"][0]
    assert run["done"] == 18 and run["remaining"] == 0
    assert set(run["log_ids"]) == {record["log_id"] for record in chunks}


@pytest.mark.parametrize("count", [129, 165])
@pytest.mark.parametrize("change", ["protected", "moved"])
def test_slow_changed_continuation_blocks_one_row_without_repeating_full_inventory(
    slow_cleanup, count, change
):
    case = slow_cleanup(count=count, timeout=300, mutation_seconds=1)
    token = approve(case)["token"]
    case.peer.bulk_row_seconds = 0.268
    started = case.clock.elapsed
    first = case.service.execute("move_to_trash", token)
    assert case.clock.elapsed - started < 295
    assert first["resume"] and 0 < first["done"] < count
    changed = next(item for item in case.items if item.folder == "INBOX")
    changed_uid = str(case.peer.uid_of("INBOX", changed.pid))
    if change == "protected":
        changed.headers = changed.headers.replace(
            b"notifications@example.invalid", b"synthetic-blocked@gmail.com"
        )
    else:
        changed.folder = "Archive"
        case.peer.sync()
    before = len(mutations(case.peer))
    results = continue_to_end(case, first["continuation_token"])
    final = results[-1]
    assert sum(result["done"] for result in results) + first["done"] == count - 1
    assert final["run_done"] == count - 1 and final["remaining"] == 1
    assert final["partial"] and not final["resume"] and len(final["blocked"]) == 1
    assert all(item.folder == "Trash" for item in case.items if item is not changed)
    assert changed.folder == ("INBOX" if change == "protected" else "Archive")
    assert all(
        changed_uid not in uid_values(command.split()[2])
        for _, _, _, command in mutations(case.peer)[before:]
    )
    run = case.client.list_actions(token=token)["runs"][0]
    assert run["done"] == count - 1 and run["blocked"] == 1 and run["remaining"] == 1
