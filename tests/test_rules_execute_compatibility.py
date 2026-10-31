"""Pin the base rules contract through the real, network-free rules caller."""

import pytest
from test_cleanup_mutations import MUTATIONS
from test_folder_rules import mailbox as mailbox
from test_folder_rules_resume import slow_moves

from proton_mail_mcp import guarded
from proton_mail_mcp.guarded import _ResponseBudgetReached
from proton_mail_mcp.mail import CallDeadlineExceeded, MailValidationError


def base_rules_stop_reason(reason, remaining, *, claimed):
    return (
        "No approved chunk could complete within this call; "
        + reason
        + f"; 0 done, {remaining} remaining unchanged, 0 uncertain; "
        + (
            "token consumed; inspect logged outcomes and request a new preview "
            "for any remaining messages"
            if claimed
            else "preview token remains valid until expiry"
        )
    )


@pytest.mark.parametrize("start", ["preview", "continuation"])
@pytest.mark.parametrize("failure", [_ResponseBudgetReached, CallDeadlineExceeded])
def test_rules_prechunk_response_exhaustion_claims_and_continues_like_base(
    mailbox, monkeypatch, start, failure
):
    case = mailbox(count=3)
    clock = slow_moves(case, monkeypatch, timeout=300)
    preview = case.rules.preview_rules()
    first = case.rules.apply_rules(preview["token"], limit=1) if start == "continuation" else None
    held = first["continuation_token"] if first else preview["token"]
    pending = case.cleanup.tokens.inspect(held, "apply_rules")
    source_before = dict(case.fake.folders["INBOX"])
    mutations_before = [call for call in case.fake.calls if call[0] in MUTATIONS]
    events = []
    log = case.cleanup.backend.log
    ensure_run = log.ensure_run
    claim = case.cleanup.tokens.claim

    def record_run(transaction, context):
        ensure_run(transaction, context)
        events.append(("ensure_run", context["run_id"]))

    def record_claim(token, action, **kwargs):
        assert action == "apply_rules" and token == held
        # The unchanged approval must already be durable when it is consumed.
        assert log.get_run(events[-1][1]) is not None
        events.append(("claim", token))
        return claim(token, action, **kwargs)

    def exhaust_before_chunk(connection, deadline, *args, **kwargs):
        # Configured admission remains open; only this call's response budget
        # expires before the backend starts or journals a physical chunk.
        clock.elapsed = deadline.io_end
        raise failure("Synthetic response exhaustion before the first chunk")

    with monkeypatch.context() as patch:
        patch.setattr(log, "ensure_run", record_run)
        patch.setattr(case.cleanup.tokens, "claim", record_claim)
        patch.setattr(case.cleanup.backend, "apply", exhaust_before_chunk)
        result = case.rules.apply_rules(held)

    assert [event[0] for event in events] == ["ensure_run", "claim"]
    assert result["token_consumed"] and result["partial"]
    assert result["done"] == result["uncertain"] == 0
    assert result["remaining"] == len(pending.messages)
    assert result["run_done"] == (first["done"] if first else 0)
    assert result["log_ids"] == [] and result["messages"] == []
    assert result["resume"] and result["available"]
    assert result["continuation_token"] and result["continuation_token"] != held
    assert result["continuation_reason"] is None
    expected = base_rules_stop_reason(
        "Execute response budget reached", len(pending.messages), claimed=True
    )
    assert result["stop_reason"] == result["error"] == expected
    assert result["error_code"] == "first_chunk_unavailable"
    run = log.get_run(result["run_id"])
    assert run["partial"] and run["stop_reason"] == expected
    assert run["done"] == result["run_done"] and run["uncertain"] == 0
    next_grant = case.cleanup.tokens.inspect(result["continuation_token"], "apply_rules")
    assert next_grant.messages == pending.messages
    assert next_grant.run_id == result["run_id"]
    if first:
        assert next_grant.expires == pending.expires
    else:
        assert next_grant.expires > pending.expires
    assert next_grant.expires_at == result["expires_at"]
    assert case.fake.folders["INBOX"] == source_before
    assert [call for call in case.fake.calls if call[0] in MUTATIONS] == mutations_before
    with pytest.raises(MailValidationError, match="already used"):
        case.rules.apply_rules(held)
    # Its exact remainder can subsequently make progress under the same run.
    resumed = case.rules.apply_rules(result["continuation_token"])
    assert resumed["run_id"] == result["run_id"] and resumed["done"] == len(pending.messages)
    assert not resumed["partial"] and not resumed["resume"]


