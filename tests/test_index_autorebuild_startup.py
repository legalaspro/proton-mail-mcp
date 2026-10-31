"""Startup recovery configuration, lifecycle and public status integration."""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from gluon_search import GluonSearchImap
from starlette.testclient import TestClient
from test_mail import FakeImap, client, message
from test_mail import settings as settings
from test_server import TOKEN, FakeMail, rpc

from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.server import create_app

BASE = {"PROTON_MCP_TOKEN": TOKEN, "PROTON_BRIDGE_USER": "bridge@example.test"}


def test_autorebuild_defaults_on_and_has_an_independent_strict_kill_switch():
    default = Settings.from_env(BASE)
    assert default.index_autorebuild_enabled is True
    assert default.background_sync_enabled is False
    assert Settings.from_env({**BASE, "PROTON_MCP_INDEX_AUTOREBUILD_ENABLED": "0"}) == replace(
        default, index_autorebuild_enabled=False
    )
    assert Settings.from_env(
        {**BASE, "PROTON_MCP_INDEX_AUTOREBUILD_ENABLED": "1"}
    ).index_autorebuild_enabled
    with pytest.raises(ConfigError, match="INDEX_AUTOREBUILD_ENABLED"):
        Settings.from_env({**BASE, "PROTON_MCP_INDEX_AUTOREBUILD_ENABLED": "true"})
    with pytest.raises(ConfigError, match="INDEX_AUTOREBUILD_ENABLED"):
        replace(default, index_autorebuild_enabled=1)


def test_recovery_lifecycle_is_app_scoped_and_yields_to_tool_calls():
    settings = replace(Settings.from_env(BASE), confirm_mutations=False)
    fake = FakeMail()
    worker = Mock()
    entries = []

    @contextmanager
    def foreground():
        entries.append("enter")
        try:
            yield
        finally:
            entries.append("exit")

    worker.foreground = foreground
    fake.index_autorebuild = worker
    app = create_app(settings, fake)
    worker.start.assert_not_called()
    with TestClient(app, base_url="http://127.0.0.1") as http:
        worker.start.assert_called_once_with()
        rpc(http, "tools/call", {"name": "reindex_status", "arguments": {}})
        rpc(http, "tools/call", {"name": "search_mail", "arguments": {}})
        # Progress polling is local and cannot starve the batch being observed.
        assert entries == ["enter", "exit"]
        worker.stop.assert_not_called()
    worker.stop.assert_called_once_with()


def test_http_startup_rebuilds_before_any_search_with_periodic_sync_disabled(settings):
    configured = replace(settings, token=TOKEN, confirm_mutations=False)
    fake = GluonSearchImap(
        {
            "All Mail": {
                "20431": message("<old@example.test>"),
                "47744": message("<new@example.test>"),
            }
        },
        uidnext=True,
    )
    reader = client(configured, fake)
    app = create_app(configured, reader)
    assert fake.calls == []
    with TestClient(app, base_url="http://127.0.0.1") as http:
        worker = reader.index_autorebuild
        assert worker._thread is not None
        worker._thread.join(5)
        assert not worker._thread.is_alive()
        status = rpc(http, "tools/call", {"name": "reindex_status", "arguments": {}})["result"][
            "structuredContent"
        ]
        assert status["source"] == "auto" and status["state"] == "succeeded"
        assert status["indexed_count"] == status["all_mail_count"] == 2
        assert status["max_uid"] == "47744"
        assert status["last_catchup_at"] is not None
        assert configured.background_sync_enabled is False
    assert worker._thread is None


@pytest.mark.parametrize("auto_state", ["queued", "running", "succeeded"])
def test_auto_progress_replaces_stale_success_but_preserves_serving_counts(settings, auto_state):
    reader = client(settings, FakeImap())
    index = reader.search_index
    index.observe("All Mail", "1", {"1": {"headers": {"Subject": "Known"}}})
    index.set_all_mail("All Mail")
    index.reconcile("All Mail", "1", ["1", "20431", "47744"])
    reader.reindex._update(
        state="succeeded",
        run_id="older-manual-run",
        old_count=1,
        new_count=1,
        all_mail_count=1,
        swap_outcome="swapped",
        swap_reason="old-result",
    )
    reader.index_autorebuild = SimpleNamespace(
        manual_run_id="older-manual-run",
        status=lambda: {
            "state": auto_state,
            "source": "auto",
            "percent_done": 33.3,
            "last_error": None,
            "eta_seconds": None,
            # A worker snapshot must not replace fresher serving DB counters.
            "indexed_count": 0,
        },
    )
    status = reader.reindex_status()
    active = auto_state in {"queued", "running"}
    assert status["state"] == ("rebuilding" if active else "incomplete")
    assert status.get("source") == ("auto" if active else None)
    assert status["indexed_count"] == 1
    assert status["all_mail_count"] == 3
    assert status["target"] == 3 and status["percent"] == 33.3
    assert status["max_uid"] == status["all_mail_max_uid"] == "47744"
    assert status["last_catchup_at"] is None
    assert status["last_full_run"] == {"old_count": 1, "new_count": 1, "all_mail_count": 1}
    assert status["run_id"] == (None if active else "older-manual-run")
    assert status["swap_outcome"] == ("not_attempted" if active else "swapped")
    assert status["swap_reason"] == (None if active else "old-result")

    reader.reindex._update(state="running", run_id="new-manual-run")
    assert reader.reindex_status()["state"] == "running"
    assert "source" not in reader.reindex_status()
    reader.reindex._update(state="succeeded")
    assert reader.reindex_status().get("source") == ("auto" if active else None)
    assert reader.reindex_status()["state"] == ("rebuilding" if active else "incomplete")
