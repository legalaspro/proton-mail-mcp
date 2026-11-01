"""Startup header recovery uses bounded, independent, read-only IMAP sessions."""

import imaplib
import io
import logging
import os
import sqlite3
import sys
import threading
import time
from contextlib import closing
from dataclasses import replace

import pytest
from gluon_search import GluonSearchImap
from test_mail import client, message
from test_mail import settings as settings
from test_reindex import assert_read_only

from proton_mail_mcp.index_autorebuild import _Yielded
from proton_mail_mcp.mail import MailClient, _Deadline, _DeadlineIMAP4
from proton_mail_mcp.search_index import SearchIndex
from proton_mail_mcp.security import CredentialError, Credentials


def items(*uids):
    return {str(uid): message(f"<{uid}@example.com>") for uid in uids}


def job(settings, fake, **changes):
    configured = replace(settings, reindex_batch_size=2, reindex_batch_sleep=0.01, **changes)
    reader = client(configured, fake)
    return reader, reader.index_autorebuild


def complete(worker):
    assert worker._thread is not None
    worker._thread.join(5)
    assert not worker._thread.is_alive(), worker.status()
    assert worker.status()["state"] == "succeeded", worker.status()


@pytest.mark.parametrize("legacy", [False, True])
def test_startup_recovers_empty_or_discarded_index_without_sync(settings, legacy, caplog):
    fake = GluonSearchImap(
        {
            "All Mail": items(10001, 20431, 47744),
            "INBOX": items(1, 2, 3),
            "Labels/View": items(999),
        },
        uidnext=True,
    )
    reader, worker = job(settings, fake)
    if legacy:
        path = reader.search_index.path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as database, database:
            database.execute("PRAGMA user_version = 1")
        os.chmod(path, 0o600)
        path.with_name(path.name + ".lock").touch(mode=0o600)
        reader.search_index = SearchIndex(reader.settings)
        assert reader.search_index._initialized
    assert reader.settings.background_sync_enabled is False
    assert fake.calls == []
    with caplog.at_level(logging.WARNING):
        worker.start()
        complete(worker)
    try:
        assert reader.search_index.known_uids("All Mail", "1") == {"10001", "20431", "47744"}
        assert reader.search_index.known_uids("INBOX", "1") == {"1", "2", "3"}
        assert reader.search_index.known_uids("Labels/View", "1") == set()
        assert reader.search_index.coverage("All Mail") == ("1", 3, "47745")
        assert reader.search_index.coverage("INBOX") == ("1", 3, "4")
        status = reader.reindex_status()
        assert status["source"] == "auto" and status["state"] == "succeeded"
        assert status["indexed_count"] == status["all_mail_count"] == 3
        assert status["all_mail_max_uid"] == "47744"
        assert status["last_catchup_at"] is not None
        assert_read_only(fake)
        assert max(len(call[1].split(",")) for call in fake.calls if call[0] == "FETCH") == 2
        logs = " ".join(row.message for row in caplog.records)
        assert "auto rebuild start" in logs
        assert "auto rebuild progress" in logs
        assert "auto rebuild finish" in logs
        assert "example.com" not in logs
    finally:
        worker.stop()


def test_kill_switch_starts_no_thread_or_connection(settings):
    fake = GluonSearchImap({"All Mail": items(1)}, uidnext=True)
    _, worker = job(settings, fake, index_autorebuild_enabled=False)
    worker.start()
    with worker.foreground():
        assert worker.status()["state"] == "disabled"
    worker.stop()
    assert fake.calls == [] and worker._thread is None


