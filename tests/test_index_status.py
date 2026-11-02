"""Incomplete-index annotations and locally observed rebuild progress."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_mail import FakeImap, client
from test_mail import settings as settings

from proton_mail_mcp.index_autorebuild import _Work
from proton_mail_mcp.index_status import RebuildRate, attach_index_status, progress_fields


@pytest.mark.parametrize(
    "indexed,target,percent,eta",
    [(1, 3, 33.3, None), (0, 0, 100.0, 0.0), (4, 3, 100.0, 0.0), (2, None, None, None)],
)
def test_progress_percent_and_unknown_rate(indexed, target, percent, eta):
    assert progress_fields(indexed, target) == {
        "indexed_count": indexed,
        "target": target,
        "percent": percent,
        "eta_seconds": eta,
    }


def test_eta_uses_new_progress_instead_of_already_cached_headers():
    rate = RebuildRate()
    assert rate.observe(900, 1000, 10.0)["eta_seconds"] is None
    assert rate.observe(900, 1000, 15.0)["eta_seconds"] is None
    progress = rate.observe(910, 1000, 20.0)
    assert progress["percent"] == 91.0
    assert progress["eta_seconds"] == 90.0
    assert rate.observe(1000, 1000, 30.0)["eta_seconds"] == 0.0
    # A replaced or reconciled inventory starts a fresh rate observation.
    assert rate.observe(800, 1000, 40.0)["eta_seconds"] is None


def test_eta_needs_elapsed_time_and_a_known_target():
    rate = RebuildRate()
    assert rate.observe(1, 10, 5.0)["eta_seconds"] is None
    assert rate.observe(2, 10, 5.0)["eta_seconds"] is None
    assert rate.observe(3, None, 6.0)["eta_seconds"] is None
    assert rate.observe(4, 10, 8.0)["eta_seconds"] is None
    assert rate.observe(5, 10, 10.0)["eta_seconds"] == 10.0
    assert RebuildRate().observe(0, 0, 1.0)["eta_seconds"] == 0.0


def status_client(counts=None, auto=None):
    return SimpleNamespace(
        search_index=SimpleNamespace(reporting_status=lambda: counts),
        index_autorebuild=SimpleNamespace(status=lambda: auto or {}),
    )


def test_shared_note_and_fields_use_local_inventory_and_worker_eta():
    reader = status_client(
        {"indexed_count": 2, "all_mail_count": 8, "all_mail_max_uid": "47744"},
        {"state": "running", "eta_seconds": 12.0},
    )
    result = {"messages": []}
    assert attach_index_status(reader, result, depends_on_index=True, incomplete=True) is result
    assert result["index_status"] == {
        "state": "rebuilding",
        "indexed_count": 2,
        "target": 8,
        "percent": 25.0,
        "eta_seconds": 12.0,
    }
    assert result["index_note"] == (
        "search index rebuilding: 2/8 messages indexed (25%); results may be incomplete"
    )


@pytest.mark.parametrize("depends,incomplete", [(False, True), (True, False), (False, False)])
def test_complete_or_index_independent_reply_does_not_access_status(depends, incomplete):
    result = {"count": 3}
    # Status reads must be skipped entirely on exact IMAP replies and complete results.
    assert attach_index_status(
        object(), result, depends_on_index=depends, incomplete=incomplete
    ) == {"count": 3}


def test_unknown_inventory_and_folder_specific_progress_are_explicit():
    reader = status_client()
    result = attach_index_status(reader, {}, depends_on_index=True, incomplete=True)
    assert result["index_status"]["target"] is None
    assert result["index_status"]["percent"] is None
    assert result["index_status"]["eta_seconds"] is None
    assert "target unknown" in result["index_note"]
    result = attach_index_status(
        reader, {}, depends_on_index=True, incomplete=True, indexed_count=1, target=4
    )
    assert result["index_status"]["target"] == 4
    assert result["index_status"]["percent"] == 25.0


def test_folder_progress_never_initializes_or_reads_the_account_index():
    reader = SimpleNamespace(
        search_index=object(),
        index_autorebuild=SimpleNamespace(status=lambda: {}),
    )
    result = attach_index_status(
        reader, {}, depends_on_index=True, incomplete=True, indexed_count=2, target=5
    )
    assert result["index_status"]["indexed_count"] == 2
    assert result["index_status"]["target"] == 5
    assert result["index_status"]["percent"] == 40.0


def test_auto_worker_reports_observed_rate_after_resuming_partial_cache(settings, monkeypatch):
    reader = client(settings, FakeImap())
    worker = reader.index_autorebuild
    worker._all_mail = "All Mail"
    worker._total = 1000
    members = [str(uid) for uid in range(1, 1001)]
    work = _Work("All Mail", "1", members, members[900:], 0)
    monkeypatch.setattr(reader.search_index, "reporting_status", lambda: None)
    times = iter([100.0, 110.0])
    monkeypatch.setattr("proton_mail_mcp.index_autorebuild.time.monotonic", lambda: next(times))
    worker._progress(work)
    assert worker.status()["indexed_count"] == 900
    assert worker.status()["eta_seconds"] is None
    del work.pending[:10]
    worker._progress(work)
    assert worker.status()["target"] == 1000
    assert worker.status()["percent"] == 91.0
    assert worker.status()["eta_seconds"] == 90.0


@pytest.mark.parametrize("auto_state", ["queued", "running", "failed", "succeeded"])
def test_public_status_reports_activity_and_never_keeps_stale_success(
    settings, monkeypatch, auto_state
):
    fake = FakeImap()
    reader = client(settings, fake)
    reader.reindex._update(state="succeeded", run_id="old-run")
    monkeypatch.setattr(
        reader.search_index,
        "reporting_status",
        lambda: {"indexed_count": 1, "all_mail_count": 4},
    )
    reader.index_autorebuild.manual_run_id = "old-run"
    reader.index_autorebuild._update(state=auto_state, target=4, eta_seconds=9.0)
    result = reader.reindex_status()
    assert (
        result["state"]
        == {
            "queued": "rebuilding",
            "running": "rebuilding",
            "failed": "failed",
            "succeeded": "incomplete",
        }[auto_state]
    )
    assert result["indexed_count"] == 1
    assert result["target"] == 4
    assert result["percent"] == 25.0
    assert result["eta_seconds"] == (9.0 if auto_state in {"queued", "running"} else None)
    assert fake.calls == []


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("manual_state", ["idle", "succeeded"])
def test_inactive_incomplete_cache_never_claims_rebuilding_or_stale_success(
    settings, monkeypatch, enabled, manual_state
):
    reader = client(replace(settings, index_autorebuild_enabled=enabled), FakeImap())
    reader.reindex._update(state=manual_state, run_id="old-run")
    monkeypatch.setattr(
        reader.search_index,
        "reporting_status",
        lambda: {"indexed_count": 3, "all_mail_count": 4},
    )
    result = reader.reindex_status()
    assert result["state"] == ("incomplete" if enabled else "disabled")
    assert result["target"] == 4 and result["percent"] == 75.0
    assert result["eta_seconds"] is None
    assert result["last_full_run"] is not None


@pytest.mark.parametrize("auto_state", ["idle", "disabled", "failed", "partial", "succeeded"])
@pytest.mark.parametrize("manual_state", ["failed", "cancelled"])
@pytest.mark.parametrize("counts", [None, {"indexed_count": 3, "all_mail_count": 4}])
def test_inactive_auto_preserves_manual_terminal_state_and_run_details(
    settings, monkeypatch, auto_state, manual_state, counts
):
    reader = client(settings, FakeImap())
    reader.reindex._update(
        state=manual_state,
        run_id="manual-run",
        last_error="coverage_below_99_percent" if manual_state == "failed" else None,
        swap_outcome="kept_old",
        swap_reason="cancelled" if manual_state == "cancelled" else "coverage_below_99_percent",
        old_count=3,
        new_count=2,
        all_mail_count=4,
    )
    monkeypatch.setattr(
        reader.search_index,
        "reporting_status",
        lambda: counts,
    )
    # The automatic worker saw this same manual run, rather than an older one.
    reader.index_autorebuild.manual_run_id = "manual-run"
    reader.index_autorebuild._update(state=auto_state, last_error="authentication_failed")
    expected = reader.reindex.status()
    assert reader.reindex_status() == expected
    assert expected["last_full_run"] == {"old_count": 3, "new_count": 2, "all_mail_count": 4}


@pytest.mark.parametrize("auto_state", ["queued", "running"])
@pytest.mark.parametrize("manual_state", ["failed", "cancelled"])
def test_actual_auto_activity_temporarily_takes_precedence_over_manual_result(
    settings, monkeypatch, auto_state, manual_state
):
    reader = client(settings, FakeImap())
    reader.reindex._update(state=manual_state, run_id="manual-run", swap_outcome="kept_old")
    monkeypatch.setattr(
        reader.search_index,
        "reporting_status",
        lambda: {"indexed_count": 3, "all_mail_count": 4},
    )
    reader.index_autorebuild._update(state=auto_state, target=4, eta_seconds=9.0)
    result = reader.reindex_status()
    assert result["state"] == "rebuilding"
    assert result["source"] == "auto"
    assert result["run_id"] is None and result["swap_outcome"] == "not_attempted"
    assert result["last_full_run"] == reader.reindex.status()["last_full_run"]
    reader.index_autorebuild._update(state="succeeded")
    assert reader.reindex_status()["state"] == manual_state
    assert reader.reindex_status()["run_id"] == "manual-run"


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("available", [False, True])
def test_unknown_or_discarded_cache_never_repeats_historical_success(
    settings, monkeypatch, enabled, available
):
    fake = FakeImap()
    reader = client(replace(settings, index_autorebuild_enabled=enabled), fake)
    reader.reindex._update(state="succeeded", run_id="old-run", swap_outcome="swapped")
    reader.search_index.enabled = available
    monkeypatch.setattr(reader.search_index, "reporting_status", lambda: None)
    result = reader.reindex_status()
    assert result["state"] == (
        "unavailable" if not available else "incomplete" if enabled else "disabled"
    )
    assert result["run_id"] == "old-run" and result["swap_outcome"] == "swapped"
    assert result["indexed_count"] is None and result["target"] is None
    assert result["eta_seconds"] is None
    assert result["last_full_run"] is not None
    assert fake.calls == []


@pytest.mark.parametrize("auto_state", ["idle", "disabled", "succeeded"])
def test_inactive_status_has_no_eta_even_if_serving_counts_advance(
    settings, monkeypatch, auto_state
):
    fake = FakeImap()
    reader = client(settings, fake)
    reader.reindex._update(state="succeeded", run_id="old-run")
    reader.index_autorebuild._update(state=auto_state, target=1000, eta_seconds=0.0)
    indexed = [900]
    monkeypatch.setattr(
        reader.search_index,
        "reporting_status",
        lambda: {"indexed_count": indexed[0], "all_mail_count": 1000},
    )
    assert reader.reindex_status()["eta_seconds"] is None
    indexed[0] = 910
    result = reader.reindex_status()
    assert result["state"] == ("disabled" if auto_state == "disabled" else "incomplete")
    assert result["indexed_count"] == 910 and result["target"] == 1000
    assert result["percent"] == 91.0 and result["eta_seconds"] is None
    assert fake.calls == []


@pytest.mark.parametrize("target,percent,eta", [(None, None, None), (0, 100.0, None)])
def test_public_queued_status_handles_unknown_and_zero_target(
    settings, monkeypatch, target, percent, eta
):
    reader = client(settings, FakeImap())
    monkeypatch.setattr(
        reader.search_index,
        "reporting_status",
        lambda: {"indexed_count": 0, "all_mail_count": target},
    )
    reader.index_autorebuild._update(state="queued", target=target)
    result = reader.reindex_status()
    assert result["state"] == "rebuilding"
    assert result["indexed_count"] == 0 and result["target"] == target
    assert result["percent"] == percent and result["eta_seconds"] == eta


@pytest.mark.parametrize("indexed,target", [(47519, 47520), (999, 1000)])
def test_incomplete_coverage_never_rounds_up_to_100_percent(indexed, target):
    assert progress_fields(indexed, target)["percent"] == 99.9


@pytest.mark.parametrize("indexed,target,eta", [(1000, 1000, 0.0), (999, 1000, 0.0), (0, 0, 0.0)])
def test_active_progress_never_promises_zero_eta(indexed, target, eta):
    assert progress_fields(indexed, target, eta_seconds=eta, active=True)["eta_seconds"] is None


@pytest.mark.parametrize("state", ["idle", "disabled", "failed", "succeeded"])
def test_incomplete_result_warnings_only_claim_activity_while_it_exists(state):
    reader = status_client(
        {"indexed_count": 1, "all_mail_count": 4},
        {"state": state, "target": 4, "eta_seconds": 0.0},
    )
    result = attach_index_status(reader, {}, depends_on_index=True, incomplete=True)
    assert result["index_status"]["state"] == (
        state if state in {"disabled", "failed"} else "incomplete"
    )
    assert result["index_status"]["eta_seconds"] is None
    assert "rebuilding" not in result["index_note"]


@pytest.mark.parametrize("state", ["queued", "running"])
def test_bridge_sync_says_why_the_index_lags_and_that_it_catches_up(state):
    reader = status_client(
        {"indexed_count": 2, "all_mail_count": 8},
        {"state": state, "last_error": "bridge_syncing"},
    )
    result = attach_index_status(reader, {}, depends_on_index=True, incomplete=True)
    assert result["index_status"]["state"] == "rebuilding"
    assert result["index_status"]["last_error"] == "bridge_syncing"
    assert result["index_note"] == (
        "search index waiting for Bridge, which is still downloading the mailbox (most likely "
        "its first sync after a login); the index catches up by itself once Bridge finishes: "
        "2/8 messages indexed (25%); results may be incomplete"
    )


def test_other_retry_reasons_while_rebuilding_keep_the_plain_note():
    reader = status_client(
        {"indexed_count": 2, "all_mail_count": 8},
        {"state": "queued", "last_error": "rebuild_failed"},
    )
    result = attach_index_status(reader, {}, depends_on_index=True, incomplete=True)
    assert "last_error" not in result["index_status"]
    assert result["index_note"].startswith("search index rebuilding: ")


def test_public_status_reports_bridge_sync_while_rebuilding(settings):
    reader = client(settings, FakeImap())
    reader.index_autorebuild._update(state="queued", last_error="bridge_syncing")
    result = reader.reindex_status()
    assert result["state"] == "rebuilding"
    assert result["source"] == "auto"
    assert result["last_error"] == "bridge_syncing"


def test_folder_specific_inventory_never_borrows_the_account_eta():
    reader = status_client(auto={"state": "running", "target": 4, "eta_seconds": 9.0})
    result = attach_index_status(
        reader, {}, depends_on_index=True, incomplete=True, indexed_count=1, target=4
    )
    assert result["index_status"]["eta_seconds"] is None
    result = attach_index_status(
        reader, {}, depends_on_index=True, incomplete=True, indexed_count=1, target=3
    )
    assert result["index_status"]["eta_seconds"] is None


def test_failed_automatic_recovery_keeps_terminal_error_with_complete_all_mail(
    settings, monkeypatch
):
    reader = client(settings, FakeImap())
    monkeypatch.setattr(
        reader.search_index,
        "reporting_status",
        lambda: {"indexed_count": 4, "all_mail_count": 4},
    )
    reader.index_autorebuild._update(state="failed", last_error="headers_incomplete", target=4)
    result = reader.reindex_status()
    assert result["state"] == "failed" and result["last_error"] == "headers_incomplete"
    assert result["eta_seconds"] is None


def test_active_real_folder_phase_never_claims_zero_eta(settings, monkeypatch):
    reader = client(settings, FakeImap())
    monkeypatch.setattr(
        reader.search_index,
        "reporting_status",
        lambda: {"indexed_count": 4, "all_mail_count": 4},
    )
    reader.index_autorebuild._update(state="running", target=4, eta_seconds=0.0)
    result = reader.reindex_status()
    assert result["state"] == "rebuilding" and result["percent"] == 100.0
    assert result["eta_seconds"] is None


def test_stale_worker_success_cannot_supply_counts_for_an_unknown_cache():
    reader = status_client(
        auto={"state": "succeeded", "indexed_count": 1000, "target": 1000, "eta_seconds": 0.0}
    )
    result = attach_index_status(reader, {}, depends_on_index=True, incomplete=True)
    assert result["index_status"]["state"] == "incomplete"
    assert result["index_status"]["target"] is None
    assert "1000" not in result["index_note"]


@pytest.mark.parametrize("auto_state", ["idle", "queued", "running"])
def test_unavailable_cache_warning_never_claims_background_activity(auto_state):
    reader = status_client(auto={"state": auto_state, "indexed_count": 1000, "target": 1000})
    reader.search_index.enabled = False
    result = attach_index_status(reader, {}, depends_on_index=True, incomplete=True)
    assert result["index_status"]["state"] == "unavailable"
    assert result["index_status"]["target"] is None
    assert result["index_note"].startswith("search index unavailable:")


@pytest.mark.parametrize("auto_state", ["idle", "queued", "running", "succeeded"])
@pytest.mark.parametrize("counts", [None, {"indexed_count": 4, "all_mail_count": 4}])
def test_unavailable_cache_does_not_borrow_worker_activity_or_counts(
    settings, monkeypatch, auto_state, counts
):
    fake = FakeImap()
    reader = client(settings, fake)
    reader.reindex._update(state="succeeded", run_id="old-run", swap_outcome="swapped")
    # Availability can change immediately after a local count snapshot.
    monkeypatch.setattr(reader.search_index, "reporting_status", lambda: counts)
    reader.search_index.enabled = False
    reader.index_autorebuild.manual_run_id = "old-run"
    reader.index_autorebuild._update(
        state=auto_state, indexed_count=1000, target=1000, eta_seconds=1.0
    )
    result = reader.reindex_status()
    assert result["state"] == "unavailable"
    assert result["indexed_count"] == (None if counts is None else 4)
    assert result["target"] == (None if counts is None else 4)
    assert result["run_id"] == "old-run" and result["swap_outcome"] == "swapped"
    assert result["eta_seconds"] is None
    assert fake.calls == []


@pytest.mark.parametrize("processed,total", [(999, 1000), (1000, 1000)])
def test_manual_running_status_never_promises_zero_eta(settings, monkeypatch, processed, total):
    reader = client(settings, FakeImap())
    reader.reindex._update(state="running", run_id="manual-run")
    reader.reindex._processed = processed
    reader.reindex._work_total = total
    reader.reindex._started = 100.0
    monkeypatch.setattr("proton_mail_mcp.reindex.time.monotonic", lambda: 100.001)
    assert reader.reindex.status()["eta_seconds"] == 0.0
    result = reader.reindex_status()
    assert result["state"] == "running" and result["run_id"] == "manual-run"
    assert result["percent_done"] == 99.9
    assert result["eta_seconds"] is None


@pytest.mark.parametrize("auto_state", ["failed", "partial"])
@pytest.mark.parametrize("indexed", [3, 4])
def test_later_manual_success_supersedes_historical_auto_failure(
    settings, monkeypatch, auto_state, indexed
):
    reader = client(settings, FakeImap())
    reader.index_autorebuild.manual_run_id = "earlier-manual-run"
    reader.index_autorebuild._update(state=auto_state, last_error="authentication_failed", target=4)
    reader.reindex._update(
        state="succeeded", run_id="new-manual-run", swap_outcome="swapped", last_error=None
    )
    monkeypatch.setattr(
        reader.search_index,
        "reporting_status",
        lambda: {"indexed_count": indexed, "all_mail_count": 4},
    )
    result = reader.reindex_status()
    assert result["state"] == ("succeeded" if indexed == 4 else "incomplete")
    assert result["run_id"] == "new-manual-run"
    assert result["swap_outcome"] == "swapped"
    assert result["last_error"] is None
    note = attach_index_status(reader, {}, depends_on_index=True, incomplete=True)
    assert note["index_status"]["state"] == "incomplete"
    assert "last_error" not in note["index_status"]
    assert "failed" not in note["index_note"]
