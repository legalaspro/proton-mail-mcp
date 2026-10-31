"""Incremental header maintenance uses only read-only in-memory IMAP doubles."""

import logging
from dataclasses import replace

import pytest
from test_header_catchup_incremental import RangeImap, assert_read_only, catchup, seed_baseline
from test_imap_idle import ScriptedStop, WatchImap
from test_mail import FakeImap, client, message
from test_mail import settings as settings

from proton_mail_mcp import imap_idle
from proton_mail_mcp.mail import CallDeadlineExceeded, MailError
from proton_mail_mcp.search_sync import sync_all_mail, sync_selected_headers


def populate(reader, count):
    index = reader.search_index
    index.observe(
        "All Mail",
        "1",
        {
            str(uid): {"headers": {"Subject": "Indexed", "Message-ID": f"<{uid}@test.invalid>"}}
            for uid in range(1, count + 1)
        },
    )
    assert index.record_coverage(
        "All Mail",
        "1",
        {"exists": count, "uidnext": str(count + 1)},
        [str(uid) for uid in range(1, count + 1)],
    )


def header_fetches(fake):
    return [call for call in fake.calls if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]]


def test_catchup_large_inventory_fetches_only_arrivals_and_reconciles_expunges(settings):
    fake = FakeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in range(1, 1105)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    populate(reader, 1101)
    del fake.folders["All Mail"]["3"]
    deadline = reader._deadline()
    with reader._session(deadline=deadline) as (connection, _):
        state = reader._select(connection, "All Mail")
        result = sync_selected_headers(reader, connection, deadline, "All Mail", state)

    assert result.complete and not result.timed_out
    assert result.fetched == {"1102", "1103", "1104"}
    assert "3" not in reader.search_index.known_uids("All Mail", "1")
    assert reader.search_index.coverage("All Mail") == ("1", 1103, "1105")
    fetches = header_fetches(fake)
    assert [call[1] for call in fetches] == ["1102,1103,1104"]
    assert all("BODY.PEEK[HEADER" in call[2] and "INTERNALDATE" in call[2] for call in fetches)
    assert all("BODYSTRUCTURE" not in call[2] for call in fetches)


def test_catchup_same_count_expunge_and_arrival_cannot_reuse_old_proof(settings):
    fake = FakeImap({"All Mail": {"2": message("<2@test.invalid>")}}, uidnext=True)
    reader = client(settings, fake)
    populate(reader, 1)
    deadline = reader._deadline()
    with reader._session(deadline=deadline) as (connection, _):
        state = reader._select(connection, "All Mail")
        result = sync_selected_headers(reader, connection, deadline, "All Mail", state)

    assert result.complete
    assert result.fetched == {"2"}
    assert reader.search_index.known_uids("All Mail", "1") == {"2"}
    assert reader.search_index.coverage("All Mail") == ("1", 1, "3")


def test_catchup_bound_does_not_fetch_historical_backlog(settings):
    settings = replace(settings, search_index_catchup_max_messages=1)
    fake = FakeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in range(1, 4)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    populate(reader, 1)
    deadline = reader._deadline()
    with reader._session(deadline=deadline) as (connection, _):
        state = reader._select(connection, "All Mail")
        result = sync_selected_headers(reader, connection, deadline, "All Mail", state)

    # A certified epoch may advance by cap arrivals per call, while an unbuilt
    # historical cache still needs an explicit rebuild (covered separately).
    assert not result.complete and result.inventory == ["1", "2", "3"]
    assert result.fetched == {"2"}
    assert [call[1] for call in header_fetches(fake)] == ["2"]
    assert [call for call in fake.calls if call[0] == "SEARCH"] == [
        ("SEARCH", "CHARSET", "UTF-8", "UID", "2:3")
    ]
    assert reader.search_index.coverage_epoch("All Mail") == ("1", 1, "2")


def test_uncertified_historical_backlog_does_not_start_catchup(settings):
    settings = replace(settings, search_index_catchup_max_messages=1)
    fake = FakeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in range(1, 4)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    reader.search_index.observe("All Mail", "1", {"1": {"headers": {"Subject": "Indexed"}}})
    deadline = reader._deadline()
    with reader._session(deadline=deadline) as (connection, _):
        state = reader._select(connection, "All Mail")
        result = sync_selected_headers(reader, connection, deadline, "All Mail", state)

    assert not result.complete and result.inventory is None
    assert not header_fetches(fake)
    assert not [call for call in fake.calls if call[0] == "SEARCH"]


