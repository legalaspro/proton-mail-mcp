"""Obsolete caches are discarded without loading or dropping their FTS tables."""

import logging
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import replace

import pytest
from test_imap_idle import ScriptedStop, WatchImap
from test_mail import client, message
from test_mail import settings as settings

from proton_mail_mcp import imap_idle
from proton_mail_mcp.background_sync import BackgroundSync
from proton_mail_mcp.search_index import SearchIndex

# Keep the released v1 layout independent of the current production schema.
_V1_SCHEMA = """
CREATE TABLE mailboxes (folder TEXT PRIMARY KEY, uidvalidity TEXT NOT NULL);
CREATE TABLE messages (
    rowid INTEGER PRIMARY KEY, folder TEXT NOT NULL, uidvalidity TEXT NOT NULL,
    uid TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '',
    from_addr TEXT NOT NULL DEFAULT '', to_addr TEXT NOT NULL DEFAULT '',
    cc TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
    headers_known INTEGER NOT NULL DEFAULT 0, UNIQUE (folder, uidvalidity, uid)
);
CREATE VIRTUAL TABLE message_fts USING fts5(
    subject, from_addr, to_addr, cc, body, content='messages',
    content_rowid='rowid', tokenize='trigram case_sensitive 1'
);
CREATE TRIGGER messages_insert AFTER INSERT ON messages BEGIN
    INSERT INTO message_fts(rowid, subject, from_addr, to_addr, cc, body)
    VALUES (new.rowid, new.subject, new.from_addr, new.to_addr, new.cc, new.body);
END;
CREATE TABLE coverage (folder TEXT PRIMARY KEY, uidvalidity TEXT NOT NULL,
    message_count INTEGER NOT NULL, uidnext TEXT NOT NULL, certified INTEGER NOT NULL);
INSERT INTO mailboxes VALUES ('All Mail', '7');
INSERT INTO messages(rowid, folder, uidvalidity, uid, subject, body, headers_known)
    VALUES (42, 'All Mail', '7', '9', 'old private subject', 'old private body', 1);
INSERT INTO coverage VALUES ('All Mail', '7', 1, '10', 1);
PRAGMA user_version = 1;
"""
_DISCARD_MESSAGE = "Old search index cache discarded; rebuilding from scratch"


@pytest.fixture
def v1_index_path(settings):
    path = SearchIndex(settings).path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as database, database:
        database.executescript(_V1_SCHEMA)
    path.chmod(0o600)
    path.with_name(path.name + ".lock").touch(mode=0o600)
    return path


def assert_empty_v2(path):
    with closing(sqlite3.connect(path)) as database, database:
        assert database.execute("PRAGMA user_version").fetchone() == (2,)
        tables = {
            "messages",
            "mailboxes",
            "coverage",
            "index_roles",
            "search_metadata",
            "index_sync",
        }
        assert {
            row[0]
            for row in database.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        } == tables
        for table in tables:
            assert database.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        assert database.execute("PRAGMA quick_check").fetchone() == ("ok",)


@pytest.mark.parametrize("fts_available", [True, False], ids=["fts5", "no-fts5"])
def test_v1_fts_cache_is_discarded_without_virtual_table_ddl(
    settings, v1_index_path, monkeypatch, caplog, fts_available
):
    path = v1_index_path
    if not fts_available:
        # A missing virtual-table module behaves like SQLite built without FTS5.
        # PRAGMA/schema inspection works, but accessing or dropping the old table
        # needs a module that this connection cannot load.
        with closing(sqlite3.connect(path)) as database, database:
            database.execute("PRAGMA writable_schema = ON")
            database.execute(
                "UPDATE sqlite_master SET sql = replace(sql, 'USING fts5', "
                "'USING unavailable_fts5') WHERE name = 'message_fts'"
            )
        with closing(sqlite3.connect(path)) as database, database:
            with pytest.raises(sqlite3.OperationalError, match="no such module"):
                database.execute("SELECT * FROM message_fts").fetchall()

    connections = []
    statements = []
    unlinked = []
    connect = sqlite3.connect
    unlink = os.unlink

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            super().close()
            self.closed = True

    def no_virtual_table_ddl(*args, **kwargs):
        database = connect(*args, factory=TrackedConnection, **kwargs)
        connections.append(database)
        database.set_trace_callback(statements.append)

        def authorize(action, *args):
            if action in (sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_VTABLE):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        database.set_authorizer(authorize)
        return database

    def unlink_closed_cache(name, *args, **kwargs):
        base = os.path.basename(name)
        if base in {path.name, path.name + "-wal", path.name + "-shm"}:
            assert connections and all(database.closed for database in connections)
            unlinked.append(base)
        return unlink(name, *args, **kwargs)

    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        sidecar.write_bytes(b"obsolete private cache sidecar")
        sidecar.chmod(0o600)
    monkeypatch.setattr(sqlite3, "connect", no_virtual_table_ddl)
    monkeypatch.setattr(os, "unlink", unlink_closed_cache)
    caplog.clear()
    caplog.set_level(logging.WARNING, logger="proton_mail_mcp.search_index")

    index = SearchIndex(settings)

    assert index.enabled and index.reason is None
    assert path.name in unlinked
    assert not any(statement.lstrip().upper().startswith("DROP") for statement in statements)
    assert all(database.closed for database in connections)
    for suffix in ("-wal", "-shm"):
        assert not path.with_name(path.name + suffix).exists()
    assert_empty_v2(path)
    assert index.coverage("All Mail") is None
    assert index.reporting_counts() is None
    assert caplog.messages == [_DISCARD_MESSAGE]
    assert str(path) not in caplog.text
    assert "old private" not in caplog.text
    # Ordinary writes and a second open use the replacement header-only cache.
    index.observe("All Mail", "1", {"1": {"headers": {"Subject": "Invoice"}}})
    reopened = SearchIndex(settings)
    assert reopened.cached_headers("All Mail", "1", ["1"])["1"]["Subject"] == "invoice"
    assert caplog.messages == [_DISCARD_MESSAGE]


