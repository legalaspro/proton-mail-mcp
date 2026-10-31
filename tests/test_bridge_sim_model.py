"""Keep optimized fixture bookkeeping faithful to mailbox identity changes."""

from bridge_sim import BridgeSim, Msg, hdr


def test_bulk_population_and_direct_mutations_preserve_mailbox_uids(monkeypatch):
    sequential, bulk = BridgeSim(), BridgeSim()
    first = Msg(hdr("<first@example.test>"), labels={"Labels/Review"})
    second = Msg(hdr("<second@example.test>"), folder="Archive", starred=True)
    for item in (first, second):
        sequential.add(item)
    bulk.add_many([first, second])
    assert bulk.uidmap == sequential.uidmap
    assert bulk.next_uid == sequential.next_uid

    # Tests change the model directly as a concurrent Bridge arrival would.
    first.folder = "Archive"
    first.labels.clear()
    second.starred = False
    del sequential.messages[second.pid]
    del bulk.messages[second.pid]
    arrival = Msg(hdr("<arrival@example.test>"), labels={"Labels/Review"}, starred=True)
    sequential.add(arrival)
    bulk.add_many([arrival])
    assert bulk.uidmap == sequential.uidmap
    assert bulk.next_uid == sequential.next_uid
    assert bulk.uidmap["INBOX"] == {2: arrival.pid}
    assert bulk.uidmap["Archive"] == {2: first.pid}
    assert bulk.uidmap["Labels/Review"] == {2: arrival.pid}
    assert bulk.uidmap["All Mail"] == {1: first.pid, 3: arrival.pid}
    assert bulk.uidmap["Starred"] == {2: arrival.pid}

    # Preserve the membership fault hooks used by approval safety tests, even
    # when they return a different iteration order from the physical messages.
    monkeypatch.setattr(bulk, "members", lambda name: [arrival.pid, first.pid])
    bulk.sync()
    assert bulk.uidmap["Folders/Reviewed"] == {1: first.pid, 2: arrival.pid}
