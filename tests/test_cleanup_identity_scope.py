"""Only candidate identities can refuse cleanup; unrelated mail remains untouched."""

import json
from pathlib import Path

import pytest
from action_records import execution_records as replay_records
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations
from test_preview_proton_shape import SYNTHETIC_BATCH, assert_header_only, headers
from test_preview_proton_shape import shape_mailbox as shape_mailbox

from proton_mail_mcp.action_log import header_digest
from proton_mail_mcp.mail import MailValidationError

UNUSABLE_IDS = [
    "<20261017.4711@[192.0.2.10]> trailing",
    "<5f3c2a1b9e8d7c6b><second>",
    "<unrelated@example.invalid> (added by relay)",
    "<2026-10-17T09:00:00Z@example.invalid>(hidden)",
]
WANTED = SYNTHETIC_BATCH[:1]


@pytest.mark.parametrize("unrelated", UNUSABLE_IDS)
@pytest.mark.parametrize("folder", ["Spam", "All Mail"])
@pytest.mark.parametrize("selector", ["ids", "query"])
@pytest.mark.parametrize("action", ["move_to_trash", "remove_label"])
def test_unrelated_identity_does_not_block_preview_execute_or_preflight(
    shape_mailbox, unrelated, folder, selector, action
):
    client, peer, _ = shape_mailbox(WANTED, mailbox_size=30)
    peer.folders[folder]["200"] = headers(unrelated)
    source = "INBOX"
    params = None
    if action == "remove_label":
        source = "Labels/Review"
        params = {"label": "Review"}
        peer.folders[source] = {"1": headers(WANTED[0])}
    kwargs = {"ids": WANTED} if selector == "ids" else {"query": {"folder": source}}
    preview = client.preview_action(action, params=params, **kwargs)
    assert preview["token"] and preview["count"] == 1
    result = getattr(client, action)(preview["token"])
    assert result["count"] == 1 and result["partial"] is False
    assert peer.folders[folder]["200"] == headers(unrelated)
    assert assert_header_only(peer)


@pytest.mark.parametrize("unrelated", UNUSABLE_IDS)
@pytest.mark.parametrize("action", ["move_to_trash", "remove_label"])
def test_digest_only_undo_ignores_unrelated_identity(tmp_path, unrelated, action):
    sim = BridgeSim(copyuid=False)
    try:
        client = make_client(sim, tmp_path)
        wanted = sim.add(Msg(hdr(WANTED[0]), labels={"Labels/Review"}))
        params = {"label": "Review"} if action == "remove_label" else None
        preview = client.preview_action(action, ids=WANTED, params=params)
        assert preview["token"]
        completed = getattr(client, action)(preview["token"])
        assert completed["count"] == 1 and not completed["partial"]
        log = Path(client.settings.action_log_path)
        record = replay_records([json.loads(line) for line in log.read_text().splitlines()])[0]
        record["messages"][0]["uid_after"] = None
        record["messages"][0]["uidvalidity_after"] = None
        log.write_text(json.dumps(record) + "\n")
        lookup_folder = "Trash" if action == "move_to_trash" else "Spam"
        unrelated_row = sim.add(Msg(hdr(unrelated), folder=lookup_folder))
        restored = client.undo_action(completed["log_ids"][0])
        assert restored["count"] == 1 and not restored["partial"]
        assert wanted.folder == "INBOX"
        assert "Labels/Review" in wanted.labels
        assert unrelated_row.folder == lookup_folder
        assert assert_header_only(sim)
        assert sim.violations == []
    finally:
        sim.close()


def test_undo_lookup_refuses_single_unusable_identity_even_with_matching_digest(shape_mailbox):
    client, peer, _ = shape_mailbox(WANTED, mailbox_size=30)
    peer.folders["Trash"]["1"] = headers(WANTED[0] + " (added by relay)")
    backend = client.cleanup.backend
    deadline = client._deadline()
    with client._session(deadline=deadline) as (connection, _):
        matches = backend._locate(connection, deadline, "Trash", WANTED[0])
        assert len(matches) == 1 and matches[0]["identity_invalid"] is True
        previous = {
            "message_id": WANTED[0],
            "header_digest": header_digest(matches[0]),
            "size": matches[0]["size"],
            "internaldate": matches[0]["internaldate"],
        }
        row, reason = backend._undo_lookup(connection, deadline, "Trash", previous)
    assert row is None
    assert reason == 'ambiguous identity during undo in "Trash"; manual handling required'
    assert WANTED[0] not in reason
    assert not peer.mutations
    assert assert_header_only(peer)


