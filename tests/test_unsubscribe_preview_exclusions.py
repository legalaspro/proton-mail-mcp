"""Unsubscribe previews exclude identity drift without weakening confirmation."""

import pytest
from test_unsubscribe import IDENTIFIER, assert_read_only, forbid_requests, newsletter, setup
from test_unsubscribe import settings as settings

from proton_mail_mcp.mail import MailValidationError

CLEAN_ID = "<clean-newsletter@example.invalid>"


@pytest.mark.parametrize("change", ["message_id", "sender"])
def test_unsubscribe_preview_excludes_identity_changed_during_details(
    settings, monkeypatch, change
):
    forbid_requests(monkeypatch)
    mailbox, fake = setup(settings, items={"1": newsletter(), "2": newsletter(CLEAN_ID)})
    original = mailbox._raw_headers

    def changed_headers(connection, uid):
        raw = original(connection, uid)
        if uid == "1":
            if change == "message_id":
                return raw.replace(IDENTIFIER.encode(), b"<changed@example.invalid>")
            return raw.replace(b"sender@example.com", b"changed@example.invalid")
        return raw

    monkeypatch.setattr(mailbox, "_raw_headers", changed_headers)
    result = mailbox.preview_action("unsubscribe", query={})
    assert result["excluded_count"] == 1 and result["excluded_ids"] == [IDENTIFIER]
    assert result["excluded_reasons"] == {"unsubscribe_identity_changed": 1}
    assert result["excluded_truncated"] is False
    assert result["count"] == 1 and result["token"] and not result["blocked"]
    grant = mailbox.cleanup.tokens.inspect(result["token"], "unsubscribe")
    assert [row["message_id"] for row in grant.messages] == [CLEAN_ID]
    assert grant.selector == {"query": {}, "candidate_ids": [CLEAN_ID]}
    assert_read_only(fake)


def test_unsubscribe_identity_exclusion_does_not_hide_protected_headers(settings, monkeypatch):
    forbid_requests(monkeypatch)
    mailbox, fake = setup(settings, items={"1": newsletter(), "2": newsletter(CLEAN_ID)})
    original = mailbox._raw_headers

    def changed_headers(connection, uid):
        raw = original(connection, uid)
        if uid == "1":
            return raw.replace(IDENTIFIER.encode(), b"<changed@example.invalid>").replace(
                b"sender@example.com", b"invalid-sender"
            )
        return raw

    monkeypatch.setattr(mailbox, "_raw_headers", changed_headers)
    with pytest.raises(MailValidationError, match="identity changed"):
        mailbox.preview_action("unsubscribe", query={})
    assert not mailbox.cleanup.tokens._grants
    assert_read_only(fake)


@pytest.mark.parametrize("folder", ["Labels/Review", "All Mail"])
def test_unsubscribe_excluded_query_stays_in_source_and_cannot_admit_arrivals(
    settings, monkeypatch, folder
):
    forbid_requests(monkeypatch)
    mailbox, fake = setup(settings, items={"1": newsletter(), "2": newsletter(CLEAN_ID)})
    fake.folders[folder] = fake.folders.pop("INBOX")
    original = mailbox._raw_headers

    def changed_headers(connection, uid):
        raw = original(connection, uid)
        return raw.replace(IDENTIFIER.encode(), b"<changed@example.invalid>") if uid == "1" else raw

    with monkeypatch.context() as patch:
        patch.setattr(mailbox, "_raw_headers", changed_headers)
        result = mailbox.preview_action("unsubscribe", query={"folder": folder})
    assert result["excluded_ids"] == [IDENTIFIER] and result["token"]
    grant = mailbox.cleanup.tokens.inspect(result["token"], "unsubscribe")
    assert grant.selector == {"query": {"folder": folder}, "candidate_ids": [CLEAN_ID]}
    assert [row["folder"] for row in grant.messages] == [folder]
    fake.folders[folder]["3"] = newsletter("<arrival@example.invalid>")
    requests = []

    def perform(target, **kwargs):
        requests.append(target)
        return 204

    monkeypatch.setattr("proton_mail_mcp.unsubscribe_http.perform", perform)
    completed = mailbox.unsubscribe(result["token"])
    assert completed["count"] == 1 and not completed["partial"]
    assert len(requests) == 1
    assert [row["message_id"] for row in grant.messages] == [CLEAN_ID]
    assert_read_only(fake)


def test_changed_approved_unsubscribe_row_still_refuses_whole_batch(settings, monkeypatch):
    forbid_requests(monkeypatch)
    mailbox, fake = setup(settings, items={"1": newsletter(), "2": newsletter(CLEAN_ID)})
    token = mailbox.preview_action("unsubscribe", query={})["token"]
    original = mailbox.cleanup.tokens.inspect(token, "unsubscribe")
    preview_log = mailbox.cleanup.backend.log.path.read_bytes()
    fake.folders["INBOX"]["2"].headers = fake.folders["INBOX"]["2"].headers.replace(
        b"Subject: Hello", b"Subject: Changed"
    )
    claims = []
    claim = mailbox.cleanup.tokens.claim

    def observe_claim(*args, **kwargs):
        claims.append(args)
        return claim(*args, **kwargs)

    monkeypatch.setattr(mailbox.cleanup.tokens, "claim", observe_claim)
    with pytest.raises(MailValidationError, match="changed"):
        mailbox.unsubscribe(token)
    assert not claims and mailbox.cleanup.backend.log.path.read_bytes() == preview_log
    assert mailbox.cleanup.tokens.inspect(token, "unsubscribe").messages == original.messages
    assert_read_only(fake)
