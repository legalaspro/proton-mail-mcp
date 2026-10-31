"""Approval retries remain safe until resolution and preflight have succeeded."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from action_records import execution_records as replay_records
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations
from resume_assertions import identity, journal_identity
from resume_recording import journal_recording_mutator
from test_cleanup_guard import settings as settings
from test_cleanup_guard import setup

from proton_mail_mcp.mail import CallDeadlineExceeded, MailValidationError


@pytest.mark.parametrize("stage", ["resolution", "preflight", "final_deadline"])
def test_execute_timeout_before_mutation_keeps_approval_retryable(settings, monkeypatch, stage):
    service, _, backend = setup(settings)
    preview = service.preview_action("archive", query={})
    token = preview["token"]
    assert token
    original_resolve = service._resolve
    original_apply = backend.apply

    def timed_out(*args, **kwargs):
        raise CallDeadlineExceeded("Synthetic read-only timeout")

    def expired_preflight(
        connection,
        deadline,
        action,
        params,
        rows,
        *,
        before_chunk=None,
        before_mutation=None,
        token_digest=None,
    ):
        deadline.end = 0
        before_mutation()
        raise AssertionError("An expired deadline must not authorize mutation")

    if stage == "resolution":
        monkeypatch.setattr(service, "_resolve", timed_out)
    else:
        monkeypatch.setattr(
            backend, "apply", expired_preflight if stage == "final_deadline" else timed_out
        )
    with pytest.raises(CallDeadlineExceeded):
        service.execute("archive", token)
    assert backend.applied == []
    monkeypatch.setattr(service, "_resolve", original_resolve)
    monkeypatch.setattr(backend, "apply", original_apply)
    assert service.execute("archive", token)["count"] == 1
    assert len(backend.applied) == 1
    with pytest.raises(MailValidationError, match="already used"):
        service.execute("archive", token)


def test_socket_preflight_timeout_keeps_approval_retryable(tmp_path, monkeypatch):
    sim = BridgeSim()
    try:
        client = make_client(sim, tmp_path)
        original = sim.add(Msg(hdr("<retry@example.invalid>"), folder="INBOX"))
        preview = client.preview_action("archive", ids=[original.message_id()])
        backend = client._cleanup_service.backend
        original_preflight = backend._preflight

        def timed_out(*args, **kwargs):
            raise CallDeadlineExceeded("Synthetic preflight timeout")

        monkeypatch.setattr(backend, "_preflight", timed_out)
        before = mutations(sim)
        with pytest.raises(CallDeadlineExceeded):
            client.archive(preview["token"])
        assert mutations(sim) == before
        monkeypatch.setattr(backend, "_preflight", original_preflight)
        assert client.archive(preview["token"])["count"] == 1
    finally:
        sim.close()


def test_socket_first_uid_check_timeout_does_not_consume_approval(tmp_path, monkeypatch):
    from proton_mail_mcp import cleanup_imap

    sim = BridgeSim()
    try:
        client = make_client(sim, tmp_path)
        original = sim.add(Msg(hdr("<uid-retry@example.invalid>"), folder="INBOX"))
        preview = client.preview_action("archive", ids=[original.message_id()])
        backend = client._cleanup_service.backend
        original_preflight, original_fetch = backend._preflight, cleanup_imap.fetch_rows
        preflight_complete = False

        def preflight(*args, **kwargs):
            nonlocal preflight_complete
            result = original_preflight(*args, **kwargs)
            preflight_complete = True
            return result

        def fetch(*args, **kwargs):
            if preflight_complete:
                raise CallDeadlineExceeded("Synthetic final UID verification timeout")
            return original_fetch(*args, **kwargs)

        monkeypatch.setattr(backend, "_preflight", preflight)
        monkeypatch.setattr(cleanup_imap, "fetch_rows", fetch)
        before = mutations(sim)
        interrupted = client.archive(preview["token"])
        assert interrupted["count"] == 0 and interrupted["partial"] is True
        assert all(row["status"] == "not_attempted" for row in interrupted["messages"])
        assert mutations(sim) == before
        monkeypatch.setattr(backend, "_preflight", original_preflight)
        monkeypatch.setattr(cleanup_imap, "fetch_rows", original_fetch)
        assert client.archive(preview["token"])["count"] == 1
    finally:
        sim.close()


def test_undo_refuses_tampered_membership_identity(tmp_path):
    sim = BridgeSim()
    try:
        client = make_client(sim, tmp_path)
        original = sim.add(
            Msg(hdr("<tamper@example.invalid>"), folder="INBOX", labels={"Labels/Review"})
        )
        preview = client.preview_action("move_to_trash", ids=[original.message_id()])
        completed = client.move_to_trash(preview["token"])
        log = Path(client.settings.action_log_path)
        record = replay_records([json.loads(line) for line in log.read_text().splitlines()])[0]
        memberships = record["messages"][0]["memberships_before"]
        label = next(item for item in memberships if item["folder"] == "Labels/Review")
        label["message_id"] = "<someone-else@example.invalid>"
        log.write_text(json.dumps(record) + "\n")
        before = mutations(sim)
        restored = client.undo_action(completed["log_ids"][0])
        assert restored["count"] == 0 and restored["partial"] is True
        assert restored["missing"][0]["reason"] == (
            'logged membership identity changed during undo in "Labels/Review"'
        )
        assert restored["missing"][0]["folder"] == "Labels/Review"
        assert mutations(sim) == before
    finally:
        sim.close()


@pytest.mark.parametrize("elapsed", [36.0, 36.01, 40.0, 40.49])
def test_preview_reserves_twenty_percent_for_execute(settings, monkeypatch, elapsed):
    from test_preview_proton_shape import WorkClock

    from proton_mail_mcp import mail

    clock = WorkClock()
    monkeypatch.setattr(mail, "time", clock)
    # Keep this boundary tied to an explicit 45 s call budget.
    settings = replace(settings, call_timeout=45)
    service, _, backend = setup(settings, count=27)
    original_resolve = service._resolve

    def resolve(*args, **kwargs):
        rows = original_resolve(*args, **kwargs)
        clock.elapsed += elapsed
        return rows

    monkeypatch.setattr(service, "_resolve", resolve)
    preview = service.preview_action("move_to_trash", query={})
    assert preview["count"] == 27
    assert preview["partial"] is False and preview["total_is_exact"] is True
    if elapsed <= 36.0:
        assert preview["token"]
        # The approval gate passes exactly at 80%. Execution still needs its
        # batched chunk allowance after its own 36 s re-resolution.
        service.client.settings = replace(settings, call_timeout=120, execute_timeout=120)
        service.settings = service.client.settings
        journal_recording_mutator(monkeypatch, backend, settings.action_log_path)
        grant = service.tokens.inspect(preview["token"], "move_to_trash")
        approved = {identity(row) for row in grant.messages}
        token, results = preview["token"], []
        while token:
            started = clock.elapsed
            result = service.execute("move_to_trash", token)
            assert clock.elapsed - started < 50
            results.append(result)
            assert result["run_id"] == results[0]["run_id"]
            actual = [identity(row) for _, _, rows in backend.applied for row in rows]
            assert len(actual) == len(set(actual)) and set(actual) <= approved
            claimed = [row for call in results for row in call["messages"]]
            with backend.log.transaction(readonly=True) as transaction:
                records = transaction.records()
            journaled = [row for record in records for row in record["messages"]]
            assert all(row["status"] == "done" for row in [*claimed, *journaled])
            assert len(claimed) == len(journaled) == len(actual)
            assert {journal_identity(row) for row in claimed} == set(actual)
            assert {journal_identity(row) for row in journaled} == set(actual)
            assert sum(call["done"] for call in results) == result["run_done"] == len(actual)
            assert result["remaining"] == len(approved - set(actual))
            assert result["uncertain"] == 0
            run = backend.log.get_run(result["run_id"])
            assert (run["status"] == "completed") == (set(actual) == approved)
            token = result["continuation_token"]
            if token:
                continuation = service.tokens.inspect(token, "move_to_trash")
                assert result["partial"] and result["resume"] and result["stop_reason"]
                assert {identity(row) for row in continuation.messages} == approved - set(actual)
                assert continuation.expires > grant.expires
                assert continuation.expires_at == results[0]["expires_at"]
            assert len(results) <= 27
        # One batched chunk (2 s plus 26 x 0.05 s and the 1 s journal reserve)
        # still fits the 6.2 s left after execution's own 36 s re-resolution.
        assert len(results) == 1 and not results[0]["partial"]
        assert sum(result["count"] for result in results) == 27 and set(actual) == approved
        with pytest.raises(MailValidationError, match="already used"):
            service.execute("move_to_trash", preview["token"])
    else:
        assert preview["token"] is None and preview["expires_at"] is None
        assert "headroom" in preview["reason"] and "80%" in preview["reason"]
        assert "retry when Bridge is idle" in preview["reason"]
        assert "PROTON_MCP_CALL_TIMEOUT" in preview["reason"]
        assert "narrow" not in preview["reason"]
        assert not service.tokens._grants
        assert not backend.applied


@pytest.mark.parametrize("elapsed", [40.5, 44.9, 44.97])
def test_preview_response_reserve_refuses_before_total_budget(settings, monkeypatch, elapsed):
    from test_preview_proton_shape import WorkClock

    from proton_mail_mcp import mail

    clock = WorkClock()
    monkeypatch.setattr(mail, "time", clock)
    settings = replace(settings, call_timeout=45)
    service, _, backend = setup(settings, count=27)
    original_resolve = service._resolve

    def resolve(*args, **kwargs):
        rows = original_resolve(*args, **kwargs)
        clock.elapsed += elapsed
        return rows

    monkeypatch.setattr(service, "_resolve", resolve)
    preview = service.preview_action("move_to_trash", query={})
    # The final 10% belongs to stream cleanup and returning the refusal.
    assert preview["token"] is None and preview["partial"] is True
    assert preview["total_is_exact"] is False
    assert "deadline" in preview["reason"].lower()
    assert clock.elapsed < settings.call_timeout
    assert not service.tokens._grants and not backend.applied
