"""Limited approvals bind a deterministic set; presentation never changes the journal."""

import json

import pytest
from test_cleanup_chunks import chunk_mailbox as chunk_mailbox
from test_cleanup_chunks import moved, mutations, records
from test_folder_rules import mailbox as mailbox
from test_folder_rules import rule

from proton_mail_mcp.cleanup_response import cleanup_response
from proton_mail_mcp.guarded import MAX_BATCH, _fingerprint
from proton_mail_mcp.mail import MailValidationError


@pytest.mark.parametrize("selector", ["query", "ids"])
def test_preview_limit_binds_exact_numeric_uid_prefix(chunk_mailbox, selector):
    case = chunk_mailbox(count=220, move_seconds=0)
    arguments = {"query": {}} if selector == "query" else {"ids": case.identifiers[::-1]}
    preview = case.mailbox.preview_action("move_to_trash", **arguments, limit=30)
    grant = case.service.tokens.inspect(preview["token"], "move_to_trash")
    assert preview["count"] == len(grant.messages) == 30
    assert [row["uid"] for row in grant.messages] == [str(i) for i in range(1, 31)]
    assert grant.fingerprint == _fingerprint(grant.messages)
    # One batched chunk confirms all 30 (the per-message allowance needed two calls).
    assert preview["estimated_calls"] == 1
    with pytest.raises(MailValidationError, match="widen"):
        case.mailbox.move_to_trash(preview["token"], limit=31)
    with pytest.raises(MailValidationError, match="message set"):
        case.service.execute("move_to_trash", preview["token"], ids=case.identifiers)
    assert not mutations(case) and not case.claims
    result = case.mailbox.move_to_trash(preview["token"])
    assert result["done"] == result["run_done"] == 30
    assert result["remaining"] == 0
    assert set(case.fake.folders["INBOX"]) == {str(i) for i in range(31, 221)}
    assert len(moved(case)) == 30


@pytest.mark.parametrize("limit", [False, True, 0, -1, 1.5, "30", 201, 501, [], {}])
def test_invalid_limits_refuse_before_opening_mail(chunk_mailbox, limit):
    case = chunk_mailbox(count=3)
    preview = case.mailbox.preview_action("archive", query={})
    calls = list(case.fake.calls)
    invocations = [
        # An execute limit stays 1..200; a preview limit may reach the bound.
        (lambda: case.mailbox.archive(preview["token"], limit=limit), "at most 200"),
    ]
    if limit != 201:
        invocations.append(
            (
                lambda: case.mailbox.preview_action("archive", query={}, limit=limit),
                "positive integer at most 500",
            )
        )
    for invoke, message in invocations:
        with pytest.raises(MailValidationError, match=message):
            invoke()
        assert case.fake.calls == calls
    assert not case.claims
    case.service.tokens.inspect(preview["token"], "archive")


def test_execute_limit_rotates_exact_remainder_without_widening(chunk_mailbox):
    case = chunk_mailbox(count=94, move_seconds=0)
    preview = case.mailbox.preview_action("move_to_trash", query={}, limit=94)
    original = case.service.tokens.inspect(preview["token"], "move_to_trash")
    token = preview["token"]
    done = set()
    counts = []
    resume_expiry = None
    expires_at = None
    while token:
        result = case.mailbox.move_to_trash(token, limit=30)
        if expires_at is None:
            expires_at = result["expires_at"]
        assert result["expires_at"] == expires_at
        counts.append(result["done"])
        done.update(row["message_id"] for row in result["messages"])
        assert 0 < result["done"] <= 30
        token = result["continuation_token"]
        assert bool(token) == bool(result["remaining"])
        if token:
            grant = case.service.tokens.inspect(token, "move_to_trash")
            assert {row["message_id"] for row in grant.messages} == set(case.identifiers) - done
            if resume_expiry is None:
                resume_expiry = grant.expires
                assert resume_expiry > original.expires
            assert grant.expires == resume_expiry
            assert grant.expires_at == expires_at
    assert counts == [30, 30, 30, 4]
    assert len(done) == len(moved(case)) == 94
    assert len(mutations(case)) == 4  # One batched MOVE per call.


def test_default_100_item_response_is_small_and_journal_stays_complete(chunk_mailbox):
    case = chunk_mailbox(count=100, move_seconds=0)
    preview = case.mailbox.preview_action("archive", query={})
    result = case.mailbox.archive(preview["token"])
    assert result["done"] == 100
    rich = cleanup_response(result, verbose=True)
    compact = cleanup_response(result)
    assert "messages" not in compact
    assert compact["message_ids"] == case.identifiers
    assert compact["done"] == compact["run_done"] == compact["approved_total"] == 100
    assert compact["remaining"] == compact["uncertain"] == 0
    assert compact["available"] is False
    assert rich["messages"] == result["messages"]
    assert [row for record in records(case) for row in record["messages"]] == rich["messages"]
    size = len(json.dumps(compact).encode())
    assert size < 6000
    assert len(json.dumps(rich).encode()) > 10 * size
    print(f"response_items=100 compact_bytes={size} verbose_bytes={len(json.dumps(rich).encode())}")


def test_omitted_preview_limit_keeps_existing_cap(chunk_mailbox):
    case = chunk_mailbox(count=MAX_BATCH + 1)
    preview = case.mailbox.preview_action("archive", query={})
    assert preview["token"] is None
    assert preview["count"] == MAX_BATCH + 1
    assert "hard cap" in preview["reason"]


def test_rules_execute_limit_applies_across_groups(mailbox):
    case = mailbox(count=12, rules=[rule("subject", "First"), rule(folder="Folders/Other")])
    for uid, item in case.fake.folders["INBOX"].items():
        if int(uid) <= 6:
            item.headers = item.headers.replace(b"Subject: Hello", b"Subject: First")
    preview = case.rules.preview_rules()
    token, counts, run_ids = preview["token"], [], set()
    while token:
        result = case.rules.apply_rules(token, limit=4)
        counts.append(result["done"])
        run_ids.add(result["run_id"])
        token = result["continuation_token"]
        assert bool(token) == bool(result["remaining"])
    assert counts == [4, 4, 4]
    assert len(run_ids) == 1
    assert not case.fake.folders["INBOX"]
    assert len(case.fake.folders["Folders/Review"]) == 6
    assert len(case.fake.folders["Folders/Other"]) == 6


@pytest.mark.parametrize("limit", [False, True, 0, -1, 1.5, "3", 201, 4])
def test_rules_execute_invalid_or_widened_limit_preserves_approval(mailbox, limit):
    case = mailbox(count=3)
    preview = case.rules.preview_rules()
    calls = list(case.fake.calls)
    with pytest.raises(MailValidationError, match="limit"):
        case.rules.apply_rules(preview["token"], limit=limit)
    assert case.fake.calls == calls
    assert case.cleanup.tokens.inspect(preview["token"], "apply_rules")
