"""Growing All Mail epochs resume through fake-only, read-only header batches."""

import logging
import sqlite3
from dataclasses import replace

import pytest
from test_mail import FakeImap, client, message
from test_mail import settings as settings

from proton_mail_mcp.mail import CallDeadlineExceeded, MailError
from proton_mail_mcp.search_sync import sync_growing_headers

_OLD_OBSERVED = "2026-10-28T16:41:00+00:00"
_CONTENT = "private-subject-do-not-log"
_ADDRESS = "private-sender@example.invalid"
_BODY = "private-body-do-not-log"


class RangeImap(FakeImap):
    """Accept the bounded UID range without parsing historical mail headers."""

    count_offset = 0

    def select(self, folder, *, readonly):
        status, data = super().select(folder, readonly=readonly)
        return status, [str(int(data[0]) + self.count_offset).encode()]

    def status(self, folder, fields):
        status, data = super().status(folder, fields)
        if self.count_offset:
            data = [
                data[0].replace(b"(MESSAGES 1 ", f"(MESSAGES {self.count_offset + 1} ".encode(), 1)
            ]
        return status, data

    def uid(self, command, *args):
        if command == "SEARCH" and args[2:3] == ("UID",) and args[3] != "1:*":
            assert args[:2] == ("CHARSET", "UTF-8")
            assert len(args) == 4
            low, high = map(int, args[3].split(":"))
            assert low <= high
            self.calls.append((command, *args))
            found = sorted(
                (uid for uid in self.folders[self.current] if low <= int(uid) <= high),
                key=int,
            )
            return "OK", [" ".join(found).encode()]
        return super().uid(command, *args)


def seed_baseline(reader, uids, *, uidnext=None, bulk=False):
    index = reader.search_index
    uidnext = uidnext or str(max(map(int, uids)) + 1)
    if bulk:
        # Empty indexed text is enough for the epoch/count regression. Bulk SQL
        # avoids constructing, parsing or tokenizing 47,468 fake messages.
        first, *remaining = uids
        index.observe("All Mail", "1", {first: {"headers": {}}})
        with sqlite3.connect(index.path) as database:
            database.executemany(
                "INSERT INTO messages(folder, uidvalidity, uid, headers_known) VALUES (?, ?, ?, ?)",
                (("All Mail", "1", uid, 1) for uid in remaining),
            )
    else:
        index.observe("All Mail", "1", {uid: {"headers": {}} for uid in uids})
    index.reconcile("All Mail", "1", uids)
    assert index.record_coverage("All Mail", "1", {"exists": len(uids), "uidnext": uidnext}, uids)
    with sqlite3.connect(index.path) as database:
        database.execute(
            "UPDATE index_sync SET observed_at = ?, last_catchup_at = ? WHERE folder = ?",
            (_OLD_OBSERVED, _OLD_OBSERVED, "All Mail"),
        )
    return index.reporting_status()


def catchup(reader, **kwargs):
    deadline = reader._deadline()
    with reader._session(deadline=deadline) as (connection, _):
        state = reader._select(connection, "All Mail")
        return sync_growing_headers(reader, connection, deadline, "All Mail", state, **kwargs)


def header_fetches(fake):
    return [call for call in fake.calls if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]]


def range_searches(fake):
    return [call for call in fake.calls if call[0] == "SEARCH" and call[3:4] == ("UID",)]


def assert_read_only(fake, *, enriched=True):
    assert all(call[2] is True for call in fake.calls if call[0] == "EXAMINE")
    assert not {"SELECT", "STORE", "APPEND", "EXPUNGE"}.intersection(call[0] for call in fake.calls)
    for call in header_fetches(fake):
        assert "BODY.PEEK[HEADER.FIELDS" in call[2]
        assert "INTERNALDATE" in call[2]
        if enriched:
            assert "ENVELOPE" in call[2] and "RFC822.SIZE" in call[2]
        assert "BODYSTRUCTURE" not in call[2]
        assert "BODY[" not in call[2]


