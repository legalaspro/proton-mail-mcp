"""Background rebuilds use offline IMAP doubles and private temporary indexes."""

import hashlib
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import datetime

import pytest
from test_mail import FakeImap, client, message
from test_mail import settings as settings

from proton_mail_mcp.action_log import ActionLog, new_record
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.reindex import ReindexService
from proton_mail_mcp.search_index import SearchIndex
from proton_mail_mcp.search_sync import sync_selected_headers


class ReindexImap(FakeImap):
    def __init__(self, count=3, *, skip=(), block=False, failure=False, final=None):
        self.items = {str(i): message(f"<{i}@example.com>") for i in range(1, count + 1)}
        super().__init__({"All Mail": self.items}, uidnext=True)
        self.skip = set(skip)
        self.block = block
        self.failure = failure
        self.final = final
        self.searches = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def uid(self, command, *args):
        if command == "SEARCH":
            self.searches += 1
            if self.searches == 2 and self.final is not None:
                self.final(self)
        if command == "FETCH":
            self.entered.set()
            if self.block:
                assert self.release.wait(5), "test did not release its fake FETCH"
            if self.failure:
                raise RuntimeError("private-server-content-must-not-escape")
            remaining = [uid for uid in args[0].split(",") if uid not in self.skip]
            if not remaining:
                self.calls.append((command, *args))
                return "OK", []
            args = (",".join(remaining), *args[1:])
        return super().uid(command, *args)


def service_for(settings, fake, **options):
    settings = replace(settings, reindex_batch_size=100, reindex_batch_sleep=0.01, **options)
    reader = client(settings, fake)
    return reader, ReindexService(reader)


def completed(service):
    thread = service._thread
    assert thread is not None
    thread.join(5)
    assert not thread.is_alive(), "background rebuild did not finish"
    return service.status()


def seed_old(reader):
    reader.search_index.observe("Unrelated folder", "7", {"9": {"headers": {"Subject": "Old"}}})
    assert reader.search_index.enabled
    return reader.search_index.path.read_bytes()


def assert_no_candidate(reader):
    assert list(reader.search_index.path.parent.glob("*.reindex-*")) == []


def assert_read_only(fake):
    assert {call[0] for call in fake.calls} <= {
        "STARTTLS",
        "LOGIN",
        "CAPABILITY",
        "LIST",
        "EXAMINE",
        "STATUS",
        "SEARCH",
        "FETCH",
        "LOGOUT",
    }
    assert all(call[2] is True for call in fake.calls if call[0] == "EXAMINE")
    for call in fake.calls:
        if call[0] == "FETCH":
            assert "BODY.PEEK[HEADER.FIELDS" in call[2]
            assert "BODYSTRUCTURE" not in call[2]
            assert "FLAGS" not in call[2]
            assert "INTERNALDATE" in call[2]
            assert "ENVELOPE" in call[2] and "RFC822.SIZE" in call[2]


def test_preview_requires_fresh_single_use_token_and_records_safe_coverage(settings):
    reader, service = service_for(settings, ReindexImap())
    original = seed_old(reader)
    with pytest.raises(MailValidationError, match="preview token"):
        service.execute(None)
    with pytest.raises(MailValidationError, match="preview token"):
        service.execute("invented")

    preview = service.preview()
    assert preview["indexed_count"] == 0
    assert preview["all_mail_count"] == preview["missing_count"] == 3
    assert preview["eta_seconds"] > 0 and preview["ttl_seconds"] == 300
    assert preview["eta_is_estimate"] is True
    assert reader.search_index.path.read_bytes() == original
    assert reader.search_index.has_mailbox("All Mail") is False
    result = service.execute(preview["token"])
    assert result["run_id"] and result["log_id"] != preview["log_id"]
    assert completed(service)["state"] == "succeeded"
    assert reader.search_index.coverage("All Mail") == ("1", 3, "4")
    with pytest.raises(MailValidationError, match="already used"):
        service.execute(preview["token"])
    text = service.log.path.read_text()
    assert preview["token"] not in text
    assert "private-server-content" not in text
    with service.log.transaction(readonly=True) as transaction:
        records = transaction.records()
    assert {row["action"] for row in records} >= {
        "reindex_preview",
        "reindex_execute",
        "reindex_result",
    }
    final = next(row for row in records if row["action"] == "reindex_result")
    assert final["run_id"] == result["run_id"]
    assert final["action_log_id"] == result["log_id"]