def test_restart_uses_persisted_batches_and_inventory_without_refetch(settings, monkeypatch):
    fake = GluonSearchImap({"All Mail": items(1, 2, 20431, 47744)}, uidnext=True)
    reader, worker = job(settings, fake)
    original = worker._batch

    def first_batch(work):
        original(work)
        worker._stop.set()

    monkeypatch.setattr(worker, "_batch", first_batch)
    worker.start()
    assert worker._thread is not None
    worker._thread.join(5)
    assert not worker._thread.is_alive()
    partial = reader.reindex_status()
    assert partial["state"] == "incomplete"
    assert partial["indexed_count"] == 2 and partial["all_mail_count"] == 4
    assert partial["last_catchup_at"] is not None
    assert partial["percent_done"] == 50.0
    assert partial["target"] == 4 and partial["percent"] == 50.0
    assert reader.search_index.coverage("All Mail") is None
    worker.stop()

    fresh_fake = GluonSearchImap(fake.folders, uidnext=True)
    restarted, recovery = job(settings, fresh_fake)
    assert restarted.search_index.known_uids("All Mail", "1") == {"20431", "47744"}
    recovery.start()
    try:
        complete(recovery)
        fetched = {
            uid for call in fresh_fake.calls if call[0] == "FETCH" for uid in call[1].split(",")
        }
        assert fetched == {"1", "2"}
        assert restarted.search_index.coverage("All Mail") == ("1", 4, "47745")
    finally:
        recovery.stop()


def test_sparse_uids_and_matching_maximum_do_not_hide_missing_headers(settings):
    fake = GluonSearchImap({"All Mail": items(1, 20431, 47744)}, uidnext=True)
    reader, worker = job(settings, fake)
    reader.search_index.observe("All Mail", "1", {"47744": {"headers": {"Subject": "Known"}}})
    reader.search_index.set_all_mail("All Mail")
    reader.search_index.reconcile("All Mail", "1", ["1", "20431", "47744"])
    worker.start()
    try:
        complete(worker)
        fetched = {uid for call in fake.calls if call[0] == "FETCH" for uid in call[1].split(",")}
        assert fetched == {"1", "20431"}
        assert reader.search_index.known_count("All Mail", "1") == 3
    finally:
        worker.stop()


@pytest.mark.parametrize("header", ["Subject", "From", "To", "Cc"])
def test_unstorable_header_has_bounded_attempts_and_later_folders_warm(settings, header):
    bad = message("<bad@example.test>")
    prefix = (header + ": ").encode()
    if prefix not in bad.headers:
        bad.headers = bad.headers.replace(b"\r\n\r\n", b"\r\n" + prefix + b"\r\n\r\n")
    bad.headers = b"\r\n".join(
        prefix + b"bad\x00value@example.test" if line.startswith(prefix) else line
        for line in bad.headers.split(b"\r\n")
    )
    fake = GluonSearchImap(
        {
            # The bad newest UID is encountered before several healthy batches.
            "All Mail": {**items(1, 2, 3, 4, 5), "6": bad},
            "INBOX": items(10, 11),
        },
        uidnext=True,
    )
    reader, worker = job(settings, fake)
    worker.start()
    try:
        worker._thread.join(5)
        assert not worker._thread.is_alive(), worker.status()
        assert worker.status()["state"] == "failed"
        assert worker.status()["last_error"] == "headers_incomplete"
        assert reader.search_index.known_uids("All Mail", "1") == {"1", "2", "3", "4", "5"}
        assert reader.search_index.coverage("All Mail") is None
        assert reader.search_index.known_uids("INBOX", "1") == {"10", "11"}
        assert reader.search_index.coverage("INBOX") == ("1", 2, "12")
        fetched = [uid for call in fake.calls if call[0] == "FETCH" for uid in call[1].split(",")]
        assert fetched.count("6") == 3
        assert all(fetched.count(str(uid)) == 1 for uid in range(1, 6))
        assert reader.reindex_status()["indexed_count"] == 5
        assert reader.reindex_status()["target"] == 6
        assert not reader.search_index.recovery_complete()
        assert_read_only(fake)
        before = len(fake.calls)
        with worker.foreground():
            pass
        assert len(fake.calls) == before
    finally:
        worker.stop()