def test_grew_only_catchup_advances_observation_then_repeated_call_is_noop(settings, caplog):
    fake = RangeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in (1, 3, 4, 5)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    baseline = seed_baseline(reader, ["1", "3"], uidnext="4")
    caplog.set_level(logging.INFO, logger="proton_mail_mcp.search_sync")

    synced = catchup(reader, batch_size=1)

    assert synced is not None and synced.complete
    assert synced.fetched == {"4", "5"}
    assert reader.search_index.coverage("All Mail") == ("1", 4, "6")
    observed = reader.search_index.reporting_status()
    assert observed["indexed_count"] == observed["all_mail_count"] == 4
    assert observed["indexed_max_uid"] == observed["all_mail_max_uid"] == "5"
    assert observed["all_mail_observed_at"] == observed["last_catchup_at"]
    assert observed["last_catchup_at"] != baseline["last_catchup_at"]
    assert observed["last_expunge_reconciliation_at"] == baseline["last_expunge_reconciliation_at"]
    assert [call[1] for call in header_fetches(fake)] == ["4", "5"]
    assert range_searches(fake) == [("SEARCH", "CHARSET", "UTF-8", "UID", "4:5")]
    assert_read_only(fake)
    fake.calls.clear()

    repeated = catchup(reader)

    assert repeated is not None and repeated.complete and not repeated.fetched
    assert reader.search_index.reporting_status() == observed
    assert not [call for call in fake.calls if call[0] in {"SEARCH", "FETCH"}]
    assert "grew" in caplog.text and "incremental" in caplog.text
    assert "unchanged" in caplog.text and "noop" in caplog.text


@pytest.mark.parametrize("change", ["uidvalidity", "count", "uidnext", "hidden_expunge"])
def test_epoch_change_or_shrink_keeps_existing_fallback(settings, caplog, change):
    fake = RangeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in (1, 2)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    baseline = seed_baseline(reader, ["1", "2"])
    if change == "uidvalidity":
        fake.uidvalidity = "2"
    elif change == "count":
        del fake.folders["All Mail"]["1"]
        fake.next_uids["All Mail"] = 3
    elif change == "uidnext":
        # A decreasing UIDNEXT is unsafe even when the count is unchanged.
        with sqlite3.connect(reader.search_index.path) as database:
            database.execute("UPDATE coverage SET uidnext = ? WHERE folder = ?", ("4", "All Mail"))
    else:
        del fake.folders["All Mail"]["1"]
        fake.folders["All Mail"]["3"] = message("<arrival@test.invalid>")
    caplog.set_level(logging.INFO, logger="proton_mail_mcp.search_sync")

    assert catchup(reader) is None

    assert not header_fetches(fake)
    if change != "uidvalidity":
        assert reader.search_index.reporting_status() == baseline
    assert "fallback" in caplog.text
    if change == "uidvalidity":
        assert "uidvalidity_changed" in caplog.text
    elif change in {"count", "uidnext"}:
        assert "shrank" in caplog.text
    assert_read_only(fake)


def test_uidnext_growth_without_visible_arrivals_advances_observation(settings):
    fake = RangeImap({"All Mail": {"1": message("<1@test.invalid>")}}, uidnext=True)
    fake.next_uids["All Mail"] = 5
    reader = client(settings, fake)
    seed_baseline(reader, ["1"], uidnext="2")

    synced = catchup(reader)

    assert synced is not None and synced.complete and not synced.fetched
    assert reader.search_index.coverage("All Mail") == ("1", 1, "5")
    observed = reader.search_index.reporting_status()
    assert observed["all_mail_observed_at"] == observed["last_catchup_at"] != _OLD_OBSERVED
    assert range_searches(fake) == [("SEARCH", "CHARSET", "UTF-8", "UID", "2:4")]
    assert not header_fetches(fake)


def test_capped_multibatch_catchup_resumes_without_advancing_partial_baseline(settings):
    settings = replace(settings, search_index_catchup_max_messages=2)
    fake = RangeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in range(1, 7)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    baseline = seed_baseline(reader, ["1"])

    first = catchup(reader, batch_size=1)

    assert first is not None and not first.complete and first.fetched == {"2", "3"}
    assert reader.search_index.coverage("All Mail") is None
    assert reader.search_index.coverage_epoch("All Mail") == ("1", 1, "2")
    partial = reader.search_index.reporting_status()
    assert partial["indexed_count"] == 3
    assert partial["all_mail_count"] == 1
    assert partial["all_mail_observed_at"] == baseline["all_mail_observed_at"]
    assert partial["last_catchup_at"] == baseline["last_catchup_at"]
    fake.calls.clear()

    second = catchup(reader, batch_size=1)

    assert second is not None and not second.complete and second.fetched == {"4", "5"}
    assert [call[1] for call in header_fetches(fake)] == ["4", "5"]
    fake.calls.clear()

    third = catchup(reader, batch_size=1)

    assert third is not None and third.complete and third.fetched == {"6"}
    assert [call[1] for call in header_fetches(fake)] == ["6"]
    assert reader.search_index.coverage("All Mail") == ("1", 6, "7")
    final = reader.search_index.reporting_status()
    assert final["indexed_count"] == final["all_mail_count"] == 6
    assert final["all_mail_observed_at"] == final["last_catchup_at"] != _OLD_OBSERVED
    assert_read_only(fake)