def test_expired_preview_token_is_refused_without_starting_worker(settings):
    _, service = service_for(settings, ReindexImap())
    preview = service.preview()
    digest = hashlib.sha256(preview["token"].encode()).hexdigest()
    service._tokens[digest] = time.monotonic() - 1
    with pytest.raises(MailValidationError, match="expired"):
        service.execute(preview["token"])
    assert service.status()["state"] == "idle"
    assert service._thread is None


@pytest.mark.parametrize("missing,succeeds", [(1, True), (2, False)])
def test_swap_requires_at_least_99_percent_and_keeps_old_on_refusal(settings, missing, succeeds):
    fake = ReindexImap(100, skip={str(i) for i in range(1, missing + 1)})
    reader, service = service_for(settings, fake)
    original = seed_old(reader)
    result = service.execute(service.preview()["token"])
    status = completed(service)
    assert status["run_id"] == result["run_id"]
    assert status["last_full_run"]["new_count"] == 100 - missing
    assert status["last_full_run"]["all_mail_count"] == 100
    assert status["last_full_run"]["old_count"] == 0
    assert status["state"] == ("succeeded" if succeeds else "failed")
    assert status["swap_outcome"] == ("swapped" if succeeds else "kept_old")
    if succeeds:
        assert reader.search_index.generation == 1
        assert reader.search_index.known_count("All Mail", "1") == 99
        assert reader.search_index.coverage("All Mail") is None
        assert reader.search_index.known_uids("Unrelated folder", "7") == set()
        assert status["swap_reason"] is status["last_error"] is None
        assert status["percent_done"] == 100 and status["eta_seconds"] == 0
    else:
        assert reader.search_index.path.read_bytes() == original
        assert reader.search_index.generation == 0
        assert status["swap_reason"] == status["last_error"] == "coverage_below_99_percent"
        assert status["percent_done"] > 0
    assert_no_candidate(reader)
    assert_read_only(fake)


def test_single_flight_and_cancel_leave_old_index_untouched(settings):
    fake = ReindexImap(block=True)
    reader, service = service_for(settings, fake)
    original = seed_old(reader)
    preview = service.preview()
    running = service.execute(preview["token"])
    assert fake.entered.wait(3)
    assert service.running is True
    assert service.status()["state"] == "running"
    assert reader.search_index.path.read_bytes() == original
    with pytest.raises(MailValidationError, match="not running"):
        service.cancel("wrong-run")
    refused = service.execute(preview["token"])
    assert refused["error"] == "already running" and refused["run_id"] == running["run_id"]
    assert refused["state"] == "running"
    calls = list(fake.calls)
    busy_preview = service.preview()
    assert busy_preview == {
        "state": "running",
        "run_id": running["run_id"],
        "error": "already running",
    }
    assert fake.calls == calls
    cancelled = service.cancel(running["run_id"])
    assert cancelled["cancel_requested"] is True
    fake.release.set()
    status = completed(service)
    assert status["state"] == "cancelled" and service.running is False
    assert status["swap_outcome"] == "kept_old" and status["swap_reason"] == "cancelled"
    assert status["last_error"] is None
    assert reader.search_index.path.read_bytes() == original
    assert_no_candidate(reader)
    assert_read_only(fake)


def test_candidate_creation_does_not_warn_that_the_new_candidate_is_missing(settings, caplog):
    reader, _ = service_for(settings, ReindexImap())
    caplog.clear()
    candidate = reader.search_index.create_candidate(reader.settings, "a" * 32)
    try:
        assert candidate.enabled and candidate._initialized
        assert "Search index fallback (missing)" not in caplog.text
    finally:
        candidate.discard()


