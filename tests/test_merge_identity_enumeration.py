"""UID enumeration retains raw identity and certified inventory checks."""

import bridge_sim
import pytest
from bridge_sim import Msg, hdr, mutations
from test_cleanup_raw_identity_hardening import BAD_FIELDS, WANTED, raw_fixture
from test_cleanup_raw_predicate import raw_mailbox as raw_mailbox
from test_identity_index import inventory as inventory
from test_identity_index_integration import (
    assert_no_mutations_or_header_search,
    assert_refuses,
    index_files,
    indexed_preview,
)
from test_identity_index_integration import indexed_mailbox as indexed_mailbox

from proton_mail_mcp.cleanup_lookup import folder_inventory
from proton_mail_mcp.mail import MailValidationError


def reject_uid_range_search(sid, folder, operation, command):
    if operation == "SEARCH" and "1:*" in command:
        pytest.fail("Identity discovery must not search a possibly nonexistent UID")


def assert_all_enumeration(sim):
    assert any(command == "UID SEARCH CHARSET UTF-8 ALL" for *_, command in sim.commands)
    assert not any(
        command.startswith("UID SEARCH") and "1:*" in command for *_, command in sim.commands
    )
    assert_no_mutations_or_header_search(sim)


@pytest.mark.parametrize("raw_kind", BAD_FIELDS)
def test_enumeration_keeps_exact_raw_identity_guard(raw_mailbox, monkeypatch, raw_kind):
    sim, client = raw_mailbox
    original, changed = raw_fixture(raw_kind)
    sim.add(Msg(changed))
    sim.hooks.append(reject_uid_range_search)
    envelope = bridge_sim.envelope_from_headers(original)
    monkeypatch.setattr(bridge_sim, "envelope_from_headers", lambda raw: envelope)
    with client._session() as (connection, _):
        rows = folder_inventory(
            client, connection, client._deadline(), "INBOX", identifiers=[WANTED]
        )
        assert len(rows) == 1 and rows[0]["identity_invalid"]
    assert_all_enumeration(sim)
    assert mutations(sim) == []


def test_enumeration_rechecks_inventory_after_valid_raw_fetch(raw_mailbox, monkeypatch):
    sim, client = raw_mailbox
    sim.add(Msg(hdr(WANTED)))
    sim.hooks.append(reject_uid_range_search)
    metadata = client._metadata
    changed = []

    def fetch_then_change(*args, **kwargs):
        rows = metadata(*args, **kwargs)
        if kwargs.get("identity_headers_only"):
            sim.add(Msg(hdr(WANTED)))
            changed.append(True)
        return rows

    monkeypatch.setattr(client, "_metadata", fetch_then_change)
    with client._session() as (connection, _):
        client._select(connection, "INBOX")
        with pytest.raises(MailValidationError, match="Message set changed"):
            folder_inventory(client, connection, client._deadline(), "INBOX", identifiers=[WANTED])
    assert changed == [True]
    assert_all_enumeration(sim)


@pytest.mark.parametrize("rules", [False, True])
def test_certified_cold_and_warm_enumeration_use_all(indexed_mailbox, rules):
    sim, client, service, _, _ = indexed_mailbox(count=1, filler=2, latency=0)
    sim.hooks.append(reject_uid_range_search)
    for _ in range(2):
        sim.commands.clear()
        result = indexed_preview(client, service, rules=rules)
        assert result["token"] and result["count"] == 1 and not result["partial"]
        assert any(command.startswith("STATUS ") for *_, command in sim.commands)
        assert_all_enumeration(sim)
        empty = {folder for folder, mapping in sim.uidmap.items() if not mapping}
        assert not any(
            folder in empty and command.startswith(("UID SEARCH", "UID FETCH"))
            for _, folder, _, command in sim.commands
        )


@pytest.mark.parametrize("rules", [False, True])
def test_certified_all_enumeration_keeps_hidden_raw_delta_guard(indexed_mailbox, rules):
    sim, client, service, _, selected = indexed_mailbox(count=1, filler=2, latency=0)
    sim.hooks.append(reject_uid_range_search)
    assert indexed_preview(client, service, rules=rules)["token"]
    filler = next(item for item in sim.messages.values() if item.folder == "Archive")
    filler.headers += f"Message-ID: {selected[0].message_id()}\r\n".encode()
    sim.commands.clear()
    assert_refuses(client, service, rules=rules)
    assert any("CHANGEDSINCE" in command for *_, command in sim.commands)
    assert_all_enumeration(sim)


