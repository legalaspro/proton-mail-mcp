"""Timeout recovery uses durable journals and an entirely synthetic mailbox."""

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from action_records import execution_log
from action_records import execution_records as replay_records
from starlette.testclient import TestClient
from test_cleanup_chunks import approve, mutations, planned
from test_cleanup_chunks import chunk_mailbox as chunk_mailbox
from test_cleanup_mutations import uid_values
from test_mail import message

from proton_mail_mcp.action_log import ActionLog, LogTransaction
from proton_mail_mcp.mail import MailError, MailValidationError
from proton_mail_mcp.server import create_app


def configure_budget(case, *, call=45, execute=300):
    case.mailbox.settings = replace(
        case.mailbox.settings, call_timeout=call, execute_timeout=execute
    )
    case.service.settings = case.mailbox.settings


def listed_ids(result):
    return {action["log_id"] for action in replay_records(result["actions"])}


def raw_records(case):
    return [json.loads(line) for line in case.log.read_text().splitlines()]


@pytest.mark.parametrize("count", [42, 50, 184, 200])
@pytest.mark.parametrize("move_seconds", [1, 2])
def test_slow_trash_calls_return_durable_log_ids_within_client_budget(
    chunk_mailbox, monkeypatch, count, move_seconds
):
    case = chunk_mailbox(count=count, move_seconds=move_seconds)
    configure_budget(case)
    token = approve(case)
    original_uid, original_apply = case.fake.uid, case.service.backend.apply
    observed_logs, chunk_sizes, prefixes = set(), [], []

    def observe_move(command, *args):
        if command == "MOVE":
            journal = raw_records(case)
            assert journal, "The undo log must exist before the first MOVE"
            named = set(uid_values(args[0]))
            active = [
                record
                for record in replay_records(journal)
                if record["action"] == "move_to_trash"
                and named
                <= {
                    entry["uid_before"]
                    for entry in record["messages"]
                    if entry["status"] == "uncertain"
                }
            ]
            assert len(active) == 1, "Each batched MOVE needs durable, scoped intents"
            observed_logs.add(active[0]["log_id"])
            assert args[1] == '"Trash"'
            before = case.log.read_bytes()
            if prefixes:
                assert before.startswith(prefixes[-1])
            prefixes.append(before)
        return original_uid(command, *args)

    def observe_chunk(connection, deadline, action, params, rows, **kwargs):
        assert len(rows) <= 200
        assert planned(rows) <= deadline.end - case.clock.elapsed
        chunk_sizes.append(len(rows))
        return original_apply(connection, deadline, action, params, rows, **kwargs)

    monkeypatch.setattr(case.fake, "uid", observe_move)
    monkeypatch.setattr(case.service.backend, "apply", observe_chunk)
    started = case.clock.elapsed
    result = case.mailbox.move_to_trash(token)
    elapsed = case.clock.elapsed - started

    assert elapsed <= 45 < 60, "A 300-second execute setting must not overrun the client"
    assert result["log_ids"] and set(result["log_ids"]) == observed_logs
    assert 0 < result["done"] <= count
    assert result["done"] == len(case.fake.folders["Trash"])
    assert result["done"] + result["remaining"] + result["uncertain"] == count
    assert result["partial"] is (result["done"] != count)
    assert result["token_consumed"] is True and case.claims == [True]
    assert chunk_sizes == [count]  # One batched chunk confirms the whole approval.
    assert all(call[0] == "MOVE" for call in mutations(case))
    assert case.log.read_bytes().startswith(prefixes[-1])
    assert listed_ids(case.mailbox.list_actions(token=token)) == observed_logs
    assert token.encode() not in case.log.read_bytes()
    with pytest.raises(MailValidationError, match="already used"):
        case.mailbox.move_to_trash(token)