def test_expunged_partial_tail_header_is_reconciled_before_certification(settings):
    settings = replace(settings, search_index_catchup_max_messages=1)
    fake = RangeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in (1, 2, 3)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    seed_baseline(reader, ["1"])

    first = catchup(reader)

    assert first is not None and not first.complete and first.fetched == {"2"}
    del fake.folders["All Mail"]["2"]
    fake.calls.clear()

    second = catchup(reader)

    assert second is not None and second.complete and second.fetched == {"3"}
    assert reader.search_index.known_uids("All Mail", "1") == {"1", "3"}
    assert reader.search_index.coverage("All Mail") == ("1", 2, "4")
    assert [call[1] for call in header_fetches(fake)] == ["3"]
    assert_read_only(fake)


def test_tail_reconciliation_is_bounded_to_its_mailbox_and_uid_range(settings):
    fake = RangeImap({"All Mail": {"1": message("<1@test.invalid>")}}, uidnext=True)
    reader = client(settings, fake)
    seed_baseline(reader, ["1"])
    index = reader.search_index
    index.observe("All Mail", "1", {uid: {"headers": {}} for uid in ("2", "3", "4")})
    index.observe("INBOX", "1", {"2": {"headers": {}}})

    # UID 4 is outside the authoritative [2, 4) range and must survive, even
    # though its extra header prevents this snapshot from being certified.
    assert not index.record_coverage(
        "All Mail",
        "1",
        {"exists": 2, "uidnext": "4"},
        ["1", "3"],
        catchup=True,
        tail_range=(2, 4),
    )

    assert index.known_uids("All Mail", "1") == {"1", "3", "4"}
    assert index.known_uids("INBOX", "1") == {"2"}
    assert index.coverage("All Mail") is None


def test_tail_reconciliation_rolls_back_with_coverage_and_reporting(settings, monkeypatch):
    fake = RangeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in (1, 3)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    baseline = seed_baseline(reader, ["1"])
    reader.search_index.observe("All Mail", "1", {"2": {"headers": {}}})

    def fail_reporting(*args, **kwargs):
        raise sqlite3.OperationalError(_CONTENT)

    monkeypatch.setattr(reader.search_index, "_record_inventory", fail_reporting)

    failed = catchup(reader)

    assert failed is not None and not failed.complete and failed.fetched == {"3"}
    with sqlite3.connect(reader.search_index.path) as database:
        assert database.execute(
            "SELECT uid FROM messages WHERE folder = ? ORDER BY uid", ("All Mail",)
        ).fetchall() == [("1",), ("2",), ("3",)]
        assert database.execute(
            "SELECT message_count, uidnext FROM coverage WHERE folder = ?", ("All Mail",)
        ).fetchone() == (1, "2")
        assert database.execute(
            "SELECT observed_at FROM index_sync WHERE folder = ?", ("All Mail",)
        ).fetchone() == (baseline["all_mail_observed_at"],)

    restarted = client(settings, fake)
    resumed = catchup(restarted)

    assert resumed is not None and resumed.complete and not resumed.fetched
    assert restarted.search_index.known_uids("All Mail", "1") == {"1", "3"}
    assert restarted.search_index.coverage("All Mail") == ("1", 2, "4")


def test_catchup_without_sync_row_does_not_invent_an_all_reconciliation_timestamp(settings):
    fake = RangeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in (1, 2)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    seed_baseline(reader, ["1"])
    with sqlite3.connect(reader.search_index.path) as database:
        database.execute("DELETE FROM index_sync WHERE folder = ?", ("All Mail",))
    reader = client(settings, fake)

    synced = catchup(reader)

    assert synced is not None and synced.complete
    observed = reader.search_index.reporting_status()
    assert observed["last_catchup_at"]
    assert observed["last_expunge_reconciliation_at"] is None
    assert not [call for call in fake.calls if call[0] == "SEARCH" and call[3:] == ("ALL",)]
    reader.search_index.reconcile("All Mail", "1", ["1", "2"])
    assert reader.search_index.reporting_status()["last_expunge_reconciliation_at"]