def test_unstorable_header_attempt_budget_survives_transient_inventory_retries(
    settings, monkeypatch
):
    import proton_mail_mcp.index_autorebuild as autorebuild

    monkeypatch.setattr(autorebuild, "_RETRY_SECONDS", 0.001)
    monkeypatch.setattr(autorebuild, "_MAX_RETRY_SECONDS", 0.001)
    fake = GluonSearchImap(
        {
            "All Mail": {**items(1, 2, 3, 4), "5": message("<bad@test>", subject="bad\x00")},
            "INBOX": items(10, 11),
        },
        uidnext=True,
    )
    reader, worker = job(settings, fake)
    original = reader._metadata
    attempts = []

    def intermittent(connection, uids, **kwargs):
        attempts.append(tuple(uids))
        if len(attempts) % 2 == 0:
            raise OSError("private-transient-diagnostic")
        return original(connection, uids, **kwargs)

    monkeypatch.setattr(reader, "_metadata", intermittent)
    worker.start()
    try:
        worker._thread.join(5)
        assert not worker._thread.is_alive(), worker.status()
        assert worker.status()["state"] == "failed"
        assert worker.status()["last_error"] == "headers_incomplete"
        successful = attempts[::2]
        assert sum("5" in batch for batch in successful) == autorebuild._MAX_HEADER_ATTEMPTS
        assert reader.search_index.known_uids("All Mail", "1") == {"1", "2", "3", "4"}
        assert reader.search_index.coverage("All Mail") is None
        assert reader.search_index.known_uids("INBOX", "1") == {"10", "11"}
        assert reader.search_index.coverage("INBOX") == ("1", 2, "12")
        assert not reader.search_index.recovery_complete()
    finally:
        worker.stop()


def test_unstorable_real_folder_header_keeps_terminal_failure_with_complete_all_mail(settings):
    fake = GluonSearchImap(
        {
            "All Mail": items(1, 2),
            "INBOX": {"10": message("<bad@test>", subject="bad\x00")},
            "Sent": items(20),
        },
        uidnext=True,
    )
    reader, worker = job(settings, fake)
    worker.start()
    try:
        worker._thread.join(5)
        assert not worker._thread.is_alive(), worker.status()
        status = reader.reindex_status()
        assert status["state"] == "failed"
        assert status["last_error"] == "headers_incomplete"
        assert status["indexed_count"] == status["all_mail_count"] == 2
        assert reader.search_index.coverage("All Mail") == ("1", 2, "3")
        assert reader.search_index.coverage("INBOX") is None
        assert reader.search_index.coverage("Sent") == ("1", 1, "21")
        assert not reader.search_index.recovery_complete()
    finally:
        worker.stop()


def test_certified_replacement_drops_old_generation_header_failure(settings):
    fake = GluonSearchImap({"All Mail": items(1)}, uidnext=True)
    reader, worker = job(settings, fake)
    index = reader.search_index
    index.observe("All Mail", "1", {"1": {"headers": {"Subject": "bad\x00"}}})
    old = ("All Mail", "1", index.generation)
    worker._header_attempts[old] = {"1": 3}
    worker._incomplete[old] = {"1"}
    candidate = index.create_candidate(reader.settings, "a" * 32)
    try:
        candidate.observe("All Mail", "1", {"1": {"headers": {"Subject": "Hello"}}})
        candidate.set_all_mail("All Mail")
        candidate.reconcile("All Mail", "1", ["1"])
        assert candidate.record_coverage(
            "All Mail", "1", {"exists": 1, "uidnext": "2"}, ["1"], account=True
        )
        index.replace_with(candidate)
        assert index.generation != old[2]
        # Resume with existing ledgers, as a yielded worker does after a manual
        # cache replacement. The live mailbox stays fixed throughout the test.
        worker._thread = threading.Thread(target=worker._run)
        worker._thread.start()
        complete(worker)
        assert worker.status()["state"] == "succeeded"
        assert worker.status()["last_error"] is None
        assert not worker._incomplete
        assert not worker._header_attempts
        assert index.recovery_complete()
        assert not any(call[0] == "FETCH" for call in fake.calls)
    finally:
        worker.stop()
        candidate.release_candidate()


def test_missing_fetch_rows_are_skipped_after_bounded_attempts(settings, monkeypatch):
    fake = GluonSearchImap({"All Mail": items(1, 2), "INBOX": items(10)}, uidnext=True)
    reader, worker = job(settings, fake)
    original = reader._metadata
    attempts = []

    def incomplete(connection, uids, **kwargs):
        attempts.extend(uids)
        return original(connection, [uid for uid in uids if uid != "1"], **kwargs)

    monkeypatch.setattr(reader, "_metadata", incomplete)
    worker.start()
    try:
        worker._thread.join(5)
        assert not worker._thread.is_alive(), worker.status()
        assert worker.status()["last_error"] == "headers_incomplete"
        assert attempts.count("1") == 3
        assert reader.search_index.known_uids("All Mail", "1") == {"2"}
        assert reader.search_index.coverage("All Mail") is None
        assert reader.search_index.known_uids("INBOX", "1") == {"10"}
    finally:
        worker.stop()


