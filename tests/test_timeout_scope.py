"""Confirmed cleanup and undo respect both call and execution deadlines."""

from dataclasses import replace

import pytest
from test_cleanup_guard import settings as settings
from test_cleanup_guard import setup
from test_cleanup_socket import socket_mailbox as socket_mailbox

from proton_mail_mcp.config import ConfigError, Settings


def test_general_and_execute_timeout_defaults(settings):
    assert settings.call_timeout == 120
    assert settings.execute_timeout == 120
    configured = Settings.from_env(
        {"PROTON_MCP_TOKEN": settings.token, "PROTON_BRIDGE_USER": settings.bridge_user}
    )
    assert configured.call_timeout == 120
    assert configured.execute_timeout == 120


@pytest.mark.parametrize(
    "overrides,general,execute",
    [
        ({"PROTON_MCP_CALL_TIMEOUT": "42.5"}, 42.5, 42.5),
        ({"PROTON_MCP_EXECUTE_TIMEOUT": "90"}, 120, 90),
        ({"PROTON_MCP_CALL_TIMEOUT": "45", "PROTON_MCP_EXECUTE_TIMEOUT": "300"}, 45, 300),
        ({"PROTON_MCP_CALL_TIMEOUT": "90", "PROTON_MCP_EXECUTE_TIMEOUT": "30"}, 90, 30),
    ],
)
def test_global_environment_override_and_execute_specific_precedence(
    settings, overrides, general, execute
):
    configured = Settings.from_env(
        {
            "PROTON_MCP_TOKEN": settings.token,
            "PROTON_BRIDGE_USER": settings.bridge_user,
            **overrides,
        }
    )
    assert configured.call_timeout == general
    assert configured.execute_timeout == execute


@pytest.mark.parametrize("value", [0, -1, 301, float("nan"), float("inf"), True])
def test_invalid_execute_timeout_is_rejected(settings, value):
    with pytest.raises(ConfigError, match="PROTON_MCP_EXECUTE_TIMEOUT"):
        replace(settings, execute_timeout=value)


@pytest.mark.parametrize("value", ["0", "-1", "301", "nan", "inf", "invalid"])
def test_invalid_execute_timeout_environment_is_rejected(settings, value):
    with pytest.raises(ConfigError):
        Settings.from_env(
            {
                "PROTON_MCP_TOKEN": settings.token,
                "PROTON_BRIDGE_USER": settings.bridge_user,
                "PROTON_MCP_EXECUTE_TIMEOUT": value,
            }
        )


@pytest.mark.parametrize("general,execute", [(45, 300), (7, 31), (90, 30)])
@pytest.mark.parametrize(
    "operation", ["search", "read_thread", "draft", "preview", "execute", "label_execute"]
)
def test_mailbox_operations_select_their_own_budget(
    settings, monkeypatch, general, execute, operation
):
    configuration = replace(settings, call_timeout=general, execute_timeout=execute)
    service, _, _ = setup(configuration)
    action = "add_label" if operation == "label_execute" else "move_to_trash"
    params = {"label": "News"} if operation == "label_execute" else {}
    token = service.preview_action(action, query={}, params=params)["token"]
    assert token
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: 100.0)
    deadlines = []

    class SessionStopped(Exception):
        pass

    def capture_session(*, deadline):
        deadlines.append(deadline.end - 100)
        raise SessionStopped

    monkeypatch.setattr(service.client, "_session", capture_session)
    calls = {
        "search": lambda: service.client.search_mail(),
        "read_thread": lambda: service.client.read_thread("<1@example.test>"),
        "draft": lambda: service.client.create_draft(
            "reader@example.test", subject="Synthetic draft", body="Synthetic content"
        ),
        "preview": lambda: service.preview_action("move_to_trash", query={}),
        "execute": lambda: service.execute("move_to_trash", token),
        "label_execute": lambda: service.execute("add_label", token),
    }
    with pytest.raises(SessionStopped):
        calls[operation]()
    from proton_mail_mcp.search import _MAX_SEARCH_SECONDS
    from proton_mail_mcp.threading import MAX_THREAD_SECONDS

    expected = min(general, execute) if operation in {"execute", "label_execute"} else general
    if operation == "read_thread":
        expected = min(expected, MAX_THREAD_SECONDS)
    if operation == "search":
        expected = min(expected, _MAX_SEARCH_SECONDS)
    assert deadlines == [expected]


@pytest.mark.parametrize("general,execute", [(45, 300), (7, 31), (90, 30)])
def test_undo_uses_shorter_call_or_execute_budget(socket_mailbox, monkeypatch, general, execute):
    client, _, _ = socket_mailbox()
    client.settings = replace(client.settings, call_timeout=general, execute_timeout=execute)
    token = client.preview_action("move_to_trash", query={"folder": "INBOX"})["token"]
    result = client.move_to_trash(token)
    assert result["done"] == 2
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: 100.0)
    deadlines = []

    class SessionStopped(Exception):
        pass

    def capture_session(*, deadline):
        deadlines.append(deadline.end - 100)
        raise SessionStopped

    monkeypatch.setattr(client, "_session", capture_session)
    with pytest.raises(SessionStopped):
        client.undo_action(result["log_ids"][0])
    assert deadlines == [min(general, execute)]
