"""Recovery gates and result-scoped progress survive the completeness contract."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_folder_mapping import complete_source
from test_mail import client, message
from test_mail import settings as settings
from test_search_index import CoverageImap

from proton_mail_mcp.mail import CallDeadlineExceeded


@pytest.mark.parametrize("state", ["queued", "running"])
@pytest.mark.parametrize("filtered", [False, True])
def test_active_recovery_keeps_requested_search_without_optional_all_mail(
    settings, monkeypatch, state, filtered
):
    reader, fake = complete_source(settings, count=3)
    reader.index_autorebuild._update(state=state)
    select = fake.select

    def select_requested(folder, **kwargs):
        assert folder == '"INBOX"', "Automatic recovery must own optional All Mail work"
        return select(folder, **kwargs)

    def no_status(*args):
        pytest.fail("Automatic recovery must own optional STATUS work")

    monkeypatch.setattr(fake, "select", select_requested)
    monkeypatch.setattr(fake, "status", no_status)

    result = reader.search_mail(
        include_snippets=False, **({"from_": "studio.example"} if filtered else {})
    )

    assert len(result["messages"]) == result["total"] == 3
    assert result["total_is_exact"] and not result["partial"]
    assert "index_note" not in result and "index_status" not in result


@pytest.mark.parametrize("warm", [False, True], ids=["missing-headers", "complete-index"])
def test_result_timeout_warns_only_when_missing_headers_limit_search(settings, monkeypatch, warm):
    fake = CoverageImap({"INBOX": {"1": message("<hit@example.invalid>")}}, uidnext=True)
    reader = client(settings, fake)
    if warm:
        reader.rebuild_search_index("INBOX")

    def timeout(*args, **kwargs):
        assert kwargs["headers"] is (not warm)
        raise CallDeadlineExceeded("Result metadata budget exhausted")

    monkeypatch.setattr(reader, "_metadata", timeout)
    result = reader.search_mail(subject="Hello", include_snippets=False)

    assert result["partial"] and result["timed_out"] and not result["total_is_exact"]
    assert result["partial_reason"] == "timed_out"
    if warm:
        assert "index_note" not in result and "index_status" not in result
    else:
        assert result["index_status"]["indexed_count"] == 0
        assert result["index_status"]["target"] == 1
        assert "0/1 messages indexed (0%)" in result["index_note"]


def test_exhausted_window_keeps_index_progress_after_cursor_reason_changes(settings, monkeypatch):
    fake = CoverageImap({"All Mail": {"1": message("<old@example.invalid>")}}, uidnext=True)
    reader = client(settings, fake)
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-10-29")

    result = reader.search_mail(folder="All Mail", include_snippets=False)

    assert result["messages"] == [] and result["next_cursor"] is None
    assert result["partial_reason"] == "no_resumable_cursor"
    assert result["index_status"]["indexed_count"] == 0
    assert result["index_status"]["target"] == 1
    assert "0/1 messages indexed (0%)" in result["index_note"]


def test_warmed_window_cursor_omits_incomplete_index_warning(settings, monkeypatch):
    reader, _ = complete_source(settings, count=3)
    reader.settings = replace(settings, search_index_catchup_max_messages=0)
    reader.search_index.remove("All Mail", "1", ["1"])
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-10-01")
    monkeypatch.setattr(
        "proton_mail_mcp.canonical.resolve_views",
        lambda client, connection, deadline, handles, **kwargs: [
            {**handle, "folder": "INBOX"} for handle in handles
        ],
    )
    filters = dict(folder="All Mail", subject="Hello", include_snippets=False, limit=1)
    first = reader.search_mail(**filters)
    assert first["partial"] and first["next_cursor"]
    assert first["partial_reason"] == "date_window"

    # Warming between calls cannot expand a cursor's original date scope.
    assert reader.search_index.record_coverage(
        "All Mail", "1", {"exists": 3, "uidnext": "4"}, ["1", "2", "3"]
    )
    result = reader.search_mail(cursor=first["next_cursor"], **filters)

    assert result["partial"] and not result["total_is_exact"]
    assert result["partial_reason"] == "date_window"
    assert result["applied_since"] == first["applied_since"]
    assert "index_note" not in result and "index_status" not in result


def test_interrupted_catchup_scoped_fallback_keeps_original_window_progress(settings, monkeypatch):
    reader, fake = complete_source(settings, count=1)
    fake.folders["All Mail"]["2"] = message("<uncached@example.invalid>")
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-10-29")
    monkeypatch.setattr(
        "proton_mail_mcp.search_sync.sync_growing_headers",
        lambda *args, **kwargs: SimpleNamespace(
            inventory=None, complete=False, timed_out=True, stream_broken=True
        ),
    )

    result = reader.search_mail(folder="All Mail", include_snippets=False)

    assert sum(call[0] == "LOGIN" for call in fake.calls) == 2
    assert result["messages"] == [] and result["partial"]
    assert result["applied_since"] == "2026-10-29"
    assert result["partial_reason"] == "no_resumable_cursor"
    assert result["index_status"]["indexed_count"] == 1
    assert result["index_status"]["target"] == 2
    assert "1/2 messages indexed (50%)" in result["index_note"]
