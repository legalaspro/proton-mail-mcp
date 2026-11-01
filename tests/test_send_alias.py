"""Sending from an allowed alias: refused unless listed, shown before approval, no bypass."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from attachment_fixtures import FakeImap, client, mail_with, part
from attachment_fixtures import settings as settings  # noqa: F401
from mcp.server.fastmcp.exceptions import ToolError
from test_attachment_drafts import SOURCE, parsed, sending_client
from test_send import DRAFT_ID, RECIPIENT, SENDER, Host, draft, redeem
from test_send import local_certificate as local_certificate
from test_send import rig as rig
from test_send_own_recipients import bridge_draft
from test_send_token_approval import own_rig as own_rig
from view_assertions import view_data
from view_browser import browser, requests, run_view, tool_result

from proton_mail_mcp import mcp_apps, views
from proton_mail_mcp import send as send_module
from proton_mail_mcp.action_log import ActionLog
from proton_mail_mcp.mail import MailValidationError, draft_audit
from proton_mail_mcp.server import create_mcp

ALIAS = "alias@mail.example"
OUTSIDE = "outside@example.test"
REFUSED = [
    "other@mail.example",
    "ALIAS@mail.example",
    "alias@mail.example ",
    f"Alias <{ALIAS}>",
    f"{ALIAS}, {SENDER}",
    f"{ALIAS}\r\nBcc: hidden@example.test",
    SENDER + "x",
    ["not", "text"],
]


def allow_alias(rig):
    rig.settings = replace(rig.settings, send_from_allowlist=(SENDER, ALIAS))
    rig.service.settings = rig.client.settings = rig.settings
    return rig


def test_an_alias_draft_is_written_previewed_confirmed_and_sent_from_the_alias(rig, monkeypatch):
    mail, fake, service = sending_client(allow_alias(rig), monkeypatch)
    created = mail.create_draft(RECIPIENT, subject="S", body="B", from_address=ALIAS)
    [(_, _, _, raw)] = fake.appended
    assert str(parsed(raw)["From"]) == ALIAS
    preview = service.preview_send(created)
    assert preview["from"] == ALIAS and preview["from_alias"] is True
    assert preview["approval"] == "host_confirmation"
    assert rig.smtp.calls == []

    def nothing_sent_yet():
        assert rig.smtp.calls == []

    host = Host(during=nothing_sent_yet)
    assert redeem(rig, preview["token"], host)["sent"] is True
    first = host.prompts[0].split("\n")[0]
    assert f'From: "{ALIAS}" (an alias, not the account\'s own address).' in first
    assert json.loads(host.prompts[0].split("\n")[1])["from_alias"] is True
    # The envelope comes from the alias; the login stays the Bridge account (the recorder checks).
    sender, recipients, delivered = rig.smtp.sent[0]
    assert (sender, recipients) == (ALIAS, (RECIPIENT,))
    assert str(parsed(delivered)["From"]) == ALIAS


def test_the_accounts_own_address_is_no_alias(rig, monkeypatch):
    mail, _, service = sending_client(allow_alias(rig), monkeypatch)
    created = mail.create_draft(RECIPIENT, subject="S", body="B")
    preview = service.preview_send(created)
    assert preview["from"] == SENDER and preview["from_alias"] is False
    host = Host()
    redeem(rig, preview["token"], host)
    assert "an alias" not in host.prompts[0]


@pytest.mark.parametrize("address", REFUSED)
def test_a_from_address_off_the_list_refuses_before_any_mail_work(rig, monkeypatch, address):
    mail, fake, _ = sending_client(allow_alias(rig), monkeypatch, source=mail_with(SOURCE))
    with pytest.raises(MailValidationError, match="not on the allowed list"):
        mail.create_draft(RECIPIENT, subject="S", body="B", from_address=address)
    with pytest.raises(MailValidationError, match="not on the allowed list"):
        mail.forward_message(SOURCE, RECIPIENT, attachments=[], from_address=address)
    assert fake.appended == [] and fake.calls == []


def test_an_empty_list_allows_no_chosen_address_not_even_the_accounts(rig, monkeypatch):
    rig.settings = replace(rig.settings, send_from_allowlist=())
    mail, fake, _ = sending_client(rig, monkeypatch)
    with pytest.raises(MailValidationError, match="not on the allowed list"):
        mail.create_draft(RECIPIENT, subject="S", body="B", from_address=SENDER)
    assert fake.appended == []


def test_a_stored_draft_from_an_address_off_the_list_still_refuses_at_preview(rig):
    # A draft another client saved: guarded send checks what is really stored.
    rig.client.raw = draft(sender="other@mail.example")
    with pytest.raises(MailValidationError, match="exact allowlisted"):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants


def test_an_alias_changed_after_the_preview_refuses_delivery(rig, monkeypatch):
    mail, _, service = sending_client(allow_alias(rig), monkeypatch)
    preview = service.preview_send(
        mail.create_draft(RECIPIENT, subject="S", body="B", from_address=ALIAS)
    )
    mail.create_draft(RECIPIENT, subject="S", body="B")
    with pytest.raises(MailValidationError, match="changed"):
        redeem(rig, preview["token"], Host())
    assert rig.smtp.sent == []


def test_mail_to_an_address_off_the_own_list_keeps_the_form(own_rig, monkeypatch):
    rig = allow_alias(own_rig)
    mail, _, service = sending_client(rig, monkeypatch)
    mail.create_draft(RECIPIENT, subject="Note", body="B")
    assert service.preview_send(DRAFT_ID)["approval"] == "host_confirmation"
    mail.create_draft(RECIPIENT, subject="Note", body="B", from_address=ALIAS)
    preview = service.preview_send(DRAFT_ID)
    assert preview["approval"] == "host_confirmation"
    host = Host()
    assert redeem(rig, preview["token"], host)["sent"] is True
    assert len(host.prompts) == 1 and rig.smtp.sent[0][0] == ALIAS


@pytest.mark.parametrize("allowed", [(ALIAS,), (RECIPIENT, ALIAS)], ids=["alias", "both"])
def test_an_alias_note_to_the_account_needs_no_form(own_rig, monkeypatch, allowed):
    # As reported: the account is the recipient, and the host has no form.
    rig = own_rig
    rig.settings = replace(rig.settings, bridge_user=RECIPIENT, send_from_allowlist=allowed)
    rig.service.settings = rig.client.settings = rig.settings
    confirm = AsyncMock(side_effect=AssertionError("Unexpected confirmation request"))
    monkeypatch.setattr(send_module, "require_confirmation", confirm)
    rig.client.raw = draft(sender=ALIAS, to=RECIPIENT)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["from_alias"] is True and preview["approval"] == "token"
    host = Host(support=False)
    assert redeem(rig, preview["token"], host)["sent"] is True
    assert rig.smtp.sent == [(ALIAS, (RECIPIENT,), rig.client.raw)]
    assert host.prompts == []
    confirm.assert_not_awaited()


@pytest.mark.parametrize(
    "sender,fields",
    [
        (ALIAS, {"to": SENDER}),
        (SENDER, {"to": ALIAS}),
        (SENDER, {"to": SENDER}),
        (ALIAS, {"to": ALIAS}),
        (ALIAS, {"to": f"{SENDER}, {ALIAS}"}),
        (ALIAS, {"to": SENDER, "cc": ALIAS}),
        (ALIAS, {"to": SENDER, "bcc": ALIAS}),
        (ALIAS, {"to": SENDER, "cc": SENDER, "bcc": SENDER}),
        (ALIAS, {"to": SENDER.upper()}),
        (ALIAS, {"to": ALIAS.title()}),
        (ALIAS, {"to": f'"Me" <{SENDER}>'}),
    ],
)
def test_every_recipient_being_the_users_own_needs_no_form(own_rig, sender, fields):
    rig = allow_alias(own_rig)
    rig.client.raw = draft(sender=sender, **fields)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "token"
    host = Host(support=False)
    assert redeem(rig, preview["token"], host)["sent"] is True
    assert host.prompts == []
    assert rig.smtp.sent[0][:2] == (sender, tuple(preview["to"] + preview["cc"] + preview["bcc"]))


@pytest.mark.parametrize(
    "account,allowed,sender,to",
    [
        # Written with capitals, on the list or as the account: case is ignored.
        (SENDER, (SENDER, ALIAS.title()), ALIAS.title(), ALIAS),
        (SENDER, (SENDER, ALIAS.title()), SENDER, ALIAS),
        (SENDER.title(), (ALIAS,), ALIAS, SENDER),
        # An account in display form still means its address.
        (f"Me <{SENDER}>", (ALIAS,), ALIAS, SENDER),
    ],
)
def test_own_addresses_match_however_they_are_configured(own_rig, account, allowed, sender, to):
    rig = own_rig
    rig.settings = replace(rig.settings, bridge_user=account, send_from_allowlist=allowed)
    rig.service.settings = rig.client.settings = rig.settings
    rig.client.raw = draft(sender=sender, to=to)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "token"
    host = Host(support=False)
    assert redeem(rig, preview["token"], host)["sent"] is True
    assert host.prompts == []


@pytest.mark.parametrize(
    "to,headers,recipients",
    [
        (f"<{SENDER}>", (), (SENDER,)),
        (f'"Me" <{SENDER}>', (), (SENDER,)),
        (f"<{SENDER}>", (f"Cc: <{ALIAS}>",), (SENDER, ALIAS)),
    ],
)
def test_a_bridge_shaped_alias_note_to_yourself_needs_no_form(own_rig, to, headers, recipients):
    rig = allow_alias(own_rig)
    rig.client.raw = bridge_draft(to, sender=ALIAS, extra_headers=headers)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["from"] == ALIAS and preview["approval"] == "token"
    host = Host(support=False)
    assert redeem(rig, preview["token"], host)["sent"] is True
    assert host.prompts == []
    assert rig.smtp.sent == [(ALIAS, recipients, rig.client.raw)]


@pytest.mark.parametrize(
    "to,headers",
    [
        (f"<{OUTSIDE}>", ()),
        (f"<{SENDER}>", (f"Cc: <{OUTSIDE}>",)),
        (f"<{SENDER}>", (f"Bcc: <{OUTSIDE}>",)),
    ],
)
def test_a_bridge_shaped_alias_draft_to_anyone_else_keeps_the_form(own_rig, to, headers):
    rig = allow_alias(own_rig)
    rig.client.raw = bridge_draft(to, sender=ALIAS, extra_headers=headers)
    assert rig.service.preview_send(DRAFT_ID)["approval"] == "host_confirmation"


@pytest.mark.parametrize("name", ["To", "Cc", "Bcc", "cC"])
@pytest.mark.parametrize("fold", ["\r\n ", "\r\n\t", " \r\n ", "\n "])
def test_an_own_recipient_folded_right_after_the_colon_refuses(own_rig, name, fold):
    # Newer parsers strip such a line break, so the own-address path must not depend on them.
    rig = allow_alias(own_rig)
    raw = bridge_draft(SENDER, sender=ALIAS)
    if name == "To":
        rig.client.raw = raw.replace(f"To: {SENDER}".encode(), f"To:{fold}{SENDER}".encode())
    else:
        rig.client.raw = bridge_draft(
            SENDER, sender=ALIAS, extra_headers=(f"{name}:{fold}{ALIAS}",)
        )
    with pytest.raises(MailValidationError, match="no controls or folded lines"):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants


def test_a_draft_with_no_recipient_refuses_with_the_flag_on(own_rig):
    rig = allow_alias(own_rig)
    rig.client.raw = bridge_draft(SENDER, sender=ALIAS).replace(f"To: {SENDER}\r\n".encode(), b"")
    with pytest.raises(MailValidationError, match="recipient count"):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants


@pytest.mark.parametrize(
    "fields",
    [
        {"to": OUTSIDE},
        {"to": f"{SENDER}, {OUTSIDE}"},
        {"to": SENDER, "cc": OUTSIDE},
        {"to": SENDER, "bcc": OUTSIDE},
        {"to": OUTSIDE, "cc": SENDER},
        {"to": f"{ALIAS}, {SENDER}", "bcc": OUTSIDE},
        {"to": "sender+tag@example.test"},
        {"to": "sender@example.test.outside.test"},
        {"to": "prefixsender@example.test"},
    ],
)
@pytest.mark.parametrize("sender", [SENDER, ALIAS])
def test_any_other_recipient_keeps_the_form(own_rig, sender, fields):
    rig = allow_alias(own_rig)
    rig.client.raw = draft(sender=sender, **fields)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "host_confirmation"
    with pytest.raises(MailValidationError, match="confirmation was not accepted"):
        redeem(rig, preview["token"], Host(support=False))
    assert rig.smtp.calls == []
    host = Host()
    assert redeem(rig, rig.service.preview_send(DRAFT_ID)["token"], host)["sent"] is True
    assert len(host.prompts) == 1


@pytest.mark.parametrize(
    "sender,to", [(ALIAS, SENDER), (SENDER, ALIAS), (SENDER, SENDER), (SENDER, RECIPIENT)]
)
def test_with_the_flag_off_a_note_to_yourself_keeps_the_form(own_rig, sender, to):
    rig = allow_alias(own_rig)
    rig.service.settings = replace(rig.settings, send_self_no_form=False)
    rig.client.raw = draft(sender=sender, to=to)
    preview = rig.service.preview_send(DRAFT_ID)
    assert preview["approval"] == "host_confirmation"
    with pytest.raises(MailValidationError, match="confirmation was not accepted"):
        redeem(rig, preview["token"], Host(support=False))
    assert rig.smtp.calls == []


@pytest.mark.parametrize("sender", ["other@mail.example", ALIAS.upper()])
def test_a_from_off_the_list_still_refuses_with_only_own_recipients(own_rig, sender):
    rig = allow_alias(own_rig)
    rig.client.raw = draft(sender=sender, to=SENDER)
    with pytest.raises(MailValidationError, match="exact allowlisted"):
        rig.service.preview_send(DRAFT_ID)
    assert not rig.tokens._grants and rig.smtp.calls == []


@pytest.mark.parametrize("fields", [{"attachment": b"file"}, {"subject": "Fwd: note"}])
def test_an_attachment_or_forward_to_yourself_keeps_the_form(own_rig, fields):
    rig = allow_alias(own_rig)
    rig.client.raw = draft(sender=ALIAS, to=SENDER, **fields)
    assert rig.service.preview_send(DRAFT_ID)["approval"] == "host_confirmation"


@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
def test_another_recipient_added_after_the_preview_refuses(own_rig, field):
    rig = allow_alias(own_rig)
    rig.client.raw = draft(sender=ALIAS, to=SENDER)
    token = rig.service.preview_send(DRAFT_ID)["token"]
    rig.client.raw = draft(sender=ALIAS, **{"to": SENDER, field: OUTSIDE})
    with pytest.raises(MailValidationError, match="changed"):
        redeem(rig, token, Host(support=False))
    assert rig.smtp.calls == []


def test_a_forward_can_come_from_an_alias_and_still_needs_the_form(rig, monkeypatch):
    source = mail_with(SOURCE, part("a.txt", "text/plain", b"file"))
    mail, _, service = sending_client(allow_alias(rig), monkeypatch, source=source)
    forwarded = mail.forward_message(SOURCE, RECIPIENT, attachments=[1], from_address=ALIAS)
    preview = service.preview_send(forwarded["draft_id"])
    assert preview["from"] == ALIAS and preview["approval"] == "host_confirmation"
    host = Host("decline")
    with pytest.raises(MailValidationError):
        redeem(rig, preview["token"], host)
    assert ALIAS in host.prompts[0] and '"a.txt"' in host.prompts[0]
    assert rig.smtp.sent == []


def mcp_for(settings, fake, tmp_path, **overrides):
    configured = replace(
        settings,
        token="alias-tests-token-" + "x" * 32,
        confirm_mutations=False,
        action_log_path=str(tmp_path / "log.jsonl"),
        send_from_allowlist=("reader@example.com", ALIAS),
        **overrides,
    )
    return create_mcp(configured, client(configured, fake)), configured


def call(mcp, name, arguments):
    return asyncio.run(mcp._tool_manager.call_tool(name, arguments, convert_result=True))


def test_listing_allowed_addresses_touches_no_mail(settings, tmp_path):
    fake = FakeImap({"INBOX": {}})
    mcp, _ = mcp_for(settings, fake, tmp_path)
    _, listed = call(mcp, "list_from_addresses", {})
    assert listed["default"] == "reader@example.com"
    assert listed["allowed"] == ["reader@example.com", ALIAS]
    assert listed["default_allowed"] is True and listed["send_enabled"] is False
    assert fake.calls == []
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    tool = tools["list_from_addresses"]
    assert tool.annotations.readOnlyHint is True
    assert tool.meta == {"ui": {"visibility": ["model"]}}
    assert "from_address" in tools["create_draft"].inputSchema["properties"]
    assert "from_address" in tools["forward_message"].inputSchema["properties"]


def test_an_account_off_the_list_is_reported(settings, tmp_path):
    configured = replace(settings, send_from_allowlist=(ALIAS,))
    listed = views.from_addresses(configured)
    assert listed["default_allowed"] is False and listed["allowed"] == [ALIAS]


def test_the_draft_tool_refuses_an_address_off_the_list_and_writes_no_draft(settings, tmp_path):
    fake = FakeImap({"Drafts": {}})
    mcp, _ = mcp_for(settings, fake, tmp_path)
    arguments = {"to": "x@example.com", "subject": "S", "body": "B"}
    with pytest.raises(ToolError, match="not on the allowed list"):
        call(mcp, "create_draft", {**arguments, "from_address": "evil@example.com"})
    assert fake.appended == [] and fake.calls == []
    call(mcp, "create_draft", {**arguments, "from_address": ALIAS})
    assert str(parsed(fake.appended[0][3])["From"]) == ALIAS


def test_the_audit_names_the_alias_domain_only(settings, tmp_path):
    fake = FakeImap({"Drafts": {}})
    mcp, configured = mcp_for(settings, fake, tmp_path)
    call(
        mcp,
        "create_draft",
        {"to": "x@example.com", "subject": "S", "body": "B", "from_address": ALIAS},
    )
    with ActionLog(configured.action_log_path).transaction(readonly=True) as transaction:
        finished = [row for row in transaction.records() if row.get("action") == "audit"][-1]
    assert finished["outcome"] == "completed" and finished["from_domain"] == "mail.example"
    assert ALIAS not in json.dumps(finished)
    assert "from_domain" not in draft_audit("a@b.test", None, "S", "B")


def composer(mcp, arguments):
    return view_data(call(mcp, "compose_reply", arguments))


def test_the_composer_starts_from_the_account_and_offers_the_allowed_addresses(settings, tmp_path):
    mcp, _ = mcp_for(settings, FakeImap({}), tmp_path)
    view = composer(mcp, {"to": "x@example.com"})
    assert view["from"] == view["default_from"] == "reader@example.com"
    assert view["from_choices"] == ["reader@example.com", ALIAS]
    assert "from_address" not in view["draft"]


def test_the_composer_starts_from_a_chosen_alias_and_refuses_any_other(settings, tmp_path):
    mcp, _ = mcp_for(settings, FakeImap({}), tmp_path)
    view = composer(mcp, {"to": "x@example.com", "from_address": ALIAS})
    assert view["from"] == view["draft"]["from_address"] == ALIAS
    with pytest.raises(ToolError, match="not on the allowed list"):
        composer(mcp, {"to": "x@example.com", "from_address": "evil@example.com"})


def stored_draft(sender):
    return {
        "message_id": "<draft@example.test>",
        "from": sender,
        "to": "alice@example.test",
        "subject": "S",
        "date": "",
        "headers": {},
        "text": "Text",
        "attachments": [],
        "truncated": False,
    }


def edit_draft(sender):
    return views.reply_view(
        {"messages": [stored_draft(sender)], "partial": False},
        "<draft@example.test>",
        own={SENDER},
        edit=True,
        sender=SENDER,
        allowed=(SENDER, ALIAS),
    )


def test_editing_a_draft_from_an_allowed_alias_keeps_it():
    text, view = edit_draft(f"Me <{ALIAS}>")
    assert view["from"] == view["draft"]["from_address"] == ALIAS
    assert text["warnings"] == []


def test_editing_a_draft_from_an_address_off_the_list_says_what_changes():
    text, view = edit_draft("gone@mail.example")
    assert view["from"] == SENDER and "from_address" not in view["draft"]
    assert text["warnings"] == [
        "This draft is from gone@mail.example, which is not an allowed From address. "
        f"Saving writes it from {SENDER}."
    ]
    # The draft's own address is no change and no warning.
    text, view = edit_draft(SENDER)
    assert text["warnings"] == [] and "from_address" not in view["draft"]


FROM_CHOICE = """
const doc = t.doc;
await t.waitFor(() => doc.getElementById("to").value, "the prefilled fields");
const select = doc.getElementById("from-select");
const save = doc.getElementById("save");
t.check("select_visible", t.visible(select));
t.check("plain_visible", t.visible(doc.getElementById("from")));
t.check("options", Array.from(select.options).map((option) => option.value));
t.check("selected", select.value);
save.click();
await t.waitFor(() => doc.querySelector("#saved .banner.ok"), "the first draft");
await t.sleep(50);
t.check("disabled_after_save", save.disabled);
select.value = select.options[1].value;
select.dispatchEvent(new Event("change", { bubbles: true }));
t.check("enabled_after_choice", !save.disabled);
save.click();
await t.waitFor(() => t.requests("tools/call").length === 2, "the second draft");
await t.sleep(50);
doc.getElementById("reset").click();
t.check("after_reset", select.value);
"""


SAVED = {
    "content": [{"type": "text", "text": "<new@example.test>"}],
    "structuredContent": {"result": "<new@example.test>"},
}


@browser
def test_the_composer_shows_the_address_and_saves_the_choice(tmp_path):
    result = tool_result(
        mcp_apps.view_result(
            *views.new_draft_view(
                "x@example.test", None, "S", "B", sender=SENDER, allowed=(SENDER, ALIAS)
            )
        )
    )
    report = run_view(
        tmp_path, "reply", result, FROM_CHOICE, tools={"create_draft": [SAVED, SAVED]}
    )
    checks = report["checks"]
    assert checks["select_visible"] is True and checks["plain_visible"] is False
    assert checks["options"] == [SENDER, ALIAS] and checks["selected"] == SENDER
    assert checks["disabled_after_save"] is True and checks["enabled_after_choice"] is True
    assert checks["after_reset"] == SENDER
    first, second = requests(report, "tools/call")
    # The account's own address is the default and is not passed; an alias is.
    assert "from_address" not in first["params"]["arguments"]
    assert second["params"]["arguments"]["from_address"] == ALIAS


PLAIN_FROM = """
const doc = t.doc;
await t.waitFor(() => doc.getElementById("to").value, "the prefilled fields");
t.check("select_visible", t.visible(doc.getElementById("from-select")));
t.check("plain", t.visible(doc.getElementById("from")) && doc.getElementById("from").textContent);
"""


@browser
def test_with_one_address_the_composer_just_names_it(tmp_path):
    result = tool_result(
        mcp_apps.view_result(*views.new_draft_view("x@example.test", None, "S", "B", sender=SENDER))
    )
    checks = run_view(tmp_path, "reply", result, PLAIN_FROM)["checks"]
    assert checks["select_visible"] is False and checks["plain"] == SENDER


REVIEW_FROM = """
const doc = t.doc;
await t.waitFor(() => doc.getElementById("hash").textContent, "the review");
const rows = Array.from(doc.querySelectorAll("#fields .field")).map((row) => [
  row.querySelector(".key").textContent,
  row.querySelector(".value").textContent,
]);
t.check("rows", rows);
"""


@browser
def test_the_send_review_marks_an_alias(rig, monkeypatch, tmp_path):
    mail, _, service = sending_client(allow_alias(rig), monkeypatch)
    created = mail.create_draft(RECIPIENT, subject="S", body="B", from_address=ALIAS)
    result = tool_result(mcp_apps.view_result(*views.review_view(service.review_send(created))))
    checks = run_view(tmp_path, "review_send", result, REVIEW_FROM)["checks"]
    assert dict(checks["rows"])["From"] == f"{ALIAS} (alias)"