class Socket:
    def __init__(self, interrupt):
        self.interrupt = interrupt

    def settimeout(self, timeout):
        assert 0 < timeout <= 30

    def shutdown(self, how):
        self.interrupt.set()


def test_foreground_preempts_own_socket_without_waiting_and_worker_resumes(settings):
    entered, interrupted = threading.Event(), threading.Event()
    instances = []
    fetched_batches = []
    folders = {"All Mail": items(1, 2, 3), "INBOX": {}}

    class Connection(GluonSearchImap):
        def __init__(self):
            super().__init__(folders, uidnext=True)
            self.sock = Socket(interrupted)

        def uid(self, command, *args):
            if command == "FETCH":
                fetched_batches.append(args[0])
                if len(fetched_batches) == 2:
                    entered.set()
                    assert interrupted.wait(3), "worker socket was not interrupted"
                    raise OSError("private server text")
            return super().uid(command, *args)

    def factory(*args, **kwargs):
        connection = Connection()
        instances.append(connection)
        return connection

    configured = replace(settings, reindex_batch_size=2, reindex_batch_sleep=0.01)
    reader = MailClient(configured, lambda _: Credentials("reader", "test-password"), factory)
    worker = reader.index_autorebuild
    worker.start()
    assert entered.wait(3)
    try:
        status = reader.reindex_status()
        assert status["state"] == "rebuilding" and status["source"] == "auto"
        assert status["indexed_count"] == 2 and status["all_mail_count"] == 3
        assert status["max_uid"] == "3" and status["last_catchup_at"] is not None
        assert status["percent_done"] == 66.7
        assert status["target"] == 3 and status["percent"] == 66.7
        started = time.monotonic()
        with worker.foreground():
            assert time.monotonic() - started < 0.5
            assert interrupted.is_set()
            assert worker.status()["state"] == "queued"
            # Foreground IMAP has its own connection and completes immediately.
            with reader._session() as (connection, _):
                reader._folders(connection)
        complete(worker)
        assert len(instances) >= 5
        assert reader.search_index.known_count("All Mail", "1") == 3
        for connection in instances:
            assert_read_only(connection)
    finally:
        worker.stop()


def test_manual_reindex_guard_queues_worker_without_imap(settings):
    fake = GluonSearchImap({"All Mail": items(1)}, uidnext=True)
    reader, worker = job(settings, fake)
    guard = reader.search_index.acquire_reindex_guard()
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while worker.status()["last_error"] is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert worker.status()["state"] == "queued"
        assert worker.status()["last_error"] == "index_busy"
        assert fake.calls == []
    finally:
        os.close(guard)
        worker._wake.set()
    try:
        complete(worker)
    finally:
        worker.stop()


@pytest.mark.parametrize("phase", ["load", "batch", "finish"])
@pytest.mark.parametrize("disabled", [False, True])
def test_index_unavailable_retries_only_while_enabled(settings, monkeypatch, phase, disabled):
    fake = GluonSearchImap({"All Mail": items(1)}, uidnext=True)
    reader, worker = job(settings, fake)
    original = getattr(worker, f"_{phase}")
    unavailable_reads, backoffs = [], []

    def unavailable(*args, **kwargs):
        unavailable_reads.append(True)
        if disabled:
            reader.search_index.enabled = False
        return None

    def temporarily_unavailable(*args):
        if not unavailable_reads:
            with monkeypatch.context() as patch:
                read = "known_count" if phase == "batch" else "known_uids"
                patch.setattr(reader.search_index, read, unavailable)
                return original(*args)
        return original(*args)

    def record_wait(seconds, *, backoff=False):
        if backoff:
            backoffs.append(worker.status())

    monkeypatch.setattr(worker, f"_{phase}", temporarily_unavailable)
    monkeypatch.setattr(worker, "_wait", record_wait)
    worker.start()
    try:
        worker._thread.join(3)
        assert not worker._thread.is_alive(), worker.status()
        assert unavailable_reads == [True]
        if disabled:
            assert backoffs == []
            assert worker.status()["state"] == "failed"
            assert worker.status()["last_error"] == "index_unavailable"
        else:
            assert len(backoffs) == 1
            assert backoffs[0]["state"] == "queued"
            assert backoffs[0]["last_error"] == "index_busy"
            assert worker.status()["state"] == "succeeded"
            assert worker.status()["last_error"] is None
            assert reader.search_index.coverage("All Mail") == ("1", 1, "2")
    finally:
        worker.stop()


