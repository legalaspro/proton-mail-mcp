"""Stored draft address checks use only synthetic messages and local recorders."""

import base64
import json
from dataclasses import replace
from email.errors import MessageError
from email.header import Header
from email.message import EmailMessage

import pytest
from test_send import DRAFT_ID, RECIPIENT, SENDER, Host, draft, records, redeem
from test_send import local_certificate as local_certificate
from test_send import rig as rig

from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.mail import MailError, MailValidationError
from proton_mail_mcp.send_address import parse_addresses

RECIPIENT_FIELDS = ("To", "Cc", "Bcc", "Reply-To")
ADDRESS_FIELDS = ("From", *RECIPIENT_FIELDS)


def stored_header(raw, field, value):
    """Insert literal header bytes without normalizing malformed test inputs."""
    header = field.encode("ascii") + b": " + value.encode("utf-8") + b"\r\n"
    prefix = field.encode("ascii") + b": "
    lines = raw.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = header
            return b"".join(lines)
    return header + raw


def encoded_name(value):
    payload = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"=?UTF-8?B?{payload}?="


@pytest.mark.parametrize(
    "name",
    [
        "Plain Owner",
        '"Plain Owner"',
        "=?utf-8?q?T=C3=A9st_Owner?=",
        encoded_name("Tést Owner"),
    ],
    ids=["plain", "quoted", "utf8-q", "utf8-b"],
)
def test_safe_stored_from_display_names_expose_only_bare_address(rig, name):
    rig.client.raw = stored_header(draft(), "From", f"{name} <{SENDER}>")
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["token"]
    assert preview["from"] == SENDER
    assert preview["to"] == [RECIPIENT]
    assert preview["cc"] == [] and preview["bcc"] == []
    assert name not in json.dumps(preview)
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "header",
    [
        b"From: Owner <sender@example.test>\r\n",
        b"From: =?ascii?q?Plain_Owner?= <sender@example.test>\r\n",
    ],
    ids=["authorized-plain-from", "authorized-ascii-q-from"],
)
def test_authorized_from_reclassifications_return_token_without_smtp(rig, header):
    rig.client.raw = draft().replace(b"From: sender@example.test\r\n", header)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["token"]
    assert preview["from"] == SENDER
    assert rig.smtp.calls == []


@pytest.mark.parametrize("field", RECIPIENT_FIELDS)
@pytest.mark.parametrize("name", ["Plain Recipient", '"Plain Recipient"', '""', '" "'])
def test_recipient_display_names_keep_existing_ascii_behavior(rig, field, name):
    rig.client.raw = stored_header(draft(), field, f"{name} <{RECIPIENT}>")
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["token"] and preview["from"] == SENDER
    assert preview["to"] == [RECIPIENT]
    assert preview["cc"] == ([RECIPIENT] if field == "Cc" else [])
    assert preview["bcc"] == ([RECIPIENT] if field == "Bcc" else [])
    assert rig.smtp.calls == []


@pytest.mark.parametrize("field", RECIPIENT_FIELDS)
@pytest.mark.parametrize(
    "name",
    [
        "=?ascii?q?Plain_Recipient?=",
        "=?utf-8?q?T=C3=A9st_Recipient?=",
        encoded_name("Tést Recipient"),
    ],
    ids=["ascii-q", "utf8-q", "utf8-b"],
)
def test_encoded_recipient_names_keep_refusing_before_token(rig, field, name):
    rig.client.raw = stored_header(draft(), field, f"{name} <{RECIPIENT}>")
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants
    assert rig.smtp.calls == []


def test_folded_encoded_from_name_is_unfolded_for_validation_only(rig):
    name = Header("Tést Owner " * 12, "utf-8", maxlinelen=60).encode(linesep="\r\n")
    assert "\r\n " in name
    rig.client.raw = stored_header(draft(), "From", f"{name} <{SENDER}>")
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["token"] and preview["from"] == SENDER
    assert preview["to"] == [RECIPIENT]
    assert rig.smtp.calls == []