def interrupted_intent(reader, run_id, **fields):
    record = new_record("reindex_execute", {})
    record.update(run_id=run_id, state="running", index_key=reader.search_index.path.name)
    record.update(fields)
    ActionLog(reader.settings.action_log_path).audit(record)
    return record


def results_for(reader):
    with ActionLog(reader.settings.action_log_path).transaction(readonly=True) as transaction:
        return [row for row in transaction.records() if row["action"] == "reindex_result"]


def test_startup_reclaims_exact_stale_candidates_orphan_locks_and_sidecars(settings):
    reader, _ = service_for(settings, ReindexImap())
    live = reader.search_index
    seed_old(reader)
    candidate = live.create_candidate(reader.settings, "a" * 32)
    candidate.release_candidate()  # Simulate the dead process dropping its OS leases.
    candidate.path.with_name(candidate.path.name + "-journal").touch(mode=0o600)
    orphan = live.path.with_name(live.path.name + ".reindex-" + "b" * 32 + ".lock")
    orphan.touch(mode=0o600)
    malformed = live.path.with_name(live.path.name + ".reindex-malformed")
    malformed.touch(mode=0o600)
    foreign = live.path.with_name("f" * 64 + ".sqlite3.reindex-" + "c" * 32)
    foreign.touch(mode=0o600)
    original = live.path.read_bytes()

    client(reader.settings, ReindexImap())

    assert live.path.read_bytes() == original
    assert not candidate.path.exists()
    assert not candidate.path.with_name(candidate.path.name + ".lock").exists()
    assert not candidate.path.with_name(candidate.path.name + "-journal").exists()
    assert not orphan.exists()
    assert malformed.exists() and foreign.exists()


def test_startup_skips_held_candidate_lock_and_recovers_after_it_is_released(settings):
    reader, _ = service_for(settings, ReindexImap())
    candidate = reader.search_index.create_candidate(reader.settings, "a" * 32)
    intent = interrupted_intent(reader, "a" * 32)
    client(reader.settings, ReindexImap())
    assert candidate.path.exists()
    assert results_for(reader) == []

    candidate.release_candidate()
    client(reader.settings, ReindexImap())
    assert not candidate.path.exists()
    (result,) = results_for(reader)
    assert result["state"] == "interrupted"
    assert result["action_log_id"] == intent["log_id"]


def test_startup_recovers_missing_candidate_intents_once_and_scopes_accounts(settings):
    reader, _ = service_for(settings, ReindexImap())
    tagged = interrupted_intent(reader, "a" * 32, index_key=reader.search_index.path.name)
    legacy = new_record("reindex_execute", {})
    legacy.update(run_id="b" * 32, state="running")
    ActionLog(reader.settings.action_log_path).audit(legacy)
    interrupted_intent(reader, "c" * 32, index_key="foreign-account.sqlite3")
    foreign_intent = interrupted_intent(reader, "d" * 32, index_key="f" * 64 + ".sqlite3")
    completed_intent = interrupted_intent(reader, "e" * 32)
    done = new_record("reindex_result", {})
    done.update(action_log_id=completed_intent["log_id"], run_id="e" * 32, state="succeeded")
    ActionLog(reader.settings.action_log_path).audit(done)
    parent = reader.search_index.path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    foreign = parent / ("f" * 64 + ".sqlite3.reindex-" + foreign_intent["run_id"])
    foreign.touch(mode=0o600)

    client(reader.settings, ReindexImap())
    client(reader.settings, ReindexImap())

    results = results_for(reader)
    recovered = [row for row in results if row["state"] == "interrupted"]
    assert {row["action_log_id"] for row in recovered} == {tagged["log_id"]}
    assert len(results) == 2
    assert all(row["swap_outcome"] == "unknown" for row in recovered)
    assert foreign.exists()


