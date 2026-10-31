"""Mailto handoff uses the existing exact-draft send token and SMTP safeguards."""

import json
from dataclasses import replace
from email import policy
from email.parser import BytesParser

import pytest
from test_send import (
    BODY,
    DRAFT_ID,
    RECIPIENT,
    SENDER,
    SUBJECT,
    Host,
    draft,
    local_certificate,  # noqa: F401
    records,
    redeem,
)
from test_send import rig as rig  # noqa: F401

from proton_mail_mcp.mail import MailValidationError


def target(**changes):
    return {"to": [RECIPIENT], "subject": SUBJECT, "body": BODY, **changes}


def test_mailto_preview_binds_target_without_sending(rig):
    expected = target()
    preview = rig.service.preview_unsubscribe(DRAFT_ID, expected)
    assert rig.smtp.calls == []
    assert preview["method"] == "MAILTO"
    assert {name: preview[name] for name in expected} == expected
    assert preview["approval"] == "host_confirmation"
    grant = rig.tokens.inspect(preview["token"], "send_draft")
    assert grant.params["unsubscribe"] == expected
    expected["to"].append("other@example.test")
    assert grant.params["unsubscribe"]["to"] == [RECIPIENT]


@pytest.mark.parametrize("body", ["", "two\r\nlines", "café + tea\n", "body\n\n", "  body  "])
def test_mailto_body_matches_draft_content_normalization(rig, body):
    rig.client.raw = draft(body=body)
    preview = rig.service.preview_unsubscribe(DRAFT_ID, target(body=body))
    assert preview["body"] == body
    assert preview["token"]
    assert rig.smtp.calls == []


def test_mailto_send_requires_valid_token_and_exact_host_approval(rig):
    preview = rig.service.preview_unsubscribe(DRAFT_ID, target())
    with pytest.raises(MailValidationError, match="invalid, expired or used"):
        redeem(rig, "unknown-token")
    assert rig.smtp.calls == []
    host = Host()
    redeem(rig, preview["token"], host)
    assert rig.smtp.sent == [(SENDER, (RECIPIENT,), rig.client.raw)]
    details = json.loads(host.prompts[0].split("\n", 1)[1])
    assert details["method"] == "MAILTO"
    assert {name: details[name] for name in ("to", "subject", "body")} == target()
    before = list(rig.smtp.calls)
    with pytest.raises(MailValidationError, match="invalid, expired or used"):
        redeem(rig, preview["token"], Host())
    assert rig.smtp.calls == before
    serialized = json.dumps(records(rig))
    assert BODY not in serialized and SUBJECT not in serialized and RECIPIENT not in serialized


def test_mailto_multiple_recipients_remain_exact(rig):
    recipients = [RECIPIENT, "second@example.test"]
    rig.client.raw = draft(to=", ".join(recipients))
    preview = rig.service.preview_unsubscribe(DRAFT_ID, target(to=recipients))
    redeem(rig, preview["token"], Host())
    assert rig.smtp.sent == [(SENDER, tuple(recipients), rig.client.raw)]


def test_mailto_foreign_recipient_still_needs_host_when_token_approval_enabled(rig):
    rig.service.settings = replace(rig.settings, send_self_no_form=True)
    preview = rig.service.preview_unsubscribe(DRAFT_ID, target())
    assert preview["approval"] == "host_confirmation"
    with pytest.raises(MailValidationError, match="confirmation was not accepted"):
        redeem(rig, preview["token"], Host(support=False))
    assert rig.smtp.calls == []
    assert records(rig)[-1]["result"] == "confirmation_unavailable"


@pytest.mark.parametrize(
    "change",
    [
        {"to": "other@example.test"},
        {"subject": "Changed subject"},
        {"body": "Changed body"},
        {"cc": "other@example.test"},
        {"bcc": "other@example.test"},
        {"attachment": b"unapproved attachment"},
    ],
)
def test_mailto_rejects_changed_draft_before_issuing_token(rig, change):
    rig.client.raw = draft(**change)
    with pytest.raises(MailValidationError, match="Unsubscribe draft does not match"):
        rig.service.preview_unsubscribe(DRAFT_ID, target())
    assert rig.tokens._grants == {}
    assert rig.smtp.calls == []


@pytest.mark.parametrize("header", ["Cc", "Bcc"])
def test_mailto_refuses_even_empty_extra_recipient_header(rig, header):
    message = BytesParser(policy=policy.SMTP).parsebytes(rig.client.raw)
    message[header] = ""
    rig.client.raw = message.as_bytes(policy=policy.SMTP.clone(max_line_length=0))
    with pytest.raises(MailValidationError):
        rig.service.preview_unsubscribe(DRAFT_ID, target())
    assert rig.smtp.calls == []


def test_mailto_rejects_html_draft(rig):
    message = BytesParser(policy=policy.SMTP).parsebytes(rig.client.raw)
    message.set_content(BODY, subtype="html")
    rig.client.raw = message.as_bytes(policy=policy.SMTP.clone(max_line_length=0))
    with pytest.raises(MailValidationError, match="Unsubscribe draft does not match"):
        rig.service.preview_unsubscribe(DRAFT_ID, target())
    assert rig.smtp.calls == []


@pytest.mark.parametrize("field", ["to", "subject", "body"])
@pytest.mark.parametrize("during_confirmation", [False, True])
def test_mailto_changes_after_preview_refuse_send(rig, field, during_confirmation):
    preview = rig.service.preview_unsubscribe(DRAFT_ID, target())

    def change():
        value = "other@example.test" if field == "to" else "Unapproved change"
        rig.client.raw = draft(**{field: value})

    host = Host(during=change if during_confirmation else None)
    if not during_confirmation:
        change()
    with pytest.raises(MailValidationError, match="changed"):
        redeem(rig, preview["token"], host)
    assert rig.smtp.calls == []
    with pytest.raises(MailValidationError, match="invalid, expired or used"):
        redeem(rig, preview["token"], Host())


def test_mailto_preview_refuses_when_send_is_disabled(rig):
    rig.service.settings = replace(rig.settings, enable_send=False)
    with pytest.raises(MailValidationError, match="Guarded send is disabled"):
        rig.service.preview_unsubscribe(DRAFT_ID, target())
    assert rig.client.reads == []
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "invalid",
    [None, {}, target(to=[]), target(to="recipient@example.test"), target(body=None)],
)
def test_mailto_send_target_must_be_explicit_and_valid(rig, invalid):
    with pytest.raises(MailValidationError, match="previewed mailto target"):
        rig.service.preview_unsubscribe(DRAFT_ID, invalid)
    assert rig.smtp.calls == []
