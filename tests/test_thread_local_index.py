"""Fresh local thread links avoid Bridge's unindexed header scans."""

from test_canonical_index import warm_folder
from test_mail import client, message
from test_mail import settings as settings
from test_search_index import CoverageImap

from proton_mail_mcp import canonical

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


def test_stale_thread_index_falls_back_to_bridge_header_search(settings):
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