def interrupt_completed_archive_batch(client, clock, monkeypatch):
    """Expire one call after a complete raw batch, preserving resumable work."""
    metadata = client._metadata
    batches = []
    interrupted = False

    def fetch_then_expire(connection, uids, **kwargs):
        nonlocal interrupted
        rows = metadata(connection, uids, **kwargs)
        if (
            kwargs.get("identity_headers_only")
            and connection._selected_inventory["folder"] == "Archive"
        ):
            batches.append(list(uids))
            if not interrupted and len(uids) == 200:
                interrupted = True
                clock.elapsed += 21
        return rows

    monkeypatch.setattr(client, "_metadata", fetch_then_expire)
    return batches


def test_resumed_certified_rules_inventory_rechecks_completed_batches(indexed_mailbox, monkeypatch):
    sim, client, service, clock, _ = indexed_mailbox(
        count=1, filler=401, latency=0, metadata_latency=0
    )
    sim.hooks.append(reject_uid_range_search)
    batches = interrupt_completed_archive_batch(client, clock, monkeypatch)
    partial = service.preview_rules(limit=1)
    assert partial["partial"] and not partial["token"] and partial["next_cursor"]
    assert not partial["total_is_exact"]
    assert len(batches) == 1 and len(batches[0]) == 200
    completed = set(batches[0])
    batches.clear()
    sim.commands.clear()

    result = service.preview_rules(limit=1, cursor=partial["next_cursor"])
    assert result["token"] and result["count"] == 1 and not result["partial"]
    assert any(command.startswith('STATUS "Archive" ') for *_, command in sim.commands)
    assert all(completed.isdisjoint(batch) for batch in batches)
    assert not index_files(client)
    assert_all_enumeration(sim)


def test_resumed_certified_rules_inventory_detects_hidden_changed_raw_identity(
    indexed_mailbox, monkeypatch
):
    sim, client, service, clock, selected = indexed_mailbox(
        count=1, filler=401, latency=0, metadata_latency=0
    )
    sim.hooks.append(reject_uid_range_search)
    batches = interrupt_completed_archive_batch(client, clock, monkeypatch)
    partial = service.preview_rules(limit=1)
    assert partial["partial"] and not partial["token"] and partial["next_cursor"]
    assert len(batches) == 1 and len(batches[0]) == 200
    changed_uid = batches[0][0]
    filler = sim.messages[sim.uidmap["Archive"][int(changed_uid)]]
    filler.headers += f"Message-ID: {selected[0].message_id()}\r\n".encode()
    batches.clear()
    sim.commands.clear()

    try:
        result = service.preview_rules(limit=1, cursor=partial["next_cursor"])
    except MailValidationError:
        pass
    else:
        assert result["token"] is None
        assert result["blocked"] or result["partial"] or result.get("reason")
    assert any(command.startswith('STATUS "Archive" ') for *_, command in sim.commands)
    assert any(
        folder == "Archive" and "CHANGEDSINCE" in command for _, folder, _, command in sim.commands
    )
    assert any(changed_uid in batch for batch in batches)
    assert not index_files(client)
    assert_all_enumeration(sim)


@pytest.mark.parametrize("uid", ["0", "01", "4294967296", "99999999999", "\u0661", "-1"])
def test_certified_enumeration_rejects_noncanonical_uids(inventory, uid):
    peer = inventory.connection
    peer.rows = {uid: {**peer.rows["1"], "uid": uid}}
    peer.next_uid = 100000000000
    assert inventory.run() is None
    assert peer.fetches == []
    assert not list(inventory.path.glob("*.json"))


@pytest.mark.parametrize("raw_identity", [False, True])
def test_empty_selection_never_starts_a_later_certified_scan(
    indexed_mailbox, monkeypatch, raw_identity
):
    sim, client, _, _, _ = indexed_mailbox(count=0, filler=0, latency=0)
    select = client._select
    arrivals = []

    def select_then_arrive(connection, folder):
        state = select(connection, folder)
        if folder == "INBOX" and not arrivals:
            assert state["exists"] == 0
            sim.add(Msg(hdr(WANTED)))
            arrivals.append(True)
        return state

    monkeypatch.setattr(client, "_select", select_then_arrive)
    with client._session() as (connection, _):
        connection._cleanup_raw_identity = raw_identity
        empty_rows = folder_inventory(
            client, connection, client._deadline(), "INBOX", identifiers=[WANTED]
        )
    assert empty_rows == []
    assert arrivals == [True]
    assert not any(
        command.startswith(("STATUS ", "UID SEARCH", "UID FETCH")) for *_, command in sim.commands
    )
    # A new selection can discover the arrival, with normal certification and
    # fresh raw identity checks; the old empty snapshot authorizes no row.
    with client._session() as (connection, _):
        connection._cleanup_raw_identity = raw_identity
        rows = folder_inventory(
            client, connection, client._deadline(), "INBOX", identifiers=[WANTED]
        )
    assert len(rows) == 1 and rows[0]["message_id"] == WANTED
    assert not rows[0].get("identity_invalid")
    assert_all_enumeration(sim)