def test_baseline_expunge_during_large_backlog_keeps_progressing_until_all_reconcile(settings):
    settings = replace(settings, search_index_catchup_max_messages=1)
    fake = RangeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in range(1, 8)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    seed_baseline(reader, ["1", "2"])

    def selected_catchup():
        deadline = reader._deadline()
        with reader._session(deadline=deadline) as (connection, _):
            state = reader._select(connection, "All Mail")
            return sync_selected_headers(reader, connection, deadline, "All Mail", state)

    first = selected_catchup()
    assert not first.complete and first.fetched == {"3"}
    del fake.folders["All Mail"]["1"]

    resumed = selected_catchup()

    assert not resumed.complete and resumed.inventory is None
    assert resumed.fetched == {"4"}
    assert reader.search_index.coverage_epoch("All Mail") == ("1", 2, "3")
    assert not [call for call in fake.calls if call[0] == "SEARCH" and call[3:] == ("ALL",)]
    for _ in range(6):
        result = selected_catchup()
        assert len(result.fetched) <= 1
        if result.complete:
            break
        assert reader.search_index.coverage("All Mail") is None
        assert reader.search_index.coverage_epoch("All Mail") == ("1", 2, "3")
    else:
        pytest.fail("bounded tail progress never enabled the reconciling ALL fallback")

    assert [call[1] for call in header_fetches(fake)] == ["3", "4", "5", "6", "7"]
    assert [call for call in fake.calls if call[0] == "SEARCH" and call[3:] == ("ALL",)]
    assert reader.search_index.known_uids("All Mail", "1") == {"2", "3", "4", "5", "6", "7"}
    assert reader.search_index.coverage("All Mail") == ("1", 6, "8")
    assert_read_only(fake)


@pytest.mark.parametrize("reason", ["backlog", "budget", "imap"])
def test_catchup_failures_are_visible_at_production_warning_level(
    settings, monkeypatch, caplog, reason
):
    settings = replace(settings, search_index_catchup_max_messages=1)
    fake = RangeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in range(1, 4)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    seed_baseline(reader, ["1"])
    if reason == "budget":
        monkeypatch.setattr("proton_mail_mcp.search_sync.has_time", lambda *_: False)
    elif reason == "imap":

        def metadata(*args, **kwargs):
            raise MailError("private-subject-do-not-log")

        monkeypatch.setattr(reader, "_metadata", metadata)
    caplog.set_level(logging.WARNING, logger="proton_mail_mcp.search_sync")

    result = catchup(reader)

    assert result is not None and not result.complete
    assert f"reason={reason}" in caplog.text
    assert all(record.levelno >= logging.WARNING for record in caplog.records)
    assert "private-subject-do-not-log" not in caplog.text


@pytest.mark.parametrize("condition", ["missing", "disabled"])
def test_missing_or_disabled_catchup_is_quiet_at_warning(settings, caplog, condition):
    if condition == "disabled":
        settings = replace(settings, search_index_catchup_max_messages=0)
    fake = RangeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in (1, 2)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    if condition == "disabled":
        seed_baseline(reader, ["1"])
    caplog.set_level(logging.WARNING, logger="proton_mail_mcp.search_sync")

    assert catchup(reader) is None

    assert not [record for record in caplog.records if record.name == "proton_mail_mcp.search_sync"]
    assert not [call for call in fake.calls if call[0] in {"SEARCH", "FETCH"}]


def test_selected_catchup_reuses_supplied_inventory_without_uid_search(settings):
    fake = RangeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in (1, 2)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    seed_baseline(reader, ["1"])
    deadline = reader._deadline()
    with reader._session(deadline=deadline) as (connection, _):
        state = reader._select(connection, "All Mail")
        result = sync_selected_headers(
            reader, connection, deadline, "All Mail", state, inventory=["1", "2"]
        )

    assert result.complete and result.fetched == {"2"}
    assert not [call for call in fake.calls if call[0] == "SEARCH"]
    assert reader.search_index.coverage("All Mail") == ("1", 2, "3")
    assert_read_only(fake)


def test_catchup_failure_to_write_never_certifies_complete(settings, monkeypatch):
    fake = FakeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in (1, 2)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    populate(reader, 1)
    monkeypatch.setattr(reader.search_index, "observe", lambda *args: None)
    deadline = reader._deadline()
    with reader._session(deadline=deadline) as (connection, _):
        state = reader._select(connection, "All Mail")
        result = sync_selected_headers(reader, connection, deadline, "All Mail", state)

    assert result.fetched == {"2"} and not result.complete
    assert reader.search_index.coverage("All Mail") is None