@pytest.mark.parametrize("after_move", [False, True])
def test_interrupted_worker_is_found_by_token_and_time_and_undo_is_exact(
    chunk_mailbox, monkeypatch, after_move
):
    case = chunk_mailbox(count=8)
    # One batched chunk with three source folders: one MOVE command per folder.
    case.fake.folders["Archive"]["5"] = case.fake.folders["INBOX"].pop("5")
    case.fake.folders["Folders/Later"] = {
        uid: case.fake.folders["INBOX"].pop(uid) for uid in ("6", "7", "8")
    }
    homes = {folder: dict(case.fake.folders[folder]) for folder in ("INBOX", "Archive")}
    homes["Folders/Later"] = dict(case.fake.folders["Folders/Later"])
    configure_budget(case)
    token = approve(case)
    unrelated = message("<unrelated-trash@example.test>")
    case.fake.folders["Trash"]["900"] = unrelated
    original_uid, original_append = case.fake.uid, LogTransaction.append
    interrupted, attempts = False, 0

    class WorkerInterrupted(BaseException):
        pass

    def append_before_interruption(transaction, record):
        # A killed process cannot rescue its journal in a finally block.
        if interrupted:
            raise WorkerInterrupted()
        return original_append(transaction, record)

    def interrupt(command, *args):
        nonlocal interrupted, attempts
        if command == "MOVE":
            attempts += 1
            if attempts == 2:
                if after_move:
                    original_uid(command, *args)
                interrupted = True
                raise WorkerInterrupted()
        return original_uid(command, *args)

    monkeypatch.setattr(LogTransaction, "append", append_before_interruption)
    monkeypatch.setattr(case.fake, "uid", interrupt)
    with pytest.raises(WorkerInterrupted):
        case.mailbox.move_to_trash(token)
    monkeypatch.setattr(LogTransaction, "append", original_append)
    monkeypatch.setattr(case.fake, "uid", original_uid)

    actual_moved = 5 if after_move else 4
    assert len(case.fake.folders["INBOX"]) == 0
    assert len(case.fake.folders["Archive"]) == (0 if after_move else 1)
    assert len(case.fake.folders["Folders/Later"]) == 3
    before_lookup = case.log.read_bytes()
    by_token = case.mailbox.list_actions(token=token)
    assert len(replay_records(by_token["actions"])) == 1
    action = by_token["actions"][0]
    assert action["done"] == 4 and action["uncertain"] == 1
    assert action["not_attempted"] == 3
    assert action["status"] == "in_progress"
    timestamp = datetime.fromisoformat(action["timestamp"])
    by_time = case.mailbox.list_actions(
        since=timestamp.isoformat(),
        before=(timestamp + timedelta(microseconds=1)).isoformat(),
    )
    assert listed_ids(by_time) == {action["log_id"]}
    assert not case.mailbox.list_actions(token="unrelated-synthetic-token")["actions"]
    assert case.log.read_bytes() == before_lookup
    with pytest.raises(MailValidationError, match="already used"):
        case.mailbox.move_to_trash(token)

    before_undo = len(mutations(case))
    undone = case.mailbox.undo_action(action["log_id"])
    assert undone["count"] == actual_moved
    for folder, items in homes.items():
        assert sorted(map(id, case.fake.folders[folder].values())) == sorted(
            map(id, items.values())
        )
    assert list(case.fake.folders["Trash"].values()) == [unrelated]
    undo_moves = mutations(case)[before_undo:]
    assert len(undo_moves) == actual_moved
    assert [call[2] for call in undo_moves] == ['"INBOX"'] * 4 + ['"Archive"'] * (actual_moved - 4)
    assert all(call[0] == "MOVE" for call in undo_moves)
    assert case.log.read_bytes().startswith(before_lookup)


