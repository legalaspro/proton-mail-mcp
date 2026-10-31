"""Preview aliases, bounded deadlines, and malformed-copy safety guards."""

import copy
import json
from dataclasses import replace

import pytest
from test_cleanup_guard import settings as settings
from test_cleanup_guard import setup

from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.mail import MailAuthenticationError, MailError, MailValidationError


@pytest.mark.parametrize(
    "query",
    [
        {"from": "SENDER@EXAMPLE.COM"},
        {"from": "sender@example.com", "from_": "sender@example.com"},
    ],
)
def test_from_alias_selects_messages_and_stores_canonical_selector(settings, query):
    service, _, backend = setup(settings)
    original = dict(query)
    result = service.preview_action("move_to_trash", query=query)
    assert result["count"] == 1 and result["token"] and result["partial"] is False
    grant = next(iter(service.tokens._grants.values()))
    assert grant.selector == {"query": {"from_": query["from"]}}
    assert query == original
    service.execute("move_to_trash", result["token"])
    assert len(backend.applied) == 1


def test_from_alias_conflict_is_rejected_before_connection(settings):
    service, fake, _ = setup(settings)
    with pytest.raises(MailValidationError, match="from and from_ must agree"):
        service.preview_action(
            "move_to_trash", query={"from": "one@example.invalid", "from_": "other@example.invalid"}
        )
    assert fake.calls == []


def test_unknown_query_fields_are_named_safely_bounded_and_list_supported_fields(settings):
    service, fake, _ = setup(settings)
    bad = 'bad"\\\r\n\u2028' + "x" * 100
    with pytest.raises(MailValidationError) as caught:
        service.preview_action("move_to_trash", query={bad: "ignored", "unsupported": "ignored"})
    error = str(caught.value)
    assert json.dumps(bad[:64] + "...", ensure_ascii=True) in error
    assert '"unsupported"' in error
    assert "x" * 100 not in error
    assert all(ord(character) >= 32 and ord(character) < 127 for character in error)
    supported = error.split("supported fields: ", 1)[1].split(", ")
    assert set(supported) == {"folder", "from", "from_", "to", "subject", "text", "since", "before"}
    assert len(error) < 600
    assert fake.calls == []


def test_unknown_query_field_count_is_bounded(settings):
    service, _, _ = setup(settings)
    with pytest.raises(MailValidationError) as caught:
        service.preview_action("move_to_trash", query={f"bad-{index}": "" for index in range(15)})
    error = str(caught.value)
    assert '"bad-9"' in error and '"bad-10"' not in error
    assert "(and 5 more)" in error


@pytest.mark.parametrize("value", [0, -1, 301, float("nan"), float("inf"), True])
def test_preview_timeout_bounds(settings, value):
    with pytest.raises(ConfigError, match="PROTON_MCP_PREVIEW_TIMEOUT"):
        replace(settings, preview_timeout=value)


def test_preview_timeout_configuration_inherits_call_budget_unless_set(settings):
    env = {
        "PROTON_MCP_TOKEN": settings.token,
        "PROTON_BRIDGE_USER": settings.bridge_user,
        "PROTON_MCP_CALL_TIMEOUT": "12.5",
    }
    inherited = Settings.from_env(env)
    assert inherited.preview_timeout is None and inherited.call_timeout == 12.5
    configured = Settings.from_env({**env, "PROTON_MCP_PREVIEW_TIMEOUT": "7.5"})
    assert configured.preview_timeout == 7.5 and configured.call_timeout == 12.5


@pytest.mark.parametrize("preview_timeout,completed", [(None, True), (1, False)])
def test_preview_budget_may_be_shorter_than_call_budget(
    settings, monkeypatch, preview_timeout, completed
):
    service, _, _ = setup(replace(settings, call_timeout=10, preview_timeout=preview_timeout))
    now = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: now[0])
    original = service._resolve

    def costly_resolution(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] += 1.5
        return result

    monkeypatch.setattr(service, "_resolve", costly_resolution)
    result = service.preview_action("move_to_trash", query={})
    assert bool(result["token"]) is completed
    assert result["partial"] is not completed
    assert service.client._deadline().end == now[0] + 10
    if not completed:
        assert result["expires_at"] is None and not service.tokens._grants
        assert "final approval check" in result["reason"]


