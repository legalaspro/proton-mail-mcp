"""Confirmation keeps actual time reserves and sizes chunks to remaining time."""

from dataclasses import replace

import pytest
from action_records import execution_log
from bridge_costs import ZERO_COSTS
from budget_assertions import SocketBudgetAudit
from test_preview_proton_shape import SYNTHETIC_BATCH, assert_header_only, headers
from test_preview_proton_shape import shape_mailbox as shape_mailbox

from proton_mail_mcp.mail import MailValidationError


def prepare(shape_mailbox, monkeypatch, action, *, count=27, timeout=45):
    identifiers = (
        SYNTHETIC_BATCH
        if count == 27
        else [f"<budget-{index}@example.invalid>" for index in range(count)]
    )
    client, peer, clock = shape_mailbox(identifiers, mailbox_size=count, costs=ZERO_COSTS)
    client.settings = replace(client.settings, call_timeout=timeout, execute_timeout=timeout)
    service = client.cleanup
    if action == "remove_label":
        peer.folders["Labels/Review"] = {
            str(index + 1): headers(identifier) for index, identifier in enumerate(identifiers)
        }
        arguments = {"ids": identifiers, "params": {"label": "Review"}}
    else:
        arguments = {"ids": identifiers}
    elapsed = [0.0]
    original_resolve = service._resolve

    def resolve(*args, **kwargs):
        rows = original_resolve(*args, **kwargs)
        clock.elapsed += elapsed[0]
        return rows

    monkeypatch.setattr(service, "_resolve", resolve)
    claims = []
    original_claim = service.tokens.claim

    def claim(*args, **kwargs):
        claims.append(True)
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(service.tokens, "claim", claim)
    return client, peer, clock, arguments, elapsed, claims


def assert_headroom_refusal(client, peer, token, action, claims):
    with pytest.raises(MailValidationError, match="execute headroom") as error:
        client.cleanup.execute(action, token)
    assert not claims
    assert peer.mutations == 0
    assert not execution_log(client.settings.action_log_path)
    client.cleanup.tokens.inspect(token, action)
    reason = str(error.value)
    assert "retry when Bridge is idle" in reason
    assert "PROTON_MCP_EXECUTE_TIMEOUT" in reason
    assert "narrow" not in reason
    for value in (token, *SYNTHETIC_BATCH, "Newsletter", "example.invalid"):
        assert value not in reason


@pytest.mark.parametrize(
    "action,db_rows,command_seconds,confirmation_db_ms",
    [
        ("move_to_trash", 40_054, 0.127, 1.118),
        ("remove_label", 40_081, 0.120, 1.115),
    ],
)
def test_confirmation_db_drift_refuses_before_claim_and_retries(
    shape_mailbox, monkeypatch, action, db_rows, command_seconds, confirmation_db_ms
):
    client, peer, clock, arguments, elapsed, claims = prepare(shape_mailbox, monkeypatch, action)
    # Reproduce the independent 40k-row model's 35.7 s preview and +25.8%/+25.4%
    # confirmation DB costs. Charge its inventory time after real socket-based
    # resolution; unchanged rows need not be materialized in every mutation run.
    elapsed[0] = db_rows * 0.000889 + command_seconds
    preview = client.preview_action(action, **arguments)
    assert preview["token"] and preview["count"] == 27
    assert 35.70 < clock.elapsed < 35.80
    elapsed[0] = db_rows * confirmation_db_ms / 1000 + command_seconds
    assert 44.8 < elapsed[0] < 45.0
    assert_headroom_refusal(client, peer, preview["token"], action, claims)

    elapsed[0] = db_rows * 0.000889 + command_seconds
    client.settings = replace(client.settings, call_timeout=120, execute_timeout=120)
    client.cleanup.settings = client.settings
    audit = SocketBudgetAudit(client, peer, clock, preview["token"], action, monkeypatch)
    results = audit.finish(preview["token"])
    # Per-message label removal needs several calls; one batched MOVE (2 s plus
    # 0.05 s per further message) fits the 7.5 s left after re-resolution.
    assert results[0]["partial"] is (action == "remove_label")
    assert sum(result["count"] for result in results) == 27
    assert not results[-1]["partial"]
    assert len(claims) == len(results)
    # Label removal stays per message (STORE and EXPUNGE); batched MOVEs named
    # each approved UID exactly once (the audit checks every UID).
    if action == "remove_label":
        assert peer.mutations == 54
    else:
        assert len(audit.actual) == 27 and peer.mutations <= 27
    assert assert_header_only(peer)
    with pytest.raises(MailValidationError, match="already used"):
        client.cleanup.execute(action, preview["token"])


def test_twelve_percent_drift_completes_with_sufficient_call_budget(shape_mailbox, monkeypatch):
    client, peer, clock, arguments, elapsed, claims = prepare(
        shape_mailbox, monkeypatch, "move_to_trash"
    )
    elapsed[0] = 40_054 * 0.000889 + 0.127
    preview = client.preview_action("move_to_trash", **arguments)
    assert preview["token"]
    elapsed[0] = 40_054 * 0.001 + 0.127  # +12.5% DB cost, about 40.2 s resolution.
    # At 45 s this leaves too little room for a MOVE and its journal reserve.
    # Retrying with 120 s leaves the MOVE allowance after the read-only work.
    assert_headroom_refusal(client, peer, preview["token"], "move_to_trash", claims)
    client.settings = replace(client.settings, call_timeout=120, execute_timeout=120)
    client.cleanup.settings = client.settings
    audit = SocketBudgetAudit(client, peer, clock, preview["token"], "move_to_trash", monkeypatch)
    results = audit.finish(preview["token"])
    assert results[0]["partial"] and sum(result["count"] for result in results) == 27
    assert not results[-1]["partial"]
    assert peer.mutations == len(results)  # One batched MOVE per call.
    assert len(claims) == len(results)
    with pytest.raises(MailValidationError, match="already used"):
        client.move_to_trash(preview["token"])


@pytest.mark.parametrize(
    "action,count,timeout,resolution_seconds",
    [("move_to_trash", 200, 20, 10), ("remove_label", 100, 60, 33)],
)
def test_batch_estimate_limits_chunks_below_elapsed_gate(
    shape_mailbox, monkeypatch, action, count, timeout, resolution_seconds
):
    client, peer, _, arguments, elapsed, claims = prepare(
        shape_mailbox, monkeypatch, action, count=count, timeout=timeout
    )
    elapsed[0] = resolution_seconds
    preview = client.preview_action(action, **arguments)
    assert preview["token"] and preview["count"] == count
    assert resolution_seconds < timeout * 0.8
    # The complete batch cannot fit its planning allowance, but a smaller
    # chunk can. Repeated inventory costs eventually exhaust this call.
    result = client.cleanup.execute(action, preview["token"])
    assert 0 < result["count"] < count and result["partial"] is True
    assert result["remaining"] == count - result["count"]
    assert result["uncertain"] == 0
    assert "unchanged" in result["error"]
    assert result["log_ids"]
    assert len(claims) == 1
    assert peer.mutations == (result["count"] * 2 if action == "remove_label" else 1)
    with pytest.raises(MailValidationError, match="already used"):
        client.cleanup.execute(action, preview["token"])
