"""Rebuild warnings describe only results limited by missing indexed headers."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from starlette.testclient import TestClient
from test_mail import client, message
from test_mail import settings as settings
from test_mail_safety import ReadOnlyImap
from test_search_index import CoverageImap
from test_server import TOKEN, rpc

from proton_mail_mcp.mail import CallDeadlineExceeded
from proton_mail_mcp.server import create_app


def assert_no_index_note(result):
    assert "index_note" not in result
    assert "index_status" not in result


@pytest.mark.parametrize("folder", ["All Mail", "INBOX"])
def test_search_warns_for_index_limited_window_or_header_scan(settings, monkeypatch, folder):
    configured = replace(settings, search_index_catchup_max_messages=0)
    fake = CoverageImap(
        {folder: {str(uid): message(f"<old-{uid}@example.test>") for uid in (1, 2)}},
        uidnext=True,
    )
    reader = client(configured, fake)
    monkeypatch.setattr("proton_mail_mcp.search._MAX_SEARCH_CANDIDATES", 1)
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-10-01")
    monkeypatch.setattr(
        "proton_mail_mcp.canonical.resolve_views",
        lambda client, connection, deadline, handles, **kwargs: [
            {**handle, "folder": "INBOX"} for handle in handles
        ],
    )

    result = reader.search_mail(folder=folder, subject="Hello", include_snippets=False)

    assert result["partial"] is True
    assert result["total_is_exact"] is False
    assert "search index incomplete: 1/2 messages indexed (50%)" in result["index_note"]
    assert "results may be incomplete" in result["index_note"]
    assert result["index_status"] == {
        "state": "incomplete",
        "indexed_count": 1,
        "target": 2,
        "percent": 50.0,
        "eta_seconds": None,
    }


def test_complete_scoped_search_omits_warning_despite_incomplete_account_index(settings):
    configured = replace(settings, search_index_catchup_max_messages=0)
    fake = CoverageImap({"INBOX": {"1": message("<inbox@example.test>")}}, uidnext=True)
    result = client(configured, fake).search_mail(
        folder="INBOX", since="2026-10-01", subject="Hello", include_snippets=False
    )
    assert result["total"] == 1 and result["partial"] is False
    assert_no_index_note(result)


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("filtered", [False, True])
def test_exact_imap_count_omits_warning_regardless_of_index(settings, complete, filtered):
    fake = CoverageImap(
        {"All Mail": {str(uid): message(f"<message-{uid}@example.test>") for uid in (1, 2)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    reader.search_index.observe("All Mail", "1", {"1": {"headers": {"Subject": "Hello"}}})
    reader.search_index.set_all_mail("All Mail")
    reader.search_index.reconcile("All Mail", "1", ["1", "2"])
    if complete:
        reader.rebuild_search_index("All Mail")
    fake.calls.clear()

    result = reader.count_mail(folder="All Mail", **({"subject": "Hello"} if filtered else {}))

    assert result == {"folder": "All Mail", "count": 2}
    assert_no_index_note(result)
    assert not any(call[0] == "FETCH" for call in fake.calls)


@pytest.mark.parametrize("complete", [False, True])
def test_limited_seed_lookup_warns_only_when_headers_are_incomplete(
    settings, monkeypatch, complete
):
    fake = ReadOnlyImap(
        {"All Mail": {str(uid): message(f"<message-{uid}@example.test>") for uid in (1, 2)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    index = reader.search_index
    index.observe("All Mail", "1", {"1": {"headers": {"Subject": "Known"}}})
    index.set_all_mail("All Mail")
    index.reconcile("All Mail", "1", ["1", "2"])
    if complete:
        reader.rebuild_search_index("All Mail")

    def limited_lookup(connection, criteria, **kwargs):
        assert "HEADER" in criteria
        raise CallDeadlineExceeded("lookup budget exhausted")

    monkeypatch.setattr(reader, "_search", limited_lookup)

    result = reader.read_thread("<missing@example.test>", include_thread=False)

    assert result["messages"] == [] and result["partial"] is True
    assert "lookup incomplete" in result["partial_reason"]
    if complete:
        assert_no_index_note(result)
    else:
        assert "1/2 messages indexed (50%)" in result["index_note"]
        assert result["index_status"]["state"] == "incomplete"
        assert result["index_status"]["target"] == 2


def test_successful_cold_live_read_omits_index_warning(settings):
    fake = ReadOnlyImap({"INBOX": {"1": message("<seed@example.test>")}}, uidnext=True)
    result = client(settings, fake).read_thread("<seed@example.test>", include_thread=False)
    assert result["partial"] is False and len(result["messages"]) == 1
    assert_no_index_note(result)


@pytest.mark.parametrize("current_epoch", ["1", "2"])
def test_limited_read_uses_current_inventory_and_epoch(settings, monkeypatch, current_epoch):
    fake = ReadOnlyImap(
        {"All Mail": {str(uid): message(f"<message-{uid}@example.test>") for uid in (1, 2)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    index = reader.search_index
    index.observe("All Mail", "1", {"1": {"headers": {"Subject": "Known"}}})
    index.set_all_mail("All Mail")
    # The saved inventory is complete, but the request's live inventory has two
    # messages and may belong to a different UIDVALIDITY.
    index.reconcile("All Mail", "1", ["1"])
    fake.uidvalidity = current_epoch

    def limited_lookup(connection, criteria, **kwargs):
        raise CallDeadlineExceeded("lookup budget exhausted")

    monkeypatch.setattr(reader, "_search", limited_lookup)
    result = reader.read_thread("<missing@example.test>", include_thread=False)

    assert result["partial"] is True
    assert result["index_status"]["target"] == 2
    assert result["index_status"]["indexed_count"] == (1 if current_epoch == "1" else 0)
    assert result["index_status"]["percent"] == (50.0 if current_epoch == "1" else 0.0)


def test_progress_count_does_not_initialize_or_wait_for_index_lock(settings):
    reader = client(settings, ReadOnlyImap())
    index = reader.search_index
    assert index.reporting_count("All Mail", "1") is None
    assert not index.path.exists()
    index.observe("All Mail", "1", {"1": {"headers": {"Subject": "Known"}}})
    assert index.reporting_count("All Mail", "1") == 1
    assert index.reporting_count("All Mail", "2") == 0
    with ThreadPoolExecutor(max_workers=1) as workers:
        with index._coverage_lock:
            assert workers.submit(index.reporting_count, "All Mail", "1").result(timeout=1) is None


def test_mcp_search_reply_preserves_human_note_and_structured_status(settings, monkeypatch):
    configured = replace(
        settings,
        token=TOKEN,
        confirm_mutations=False,
        index_autorebuild_enabled=False,
        search_index_catchup_max_messages=0,
    )
    fake = CoverageImap({"All Mail": {"1": message("<old@example.test>")}}, uidnext=True)
    reader = client(configured, fake)
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-10-29")
    with TestClient(create_app(configured, reader), base_url="http://127.0.0.1") as http:
        reply = rpc(
            http,
            "tools/call",
            {"name": "search_mail", "arguments": {"folder": "All Mail", "include_snippets": False}},
        )["result"]
    assert reply["structuredContent"]["index_status"]["target"] == 1
    assert reply["structuredContent"]["index_note"].startswith(
        "search index incomplete (automatic recovery disabled):"
    )
    assert any("automatic recovery disabled" in block.get("text", "") for block in reply["content"])


@pytest.mark.parametrize("old_state", ["idle", "succeeded"])
def test_kill_switch_with_empty_cache_reports_disabled_without_imap(settings, old_state):
    configured = replace(
        settings, token=TOKEN, confirm_mutations=False, index_autorebuild_enabled=False
    )
    fake = ReadOnlyImap()
    reader = client(configured, fake)
    reader.reindex._update(state=old_state, run_id="old-run" if old_state == "succeeded" else None)
    with TestClient(create_app(configured, reader), base_url="http://127.0.0.1") as http:
        reply = rpc(http, "tools/call", {"name": "reindex_status", "arguments": {}})["result"]
    result = reply["structuredContent"]
    assert result["state"] == "disabled"
    assert result["target"] is None and result["eta_seconds"] is None
    assert reader.index_autorebuild._thread is None
    assert fake.calls == []
    assert not reader.search_index.path.exists()
