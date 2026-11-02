"""One complete inventory restores a folder's index proof, and the next search reuses it.

A fake clock charges Bridge's slow commands, so a skipped live check shows in the time.
"""

import threading
import time
from types import SimpleNamespace

import pytest
from test_canonical_index import warm_folder
from test_header_search_index import indexed_message
from test_mail import client
from test_mail import settings as settings
from test_search_index import CoverageImap

ALL_INVENTORY = ("SEARCH", "CHARSET", "UTF-8", "ALL")
SENDER = "info@studio.example"


class SlowBridge(CoverageImap):
    """Charge a fake clock for each command; EXAMINE and SEARCH are the slow ones."""

    def __init__(self, folders, clock):
        super().__init__(folders, uidnext=True)
        self.clock = clock
        self.costs = {"EXAMINE": 1.0, "SEARCH": 0.5, "FETCH": 0.05, "STATUS": 0.01}

    def select(self, folder, *, readonly):
        self.clock[0] += self.costs["EXAMINE"]
        return super().select(folder, readonly=readonly)

    def status(self, folder, fields):
        self.clock[0] += self.costs["STATUS"]
        return super().status(folder, fields)

    def uid(self, command, *args):
        self.clock[0] += self.costs[command]
        return super().uid(command, *args)

    def touched(self, folder):
        return [call[0] for call in self.calls if f'"{folder}"' in call[1:2]]

    def day_scans(self):
        """The folders a live canonical lookup searched by day, in order."""
        scans, selected = [], None
        for call in self.calls:
            if call[0] == "EXAMINE":
                selected = call[1].strip('"')
            elif call[0] == "SEARCH" and "ON" in call:
                scans.append(selected)
        return scans


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: now[0])
    return now


def account(settings, clock, folders):
    """Warm every folder as startup recovery does, then start the fake clock."""
    fake = SlowBridge(folders, clock)
    reader = client(settings, fake)
    for folder in folders:
        warm_folder(reader, folder)
    restart_clock(fake, clock)
    # A new client, as after a restart: only the persisted proofs carry over.
    return client(settings, fake), fake


def restart_clock(fake, clock):
    fake.calls.clear()
    clock[0] = 100.0


def inbox_account(settings, clock):
    items = {
        str(uid): indexed_message(f"<message-{uid}@example.invalid>", sender=SENDER)
        for uid in range(1, 6)
    }
    return account(settings, clock, {"All Mail": dict(items), "INBOX": items})


def test_one_reconcile_restores_the_mark_and_the_next_search_skips_the_live_check(settings, clock):
    reader, fake = inbox_account(settings, clock)
    index = reader.search_index
    epoch = index.coverage("INBOX")
    index.reconcile("INBOX", "1", list(fake.folders["INBOX"]))
    assert index.coverage("INBOX") is None

    first = reader.search_mail(from_=SENDER, include_snippets=False)

    assert fake.calls.count(ALL_INVENTORY) == 1
    assert index.coverage("INBOX") == epoch
    first_cost = clock[0] - 100.0
    fake.calls.clear()
    clock[0] = 100.0

    second = reader.search_mail(from_=SENDER, include_snippets=False)

    assert fake.calls.count(ALL_INVENTORY) == 0
    assert clock[0] - 100.0 == pytest.approx(first_cost - fake.costs["SEARCH"])
    assert index.coverage("INBOX") == epoch
    assert second == first


def test_missing_headers_fetched_by_the_search_restore_the_mark_in_the_same_call(settings, clock):
    # The reported shape: six live INBOX UIDs lack cached headers after an inventory.
    reader, fake = inbox_account(settings, clock)
    index = reader.search_index
    for uid in range(6, 12):
        item = indexed_message(f"<message-{uid}@example.invalid>", sender=SENDER)
        fake.folders["INBOX"][str(uid)] = item
        fake.folders["All Mail"][str(uid)] = item
    warm_folder(reader, "All Mail")
    restart_clock(fake, clock)

    first = reader.search_mail(from_=SENDER, limit=20, include_snippets=False)

    assert first["total"] == 11 and first["partial"] is False
    assert index.known_uids("INBOX", "1") == set(fake.folders["INBOX"])
    assert index.coverage("INBOX") == ("1", 11, "12")
    fake.calls.clear()

    second = reader.search_mail(from_=SENDER, limit=20, include_snippets=False)

    assert ALL_INVENTORY not in fake.calls
    assert second["total"] == 11 and second["partial"] is False


