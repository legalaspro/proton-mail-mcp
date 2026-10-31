from dataclasses import replace

import pytest

from proton_mail_mcp.config import ConfigError, Settings

BASE = {"PROTON_MCP_TOKEN": "x" * 32, "PROTON_BRIDGE_USER": "sender@example.test"}


def test_send_defaults_are_disabled_and_conservative():
    settings = Settings.from_env(BASE)
    assert settings.enable_send is False
    assert settings.smtp_allow_unverified is False
    assert settings.send_from_allowlist == ()
    assert settings.send_max_recipients == 10
    assert settings.send_daily_cap == 20
    assert settings.send_token_ttl == 300
    assert settings.send_confirm_timeout == 120
    assert settings.smtp_host == "127.0.0.1"
    assert settings.smtp_port == 1025
    assert settings.smtp_tls is False


def test_send_environment_configuration_and_private_allowlist_repr():
    settings = Settings.from_env(
        {
            **BASE,
            "PROTON_MCP_ENABLE_SEND": "1",
            "PROTON_MCP_SEND_FROM": "sender@example.test, other@example.test",
            "PROTON_MCP_SEND_MAX_RECIPIENTS": "3",
            "PROTON_MCP_SEND_DAILY_CAP": "4",
            "PROTON_MCP_SEND_TOKEN_TTL": "90",
            "PROTON_MCP_SEND_CONFIRM_TIMEOUT": "20",
            "PROTON_BRIDGE_SMTP_HOST": "::1",
            "PROTON_BRIDGE_SMTP_PORT": "1465",
            "PROTON_BRIDGE_SMTP_TLS": "1",
            "PROTON_BRIDGE_SMTP_ALLOW_UNVERIFIED": "1",
        }
    )
    assert settings.enable_send and settings.smtp_tls and settings.smtp_allow_unverified
    assert settings.send_from_allowlist == ("sender@example.test", "other@example.test")
    assert "other@example.test" not in repr(settings)
    assert (settings.send_max_recipients, settings.send_daily_cap) == (3, 4)
    assert (settings.send_token_ttl, settings.send_confirm_timeout) == (90, 20)
    assert (settings.smtp_host, settings.smtp_port) == ("::1", 1465)


@pytest.mark.parametrize(
    "name",
    ["PROTON_MCP_ENABLE_SEND", "PROTON_BRIDGE_SMTP_TLS", "PROTON_BRIDGE_SMTP_ALLOW_UNVERIFIED"],
)
@pytest.mark.parametrize("value", ["true", "yes", "2", "", " 1"])
def test_send_switches_require_explicit_zero_or_one(name, value):
    with pytest.raises(ConfigError):
        Settings.from_env({**BASE, name: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"smtp_host": "localhost"},
        {"smtp_host": "smtp.example.test"},
        {"smtp_host": "127.0.0.2"},
        {"smtp_port": 0},
        {"send_max_recipients": 0},
        {"send_max_recipients": 51},
        {"send_max_recipients": True},
        {"send_daily_cap": 0},
        {"send_daily_cap": 201},
        {"send_token_ttl": 0},
        {"send_token_ttl": 601},
        {"send_confirm_timeout": 301},
        {"send_confirm_timeout": float("nan")},
        {"enable_send": "1"},
        {"send_from_allowlist": ("Name <sender@example.test>",)},
        {"send_from_allowlist": ("séndér@example.test",)},
        {"send_from_allowlist": ("sender@example..test",)},
        {"send_from_allowlist": ("sender@localhost",)},
        {"send_from_allowlist": ("sender@-example.test",)},
        {"send_from_allowlist": ("sender@example-.test",)},
        {"send_from_allowlist": ("sender@example.-test",)},
        {"send_from_allowlist": ("sender@example.test\r\nBcc: other@example.test",)},
        {"send_from_allowlist": ["sender@example.test"]},
    ],
)
def test_invalid_send_settings_refuse(changes):
    with pytest.raises(ConfigError):
        replace(Settings.from_env(BASE), **changes)
