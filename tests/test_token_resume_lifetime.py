"""Started approvals outlive preview expiry without widening or renewing their scope."""

import copy
from dataclasses import replace

import pytest
from bridge_sim import Msg, hdr, mutations
from resume_assertions import identity, journal_identity
from test_cleanup_resume import records
from test_cleanup_resume import slow_cleanup as slow_cleanup
from test_folder_rules import mailbox as mailbox
from test_folder_rules_resume import slow_moves

from proton_mail_mcp.guarded import ACTIONS, RESUME_TOKEN_TTL_SECONDS, TokenStore
from proton_mail_mcp.mail import MailClient, MailValidationError
from proton_mail_mcp.security import Credentials

ACTION = "move_to_folder"
PARAMS = {"folder": "Folders/Reviewed"}
ROWS = [
    {
        "folder": "INBOX",
        "uidvalidity": "7",
        "uid": str(index),
        "message_id": f"<resume-{index}@example.test>",
    }
    for index in range(1, 4)
]


@pytest.mark.parametrize("action", sorted(ACTIONS | {"apply_rules"}))
def test_first_claim_starts_one_fixed_resume_window(action):
    clock = [0.0]
    store = TokenStore(ttl=10, clock=lambda: clock[0])
    token, preview_expiry = store.issue(action, {}, {"query": {"folder": "INBOX"}}, ROWS)
    clock[0] = 9
    claimed = store.claim(token, action)
    assert claimed.expires == 9 + RESUME_TOKEN_TTL_SECONDS
    assert claimed.resume_started and claimed.expires_at != preview_expiry
    deadline, deadline_text = claimed.expires, claimed.expires_at
    clock[0] = 11  # The original preview has expired while the first chunk ran.
    continuation = store.continue_grant(claimed, claimed.messages[1:])
    assert continuation
    remainder = store.inspect(continuation, action)
    assert remainder.messages == ROWS[1:]
    assert remainder.action_hash == claimed.action_hash
    assert remainder.expires == deadline and remainder.expires_at == deadline_text
    clock[0] = deadline - 1
    claimed = store.claim(continuation, action)
    next_token = store.continue_grant(claimed, claimed.messages[1:])
    assert next_token
    assert store.inspect(next_token, action).expires == deadline
    clock[0] = deadline
    with pytest.raises(MailValidationError, match="expired"):
        store.claim(next_token, action)
    assert not store._grants


def test_inspection_and_unclaimed_retry_do_not_start_a_resume_window():
    clock = [0.0]
    store = TokenStore(ttl=10, clock=lambda: clock[0])
    token, expiry = store.issue(ACTION, PARAMS, {}, ROWS)
    clock[0] = 9
    grant = store.inspect(token, ACTION)
    assert grant.expires == 10 and grant.expires_at == expiry and not grant.resume_started
    clock[0] = 10
    with pytest.raises(MailValidationError, match="expired"):
        store.claim(token, ACTION)
    assert not store._grants


@pytest.mark.parametrize("confirmed", [False, True])
def test_only_host_confirmed_approval_can_start_resume_window(confirmed):
    clock = [0.0]
    store = TokenStore(ttl=10, clock=lambda: clock[0], confirmation_required=True)
    token, expiry = store.issue(ACTION, PARAMS, {}, ROWS)
    approval = store.describe(token, ACTION)
    clock[0] = 9
    with pytest.raises(MailValidationError, match="confirmation"):
        store.claim(token, ACTION)
    pending = store.describe(token, ACTION)
    assert pending.expires == 10 and pending.expires_at == expiry
    assert not pending.resume_started
    if confirmed:
        store.confirm(token, ACTION, approval.action_hash)
        granted = store.claim(token, ACTION)
        assert granted.resume_started and granted.expires == 9 + RESUME_TOKEN_TTL_SECONDS
    else:
        store.revoke(token, ACTION)
        with pytest.raises(MailValidationError, match="already used"):
            store.claim(token, ACTION)
        assert not store._grants


