from dataclasses import replace
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient
from test_server import FakeMail

from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.server import create_app

BASE = {"PROTON_MCP_TOKEN": "x" * 32, "PROTON_BRIDGE_USER": "watcher@example.test"}


def test_idle_configuration_defaults_and_json_folder_names():
    defaults = Settings.from_env(BASE)
    assert defaults.imap_idle_enabled is False
    assert defaults.imap_idle_folders == ("INBOX",)
    assert defaults.imap_idle_poll_interval == 60
    configured = Settings.from_env(
        {
            **BASE,
            "PROTON_MCP_IMAP_IDLE_ENABLED": "1",
            "PROTON_MCP_IMAP_IDLE_FOLDERS": '["INBOX", "Folders/Factures, été"]',
            "PROTON_MCP_IMAP_IDLE_POLL_INTERVAL": "90",
        }
    )
    assert configured.imap_idle_enabled is True
    assert configured.imap_idle_folders == ("INBOX", "Folders/Factures, été")
    assert configured.imap_idle_poll_interval == 90
    for name, value in (
        ("PROTON_MCP_IMAP_IDLE_ENABLED", "true"),
        ("PROTON_MCP_IMAP_IDLE_FOLDERS", "INBOX,Archive"),
        ("PROTON_MCP_IMAP_IDLE_FOLDERS", '"INBOX"'),
        ("PROTON_MCP_IMAP_IDLE_POLL_INTERVAL", "NaN"),
    ):
        with pytest.raises(ConfigError):
            Settings.from_env({**BASE, name: value})


@pytest.mark.parametrize(
    "change",
    [
        {"imap_idle_enabled": 1},
        {"imap_idle_folders": []},
        {"imap_idle_folders": ()},
        {"imap_idle_folders": tuple(f"Folders/{n}" for n in range(9))},
        {"imap_idle_folders": ("INBOX", "inbox")},
        {"imap_idle_folders": ("Archive", "Archive")},
        {"imap_idle_folders": ("",)},
        {"imap_idle_folders": (None,)},
        {"imap_idle_folders": ("INBOX\r\nLOGOUT",)},
        {"imap_idle_folders": ("Folders/\ud800",)},
        {"imap_idle_poll_interval": True},
        {"imap_idle_poll_interval": 29},
        {"imap_idle_poll_interval": 301},
    ],
)
def test_invalid_idle_settings_refuse(change):
    with pytest.raises(ConfigError):
        replace(Settings.from_env(BASE), **change)


def test_idle_lifecycle_is_app_scoped(monkeypatch):
    from test_server import TOKEN

    settings = Settings(
        token=TOKEN,
        bridge_user="watcher@example.test",
        imap_idle_enabled=True,
        confirm_mutations=False,
    )
    mail = FakeMail()
    watcher = Mock()
    factory = Mock(return_value=watcher)
    monkeypatch.setattr("proton_mail_mcp.server.MailWatcher", factory)
    app = create_app(settings, mail)
    factory.assert_called_once_with(mail, settings)
    watcher.start.assert_not_called()
    with TestClient(app, base_url="http://127.0.0.1"):
        watcher.start.assert_called_once_with()
        watcher.stop.assert_not_called()
    watcher.stop.assert_called_once_with()
    assert mail.calls == []