def test_optional_catchup_preserves_reserve_and_marks_interrupted_stream(settings, monkeypatch):
    fake = FakeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in (1, 2)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    populate(reader, 1)
    deadline = reader._deadline()
    original_end = deadline.end

    def metadata(*args, **kwargs):
        assert deadline.end == original_end - min(5, settings.call_timeout / 4)
        deadline.end = 0
        raise CallDeadlineExceeded("Simulated timeout")

    monkeypatch.setattr(reader, "_metadata", metadata)
    with reader._session(deadline=deadline) as (connection, _):
        state = reader._select(connection, "All Mail")
        result = sync_selected_headers(reader, connection, deadline, "All Mail", state)

    assert result.timed_out and result.stream_broken and not result.complete
    assert deadline.end == original_end
    # A failed first batch leaves the saved epoch untouched; it cannot match
    # the newly selected live epoch and therefore cannot claim freshness.
    assert reader.search_index.coverage("All Mail") == ("1", 1, "2")


def test_all_mail_catchup_restores_folder_and_unchanged_proof_needs_only_status(settings):
    fake = FakeImap(
        {
            "INBOX": {"20": message("<2@test.invalid>")},
            "All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in (1, 2)},
        },
        uidnext=True,
    )
    reader = client(settings, fake)
    populate(reader, 1)
    deadline = reader._deadline()
    with reader._session(deadline=deadline) as (connection, _):
        reader._select(connection, "INBOX")
        result = sync_all_mail(reader, connection, deadline, "All Mail")
        assert result.complete and result.fetched == {"2"}
        assert fake.current == "INBOX"
        assert connection._search_selection == ("INBOX", "1")
        fake.calls.clear()
        repeated = sync_all_mail(reader, connection, deadline, "All Mail")
        assert repeated.complete and not repeated.fetched
        assert [call[0] for call in fake.calls] == ["STATUS"]


def test_selected_all_mail_catchup_refreshes_epoch_after_mid_page_arrival(settings):
    fake = FakeImap({"All Mail": {"1": message("<1@test.invalid>")}}, uidnext=True)
    reader = client(settings, fake)
    populate(reader, 1)
    deadline = reader._deadline()
    with reader._session(deadline=deadline) as (connection, _):
        previous = reader._select(connection, "All Mail")
        fake.folders["All Mail"]["2"] = message("<2@test.invalid>")
        fake.calls.clear()

        result = sync_all_mail(reader, connection, deadline, "All Mail")

        assert result.complete and result.fetched == {"2"}
        assert connection._selected_inventory != previous
        assert connection._selected_inventory["exists"] == 2
        assert [call[0] for call in fake.calls].count("EXAMINE") == 1
        assert reader.search_index.coverage("All Mail") == ("1", 2, "3")
        fake.calls.clear()
        repeated = sync_all_mail(reader, connection, deadline, "All Mail")
        assert repeated.complete and not repeated.fetched
        assert [call[0] for call in fake.calls] == ["STATUS"]


def test_idle_catches_up_account_index_and_restores_watched_folder(settings, monkeypatch):
    settings = replace(settings, imap_idle_enabled=True)
    fake = WatchImap(
        {
            "INBOX": {"20": message("<2@test.invalid>")},
            "All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in (1, 2)},
        }
    )
    reader = client(settings, fake)
    populate(reader, 1)
    watcher = imap_idle.MailWatcher(reader, settings)
    watcher._stop = ScriptedStop()
    proofs = []

    def idle(connection, deadline, settings):
        proofs.append(reader.search_index.coverage("All Mail"))
        assert fake.current == "INBOX"
        assert not imap_idle._changed(connection)
        if len(proofs) == 1:
            assert [call[1] for call in header_fetches(fake)] == ["2"]
            del fake.folders["All Mail"]["1"]
            fake.folders["All Mail"]["3"] = message("<3@test.invalid>")
            fake.folders["INBOX"]["30"] = message("<3@test.invalid>")
            fake.responses["EXISTS"] = [b"2"]
            return True
        watcher._stop.set()
        return False

    monkeypatch.setattr(imap_idle, "_idle", idle)
    watcher._run("INBOX")

    assert proofs == [("1", 2, "3"), ("1", 2, "4")]
    assert reader.search_index.known_uids("INBOX", "1") == {"30"}
    assert reader.search_index.known_uids("All Mail", "1") == {"2", "3"}
    assert [call[1] for call in header_fetches(fake)] == ["2", "30", "3"]
    assert all("BODY.PEEK[HEADER" in call[2] for call in header_fetches(fake))


