"""Expired capability digests are removed on every kind of store access."""

import pytest

from proton_mail_mcp.guarded import TokenStore
from proton_mail_mcp.mail import MailValidationError


def pending_tokens():
    clock = [0.0]
    store = TokenStore(ttl=10, clock=lambda: clock[0])
    messages = [{"message_id": "<cleanup@example.invalid>", "folder": "INBOX"}]
    stale = [store.issue("move_to_trash", {}, {"ids": []}, messages)[0] for _ in range(2)]
    clock[0] = 5
    fresh = store.issue("move_to_trash", {}, {"ids": []}, messages)[0]
    clock[0] = 10
    return store, stale, fresh, messages


@pytest.mark.parametrize("operation", ["inspect", "claim"])
def test_expired_lookup_prunes_all_stale_digests_and_preserves_valid_grant(operation):
    store, stale, fresh, messages = pending_tokens()
    with pytest.raises(MailValidationError, match="expired; request a new preview"):
        getattr(store, operation)(stale[0], "move_to_trash")
    assert len(store._grants) == 1
    assert store.inspect(fresh, "move_to_trash").messages == messages
    for expired in stale:
        with pytest.raises(MailValidationError, match="Invalid or already used"):
            store.inspect(expired, "move_to_trash")


@pytest.mark.parametrize("operation", ["inspect", "claim"])
def test_valid_lookup_also_cleans_up_other_expired_previews(operation):
    store, _, fresh, messages = pending_tokens()
    assert getattr(store, operation)(fresh, "move_to_trash").messages == messages
    assert len(store._grants) == (1 if operation == "inspect" else 0)
    if operation == "claim":
        with pytest.raises(MailValidationError, match="Invalid or already used"):
            store.claim(fresh, "move_to_trash")


@pytest.mark.parametrize("operation", ["inspect", "claim"])
@pytest.mark.parametrize(
    "invalid",
    [None, "non-ascii-\u00e9", "x" * 257, "unknown-offline-token"],
    ids=["not-text", "non-ascii", "too-long", "unknown"],
)
def test_invalid_attempts_prune_expired_grants_without_consuming_valid_ones(operation, invalid):
    store, _, fresh, _ = pending_tokens()
    with pytest.raises(MailValidationError, match="Invalid or already used"):
        getattr(store, operation)(invalid, "move_to_trash")
    assert len(store._grants) == 1
    store.inspect(fresh, "move_to_trash")


def test_issue_still_prunes_expired_digests():
    store, _, fresh, messages = pending_tokens()
    issued, _ = store.issue("move_to_trash", {}, {"ids": []}, messages)
    assert len(store._grants) == 2
    store.inspect(fresh, "move_to_trash")
    store.inspect(issued, "move_to_trash")


@pytest.mark.parametrize("operation", ["inspect", "claim"])
def test_expiry_during_lookup_still_refuses_and_removes_digest(operation):
    store = TokenStore(ttl=10, clock=lambda: 0.0)
    messages = [{"message_id": "<cleanup@example.invalid>", "folder": "INBOX"}]
    token, _ = store.issue("move_to_trash", {}, {"ids": []}, messages)
    ticks = iter([9.9, 10.0])
    store.clock = lambda: next(ticks)
    with pytest.raises(MailValidationError, match="expired"):
        getattr(store, operation)(token, "move_to_trash")
    assert not store._grants


def test_mismatched_inspection_preserves_approval_and_mismatched_claim_consumes_it():
    store, _, fresh, _ = pending_tokens()
    with pytest.raises(MailValidationError, match="requested action"):
        store.inspect(fresh, "archive")
    assert len(store._grants) == 1
    store.inspect(fresh, "move_to_trash")
    with pytest.raises(MailValidationError, match="requested message set"):
        store.claim(fresh, "move_to_trash", ids=["<different@example.invalid>"])
    assert not store._grants