@pytest.mark.parametrize("preview_timeout,call_timeout", [(300, 45), (4, 2), (45.1, 45)])
def test_preview_budget_cannot_exceed_general_call_budget(settings, preview_timeout, call_timeout):
    with pytest.raises(ConfigError, match="must not exceed PROTON_MCP_CALL_TIMEOUT"):
        replace(settings, call_timeout=call_timeout, preview_timeout=preview_timeout)
    with pytest.raises(ConfigError, match="must not exceed PROTON_MCP_CALL_TIMEOUT"):
        Settings.from_env(
            {
                "PROTON_MCP_TOKEN": settings.token,
                "PROTON_BRIDGE_USER": settings.bridge_user,
                "PROTON_MCP_CALL_TIMEOUT": str(call_timeout),
                "PROTON_MCP_PREVIEW_TIMEOUT": str(preview_timeout),
            }
        )


def test_expired_final_approval_check_never_issues_token(settings, monkeypatch):
    service, _, _ = setup(replace(settings, call_timeout=1))
    now = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: now[0])

    def late_policy_result(rows, real):
        now[0] += 2
        return []

    monkeypatch.setattr(service, "_blocked", late_policy_result)
    result = service.preview_action("move_to_trash", query={})
    assert result["count"] == 1
    assert result["token"] is None and result["expires_at"] is None
    assert result["partial"] is True and result["total_is_exact"] is False
    assert "final approval check" in result["reason"]
    assert not service.tokens._grants


@pytest.mark.parametrize(
    "preview_budget,execute_budget,elapsed",
    [(10, 120, 8.1), (120, 10, 8.1), (120, 4, 1.1)],
)
def test_preview_refuses_shorter_budget_or_missing_mutation_reserve(
    settings, monkeypatch, preview_budget, execute_budget, elapsed
):
    service, _, backend = setup(
        replace(settings, preview_timeout=preview_budget, execute_timeout=execute_budget)
    )
    now = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: now[0])
    resolve = service._resolve

    def delayed(*args, **kwargs):
        rows = resolve(*args, **kwargs)
        now[0] += elapsed
        return rows

    monkeypatch.setattr(service, "_resolve", delayed)
    result = service.preview_action("move_to_trash", query={})
    assert result["count"] == 1 and not result["partial"]
    assert result["token"] is None and "headroom" in result["reason"]
    assert not service.tokens._grants and not backend.applied


@pytest.mark.parametrize("stage", ["connect", "starttls", "login"])
def test_preview_setup_timeout_returns_staged_partial_without_token(settings, monkeypatch, stage):
    service, fake, _ = setup(settings)

    def timeout(*args, **kwargs):
        raise TimeoutError("Synthetic detail that must not enter the reason")

    if stage == "connect":
        monkeypatch.setattr(service.client, "imap_factory", timeout)
    else:
        monkeypatch.setattr(fake, stage, timeout)
    result = service.preview_action("move_to_trash", query={})
    assert result["partial"] is True and result["total_is_exact"] is False
    assert result["token"] is None and result["expires_at"] is None
    assert result["reason"] == "Preview deadline reached during connection setup; preview again"
    assert not service.tokens._grants


def test_preview_non_timeout_setup_failure_is_not_described_as_deadline(settings, monkeypatch):
    service, fake, _ = setup(settings)

    def unavailable(*args, **kwargs):
        raise MailError("Synthetic login failure")

    monkeypatch.setattr(fake, "login", unavailable)
    with pytest.raises(MailAuthenticationError, match="Mailbox authentication failed"):
        service.preview_action("move_to_trash", query={})


@pytest.mark.parametrize("folder", ["All Mail", "Labels/News"])
@pytest.mark.parametrize("header", ["multiple_ids", "malformed"])
def test_protected_copy_with_malformed_message_id_is_still_blocked(settings, folder, header):
    service, fake, backend = setup(settings)
    protected = copy.deepcopy(fake.folders["INBOX"]["1"])
    protected.headers = protected.headers.replace(
        b"sender@example.com", b"alerts@broker.example.org"
    )
    if header == "multiple_ids":
        protected.headers = protected.headers.replace(
            b"Message-ID: <1@example.test>\r\n",
            b"Message-ID: <other@example.invalid> <1@example.test>\r\n",
        )
    else:
        protected.headers = protected.headers.replace(
            b"Message-ID: <1@example.test>\r\n", b"Message-ID: <1@example.test> trailing-junk\r\n"
        )
    fake.folders[folder]["1"] = protected
    result = service.preview_action("move_to_trash", ids=["<1@example.test>"])
    assert result["count"] == 1 and result["token"] is None
    assert len(result["blocked"]) == 1
    reason = result["blocked"][0]["reason"]
    assert "Protected" in reason
    assert 'Message-ID header during membership in "' + folder + '"' in reason
    assert "<1@example.test>" not in reason and "<other@example.invalid>" not in reason
    assert backend.applied == []
