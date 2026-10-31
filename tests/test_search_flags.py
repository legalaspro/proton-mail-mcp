"""One completeness contract for indexed and fallback results, entirely offline."""

from dataclasses import replace

import pytest
from test_folder_mapping import complete_source
from test_header_search_index import indexed_message
from test_mail import client
from test_mail import settings as settings
from test_search_index import CoverageImap
from test_search_index_followup import warm_real

from proton_mail_mcp.mail import CallDeadlineExceeded


def assert_completeness(result, reason=None):
    assert result["partial"] is (reason is not None)
    assert result.get("partial_reason") == reason
    assert not {"search_index", "search_index_details", "retryable"} & result.keys()


def test_inbox_sender_pages_have_one_completeness_signal_after_warming(settings):
    fake = CoverageImap(
        {"INBOX": {str(uid): indexed_message(f"<{uid}@example.invalid>") for uid in range(1, 4)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    filters = dict(from_="studio.example", include_snippets=False, limit=1)

    first = reader.search_mail(**filters)
    assert first["next_cursor"]
    second = reader.search_mail(cursor=first["next_cursor"], **filters)

    assert [item["uid"] for item in first["messages"]] == ["3"]
    assert [item["uid"] for item in second["messages"]] == ["2"]
    for result in (first, second):
        assert result["total"] == 3 and result["total_is_exact"] and not result["partial"]
        assert_completeness(result)


@pytest.mark.parametrize("scoped", [False, True])
def test_exact_index_result_has_no_account_freshness_fields(settings, scoped):
    fake = CoverageImap({"INBOX": {"1": indexed_message("<hit@example.invalid>")}}, uidnext=True)
    reader = client(settings, fake)
    reader.rebuild_search_index("INBOX")

    result = reader.search_mail(
        from_="studio.example",
        include_snippets=False,
        **({"since": "2026-10-01"} if scoped else {}),
    )

    assert result["total"] == 1 and result["total_is_exact"] and not result["partial"]
    assert result["messages"][0]["uid"] == "1"
    assert_completeness(result)


def test_exact_index_cursor_ignores_optional_timeouts_outside_its_snapshot(settings, monkeypatch):
    reader, fake = complete_source(settings, count=2)
    warm_real(reader, fake)
    filters = dict(from_="studio.example", include_snippets=False, limit=1)
    first = reader.search_mail(**filters)
    assert first["next_cursor"]
    arrival = indexed_message("<arrival@example.invalid>")
    fake.folders["All Mail"]["3"] = arrival
    fake.folders["INBOX"]["4003"] = arrival
    monkeypatch.setattr("proton_mail_mcp.search_sync.has_time", lambda *_: False)

    result = reader.search_mail(cursor=first["next_cursor"], **filters)

    assert result["total"] == 2 and result["total_is_exact"] and not result["partial"]
    assert [item["uid"] for item in result["messages"]] == ["4001"]
    assert_completeness(result)


@pytest.mark.parametrize("timed_out", [False, True])
def test_partial_fallback_reports_candidate_cap_despite_optional_budget(
    settings, monkeypatch, timed_out
):
    settings = replace(settings, timeout=1, call_timeout=60)
    reader, fake = complete_source(settings, count=5)
    monkeypatch.setattr("proton_mail_mcp.search._MAX_SEARCH_CANDIDATES", 3)
    # With warming disabled there is a cap, but no timeout. A short optional
    # budget prevents the same mapping and causes the same capped result.
    if timed_out:
        monkeypatch.setattr("proton_mail_mcp.search_sync.has_time", lambda *_: False)
    else:
        reader.settings = replace(settings, search_index_catchup_max_messages=0)

    result = reader.search_mail(from_="studio.example", include_snippets=False)

    assert result["partial"] and not result["total_is_exact"]
    assert result["total"] == 3 and result["next_cursor"]
    assert len(result["messages"]) == 3
    assert_completeness(result, "candidate_cap")
    assert not result.get("timed_out")


@pytest.mark.parametrize("failure", [CallDeadlineExceeded, OSError])
def test_interrupted_mapping_keeps_resumable_capped_fallback(settings, monkeypatch, failure):
    settings = replace(settings, timeout=1, call_timeout=60)
    reader, fake = complete_source(settings, count=5, missing_metadata=True)
    monkeypatch.setattr("proton_mail_mcp.search._MAX_SEARCH_CANDIDATES", 3)
    metadata = reader._metadata
    interrupted = []

    def fail_mapping(connection, uids, **kwargs):
        if fake.current == "All Mail" and kwargs.get("envelope"):
            interrupted.append(connection)
            raise failure("Simulated mapping interruption")
        assert connection not in interrupted
        return metadata(connection, uids, **kwargs)

    monkeypatch.setattr(reader, "_metadata", fail_mapping)

    result = reader.search_mail(from_="studio.example", include_snippets=False)

    assert len(interrupted) == 1
    assert result["partial"] and not result["total_is_exact"]
    assert result["total"] == 3 and result["next_cursor"]
    assert_completeness(result, "candidate_cap")


def test_index_page_timeout_reports_single_completeness_reason(settings, monkeypatch):
    fake = CoverageImap({"INBOX": {"1": indexed_message("<hit@example.invalid>")}}, uidnext=True)
    reader = client(settings, fake)
    reader.rebuild_search_index("INBOX")

    def fail_metadata(*args, **kwargs):
        raise CallDeadlineExceeded("Simulated result metadata timeout")

    monkeypatch.setattr(reader, "_metadata", fail_metadata)

    result = reader.search_mail(from_="studio.example", include_snippets=False)

    assert result["partial"] and result["timed_out"] and not result["total_is_exact"]
    assert result["messages"] == []
    assert_completeness(result, "timed_out")


def test_account_sync_timeout_preserves_resumable_capped_fallback(settings, monkeypatch):
    settings = replace(settings, timeout=1, call_timeout=60)
    reader, fake = complete_source(settings, count=5)
    monkeypatch.setattr("proton_mail_mcp.search._MAX_SEARCH_CANDIDATES", 3)
    status = fake.status
    interrupted = []

    def fail_once(*args):
        if not interrupted:
            interrupted.append(True)
            raise CallDeadlineExceeded("Simulated account sync timeout")
        return status(*args)

    monkeypatch.setattr(fake, "status", fail_once)

    result = reader.search_mail(from_="studio.example", include_snippets=False)

    assert result["partial"] and not result["total_is_exact"] and result["total"] == 3
    assert_completeness(result, "candidate_cap")
    assert result["next_cursor"]


def test_warmed_index_keeps_window_cursor_completeness_reason(settings):
    reader, fake = complete_source(settings, count=3)
    reader.settings = replace(settings, search_index_catchup_max_messages=0)
    reader.search_index.remove("All Mail", "1", ["1"])
    filters = dict(folder="All Mail", from_="studio.example", include_snippets=False, limit=1)
    first = reader.search_mail(**filters)
    assert first["partial"] and first["next_cursor"]
    assert_completeness(first, "date_window")
    assert reader.search_index.record_coverage(
        "All Mail", "1", {"exists": 3, "uidnext": "4"}, ["1", "2", "3"]
    )

    result = reader.search_mail(cursor=first["next_cursor"], **filters)

    assert result["partial"] and not result["total_is_exact"]
    assert result["applied_since"] == first["applied_since"]
    assert_completeness(result, "date_window")


def test_window_search_timeout_reports_single_completeness_reason(settings, monkeypatch):
    reader, fake = complete_source(settings, count=2)
    fake.folders["All Mail"]["3"] = indexed_message("<arrival@example.invalid>")
    monkeypatch.setattr("proton_mail_mcp.search_sync.has_time", lambda *_: False)

    def fail_search(*args, **kwargs):
        raise CallDeadlineExceeded("Simulated window search timeout")

    monkeypatch.setattr(reader, "_search", fail_search)

    result = reader.search_mail(folder="All Mail", from_="studio.example", include_snippets=False)

    assert result["partial"] and result["timed_out"] and not result["total_is_exact"]
    assert result["applied_since"]
    assert_completeness(result, "timed_out")


def test_optional_maintenance_timeout_preserves_candidate_cap_reason(settings, monkeypatch):
    reader, fake = complete_source(settings, count=5)
    reader.settings = replace(settings, search_index_catchup_max_messages=0)
    monkeypatch.setattr("proton_mail_mcp.search._MAX_SEARCH_CANDIDATES", 3)
    status = fake.status

    def fail_after_scan(*args):
        if any(call[0] == "FETCH" and "HEADER.FIELDS" in call[2] for call in fake.calls):
            raise CallDeadlineExceeded("Simulated optional maintenance timeout")
        return status(*args)

    monkeypatch.setattr(fake, "status", fail_after_scan)

    result = reader.search_mail(from_="studio.example", include_snippets=False)

    assert result["partial"] and not result["total_is_exact"]
    assert result["total"] == 3
    assert_completeness(result, "candidate_cap")
    assert not result.get("timed_out")
