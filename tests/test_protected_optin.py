"""Protected senders are opt-in: nothing is listed by default, a configured file still refuses."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_attachment_drafts import sending_client
from test_cleanup_guard import settings as settings
from test_cleanup_guard import setup as cleanup_setup
from test_delete_draft import DraftImap, set_draft_sender
from test_mail import client, message
from test_send import Host, redeem
from test_send import local_certificate as local_certificate
from test_send import rig as rig
from test_unsubscribe import setup as unsubscribe_setup

from proton_mail_mcp import protected
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.guarded import CleanupService
from proton_mail_mcp.protected import ProtectedPolicy
from proton_mail_mcp.server import create_mcp

# conftest gives every other test module generic lists; this one tests what really ships.
SHIPPED_PROTECTED_DEFAULTS = True

GENERIC = Path(__file__).parent / "fixtures" / "generic_protected"
LISTED = "listed.example.org"
# The generic lists refuse this sender too, so a preview that passes shows they are gone.
BANK = "bank.example.org"
DOMAIN = "Protected sender domain in From"
# The format of a local policy file: every field a deployed one may hold.
POLICY_FILE = {
    "protect_personal_domains": True,
    "personal_provider_allowlist": ["approved@gmail.com"],
    "addresses": ["friend@example.test"],
    "domains": [LISTED, "bücher.example.org"],
    "domain_patterns": ["*bank*", "*.gov"],
    "keywords": ["steueramt"],
    "personal_domains": ["gmail.com"],
}


def environment(**values):
    return {"PROTON_MCP_TOKEN": "x" * 32, "PROTON_BRIDGE_USER": "user@example.test", **values}


def policy_from_env(values):
    """The policy a running server loads, from its environment."""
    return CleanupService(SimpleNamespace(), Settings.from_env(values)).policy


def write_policy(tmp_path, **fields):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return str(path)


def with_sender(item, address):
    item.headers = item.headers.replace(b"sender@example.com", address.encode("ascii"))


def assert_off_then_on(settings, tmp_path, preview):
    off = preview(settings)
    assert off["token"] and off["blocked"] == []
    path = write_policy(tmp_path, domains=[BANK])
    on = preview(replace(settings, protected_config_path=path))
    assert on["token"] is None and DOMAIN in on["blocked"][0]["reason"]


def test_the_shipped_defaults_list_nothing():
    data = json.loads(Path(protected.__file__).with_name("protected_default.json").read_text())
    assert set(data) - protected._FIELDS == {"protect_personal_domains"}
    assert not any(data[name] for name in protected._FIELDS)


@pytest.mark.parametrize(
    "sender",
    [
        f"alerts@{LISTED}",
        "alerts@bank.example.org",
        "info@service.gov",
        "person@gmail.com",
        "person@proton.me",
        "Kantonales Steueramt <info@office.example>",
    ],
)
def test_with_no_configured_file_no_sender_is_refused(sender):
    assert policy_from_env(environment()).evaluate({"from": sender}) == ([], [])


def test_an_empty_setting_counts_as_unset():
    values = environment(PROTON_MCP_PROTECTED_CONFIG="")
    assert policy_from_env(values).check({"from": f"alerts@{BANK}"}) == []


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"from": ""},
        {"from": "broken <"},
        {"from": "good@example.org", "sender": "malformed"},
        {"from": "good@example.org", "reply_to": ""},
        {"from": "good@example.org", "sender_invalid": True},
    ],
)
def test_unusable_sender_headers_refuse_with_no_list(row):
    assert policy_from_env(environment()).check(row)


@pytest.mark.parametrize("mode", [{}, {"mode": "extend"}, {"mode": "replace"}])
@pytest.mark.parametrize(
    "sender,reason",
    [
        (f"a@{LISTED}", DOMAIN),
        (f"A@{LISTED.upper()}", DOMAIN),
        (f"Display <a@mail.{LISTED}>", DOMAIN),
        ("a@xn--bcher-kva.example.org", DOMAIN),
        ("a@regionalbank.example", "Protected financial, insurance or government domain in From"),
        ("a@service.gov", "Protected financial, insurance or government domain in From"),
        ("Kantonales Steueramt <a@office.example>", "Protected sender display name in From"),
        ("person@gmail.com", "Personal mail provider in From"),
        ("friend@example.test", "Protected personal address in From"),
    ],
)
def test_a_file_in_the_current_format_refuses_what_it_lists(tmp_path, mode, sender, reason):
    path = write_policy(tmp_path, **POLICY_FILE, **mode)
    policy = policy_from_env(environment(PROTON_MCP_PROTECTED_CONFIG=path))
    assert policy.check({"from": sender}) == [reason]


@pytest.mark.parametrize("mode", [{}, {"mode": "extend"}, {"mode": "replace"}])
def test_a_file_in_the_current_format_keeps_its_exceptions_and_every_header(tmp_path, mode):
    path = write_policy(tmp_path, **POLICY_FILE, **mode)
    policy = policy_from_env(environment(PROTON_MCP_PROTECTED_CONFIG=path))
    assert policy.check({"from": "person@example.org"}) == []
    reasons, bypasses = policy.evaluate({"from": "approved@gmail.com"})
    assert reasons == [] and [bypass["header"] for bypass in bypasses] == ["From"]
    row = {"from": "person@example.org", "sender": f"a@{LISTED}", "reply_to": "b@gmail.com"}
    assert policy.check(row) == [
        "Protected sender domain in Sender",
        "Personal mail provider in Reply-To",
    ]


def test_a_file_holding_the_old_lists_gives_the_old_rules(tmp_path, monkeypatch):
    # The fixture holds the lists that shipped as defaults before they became opt-in.
    path = tmp_path / "policy.json"
    path.write_text((GENERIC / "protected_default.json").read_text())
    configured = ProtectedPolicy(str(path))
    monkeypatch.setattr(protected, "__file__", str(GENERIC / "protected.py"))
    before = ProtectedPolicy()
    assert before.rules["domain_patterns"] and before.rules["personal_domains"]
    assert configured.rules == before.rules
    assert configured.protect_personal is before.protect_personal is True
    assert configured.allowlist_digest() == before.allowlist_digest()


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{not json",
        b'{"domains": ["\xff"]}',
        b"[]",
        b"null",
        b'{"blocked_domains": []}',
        b'{"domains": "listed.example.org"}',
        b'{"domains": [""]}',
        b'{"domains": [1]}',
        b'{"mode": "off"}',
        b'{"protect_personal_domains": "false"}',
        b'{"personal_provider_allowlist": ["*@gmail.com"]}',
    ],
)
def test_a_malformed_configured_file_fails_closed(tmp_path, content):
    path = tmp_path / "private-name.json"
    path.write_bytes(content)
    with pytest.raises(ConfigError) as error:
        policy_from_env(environment(PROTON_MCP_PROTECTED_CONFIG=str(path)))
    assert "private-name" not in str(error.value)


@pytest.mark.parametrize("name", ["missing.json", "directory"])
def test_an_unreadable_configured_file_fails_closed(tmp_path, name):
    (tmp_path / "directory").mkdir()
    path = str(tmp_path / name)
    with pytest.raises(ConfigError) as error:
        policy_from_env(environment(PROTON_MCP_PROTECTED_CONFIG=path))
    assert str(tmp_path) not in str(error.value)


def test_a_bad_configured_file_stops_cleanup_rather_than_turning_the_list_off(settings, tmp_path):
    path = tmp_path / "policy.json"
    path.write_text("{not json")
    mail, _ = unsubscribe_setup(replace(settings, protected_config_path=str(path)))
    for action in ("archive", "unsubscribe", "delete_draft"):
        with pytest.raises(ConfigError):
            mail.preview_action(action, query={})


def test_a_cleanup_preview_is_off_with_no_file_and_on_with_one(settings, tmp_path):
    def preview(current):
        service, fake, _ = cleanup_setup(current)
        with_sender(fake.folders["INBOX"]["1"], f"alerts@{BANK}")
        return service.preview_action("archive", query={})

    assert_off_then_on(settings, tmp_path, preview)


def test_an_unsubscribe_preview_is_off_with_no_file_and_on_with_one(settings, tmp_path):
    def preview(current):
        mail, fake = unsubscribe_setup(current)
        with_sender(fake.folders["INBOX"]["1"], f"alerts@{BANK}")
        return mail.preview_action("unsubscribe", query={})

    assert_off_then_on(settings, tmp_path, preview)


def test_a_draft_deletion_preview_is_off_with_no_file_and_on_with_one(settings, tmp_path):
    def preview(current):
        drafts = {"1": message("<first@example.com>", flags={r"\Draft"})}
        fake = DraftImap(
            {"INBOX": {}, "Drafts": drafts}, folder_flags={"Drafts": r"\HasNoChildren \Drafts"}
        )
        set_draft_sender(fake, f"alerts@{BANK}")
        return client(current, fake).preview_action("delete_draft", ids=["<first@example.com>"])

    assert_off_then_on(settings, tmp_path, preview)


@pytest.mark.parametrize("configured", [False, True], ids=["no-file", "file"])
@pytest.mark.parametrize("recipient", [f"a@{LISTED}", "a@bank.example.org", "a@gmail.com"])
def test_the_send_path_never_refuses_a_recipient_by_domain(
    rig, monkeypatch, tmp_path, configured, recipient
):
    if configured:
        path = write_policy(tmp_path, **POLICY_FILE)
        rig.settings = replace(rig.settings, protected_config_path=path)
    mail, _, service = sending_client(rig, monkeypatch)
    preview = service.preview_send(mail.create_draft(recipient, subject="S", body="B"))
    assert preview["to"] == [recipient]
    assert redeem(rig, preview["token"], Host())["sent"] is True
    assert rig.smtp.sent[0][1] == (recipient,)


def test_the_send_tools_state_what_the_list_covers(rig):
    tools = {
        tool.name: tool for tool in asyncio.run(create_mcp(rig.settings, rig.client).list_tools())
    }
    for name in ("create_draft", "preview_send", "send_draft"):
        description = " ".join(tools[name].description.split())
        assert (
            "Protected domains, an optional operator block list set via "
            "PROTON_MCP_PROTECTED_CONFIG and off by default, check cleanup senders, "
            "not recipients." in description
        )