def test_lookup_filters_exact_batches_and_does_not_touch_log_or_mail(chunk_mailbox, monkeypatch):
    case = chunk_mailbox(count=3)
    results, tokens = [], []
    for identifier in case.identifiers:
        preview = case.mailbox.preview_action("move_to_trash", ids=[identifier])
        tokens.append(preview["token"])
        results.append(case.mailbox.move_to_trash(preview["token"]))

    before_bytes, before_stat = case.log.read_bytes(), case.log.stat()
    before_calls, before_claims = list(case.fake.calls), list(case.claims)
    monkeypatch.setattr(
        case.mailbox, "_session", lambda *a, **kw: pytest.fail("Lookup opened a mailbox session")
    )
    summaries = []
    for token, result in zip(tokens, results, strict=True):
        found = case.mailbox.list_actions(token=token)
        assert listed_ids(found) == set(result["log_ids"])
        assert found["truncated"] is False
        summaries.extend(replay_records(found["actions"]))
    for action in summaries:
        assert set(action) == {
            "log_id",
            "timestamp",
            "action",
            "status",
            "count",
            "done",
            "uncertain",
            "not_attempted",
            "partial",
        }
    for token in tokens:
        assert not case.mailbox.list_actions(token=token + "different")["actions"]
    second = summaries[1]
    assert listed_ids(
        case.mailbox.list_actions(since=second["timestamp"], before=summaries[2]["timestamp"])
    ) == {second["log_id"]}
    assert not replay_records(
        case.mailbox.list_actions(before=summaries[0]["timestamp"])["actions"]
    )
    newest = case.mailbox.list_actions(limit=4)
    assert [row["log_id"] for row in replay_records(newest["actions"])] == [
        summaries[2]["log_id"],
        summaries[1]["log_id"],
    ]
    assert newest["truncated"] is True

    visible = json.dumps(summaries)
    for value in [*tokens, *case.identifiers, "sender@example.com", "Hello"]:
        assert value not in visible
    for token in tokens:
        assert hashlib.sha256(token.encode("ascii")).hexdigest() not in visible
    after_stat = case.log.stat()
    assert case.log.read_bytes() == before_bytes
    assert (after_stat.st_mode, after_stat.st_mtime_ns, after_stat.st_ctime_ns) == (
        before_stat.st_mode,
        before_stat.st_mtime_ns,
        before_stat.st_ctime_ns,
    )
    assert case.fake.calls == before_calls and case.claims == before_claims


def test_uncertain_move_cannot_restore_a_matching_trash_copy_when_source_reappears(
    chunk_mailbox, monkeypatch
):
    case = chunk_mailbox(count=1)
    token = approve(case)
    original_uid, original_append = case.fake.uid, LogTransaction.append
    interrupted = False
    unrelated = message(case.identifiers[0])

    class WorkerInterrupted(BaseException):
        pass

    def append_before_interruption(transaction, record):
        if interrupted:
            raise WorkerInterrupted()
        return original_append(transaction, record)

    def interrupt(command, *args):
        nonlocal interrupted
        if command == "MOVE":
            # An identical header in Trash is not proof this MOVE happened:
            # the source can have acquired a new UID after the intent was saved.
            case.fake.folders["INBOX"]["500"] = case.fake.folders["INBOX"].pop("1")
            case.fake.folders["Trash"]["901"] = unrelated
            interrupted = True
            raise WorkerInterrupted()
        return original_uid(command, *args)

    monkeypatch.setattr(LogTransaction, "append", append_before_interruption)
    monkeypatch.setattr(case.fake, "uid", interrupt)
    with pytest.raises(WorkerInterrupted):
        case.mailbox.move_to_trash(token)
    monkeypatch.setattr(LogTransaction, "append", original_append)
    monkeypatch.setattr(case.fake, "uid", original_uid)

    action = case.mailbox.list_actions(token=token)["actions"][0]
    assert action["done"] == 0 and action["uncertain"] == 1
    result = case.mailbox.undo_action(action["log_id"])
    assert result["count"] == 0 and result["partial"] is True
    assert set(case.fake.folders["INBOX"]) == {"500"}
    assert case.fake.folders["Trash"] == {"901": unrelated}
    assert not mutations(case)


def test_lookup_missing_log_does_not_create_state(chunk_mailbox, monkeypatch):
    case = chunk_mailbox(count=1)
    missing = case.log.parent / "absent" / "actions.jsonl"
    case.mailbox.settings = replace(case.mailbox.settings, action_log_path=str(missing))
    monkeypatch.setattr(
        case.mailbox, "_session", lambda *a, **kw: pytest.fail("Lookup opened a mailbox session")
    )
    assert case.mailbox.list_actions() == {"actions": [], "truncated": False}
    assert not missing.parent.exists()
    assert not case.fake.calls and not case.claims


