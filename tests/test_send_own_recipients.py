"""Synthetic Bridge-shaped own-recipient drafts retain strict approval boundaries."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from test_send import DRAFT_ID, RECIPIENT, SENDER, Host, records, redeem
from test_send import local_certificate as local_certificate
from test_send_token_approval import own_rig as own_rig
from test_send_token_approval import rig as rig

from proton_mail_mcp import send as send_module
from proton_mail_mcp.mail import MailError, MailValidationError

FAILURES = (MailError, MailValidationError)
ACCOUNT = "me@example.com"
LISTED = "alias@example.com"
STRANGER = "stranger@example.com"
BRIDGE_SHAPES = [
    pytest.param(f"<{RECIPIENT}>", id="shape-a-angle-only"),
    pytest.param(f'"Plain Owner" <{RECIPIENT}>', id="shape-b-quoted-display"),
    pytest.param(f'"{RECIPIENT}" <{RECIPIENT}>', id="shape-c-identical-address-display"),
]


def bridge_draft(to, *, extra_headers=(), sender=SENDER):
    """Keep physical header spelling and order; never normalize through EmailMessage."""
    lines = [
        "X-Pm-Origin: synthetic",
        "X-Pm-Content-Encryption: plain",
        f"To: {to}",
        *extra_headers,
        f'From: "Plain Sender" <{sender}>',
        "Subject: Synthetic own-recipient regression",
        f"Message-Id: {DRAFT_ID}",
        "Date: Sun, 18 Oct 2026 12:00:00 +0000",
        "Mime-Version: 1.0",
        "",
        "Synthetic message body.",
        "",
    ]
    return "\r\n".join(lines).encode("ascii")


@pytest.mark.parametrize("to", BRIDGE_SHAPES)
def test_bridge_own_shapes_receive_token(own_rig, to):
    rig = own_rig
    rig.client.raw = bridge_draft(to)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "token"
    assert preview["to"] == [RECIPIENT]
    assert preview["cc"] == preview["bcc"] == []
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "to",
    [
        RECIPIENT,
        f"Plain Owner <{RECIPIENT}>",
    ],
)
def test_safe_own_display_forms_receive_token(own_rig, to):
    rig = own_rig
    rig.client.raw = bridge_draft(to)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "token"
    assert preview["to"] == [RECIPIENT]
    assert preview["cc"] == preview["bcc"] == []
    assert rig.smtp.calls == []


def test_bridge_quoted_display_sends_once_without_confirmation(own_rig, monkeypatch):
    rig = own_rig
    raw = bridge_draft(f'"Plain Owner" <{RECIPIENT}>')
    rig.client.raw = raw
    confirm = AsyncMock(side_effect=AssertionError("Unexpected confirmation request"))
    monkeypatch.setattr(send_module, "require_confirmation", confirm)
    host = Host(support=False)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "token"
    result = redeem(rig, preview["token"], host)
    assert result["sent"] is True and result["token_consumed"] is True
    assert rig.smtp.sent == [(SENDER, (RECIPIENT,), raw)]
    assert sum(call[0] == "data" for call in rig.smtp.calls) == 1
    assert host.prompts == []
    confirm.assert_not_awaited()
    with pytest.raises(FAILURES):
        redeem(rig, preview["token"], host)
    assert len(rig.smtp.sent) == 1


@pytest.mark.parametrize("field", ["Cc", "Bcc"])
def test_bridge_draft_adding_recipient_after_preview_refuses(own_rig, field):
    rig = own_rig
    to = f'"Plain Owner" <{RECIPIENT}>'
    rig.client.raw = bridge_draft(to)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "token"
    rig.client.raw = bridge_draft(to, extra_headers=(f"{field}: {RECIPIENT}",))
    host = Host(support=False)
    with pytest.raises(FAILURES):
        redeem(rig, preview["token"], host)
    assert rig.smtp.calls == [] and host.prompts == []
    assert records(rig)[-1]["result"] == "draft_changed"


@pytest.mark.parametrize(
    "to,extra_headers",
    [
        ("Recipient@example.test", ()),
        ("recipient@EXAMPLE.test", ()),
        (f"{RECIPIENT}, {RECIPIENT}", ()),
        (RECIPIENT, (f"Cc: {RECIPIENT}",)),
        (RECIPIENT, (f"Bcc: {RECIPIENT}",)),
    ],
)
def test_valid_drafts_to_own_addresses_alone_receive_token(own_rig, to, extra_headers):
    rig = own_rig
    rig.client.raw = bridge_draft(to, extra_headers=extra_headers)
    assert rig.service.preview_send(DRAFT_ID)["approval"] == "token"
    assert rig.smtp.calls == []


def login_as(rig, account, allowed):
    rig.settings = replace(
        rig.settings, bridge_user=account, send_from_allowlist=allowed, send_self_no_form=True
    )
    rig.service.settings = rig.client.settings = rig.settings
    return rig


@pytest.mark.parametrize(
    "to,approval",
    [
        (ACCOUNT, "token"),
        (LISTED, "token"),
        (f"{ACCOUNT}, {LISTED}", "token"),
        (STRANGER, "host_confirmation"),
        (f"{ACCOUNT}, {STRANGER}", "host_confirmation"),
    ],
)
def test_own_addresses_are_the_login_and_the_send_from_list(rig, to, approval):
    rig = login_as(rig, ACCOUNT, (ACCOUNT, LISTED))
    rig.client.raw = bridge_draft(to, sender=ACCOUNT)
    assert rig.service.preview_send(DRAFT_ID)["approval"] == approval


def test_the_login_counts_when_only_an_alias_is_on_the_send_from_list(rig):
    rig = login_as(rig, ACCOUNT, (LISTED,))
    rig.client.raw = bridge_draft(ACCOUNT, sender=LISTED)
    assert rig.service.preview_send(DRAFT_ID)["approval"] == "token"


@pytest.mark.parametrize("account", ["me", f"{ACCOUNT}, {STRANGER}"], ids=["bare-name", "two"])
def test_a_login_that_is_no_single_address_adds_no_own_address(rig, account):
    rig = login_as(rig, account, (LISTED,))
    rig.client.raw = bridge_draft(ACCOUNT, sender=LISTED)
    assert rig.service.preview_send(DRAFT_ID)["approval"] == "host_confirmation"
    rig.client.raw = bridge_draft(LISTED, sender=LISTED)
    assert rig.service.preview_send(DRAFT_ID)["approval"] == "token"


@pytest.mark.parametrize(
    "to,extra_headers",
    [
        ("recipient+tag@example.test", ()),
        ("recipient@xn--exmple-cua.test", ()),
        ("other@example.test", ()),
        (f"{RECIPIENT}, other@example.test", ()),
        (f'"Plain Owner" <{RECIPIENT}>, other@example.test', ()),
        (RECIPIENT, ("Cc: other@example.test",)),
        (RECIPIENT, ("Bcc: other@example.test",)),
    ],
)
def test_valid_drafts_with_anyone_else_require_host(own_rig, to, extra_headers):
    rig = own_rig
    rig.client.raw = bridge_draft(to, extra_headers=extra_headers)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "host_confirmation"
    with pytest.raises(FAILURES):
        redeem(rig, preview["token"], Host(support=False))
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "to,extra_headers",
    [
        (f'"{RECIPIENT}" <other@example.test>', ()),
        (f'"other@example.test" <{RECIPIENT}>', ()),
        (f"grp: {RECIPIENT};", ()),
        (f"{RECIPIENT} (other@example.test)", ()),
        (f'"Na(me" <{RECIPIENT}>', ()),
        (f'"Na)me" <{RECIPIENT}>', ()),
        (f"<{RECIPIENT}> <other@example.test>", ()),
        (f"{RECIPIENT},", ()),
        ("", ()),
        (f"{RECIPIENT}\r\n continued", ()),
        (f"{RECIPIENT}\rhidden", ()),
        (f"{RECIPIENT}\nhidden", ()),
        (f"{RECIPIENT}\x00", ()),
        (RECIPIENT, ("Cc:",)),
        (RECIPIENT, ("Bcc:",)),
        (RECIPIENT, (f"To: {RECIPIENT}",)),
        (RECIPIENT, (f"Cc: {RECIPIENT}", f"Cc: {RECIPIENT}")),
        (RECIPIENT, (f"Bcc: {RECIPIENT}", f"Bcc: {RECIPIENT}")),
        (RECIPIENT, (f"Resent-To: {RECIPIENT}",)),
        (f"=?utf-8?q?other=40example.test?= <{RECIPIENT}>", ()),
        (f"=?utf-8?q?=3CPlain?= <{RECIPIENT}>", ()),
        (f"=?utf-8?b?!!invalid!!?= <{RECIPIENT}>", ()),
    ],
)
def test_malformed_raw_drafts_keep_validation_refusal(own_rig, to, extra_headers):
    rig = own_rig
    rig.client.raw = bridge_draft(to, extra_headers=extra_headers)
    with pytest.raises(FAILURES):
        rig.service.preview_send(DRAFT_ID)
    assert rig.smtp.calls == []


def test_nonascii_outside_encoded_word_keeps_validation_refusal(own_rig):
    rig = own_rig
    rig.client.raw = bridge_draft(f'"Plain Owner" <{RECIPIENT}>').replace(
        b"Plain Owner", "René Owner".encode("utf-8")
    )
    with pytest.raises(FAILURES):
        rig.service.preview_send(DRAFT_ID)
    assert rig.smtp.calls == []


@pytest.mark.parametrize("to", BRIDGE_SHAPES)
def test_bridge_own_shapes_with_flag_off_require_host(own_rig, to):
    rig = own_rig
    rig.service.settings = replace(rig.settings, send_self_no_form=False)
    rig.client.raw = bridge_draft(to)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "host_confirmation"
    host = Host()
    assert redeem(rig, preview["token"], host)["sent"] is True
    assert len(host.prompts) == 1 and len(rig.smtp.sent) == 1


@pytest.mark.parametrize("flag", [False, True])
def test_encoded_recipient_keeps_validation_refusal(own_rig, flag):
    rig = own_rig
    rig.service.settings = replace(rig.settings, send_self_no_form=flag)
    rig.client.raw = bridge_draft(f"=?utf-8?q?Plain_Owner?= <{RECIPIENT}>")
    with pytest.raises(FAILURES):
        rig.service.preview_send(DRAFT_ID)
    assert rig.smtp.calls == []
