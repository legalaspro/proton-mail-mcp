"""Manual rule continuations preserve the reviewed source and rule assignment."""

import json
from dataclasses import replace

import pytest
from test_folder_rules import mailbox as mailbox
from test_folder_rules import rule
from test_mail import message
from test_preview_proton_shape import WorkClock

from proton_mail_mcp import guarded, mail
from proton_mail_mcp.action_log import replay_records
from proton_mail_mcp.mail import MailValidationError


def slow_moves(case, monkeypatch, *, timeout=76):
    # Rule continuations across calls: one-UID batched chunks (each still planned
    # with its command-group allowance) keep calls small, as per-message moves did.
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 1)
    clock = WorkClock()
    monkeypatch.setattr(mail, "time", clock)
    settings = replace(case.cleanup.settings, call_timeout=timeout, execute_timeout=timeout)
    case.cleanup.settings = case.cleanup.client.settings = settings
    case.cleanup.tokens.clock = lambda: clock.elapsed
    uid = case.fake.uid

    def slow(command, *args):
        result = uid(command, *args)
        if command == "MOVE":
            clock.elapsed += 2
        return result

    monkeypatch.setattr(case.fake, "uid", slow)
    return clock


@pytest.mark.parametrize("count", [129, 200])
def test_rules_complete_same_approval_and_one_run_across_calls(mailbox, monkeypatch, count):
    case = mailbox(count=count)
    slow_moves(case, monkeypatch)
    preview = case.rules.preview_rules()
    assert preview["estimated_calls"] > 1
    token = preview["token"]
    ids = {id(row) for row in case.fake.folders["INBOX"].values()}
    calls, logs, run_ids, done = 0, [], set(), 0
    resolve = case.cleanup._resolve

    def bounded(*args, **kwargs):
        assert kwargs.get("bound_rows") is not None
        assert len(kwargs["bound_rows"]) <= count
        return resolve(*args, **kwargs)

    monkeypatch.setattr(case.cleanup, "_resolve", bounded)
    newcomer = message("<unapproved-rule-arrival@example.test>")
    while token:
        result = case.rules.apply_rules(token)
        calls += 1
        assert calls <= 10 and result["done"] > 0
        done += result["done"]
        logs.extend(result["log_ids"])
        run_ids.add(result["run_id"])
        with pytest.raises(MailValidationError, match="already used"):
            case.rules.apply_rules(token)
        if calls == 1:
            case.fake.folders["INBOX"]["901"] = newcomer
            case.fake.folders["All Mail"]["901"] = newcomer
        token = result["continuation_token"]
        assert result["resume"] is (token is not None)
    assert calls > 1 and done == count and len(run_ids) == 1
    assert not result["partial"] and result["remaining"] == 0
    assert case.fake.folders["INBOX"] == {"901": newcomer}
    assert {id(row) for row in case.fake.folders["Folders/Review"].values()} == ids
    # One-UID chunks: list every chunk record of this run.
    found = case.cleanup.backend.log.list_actions(token=preview["token"], limit=200)
    assert len(found["runs"]) == 1
    run = found["runs"][0]
    assert run["action"] == "apply_rules" and run["status"] == "completed"
    assert run["done"] == run["approved_total"] == count
    assert run["log_ids"] == logs
    assert {row["log_id"] for row in found["actions"]} == set(run["log_ids"])


def test_rule_groups_share_run_total_and_chunk_indexes(mailbox, monkeypatch):
    case = mailbox(count=12, rules=[rule("subject", "First"), rule(folder="Folders/Other")])
    for uid, item in case.fake.folders["INBOX"].items():
        if int(uid) <= 6:
            item.headers = item.headers.replace(b"Subject: Hello", b"Subject: First")
    slow_moves(case, monkeypatch, timeout=24)
    first = case.rules.apply_rules(case.rules.preview_rules()["token"])
    assert first["partial"] and first["resume"] and first["approved_total"] == 12
    second = case.rules.apply_rules(first["continuation_token"])
    assert not second["partial"] and second["run_id"] == first["run_id"]
    found = case.cleanup.backend.log.list_actions()
    assert len(found["runs"]) == 1 and found["runs"][0]["done"] == 12
    chunks = replay_records(
        [json.loads(line) for line in case.cleanup.backend.log.path.read_text().splitlines()]
    )
    assert set(found["runs"][0]["log_ids"]) == {row["log_id"] for row in found["actions"]}
    assert {row["approved_total"] for row in chunks} == {12}
    assert len({row["chunk_index"] for row in chunks}) == len(chunks)
    assert len(case.fake.folders["Folders/Review"]) == 6
    assert len(case.fake.folders["Folders/Other"]) == 6


def test_rule_continuation_refuses_changed_row_and_changed_rules(mailbox, monkeypatch):
    case = mailbox(count=12)
    slow_moves(case, monkeypatch, timeout=24)
    first = case.rules.apply_rules(case.rules.preview_rules()["token"])
    assert first["resume"]
    uid, untouched = next(iter(case.fake.folders["INBOX"].items()))
    untouched.headers = untouched.headers.replace(b"Subject: Hello", b"Subject: Changed")
    before = len(case.fake.folders["Folders/Review"])
    with pytest.raises(MailValidationError, match="changed"):
        case.rules.apply_rules(first["continuation_token"])
    assert case.fake.folders["INBOX"][uid] is untouched
    assert len(case.fake.folders["Folders/Review"]) == before
    assert case.cleanup.tokens.inspect(first["continuation_token"], "apply_rules")
    case = mailbox(count=12)
    slow_moves(case, monkeypatch, timeout=24)
    first = case.rules.apply_rules(case.rules.preview_rules()["token"])
    case.path.write_text(json.dumps({"version": 1, "rules": [rule(folder="Folders/Other")]}))
    with pytest.raises(MailValidationError, match="rules changed"):
        case.rules.apply_rules(first["continuation_token"])


def test_rule_continuation_keeps_skipped_reviewed_rows_pinned(mailbox, monkeypatch):
    case = mailbox(count=13, rules=[rule("subject", "Hello")])
    skipped = case.fake.folders["INBOX"]["13"]
    skipped.headers = skipped.headers.replace(b"sender@example.com", b"relative@gmail.com")
    slow_moves(case, monkeypatch, timeout=24)
    first = case.rules.apply_rules(case.rules.preview_rules()["token"])
    assert first["resume"]
    skipped.headers = skipped.headers.replace(b"10:00:00", b"11:00:00")
    before = len(case.fake.folders["Folders/Review"])
    with pytest.raises(MailValidationError, match="Reviewed rule messages changed"):
        case.rules.apply_rules(first["continuation_token"])
    assert len(case.fake.folders["Folders/Review"]) == before