@pytest.mark.parametrize("action", ["unsubscribe", "create_folder", "create_sieve_filter"])
def test_single_action_claim_keeps_original_preview_lifetime(action):
    clock = [0.0]
    store = TokenStore(ttl=10, clock=lambda: clock[0])
    token, expiry = store.issue(action, {}, {}, ROWS[:1] if action == "unsubscribe" else [])
    clock[0] = 9
    granted = store.claim(token, action)
    assert granted.expires == 10 and granted.expires_at == expiry
    assert not granted.resume_started
    with pytest.raises(MailValidationError, match="already used"):
        store.claim(token, action)


@pytest.mark.parametrize("change", ["completed", "outsider", "identity", "duplicate"])
def test_continuation_cannot_publish_outside_its_current_remainder(change):
    store = TokenStore()
    token, _ = store.issue(ACTION, PARAMS, {}, ROWS)
    claimed = store.claim(token, ACTION)
    continuation = store.continue_grant(claimed, claimed.messages[1:])
    remainder = store.claim(continuation, ACTION)
    attempted = copy.deepcopy(remainder.messages)
    if change == "completed":
        attempted.append(copy.deepcopy(ROWS[0]))
    elif change == "outsider":
        attempted.append(dict(ROWS[0], uid="99", message_id="<outsider@example.test>"))
    elif change == "identity":
        attempted[0]["message_id"] = "<changed@example.test>"
    else:
        attempted.append(copy.deepcopy(attempted[0]))
    with pytest.raises(MailValidationError, match="approved message set|duplicate"):
        store.continue_grant(remainder, attempted)
    assert not store._grants


@pytest.mark.parametrize("held_token", ["latest", "original-lost-response"])
def test_mid_batch_preview_expiry_resumes_exact_remainder_once(slow_cleanup, held_token):
    case = slow_cleanup(count=50, timeout=24, mutation_seconds=2, batch_size=200)
    token_clock = [0.0]
    case.service.tokens.clock = lambda: token_clock[0]
    case.service.tokens.ttl = 10
    preview = case.service.preview_action(
        ACTION,
        query={"folder": "INBOX", "from_": "notifications@example.invalid"},
        params=PARAMS,
    )
    original = case.service.tokens.inspect(preview["token"], ACTION)
    approved = {identity(row): row for row in original.messages}
    token_clock[0] = 9

    def expire_during_mutation(session, folder, operation, command):
        if operation == "MOVE":
            token_clock[0] = max(token_clock[0], original.expires + 1)

    case.peer.arrival_hook = expire_during_mutation
    first = case.service.execute(ACTION, preview["token"], limit=5)
    assert first["done"] == 5 and first["remaining"] == 45
    assert token_clock[0] > original.expires and first["continuation_token"]
    continuation = case.service.tokens.inspect(first["continuation_token"], ACTION)
    assert continuation.expires > original.expires
    assert first["expires_at"] == continuation.expires_at
    assert continuation.action_hash == original.action_hash
    deadline = continuation.expires
    outsider = case.peer.add(
        Msg(hdr("<unapproved-arrival@example.invalid>", from_="notifications@example.invalid"))
    )
    tokens, results = [preview["token"], first["continuation_token"]], [first]
    token = first["continuation_token"] if held_token == "latest" else preview["token"]
    before = list(mutations(case.peer))
    for action, kwargs in [
        ("move_to_trash", {}),
        (ACTION, {"params": {"folder": "Archive"}}),
        (ACTION, {"ids": [row["message_id"] for row in original.messages]}),
        (ACTION, {"ids": [outsider.message_id()]}),
        (ACTION, {"limit": 51}),
    ]:
        with pytest.raises(MailValidationError):
            case.service.execute(action, token, **kwargs)
    assert mutations(case.peer) == before
    while token:
        pending = case.service.tokens.inspect(token, ACTION)
        done = {journal_identity(row) for result in results for row in result["messages"]}
        assert {identity(row) for row in pending.messages} == set(approved) - done
        assert all(row == approved[identity(row)] for row in pending.messages)
        assert pending.expires == deadline and pending.action_hash == original.action_hash
        token_clock[0] += 10
        result = case.service.execute(ACTION, token, limit=5)
        results.append(result)
        assert result["done"] == 5 and result["uncertain"] == 0
        returned = result["continuation_token"]
        if returned:
            tokens.append(returned)
        token = (
            preview["token"] if returned and held_token == "original-lost-response" else returned
        )
    logged = [row for record in records(case) for row in record["messages"]]
    assert len(logged) == len({journal_identity(row) for row in logged}) == len(approved)
    assert {journal_identity(row) for row in logged} == set(approved)
    assert all(row["status"] == "done" for row in logged)
    assert len({result["run_id"] for result in results}) == 1
    assert results[-1]["run_done"] == len(approved) and results[-1]["remaining"] == 0
    assert all(item.folder == PARAMS["folder"] for item in case.items)
    assert outsider.folder == "INBOX"
    before = list(mutations(case.peer))
    for used in tokens:
        with pytest.raises(MailValidationError, match="already used"):
            case.service.execute(ACTION, used)
    assert mutations(case.peer) == before