def test_legacy_completed_record_is_recoverable_by_time_without_token(chunk_mailbox):
    case = chunk_mailbox(count=1)
    token = approve(case)
    result = case.mailbox.move_to_trash(token)
    legacy = replay_records(raw_records(case))[0]
    legacy.pop("token_digest")
    legacy.pop("status")
    # Reproduce the single completed record written by the old version, in a
    # fresh synthetic log; the live journal is never rewritten by the lookup.
    path = case.log.with_name("synthetic-legacy.jsonl")
    path.write_text(json.dumps(legacy) + "\n")
    case.mailbox.settings = replace(case.mailbox.settings, action_log_path=str(path))
    case.service.backend.log = ActionLog(path)
    before = path.read_bytes()
    found = case.mailbox.list_actions(since=legacy["timestamp"])
    assert listed_ids(found) == set(result["log_ids"])
    assert found["actions"][0]["status"] == "completed"
    assert found["actions"][0]["done"] == 1
    assert not case.mailbox.list_actions(token=token)["actions"]
    assert path.read_bytes() == before
    undone = case.mailbox.undo_action(result["log_ids"][0])
    assert undone["count"] == 1 and not case.fake.folders["Trash"]


@pytest.mark.parametrize("change", ["batch", "index", "identity"])
def test_replay_rejects_progress_outside_its_approved_batch(chunk_mailbox, change):
    case = chunk_mailbox(count=1)
    token = approve(case)
    case.mailbox.move_to_trash(token)
    journal = raw_records(case)
    progress = next(record for record in journal if record["action"] == "execute_progress")
    if change == "batch":
        progress["action_log_id"] = "0" * 32
    elif change == "index":
        progress["index"] = 1
    else:
        progress["message"]["message_id"] = "<outside-preview@example.test>"
    with pytest.raises(MailError, match="invalid execution"):
        replay_records(journal)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"since": "2026-10-18T12:00:00"},
        {"before": "2026-13-18"},
        {"since": "invalid-private-input"},
        {"limit": 0},
        {"limit": True},
    ],
)
def test_lookup_validates_without_opening_mail_or_creating_log(chunk_mailbox, monkeypatch, kwargs):
    case = chunk_mailbox(count=1)
    monkeypatch.setattr(
        case.mailbox, "_session", lambda *a, **kw: pytest.fail("Lookup opened a mailbox session")
    )
    with pytest.raises(MailValidationError) as error:
        case.mailbox.list_actions(**kwargs)
    assert "invalid-private-input" not in str(error.value)
    assert not execution_log(case.log) and not case.fake.calls


def test_lookup_mcp_tool_is_read_only_and_calls_local_backend(chunk_mailbox, monkeypatch):
    case = chunk_mailbox(count=1)
    token = approve(case)
    result = case.mailbox.move_to_trash(token)
    before = case.log.read_bytes()
    # This test isolates the local lookup from independent startup maintenance.
    monkeypatch.setattr(case.mailbox.index_autorebuild, "start", lambda: None)
    monkeypatch.setattr(
        case.mailbox, "_session", lambda *a, **kw: pytest.fail("Lookup opened a mailbox session")
    )
    app = create_app(replace(case.mailbox.settings, confirm_mutations=False), case.mailbox)
    headers = {
        "Authorization": f"Bearer {case.mailbox.settings.token}",
        "Accept": "application/json, text/event-stream",
    }
    with TestClient(app, base_url="http://127.0.0.1") as http:
        listed = http.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers
        ).json()["result"]["tools"]
        tool = next(item for item in listed if item["name"] == "list_actions")
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["openWorldHint"] is False
        assert set(tool["inputSchema"]["properties"]) == {
            "token",
            "since",
            "before",
            "limit",
            "action",
            "client_name",
        }
        response = http.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_actions", "arguments": {"token": token}},
            },
            headers=headers,
        ).json()["result"]
    assert not response.get("isError")
    assert listed_ids(response["structuredContent"]) == set(result["log_ids"])
    assert token not in json.dumps(response) and case.identifiers[0] not in json.dumps(response)
    assert case.log.read_bytes() == before