def test_startup_does_not_interrupt_peer_run_before_candidate_creation(settings, monkeypatch):
    reader, service = service_for(settings, ReindexImap())
    entered, release = threading.Event(), threading.Event()
    original_create = reader.search_index.create_candidate

    def create(*args):
        entered.set()
        assert release.wait(5)
        return original_create(*args)

    monkeypatch.setattr(reader.search_index, "create_candidate", create)
    service.execute(service.preview()["token"])
    assert entered.wait(3)
    try:
        client(reader.settings, ReindexImap())
        assert results_for(reader) == []
    finally:
        release.set()
    assert completed(service)["state"] == "succeeded"


def test_startup_retries_files_left_by_failed_discard(settings, monkeypatch):
    reader, service = service_for(settings, ReindexImap(failure=True))
    original_discard = SearchIndex.discard

    def discard(candidate, **kwargs):
        if candidate._candidate_lock is not None:
            raise OSError("private-cleanup-details")
        return original_discard(candidate, **kwargs)

    monkeypatch.setattr(SearchIndex, "discard", discard)
    service.execute(service.preview()["token"])
    assert completed(service)["last_error"] == "candidate_cleanup_failed"
    assert list(reader.search_index.path.parent.glob("*.reindex-*"))
    client(reader.settings, ReindexImap())
    assert_no_candidate(reader)
    assert "private-cleanup-details" not in service.log.path.read_text()


def test_cancel_wakes_rate_limit_sleep_before_another_fetch(settings):
    fake = ReindexImap(count=3)
    reader = client(replace(settings, reindex_batch_size=1, reindex_batch_sleep=60), fake)
    service = ReindexService(reader)
    original = seed_old(reader)
    run = service.execute(service.preview()["token"])
    assert fake.entered.wait(3)
    service.cancel(run["run_id"])
    assert completed(service)["state"] == "cancelled"
    assert len([call for call in fake.calls if call[0] == "FETCH"]) == 1
    assert reader.search_index.path.read_bytes() == original


@pytest.mark.parametrize("change", ["arrival", "expunge_and_arrival", "epoch", "incomplete"])
def test_final_inventory_and_epoch_are_authoritative(settings, change):
    fake = ReindexImap(100)
    original_select = fake.select
    selections = []

    def select(folder, *, readonly):
        selections.append(folder)
        if len(selections) == 3:
            if change in {"arrival", "expunge_and_arrival"}:
                if change == "expunge_and_arrival":
                    for uid in ("1", "2", "3"):
                        del fake.items[uid]
                for uid in ("101", "102", "103"):
                    fake.items[uid] = message(f"<{uid}@example.com>")
            elif change == "epoch":
                fake.uidvalidity = "2"
        return original_select(folder, readonly=readonly)

    fake.select = select
    original_uid = fake.uid

    def uid(command, *args):
        response = original_uid(command, *args)
        if change == "incomplete" and command == "SEARCH" and fake.searches == 3:
            return "OK", [b"1 2"]
        return response

    fake.uid = uid
    reader, service = service_for(settings, fake)
    original = seed_old(reader)
    service.execute(service.preview()["token"])
    status = completed(service)
    assert status["state"] == "failed"
    assert (
        status["swap_reason"]
        == {
            "arrival": "coverage_below_99_percent",
            "expunge_and_arrival": "coverage_below_99_percent",
            "epoch": "uidvalidity_changed",
            "incomplete": "incomplete_inventory",
        }[change]
    )
    assert reader.search_index.path.read_bytes() == original
    assert_no_candidate(reader)


def test_worker_error_is_safe_and_status_always_has_required_fields(settings):
    fake = ReindexImap(failure=True)
    reader, service = service_for(settings, fake)
    required = {
        "state",
        "percent_done",
        "eta_seconds",
        "last_error",
        "last_full_run",
        "indexed_count",
        "indexed_max_uid",
        "all_mail_count",
        "all_mail_max_uid",
        "all_mail_observed_at",
        "last_catchup_at",
        "last_expunge_reconciliation_at",
        "run_id",
        "swap_outcome",
        "swap_reason",
    }
    assert set(service.status()) == required
    assert service.status()["state"] == "idle"
    original = seed_old(reader)
    service.execute(service.preview()["token"])
    status = completed(service)
    assert set(status) == required
    assert status["state"] == "failed" and status["last_error"] == "rebuild_failed"
    assert reader.search_index.path.read_bytes() == original
    assert_no_candidate(reader)
    assert "private-server-content" not in service.log.path.read_text()