def test_failure_retries_with_fixed_reason_and_does_not_leak_exception(settings, caplog):
    fake = GluonSearchImap({"All Mail": items(1, 2, 3)}, uidnext=True)
    reader, worker = job(settings, fake)
    original = reader._metadata
    failures = []

    def one_failure(*args, **kwargs):
        if not failures:
            failures.append(True)
            raise RuntimeError("private-server-content-must-not-escape")
        return original(*args, **kwargs)

    reader._metadata = one_failure
    with caplog.at_level(logging.WARNING):
        worker.start()
        try:
            complete(worker)
        finally:
            worker.stop()
    assert failures
    assert any("reason=rebuild_failed" in row.message for row in caplog.records)
    assert "private-server-content" not in str(worker.status())
    assert all("private-server-content" not in row.message for row in caplog.records)


def test_missing_all_mail_role_is_a_terminal_failure(settings):
    fake = GluonSearchImap({"INBOX": items(1)}, uidnext=True)
    _, worker = job(settings, fake)
    worker.start()
    try:
        worker._thread.join(3)
        assert not worker._thread.is_alive(), worker.status()
        assert worker.status()["state"] == "failed"
        assert worker.status()["last_error"] == "no_all_mail"
        assert sum(call[0] == "LOGIN" for call in fake.calls) == 1
        assert not any(call[0] in {"SEARCH", "FETCH"} for call in fake.calls)
    finally:
        worker.stop()


def test_authentication_failure_is_terminal_without_diagnostic_leak(settings, caplog):
    class Rejected(GluonSearchImap):
        attempts = 0

        def login(self, *_):
            self.attempts += 1
            raise imaplib.IMAP4.error("private-authentication-diagnostic")

    fake = Rejected({"All Mail": items(1)}, uidnext=True)
    _, worker = job(settings, fake)
    with caplog.at_level(logging.WARNING):
        worker.start()
        try:
            worker._thread.join(3)
            assert not worker._thread.is_alive(), worker.status()
            assert worker.status()["state"] == "failed"
            assert worker.status()["last_error"] == "authentication_failed"
            assert fake.attempts == 1
            assert "private-authentication" not in str(worker.status())
            assert all("private-authentication" not in row.message for row in caplog.records)
        finally:
            worker.stop()


def test_credential_failure_stops_before_connection_without_diagnostic_leak(
    settings, caplog, monkeypatch
):
    fake = GluonSearchImap({"All Mail": items(1)}, uidnext=True)
    reader, worker = job(settings, fake)
    attempts = []

    def unavailable(_):
        attempts.append(True)
        raise CredentialError("private-credential-diagnostic")

    monkeypatch.setattr(reader, "credentials_provider", unavailable)
    with caplog.at_level(logging.WARNING):
        worker.start()
        try:
            worker._thread.join(3)
            assert not worker._thread.is_alive(), worker.status()
            assert worker.status()["state"] == "failed"
            assert worker.status()["last_error"] == "authentication_failed"
            assert len(attempts) == 1
            assert not fake.calls
            assert "private-credential" not in str(worker.status())
            assert all("private-credential" not in row.message for row in caplog.records)
        finally:
            worker.stop()


