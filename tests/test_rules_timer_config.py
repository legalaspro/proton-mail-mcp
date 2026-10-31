"""The unattended move limit stays bounded through environment configuration."""

import pytest
from test_rules_timer import arrive, mark
from test_rules_timer import timer_case as timer_case

from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.guarded import MAX_BATCH


def settings(value=None):
    env = {"PROTON_MCP_TOKEN": "x" * 32, "PROTON_BRIDGE_USER": "reader@example.test"}
    if value is not None:
        env["PROTON_MCP_RULES_MAX_MOVES"] = value
    return Settings.from_env(env)


def test_timer_default_and_maximum():
    assert settings().rules_max_moves == 50
    assert settings(str(MAX_BATCH)).rules_max_moves == MAX_BATCH


@pytest.mark.parametrize("value", ["0", "-1", str(MAX_BATCH + 1), "true", "1.5"])
def test_invalid_environment_cap_fails_closed(value):
    with pytest.raises(ConfigError):
        settings(value)


def test_runner_honors_configured_default_cap(timer_case):
    from dataclasses import replace

    case = timer_case(count=0)
    case.cleanup.settings = replace(case.cleanup.settings, rules_max_moves=1)
    case.timer.run()
    arrive(case, 1)
    arrive(case, 2)
    assert case.timer.run()["count"] == 1 and mark(case)["uid"] == 1
    assert case.timer.run()["count"] == 1 and mark(case)["uid"] == 2