@pytest.mark.parametrize("field", (*RECIPIENT_FIELDS, "Sender"))
@pytest.mark.parametrize(
    "name", ["Plain\r\n Owner", "=?utf-8?q?T=C3=A9st?=\r\n =?utf-8?q?_Owner?="]
)
def test_non_from_address_header_folding_keeps_refusing(rig, field, name):
    address = SENDER if field == "Sender" else RECIPIENT
    rig.client.raw = stored_header(draft(), field, f"{name} <{address}>")
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants
    assert rig.smtp.calls == []


# The stdlib parser raises TypeError, AttributeError or UnboundLocalError on such values on
# some Python versions, and ValueError on others.
PARSER_JUNK = ["=.<$", "x,.<>", "a@b.test, .<$", "*@[  ", "}+x@[ "]


@pytest.mark.parametrize("field", (*ADDRESS_FIELDS, "Sender"))
@pytest.mark.parametrize("value", PARSER_JUNK)
def test_junk_the_address_parser_breaks_on_gets_the_address_refusal(rig, field, value):
    rig.client.raw = stored_header(draft(), field, value)
    with pytest.raises(MailValidationError, match="unambiguous ASCII"):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants


@pytest.mark.parametrize("value", PARSER_JUNK)
def test_a_draft_turned_to_parser_junk_after_preview_is_a_changed_draft(rig, value):
    preview = rig.service.preview_send(DRAFT_ID)
    rig.client.raw = stored_header(draft(), "To", value)
    with pytest.raises(MailValidationError, match="Draft changed"):
        redeem(rig, preview["token"], Host())
    assert records(rig)[-1]["result"] == "draft_changed"
    assert rig.smtp.calls == []


def test_a_header_the_generator_refuses_gets_the_malformed_refusal(rig, monkeypatch):
    # HeaderWriteError, which a stored header triggers only on some Python versions.
    def refuse(self, **options):
        raise MessageError("folded header contains newline")

    monkeypatch.setattr(EmailMessage, "as_bytes", refuse)
    with pytest.raises(MailValidationError, match="Malformed draft content"):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants


@pytest.mark.parametrize(
    "name",
    ["Plain Owner", '"Plain Owner"', "=?utf-8?q?T=C3=A9st_Owner?=", encoded_name("Tést Owner")],
    ids=["plain", "quoted", "utf8-q", "utf8-b"],
)
def test_matching_named_sender_keeps_refusing_before_token(rig, name):
    rig.client.raw = stored_header(draft(), "Sender", f"{name} <{SENDER}>")
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants
    assert rig.smtp.calls == []


def test_punycode_recipient_is_kept_as_its_literal_ascii_addr_spec(rig):
    address = "recipient@xn--exmple-cua.test"
    rig.client.raw = stored_header(draft(), "To", f"Plain Recipient <{address}>")
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["token"] and preview["to"] == [address]
    assert rig.smtp.calls == []


def test_display_names_do_not_change_envelope_journal_or_stored_wire(rig):
    names = {
        "From": "Plain Owner",
        "To": '"Plain Recipient"',
        "Cc": "Plain Carbon",
        "Bcc": "Plain Blind",
        "Reply-To": "Plain Reply",
    }
    addresses = {
        "From": SENDER,
        "To": RECIPIENT,
        "Cc": "carbon@example.test",
        "Bcc": "blind@example.test",
        "Reply-To": "reply@example.test",
    }
    raw = draft()
    for field, name in names.items():
        raw = stored_header(raw, field, f"{name} <{addresses[field]}>")
    bcc_line = f"Bcc: {names['Bcc']} <{addresses['Bcc']}>\r\n".encode("ascii")
    expected_wire = raw.replace(bcc_line, b"", 1)
    rig.client.raw = raw
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["from"] == SENDER and preview["to"] == [RECIPIENT]
    assert preview["cc"] == [addresses["Cc"]] and preview["bcc"] == [addresses["Bcc"]]
    assert rig.smtp.calls == []
    assert redeem(rig, preview["token"])["sent"] is True
    assert rig.smtp.sent == [
        (SENDER, (RECIPIENT, addresses["Cc"], addresses["Bcc"]), expected_wire)
    ]
    assert rig.client.raw == raw
    audit = records(rig)
    assert audit
    visible = json.dumps(audit, ensure_ascii=False)
    assert all(name not in visible for name in names.values())
    assert all(address not in visible for address in addresses.values())
    assert all(row["recipient_count"] == 3 for row in audit)
    assert all(row["recipient_domains"] == ["example.test"] for row in audit)


