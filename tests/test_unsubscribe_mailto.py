"""Mailto unsubscribe approvals prepare exact unsent drafts with no network send."""

import json
from dataclasses import replace
from email import policy
from email.parser import BytesParser

import pytest
from test_unsubscribe import IDENTIFIER, assert_read_only, forbid_requests, newsletter, setup
from test_unsubscribe import settings as settings

from proton_mail_mcp.action_log import ActionLog, audit_record, replay_records
from proton_mail_mcp.confirmation import approval_text, describe_approval
from proton_mail_mcp.mail import MailError, MailValidationError
from proton_mail_mcp.server import _audit_result

RECIPIENTS = ["leave@example.com", "leave-second@example.com"]
SUBJECT = "Please unsubscribe + café"
BODY = "Remove this subscriber.\r\nReference: synthetic-body-secret"
MAILTO = (
    "mailto:leave@example.com,leave-second@example.com"
    "?subject=Please%20unsubscribe%20%2B%20caf%C3%A9"
    "&body=Remove%20this%20subscriber.%0D%0AReference%3A%20synthetic-body-secret"
)


@pytest.fixture
def mailto_settings(settings):
    return replace(
        settings,
        enable_send=True,
        send_from_allowlist=("reader@example.com",),
    )


@pytest.fixture(autouse=True)
def never_send(monkeypatch):
    forbid_requests(monkeypatch)


def mailto_mailbox(settings):
    return setup(settings, items={"1": newsletter(url=MAILTO, one_click=False)})


def test_mailto_preview_and_confirmation_show_exact_draft_content(mailto_settings):
    mailbox, fake = mailto_mailbox(mailto_settings)

    preview = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])

    assert preview["token"] and not preview["blocked"]
    assert preview["targets"] == [
        {
            "method": "MAILTO",
            "host": "example.com",
            "to": RECIPIENTS,
            "subject": SUBJECT,
            "body": BODY,
            "requires_send_approval": True,
        }
    ]
    summary = describe_approval(mailbox.cleanup, "unsubscribe", preview["token"])
    assert summary["drafts"] == preview["targets"]
    assert summary["targets"] == [{"method": "MAILTO", "host": "example.com", "count": 1}]
    text = approval_text(summary)
    assert "unsent" in text and "separate send approval" in text
    for value in (RECIPIENTS, SUBJECT, BODY):
        assert json.dumps(value, ensure_ascii=True) in text
    assert_read_only(fake)


def test_approved_mailto_only_saves_exact_unsent_draft_with_pending_audit(
    mailto_settings, monkeypatch
):
    mailbox, fake = mailto_mailbox(mailto_settings)
    preview = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])
    append = fake.append

    def append_after_intent(*args):
        records = replay_records(
            [json.loads(line) for line in mailbox.cleanup.backend.log.path.read_text().splitlines()]
        )
        assert records[-1]["action"] == "unsubscribe"
        assert records[-1]["messages"][0]["status"] == "uncertain"
        assert records[-1]["messages"][0]["method"] == "MAILTO"
        return append(*args)

    monkeypatch.setattr(fake, "append", append_after_intent)

    result = mailbox.unsubscribe(preview["token"])

    assert result["count"] == 0 and result["partial"] is False
    assert result["status"] == "pending_send"
    assert result["pending_send"] == 1 and result["token_consumed"] is True
    assert result["messages"][0]["status"] == "pending_send"
    assert "unsent" in result["reason"]
    assert len(fake.appended) == 1
    folder, flags, _, raw = fake.appended[0]
    assert folder == '"Drafts"' and flags == r"(\Draft)"
    saved = BytesParser(policy=policy.SMTP).parsebytes(raw)
    assert [address.addr_spec for address in saved["To"].addresses] == RECIPIENTS
    assert saved["Subject"] == SUBJECT
    assert saved.get_content() == BODY + "\r\n"
    assert "Cc" not in saved and "Bcc" not in saved
    assert result["drafts"] == [
        {
            "draft_id": str(saved["Message-ID"]),
            "method": "MAILTO",
            "to": RECIPIENTS,
            "subject": SUBJECT,
            "body": BODY,
        }
    ]
    action = mailbox.list_actions(token=preview["token"])["actions"][0]
    assert action["action"] == "unsubscribe" and action["partial"] is False
    assert action["status"] == "pending_send"
    assert action["done"] == action["uncertain"] == action["not_attempted"] == 0
    assert action["pending_send"] == 1
    assert action["draft_ids"] == [str(saved["Message-ID"])]
    audit = _audit_result("unsubscribe", result)
    assert audit["outcome"] == "pending_send"
    assert audit["counts"] == {
        "approved": 1,
        "done": 0,
        "uncertain": 0,
        "remaining": 0,
        "pending_send": 1,
    }
    journal = mailbox.cleanup.backend.log.path.read_text()
    records = [json.loads(line) for line in journal.splitlines()]
    assert records[-1]["action"] == "execute_result"
    assert records[-1]["status"] == "pending_send" and records[-1]["partial"] is False
    assert replay_records(records)[-1]["status"] == "pending_send"
    mailbox.cleanup.backend.log.audit(
        audit_record(
            "unsubscribe",
            audit.pop("outcome"),
            audit_id="test",
            token=preview["token"],
            **audit,
        )
    )
    assert mailbox.list_actions(token=preview["token"])["audit"][0]["outcome"] == "pending_send"
    for private in (*RECIPIENTS, SUBJECT, "synthetic-body-secret", MAILTO, preview["token"]):
        assert private not in journal

    before = list(fake.calls)
    with pytest.raises(MailValidationError, match="already used"):
        mailbox.unsubscribe(preview["token"])
    assert fake.calls == before and len(fake.appended) == 1


