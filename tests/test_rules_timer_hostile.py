"""Independent review regressions, using synthetic in-memory mail only."""

import json

import pytest
from test_rules_timer import arrive, baseline, changed, mark
from test_rules_timer import timer_case as timer_case

from proton_mail_mcp.folder_rules import FolderRulesService
from proton_mail_mcp.mail import MailError, MailValidationError


def test_post_move_checkpoint_failure_keeps_pending_and_blocks_retry(timer_case, monkeypatch):
    case = timer_case()
    baseline(case)
    arrive(case, 3)
    original = case.timer._save_state

    def fail_completion(state):
        entry = state["mailboxes"]["INBOX"]
        if entry["uid"] == 3 and not entry["pending"]:
            raise OSError("synthetic completion fsync failure")
        return original(state)

    monkeypatch.setattr(case.timer, "_save_state", fail_completion)
    with pytest.raises((OSError, MailError)):
        case.timer.run()
    assert len(changed(case)) == 1
    assert mark(case) == {"uidvalidity": "1", "uid": 2, "pending": True}
    before = len(case.fake.calls)
    with pytest.raises(MailError, match="inspection"):
        case.timer.run()
    assert len(case.fake.calls) == before


def test_candidate_disappears_before_plan_does_not_revisit_backlog(timer_case, monkeypatch):
    case = timer_case()
    baseline(case)
    arrive(case, 3)
    arrive(case, 4)
    original = FolderRulesService._plan

    def disappear(*args, **kwargs):
        case.fake.folders["INBOX"].pop("3", None)
        case.fake.folders["All Mail"].pop("10003", None)
        return original(*args, **kwargs)

    monkeypatch.setattr(FolderRulesService, "_plan", disappear)
    result = case.timer.run()
    assert result["count"] == 1 and result["skipped"] == 1
    assert mark(case)["uid"] == 4
    assert set(case.fake.folders["INBOX"]) == {"1", "2"}
    assert [call[1] for call in changed(case)] == ["4"]


def test_incomplete_source_fetch_never_acknowledges_candidate(timer_case, monkeypatch):
    case = timer_case()
    baseline(case)
    arrive(case, 3)
    before = case.state.read_bytes()
    original = case.cleanup.client._metadata

    def incomplete(connection, uids, **kwargs):
        values = original(connection, uids, **kwargs)
        if case.fake.current == "INBOX":
            values.pop("3", None)
        return values

    monkeypatch.setattr(case.cleanup.client, "_metadata", incomplete)
    with pytest.raises((MailError, MailValidationError)):
        case.timer.run()
    assert case.state.read_bytes() == before and changed(case) == []
    assert "3" in case.fake.folders["INBOX"]


def test_candidate_cap_200_does_not_acknowledge_201st(timer_case):
    case = timer_case(count=0)
    baseline(case)
    for uid in range(1, 202):
        arrive(case, uid)
    result = case.timer.run(cap=200)
    assert result["count"] == 200 and mark(case)["uid"] == 200
    assert set(case.fake.folders["INBOX"]) == {"201"}
    assert case.timer.run(cap=200)["count"] == 1
    assert mark(case)["uid"] == 201


def test_second_chunk_verification_failure_keeps_first_checkpoint(timer_case, monkeypatch):
    case = timer_case(count=0)
    baseline(case)
    for uid in range(1, 52):
        arrive(case, uid)
    original = case.cleanup.backend.apply
    chunks = []

    def fail_second(*args, **kwargs):
        chunks.append(len(args[4]))
        if len(chunks) == 2:
            raise MailError("synthetic second chunk failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(case.cleanup.backend, "apply", fail_second)
    with pytest.raises(MailError):
        case.timer.run(cap=51)
    assert chunks == [50, 1]
    assert mark(case) == {"uidvalidity": "1", "uid": 50, "pending": False}
    assert set(case.fake.folders["INBOX"]) == {"51"}
    monkeypatch.setattr(case.cleanup.backend, "apply", original)
    assert case.timer.run(cap=51)["count"] == 1
    assert mark(case)["uid"] == 51


def test_uidvalidity_reset_keeps_legacy_mailbox_mark_and_refuses_its_source(timer_case):
    case = timer_case()
    baseline(case)
    state = json.loads(case.state.read_text())
    legacy = {"uidvalidity": "1", "uid": 8, "pending": False}
    state["mailboxes"]["Folders/Other"] = legacy
    case.state.write_text(json.dumps(state))
    arrive(case, 9, folder="Folders/Other")
    case.fake.uidvalidity = "2"
    before = case.state.read_bytes()
    with pytest.raises(MailValidationError, match="source"):
        case.timer.run(folder="Folders/Other")
    assert case.state.read_bytes() == before and changed(case) == []
    arrive(case, 3)
    result = case.timer.run()
    assert result["status"] == "reset" and result["count"] == 0
    assert mark(case) == {"uidvalidity": "2", "uid": 3, "pending": False}
    assert mark(case, "Folders/Other") == legacy
    assert changed(case) == []
