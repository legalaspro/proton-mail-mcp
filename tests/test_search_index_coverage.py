"""Coverage proofs stay consistent with concurrent local cache mutations."""

import sqlite3
import threading

import pytest
from test_mail import settings as settings

from proton_mail_mcp.search_index import SearchIndex


def complete_index(settings):
    index = SearchIndex(settings)
    index.observe(
        "All Mail",
        "1",
        {"1": {"headers": {"Subject": "First"}}, "2": {"headers": {"Subject": "Second"}}},
    )
    assert index.record_coverage("All Mail", "1", {"exists": 2, "uidnext": "3"}, ["1", "2"])
    assert index.coverage("All Mail") == ("1", 2, "3")
    return index


def test_recovery_gate_needs_saved_proofs_and_does_not_create_empty_cache(settings):
    index = SearchIndex(settings)
    assert not index.recovery_complete()
    assert not index.path.exists()
    index = complete_index(settings)
    assert index.recovery_complete()
    assert SearchIndex(settings).recovery_complete()
    index.select("INBOX", "1")
    assert not index.recovery_complete()
    assert index.record_coverage("INBOX", "1", {"exists": 0, "uidnext": "1"}, [], account=False)
    assert index.recovery_complete()
    index.observe("All Mail", "1", {"2": {"headers": {"Subject": "Unsafe\x00header"}}})
    assert not index.recovery_complete()


def test_pending_real_folder_pass_survives_restart_after_all_mail_finishes(settings):
    index = complete_index(settings)
    index.record_recovery_complete(False)
    assert not index.recovery_complete()
    restarted = SearchIndex(settings)
    assert not restarted.recovery_complete()
    restarted.record_recovery_complete(True)
    assert restarted.recovery_complete()
    # The completion marker cannot override a lost proof or missing header.
    restarted.remove("All Mail", "1", ["2"])
    assert not restarted.recovery_complete()


def test_recovery_gate_only_requires_folders_from_the_completed_recovery_pass(settings):
    index = complete_index(settings)
    index.record_recovery_complete(False, folders=["All Mail", "INBOX"])
    assert not index.recovery_complete()
    index.select("INBOX", "1")
    assert index.record_coverage("INBOX", "1", {"exists": 0, "uidnext": "1"}, [], account=False)
    index.record_recovery_complete(True)
    index.select("Labels/Unwarmed", "1")
    assert index.coverage("Labels/Unwarmed") is None
    assert index.recovery_complete()
    assert SearchIndex(settings).recovery_complete()
    index.select("INBOX", "2")
    assert not index.recovery_complete()


def test_stale_known_hint_cannot_restore_proof_after_reconcile(settings):
    index = complete_index(settings)
    known = index.known_uids("All Mail", "1")
    index.reconcile("All Mail", "1", ["1"])

    assert not index.record_coverage(
        "All Mail", "1", {"exists": 2, "uidnext": "3"}, ["1", "2"], known=known
    )
    assert index.coverage("All Mail") is None


def test_proof_validation_and_reconcile_are_serialized(settings, monkeypatch):
    index = complete_index(settings)
    known = index.known_uids("All Mail", "1")
    read, release, attempted, changed = (threading.Event() for _ in range(4))
    original_known = index.known_uids
    results, errors = [], []

    def paused_known(*args):
        result = original_known(*args)
        if threading.current_thread().name == "coverage-proof":
            read.set()
            assert release.wait(3), "test did not release its coverage read"
        return result

    monkeypatch.setattr(index, "known_uids", paused_known)

    def prove():
        try:
            results.append(
                index.record_coverage(
                    "All Mail", "1", {"exists": 2, "uidnext": "3"}, ["1", "2"], known=known
                )
            )
        except Exception as error:
            errors.append(error)

    def reconcile():
        attempted.set()
        try:
            index.reconcile("All Mail", "1", ["1"])
        except Exception as error:
            errors.append(error)
        finally:
            changed.set()

    proof_thread = threading.Thread(target=prove, name="coverage-proof")
    reconcile_thread = threading.Thread(target=reconcile)
    proof_thread.start()
    try:
        assert read.wait(3)
        reconcile_thread.start()
        assert attempted.wait(3)
        assert not changed.wait(0.05)
        assert index.coverage("All Mail") is None  # A long hold ends in no proof after 1 s.
    finally:
        release.set()
        proof_thread.join(3)
        if reconcile_thread.ident is not None:
            reconcile_thread.join(3)
    assert not proof_thread.is_alive() and not reconcile_thread.is_alive()
    assert not errors and results == [True]
    assert index.known_uids("All Mail", "1") == {"1"}
    assert index.coverage("All Mail") is None