def test_expunged_rows_are_removed_and_the_mark_returns_at_the_new_epoch(settings, clock):
    reader, fake = inbox_account(settings, clock)
    index = reader.search_index
    del fake.folders["INBOX"]["2"]

    first = reader.search_mail(include_snippets=False)

    assert fake.calls.count(ALL_INVENTORY) == 1
    assert [item["uid"] for item in first["messages"]] == ["5", "4", "3", "1"]
    assert index.known_uids("INBOX", "1") == {"1", "3", "4", "5"}
    assert index.coverage("INBOX") == ("1", 4, "6")
    fake.calls.clear()

    second = reader.search_mail(include_snippets=False)

    assert ALL_INVENTORY not in fake.calls
    assert second == first


def test_same_count_with_a_new_uid_is_not_the_certified_inventory(settings, clock):
    # An expunge plus an arrival keeps the count; only UIDNEXT shows the change.
    reader, fake = inbox_account(settings, clock)
    del fake.folders["INBOX"]["2"]
    fake.folders["INBOX"]["6"] = indexed_message("<message-6@example.invalid>", sender=SENDER)

    result = reader.search_mail(include_snippets=False)

    assert fake.calls.count(ALL_INVENTORY) == 1
    assert [item["uid"] for item in result["messages"]] == ["6", "5", "4", "3", "1"]
    assert reader.search_index.coverage("INBOX") == ("1", 5, "7")


@pytest.mark.parametrize("expunged", [False, True])
def test_all_mail_catchup_with_six_missing_restores_its_mark_for_the_next_search(
    settings, clock, expunged
):
    # The reported missing=6 comes from All Mail: its catch-up alone logs that count.
    reader, fake = inbox_account(settings, clock)
    index = reader.search_index
    for uid in range(6, 12):
        fake.folders["All Mail"][str(uid)] = indexed_message(
            f"<message-{uid}@example.invalid>", sender=SENDER
        )
    if expunged:
        # A removal hidden by those arrivals sends catch-up to the full inventory.
        del fake.folders["All Mail"]["1"]

    first = reader.search_mail(from_=SENDER, include_snippets=False)

    assert first["total"] == 5
    assert (ALL_INVENTORY in fake.calls) is expunged
    assert index.coverage("All Mail") == ("1", 10 if expunged else 11, "12")
    fake.calls.clear()

    second = reader.search_mail(from_=SENDER, include_snippets=False)

    assert second["messages"] == first["messages"]
    assert [call[1] for call in fake.calls if call[0] == "EXAMINE"] == ['"INBOX"']
    assert ALL_INVENTORY not in fake.calls


def test_all_mail_with_a_header_that_cannot_be_cached_stays_uncertified(settings, clock):
    reader, fake = inbox_account(settings, clock)
    index = reader.search_index
    item = indexed_message("<unsafe@example.invalid>", sender=SENDER)
    item.headers = item.headers.replace(b"Subject: Hello", b"Subject: Unsafe\x00header")
    fake.folders["All Mail"]["6"] = item

    reader.search_mail(from_=SENDER, include_snippets=False)

    assert index.coverage("All Mail") is None
    assert index.coverage("INBOX") is not None


