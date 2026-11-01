"""Optional folder membership warming preserves the ordinary bounded search."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_header_search_index import indexed_message
from test_mail import client
from test_mail import settings as settings
from test_search_index import CoverageImap, header_uids

from proton_mail_mcp.mail import CallDeadlineExceeded, MailError, _Deadline


class MappingImap(CoverageImap):
    def shutdown(self):
        self.calls.append(("SHUTDOWN",))


def complete_source(settings, count=5, *, missing_metadata=False):
    mailbox = {
        str(uid): indexed_message(f"<mapping-{uid}@example.invalid>") for uid in range(1, count + 1)
    }
    inbox = {str(int(uid) + 4000): item for uid, item in mailbox.items()}
    fake = MappingImap({"All Mail": mailbox, "INBOX": inbox}, uidnext=True)
    reader = client(settings, fake)
    assert reader.rebuild_search_index("All Mail")["indexed"] == count
    if missing_metadata:
        # Complete headers do not imply immutable date/envelope evidence.
        with closing(sqlite3.connect(reader.search_index.path)) as database, database:
            database.execute("DELETE FROM search_metadata")
        assert reader.search_index.metadata_uids("All Mail", "1") == set()
        assert reader.search_index.coverage("All Mail") == ("1", count, str(count + 1))
    fake.calls.clear()
    return reader, fake


def assert_inbox_result(result, count):
    assert len(result["messages"]) == min(20, count)
    assert result["total"] == count
    assert not result.get("timed_out", False)
    assert all(
        row["folder"] == "INBOX" and row["uidvalidity"] == "1" and row["uid"] is not None
        for row in result["messages"]
    )


def test_missing_metadata_folder_mapping_short_deadline_keeps_capped_inbox_scan(
    settings, monkeypatch
):
    settings = replace(settings, timeout=1, call_timeout=4)
    reader, fake = complete_source(settings, 1003, missing_metadata=True)
    # A deterministic short deadline leaves enough time for the in-memory
    # ordinary scan, but less than one socket timeout plus the optional reserve.
    deadline = _Deadline(2, settings.timeout)
    monkeypatch.setattr(reader, "_deadline", lambda: deadline)

    result = reader.search_mail(from_="studio.example", include_snippets=False)

    assert_inbox_result(result, 1000)
    assert result["partial"] and not result["total_is_exact"]
    assert result["partial_reason"] == "candidate_cap"
    assert header_uids(fake) == set(map(str, range(4004, 5004)))
    assert not any(call[0] == "FETCH" and "ENVELOPE" in call[2] for call in fake.calls)
    assert sum(call[0] == "LOGIN" for call in fake.calls) == 1


@pytest.mark.parametrize(
    "missing_metadata", [True, False], ids=["source-metadata", "target-metadata"]
)
def test_folder_mapping_caps_optional_metadata_and_keeps_fallback_light(
    settings, monkeypatch, missing_metadata
):
    settings = replace(settings, timeout=1, call_timeout=60, search_index_catchup_max_messages=3)
    reader, fake = complete_source(settings, missing_metadata=missing_metadata)
    metadata = reader._metadata
    optional = []

    def record_metadata(connection, uids, **kwargs):
        if not kwargs.get("headers", True) and kwargs.get("envelope"):
            optional.append((fake.current, list(uids)))
        return metadata(connection, uids, **kwargs)

    monkeypatch.setattr(reader, "_metadata", record_metadata)

    result = reader.search_mail(from_="studio.example", include_snippets=False)

    assert_inbox_result(result, 5)
    assert sum(len(uids) for _, uids in optional) == 3
    assert {folder for folder, _ in optional} == {"All Mail" if missing_metadata else "INBOX"}
    ordinary = [call for call in fake.calls if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]]
    assert ordinary
    assert all("ENVELOPE" not in call[2] and "RFC822.SIZE" not in call[2] for call in ordinary)


def test_folder_mapping_shares_cap_across_source_and_target_and_retains_warming(
    settings, monkeypatch
):
    settings = replace(settings, timeout=1, call_timeout=60, search_index_catchup_max_messages=7)
    reader, fake = complete_source(settings, missing_metadata=True)
    metadata = reader._metadata
    optional = []
    mappings = []

    def record_metadata(connection, uids, **kwargs):
        if not kwargs.get("headers", True) and kwargs.get("envelope"):
            optional.append((fake.current, list(uids)))
        return metadata(connection, uids, **kwargs)

    monkeypatch.setattr(reader, "_metadata", record_metadata)
    monkeypatch.setattr(reader.search_index, "map_headers", lambda *args: mappings.append(args))

    result = reader.search_mail(from_="studio.example", include_snippets=False)

    assert_inbox_result(result, 5)
    assert optional == [
        ("All Mail", ["1", "2", "3", "4", "5"]),
        ("INBOX", ["4001", "4002"]),
    ]
    assert sum(len(uids) for _, uids in optional) == 7
    assert not mappings, "A capped target sample cannot establish Message-ID uniqueness"
    # The ordinary header scan supplies the other target dates/IDs. Only the
    # bounded warm step supplies envelope fingerprints, which survive on disk.
    with closing(sqlite3.connect(reader.search_index.path)) as database, database:
        retained = database.execute(
            "SELECT folder, uid FROM search_metadata WHERE envelope_digest IS NOT NULL"
        ).fetchall()
    assert set(retained) == {
        *(("All Mail", str(uid)) for uid in range(1, 6)),
        ("INBOX", "4001"),
        ("INBOX", "4002"),
    }


def test_folder_mapping_does_not_reuse_headers_before_duplicate_target_is_warmed(
    settings, monkeypatch
):
    settings = replace(settings, timeout=1, call_timeout=60, search_index_catchup_max_messages=3)
    reader, fake = complete_source(settings)
    # The duplicate is outside the optional sample. A sample alone would make
    # the first target's Message-ID appear unique in this mailbox.
    fake.folders["INBOX"]["4005"] = fake.folders["INBOX"]["4001"]
    mappings = []
    monkeypatch.setattr(reader.search_index, "map_headers", lambda *args: mappings.append(args))

    result = reader.search_mail(from_="studio.example", include_snippets=False)

    assert_inbox_result(result, 5)
    assert not mappings
    assert header_uids(fake) == set(map(str, range(4001, 4006)))
    assert [row["message_id"] for row in result["messages"]].count(
        "<mapping-1@example.invalid>"
    ) == 2


@pytest.mark.parametrize(
    "missing_metadata", [True, False], ids=["source-metadata", "target-metadata"]
)
def test_folder_mapping_reserves_time_for_search_between_batches(
    settings, monkeypatch, missing_metadata
):
    settings = replace(settings, timeout=1, call_timeout=60)
    reader, fake = complete_source(settings, missing_metadata=missing_metadata)
    metadata = reader._metadata
    deadline = _Deadline(20, settings.timeout)
    original_end = deadline.end
    optional = []
    enough_time = True

    def warm_one_batch(connection, uids, **kwargs):
        nonlocal enough_time
        if not kwargs.get("headers", True) and kwargs.get("envelope"):
            assert deadline.end == original_end - 5
            optional.append((fake.current, list(uids)))
            enough_time = False
        return metadata(connection, uids, **kwargs)

    monkeypatch.setattr(reader, "_metadata", warm_one_batch)
    monkeypatch.setattr(reader, "_deadline", lambda: deadline)
    monkeypatch.setattr("proton_mail_mcp.search._METADATA_BATCH_SIZE", 2)
    monkeypatch.setattr("proton_mail_mcp.search_sync.has_time", lambda *_: enough_time)

    result = reader.search_mail(from_="studio.example", include_snippets=False)

    assert_inbox_result(result, 5)
    expected = [("All Mail", ["1", "2"])] if missing_metadata else [("INBOX", ["4001", "4002"])]
    assert optional == expected
    assert deadline.end == original_end
    assert fake.current == "INBOX"
    if missing_metadata:
        assert reader.search_index.metadata_uids("All Mail", "1") == {"1", "2"}


@pytest.mark.parametrize("mapping_error", [False, True])
def test_folder_arrival_during_source_mapping_keeps_ordinary_scan(
    settings, monkeypatch, mapping_error
):
    settings = replace(settings, timeout=1, call_timeout=60)
    reader, fake = complete_source(settings, missing_metadata=True)
    metadata = reader._metadata
    arrivals = []

    def receive_during_mapping(connection, uids, **kwargs):
        fetched = metadata(connection, uids, **kwargs)
        if fake.current == "All Mail" and kwargs.get("envelope") and not arrivals:
            fake.folders["INBOX"]["9000"] = indexed_message("<arrival@example.invalid>")
            arrivals.append("9000")
            if mapping_error:
                raise MailError("Simulated mapping failure after arrival")
        return fetched

    monkeypatch.setattr(reader, "_metadata", receive_during_mapping)

    result = reader.search_mail(from_="studio.example", include_snippets=False)

    assert arrivals == ["9000"]
    assert_inbox_result(result, 5)
    assert fake.current == "INBOX"
    assert sum(call[0] == "LOGIN" for call in fake.calls) == 1
    assert not any(call[0] == "SHUTDOWN" for call in fake.calls)


@pytest.mark.parametrize("failure", [MailError, CallDeadlineExceeded])
def test_optional_mapping_error_falls_back_and_only_broken_stream_reconnects(
    settings, monkeypatch, failure
):
    settings = replace(settings, timeout=1, call_timeout=60)
    reader, fake = complete_source(settings, missing_metadata=True)
    metadata = reader._metadata
    attempted = []

    def fail_source(connection, uids, **kwargs):
        if fake.current == "All Mail" and kwargs.get("envelope"):
            attempted.append(list(uids))
            raise failure("Simulated optional mapping failure")
        return metadata(connection, uids, **kwargs)

    monkeypatch.setattr(reader, "_metadata", fail_source)

    result = reader.search_mail(from_="studio.example", include_snippets=False)

    assert_inbox_result(result, 5)
    assert len(attempted) == 1
    assert fake.current == "INBOX"
    broken = failure is CallDeadlineExceeded
    assert sum(call[0] == "LOGIN" for call in fake.calls) == (2 if broken else 1)
    assert any(call[0] == "SHUTDOWN" for call in fake.calls) is broken


def test_folder_mapping_skips_warming_during_reindex(settings, monkeypatch):
    settings = replace(settings, timeout=1, call_timeout=60)
    reader, fake = complete_source(settings, missing_metadata=True)
    reader._reindex_service = SimpleNamespace(running=True)
    metadata = reader._metadata

    def ordinary_metadata_only(connection, uids, **kwargs):
        assert not kwargs.get("envelope")
        return metadata(connection, uids, **kwargs)

    monkeypatch.setattr(reader, "_metadata", ordinary_metadata_only)

    result = reader.search_mail(from_="studio.example", include_snippets=False)

    assert_inbox_result(result, 5)
    assert not result["partial"] and "partial_reason" not in result
    assert not any(call[0] == "FETCH" and "ENVELOPE" in call[2] for call in fake.calls)


@pytest.mark.parametrize("folder", ["INBOX", "inbox"])
def test_mapping_timeout_retries_incoming_cursor_and_original_scope(settings, monkeypatch, folder):
    settings = replace(settings, timeout=1, call_timeout=60)
    reader, fake = complete_source(settings, missing_metadata=True)
    filters = dict(
        folder=folder,
        from_="studio.example",
        since="2026-10-01",
        before="2026-11-01",
        limit=1,
        include_snippets=False,
    )
    with monkeypatch.context() as skipped:
        skipped.setattr("proton_mail_mcp.search_sync.has_time", lambda *_: False)
        first = reader.search_mail(**filters)
    assert first["next_cursor"]
    reader.search_index.remove("INBOX", "1", list(fake.folders["INBOX"]))
    metadata = reader._metadata
    interrupted = []

    def fail_source(connection, uids, **kwargs):
        if fake.current == "All Mail" and kwargs.get("envelope"):
            interrupted.append(connection)
            raise CallDeadlineExceeded("Simulated optional mapping timeout")
        assert connection not in interrupted
        return metadata(connection, uids, **kwargs)

    monkeypatch.setattr(reader, "_metadata", fail_source)
    fake.calls.clear()

    result = reader.search_mail(cursor=first["next_cursor"], **filters)

    assert len(interrupted) == 1
    assert [row["uid"] for row in result["messages"]] == ["4004"]
    assert result["next_cursor"] and result["total"] == 5
    assert not result["partial"] and "partial_reason" not in result
    assert sum(call[0] == "LOGIN" for call in fake.calls) == 2
    assert "applied_since" not in result
