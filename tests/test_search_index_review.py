"""Complete search pages and bounded optional header catch-up, entirely offline."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_mail import client, message
from test_mail import settings as settings
from test_search_index import CoverageImap

from proton_mail_mcp.mail import CallDeadlineExceeded, MailError


@pytest.fixture(autouse=True)
def focus_on_index_maintenance(monkeypatch):
    # These fixtures isolate account coverage with All Mail-only messages.
    # Canonical folder verification is exercised by its dedicated test suite.
    monkeypatch.setattr(
        "proton_mail_mcp.canonical.resolve_views",
        lambda client, connection, deadline, handles, **kwargs: [
            {"folder": "INBOX", "uidvalidity": item["uidvalidity"], "uid": item["uid"]}
            for item in handles
        ],
    )


def warm_account(settings):
    fake = CoverageImap(
        {
            "INBOX": {"10": message("<inbox@example.invalid>")},
            "All Mail": {"1": message("<all@example.invalid>")},
        },
        uidnext=True,
    )
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    assert reader.search_index.known_uids("All Mail", "1") == {"1"}
    fake.calls.clear()
    return reader, fake


def test_real_folder_maintenance_uses_only_status_after_search(settings, monkeypatch):
    reader, fake = warm_account(settings)
    original_reconcile = reader.search_index.reconcile
    original_known = reader.search_index.known_uids

    def reconcile(folder, *args, **kwargs):
        assert folder == "INBOX"
        return original_reconcile(folder, *args, **kwargs)

    def no_uid_read(folder, *args):
        # Only the searched folder's own inventory proof reads its cached UIDs.
        if folder != "INBOX":
            pytest.fail("Complete account maintenance must not load all cached UIDs")
        return original_known(folder, *args)

    monkeypatch.setattr(reader.search_index, "known_uids", no_uid_read)
    monkeypatch.setattr(reader.search_index, "reconcile", reconcile)
    result = reader.search_mail(include_snippets=False)

    assert "search_index" not in result
    assert result["total"] == 1 and not result["partial"]
    assert [call[1] for call in fake.calls if call[0] == "EXAMINE"] == ['"INBOX"']
    status_position = next(i for i, call in enumerate(fake.calls) if call[0] == "STATUS")
    assert all(call[0] == "LOGOUT" for call in fake.calls[status_position + 1 :])
    assert fake.current == "INBOX"


@pytest.mark.parametrize(
    "error,source",
    [
        (MailError("Unavailable"), "status"),
        (CallDeadlineExceeded("Deadline"), "status"),
        (sqlite3.OperationalError("Unavailable"), "coverage"),
    ],
)
def test_optional_maintenance_failures_keep_complete_real_folder_results(
    settings, monkeypatch, error, source
):
    reader, fake = warm_account(settings)

    def fail(*args):
        raise error

    monkeypatch.setattr(fake if source == "status" else reader.search_index, source, fail)
    result = reader.search_mail(include_snippets=False)

    assert "search_index" not in result
    assert result["total"] == 1 and result["total_is_exact"]
    assert len(result["messages"]) == 1
    assert not result["partial"] and "partial_reason" not in result
    assert not result.get("timed_out")


@pytest.mark.parametrize(
    "status,all_mail_examines",
    [
        (("OK", [b'"All Mail" (MESSAGES 1 UIDNEXT 2 UIDVALIDITY 1)']), 0),
        (("NO", [b"unavailable"]), 0),
        (("OK", [b'"Other" (MESSAGES 1 UIDNEXT 3 UIDVALIDITY 1)']), 0),
        (("OK", [b'"All Mail" (MESSAGES 1 UIDNEXT 2 UIDNEXT 1)']), 0),
        (("OK", [b'"All Mail" (MESSAGES 1 UIDNEXT 2 UIDVALIDITY 2)']), 1),
        (("OK", [b'"All Mail" (MESSAGES 0 UIDNEXT 2 UIDVALIDITY 1)']), 1),
        (("OK", [b'"All Mail" (MESSAGES 1 UIDNEXT 3 UIDVALIDITY 1)']), 1),
    ],
)
def test_status_must_match_the_full_proven_epoch(settings, monkeypatch, status, all_mail_examines):
    reader, fake = warm_account(settings)
    monkeypatch.setattr(fake, "status", lambda *args: status)
    result = reader.search_mail(include_snippets=False)
    assert "search_index" not in result
    assert result["total"] == 1 and not result["partial"]
    assert sum(call[:2] == ("EXAMINE", '"All Mail"') for call in fake.calls) == all_mail_examines


def test_running_reindex_skips_optional_account_maintenance(settings, monkeypatch):
    reader, fake = warm_account(settings)
    reader._reindex_service = SimpleNamespace(running=True)

    def fail(*args):
        pytest.fail("No optional All Mail work while rebuilding")

    monkeypatch.setattr(reader.search_index, "coverage", fail)
    monkeypatch.setattr(fake, "status", fail)
    result = reader.search_mail(include_snippets=False)
    assert result["total"] == 1 and result["total_is_exact"] and not result["partial"]
    assert not {"search_index", "search_index_details", "retryable"} & result.keys()
    assert [call[1] for call in fake.calls if call[0] == "EXAMINE"] == ['"INBOX"']


@pytest.mark.parametrize("state", ["queued", "running"])
def test_active_auto_recovery_reports_rebuilding_without_optional_imap(
    settings, monkeypatch, state
):
    reader, fake = warm_account(settings)
    reader.index_autorebuild._update(state=state)

    def fail(*args):
        pytest.fail("No optional freshness work while recovery is active")

    monkeypatch.setattr(fake, "status", fail)
    result = reader.search_mail(include_snippets=False)
    assert result["total"] == 1 and result["total_is_exact"] is True
    assert result["partial"] is False and "partial_reason" not in result
    assert len(result["messages"]) == 1
    assert result["messages"][0]["message_id"] == "<inbox@example.invalid>"
    assert not result.get("timed_out")
    assert (
        not {"search_index", "search_index_details", "index_note", "index_status"} & result.keys()
    )
    assert [call[1] for call in fake.calls if call[0] == "EXAMINE"] == ['"INBOX"']
    assert reader.index_autorebuild.status()["state"] == state


def test_cache_invalidation_during_maintenance_keeps_complete_folder_results(settings, monkeypatch):
    reader, fake = warm_account(settings)
    original = fake.status

    def status(*args):
        reader.search_index.remove("All Mail", "1", ["1"])
        return original(*args)

    monkeypatch.setattr(fake, "status", status)
    result = reader.search_mail(include_snippets=False)
    assert result["total"] == 1 and result["total_is_exact"] and not result["partial"]


@pytest.mark.parametrize("when", ["before_search", "during_status"])
def test_legacy_writer_header_loss_does_not_limit_the_requested_folder(settings, monkeypatch, when):
    reader, fake = warm_account(settings)
    original = fake.status

    def downgrade():
        # Simulate an older process that leaves the new proof metadata alone.
        with closing(sqlite3.connect(reader.search_index.path)) as database, database:
            database.execute("UPDATE messages SET headers_known = 0 WHERE folder = 'All Mail'")
            assert database.execute("SELECT certified FROM coverage").fetchone() == (1,)

    if when == "before_search":
        downgrade()
    else:

        def status(*args):
            downgrade()
            return original(*args)

        monkeypatch.setattr(fake, "status", status)

    result = reader.search_mail(include_snippets=False)

    # This page warms INBOX's own full inventory regardless of All Mail's headers.
    assert reader.search_index.covers("INBOX", "1", ["10"])
    assert result["total"] == 1 and not result["partial"]
    assert reader.search_index.coverage("All Mail") == (
        ("1", 1, "2") if when == "before_search" else None
    )


def test_restart_validates_persisted_proof_for_every_folder(settings):
    _, fake = warm_account(settings)
    reader = client(settings, fake)
    result = reader.search_mail(include_snippets=False)
    assert result["total"] == 1 and result["total_is_exact"] and not result["partial"]
    assert any(call[0] == "STATUS" for call in fake.calls)
    result = reader.search_mail(folder="All Mail", include_snippets=False)
    assert result["total"] == 1 and result["total_is_exact"] and not result["partial"]
    result = reader.search_mail(include_snippets=False)
    assert result["total"] == 1 and result["total_is_exact"] and not result["partial"]


@pytest.mark.parametrize("bounds", [{"since": "2026-09-01"}, {"before": "2026-11-01"}])
def test_dated_search_completes_scoped_page_before_catchup_inventory(settings, monkeypatch, bounds):
    reader, fake = warm_account(settings)
    fake.folders["All Mail"]["2"] = message("<arrival@example.invalid>")
    original = reader._metadata

    def metadata(*args, **kwargs):
        assert kwargs.get("internaldate", True), "No header-only catch-up for dated searches"
        return original(*args, **kwargs)

    monkeypatch.setattr(reader, "_metadata", metadata)
    result = reader.search_mail(folder="All Mail", include_snippets=False, **bounds)
    assert result["total"] == 2
    searches = [call[3:] for call in fake.calls if call[0] == "SEARCH"]
    assert searches[0][0] in ("SINCE", "BEFORE") and searches[-1] == ("UID", "2:2")
    assert "search_index" not in result
    assert reader.search_index.coverage("All Mail") == ("1", 2, "3")


def test_cursor_page_keeps_its_uid_snapshot_while_catching_up_new_headers(settings, monkeypatch):
    fake = CoverageImap(
        {"All Mail": {str(uid): message(f"<{uid}@example.invalid>") for uid in (1, 2)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    first = reader.search_mail(folder="All Mail", limit=1, include_snippets=False)
    fake.folders["All Mail"]["3"] = message("<arrival@example.invalid>")
    fake.calls.clear()
    original = reader._metadata
    fetched = []

    def metadata(connection, uids, **kwargs):
        assert kwargs.get("internaldate", True), "No header-only catch-up on cursor pages"
        fetched.append(list(uids))
        return original(connection, uids, **kwargs)

    monkeypatch.setattr(reader, "_metadata", metadata)
    result = reader.search_mail(
        folder="All Mail", limit=1, cursor=first["next_cursor"], include_snippets=False
    )
    assert [item["alias"]["uid"] for item in result["messages"]] == ["1"]
    assert fetched[0] == ["1"] and fetched[-1] == ["3"]
    assert reader.search_index.coverage("All Mail") == ("1", 3, "4")
    assert "search_index" not in result


@pytest.mark.parametrize("stop_after_one", [False, True])
def test_catchup_reserves_time_and_runs_the_normal_date_search(
    settings, monkeypatch, stop_after_one
):
    reader, fake = warm_account(settings)
    fake.folders["All Mail"].update(
        {
            str(uid): message(f"<{uid}@example.invalid>", internaldate="01-Jan-2020 00:00:00 +0000")
            for uid in (2, 3)
        }
    )
    monkeypatch.setattr("proton_mail_mcp.search._METADATA_BATCH_SIZE", 1)
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-09-27")
    checks = iter([True, True, False] if stop_after_one else [False])
    monkeypatch.setattr("proton_mail_mcp.search_sync.has_time", lambda *args: next(checks))
    result = reader.search_mail(folder="All Mail", include_snippets=False)

    assert result["partial_reason"] == "no_resumable_cursor"
    assert result["partial"] and not result["total_is_exact"]
    assert result["applied_since"] == "2026-09-27" and result["total"] == 1
    assert [call[3:] for call in fake.calls if call[0] == "SEARCH" and call[3] != "ON"] == (
        ([("UID", "2:3")] if stop_after_one else []) + [("SINCE", "27-Sep-2026")]
    )
    catchup_headers = [
        call[1]
        for call in fake.calls
        if call[0] == "FETCH" and "HEADER.FIELDS" in call[2] and call[1] in ("2", "3")
    ]
    assert catchup_headers == (["2"] if stop_after_one else [])


def test_short_call_budget_preserves_fallback_before_optional_inventory(settings, monkeypatch):
    settings = replace(settings, call_timeout=12, timeout=30)
    reader, fake = warm_account(settings)
    fake.folders["All Mail"]["2"] = message("<arrival@example.invalid>")
    clock = [1000.0]
    monkeypatch.setattr("proton_mail_mcp.search.time.monotonic", lambda: clock[0])
    deadline = reader._deadline()
    monkeypatch.setattr(reader, "_deadline", lambda: deadline)
    original_select, original_search = fake.select, reader._search

    def select(folder, *, readonly):
        result = original_select(folder, readonly=readonly)
        clock[0] += 7.0
        return result

    def search(connection, criteria, **kwargs):
        if criteria[0] == "SINCE":
            assert deadline.end == 1012.0
            assert deadline.io_end - clock[0] == pytest.approx(3.8)
        return original_search(connection, criteria, **kwargs)

    monkeypatch.setattr(fake, "select", select)
    monkeypatch.setattr(reader, "_search", search)
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-09-27")
    result = reader.search_mail(folder="All Mail", include_snippets=False)
    assert result["partial_reason"] == "no_resumable_cursor"
    assert result["total"] == 2
    assert [call[3:] for call in fake.calls if call[0] == "SEARCH" and call[3] != "ON"] == [
        ("SINCE", "27-Sep-2026")
    ]


@pytest.mark.parametrize("stop_at", ["inventory", "headers"])
def test_catchup_io_cutoff_leaves_time_for_a_new_fallback_session(settings, monkeypatch, stop_at):
    monkeypatch.setattr("proton_mail_mcp.search._MAX_SEARCH_SECONDS", settings.call_timeout)
    reader, fake = warm_account(settings)
    fake.folders["All Mail"]["2"] = message("<arrival@example.invalid>")
    deadline = reader._deadline()
    original_end = deadline.end
    monkeypatch.setattr(reader, "_deadline", lambda: deadline)
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-09-27")
    original_search, original_metadata = reader._search, reader._metadata
    interrupted = []

    def stop(connection):
        assert deadline.end == original_end - min(5, settings.call_timeout / 4)
        interrupted.append(connection)
        # Simulate the real watchdog expiring an optional I/O deadline.
        deadline.end = 0
        raise CallDeadlineExceeded("Optional command timed out")

    def search(connection, criteria, **kwargs):
        if criteria == ["UID", "2:2"] and stop_at == "inventory":
            stop(connection)
        assert connection not in interrupted
        if criteria[0] == "SINCE":
            assert deadline.end == original_end
        return original_search(connection, criteria, **kwargs)

    def metadata(connection, uids, **kwargs):
        if deadline.end < original_end and stop_at == "headers":
            stop(connection)
        assert connection not in interrupted
        return original_metadata(connection, uids, **kwargs)

    monkeypatch.setattr(reader, "_search", search)
    monkeypatch.setattr(reader, "_metadata", metadata)
    result = reader.search_mail(folder="All Mail", include_snippets=False)

    assert len(interrupted) == 1
    assert result["partial_reason"] == "no_resumable_cursor"
    assert result["applied_since"] == "2026-09-27"
    assert result["total"] == 2 and result["partial"]
    assert sum(call[0] == "LOGIN" for call in fake.calls) == 2
    assert deadline.end == original_end