@pytest.mark.parametrize("identity", [None, *UNUSABLE_IDS])
def test_unusable_query_source_candidate_remains_blocked(shape_mailbox, identity):
    client, peer, _ = shape_mailbox(WANTED, mailbox_size=30)
    peer.folders["INBOX"]["1"] = hdr(identity)
    preview = client.preview_action("move_to_trash", query={"folder": "INBOX"})
    assert preview["token"] is None
    assert preview["excluded_count"] == 1
    assert preview["excluded_reasons"] == {"invalid_identity": 1}
    assert identity is None or identity not in preview.get("reason", "")
    assert not peer.mutations


@pytest.mark.parametrize("folder,stage", [("INBOX", "source"), ("All Mail", "membership")])
def test_matching_invalid_identity_blocks_with_stage_and_folder(shape_mailbox, folder, stage):
    client, peer, _ = shape_mailbox(WANTED, mailbox_size=30)
    uid = "1" if folder == "INBOX" else "30"
    peer.folders[folder][uid] = headers(WANTED[0] + " (added by relay)")
    preview = client.preview_action("move_to_trash", ids=WANTED)
    assert preview["token"] is None
    assert preview["excluded_ids"] == WANTED
    assert preview["excluded_reasons"] == {"invalid_identity": 1}
    assert WANTED[0] not in preview.get("reason", "")
    assert not peer.mutations


def test_execute_invalid_source_refusal_names_stage_and_folder(shape_mailbox):
    client, peer, _ = shape_mailbox(WANTED, mailbox_size=30)
    preview = client.preview_action("move_to_trash", ids=WANTED)
    assert preview["token"]
    peer.folders["INBOX"]["1"] = headers(WANTED[0] + " (added by relay)")
    with pytest.raises(MailValidationError, match='execute in "INBOX"') as error:
        client.move_to_trash(preview["token"])
    assert WANTED[0] not in str(error.value)
    assert not peer.mutations


def test_undo_invalid_logged_identity_refusal_names_stage_and_folder(tmp_path):
    sim = BridgeSim()
    try:
        client = make_client(sim, tmp_path)
        wanted = sim.add(Msg(hdr(WANTED[0])))
        preview = client.preview_action("move_to_trash", ids=WANTED)
        completed = client.move_to_trash(preview["token"])
        wanted.headers = hdr(WANTED[0] + " (added by relay)")
        before = mutations(sim)
        with pytest.raises(MailValidationError, match='undo in "Trash"') as error:
            client.undo_action(completed["log_ids"][0])
        assert WANTED[0] not in str(error.value)
        assert mutations(sim) == before
    finally:
        sim.close()


def test_preflight_invalid_recovery_identity_names_stage_and_folder(shape_mailbox, monkeypatch):
    client, peer, _ = shape_mailbox(WANTED, mailbox_size=30)
    peer.folders["Labels/Review"] = {"1": headers(WANTED[0])}
    preview = client.preview_action("remove_label", ids=WANTED, params={"label": "Review"})
    assert preview["token"]
    backend = client.cleanup.backend
    original_preflight = backend._preflight

    def changed_recovery(connection, deadline, *args):
        peer.folders["All Mail"]["30"] = headers(WANTED[0] + " (added by relay)")
        connection._cleanup_inventory = {}
        return original_preflight(connection, deadline, *args)

    monkeypatch.setattr(backend, "_preflight", changed_recovery)
    with pytest.raises(MailValidationError, match='preflight in "All Mail"') as error:
        client.remove_label(preview["token"])
    assert WANTED[0] not in str(error.value)
    assert not peer.mutations


@pytest.mark.parametrize("stage", ["source", "membership", "preflight", "undo"])
def test_multi_identity_refusal_reports_stage_and_bounded_folder(stage):
    from proton_mail_mcp.cleanup_lookup import matching_rows

    identifier = WANTED[0]
    other = "<other@example.invalid>"
    folder = "Folders/" + "x" * 80 + "\n"
    row = {"message_id": identifier + " " + other, "folder": folder, "identity_invalid": True}
    with pytest.raises(MailValidationError) as error:
        matching_rows([row], [identifier, other], stage=stage, folder=folder)
    reason = str(error.value)
    assert stage in reason and "Folders/" in reason
    assert identifier not in reason and other not in reason
    assert "\n" not in reason and len(reason) < 150


