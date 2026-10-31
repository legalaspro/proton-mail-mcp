"""Watcher behavior uses real indexing and IMAP parsing with an in-memory mailbox."""

import socket
import sqlite3
import threading
from dataclasses import replace

from test_mail import FakeImap, client, message
from test_mail import settings as settings

from proton_mail_mcp import imap_idle


class WatchImap(FakeImap):
    capabilities = (b"IMAP4rev1", b"IDLE")

    def __init__(self, folders=None):
        super().__init__(folders, uidnext=True)
        self.responses = {}
        self.on_noop = lambda: None

    @property
    def untagged_responses(self):
        return self.responses

    def select(self, folder, *, readonly):
        result = super().select(folder, readonly=readonly)
        self.responses.clear()
        self.responses.update(super().untagged_responses)
        return result

    def noop(self):
        self.calls.append(("NOOP",))
        self.on_noop()
        return "OK", []

    def shutdown(self):
        self.calls.append(("SHUTDOWN",))


class ScriptedStop:
    """Replace Event.wait without sleeping; unexpected waits terminate the worker."""

    def __init__(self, on_wait=None):
        self.stopped = False
        self.waits = []
        self.on_wait = on_wait

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.on_wait is None:
            self.set()
        else:
            self.on_wait(seconds)
        return self.stopped


def rows(index):
    with sqlite3.connect(index.path) as database:
        return set(database.execute("SELECT folder, uidvalidity, uid, subject FROM messages"))


def headers_fetched(fake):
    return [call[1] for call in fake.calls if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]]


def test_idle_arrival_updates_headers_and_expunge_reconciles_without_rebuilding_history(
    settings, monkeypatch
):
    settings = replace(settings, imap_idle_enabled=True)
    fake = WatchImap(
        {"INBOX": {"1": message("<old@example.test>"), "2": message("<history@example.test>")}}
    )
    reader = client(settings, fake)
    reader.search_index.observe("INBOX", "1", {"1": {"headers": {"Subject": "Old"}}})
    watcher = imap_idle.MailWatcher(reader, settings)
    watcher._stop = ScriptedStop()
    baseline = []
    cleared = []

    def idle(connection, deadline, settings):
        if not baseline:
            baseline.append(rows(reader.search_index))
            assert headers_fetched(fake) == []
            del fake.folders["INBOX"]["1"]
            fake.folders["INBOX"]["3"] = message("<new@example.test>", subject="Invoice new")
            fake.responses.update({"EXISTS": [b"2"], "EXPUNGE": [b"1"]})
            return True
        cleared.append(imap_idle._changed(connection))
        watcher._stop.set()
        return False

    monkeypatch.setattr(imap_idle, "_idle", idle)
    watcher._run("INBOX")

    assert baseline == [{("INBOX", "1", "1", "old")}]
    assert cleared == [False]
    assert rows(reader.search_index) == {("INBOX", "1", "3", "invoice new")}
    assert headers_fetched(fake) == ["3"]
    assert all("BODY.PEEK[HEADER" in call[2] for call in fake.calls if call[0] == "FETCH")
    assert all(call[2] is True for call in fake.calls if call[0] == "EXAMINE")
    assert {call[0] for call in fake.calls} <= {
        "STARTTLS",
        "LOGIN",
        "CAPABILITY",
        "EXAMINE",
        "SEARCH",
        "FETCH",
        "LOGOUT",
    }
    assert watcher._statuses == {"INBOX": "stopped"}
    assert watcher._connections == {}


def test_reconnect_retains_inventory_and_caps_backoff_without_duplicate_header_fetches(
    settings, monkeypatch
):
    settings = replace(settings, imap_idle_enabled=True)
    fake = WatchImap({"INBOX": {"1": message("<history@example.test>")}})
    reader = client(settings, fake)
    watcher = imap_idle.MailWatcher(reader, settings)
    now = [0.0]
    attempts = []

    def wait(seconds):
        now[0] += seconds
        fake.folders["INBOX"]["2"] = message("<arrival@example.test>", subject="During drop")
        if len(watcher._stop.waits) > 8:
            watcher._stop.set()

    watcher._stop = ScriptedStop(wait)
    monkeypatch.setattr(imap_idle.time, "monotonic", lambda: now[0])

    def idle(connection, deadline, settings):
        attempts.append(connection)
        if len(attempts) <= 8:
            raise OSError("simulated disconnected stream")
        watcher._stop.set()
        return False

    monkeypatch.setattr(imap_idle, "_idle", idle)
    watcher._run("INBOX")

    assert watcher._stop.waits == [1, 2, 4, 8, 16, 32, 60, 60]
    assert len(attempts) == 9
    assert len([call for call in fake.calls if call[0] == "CAPABILITY"]) == 9
    assert len([call for call in fake.calls if call[0] == "SHUTDOWN"]) == 8
    assert headers_fetched(fake) == ["2"]
    assert rows(reader.search_index) == {("INBOX", "1", "2", "during drop")}
    assert watcher._connections == {}