def test_all_mail_idle_expunge_refreshes_coverage_without_header_fetch(settings, monkeypatch):
    settings = replace(settings, imap_idle_enabled=True, imap_idle_folders=("All Mail",))
    fake = WatchImap({"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in (1, 2)}})
    reader = client(settings, fake)
    populate(reader, 2)
    watcher = imap_idle.MailWatcher(reader, settings)
    watcher._stop = ScriptedStop()
    calls = []

    def idle(connection, deadline, settings):
        calls.append(reader.search_index.coverage("All Mail"))
        if len(calls) == 1:
            del fake.folders["All Mail"]["2"]
            fake.responses["EXPUNGE"] = [b"2"]
            return True
        watcher._stop.set()
        return False

    monkeypatch.setattr(imap_idle, "_idle", idle)
    watcher._run("All Mail")

    assert calls == [("1", 2, "3"), ("1", 1, "3")]
    assert not header_fetches(fake)
    assert reader.search_index.known_uids("All Mail", "1") == {"1"}


def test_inbox_plain_search_catches_up_all_mail_and_keeps_fresh(settings):
    fake = FakeImap(
        {
            "INBOX": {"10": message("<1@test.invalid>")},
            "All Mail": {"1": message("<1@test.invalid>")},
        },
        uidnext=True,
    )
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    fake.folders["INBOX"]["20"] = message("<2@test.invalid>")
    fake.folders["All Mail"]["2"] = message("<2@test.invalid>")
    fake.calls.clear()

    result = reader.search_mail(include_snippets=False)

    assert "partial_reason" not in result
    assert result["total"] == 2 and result["total_is_exact"] and not result["partial"]
    assert {item["uid"] for item in result["messages"]} == {"10", "20"}
    assert reader.search_index.coverage("All Mail") == ("1", 2, "3")
    assert fake.current == "INBOX"
    account_fetches = [call for call in header_fetches(fake) if call[1] == "2"]
    assert len(account_fetches) == 1
    assert "INTERNALDATE" in account_fetches[0][2]
    assert "ENVELOPE" in account_fetches[0][2] and "RFC822.SIZE" in account_fetches[0][2]

    del fake.folders["All Mail"]["1"]
    fake.calls.clear()
    repeated = reader.search_mail(include_snippets=False)
    assert not repeated["partial"] and "partial_reason" not in repeated
    assert repeated["total"] == 2
    assert reader.search_index.known_uids("All Mail", "1") == {"2"}
    assert reader.search_index.coverage("All Mail") == ("1", 1, "3")


def test_all_mail_search_catches_up_new_uids_and_reconciles_expunges(settings):
    fake = FakeImap(
        {
            "INBOX": {"10": message("<1@test.invalid>")},
            "All Mail": {"1": message("<1@test.invalid>")},
        },
        uidnext=True,
    )
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    del fake.folders["All Mail"]["1"]
    del fake.folders["INBOX"]["10"]
    fake.folders["All Mail"]["2"] = message("<2@test.invalid>")
    fake.folders["INBOX"]["20"] = message("<2@test.invalid>")

    result = reader.search_mail(folder="All Mail", include_snippets=False)

    assert result["total"] == 1 and "partial_reason" not in result
    assert result["total_is_exact"] and not result["partial"]
    assert reader.search_index.known_uids("All Mail", "1") == {"2"}
    assert reader.search_index.coverage("All Mail") == ("1", 1, "3")


def test_failed_optional_account_sync_keeps_completed_inbox_page_and_stops_io(
    settings, monkeypatch
):
    fake = FakeImap(
        {
            "INBOX": {"10": message("<1@test.invalid>")},
            "All Mail": {"1": message("<1@test.invalid>")},
        },
        uidnext=True,
    )
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    fake.folders["All Mail"]["2"] = message("<2@test.invalid>")
    metadata = reader._metadata
    interrupted_at = []

    def fail_account_sync(connection, uids, **kwargs):
        if fake.current == "All Mail":
            interrupted_at.append(len(fake.calls))
            raise CallDeadlineExceeded("Simulated optional timeout")
        return metadata(connection, uids, **kwargs)

    monkeypatch.setattr(reader, "_metadata", fail_account_sync)
    result = reader.search_mail(include_snippets=False)

    assert "partial_reason" not in result
    assert result["total"] == 1 and result["total_is_exact"]
    assert result["messages"][0]["uid"] == "10"
    assert not result["partial"]
    assert interrupted_at and fake.calls[interrupted_at[0] :] == []