def test_coverage_waits_out_a_short_hold_by_a_parallel_read(settings):
    index = complete_index(settings)
    attempted, finished = threading.Event(), threading.Event()
    results, errors = [], []

    def read():
        attempted.set()
        try:
            results.append(index.coverage("All Mail"))
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    reader = threading.Thread(target=read)
    index._coverage_lock.acquire()
    try:
        reader.start()
        assert attempted.wait(3)
        assert not finished.wait(0.1)  # A busy lock must not read as a stale index.
    finally:
        index._coverage_lock.release()
        reader.join(3)
    assert not reader.is_alive()
    assert not errors and results == [("1", 2, "3")]


@pytest.mark.parametrize("mutation", ["nul", "select", "observe", "remove"])
def test_header_loss_and_epoch_changes_invalidate_existing_proof(settings, mutation):
    index = complete_index(settings)
    if mutation == "nul":
        index.observe("All Mail", "1", {"2": {"headers": {"Subject": "Unsafe\x00header"}}})
    elif mutation == "select":
        index.select("All Mail", "2")
    elif mutation == "observe":
        index.observe("All Mail", "2", {"1": {"headers": {"Subject": "New epoch"}}})
    else:
        index.remove("All Mail", "1", ["2"])
    assert index.coverage("All Mail") is None


@pytest.mark.parametrize("mutation", ["downgrade", "remove", "extra"])
def test_legacy_writer_cannot_leave_certified_coverage_after_header_count_changes(
    settings, mutation
):
    index = complete_index(settings)
    # Older processes mutate message rows without knowing the coverage table.
    with sqlite3.connect(index.path) as database:
        if mutation == "downgrade":
            database.execute("UPDATE messages SET headers_known = 0 WHERE uid = '2'")
        elif mutation == "remove":
            database.execute("DELETE FROM messages WHERE uid = '2'")
        else:
            database.execute(
                "INSERT INTO messages(folder, uidvalidity, uid, headers_known) "
                "VALUES ('All Mail', '1', '3', 1)"
            )
        assert database.execute("SELECT certified FROM coverage").fetchone() == (1,)

    assert index.coverage("All Mail") is None
    assert SearchIndex(settings).coverage("All Mail") is None


def test_cached_headers_only_returns_requested_known_headers_in_the_epoch(settings):
    index = SearchIndex(settings)
    index.observe(
        "All Mail",
        "1",
        {
            "1": {
                "headers": {
                    "From": "=?utf-8?q?Stra=C3=9Fe?= <Sender@Example.invalid>",
                    "To": "Team@Example.invalid",
                    "Subject": "Straße Topic",
                }
            },
            "2": {"headers": {"Subject": "Unrequested"}},
            "3": {"headers": {"Subject": "Unsafe\x00header"}},
        },
    )
    # Metadata-only membership rows cannot establish decoded headers.
    index.observe("All Mail", "1", {"4": {"internaldate": 1.0}}, membership=True)
    index.observe("INBOX", "1", {"5": {"headers": {"Subject": "Other folder"}}})

    assert index.cached_headers("All Mail", "1", ["1", "3", "4", "5", "99"]) == {
        "1": {
            "From": "strasse <sender@example.invalid>",
            "To": "team@example.invalid",
            "Subject": "strasse topic",
        }
    }
    assert index.cached_headers("All Mail", "2", ["1"]) == {}
    assert index.cached_headers("All Mail", "1", []) == {}


def test_cached_headers_batches_beyond_legacy_sqlite_parameter_limit(settings, monkeypatch):
    index = SearchIndex(settings)
    uids = [str(uid) for uid in range(1, 1102)]
    index.observe(
        "All Mail", "1", {uid: {"headers": {"Subject": f"Message {uid}"}} for uid in uids}
    )
    original_connect = index._connect

    def connect():
        database = original_connect()
        database.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        return database

    monkeypatch.setattr(index, "_connect", connect)
    cached = index.cached_headers("All Mail", "1", uids)

    assert set(cached) == set(uids)
    assert cached["1101"]["Subject"] == "message 1101"


def test_cached_headers_unavailable_cache_leaves_headers_unknown(settings):
    index = SearchIndex(settings)
    assert index.cached_headers("All Mail", "1", ["1"]) == {}
    assert not index.path.exists()


def test_folder_header_coverage_uses_requested_inventory_and_epoch(settings, monkeypatch):
    index = SearchIndex(settings)
    assert not index.covers("INBOX", "1", ["1"])
    uids = [str(uid) for uid in range(1, 1102)]
    index.observe("INBOX", "1", {uid: {"headers": {"Subject": "Known"}} for uid in uids})
    original_connect = index._connect

    def connect():
        database = original_connect()
        database.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        return database

    monkeypatch.setattr(index, "_connect", connect)
    assert index.covers("INBOX", "1", uids)
    assert index.covers("INBOX", "1", ["1", "1"])
    assert not index.covers("All Mail", "1", uids)
    assert not index.covers("INBOX", "2", uids)
    assert not index.covers("INBOX", "1", [*uids, "1102"])
    index.observe("INBOX", "1", {"1": {"headers": {"Subject": "Unsafe\x00header"}}})
    assert not index.covers("INBOX", "1", uids)