def test_idle_status_reports_local_all_mail_counts_after_warmup_and_restart(settings):
    fake = ReindexImap()
    reader, service = service_for(settings, fake)
    assert service.status()["last_full_run"] is None
    assert reader.rebuild_search_index("All Mail")["indexed"] == 3
    # Other folders contain duplicate labels; status must not add their rows.
    reader.search_index.observe("INBOX", "1", {"1": {"headers": {"Subject": "Duplicate"}}})
    calls = list(fake.calls)
    status = service.status()
    assert status["state"] == "idle"
    assert status["last_full_run"] is None
    assert status["indexed_count"] == status["all_mail_count"] == 3
    assert status["indexed_max_uid"] == status["all_mail_max_uid"] == "3"
    assert fake.calls == calls

    unopened = ReindexImap(999)
    restarted = client(reader.settings, unopened)
    restored = restarted.reindex_status()
    assert restored["state"] == "idle"
    assert restored == status
    assert unopened.calls == []


@pytest.mark.parametrize("failure", [False, True])
def test_status_restores_last_full_run_and_refreshes_current_counts(settings, failure):
    fake = ReindexImap(skip={"2", "3"} if failure else ())
    reader, service = service_for(settings, fake)
    reader.search_index.observe("All Mail", "1", {"1": {"headers": {"Subject": "Old"}}})
    service.execute(service.preview()["token"])
    finished = completed(service)
    assert finished["last_full_run"]["old_count"] == 1
    assert finished["last_full_run"]["new_count"] == (1 if failure else 3)
    assert finished["last_full_run"]["all_mail_count"] == 3
    assert finished["state"] == ("failed" if failure else "succeeded")
    reader.search_index.remove("All Mail", "1", ["1"])
    current = service.status()

    unopened = ReindexImap(999)
    restarted = client(reader.settings, unopened)
    restored = restarted.reindex_status()
    assert restored["last_full_run"] == finished["last_full_run"]
    if failure:
        assert restored["state"] == finished["state"]
        assert restored["run_id"] == finished["run_id"]
        assert restored == current
    else:
        # The completed manual run is historical; the serving index is now behind.
        assert restored["state"] == "incomplete"
        assert restored["run_id"] == current["run_id"]
        assert restored["swap_outcome"] == current["swap_outcome"]
        assert restored["last_full_run"] == current["last_full_run"]
        assert restored["target"] == 3 and restored["percent"] == 66.7
        assert restored["eta_seconds"] is None
        assert current["state"] == "succeeded"
        assert restored["indexed_count"] == 2
        assert restored["indexed_max_uid"] == "3"
    assert unopened.calls == []