def test_lost_unsubscribe_response_recovers_only_prepared_draft_ids(mailto_settings):
    mailbox, fake = setup(
        mailto_settings,
        items={
            "1": newsletter(url=MAILTO, one_click=False),
            "2": newsletter("<second@example.test>", url=MAILTO, one_click=False),
        },
    )
    token = mailbox.preview_action("unsubscribe", query={})["token"]
    # Simulate a lost response: recovery has only the original preview token.
    mailbox.unsubscribe(token)
    before = list(fake.calls)

    lookup = ActionLog(mailto_settings.action_log_path).list_actions(token=token)

    assert fake.calls == before
    assert [row["action"] for row in lookup["actions"]] == ["unsubscribe", "preview_action"]
    action = lookup["actions"][0]
    saved_ids = [
        str(BytesParser(policy=policy.SMTP).parsebytes(raw)["Message-ID"])
        for _, _, _, raw in fake.appended
    ]
    assert len(saved_ids) == 2
    assert action["draft_ids"] == saved_ids
    assert action["status"] == "pending_send" and action["partial"] is False
    assert action["pending_send"] == 2
    assert not {"to", "subject", "body", "token", "token_digest", "messages"} & action.keys()
    journal = mailbox.cleanup.backend.log.path.read_text()
    for private in (*RECIPIENTS, SUBJECT, "synthetic-body-secret", MAILTO, token):
        assert private not in json.dumps(lookup)
        assert private not in journal


def test_completed_http_and_prepared_mailto_have_pending_send_status(mailto_settings, monkeypatch):
    mailbox, fake = setup(
        mailto_settings,
        items={
            "1": newsletter(url=MAILTO, one_click=False),
            "2": newsletter("<second@example.test>"),
        },
    )
    requests = []

    def perform(target, **kwargs):
        requests.append(target)
        return 204

    monkeypatch.setattr("proton_mail_mcp.unsubscribe_http.perform", perform)
    token = mailbox.preview_action("unsubscribe", query={})["token"]

    result = mailbox.unsubscribe(token)

    assert result["status"] == "pending_send" and result["partial"] is False
    assert result["count"] == result["pending_send"] == 1
    assert len(fake.appended) == len(requests) == 1
    action = mailbox.list_actions(token=token)["actions"][0]
    assert action["status"] == "pending_send" and action["partial"] is False
    assert action["done"] == action["pending_send"] == 1
    audit = _audit_result("unsubscribe", result)
    assert audit["outcome"] == "pending_send"
    assert audit["counts"] == {
        "approved": 2,
        "done": 1,
        "uncertain": 0,
        "remaining": 0,
        "pending_send": 1,
    }