BAD_MAILBOXES = [
    pytest.param("", id="empty"),
    pytest.param(" ", id="whitespace"),
    pytest.param("not-an-address", id="missing-at"),
    pytest.param("@example.test", id="empty-local-part"),
    pytest.param("owner@", id="empty-domain"),
    pytest.param("owner@example..test", id="empty-domain-label"),
    pytest.param("owner@-example.test", id="leading-domain-hyphen"),
    pytest.param("owner@example-.test", id="trailing-domain-hyphen"),
    pytest.param("owner@localhost", id="single-domain-label"),
    pytest.param('"owner"@example.test', id="quoted-local-part"),
    pytest.param("ownér@example.test", id="unicode-local-part"),
    pytest.param("owner@exámple.test", id="unicode-domain"),
    pytest.param("=?utf-8?q?owner?=@example.test", id="encoded-local-part"),
    pytest.param("owner@=?utf-8?q?example?=.test", id="encoded-domain"),
    pytest.param("{address},", id="trailing-comma"),
    pytest.param(", {address}", id="leading-comma"),
    pytest.param("{address},, other@example.test", id="duplicate-comma"),
    pytest.param("{address}, , other@example.test", id="empty-middle-entry"),
    pytest.param("Owners: {address};", id="group"),
    pytest.param("Owners:;", id="empty-group"),
    pytest.param("{address} (Owner)", id="trailing-comment"),
    pytest.param("(Owner) {address}", id="leading-comment"),
    pytest.param("owner(comment)@example.test", id="embedded-comment"),
    pytest.param("Plain Owner <{address}", id="missing-angle-close"),
    pytest.param("Plain Owner {address}>", id="missing-angle-open"),
    pytest.param('"Plain Owner <{address}>', id="unclosed-quote"),
    pytest.param("Plain Owner <>", id="empty-angle-address"),
    pytest.param("=?utf-8?q??= <{address}>", id="empty-encoded-display-name"),
    pytest.param("=?utf-8?q?_?= <{address}>", id="blank-encoded-display-name"),
    pytest.param("{address} trailing", id="trailing-text"),
    pytest.param("Plain Ownér <{address}>", id="raw-unicode-name"),
]


@pytest.mark.parametrize("field", (*ADDRESS_FIELDS, "Sender"))
@pytest.mark.parametrize("value", BAD_MAILBOXES)
def test_malformed_stored_address_refuses_before_token(rig, field, value):
    address = SENDER if field in {"From", "Sender"} else RECIPIENT
    rig.client.raw = stored_header(draft(), field, value.format(address=address))
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants
    assert rig.smtp.calls == []


@pytest.mark.parametrize("field", ["From", "Sender"])
@pytest.mark.parametrize("name", ['""', '" "'], ids=["empty", "blank"])
def test_empty_or_blank_from_and_sender_names_refuse_before_token(rig, field, name):
    rig.client.raw = stored_header(draft(), field, f"{name} <{SENDER}>")
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants
    assert rig.smtp.calls == []