def test_transient_failures_stop_after_bounded_attempts(settings, monkeypatch):
    import proton_mail_mcp.index_autorebuild as autorebuild

    monkeypatch.setattr(autorebuild, "_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(autorebuild, "_MAX_RETRY_SECONDS", 0.01)
    fake = GluonSearchImap({"All Mail": items(1)}, uidnext=True)
    reader, worker = job(settings, fake)
    attempts = []

    def unavailable(*args, **kwargs):
        attempts.append(True)
        raise OSError("private-diagnostic")

    monkeypatch.setattr(reader, "_metadata", unavailable)
    worker.start()
    try:
        worker._thread.join(3)
        assert not worker._thread.is_alive(), worker.status()
        assert len(attempts) == autorebuild._MAX_FAILURES
        assert worker.status()["state"] == "failed"
        assert worker.status()["last_error"] == "rebuild_failed"
    finally:
        worker.stop()


def test_persistent_later_folder_failure_keeps_retry_budget_and_backoff(settings, monkeypatch):
    import proton_mail_mcp.index_autorebuild as autorebuild

    fake = GluonSearchImap({"All Mail": items(1, 2), "INBOX": items(10)}, uidnext=True)
    reader, worker = job(settings, fake)
    original = reader._metadata
    attempts, backoffs, statuses = [], [], []

    def fail_inbox(connection, uids, **kwargs):
        if connection.current == "INBOX":
            attempts.append(tuple(uids))
            raise OSError("private-permanent-diagnostic")
        return original(connection, uids, **kwargs)

    def record_wait(seconds, *, backoff=False):
        if backoff:
            backoffs.append(seconds)
            statuses.append((worker.status(), reader.reindex_status()))

    monkeypatch.setattr(reader, "_metadata", fail_inbox)
    monkeypatch.setattr(worker, "_wait", record_wait)
    worker.start()
    try:
        worker._thread.join(5)
        assert not worker._thread.is_alive(), worker.status()
        assert len(attempts) == autorebuild._MAX_FAILURES
        assert backoffs == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]
        assert all(status["state"] == "queued" for status, _ in statuses)
        assert all(status["last_error"] == "rebuild_failed" for status, _ in statuses)
        assert all(status["state"] == "rebuilding" for _, status in statuses)
        assert all(status["last_error"] == "rebuild_failed" for _, status in statuses)
        assert reader.search_index.coverage("All Mail") == ("1", 2, "3")
        assert reader.search_index.coverage("INBOX") is None
        assert reader.reindex_status()["state"] == "failed"
        assert reader.reindex_status()["last_error"] == "rebuild_failed"
        assert not reader.search_index.recovery_complete()
    finally:
        worker.stop()


def test_foreground_wake_does_not_shorten_failure_backoff(settings):
    _, worker = job(settings, GluonSearchImap({"All Mail": items(1)}, uidnext=True))
    worker._wake.set()
    started = time.monotonic()
    waiting = threading.Thread(target=worker._wait, args=(0.15,), kwargs={"backoff": True})
    waiting.start()
    with worker.foreground():
        pass
    waiting.join(2)
    assert not waiting.is_alive()
    assert time.monotonic() - started >= 0.13


def test_register_rejection_inside_real_deadline_open_closes_new_socket(settings, monkeypatch):
    interrupted, closed = threading.Event(), threading.Event()
    stream = io.BytesIO()

    class NewSocket(Socket):
        def close(self):
            closed.set()

    def open_socket(connection, *args, **kwargs):
        connection.sock = NewSocket(interrupted)
        connection._file = stream
        if sys.version_info < (3, 14):  # Since 3.14 IMAP4.file is a read-only property.
            connection.file = stream

    # Keep the real subclass constructor/open path; replace only socket I/O.
    monkeypatch.setattr(imaplib.IMAP4, "open", open_socket)
    _, worker = job(settings, GluonSearchImap({"All Mail": items(1)}, uidnext=True))
    with worker.foreground(), pytest.raises(_Yielded):
        _DeadlineIMAP4(
            "localhost", 1143, timeout=1, deadline=_Deadline(1, 1), on_connect=worker._register
        )
    assert interrupted.is_set() and closed.is_set()
    assert stream.closed


def test_recovery_fetches_newest_first_and_withholds_eta_until_all_folders_finish(
    settings, monkeypatch
):
    fake = GluonSearchImap(
        {"All Mail": items(1, 20431, 47744), "INBOX": items(10, 11)}, uidnext=True
    )
    _, worker = job(settings, fake)
    original = worker._batch
    progress = []

    def sample(work):
        original(work)
        progress.append((work.folder, worker.status()))

    monkeypatch.setattr(worker, "_batch", sample)
    worker.start()
    try:
        complete(worker)
        assert [call[1] for call in fake.calls if call[0] == "FETCH"] == [
            "47744,20431",
            "1",
            "11,10",
        ]
        assert progress
        assert all(status["eta_seconds"] != 0 for _, status in progress)
        assert all(
            status["eta_seconds"] is None for folder, status in progress if folder == "INBOX"
        )
        assert worker.status()["eta_seconds"] == 0
    finally:
        worker.stop()