@pytest.mark.parametrize("stage", ["source", "membership", "preflight", "undo"])
def test_incomplete_inventory_refusal_names_stage_and_folder(stage):
    from types import SimpleNamespace

    from proton_mail_mcp.cleanup_lookup import fetch_rows

    client = SimpleNamespace(_metadata=lambda *args, **kwargs: {})
    with pytest.raises(MailValidationError, match=stage + ' in "All Mail"'):
        fetch_rows(client, object(), object(), "All Mail", ["1"], "100", stage=stage)


@pytest.mark.parametrize("copies", [2, 201])
def test_digest_only_undo_collision_refusal_names_stage_and_folder(tmp_path, copies):
    sim = BridgeSim(copyuid=False)
    try:
        client = make_client(sim, tmp_path)
        moved = sim.add(Msg(hdr(WANTED[0])))
        preview = client.preview_action("move_to_trash", ids=WANTED)
        # Its copy hidden from verification, the move stays uncertain without a
        # destination UID, so undo falls back to the logged digest identity.
        sim.fetch_omit.add(("Trash", moved.pid))
        completed = client.move_to_trash(preview["token"])
        sim.fetch_omit.clear()
        assert completed["uncertain"] == 1 and completed["messages"][0]["uid_after"] is None
        for _ in range(copies - 1):
            sim.add(Msg(hdr(WANTED[0]), folder="Trash"))
        before = mutations(sim)
        if copies > 200:
            with pytest.raises(MailValidationError, match='safety cap during undo in "Trash"'):
                client.undo_action(completed["log_ids"][0])
        else:
            result = client.undo_action(completed["log_ids"][0])
            assert result["count"] == 0 and result["partial"]
            assert result["missing"][0]["reason"] == (
                'ambiguous virtual view during undo in "All Mail"; manual handling required'
            )
        assert mutations(sim) == before
    finally:
        sim.close()


def test_execute_fingerprint_refusal_names_source_folder(shape_mailbox):
    client, peer, _ = shape_mailbox(WANTED, mailbox_size=30)
    preview = client.preview_action("move_to_trash", ids=WANTED)
    peer.folders["INBOX"]["1"] = headers(WANTED[0]).replace(
        b"Synthetic preview fixture", b"Changed synthetic subject"
    )
    with pytest.raises(MailValidationError, match='execute in "INBOX"') as error:
        client.move_to_trash(preview["token"])
    assert "synthetic subject" not in str(error.value) and WANTED[0] not in str(error.value)
    assert not peer.mutations


def test_disappearing_source_membership_refusal_names_folder(shape_mailbox, monkeypatch):
    client, peer, _ = shape_mailbox(WANTED, mailbox_size=30)
    service = client.cleanup
    original = service._folder_rows

    def without_source(connection, deadline, folder, identifiers, *, stage, **kwargs):
        rows = original(connection, deadline, folder, identifiers, stage=stage, **kwargs)
        return [] if stage == "membership" and folder == "INBOX" else rows

    monkeypatch.setattr(service, "_folder_rows", without_source)
    result = client.preview_action("move_to_trash", ids=WANTED)
    assert result["token"] is None and result["excluded_ids"] == WANTED
    assert result["excluded_reasons"] == {"source_membership_changed": 1}
    assert not peer.mutations


def test_undo_late_protection_uses_undo_stage_without_content(tmp_path, monkeypatch):
    sim = BridgeSim()
    try:
        client = make_client(sim, tmp_path)
        message = sim.add(Msg(hdr(WANTED[0])))
        preview = client.preview_action("move_to_trash", ids=WANTED)
        completed = client.move_to_trash(preview["token"])
        backend = client.cleanup.backend
        original = backend._check_protected
        stages = []

        def check(rows, *, stage="preflight"):
            if any(backend.policy.check(row) for row in rows):
                stages.append(stage)
            return original(rows, stage=stage)

        def protect(sid, selected, operation, command):
            if operation == "SELECT" and command == 'SELECT "Trash"':
                message.headers = hdr(WANTED[0], from_="security@bank.example.com")

        monkeypatch.setattr(backend, "_check_protected", check)
        sim.hooks.append(protect)
        before = mutations(sim)
        result = client.undo_action(completed["log_ids"][0])
        assert result["count"] == 0 and result["partial"]
        assert stages == ["undo"]
        assert 'undo verification in "Trash"' in result["error"]
        assert "bank.example.com" not in result["error"] and WANTED[0] not in result["error"]
        assert mutations(sim) == before
    finally:
        sim.close()
