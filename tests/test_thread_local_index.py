"""Fresh local thread links avoid Bridge's unindexed header scans."""

from dataclasses import replace

from test_canonical_index import warm_folder
from test_mail import client, message
from test_mail import settings as settings
from test_search_index import CoverageImap

from proton_mail_mcp import canonical
from proton_mail_mcp.threading import _EXPANSION_LIMIT

ROOT = "<root@example.invalid>"
CHILD = "<réponse@example.invalid>"
LEAF = "<leaf@example.invalid>"


def indexed_thread(settings, *, leaf=True):
    messages = {
        "70": message(ROOT),
        "71": message(CHILD, reply=ROOT),
        "72": message(LEAF, references=CHILD),
    }
    if not leaf:
        del messages["72"]
    fake = CoverageImap(
        {
            "All Mail": messages,
            "INBOX": {str(int(uid) - 69): item for uid, item in messages.items()},
        },
        uidnext=True,
    )
    reader = client(settings, fake)
    warm_folder(reader, "All Mail")
    warm_folder(reader, "INBOX")
    fake.calls.clear()
    return client(settings, fake), fake


def test_fresh_thread_index_preserves_canonical_handles_before_slow_bridge_search(
    settings, monkeypatch
):
    reader, fake = indexed_thread(settings)
    clock = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: clock[0])
    original_uid = fake.uid

    def slow_header_search(command, *args):
        if command == "SEARCH" and "HEADER" in args:
            clock[0] += 15.0
        return original_uid(command, *args)

    monkeypatch.setattr(fake, "uid", slow_header_search)
    seed_locations = []
    describe = canonical.describe

    def describe_seed(item, found, location):
        if item["message_id"] == ROOT and location is not None:
            seed_locations.append(location)
        describe(item, found, location)

    monkeypatch.setattr(canonical, "describe", describe_seed)
    thread_uids = reader.search_index.thread_uids
    expansions = []

    def indexed_expansion(*args):
        assert seed_locations
        assert seed_locations[0] == {"folder": "INBOX", "uidvalidity": "1", "uid": "1"}
        matches = thread_uids(*args)
        assert matches is not None
        expansions.append(matches)
        return matches

    monkeypatch.setattr(reader.search_index, "thread_uids", indexed_expansion)

    result = reader.read_thread(ROOT)

    assert result["partial"] is False and not result.get("timed_out")
    assert expansions
    assert {
        item["message_id"]: (item["folder"], item["uidvalidity"], item["uid"])
        for item in result["messages"]
    } == {ROOT: ("INBOX", "1", "1"), CHILD: ("INBOX", "1", "2"), LEAF: ("INBOX", "1", "3")}
    assert all(item["text"] == "Hello plain text" for item in result["messages"])
    assert not any(call[0] == "SEARCH" and "HEADER" in call for call in fake.calls)
    assert clock == [100.0]


def test_stale_thread_index_catches_up_with_no_bridge_header_search(settings):
    reader, fake = indexed_thread(settings, leaf=False)
    # This mailbox change predates the request and invalidates its cached proof.
    leaf = message(LEAF, references=CHILD)
    fake.folders["All Mail"]["72"] = leaf
    fake.folders["INBOX"]["3"] = leaf

    result = reader.read_thread(ROOT)

    assert result["partial"] is False
    assert {item["message_id"] for item in result["messages"]} == {ROOT, CHILD, LEAF}
    assert not any(call[0] == "SEARCH" and "HEADER" in call for call in fake.calls)
    assert reader.search_index.coverage("All Mail") == ("1", 3, "73")


def test_read_with_no_expansion_does_not_catch_up(settings):
    reader, fake = indexed_thread(settings, leaf=False)
    fake.folders["All Mail"]["72"] = message(LEAF, references=CHILD)

    reader.read_thread(ROOT, include_thread=False)

    assert reader.search_index.coverage("All Mail") == ("1", 2, "72")


