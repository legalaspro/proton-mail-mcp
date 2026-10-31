"""Resumable rules identity discovery uses only synthetic mail and private state."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_identity_index import inventory as inventory

from proton_mail_mcp import cleanup_lookup, identity_index
from proton_mail_mcp.mail import CallDeadlineExceeded, MailValidationError


def cached_inventory(case):
    return identity_index.certified_inventory(
        case.client,
        case.connection,
        case.deadline,
        "INBOX",
        str(case.connection.validity),
        fetch_rows=lambda *args, **kwargs: pytest.fail("Preview refreshed the index"),
        allow_refresh=False,
    )


def test_preview_index_never_creates_missing_storage(inventory):
    assert cached_inventory(inventory) is None
    assert not inventory.path.exists()
    assert not inventory.connection.fetches


def test_preview_index_reuses_only_exact_saved_snapshot(inventory):
    assert inventory.run()
    path = next(inventory.path.glob("*.json"))
    original = path.read_bytes()
    assert cached_inventory(inventory)
    inventory.connection.modseq += 1
    assert cached_inventory(inventory) is None
    assert path.read_bytes() == original
    assert inventory.connection.fetches == [["1"]]
    assert not any("CHANGEDSINCE" in command[-1] for command in inventory.connection.commands)


def certified_preview(case, state=None):
    def fetch(client, peer, deadline, folder, uids, validity, *, raw_fields):
        assert raw_fields
        peer.fetches.append(list(uids))
        return [peer.rows[uid].copy() for uid in uids]

    return identity_index.certified_inventory(
        case.client,
        case.connection,
        case.deadline,
        "INBOX",
        str(case.connection.validity),
        fetch_rows=fetch,
        allow_refresh=False,
        preview_state={} if state is None else state,
    )


def test_certified_previews_reconcile_deltas_without_creating_storage(inventory):
    assert certified_preview(inventory)
    assert not inventory.path.exists()
    peer = inventory.connection
    assert peer.fetches == [["1"]]
    assert certified_preview(inventory)
    assert peer.fetches == [["1"]]
    peer.rows["1"]["_raw_identity_fields"] = ["<one@example.invalid>", "<hidden@example.invalid>"]
    peer.rows["1"]["identity_invalid"] = True
    peer.modseq, peer.changed = 2, {"1": 2}

    rows = certified_preview(inventory)

    assert rows[0]["identity_invalid"]
    assert rows[0]["_raw_identity_fields"] == ["<one@example.invalid>", "<hidden@example.invalid>"]
    assert peer.fetches == [["1"], ["1"]]
    assert any("CHANGEDSINCE" in command[-1] for command in peer.commands)
    assert not inventory.path.exists()


@pytest.mark.parametrize("scope", ["client", "account", "endpoint"])
def test_certified_preview_cache_is_scoped_to_client_and_account(inventory, scope):
    assert certified_preview(inventory)
    if scope == "client":
        inventory.client = SimpleNamespace(
            settings=inventory.client.settings, _search=inventory.client._search
        )
    else:
        changes = (
            {"bridge_user": "other@example.invalid"}
            if scope == "account"
            else {"imap_host": "127.0.0.2"}
        )
        inventory.client.settings = replace(inventory.client.settings, **changes)
    inventory.connection.rows["1"]["_raw_identity_fields"] = ["<different@example.invalid>"]

    rows = certified_preview(inventory)

    assert rows[0]["_raw_identity_fields"] == ["<different@example.invalid>"]
    assert inventory.connection.fetches == [["1"], ["1"]]
    assert not inventory.path.exists()


class Deadline:
    expired = False

    def check(self, connection=None):
        if self.expired:
            raise CallDeadlineExceeded("Synthetic preview deadline reached")


def raw_row(uid, identifier=None):
    identifier = identifier or f"<synthetic-{uid}@example.invalid>"
    return {"uid": uid, "message_id": identifier, "_raw_identity_fields": [identifier]}


def raw_checkpoint():
    saved = {
        "validity": "1",
        "uids": ["1"],
        "rows": [{**raw_row("1"), "identity_invalid": False}],
        "snapshot": None,
        "options": None,
    }
    saved["checksum"] = cleanup_lookup._preview_inventory_checksum(saved)
    return saved


def test_preview_raw_audit_keeps_completed_batches_on_deadline(monkeypatch):
    peer = SimpleNamespace(_rules_preview_state={})
    deadline = Deadline()
    uids = [str(uid) for uid in range(1, 451)]
    reads = []
    monkeypatch.setattr(cleanup_lookup, "enumerate_uids", lambda *args: uids.copy())

    def fetch(*args, **kwargs):
        requested = args[4]
        reads.append(requested)
        if len(reads) == 1:
            deadline.expired = True
        return [raw_row(uid) for uid in requested]

    monkeypatch.setattr(cleanup_lookup, "fetch_rows", fetch)
    with pytest.raises(CallDeadlineExceeded):
        cleanup_lookup._preview_raw_inventory(None, peer, deadline, "INBOX", "1", "membership")
    assert len(peer._rules_preview_state["inventories"]["INBOX"]["rows"]) == 200
    deadline.expired = False
    rows = cleanup_lookup._preview_raw_inventory(None, peer, deadline, "INBOX", "1", "membership")
    assert [row["uid"] for row in rows] == uids
    assert [len(batch) for batch in reads] == [200, 200, 50]
    assert [uid for batch in reads for uid in batch] == uids


def test_preview_raw_audit_refuses_uidvalidity_change(monkeypatch):
    saved = raw_checkpoint()
    peer = SimpleNamespace(_rules_preview_state={"inventories": {"INBOX": saved}})
    monkeypatch.setattr(cleanup_lookup, "enumerate_uids", lambda *args: ["1"])
    monkeypatch.setattr(
        cleanup_lookup,
        "fetch_rows",
        lambda *args, **kwargs: pytest.fail("Changed audit fetched mail"),
    )
    with pytest.raises(MailValidationError, match="Rules preview mailbox changed"):
        cleanup_lookup._preview_raw_inventory(None, peer, Deadline(), "INBOX", "2", "membership")


@pytest.mark.parametrize("uids", [["2"], []])
def test_preview_raw_audit_reconciles_membership_changes(monkeypatch, uids):
    saved = raw_checkpoint()
    peer = SimpleNamespace(_rules_preview_state={"inventories": {"INBOX": saved}})
    monkeypatch.setattr(cleanup_lookup, "enumerate_uids", lambda *args: uids)
    reads = []

    def fetch(*args, **kwargs):
        reads.append(args[4])
        return [raw_row(uid) for uid in args[4]]

    monkeypatch.setattr(cleanup_lookup, "fetch_rows", fetch)
    rows = cleanup_lookup._preview_raw_inventory(None, peer, Deadline(), "INBOX", "1", "membership")
    assert [row["uid"] for row in rows] == uids
    assert saved["uids"] == uids
    assert reads == ([uids] if uids else [])


def test_preview_raw_audit_refreshes_matches_and_preserves_hidden_identity(monkeypatch):
    wanted = "<wanted@example.invalid>"
    peer = SimpleNamespace(
        _rules_preview_state={},
        _cleanup_raw_identity=True,
        _cleanup_inventory={},
        uid=lambda *args: ("OK", [b"1 2"]),
    )

    def select(connection, folder):
        connection._selected_inventory = {"folder": folder, "exists": 2, "uidnext": "3"}
        return connection._selected_inventory

    client = SimpleNamespace(_select=select)
    reads = []
    monkeypatch.setattr(cleanup_lookup, "_uidvalidity", lambda connection: "1")
    monkeypatch.setattr(cleanup_lookup, "indexed_identity_uids", lambda *args, **kwargs: None)

    def fetch(*args, **kwargs):
        reads.append(args[4])
        assert kwargs["raw_fields"]
        return [
            {**raw_row("1", wanted), "from": f"fresh-{len(reads)}"},
            {
                **raw_row("2"),
                "_raw_identity_fields": ["<other@example.invalid>", wanted],
                "identity_invalid": True,
                "from": f"fresh-{len(reads)}",
            },
        ]

    monkeypatch.setattr(cleanup_lookup, "fetch_rows", fetch)
    rows = cleanup_lookup.folder_inventory(
        client, peer, Deadline(), "INBOX", stage="membership", identifiers=[wanted]
    )
    # The complete raw audit already supplied this call's fresh candidate
    # fields, including the hidden conflicting identity on the second UID.
    assert reads == [["1", "2"]]
    assert all(row["from"] == "fresh-1" for row in rows)
    assert rows[1]["identity_invalid"]
    assert len(peer._rules_preview_state["inventories"]["INBOX"]["rows"]) == 2
