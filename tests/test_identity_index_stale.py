"""An existing certified inventory cannot authorize a later unstable snapshot."""

from test_identity_index import inventory as inventory


def test_warm_inventory_drift_refuses_without_reusing_saved_rows(inventory):
    assert inventory.run()
    path = next(inventory.path.glob("*.json"))
    before = path.read_bytes()
    peer = inventory.connection
    peer.status_hook = lambda current: setattr(current, "modseq", current.modseq + 1)
    assert inventory.run() is None
    assert path.read_bytes() == before
    assert peer.fetches == [["1"]]


def test_nonempty_inventory_reconciles_expunge_without_retaining_stale_rows(inventory):
    peer = inventory.connection
    peer.rows["2"] = {
        "uid": "2",
        "message_id": "<two@example.invalid>",
        "_raw_identity_fields": ["<two@example.invalid>"],
    }
    peer.next_uid = 3
    assert {row["uid"] for row in inventory.run()} == {"1", "2"}
    del peer.rows["2"]
    peer.modseq += 1
    assert [row["uid"] for row in inventory.run()] == ["1"]
    assert peer.fetches == [["1", "2"], []]
