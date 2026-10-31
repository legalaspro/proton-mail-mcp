"""Thread seed hints stay cheap and optional while the mail index is busy."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from test_mail import client, message
from test_mail import settings as settings
from test_mail_safety import ReadOnlyImap

from proton_mail_mcp.search_index import SearchIndex
from proton_mail_mcp.threading import MAX_THREAD_HEADER_BYTES


@pytest.fixture
def index(settings):
    index = SearchIndex(settings)
    index.observe("All Mail", "1", {"20431": {"headers": {"Message-ID": "<seed@example.invalid>"}}})
    assert index.enabled and index._initialized
    return index


def test_seed_uid_is_scoped_to_message_id_folder_and_uidvalidity(index):
    assert index.seed_uid("All Mail", "1", "<seed@example.invalid>") == "20431"
    assert index.seed_uid("INBOX", "1", "<seed@example.invalid>") is None
    assert index.seed_uid("All Mail", "2", "<seed@example.invalid>") is None
    assert index.seed_uid("All Mail", "1", "<missing@example.invalid>") is None
    assert index.seed_uid("All Mail", "1", "' OR 1 = 1 --") is None


def test_seed_uid_does_not_wait_for_coverage_lock_or_write_cache(index, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Seed hints must not initialize or use the ordinary index path")

    monkeypatch.setattr(index, "_run", forbidden)
    monkeypatch.setattr(index, "_initialize", forbidden)
    before = {path.name: path.stat().st_mtime_ns for path in index.path.parent.iterdir()}
    with ThreadPoolExecutor(max_workers=1) as workers:
        with index._coverage_lock:
            assert (
                workers.submit(index.seed_uid, "All Mail", "1", "<seed@example.invalid>").result(
                    timeout=1
                )
                == "20431"
            )
    assert {path.name: path.stat().st_mtime_ns for path in index.path.parent.iterdir()} == before


def test_seed_uid_busy_sqlite_returns_without_disabling_index(index):
    before = (index.enabled, index._initialized, index.reason, index._warned)
    with closing(sqlite3.connect(index.path)) as writer, writer:
        writer.execute("BEGIN EXCLUSIVE")
        assert index.seed_uid("All Mail", "1", "<seed@example.invalid>") is None
    assert (index.enabled, index._initialized, index.reason, index._warned) == before
    assert index.seed_uid("All Mail", "1", "<seed@example.invalid>") == "20431"


@pytest.mark.parametrize("missing", ["search_metadata", "search_metadata_message_id"])
def test_seed_uid_does_not_scan_or_upgrade_old_metadata(index, missing):
    with closing(sqlite3.connect(index.path)) as database, database:
        database.execute(
            "DROP " + ("TABLE " if missing == "search_metadata" else "INDEX ") + missing
        )
    before = index.path.read_bytes()
    assert index.seed_uid("All Mail", "1", "<seed@example.invalid>") is None
    assert index.path.read_bytes() == before


def test_seed_uid_uses_existing_message_id_index(index, monkeypatch):
    connect = sqlite3.connect
    plans = []

    class PlannedConnection(sqlite3.Connection):
        def execute(self, query, parameters=()):
            if query.startswith("SELECT uid FROM search_metadata"):
                plans.extend(super().execute("EXPLAIN QUERY PLAN " + query, parameters))
            return super().execute(query, parameters)

    def traced_connect(*args, **kwargs):
        assert kwargs["timeout"] == 0 and kwargs["uri"] is True
        assert args[0].endswith("?mode=ro")
        return connect(*args, **kwargs, factory=PlannedConnection)

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    assert index.seed_uid("All Mail", "1", "<seed@example.invalid>") == "20431"
    assert len(plans) == 1
    assert "SEARCH search_metadata USING INDEX search_metadata_message_id" in plans[0][3]


@pytest.mark.parametrize(
    "failure", ["missing", "corrupt", "wrong_version", "permissions", "symlink"]
)
def test_seed_uid_unavailable_or_unsafe_cache_returns_no_hint(index, failure, caplog):
    if failure == "missing":
        index.path.unlink()
    elif failure == "corrupt":
        index.path.write_bytes(b"not sqlite")
    elif failure == "wrong_version":
        with closing(sqlite3.connect(index.path)) as database, database:
            database.execute("PRAGMA user_version = 999")
    elif failure == "permissions":
        index.path.chmod(0o644)
    else:
        original = index.path.with_name("original.sqlite3")
        index.path.rename(original)
        index.path.symlink_to(original)
    caplog.clear()
    before = (index.enabled, index._initialized, index.reason, index._warned)
    assert index.seed_uid("All Mail", "1", "<seed@example.invalid>") is None
    assert (index.enabled, index._initialized, index.reason, index._warned) == before
    assert not caplog.records


def test_seed_uid_does_not_create_wal_sidecars(index):
    with closing(sqlite3.connect(index.path)) as writer, writer:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("UPDATE search_metadata SET internaldate = 1")
        writer.commit()
        before = {path.name: path.stat().st_mtime_ns for path in index.path.parent.iterdir()}
        assert index.seed_uid("All Mail", "1", "<seed@example.invalid>") is None
        assert {
            path.name: path.stat().st_mtime_ns for path in index.path.parent.iterdir()
        } == before


@pytest.mark.parametrize("hint", ["hit", "missing", "mismatched", "truncated_mismatched"])
def test_thread_confirms_one_index_hint_then_falls_back_if_needed(settings, index, hint):
    seed = "<seed@example.invalid>"
    messages = {"20431" if hint == "hit" else "20432": message(seed)}
    if hint in {"mismatched", "truncated_mismatched"}:
        messages["20431"] = message(
            "<other@example.invalid>"
            if hint == "truncated_mismatched"
            else "<seed@example.invalid.extra>",
            subject="x" * MAX_THREAD_HEADER_BYTES if hint == "truncated_mismatched" else "Other",
        )
    fake = ReadOnlyImap({"INBOX": messages}, uidnext=True)
    mailbox = client(settings, fake)
    mailbox.search_index = index
    index.observe("INBOX", "1", {"20431": {"headers": {"Message-ID": seed}}})

    result = mailbox.read_thread(seed, include_thread=False)

    assert not result["partial"]
    [item] = result["messages"]
    assert item["message_id"] == seed and item["text"] == "Hello plain text"
    headers = [call[1] for call in fake.calls if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]]
    assert headers == (["20431"] if hint == "hit" else ["20431", "20432"])
    searches = [call for call in fake.calls if call[0] == "SEARCH"]
    if hint == "hit":
        assert searches == []
    else:
        [search] = searches
        assert search[3:5] == ("HEADER", "Message-ID")


def test_thread_missing_index_finds_old_message_without_hundreds_of_range_searches(settings, index):
    fake = ReadOnlyImap(
        {
            "INBOX": {
                "20431": message("<seed@example.invalid>"),
                "47744": message("<newest@example.invalid>"),
            }
        },
        uidnext=True,
    )
    mailbox = client(settings, fake)
    mailbox.search_index = index

    result = mailbox.read_thread("<seed@example.invalid>", include_thread=False)

    assert not result["partial"] and result["messages"][0]["alias"]["uid"] == "20431"
    [search] = [call for call in fake.calls if call[0] == "SEARCH"]
    assert search[3:5] == ("HEADER", "Message-ID")
    assert not any(call[0] == "STORE" for call in fake.calls)


@pytest.mark.parametrize("hinted", [True, False])
def test_thread_confirms_every_folder_hint_before_scanning_nonempty_folders(
    settings, index, hinted
):
    # Without All Mail, a later folder's hint is checked before INBOX is
    # scanned, and an emptied folder with a high UIDNEXT is never scanned.
    seed = "<seed@example.invalid>"
    fake = ReadOnlyImap(
        {
            "INBOX": {"20631": message("<newest@example.invalid>")},
            "Trash": {},
            "Archive": {"20431": message(seed)},
        },
        uidnext=True,
    )
    fake.next_uids["Trash"] = 50_001
    mailbox = client(settings, fake)
    mailbox.search_index = index
    if hinted:
        index.observe("Archive", "1", {"20431": {"headers": {"Message-ID": seed}}})

    result = mailbox.read_thread(seed, include_thread=False)

    assert not result["partial"] and result["messages"][0]["alias"]["folder"] == "Archive"
    searched, selected = [], None
    for call in fake.calls:
        if call[0] == "EXAMINE":
            selected = call[1].strip('"')
        elif call[0] == "SEARCH":
            searched.append(selected)
    assert searched == ([] if hinted else ["INBOX", "Archive"])


def test_thread_confirmed_hint_with_truncated_seed_headers_stays_partial(settings, index):
    seed = "<seed@example.invalid>"
    fake = ReadOnlyImap(
        {"INBOX": {"20431": message(seed, subject="x" * MAX_THREAD_HEADER_BYTES)}}, uidnext=True
    )
    mailbox = client(settings, fake)
    mailbox.search_index = index
    index.observe("INBOX", "1", {"20431": {"headers": {"Message-ID": seed}}})

    result = mailbox.read_thread(seed, include_thread=False)

    assert result["partial"] and result["messages"][0]["headers_truncated"]
    assert not any(call[0] == "SEARCH" for call in fake.calls)