def test_uncertified_rules_frozen_chunk_keeps_base_size(mailbox, monkeypatch):
    case = mailbox(count=30)
    # 4.635 s of I/O: 30 rows need one 2 s command plus 29 x 0.05 s and the 1 s
    # journal reserve, so reserving the 0.15 s final read would shrink the chunk.
    clock = slow_moves(case, monkeypatch, timeout=5.15)
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 200)
    preview = case.rules.preview_rules()
    grant = case.cleanup.tokens.inspect(preview["token"], "apply_rules")
    assert not grant.bounded_recovery
    assert all("label_states" not in row for row in grant.messages)
    verify = case.cleanup.backend._verify
    apply = case.cleanup.backend.apply
    frozen = []

    def measured_verify(*args, **kwargs):
        result = verify(*args, **kwargs)
        # Rules did not reserve this uncertified final-read measurement in the
        # base chunk-sizing calculation; the real final identity checks remain.
        clock.elapsed += 0.15
        return result

    def capture_apply(connection, deadline, action, params, rows, **kwargs):
        before_chunk = kwargs["before_chunk"]

        def capture_frozen():
            before_chunk()
            frozen.append((len(rows), deadline.io_end - clock.elapsed))

        return apply(
            connection,
            deadline,
            action,
            params,
            rows,
            **{**kwargs, "before_chunk": capture_frozen},
        )

    monkeypatch.setattr(case.cleanup.backend, "_verify", measured_verify)
    monkeypatch.setattr(case.cleanup.backend, "apply", capture_apply)
    result = case.rules.apply_rules(preview["token"])
    assert frozen[0][0] == 30
    assert frozen[0][1] == pytest.approx(4.485)
    assert result["done"] == 30 and result["remaining"] == 0 and not result["resume"]
    assert all(row["status"] == "done" for row in result["messages"])


@pytest.mark.parametrize("claimed", [False, True], ids=["unclaimed", "claimed"])
def test_rules_not_attempted_chunk_keeps_exact_base_stop_reason(mailbox, monkeypatch, claimed):
    case = mailbox(count=3)
    slow_moves(case, monkeypatch, timeout=300)
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 200)  # One chunk of all three.
    preview = case.rules.preview_rules()
    source_before = dict(case.fake.folders["INBOX"])

    def interrupt_before_mutation(*args, **kwargs):
        raise CallDeadlineExceeded("Synthetic interruption before any row is attempted")

    # Claimed: after the final check authorized the chunk; else during that check.
    stage = "_mutate_batch" if claimed else "_final_batch_check"
    monkeypatch.setattr(case.cleanup.backend, stage, interrupt_before_mutation)
    result = case.rules.apply_rules(preview["token"])
    expected = base_rules_stop_reason(
        'Cleanup interrupted during execute verification in "INBOX"; '
        "inspect logged outcomes before another action",
        3,
        claimed=claimed,
    )
    assert result["stop_reason"] == result["error"] == expected
    assert result["token_consumed"] is claimed
    assert result["done"] == result["uncertain"] == 0 and result["remaining"] == 3
    assert result["partial"] and result["resume"] and result["available"]
    assert result["continuation_reason"] is None
    assert result["error_code"] == "first_chunk_unavailable"
    assert bool(result["log_ids"]) and len(result["messages"]) == 3
    assert all(row["status"] == "not_attempted" for row in result["messages"])
    assert (result["continuation_token"] != preview["token"]) is claimed
    assert case.fake.folders["INBOX"] == source_before
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)