@pytest.mark.parametrize(
    "change,shared_sync",
    [("arrival", False), ("replacement", False), ("expunge_highest", False), ("arrival", True)],
)
def test_status_reports_current_counts_and_persisted_incremental_catchup(
    settings, change, shared_sync
):
    fake = ReindexImap()
    reader, service = service_for(settings, fake)
    service.execute(service.preview()["token"])
    finished = completed(service)
    assert finished["last_full_run"] == {
        "old_count": 0,
        "new_count": 3,
        "all_mail_count": 3,
    }
    assert finished["last_catchup_at"] is None
    if change in {"replacement", "expunge_highest"}:
        del fake.items["3"]
    if change != "expunge_highest":
        fake.items["10"] = message("<arrival@example.com>")

    if shared_sync:
        deadline = reader._deadline()
        with reader._session(deadline=deadline) as (connection, _):
            state = reader._select(connection, "All Mail")
            assert sync_selected_headers(reader, connection, deadline, "All Mail", state).complete
    else:
        found = reader.search_mail(folder="All Mail", subject="No match", include_snippets=False)
        assert not found["partial"] and "partial_reason" not in found
        assert found["total"] == 0
    # Duplicate label rows must not inflate the All Mail count or maximum UID.
    reader.search_index.observe("INBOX", "1", {"999": {"headers": {"Subject": "Duplicate"}}})
    calls = list(fake.calls)
    original = reader.search_index.path.read_bytes()
    status = service.status()
    expected_count = {"arrival": 4, "replacement": 3, "expunge_highest": 2}[change]
    expected_max = "2" if change == "expunge_highest" else "10"
    assert status["indexed_count"] == status["all_mail_count"] == expected_count
    assert status["last_full_run"]["all_mail_count"] == 3
    assert status["indexed_max_uid"] == status["all_mail_max_uid"] == expected_max
    assert status["last_full_run"] == finished["last_full_run"]
    assert status["run_id"] == finished["run_id"]
    for field in (
        "all_mail_observed_at",
        "last_catchup_at",
        "last_expunge_reconciliation_at",
    ):
        assert datetime.fromisoformat(status[field]).utcoffset().total_seconds() == 0
    assert service.status() == status
    assert fake.calls == calls
    assert reader.search_index.path.read_bytes() == original
    assert_read_only(fake)
    reader.search_mail(folder="All Mail", subject="No match", include_snippets=False)
    assert service.status()["last_catchup_at"] == status["last_catchup_at"]
    assert (
        service.status()["last_expunge_reconciliation_at"]
        == status["last_expunge_reconciliation_at"]
    )

    unopened = ReindexImap(999)
    restarted = client(reader.settings, unopened)
    assert restarted.reindex_status() == status
    assert unopened.calls == []


def test_status_reports_known_counts_without_inventing_missing_inventory_metadata(settings):
    fake = ReindexImap()
    reader, service = service_for(settings, fake)
    service.execute(service.preview()["token"])
    finished = completed(service)
    with sqlite3.connect(reader.search_index.path) as database:
        database.execute("DELETE FROM index_sync")
    calls = list(fake.calls)
    original = reader.search_index.path.read_bytes()

    status = service.status()

    assert status["indexed_count"] == 3
    assert status["all_mail_count"] is None
    assert status["indexed_max_uid"] == "3"
    assert status["last_full_run"] == finished["last_full_run"]
    for field in (
        "all_mail_max_uid",
        "all_mail_observed_at",
        "last_catchup_at",
        "last_expunge_reconciliation_at",
    ):
        assert status[field] is None
    assert fake.calls == calls
    assert reader.search_index.path.read_bytes() == original


def test_status_does_not_restore_another_accounts_last_run(settings):
    reader, service = service_for(settings, ReindexImap())
    service.execute(service.preview()["token"])
    assert completed(service)["last_full_run"]["new_count"] == 3
    unopened = ReindexImap()
    other = client(replace(reader.settings, bridge_user="different-account"), unopened)
    status = other.reindex_status()
    assert status["state"] == "incomplete"
    assert status["run_id"] is None
    assert status["last_full_run"] is None
    assert status["indexed_count"] is status["all_mail_count"] is None
    assert unopened.calls == []


def test_live_reader_lock_refuses_swap_and_candidate_can_replace_corrupt_cache(
    settings, monkeypatch
):
    import fcntl

    fake = ReindexImap()
    monkeypatch.setattr("proton_mail_mcp.reindex._SWAP_WAIT_SECONDS", 0.01)
    reader, service = service_for(settings, fake)
    original = seed_old(reader)
    lock = reader.search_index.path.with_name(reader.search_index.path.name + ".lock")
    with lock.open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
        service.execute(service.preview()["token"])
        status = completed(service)
    assert status["state"] == "failed" and status["swap_outcome"] == "kept_old"
    assert status["swap_reason"] == "swap_busy"
    assert reader.search_index.path.read_bytes() == original
    assert_no_candidate(reader)

    reader.search_index.path.write_bytes(b"invalid SQLite")
    reader.search_index = SearchIndex(reader.settings)
    assert reader.search_index.enabled is False
    service.execute(service.preview()["token"])
    assert completed(service)["state"] == "succeeded"
    assert reader.search_index.enabled is True
    assert reader.search_index.known_count("All Mail", "1") == 3


