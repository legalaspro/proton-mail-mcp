"""An execute call cannot rotate approval without a new row outcome."""

import pytest
from test_cleanup_chunks import approve, moved, mutations, records
from test_cleanup_chunks import chunk_mailbox as chunk_mailbox

from proton_mail_mcp.mail import CallDeadlineExceeded, MailValidationError


def pending_approval(chunk_mailbox, continuing):
    case = chunk_mailbox(count=30, timeout=300, move_seconds=2)
    case.service.tokens.clock = case.clock.monotonic
    token = approve(case)
    prior_done = 0
    if continuing:
        first = case.service.execute("move_to_trash", token, limit=1)
        assert first["done"] == 1 and first["continuation_token"]
        token = first["continuation_token"]
        prior_done = first["done"]
        if continuing == "previously-blocked":
            # Keep a blocked identity from an earlier productive call in the
            # grant. Repeating that old outcome cannot justify another token.
            changed = case.fake.folders["INBOX"]["2"]
            changed.headers = changed.headers.replace(b"Subject: Hello", b"Subject: Changed")
            progressed = case.service.execute("move_to_trash", token, limit=1)
            assert progressed["done"] == 1 and len(progressed["blocked"]) == 1
            assert progressed["continuation_token"]
            token = progressed["continuation_token"]
            prior_done += progressed["done"]
    return case, token, prior_done


def assert_terminal_no_progress(result, *, prior_done, remaining, consumed, blocked):
    assert result["done"] == result["uncertain"] == 0
    assert result["run_done"] == prior_done
    assert result["remaining"] == remaining
    assert result["blocked"] == blocked
    assert result["error_code"] == "first_chunk_unavailable"
    assert "No approved chunk could complete" in result["error"]
    assert result["error"] and result["continuation_reason"]
    assert result["token_consumed"] is consumed
    assert result["continuation_token"] is None
    assert not result["resume"] and not result["available"]


@pytest.mark.parametrize(
    "continuing",
    [False, True, "previously-blocked"],
    ids=["initial", "continuation", "continuation-with-prior-blocked"],
)
def test_response_budget_before_claim_preserves_token_without_new_continuation(
    chunk_mailbox, monkeypatch, continuing
):
    case, token, prior_done = pending_approval(chunk_mailbox, continuing)
    grant = case.service.tokens.inspect(token, "move_to_trash")
    before_records = records(case)
    before_journal = case.log.read_bytes() if case.log.exists() else b""
    before_mutations = mutations(case)
    before_claims = list(case.claims)
    original_cap = case.service._cap_execute_deadline

    def cap_before_first_chunk(deadline, started):
        previously_capped = hasattr(deadline, "cleanup_admission_end")
        result = original_cap(deadline, started)
        if previously_capped:
            # Separate the response watchdog from the unchanged 300 s
            # configured admission. Two I/O seconds cannot admit a 2 s move
            # plus the mandatory 1 s journal reserve.
            deadline.end = min(deadline.end, case.clock.monotonic() + deadline.response_reserve + 2)
        return result

    def never_rotate(*args, **kwargs):
        pytest.fail("A no-progress call attempted to issue another continuation")

    with monkeypatch.context() as patch:
        patch.setattr(case.service, "_cap_execute_deadline", cap_before_first_chunk)
        patch.setattr(case.service.tokens, "continue_grant", never_rotate)
        # Retrying the same still-valid input must not create the second token
        # in the live failure's endless zero-progress continuation chain.
        for _ in range(2):
            result = case.service.execute("move_to_trash", token)
            assert_terminal_no_progress(
                result,
                prior_done=prior_done,
                remaining=30 - prior_done,
                consumed=False,
                blocked=grant.blocked,
            )
            assert "valid until expiry" in result["error"]
            assert "response budget" in result["error"].lower()
            assert case.service.tokens.inspect(token, "move_to_trash") == grant
            assert mutations(case) == before_mutations and case.claims == before_claims
            assert records(case) == before_records
            assert (case.log.read_bytes() if case.log.exists() else b"") == before_journal
    retried = case.service.execute("move_to_trash", token, limit=1)
    assert retried["done"] == 1 and retried["run_done"] == prior_done + 1