def test_no_idle_uses_noop_interval_and_logs_fallback_once_across_reconnect(
    settings, monkeypatch, caplog
):
    settings = replace(settings, imap_idle_enabled=True, imap_idle_poll_interval=90)
    fake = WatchImap({"INBOX": {"1": message("<history@example.test>")}})
    fake.capabilities = (b"IMAP4rev1",)
    reader = client(settings, fake)
    watcher = imap_idle.MailWatcher(reader, settings)
    idle_calls = []
    monkeypatch.setattr(imap_idle, "_idle", lambda *args: idle_calls.append(args))

    def wait(seconds):
        if len(watcher._stop.waits) >= 4:
            watcher._stop.set()

    watcher._stop = ScriptedStop(wait)

    def noop():
        if len([call for call in fake.calls if call[0] == "NOOP"]) == 1:
            fake.folders["INBOX"]["2"] = message("<poll@example.test>", subject="Fallback")
        else:
            raise OSError("simulated drop")

    fake.on_noop = noop
    watcher._run("INBOX")

    assert watcher._stop.waits == [90, 90, 1, 90]
    assert idle_calls == []
    assert len([call for call in fake.calls if call[0] == "NOOP"]) == 2
    assert len([call for call in fake.calls if call[0] == "CAPABILITY"]) == 2
    assert headers_fetched(fake) == ["2"]
    assert rows(reader.search_index) == {("INBOX", "1", "2", "fallback")}
    assert [record.message for record in caplog.records if record.name == imap_idle.__name__] == [
        "IMAP IDLE unavailable; using periodic NOOP checks"
    ]


def test_uidvalidity_reset_discards_index_and_reused_uids_establish_new_baseline(
    settings, monkeypatch
):
    settings = replace(settings, imap_idle_enabled=True)
    fake = WatchImap({"INBOX": {"1": message("<old@example.test>")}})
    reader = client(settings, fake)
    reader.search_index.observe("INBOX", "1", {"1": {"headers": {"Subject": "Old"}}})
    watcher = imap_idle.MailWatcher(reader, settings)
    watcher._stop = ScriptedStop()
    stages = []
    reset_rows = []

    def idle(connection, deadline, settings):
        stages.append(None)
        if len(stages) == 1:
            fake.uidvalidity = "2"
            fake.folders["INBOX"] = {"1": message("<replacement@example.test>")}
            fake.responses["UIDVALIDITY"] = [b"2"]
            return True
        if len(stages) == 2:
            reset_rows.append(rows(reader.search_index))
            fake.folders["INBOX"]["2"] = message("<new@example.test>", subject="Fresh")
            fake.responses["EXISTS"] = [b"2"]
            return True
        watcher._stop.set()
        return False

    monkeypatch.setattr(imap_idle, "_idle", idle)
    watcher._run("INBOX")

    assert reset_rows == [set()]
    assert headers_fetched(fake) == ["2"]
    assert rows(reader.search_index) == {("INBOX", "2", "2", "fresh")}


def test_folder_workers_do_not_block_tools_and_stop_interrupts_login_and_idle(
    settings, monkeypatch
):
    settings = replace(settings, imap_idle_enabled=True, imap_idle_folders=("INBOX", "Archive"))
    reader = client(settings, WatchImap())
    watcher = imap_idle.MailWatcher(reader, settings)
    login_entered, idle_entered = threading.Event(), threading.Event()
    connections = []
    timed_out = []
    factory_lock = threading.Lock()

    class Socket:
        def __init__(self):
            self.closed = threading.Event()
            self.shutdowns = []

        def settimeout(self, seconds):
            pass

        def shutdown(self, how):
            self.shutdowns.append(how)
            self.closed.set()

    def factory(host, port, *, timeout):
        fake = WatchImap({"INBOX": {}, "Archive": {}})
        fake.sock = Socket()
        with factory_lock:
            connections.append(fake)
            first = len(connections) == 1
        if first:

            def login(username, password):
                login_entered.set()
                if not fake.sock.closed.wait(2):
                    timed_out.append("login")
                raise OSError("simulated interrupted login")

            fake.login = login
        return fake

    def idle(connection, deadline, settings):
        idle_entered.set()
        if not connection.sock.closed.wait(2):
            timed_out.append("idle")
        raise OSError("simulated interrupted IDLE")

    reader.imap_factory = factory
    monkeypatch.setattr(imap_idle, "_idle", idle)
    watcher.start()
    workers = list(watcher._threads)
    try:
        assert login_entered.wait(2)
        assert idle_entered.wait(2)
        assert len(workers) == 2
        assert all(worker.is_alive() for worker in workers)
        with watcher._lock:
            active = dict(watcher._connections)
        # Registration precedes authentication, so stop can wake the first
        # session even though the other folder is already receiving updates.
        assert set(active) == {"INBOX", "Archive"}
        assert len({id(connection) for connection in active.values()}) == 2
        assert set(active.values()) == set(connections)
        assert set(watcher._statuses.values()) == {"starting", "idle"}

        found = reader.search_mail(include_snippets=False)
        assert found["messages"] == []
        assert len(connections) == 3
        assert connections[2] not in active.values()
        assert connections[2].calls[-1] == ("LOGOUT",)
        assert all(not connection.sock.closed.is_set() for connection in connections)
    finally:
        watcher.stop()

    assert timed_out == []
    assert all(connection.sock.shutdowns == [socket.SHUT_RDWR] for connection in connections[:2])
    assert connections[2].sock.shutdowns == []
    assert all(not worker.is_alive() for worker in workers)
    assert watcher._threads == []
    assert watcher._connections == {}
    assert watcher._statuses == {"INBOX": "stopped", "Archive": "stopped"}
