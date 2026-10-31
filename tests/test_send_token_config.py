"""Token approval has a strict opt-in and no recipient configuration escape hatch."""

import hashlib
from dataclasses import fields, replace
from unittest.mock import Mock

import pytest

from proton_mail_mcp import send
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.server import main

BASE = {"PROTON_MCP_TOKEN": "x" * 32, "PROTON_BRIDGE_USER": "sender@example.test"}


def test_token_approval_defaults_off():
    assert Settings.from_env(BASE).send_self_no_form is False


def test_token_approval_explicit_zero_stays_off():
    configured = Settings.from_env({**BASE, "PROTON_MCP_SEND_SELF_NO_FORM": "0"})
    assert configured.send_self_no_form is False


@pytest.mark.parametrize("value", ["", "true", "True", "yes", "2", " 1", "1 ", "01"])
def test_token_approval_accepts_only_zero_or_one(value):
    with pytest.raises(ConfigError, match="PROTON_MCP_SEND_SELF_NO_FORM must be 0 or 1"):
        Settings.from_env({**BASE, "PROTON_MCP_SEND_SELF_NO_FORM": value})


def test_typed_ok_defaults_on():
    assert Settings.from_env(BASE).send_typed_ok is True


@pytest.mark.parametrize("value,expected", [("0", False), ("1", True)])
def test_typed_ok_switch_reads_exactly_zero_or_one(value, expected):
    configured = Settings.from_env({**BASE, "PROTON_MCP_SEND_TYPED_OK": value})
    assert configured.send_typed_ok is expected


@pytest.mark.parametrize("value", ["", "off", "false", "no", "2", " 0", "0 ", "00"])
def test_typed_ok_switch_refuses_anything_else(value):
    with pytest.raises(ConfigError, match="PROTON_MCP_SEND_TYPED_OK must be 0 or 1"):
        Settings.from_env({**BASE, "PROTON_MCP_SEND_TYPED_OK": value})


def test_typed_ok_switch_must_be_boolean():
    with pytest.raises(ConfigError, match="Send switches must be boolean"):
        replace(Settings.from_env(BASE), send_typed_ok=0)


def test_old_flag_name_returns_to_the_form():
    configured = Settings.from_env({**BASE, "PMM_SEND_TOKEN_APPROVAL": "1"})
    assert configured.send_self_no_form is False


@pytest.mark.parametrize(
    "env,warned",
    [
        ({**BASE, "PMM_SEND_TOKEN_APPROVAL": "1"}, True),
        ({**BASE, "PMM_SEND_TOKEN_APPROVAL": "1", "PROTON_MCP_SEND_SELF_NO_FORM": "1"}, True),
        ({**BASE, "PROTON_MCP_SEND_SELF_NO_FORM": "1"}, False),
        (BASE, False),
    ],
)
def test_startup_warns_once_while_the_old_flag_name_is_set(monkeypatch, capsys, env, warned):
    run = Mock()
    monkeypatch.setattr("proton_mail_mcp.server.create_app", Mock())
    monkeypatch.setattr("proton_mail_mcp.server.uvicorn.run", run)
    main(env)
    run.assert_called_once()
    assert capsys.readouterr().err.count("PMM_SEND_TOKEN_APPROVAL is ignored") == int(warned)


@pytest.mark.parametrize(
    "env,warned",
    [
        ({**BASE, "PROTON_MCP_ENABLE_SEND": "1", "PROTON_MCP_SEND_SELF_NO_FORM": "1"}, True),
        ({**BASE, "PROTON_MCP_SEND_SELF_NO_FORM": "1"}, False),
        ({**BASE, "PROTON_MCP_ENABLE_SEND": "1"}, False),
        (BASE, False),
    ],
)
def test_startup_says_the_send_from_list_receives_with_no_form(monkeypatch, capsys, env, warned):
    monkeypatch.setattr("proton_mail_mcp.server.create_app", Mock())
    monkeypatch.setattr("proton_mail_mcp.server.uvicorn.run", Mock())
    main(env)
    assert capsys.readouterr().err.count("can receive mail with no form") == int(warned)


def test_startup_reads_the_old_flag_name_from_the_process_environment(monkeypatch, capsys):
    for name, value in {**BASE, "PMM_SEND_TOKEN_APPROVAL": "1"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("proton_mail_mcp.server.create_app", Mock())
    monkeypatch.setattr("proton_mail_mcp.server.uvicorn.run", Mock())
    main()
    assert "PMM_SEND_TOKEN_APPROVAL is ignored" in capsys.readouterr().err


def test_token_approval_exact_one_enables():
    configured = Settings.from_env({**BASE, "PROTON_MCP_SEND_SELF_NO_FORM": "1"})
    assert configured.send_self_no_form is True


@pytest.mark.parametrize("value", [1, 0, "1", "true", None])
def test_token_approval_direct_configuration_requires_boolean(value):
    with pytest.raises(ConfigError, match="boolean"):
        replace(Settings.from_env(BASE), send_self_no_form=value)


def test_recipient_pin_has_no_environment_or_settings_override(monkeypatch):
    original_pin = send._OWN_ADDRESS_SHA256
    other_pin = hashlib.sha256(b"other@example.test").hexdigest()
    overrides = {
        "PROTON_MCP_SEND_SELF_NO_FORM": "1",
        "PMM_SEND_TOKEN_ADDRESS": "other@example.test",
        "PMM_SEND_TOKEN_ADDRESS_SHA256": other_pin,
        "PMM_SEND_TOKEN_RECIPIENTS": "other@example.test",
        "PMM_SEND_TOKEN_ALLOWLIST": "other@example.test",
        "PMM_SEND_OWN_ADDRESS_SHA256": other_pin,
        "PROTON_MCP_SEND_FROM": "other@example.test",
        "_OWN_ADDRESS_SHA256": other_pin,
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    configured = Settings.from_env({**BASE, **overrides})
    assert configured.send_self_no_form is True
    assert configured.send_from_allowlist == ("other@example.test",)
    assert send._OWN_ADDRESS_SHA256 == original_pin
    assert send._OWN_ADDRESS_SHA256 != other_pin
    assert original_pin == "1632be39658c2291f7b38247c04b3ab4d009cfeb07ec24fbf7fe78762a89dca5"
    assert send._own_address("other@example.test") is False
    field_names = {field.name for field in fields(Settings)}
    assert not field_names & {
        "send_token_address",
        "send_token_address_sha256",
        "send_token_recipients",
        "send_token_allowlist",
        "send_own_address_sha256",
    }
    assert not [name for name in field_names if "own_address" in name or "recipient_pin" in name]


def test_token_approval_keeps_ttl_default_and_ceiling():
    values = {**BASE, "PROTON_MCP_SEND_SELF_NO_FORM": "1"}
    assert Settings.from_env(values).send_token_ttl == 300
    assert Settings.from_env({**values, "PROTON_MCP_SEND_TOKEN_TTL": "600"}).send_token_ttl == 600
    with pytest.raises(ConfigError, match="at most 600"):
        Settings.from_env({**values, "PROTON_MCP_SEND_TOKEN_TTL": "601"})