def test_prepared_and_uncertain_mailto_drafts_remain_partial(mailto_settings, monkeypatch):
    mailbox, fake = setup(
        mailto_settings,
        items={
            str(index): newsletter(f"<{index}@example.test>", url=MAILTO, one_click=False)
            for index in range(1, 4)
        },
    )
    token = mailbox.preview_action("unsubscribe", query={})["token"]
    append = fake.append

    def second_append_uncertain(*args):
        response = append(*args)
        if len(fake.appended) == 2:
            raise OSError("synthetic-private-append-diagnostic")
        return response

    monkeypatch.setattr(fake, "append", second_append_uncertain)

    result = mailbox.unsubscribe(token)

    assert result["status"] == "partial" and result["partial"] is True
    assert [row["status"] for row in result["messages"]] == [
        "pending_send",
        "uncertain",
        "not_attempted",
    ]
    assert result["pending_send"] == 1 and len(fake.appended) == 2
    log = mailbox.cleanup.backend.log
    audit = _audit_result("unsubscribe", result)
    log.audit(
        audit_record("unsubscribe", audit.pop("outcome"), audit_id="test", token=token, **audit)
    )
    lookup = mailbox.list_actions(token=token)
    action = lookup["actions"][0]
    assert action["status"] == "partial" and action["partial"] is True
    assert action["pending_send"] == action["uncertain"] == action["not_attempted"] == 1
    assert action["draft_ids"] == [result["drafts"][0]["draft_id"]]
    assert lookup["audit"][0]["outcome"] == "partial"
    assert lookup["audit"][0]["counts"] == {
        "approved": 3,
        "done": 0,
        "uncertain": 1,
        "remaining": 1,
        "pending_send": 1,
    }
    for private in (*RECIPIENTS, SUBJECT, "synthetic-body-secret", MAILTO, token):
        assert private not in json.dumps(lookup)
        assert private not in log.path.read_text()


@pytest.mark.parametrize(
    "old,new",
    [
        (b"leave@example.com", b"other@example.com"),
        (b"Please%20unsubscribe", b"Please%20resubscribe"),
        (b"synthetic-body-secret", b"different-body-secret"),
    ],
    ids=["recipient", "subject", "body"],
)
def test_changed_mailto_target_refuses_before_creating_draft(mailto_settings, old, new):
    mailbox, fake = mailto_mailbox(mailto_settings)
    preview = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])
    item = fake.folders["INBOX"]["1"]
    item.headers = item.headers.replace(old, new)

    with pytest.raises(MailValidationError, match="changed"):
        mailbox.unsubscribe(preview["token"])

    assert mailbox.cleanup.tokens.inspect(preview["token"], "unsubscribe")
    assert_read_only(fake)


@pytest.mark.parametrize("token", [None, "", "forged-token", "é" * 43])
def test_mailto_unsubscribe_requires_valid_token_before_mail_access(mailto_settings, token):
    mailbox, fake = mailto_mailbox(mailto_settings)

    with pytest.raises(MailValidationError):
        mailbox.unsubscribe(token)

    assert fake.calls == [] and fake.appended == []


@pytest.mark.parametrize(
    "change,reason",
    [({"enable_send": False}, "Guarded send is disabled"), ({"send_max_recipients": 1}, "cap")],
)
def test_mailto_unsubscribe_respects_send_enablement_and_recipient_cap(
    mailto_settings, change, reason
):
    mailbox, fake = mailto_mailbox(replace(mailto_settings, **change))

    preview = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])

    assert preview["token"] is None and reason in preview["blocked"][0]["reason"]
    assert_read_only(fake)


def test_failed_journal_prevents_mailto_draft_append(mailto_settings):
    mailbox, fake = mailto_mailbox(mailto_settings)
    preview = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])
    mailbox.cleanup.backend.log.path.write_text("invalid complete record\n")

    with pytest.raises(MailError):
        mailbox.unsubscribe(preview["token"])

    assert_read_only(fake)


def test_ambiguous_mailto_append_is_uncertain_and_never_retried(mailto_settings, monkeypatch):
    mailbox, fake = mailto_mailbox(mailto_settings)
    preview = mailbox.preview_action("unsubscribe", ids=[IDENTIFIER])
    append = fake.append

    def uncertain_append(*args):
        append(*args)
        raise OSError("synthetic-private-append-diagnostic")

    monkeypatch.setattr(fake, "append", uncertain_append)

    result = mailbox.unsubscribe(preview["token"])

    assert result["count"] == 0 and result["partial"] is True
    assert result["token_consumed"] is True
    assert result["messages"][0]["status"] == "uncertain"
    assert not result.get("drafts") and not result.get("pending_send")
    assert len(fake.appended) == 1
    action = mailbox.list_actions(token=preview["token"])["actions"][0]
    assert action["done"] == 0 and action["uncertain"] == 1
    assert "synthetic-private-append-diagnostic" not in json.dumps(result)
    assert "synthetic-private-append-diagnostic" not in mailbox.cleanup.backend.log.path.read_text()
    before = list(fake.calls)
    with pytest.raises(MailValidationError, match="already used"):
        mailbox.unsubscribe(preview["token"])
    assert fake.calls == before and len(fake.appended) == 1