@pytest.mark.parametrize(
    "continuing",
    [False, True, "previously-blocked"],
    ids=["initial", "continuation", "continuation-with-prior-blocked"],
)
def test_claimed_not_attempted_chunk_cannot_publish_no_progress_continuation(
    chunk_mailbox, monkeypatch, continuing
):
    case, token, prior_done = pending_approval(chunk_mailbox, continuing)
    grant = case.service.tokens.inspect(token, "move_to_trash")
    before_records = records(case)
    before_mutations = mutations(case)
    before_claims = len(case.claims)
    authorized = []
    published = []
    original_continue = case.service.tokens.continue_grant

    def authorize_without_mutating(*args, **kwargs):
        # Exercise the publishing guard through a durable real backend chunk:
        # the batched chunk's final check claimed the approval, but the backend
        # stopped before marking any row attempted. A future backend failure
        # must not rotate this token.
        authorized.append(True)
        raise CallDeadlineExceeded("Synthetic interruption after approval before mutation")

    def capture_continuation(*args, **kwargs):
        published.append(True)
        return original_continue(*args, **kwargs)

    monkeypatch.setattr(case.service.backend, "_mutate_batch", authorize_without_mutating)
    monkeypatch.setattr(case.service.tokens, "continue_grant", capture_continuation)
    result = case.service.execute("move_to_trash", token)
    assert authorized == [True]
    assert_terminal_no_progress(
        result,
        prior_done=prior_done,
        remaining=30 - prior_done,
        consumed=True,
        blocked=grant.blocked,
    )
    assert not published
    assert "No progress was recorded" in result["continuation_reason"]
    assert "new preview" in result["error"].lower()
    assert mutations(case) == before_mutations and len(case.claims) == before_claims + 1
    chunks = records(case)
    assert chunks[: len(before_records)] == before_records
    assert len(chunks) == len(before_records) + 1
    assert result["log_ids"] == [chunks[-1]["log_id"]]
    assert all(row["status"] == "not_attempted" for row in chunks[-1]["messages"])
    run = case.service.backend.log.get_run(result["run_id"])
    assert run["done"] == prior_done and run["uncertain"] == 0
    assert run["remaining"] == 30 - prior_done
    with pytest.raises(MailValidationError, match="already used"):
        case.service.execute("move_to_trash", token)
    assert not published and mutations(case) == before_mutations


def test_uncertified_first_chunk_reserves_final_identity_read_before_claim(
    chunk_mailbox, monkeypatch
):
    # 4.77 s of I/O: one command (2 s, including its first row) for 30 rows
    # (29 x 0.05 s) plus the 1 s journal reserve fits only without reserving the
    # final identity read.
    case = chunk_mailbox(count=30, timeout=5.3, move_seconds=1.5)
    case.service.tokens.clock = case.clock.monotonic
    token = approve(case)
    grant = case.service.tokens.inspect(token, "move_to_trash")
    assert not grant.bounded_recovery
    assert all("label_states" not in row for row in grant.messages)
    verify = case.service.backend._verify
    perform = case.service.backend._final_batch_check
    uid = case.fake.uid
    apply = case.service.backend.apply
    final_read = []
    final_delays = []
    frozen = []

    def measured_verify(*args, **kwargs):
        result = verify(*args, **kwargs)
        # Preflight measures this bounded SELECT/FETCH work and must reserve
        # it before freezing the chunk, even without a preview certificate.
        case.clock.elapsed += 0.15
        return result

    def observed_perform(*args, **kwargs):
        final_read[:] = [True]
        try:
            return perform(*args, **kwargs)
        finally:
            final_read.clear()

    def delayed_final_uid_fetch(command, *args):
        result = uid(command, *args)
        if command == "FETCH" and final_read and final_read[0]:
            final_read[0] = False
            case.clock.elapsed += 0.15
            final_delays.append(0.15)
        return result

    def observed_apply(connection, deadline, action, params, rows, **kwargs):
        before_chunk = kwargs["before_chunk"]

        def capture_frozen_chunk():
            before_chunk()
            frozen.append((len(rows), deadline.io_end - case.clock.elapsed))

        return apply(
            connection,
            deadline,
            action,
            params,
            rows,
            **{**kwargs, "before_chunk": capture_frozen_chunk},
        )

    monkeypatch.setattr(case.service.backend, "_verify", measured_verify)
    monkeypatch.setattr(case.service.backend, "_final_batch_check", observed_perform)
    monkeypatch.setattr(case.fake, "uid", delayed_final_uid_fetch)
    monkeypatch.setattr(case.service.backend, "apply", observed_apply)
    results = []
    while token:
        result = case.service.execute("move_to_trash", token)
        results.append(result)
        assert result["done"] > 0, "Final identity reads must leave a productive first chunk"
        assert result["uncertain"] == 0 and not result["blocked"]
        token = result["continuation_token"]
        assert len(results) <= 3
    # The unreserved first chunk would freeze all 30 rows: after the 0.15 s final
    # read, their 3.45 s command allowance plus 1 s journal reserve no longer fits.
    # Reserving the measured read twice (the batched final check re-reads every
    # row) freezes 27 rows before any journal entry.
    assert frozen[0][0] == 27
    assert frozen[0][1] == pytest.approx(4.62)
    assert final_delays and sum(result["done"] for result in results) == 30
    assert len(moved(case)) == 30 and len(case.fake.folders["Trash"]) == 30
    assert not case.fake.folders["INBOX"]
    assert all(row["status"] == "done" for record in records(case) for row in record["messages"])
