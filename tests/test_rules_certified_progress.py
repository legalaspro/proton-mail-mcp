"""Adaptive preview batches publish only fully validated synthetic identity rows."""

import pytest
from test_identity_index import inventory as inventory

from proton_mail_mcp import identity_index
from proton_mail_mcp.mail import CallDeadlineExceeded


def populate(case, count):
    case.connection.rows = {
        str(uid): {
            "uid": str(uid),
            "message_id": f"<synthetic-{uid}@example.invalid>",
            "_raw_identity_fields": [f"<synthetic-{uid}@example.invalid>"],
        }
        for uid in range(1, count + 1)
    }
    case.connection.next_uid = count + 1


def preview(case, state, fetch):
    return identity_index.certified_inventory(
        case.client,
        case.connection,
        case.deadline,
        "INBOX",
        str(case.connection.validity),
        fetch_rows=fetch,
        allow_refresh=False,
        preview_state=state,
    )


def test_certified_preview_shrinks_batches_and_counts_completed_progress(inventory):
    populate(inventory, 61)
    state = {"batch_scale": 8}

    def fetch(client, peer, deadline, folder, uids, validity, *, raw_fields):
        assert raw_fields
        peer.fetches.append(list(uids))
        deadline.expired = len(peer.fetches) == 1
        return [peer.rows[uid].copy() for uid in uids]

    with pytest.raises(CallDeadlineExceeded):
        preview(inventory, state, fetch)

    checkpoint = next(iter(state["certified_inventories"].values()))
    assert len(checkpoint["rows"]) == state["progress"] == 25
    inventory.deadline.expired = False
    state["batch_scale"] = 16

    rows = preview(inventory, state, fetch)

    assert {row["uid"] for row in rows} == set(inventory.connection.rows)
    assert [len(batch) for batch in inventory.connection.fetches] == [25, 12, 12, 12]
    assert state["progress"] == 61
    assert not inventory.path.exists()


def test_certified_preview_does_not_count_a_timed_out_fetch(inventory):
    populate(inventory, 3)
    state = {"progress": 7, "batch_scale": 1000}

    def fetch(client, peer, deadline, folder, uids, validity, *, raw_fields):
        peer.fetches.append(list(uids))
        if len(peer.fetches) == 1:
            raise CallDeadlineExceeded("Synthetic slow raw batch")
        return [peer.rows[uid].copy() for uid in uids]

    with pytest.raises(CallDeadlineExceeded):
        preview(inventory, state, fetch)

    checkpoint = next(iter(state["certified_inventories"].values()))
    assert checkpoint["rows"] == {}
    assert state["progress"] == 7

    rows = preview(inventory, state, fetch)

    assert len(rows) == 3
    assert inventory.connection.fetches == [["1"], ["1"], ["2"], ["3"]]
    assert state["progress"] == 10
