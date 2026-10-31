"""Rules retain a usable approval when final reads refuse before the first claim."""

import json

import pytest
from test_folder_rules import mailbox as mailbox
from test_folder_rules_resume import slow_moves

from proton_mail_mcp import guarded
from proton_mail_mcp.action_log import replay_records
from proton_mail_mcp.cleanup_response import cleanup_response
from proton_mail_mcp.guarded import EXECUTE_RESPONSE_BUDGET_S
from proton_mail_mcp.mail import MailValidationError


def delay_final_read(case, clock, monkeypatch):
    final_check = case.cleanup.backend._final_batch_check

    def delayed(connection, deadline, *args, **kwargs):
        # Read-only work leaves room for a one-row command but invalidates the
        # frozen larger prefix before its claim. All ordinary final identity
        # checks run.
        clock.elapsed = deadline.io_end - 3.5
        return final_check(connection, deadline, *args, **kwargs)

    monkeypatch.setattr(case.cleanup.backend, "_final_batch_check", delayed)
    return final_check


@pytest.mark.parametrize("start", ["preview", "continuation"])
def test_unclaimed_rules_budget_stop_preserves_exact_usable_approval(mailbox, monkeypatch, start):
    case = mailbox(count=30)
    clock = slow_moves(case, monkeypatch, timeout=120)
    # A full batched chunk: the delayed final read invalidates its frozen prefix.
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 200)
    preview = case.rules.preview_rules()
    original = case.cleanup.tokens.inspect(preview["token"], "apply_rules")
    first = case.rules.apply_rules(preview["token"], limit=5) if start == "continuation" else None
    held = first["continuation_token"] if first else preview["token"]
    pending = case.cleanup.tokens.inspect(held, "apply_rules")
    source_before = dict(case.fake.folders["INBOX"])
    perform = delay_final_read(case, clock, monkeypatch)
    began = clock.elapsed
    result = cleanup_response(case.rules.apply_rules(held), verbose=True)
    assert clock.elapsed - began <= EXECUTE_RESPONSE_BUDGET_S
    assert result["done"] == result["uncertain"] == 0
    assert result["remaining"] == len(pending.messages)
    assert result["error_code"] == "first_chunk_unavailable"
    assert "No approved chunk could complete" in result["error"]
    assert result["available"] and result["resume"]
    assert result["continuation_token"] == held
    assert not result["token_consumed"]
    assert result["continuation_reason"] is None
    assert case.cleanup.tokens.inspect(held, "apply_rules") == pending
    assert case.fake.folders["INBOX"] == source_before
    assert all(row["status"] == "not_attempted" for row in result["messages"])

    monkeypatch.setattr(case.cleanup.backend, "_final_batch_check", perform)
    token = result["continuation_token"]
    done = first["done"] if first else 0
    while token:
        began = clock.elapsed
        result = case.rules.apply_rules(token)
        assert clock.elapsed - began <= EXECUTE_RESPONSE_BUDGET_S
        assert result["done"] > 0 and result["uncertain"] == 0
        done += result["done"]
        assert result["run_done"] == done
        assert result["remaining"] == 30 - done
        if first:
            assert result["run_id"] == first["run_id"]
        token = result["continuation_token"]
        assert bool(token) is (result["remaining"] > 0)
        if token:
            resumed = case.cleanup.tokens.inspect(token, "apply_rules")
            if first:
                assert resumed.expires == pending.expires
            else:
                assert resumed.expires > original.expires
            assert resumed.expires_at == result["expires_at"]
            assert {row["uid"] for row in resumed.messages} == set(case.fake.folders["INBOX"])
    records = replay_records(
        [json.loads(line) for line in case.cleanup.backend.log.path.read_text().splitlines()]
    )
    completed = [row for record in records for row in record["messages"] if row["status"] == "done"]
    assert len(completed) == len({row["uid_before"] for row in completed}) == done == 30
    assert {row["uid_before"] for row in completed} == {row["uid"] for row in original.messages}
    assert not case.fake.folders["INBOX"]
    assert len(case.fake.folders["Folders/Review"]) == 30


@pytest.mark.parametrize("failure", ["expired", "journal_failure"])
def test_unclaimed_rules_hard_error_has_explicit_unavailable_reason(mailbox, monkeypatch, failure):
    case = mailbox(count=30)
    clock = slow_moves(case, monkeypatch, timeout=120)
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 200)
    preview = case.rules.preview_rules()
    first = case.rules.apply_rules(preview["token"], limit=5)
    held = first["continuation_token"]
    pending = case.cleanup.tokens.inspect(held, "apply_rules")
    source_before = dict(case.fake.folders["INBOX"])
    delay_final_read(case, clock, monkeypatch)
    if failure == "expired":
        # Valid at entry; the final read consumes the fixed resume window.
        offset = pending.expires - clock.elapsed - 20
        case.cleanup.tokens.clock = lambda: clock.elapsed + offset
    else:

        def refuse_checkpoint(*args, **kwargs):
            raise OSError("synthetic journal checkpoint failure")

        monkeypatch.setattr(case.cleanup.backend.log, "finish_run", refuse_checkpoint)
    began = clock.elapsed
    result = cleanup_response(case.rules.apply_rules(held))
    assert clock.elapsed - began <= EXECUTE_RESPONSE_BUDGET_S
    assert result["done"] == result["uncertain"] == 0
    assert result["remaining"] == len(pending.messages)
    assert result["error_code"] == "first_chunk_unavailable"
    assert not result["available"] and not result["resume"]
    assert result["continuation_token"] is None
    assert not result["token_consumed"]
    assert case.fake.folders["INBOX"] == source_before
    assert result["continuation_reason"]
    if failure == "expired":
        assert "expired" in result["continuation_reason"]
        with pytest.raises(MailValidationError, match="expired"):
            case.cleanup.tokens.inspect(held, "apply_rules")
    else:
        assert "durably verified" in result["continuation_reason"]
        assert case.cleanup.tokens.inspect(held, "apply_rules") == pending