UNSAFE_NAMES = [
    pytest.param("other@example.test", id="hidden-address"),
    pytest.param("Other <Owner>", id="angle-brackets"),
    pytest.param("Other, Owner", id="comma"),
    pytest.param("Other; Owner", id="semicolon"),
    pytest.param("Other: Owner", id="colon"),
    pytest.param('Other "Owner"', id="quote"),
    pytest.param("Other\\Owner", id="backslash"),
    pytest.param("=?utf-8?q?Other_Owner?=", id="nested-encoded-word"),
    pytest.param("Other\x00Owner", id="nul"),
    pytest.param("Other\x1fOwner", id="ascii-control"),
    pytest.param("Other\x7fOwner", id="delete"),
    pytest.param("Other\x85Owner", id="unicode-control"),
    pytest.param("Other\u202eOwner", id="bidi-override"),
    pytest.param("Other\u200bOwner", id="zero-width-space"),
    pytest.param("Other\u2066Owner", id="bidi-isolate"),
    pytest.param("Other\ufeffOwner", id="zero-width-no-break-space"),
]


@pytest.mark.parametrize("field", ADDRESS_FIELDS)
@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_decoded_display_name_cannot_hide_address_or_controls(rig, field, name):
    address = SENDER if field == "From" else RECIPIENT
    rig.client.raw = stored_header(draft(), field, f"{encoded_name(name)} <{address}>")
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants
    assert rig.smtp.calls == []


@pytest.mark.parametrize("field", ADDRESS_FIELDS)
@pytest.mark.parametrize(
    "name",
    [
        '"other@example.test"',
        '"Other <Owner>"',
        '"Other, Owner"',
        '"Other; Owner"',
        '"Other: Owner"',
        '"Other\\"Owner"',
        '"Other\\\\Owner"',
        "Other\x00Owner",
        "Other\x7fOwner",
        "Other\tOwner",
        "Other\rOwner",
        "Other\nOwner",
    ],
    ids=[
        "hidden-address",
        "angle-brackets",
        "comma",
        "semicolon",
        "colon",
        "escaped-quote",
        "escaped-backslash",
        "nul",
        "delete",
        "tab",
        "carriage-return",
        "line-feed",
    ],
)
def test_raw_display_name_cannot_hide_address_or_controls(rig, field, name):
    address = SENDER if field == "From" else RECIPIENT
    rig.client.raw = stored_header(draft(), field, f"{name} <{address}>")
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants
    assert rig.smtp.calls == []


@pytest.mark.parametrize("field", ADDRESS_FIELDS)
@pytest.mark.parametrize(
    "word",
    [
        "=?utf-8?q?other=40example.test?=",
        "=?unknown-charset?q?Plain_Owner?=",
        "=?utf-8?q?=FF?=",
        "=?utf-8?B?/w==?=",
        "=?utf-8?B?YWJj!?=",
        "=?utf-8?B?YWJjZ?=",
        "=?utf-7?q?+2AA-?=",
        "=?utf-8?q?Plain=GGOwner?=",
        "=?utf-8?q?Plain=?=",
        "=?utf-8?x?Plain_Owner?=",
        "=?utf-8?q?Plain_Owner",
        "=?utf-8?q?=3D?= =?ascii?q?=3Futf-8=3Fq=3FName=3F=3D?=",
        "=?utf-8?q?Plain=0d=0aOwner?=",
        '"=?utf-8?q?Plain_Owner?="',
    ],
    ids=[
        "q-hidden-address",
        "unknown-charset",
        "q-invalid-utf8",
        "b-invalid-utf8",
        "b-invalid-character",
        "b-invalid-length",
        "decoded-surrogate",
        "q-invalid-escape",
        "q-incomplete-escape",
        "invalid-transfer-encoding",
        "incomplete-word",
        "nested-word-across-charsets",
        "encoded-line-break",
        "encoded-word-inside-quoted-name-defect",
    ],
)
def test_invalid_encoded_display_names_refuse_before_token(rig, field, word):
    address = SENDER if field == "From" else RECIPIENT
    rig.client.raw = stored_header(draft(), field, f"{word} <{address}>")
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "word",
    [
        *[f"=?utf{delimiter}8?q?Plain_Owner?=" for delimiter in '@<>,;:()"\\'],
        *[f"=?utf-16-be?q?{delimiter}A?=" for delimiter in '@<>,;:()"\\'],
    ],
)
def test_raw_encoded_from_name_cannot_hide_address_delimiters(rig, word):
    rig.client.raw = stored_header(draft(), "From", f"{word} <{SENDER}>")
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants
    assert rig.smtp.calls == []