def test_new_run_cannot_relabel_previous_terminal_record(settings, monkeypatch):
    reader, service = service_for(settings, ReindexImap())
    token1 = service.preview()["token"]
    token2 = service.preview()["token"]
    original_audit = service.log.audit
    result_entered = threading.Event()
    release_result = threading.Event()

    def audit(record, **kwargs):
        if record["action"] == "reindex_result" and not result_entered.is_set():
            result_entered.set()
            assert release_result.wait(5)
        return original_audit(record, **kwargs)

    monkeypatch.setattr(service.log, "audit", audit)
    first = service.execute(token1)
    first_thread = service._thread
    assert result_entered.wait(3)
    second = service.execute(token2)
    assert completed(service)["state"] == "succeeded"
    release_result.set()
    first_thread.join(3)
    with ActionLog(reader.settings.action_log_path).transaction(readonly=True) as transaction:
        results = [row for row in transaction.records() if row["action"] == "reindex_result"]
    assert {(row["run_id"], row["action_log_id"]) for row in results} == {
        (first["run_id"], first["log_id"]),
        (second["run_id"], second["log_id"]),
    }
    restarted = client(reader.settings, ReindexImap())
    restored = restarted.reindex_status()
    assert restored["run_id"] == second["run_id"]
    assert restored["last_full_run"]["old_count"] == restored["last_full_run"]["new_count"] == 3


@pytest.mark.parametrize("inventory", [b"1 2", b"1 2 2"])
def test_preview_refuses_partial_or_duplicate_inventory(settings, inventory):
    fake = ReindexImap()
    original_uid = fake.uid

    def uid(command, *args):
        if command == "SEARCH":
            return "OK", [inventory]
        return original_uid(command, *args)

    fake.uid = uid
    reader, service = service_for(settings, fake)
    original = seed_old(reader)
    with pytest.raises(MailValidationError, match="inventory changed"):
        service.preview()
    assert not service._tokens
    assert reader.search_index.path.read_bytes() == original


def test_token_pruning_during_start_audit_does_not_break_valid_execution(settings, monkeypatch):
    _, service = service_for(settings, ReindexImap())
    preview = service.preview()
    original_audit = service.log.audit

    def audit(record, **kwargs):
        if record["action"] == "reindex_execute":
            # A concurrent preview may prune an expired/evicted digest while
            # execute persists the already admitted command's start record.
            with service._lock:
                service._tokens.clear()
        return original_audit(record, **kwargs)

    monkeypatch.setattr(service.log, "audit", audit)
    result = service.execute(preview["token"])
    assert result["token_consumed"] is True
    assert completed(service)["state"] == "succeeded"


def test_active_rebuild_uses_dedicated_connection_and_search_serves_old_index(settings):
    preview_imap = ReindexImap()
    worker_imap = ReindexImap(block=True)
    search_imap = ReindexImap()
    search_imap.folders["INBOX"] = search_imap.items
    reader = client(replace(settings, reindex_batch_sleep=0.01), preview_imap)
    connections = iter((preview_imap, worker_imap, search_imap))
    opened = []

    def factory(*args, **kwargs):
        connection = next(connections)
        opened.append(connection)
        return connection

    reader.imap_factory = factory
    service = reader.reindex
    live = reader.search_index
    live.observe("All Mail", "1", {"1": {"headers": {"Subject": "Hello"}}})
    run = service.execute(service.preview()["token"])
    assert worker_imap.entered.wait(3)
    try:
        found = reader.search_mail(folder="INBOX", subject="Hello", include_snippets=False)
        assert not found["partial"] and "partial_reason" not in found
        assert len(found["messages"]) == 3
        assert reader.search_index is live
        assert live.known_uids("All Mail", "1") == {"1"}
        assert worker_imap._search_index is not live
        assert search_imap._search_index is live
        assert opened == [preview_imap, worker_imap, search_imap]
        service.cancel(run["run_id"])
    finally:
        worker_imap.release.set()
    assert completed(service)["state"] == "cancelled"
    assert_no_candidate(reader)