@pytest.mark.parametrize("version", [0, 3])
def test_non_v2_cache_is_discarded(settings, version, caplog):
    path = SearchIndex(settings).path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("CREATE TABLE obsolete_cache (private_content TEXT)")
        database.execute("INSERT INTO obsolete_cache VALUES ('old private data')")
        database.execute(f"PRAGMA user_version = {version}")
    path.chmod(0o600)
    path.with_name(path.name + ".lock").touch(mode=0o600)
    caplog.clear()

    index = SearchIndex(settings)

    assert index.enabled and index.reason is None
    assert_empty_v2(path)
    with closing(sqlite3.connect(path)) as database, database:
        assert not database.execute(
            "SELECT name FROM sqlite_master WHERE name = 'obsolete_cache'"
        ).fetchall()
    assert caplog.messages == [_DISCARD_MESSAGE]


@pytest.mark.parametrize(
    "module", ["fts5", "/* old FTS module */ fts5", '"fts5"'], ids=["plain", "comment", "quoted"]
)
def test_v2_cache_containing_fts_is_discarded(settings, caplog, module):
    path = SearchIndex(settings, rebuild=True).path
    with closing(sqlite3.connect(path)) as database, database:
        database.execute(f"CREATE VIRTUAL TABLE unrelated_search USING {module}(content)")
        database.execute("INSERT INTO unrelated_search VALUES ('old private data')")
    caplog.clear()

    index = SearchIndex(settings)

    assert index.enabled and index.reason is None
    assert_empty_v2(path)
    assert caplog.messages == [_DISCARD_MESSAGE]


@pytest.mark.parametrize("populated", [False, True], ids=["fresh", "populated"])
def test_v2_cache_opens_untouched(settings, caplog, populated):
    original = SearchIndex(settings, rebuild=True)
    if populated:
        original.observe("All Mail", "1", {"1": {"headers": {"Subject": "Invoice"}}})
        assert original.record_coverage("All Mail", "1", {"exists": 1, "uidnext": "2"}, ["1"])
    contents = original.path.read_bytes()
    info = original.path.stat()
    caplog.clear()

    reopened = SearchIndex(settings)

    assert reopened.enabled and reopened.reason is None
    assert reopened.path.stat().st_ino == info.st_ino
    assert reopened.path.stat().st_mtime_ns == info.st_mtime_ns
    assert reopened.path.read_bytes() == contents
    if populated:
        assert reopened.coverage("All Mail") == ("1", 1, "2")
        assert reopened.reporting_counts() == {"all_mail_count": 1, "indexed_count": 1}
        assert reopened.cached_headers("All Mail", "1", ["1"])["1"]["Subject"] == "invoice"
    else:
        assert_empty_v2(reopened.path)
    assert caplog.messages == []


@pytest.mark.parametrize("maintenance", ["background", "idle"])
def test_rebuilding_discarded_cache_restores_all_mail_maintenance(
    settings, v1_index_path, monkeypatch, maintenance
):
    settings = replace(
        settings,
        background_sync_enabled=maintenance == "background",
        imap_idle_enabled=maintenance == "idle",
    )
    first = message("<1@test.invalid>")
    fake = WatchImap({"INBOX": {"11": first}, "All Mail": {"1": first}})
    reader = client(settings, fake)
    assert reader.search_index.reporting_counts() is None
    assert reader.search_index.coverage("All Mail") is None

    rebuilt = reader.rebuild_search_index("All Mail")

    assert rebuilt == {"folder": "All Mail", "indexed": 1, "available": True}
    with closing(sqlite3.connect(v1_index_path)) as database, database:
        assert database.execute("SELECT * FROM index_roles").fetchall() == [
            ("all_mail", "All Mail")
        ]
    # Check persisted eligibility, including a process restart after the rebuild.
    reader = client(settings, fake)
    assert reader.search_index.reporting_counts() == {"all_mail_count": 1, "indexed_count": 1}
    fake.folders["All Mail"]["2"] = message("<2@test.invalid>")
    fake.calls.clear()
    if maintenance == "background":
        sync = BackgroundSync(reader, settings)
        sync._last_request = time.monotonic() - 60

        sync._tick()

        assert sync.status()["last_skip_reason"] is None
        assert sync.status()["last_outcome"] == "caught_up"
        assert sync.status()["rows_today"] == 1
    else:
        watcher = imap_idle.MailWatcher(reader, settings)
        watcher._stop = ScriptedStop()
        proofs = []

        def idle(connection, deadline, settings):
            proofs.append(reader.search_index.coverage("All Mail"))
            watcher._stop.set()
            return False

        monkeypatch.setattr(imap_idle, "_idle", idle)

        watcher._run("INBOX")

        assert watcher._roles_loaded and watcher._all_mail == "All Mail"
        assert proofs == [("1", 2, "3")]
    assert reader.search_index.coverage("All Mail") == ("1", 2, "3")
    assert reader.search_index.known_uids("All Mail", "1") == {"1", "2"}
    assert [call[1] for call in fake.calls if call[0] == "FETCH"] == ["2"]
    assert all("BODY.PEEK[HEADER" in call[2] for call in fake.calls if call[0] == "FETCH")
