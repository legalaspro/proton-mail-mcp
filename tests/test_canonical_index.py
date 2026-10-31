"""Canonical proofs recover cheap folder changes without a metadata day scan."""

from types import SimpleNamespace

import pytest
from test_header_search_index import indexed_message
from test_mail import client
from test_mail import settings as settings
from test_search_index import CoverageImap

from proton_mail_mcp.canonical import resolve_indexed_views
from proton_mail_mcp.mail import CallDeadlineExceeded


def warm_folder(reader, folder, *, certify=True):
    deadline = reader._deadline()
    with reader._session(deadline=deadline) as (connection, _):
        state = reader._select(connection, folder)
        inventory = reader._search(connection, ["ALL"])
        list(reader._index_header_batches(connection, inventory, deadline=deadline, batch_size=500))
        if certify:
            assert reader.search_index.record_coverage(
                folder, "1", state, inventory, account=folder == "All Mail"
            )


def indexed_reader(settings):
    target = indexed_message("<target@example.invalid>")
    other = indexed_message("<other@example.invalid>")
    fake = CoverageImap(
        {"All Mail": {"70": target, "71": other}, "INBOX": {"1": target, "2": other}},
        uidnext=True,
    )
    reader = client(settings, fake)
    warm_folder(reader, "All Mail")
    warm_folder(reader, "INBOX")
    fake.calls.clear()
    return reader, fake


def indexed_lookup(reader):
    deadline = reader._deadline()
    with reader._session(deadline=deadline) as (connection, _):
        return resolve_indexed_views(
            reader,
            connection,
            deadline,
            [{"folder": "All Mail", "uidvalidity": "1", "uid": "70"}],
            real={"INBOX"},
            all_mail="All Mail",
        )


@pytest.mark.parametrize("cached_arrival", [False, True])
def test_indexed_canonical_recertifies_expunges_and_already_observed_arrivals(
    settings, cached_arrival
):
    reader, fake = indexed_reader(settings)
    other = fake.folders["INBOX"].pop("2")
    if cached_arrival:
        fake.folders["INBOX"]["3"] = other
        warm_folder(reader, "INBOX", certify=False)
        fake.calls.clear()

    indexed = indexed_lookup(reader)
    assert indexed.locations == [{"folder": "INBOX", "uidvalidity": "1", "uid": "1"}]
    assert not indexed.pending

    expected = {"1", "3"} if cached_arrival else {"1"}
    assert reader.search_index.known_uids("INBOX", "1") == expected
    assert reader.search_index.coverage("INBOX") == (
        "1",
        len(expected),
        "4" if cached_arrival else "3",
    )
    assert reader.search_index.reporting_counts() == {"all_mail_count": 2, "indexed_count": 2}
    assert not any(call[0] == "FETCH" for call in fake.calls)
    assert [call for call in fake.calls if call[0] == "SEARCH"] == [
        ("SEARCH", "CHARSET", "UTF-8", "ALL")
    ]


@pytest.mark.parametrize("reason", ["missing_headers", "rebuilding", "budget"])
def test_indexed_canonical_keeps_live_fallback_when_recertification_is_not_cheap(
    settings, monkeypatch, reason
):
    reader, fake = indexed_reader(settings)
    if reason == "missing_headers":
        fake.folders["INBOX"]["3"] = indexed_message("<late@example.invalid>")
    else:
        del fake.folders["INBOX"]["2"]
        if reason == "rebuilding":
            reader._reindex_service = SimpleNamespace(running=True)
        else:
            monkeypatch.setattr("proton_mail_mcp.search_sync.has_time", lambda *args: False)

    # All Mail's proof holds, so only this handle waits for a live scan of INBOX.
    indexed = indexed_lookup(reader)
    assert indexed.locations == [None]
    assert list(indexed.pending) == [0] and indexed.pending[0].holders == ("INBOX",)
    assert set(indexed.proofs) == {"All Mail"}
    assert not any(call[0] in {"FETCH", "SEARCH", "EXAMINE"} for call in fake.calls)


def test_indexed_canonical_rechecks_arrival_during_recertification(settings, monkeypatch):
    reader, fake = indexed_reader(settings)
    del fake.folders["INBOX"]["2"]
    original_uid = fake.uid

    def arrive_after_inventory(command, *args):
        result = original_uid(command, *args)
        if command == "SEARCH" and fake.current == "INBOX":
            fake.folders["INBOX"]["3"] = indexed_message("<late@example.invalid>")
        return result

    monkeypatch.setattr(fake, "uid", arrive_after_inventory)

    assert indexed_lookup(reader) is None
    assert "3" in fake.folders["INBOX"]
    assert not any(call[0] == "FETCH" for call in fake.calls)


@pytest.mark.parametrize("command", ["SELECT", "SEARCH"])
def test_indexed_canonical_refresh_timeout_shuts_down_without_logout(
    settings, monkeypatch, command
):
    reader, fake = indexed_reader(settings)
    del fake.folders["INBOX"]["2"]
    deadline = reader._deadline()
    original_end = deadline.end
    original_select, original_uid = fake.select, fake.uid

    def interrupt():
        assert deadline.end < original_end
        deadline.end = 0
        raise CallDeadlineExceeded("Simulated interrupted canonical inventory")

    def select(folder, *, readonly):
        if command == "SELECT" and folder == '"INBOX"':
            interrupt()
        return original_select(folder, readonly=readonly)

    def uid(operation, *args):
        if command == "SEARCH" and operation == "SEARCH" and fake.current == "INBOX":
            interrupt()
        return original_uid(operation, *args)

    monkeypatch.setattr(fake, "select", select)
    monkeypatch.setattr(fake, "uid", uid)
    monkeypatch.setattr(fake, "shutdown", lambda: fake.calls.append(("SHUTDOWN",)), raising=False)

    with reader._session(deadline=deadline) as (connection, _):
        # Search and thread callers catch canonical timeouts before leaving
        # their session. Cleanup must still avoid commands on the broken stream.
        with pytest.raises(CallDeadlineExceeded):
            resolve_indexed_views(
                reader,
                connection,
                deadline,
                [{"folder": "All Mail", "uidvalidity": "1", "uid": "70"}],
                real={"INBOX"},
                all_mail="All Mail",
            )
        assert deadline.expired

    assert ("SHUTDOWN",) in fake.calls
    assert ("LOGOUT",) not in fake.calls