def test_index_that_cannot_catch_up_falls_back_to_bridge_header_search(settings):
    settings = replace(settings, search_index_catchup_max_messages=0)
    reader, fake = indexed_thread(settings, leaf=False)
    assert reader.search_index.coverage("All Mail") == ("1", 2, "72")
    # This mailbox change predates the request and invalidates its cached proof.
    leaf = message(LEAF, references=CHILD)
    fake.folders["All Mail"]["72"] = leaf
    fake.folders["INBOX"]["3"] = leaf

    result = reader.read_thread(ROOT)

    assert result["partial"] is False
    assert {item["message_id"] for item in result["messages"]} == {ROOT, CHILD, LEAF}
    assert all(item["folder"] == "INBOX" and item["uid"] for item in result["messages"])
    assert any(call[0] == "SEARCH" and "OR" in call and "HEADER" in call for call in fake.calls)


def test_large_mailbox_skips_a_doomed_search_and_uses_cached_links(settings, monkeypatch):
    monkeypatch.setattr("proton_mail_mcp.threading._LIVE_HEADER_SEARCH_MAX_MESSAGES", 1)
    settings = replace(settings, search_index_catchup_max_messages=0)
    reader, fake = indexed_thread(settings, leaf=False)
    fake.folders["All Mail"]["72"] = message(LEAF, references=CHILD)

    result = reader.read_thread(ROOT)

    # The arrival's links are not cached, so the answer says it may be incomplete.
    assert result["partial"] is True and "timed_out" not in result
    assert result["partial_reason"].startswith(_EXPANSION_LIMIT)
    assert {item["message_id"] for item in result["messages"]} == {ROOT, CHILD}
    assert not any(call[0] == "SEARCH" and "HEADER" in call for call in fake.calls)


def test_cached_links_are_confirmed_by_a_live_fetch(settings, monkeypatch):
    monkeypatch.setattr("proton_mail_mcp.threading._LIVE_HEADER_SEARCH_MAX_MESSAGES", 1)
    settings = replace(settings, search_index_catchup_max_messages=0)
    reader, fake = indexed_thread(settings)
    # The cached link is stale: the leaf has gone, and something else has its UID's place.
    del fake.folders["All Mail"]["72"]
    fake.folders["All Mail"]["73"] = message("<unrelated@example.invalid>")

    result = reader.read_thread(ROOT)

    assert {item["message_id"] for item in result["messages"]} == {ROOT, CHILD}
    assert result["partial"] is True


def test_thread_hints_answer_only_where_links_are_cached(settings):
    reader, _ = indexed_thread(settings)

    assert reader.search_index.thread_hints("Archive", "1", [ROOT]) is None
    assert set(reader.search_index.thread_hints("All Mail", "1", [ROOT])) == {"70", "71"}


def test_no_cached_links_leaves_the_search_to_bridge(settings, monkeypatch):
    monkeypatch.setattr("proton_mail_mcp.threading._LIVE_HEADER_SEARCH_MAX_MESSAGES", 1)
    messages = {"70": message(ROOT), "71": message(CHILD, reply=ROOT)}
    fake = CoverageImap({"All Mail": messages, "INBOX": {}}, uidnext=True)
    reader = client(settings, fake)

    result = reader.read_thread(ROOT)

    assert any(call[0] == "SEARCH" and "HEADER" in call for call in fake.calls)
    assert {item["message_id"] for item in result["messages"]} == {ROOT, CHILD}
    assert result.get("partial_reason") != _EXPANSION_LIMIT


def test_catch_up_that_loses_the_stream_ends_with_a_new_session(settings):
    reader, fake = indexed_thread(settings, leaf=False)
    fake.folders["All Mail"]["72"] = message(LEAF, references=CHILD)
    original = fake.uid
    interrupted = []

    def stalled_tail(command, *args):
        if command == "SEARCH" and "UID" in args and not interrupted:
            interrupted.append(True)
            raise TimeoutError
        return original(command, *args)

    fake.uid = stalled_tail

    result = reader.read_thread(ROOT)

    assert interrupted
    assert result["partial"] is True and result["timed_out"] is True
    [item] = result["messages"]
    assert item["message_id"] == ROOT and item["text"] == "Hello plain text"
    assert sum(call[0] == "EXAMINE" for call in fake.calls) >= 1


def test_rebuild_keeps_catch_up_to_itself(settings, monkeypatch):
    reader, fake = indexed_thread(settings, leaf=False)
    fake.folders["All Mail"]["72"] = message(LEAF, references=CHILD)
    monkeypatch.setattr("proton_mail_mcp.search._index_rebuilding", lambda client: True)

    reader.read_thread(ROOT)

    assert reader.search_index.coverage("All Mail") == ("1", 2, "72")