def test_continuation_cannot_be_used_by_another_account(slow_cleanup, tmp_path):
    case = slow_cleanup(count=10, timeout=24, mutation_seconds=0, batch_size=200)
    preview = case.service.preview_action(ACTION, query={"folder": "INBOX"}, params=PARAMS)
    first = case.service.execute(ACTION, preview["token"], limit=5)
    continuation = first["continuation_token"]
    assert continuation
    settings = replace(
        case.client.settings,
        bridge_user="other-account@example.test",
        action_log_path=str(tmp_path / "other-account-actions.jsonl"),
    )
    other = MailClient(
        settings,
        lambda _: Credentials("other-account@example.test", "offline-only-password"),
        case.peer.factory(),
    )
    before = list(case.peer.commands)
    with pytest.raises(MailValidationError, match="already used"):
        other.cleanup.execute(ACTION, continuation)
    assert case.peer.commands == before
    assert case.service.execute(ACTION, continuation)["done"] == 5


def test_rule_batch_continues_after_preview_expiry_without_renewal(mailbox, monkeypatch):
    case = mailbox(count=30)
    slow_moves(case, monkeypatch, timeout=24)
    token_clock = [0.0]
    case.cleanup.tokens.clock = lambda: token_clock[0]
    case.cleanup.tokens.ttl = 10
    preview = case.rules.preview_rules()
    original = case.cleanup.tokens.inspect(preview["token"], "apply_rules")
    original_ids = {row["message_id"] for row in original.messages}
    uid = case.fake.uid

    def expire_during_move(command, *args):
        result = uid(command, *args)
        if command == "MOVE":
            token_clock[0] = max(token_clock[0], original.expires + 1)
        return result

    monkeypatch.setattr(case.fake, "uid", expire_during_move)
    token_clock[0] = 9
    token, results, deadline = preview["token"], [], None
    while token:
        result = case.rules.apply_rules(token)
        results.append(result)
        assert result["done"] > 0 and result["uncertain"] == 0
        token = result["continuation_token"]
        if token:
            pending = case.cleanup.tokens.inspect(token, "apply_rules")
            if deadline is None:
                deadline = pending.expires
                assert token_clock[0] > original.expires and deadline > original.expires
            assert pending.expires == deadline
            assert pending.expires_at == results[0]["expires_at"]
            assert pending.action_hash == original.action_hash
            done = {row["message_id"] for item in results for row in item["messages"]}
            assert {row["message_id"] for row in pending.messages} == original_ids - done
            token_clock[0] += 10
    assert len(results) > 1 and len({result["run_id"] for result in results}) == 1
    assert results[-1]["run_done"] == 30 and results[-1]["remaining"] == 0
    assert len(case.fake.folders["Folders/Review"]) == 30
    assert not case.fake.folders["INBOX"]