@pytest.mark.parametrize("field", (*ADDRESS_FIELDS, "Sender"))
def test_duplicate_stored_address_headers_refuse_before_token(rig, field):
    address = SENDER if field in {"From", "Sender"} else RECIPIENT
    raw = stored_header(draft(), field, address)
    rig.client.raw = f"{field.lower()}: {address}\r\n".encode("ascii") + raw
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "value",
    [
        "sender@example.test, sender@example.test",
        "Plain Owner <sender@example.test>, Other Owner <other@example.test>",
        "Sender@example.test",
        "sender@EXAMPLE.test",
        "Plain Owner <other@example.test>",
        '"sender@example.test" <other@example.test>',
        "sender@xn--exmple-cua.test",
    ],
    ids=[
        "duplicate-mailbox",
        "multiple-mailboxes",
        "case-local-part",
        "case-domain",
        "nonallowlisted-mailbox",
        "allowlisted-display-only",
        "punycode-not-decoded",
    ],
)
def test_from_matches_exactly_one_allowlisted_addr_spec(rig, value):
    rig.client.raw = stored_header(draft(), "From", value)
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "address", ["owner=?name@example.test", "=?abc@example.test", "=?abc?=@example.test"]
)
@pytest.mark.parametrize("named", [False, True], ids=["bare", "named"])
def test_from_addr_spec_keeps_refusing_encoded_word_markers(rig, address, named):
    rig.service.settings = replace(rig.settings, send_from_allowlist=(address,))
    value = f"Plain Owner <{address}>" if named else address
    rig.client.raw = stored_header(draft(), "From", value)
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "value",
    [
        "other@example.test",
        "Sender@example.test",
        "sender@EXAMPLE.test",
        "sender@example.test, other@example.test",
        "sender@example.test, sender@example.test",
    ],
    ids=["mismatch", "case-local-part", "case-domain", "multiple", "duplicate"],
)
def test_sender_must_match_exactly_one_from_addr_spec(rig, value):
    rig.client.raw = stored_header(draft(), "Sender", value)
    with pytest.raises((MailError, MailValidationError)):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants
    assert rig.smtp.calls == []


@pytest.mark.parametrize(
    "value",
    [
        f"Plain Owner <{SENDER}>",
        f'"Plain Owner" <{SENDER}>',
        f"=?utf-8?q?T=C3=A9st_Owner?= <{SENDER}>",
        f"{encoded_name('Tést Owner')} <{SENDER}>",
    ],
    ids=["plain", "quoted", "utf8-q", "utf8-b"],
)
def test_configuration_parser_and_allowlist_stay_bare_only(value):
    with pytest.raises(ValueError):
        parse_addresses(value)
    settings = Settings(token="x" * 32, bridge_user=SENDER)
    with pytest.raises(ConfigError):
        replace(settings, send_from_allowlist=(value,))
    with pytest.raises(ConfigError):
        Settings.from_env(
            {
                "PROTON_MCP_TOKEN": "x" * 32,
                "PROTON_BRIDGE_USER": SENDER,
                "PROTON_MCP_SEND_FROM": value,
            }
        )


def test_configuration_parser_preserves_bare_address_case():
    assert parse_addresses(f"{SENDER}, Sender@example.test") == [SENDER, "Sender@example.test"]