def test_later_pages_reuse_the_certified_inventory(settings, clock):
    reader, fake = inbox_account(settings, clock)
    uids = []
    cursor = None
    while True:
        page = reader.search_mail(limit=2, cursor=cursor, include_snippets=False)
        uids += [item["uid"] for item in page["messages"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert uids == ["5", "4", "3", "2", "1"]
    assert ALL_INVENTORY not in fake.calls


def test_every_all_inventory_restores_the_mark_it_clears(settings, clock):
    # Previews, rules and counts reach the same reconcile through _search.
    reader, fake = inbox_account(settings, clock)
    index = reader.search_index
    epoch = index.coverage("INBOX")
    deadline = reader._deadline()
    with reader._session(deadline=deadline) as (connection, _):
        reader._select(connection, "INBOX")
        reader._search(connection, ["ALL"])
    assert index.coverage("INBOX") == epoch


# Never mark a folder complete without the proof.
def test_header_that_cannot_be_cached_keeps_the_mark_cleared(settings, clock):
    reader, fake = inbox_account(settings, clock)
    index = reader.search_index
    # SQLite cannot search a NUL safely, so this row stays unknown.
    index.observe("INBOX", "1", {"3": {"headers": {"Subject": "Unsafe\x00header"}}})
    assert index.coverage("INBOX") is None

    reader.search_mail(limit=1, include_snippets=False)
    assert index.coverage("INBOX") is None
    fake.calls.clear()

    reader.search_mail(limit=1, include_snippets=False)
    assert fake.calls.count(ALL_INVENTORY) == 1
    assert index.coverage("INBOX") is None


def test_headers_outside_the_page_keep_the_mark_cleared_until_cached(settings, clock):
    reader, fake = inbox_account(settings, clock)
    index = reader.search_index
    for uid in (6, 7):
        fake.folders["INBOX"][str(uid)] = indexed_message(
            f"<message-{uid}@example.invalid>", sender=SENDER
        )

    # A plain page caches only its own headers, so UID 6 is still unknown.
    assert [
        item["uid"] for item in reader.search_mail(limit=1, include_snippets=False)["messages"]
    ] == ["7"]
    assert index.coverage("INBOX") is None
    assert index.known_uids("INBOX", "1") == set(fake.folders["INBOX"]) - {"6"}

    # The next page caches UID 6, and with it the whole inventory is known.
    assert [
        item["uid"] for item in reader.search_mail(limit=2, include_snippets=False)["messages"]
    ] == ["7", "6"]
    assert index.coverage("INBOX") == ("1", 7, "8")


def test_date_scoped_inventory_never_restores_the_mark(settings, clock):
    reader, fake = inbox_account(settings, clock)
    index = reader.search_index
    index.reconcile("INBOX", "1", list(fake.folders["INBOX"]))

    result = reader.search_mail(since="2026-01-01", include_snippets=False)

    assert result["total"] == 5
    assert ALL_INVENTORY not in fake.calls
    assert index.coverage("INBOX") is None


def test_uidvalidity_change_drops_the_old_rows_and_certifies_only_the_new_epoch(settings, clock):
    reader, fake = inbox_account(settings, clock)
    index = reader.search_index
    fake.uidvalidity = "2"

    first = reader.search_mail(limit=20, include_snippets=False)

    assert first["total"] == 5
    assert index.known_uids("INBOX", "1") == set()
    assert index.coverage("INBOX") == ("2", 5, "6")


@pytest.mark.parametrize(
    "state",
    [
        {"exists": 4, "uidnext": "6"},  # A count that differs from the inventory.
        {"exists": 5, "uidnext": "5"},  # UIDNEXT below a listed UID.
        {"exists": 5, "uidnext": None},  # A mailbox without UIDNEXT proves nothing.
    ],
)
def test_reconcile_refuses_a_state_that_does_not_describe_the_inventory(settings, clock, state):
    reader, fake = inbox_account(settings, clock)
    index = reader.search_index

    index.reconcile("INBOX", "1", list(fake.folders["INBOX"]), state=state)

    assert index.coverage("INBOX") is None


def test_reconcile_without_every_header_known_clears_the_mark(settings, clock):
    reader, fake = inbox_account(settings, clock)
    index = reader.search_index

    index.reconcile(
        "INBOX", "1", ["1", "2", "3", "4", "5", "6"], state={"exists": 6, "uidnext": "7"}
    )

    assert index.coverage("INBOX") is None
    assert index.known_uids("INBOX", "1") == {"1", "2", "3", "4", "5"}


def test_reconcile_gap_is_invisible_to_a_reader_on_another_thread(settings, clock):
    # A parallel read must not take a slow path for a proof about to return.
    reader, fake = inbox_account(settings, clock)
    index = reader.search_index
    epoch = index.coverage("INBOX")
    original = index.record_coverage
    seen, started = [], threading.Event()

    def read():
        started.set()
        seen.append(index.coverage("INBOX"))

    def paused(*args, **kwargs):
        worker = threading.Thread(target=read)
        worker.start()
        assert started.wait(3)
        result = original(*args, **kwargs)
        threads.append(worker)
        return result

    threads = []
    index.record_coverage = paused
    index.reconcile("INBOX", "1", list(fake.folders["INBOX"]), state={"exists": 5, "uidnext": "6"})
    threads[0].join(3)
    assert seen == [epoch]


def test_rebuild_keeps_the_live_inventory(settings, clock):
    reader, fake = inbox_account(settings, clock)
    reader._reindex_service = SimpleNamespace(running=True)

    result = reader.search_mail(include_snippets=False)

    assert result["total"] == 5
    assert fake.calls.count(ALL_INVENTORY) == 1


# The coverage lock's bounded wait under parallel reads.
def test_coverage_never_waits_on_an_idle_lock(settings, clock):
    reader, _ = inbox_account(settings, clock)
    index = reader.search_index
    lock = index._coverage_lock
    waits = []

    class TimedLock:
        def acquire(self, *args, **kwargs):
            started = time.perf_counter()
            acquired = lock.acquire(*args, **kwargs)
            waits.append(time.perf_counter() - started)
            return acquired

        def release(self):
            lock.release()

        def __enter__(self):
            self.acquire()

        def __exit__(self, *args):
            self.release()

    index._coverage_lock = TimedLock()
    assert index.coverage("INBOX") == ("1", 5, "6")
    assert waits and max(waits) < 0.5