@pytest.mark.parametrize("failure", [MailError, CallDeadlineExceeded])
def test_error_after_batch_preserves_progress_and_logs_no_mail_content(
    settings, monkeypatch, caplog, failure
):
    private = message(
        "<private-message-id@example.invalid>", subject=_CONTENT, sections={"1": _BODY.encode()}
    )
    private.headers = private.headers.replace(b"sender@example.com", _ADDRESS.encode())
    fake = RangeImap({"All Mail": {str(uid): private for uid in (1, 2, 3)}}, uidnext=True)
    reader = client(settings, fake)
    baseline = seed_baseline(reader, ["1"])
    original_metadata = reader._metadata
    calls = 0

    def metadata(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure(f"{_CONTENT} {_ADDRESS} {_BODY}")
        return original_metadata(*args, **kwargs)

    monkeypatch.setattr(reader, "_metadata", metadata)
    caplog.set_level(logging.INFO, logger="proton_mail_mcp.search_sync")

    interrupted = catchup(reader, batch_size=1)

    assert interrupted is not None and not interrupted.complete
    assert interrupted.fetched == {"2"}
    if failure is CallDeadlineExceeded:
        assert interrupted.timed_out and interrupted.stream_broken
    assert reader.search_index.coverage_epoch("All Mail") == ("1", 1, "2")
    partial = reader.search_index.reporting_status()
    assert partial["indexed_count"] == 2
    assert partial["all_mail_count"] == baseline["all_mail_count"]
    assert partial["last_catchup_at"] == baseline["last_catchup_at"]
    assert partial["all_mail_observed_at"] == baseline["all_mail_observed_at"]
    assert "error" in caplog.text
    for content in (
        _CONTENT,
        _ADDRESS,
        _BODY,
        "private-message-id",
        "sender@example.com",
        "All Mail",
    ):
        assert content not in caplog.text
    monkeypatch.setattr(reader, "_metadata", original_metadata)
    fake.calls.clear()

    resumed = catchup(reader, batch_size=1)

    assert resumed is not None and resumed.complete and resumed.fetched == {"3"}
    assert [call[1] for call in header_fetches(fake)] == ["3"]
    assert reader.search_index.coverage("All Mail") == ("1", 3, "4")
    for content in (_CONTENT, _ADDRESS, _BODY, "private-message-id", "All Mail"):
        assert content not in caplog.text
    assert_read_only(fake)


def test_live_stuck_47468_headers_max_uid_47477_catches_up(settings):
    fake = RangeImap({"All Mail": {"47478": message("<arrival@test.invalid>")}}, uidnext=True)
    fake.count_offset = 47468
    reader = client(settings, fake)
    historical = [str(uid) for uid in range(1, 47468)] + ["47477"]
    before = seed_baseline(reader, historical, uidnext="47478", bulk=True)
    assert before["indexed_count"] == 47468 and before["indexed_max_uid"] == "47477"

    synced = catchup(reader)

    assert synced is not None and synced.complete and synced.fetched == {"47478"}
    assert reader.search_index.coverage("All Mail") == ("1", 47469, "47479")
    observed = reader.search_index.reporting_status()
    assert observed["indexed_count"] == observed["all_mail_count"] == 47469
    assert observed["indexed_max_uid"] == observed["all_mail_max_uid"] == "47478"
    assert observed["all_mail_observed_at"] == observed["last_catchup_at"] != _OLD_OBSERVED
    assert range_searches(fake) == [("SEARCH", "CHARSET", "UTF-8", "UID", "47478:47478")]
    assert [call[1] for call in header_fetches(fake)] == ["47478"]
    assert_read_only(fake)


@pytest.mark.parametrize("write", ["missing", "unsafe_header"])
def test_missing_or_unsafe_header_write_never_advances_observation(settings, monkeypatch, write):
    arrival = message("<new@test.invalid>")
    if write == "unsafe_header":
        arrival.headers = arrival.headers.replace(b"Subject: Hello", b"Subject: Unsafe\x00header")
    fake = RangeImap({"All Mail": {"1": message("<old@test.invalid>"), "2": arrival}}, uidnext=True)
    reader = client(settings, fake)
    baseline = seed_baseline(reader, ["1"])
    original_observe = reader.search_index.observe
    if write == "missing":
        monkeypatch.setattr(reader.search_index, "observe", lambda *args, **kwargs: None)

    incomplete = catchup(reader)

    assert incomplete is not None and not incomplete.complete and incomplete.fetched == {"2"}
    assert reader.search_index.coverage("All Mail") is None
    assert reader.search_index.coverage_epoch("All Mail") == ("1", 1, "2")
    assert reader.search_index.reporting_status() == baseline
    monkeypatch.setattr(reader.search_index, "observe", original_observe)
    fake.folders["All Mail"]["2"] = message("<new@test.invalid>")
    fake.calls.clear()

    retried = catchup(reader)

    assert retried is not None and retried.complete and retried.fetched == {"2"}
    assert [call[1] for call in header_fetches(fake)] == ["2"]
    assert reader.search_index.coverage("All Mail") == ("1", 2, "3")


def test_observation_and_coverage_roll_back_together_on_database_failure(settings, monkeypatch):
    fake = RangeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in (1, 2)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    baseline = seed_baseline(reader, ["1"])
    original_inventory = reader.search_index._record_inventory

    def fail_after_inventory(*args, **kwargs):
        original_inventory(*args, **kwargs)
        raise sqlite3.OperationalError(_CONTENT)

    monkeypatch.setattr(reader.search_index, "_record_inventory", fail_after_inventory)

    incomplete = catchup(reader)

    assert incomplete is not None and not incomplete.complete and incomplete.fetched == {"2"}
    # The simulated failure disables this optional index object. Read the saved
    # state directly to verify that both writes shared a rolled-back transaction.
    with sqlite3.connect(reader.search_index.path) as database:
        assert database.execute(
            "SELECT uidvalidity, message_count, uidnext FROM coverage WHERE folder = ?",
            ("All Mail",),
        ).fetchone() == ("1", 1, "2")
        assert database.execute(
            "SELECT message_count, max_uid, observed_at, last_catchup_at "
            "FROM index_sync WHERE folder = ?",
            ("All Mail",),
        ).fetchone() == (
            baseline["all_mail_count"],
            baseline["all_mail_max_uid"],
            baseline["all_mail_observed_at"],
            baseline["last_catchup_at"],
        )
        assert database.execute(
            "SELECT count(*) FROM messages WHERE folder = ? AND headers_known = ?",
            ("All Mail", 1),
        ).fetchone() == (2,)
    fake.calls.clear()
    restarted = client(settings, fake)

    retried = catchup(restarted)

    assert retried is not None and retried.complete and not retried.fetched
    assert not header_fetches(fake)
    assert restarted.search_index.coverage("All Mail") == ("1", 2, "3")


def test_arrival_during_header_fetch_defers_observation_until_next_call(settings, monkeypatch):
    fake = RangeImap(
        {"All Mail": {str(uid): message(f"<{uid}@test.invalid>") for uid in (1, 2)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    baseline = seed_baseline(reader, ["1"])
    original_metadata = reader._metadata

    def metadata(*args, **kwargs):
        result = original_metadata(*args, **kwargs)
        fake.folders["All Mail"]["3"] = message("<late@test.invalid>")
        return result

    monkeypatch.setattr(reader, "_metadata", metadata)

    incomplete = catchup(reader)

    assert incomplete is not None and not incomplete.complete and incomplete.fetched == {"2"}
    assert reader.search_index.coverage_epoch("All Mail") == ("1", 1, "2")
    partial = reader.search_index.reporting_status()
    assert partial["all_mail_count"] == baseline["all_mail_count"]
    assert partial["all_mail_observed_at"] == baseline["all_mail_observed_at"]
    assert partial["last_catchup_at"] == baseline["last_catchup_at"]
    monkeypatch.setattr(reader, "_metadata", original_metadata)
    fake.calls.clear()

    retried = catchup(reader)

    assert retried is not None and retried.complete and retried.fetched == {"3"}
    assert [call[1] for call in header_fetches(fake)] == ["3"]
    assert reader.search_index.coverage("All Mail") == ("1", 3, "4")


def test_search_mail_runs_incremental_catchup_when_all_mail_grows(settings, monkeypatch):
    old = message("<old@test.invalid>", subject="Old indexed message")
    fake = RangeImap({"All Mail": {"1": old}, "INBOX": {"10": old}}, uidnext=True)
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    arrival = message("<new@test.invalid>", subject="New matching message")
    fake.folders["All Mail"]["2"] = arrival
    fake.folders["INBOX"]["20"] = arrival
    fake.calls.clear()

    def resolve(client, connection, deadline, handles, **kwargs):
        return [{**handle, "folder": "INBOX"} for handle in handles]

    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_views", resolve)

    found = reader.search_mail(folder="All Mail", subject="matching", include_snippets=False)

    assert [item["message_id"] for item in found["messages"]] == ["<new@test.invalid>"]
    assert "partial_reason" not in found
    assert not found["partial"] and found["total_is_exact"]
    assert reader.search_index.coverage("All Mail") == ("1", 2, "3")
    assert range_searches(fake) == [("SEARCH", "CHARSET", "UTF-8", "UID", "2:2")]
    assert not [call for call in fake.calls if call[0] == "SEARCH" and call[3:] == ("ALL",)]
    assert_read_only(fake, enriched=False)