def test_healthy_restart_retains_certified_coverage_and_skips_all_searches(settings, monkeypatch):
    fake = GluonSearchImap({"All Mail": items(20431, 47744), "INBOX": items(1, 2)}, uidnext=True)
    reader, worker = job(settings, fake)
    worker.start()
    complete(worker)
    before = reader.search_index.reporting_status()
    worker.stop()

    fresh_fake = GluonSearchImap(fake.folders, uidnext=True)
    restarted, recovery = job(settings, fresh_fake)

    def unexpected_credentials(_):
        pytest.fail("healthy startup must not read credentials")

    monkeypatch.setattr(restarted, "credentials_provider", unexpected_credentials)
    recovery.start()
    try:
        assert recovery._thread is None
        assert recovery.status()["state"] == "idle"
        assert fresh_fake.calls == []
        assert restarted.search_index.coverage("All Mail") == ("1", 2, "47745")
        assert restarted.search_index.coverage("INBOX") == ("1", 2, "3")
        assert restarted.search_index.reporting_status() == before
    finally:
        recovery.stop()


def test_restart_resumes_real_folders_after_all_mail_already_finished(settings, monkeypatch):
    fake = GluonSearchImap({"All Mail": items(1, 2), "INBOX": items(10)}, uidnext=True)
    reader, worker = job(settings, fake)
    original = worker._finish

    def stop_after_all_mail(work):
        result = original(work)
        if work.folder == "All Mail" and result:
            worker._stop.set()
        return result

    monkeypatch.setattr(worker, "_finish", stop_after_all_mail)
    worker.start()
    worker._thread.join(3)
    assert not worker._thread.is_alive()
    assert reader.search_index.coverage("All Mail") == ("1", 2, "3")
    assert reader.search_index.coverage("INBOX") is None
    worker.stop()

    fresh_fake = GluonSearchImap(fake.folders, uidnext=True)
    restarted, recovery = job(settings, fresh_fake)
    recovery.start()
    try:
        complete(recovery)
        assert restarted.search_index.coverage("INBOX") == ("1", 1, "11")
        assert [call[1] for call in fresh_fake.calls if call[0] == "FETCH"] == ["10"]
    finally:
        recovery.stop()


def test_healthy_restart_ignores_incomplete_virtual_folder_cache(settings):
    fake = GluonSearchImap(
        {"All Mail": items(1, 2), "INBOX": items(10), "Labels/View": items(100)}, uidnext=True
    )
    reader, worker = job(settings, fake)
    reader.search_index.select("Labels/View", "1")
    worker.start()
    complete(worker)
    worker.stop()
    assert reader.search_index.coverage("Labels/View") is None

    fresh_fake = GluonSearchImap(fake.folders, uidnext=True)
    _, recovery = job(settings, fresh_fake)
    recovery.start()
    try:
        assert recovery._thread is None
        assert recovery.status()["state"] == "idle"
        assert fresh_fake.calls == []
    finally:
        recovery.stop()


def test_stop_interrupts_and_joins_only_worker_connection(settings):
    entered, interrupted = threading.Event(), threading.Event()

    class Blocked(GluonSearchImap):
        def __init__(self):
            super().__init__({"All Mail": items(1, 2)}, uidnext=True)
            self.sock = Socket(interrupted)

        def uid(self, command, *args):
            if command == "FETCH":
                entered.set()
                assert interrupted.wait(3), "stop did not interrupt the worker socket"
                raise OSError("private content")
            return super().uid(command, *args)

    _, worker = job(settings, Blocked())
    worker.start()
    assert entered.wait(3)
    started = time.monotonic()
    worker.stop()
    assert time.monotonic() - started < 0.5
    assert interrupted.is_set() and worker._thread is None
    assert worker.status()["state"] == "idle"
