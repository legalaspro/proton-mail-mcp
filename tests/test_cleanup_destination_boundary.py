"""Destination recovery uses bounded append evidence through the real wire parser."""

import re

import pytest
from action_records import execution_log
from action_records import execution_records as replay_records
from test_cleanup_socket import mutations
from test_cleanup_socket import socket_mailbox as socket_mailbox
from test_mail import message
from test_preview_proton_shape import WorkClock

from proton_mail_mcp import cleanup_imap, mail
from proton_mail_mcp.mail import MailError, MailValidationError

TARGET = "Folders/Reviewed"


def approve(client):
    preview = client.preview_action(
        "move_to_folder", query={"folder": "INBOX"}, params={"folder": TARGET}
    )
    assert preview["count"] == 2 and preview["token"] and not preview["blocked"]
    return preview["token"]


@pytest.mark.parametrize("mapping", ["missing", "preexisting", "validity", "earlier_move"])
def test_destination_boundary_rejects_stale_mapping_and_pins_only_new_uids(
    socket_mailbox, monkeypatch, mapping
):
    client, peer, _ = socket_mailbox()
    token = approve(client)
    originals = dict(peer.folders["INBOX"])
    # An arrival after preview is not part of its bound memberships. The
    # destination append boundary must still exclude it from undo authority.
    peer.folders[TARGET]["501"] = originals["7"]
    peer.next_uid[TARGET] = 502
    copyuid = cleanup_imap._copyuid_map
    first = []

    def altered(connection, response, uids):
        supplied = copyuid(connection, response, uids)
        result = {}
        for uid in uids:
            validity, actual = supplied[uid]
            first.append(actual)
            if mapping == "preexisting":
                result[uid] = (validity, "501")
            elif mapping == "validity":
                result[uid] = ("999", actual)
            elif mapping == "earlier_move":
                result[uid] = (validity, first[0])
        return result

    start = len(peer.commands)
    with monkeypatch.context() as patch:
        patch.setattr(cleanup_imap, "_copyuid_map", altered)
        result = client.move_to_folder(token)
    commands = peer.commands[start:]
    assert result["count"] == 2 and not result["partial"]
    assert [entry["uid_after"] for entry in result["messages"]] == ["502", "503"]
    assert all(entry["destination_uidnext"] == "502" for entry in result["messages"])
    assert not any(command.startswith(b"UID SEARCH") for _, command in commands)
    destination_fetches = [
        command
        for folder, command in commands
        if folder == TARGET and command.startswith(b"UID FETCH")
    ]
    assert destination_fetches
    assert all(
        set(command.split()[2].decode().split(",")) <= {"502", "503"}
        for command in destination_fetches
    )
    # One batched MOVE names both approved UIDs.
    assert [command.split()[2] for _, command in mutations(commands)] == [b"7:8"]
    restored = client.undo_action(result["log_ids"][0])
    assert restored["count"] == 2 and not restored["partial"]
    assert peer.folders[TARGET] == {"501": originals["7"]}
    assert sorted(peer.folders["INBOX"].values()) == sorted(originals.values())


def test_destination_without_uidnext_retains_recovery_inventory(socket_mailbox, monkeypatch):
    clock = WorkClock()
    monkeypatch.setattr(mail, "time", clock)
    client, peer, _ = socket_mailbox(clock=clock)
    peer.folders[TARGET]["500"] = message("<other@example.invalid>").headers

    def inventory_cost(folder, operation, command):
        if folder == TARGET and operation == "FETCH":
            clock.elapsed += 0.25

    def omit_uidnext(folder, operation, command, response):
        if folder == TARGET:
            return re.sub(rb"\* OK \[UIDNEXT [0-9]+\][^\r\n]*\r\n", b"", response)
        return response

    peer.response_hook = omit_uidnext
    peer.command_hook = inventory_cost
    token = approve(client)
    grant = client.cleanup.tokens.inspect(token, "move_to_folder")
    assert all("label_states" in row for row in grant.messages)
    assert grant.bounded_recovery is False
    assert grant.inventory_seconds >= 0.25
    assert grant.verification_seconds == grant.inventory_seconds / len(grant.messages)
    start = len(peer.commands)
    result = client.move_to_folder(token)
    commands = peer.commands[start:]
    assert result["count"] == 2 and not result["partial"]
    assert all("destination_uidnext" not in entry for entry in result["messages"])
    assert any(
        folder == TARGET and command.startswith(b"UID SEARCH") for folder, command in commands
    )
    assert "500" in peer.folders[TARGET]


def test_destination_uidnext_disappearing_after_preview_refuses_before_claim(
    socket_mailbox, monkeypatch
):
    client, peer, log_path = socket_mailbox()
    token = approve(client)
    grant = client.cleanup.tokens.inspect(token, "move_to_folder")
    assert grant.bounded_recovery is True
    assert grant.inventory_seconds == grant.verification_seconds == 0
    approved = dict(peer.folders["INBOX"])

    def omit_uidnext(folder, operation, command, response):
        if folder == TARGET:
            return re.sub(rb"\* OK \[UIDNEXT [0-9]+\][^\r\n]*\r\n", b"", response)
        return response

    peer.response_hook = omit_uidnext
    monkeypatch.setattr(
        client.cleanup.tokens,
        "claim",
        lambda *args, **kwargs: pytest.fail("Missing recovery evidence consumed approval"),
    )
    start = len(peer.commands)
    with pytest.raises(MailValidationError, match="Destination recovery evidence changed"):
        client.move_to_folder(token)
    commands = peer.commands[start:]
    assert not mutations(commands)
    assert not execution_log(log_path)
    assert peer.folders["INBOX"] == approved and not peer.folders[TARGET]
    assert not any(
        folder == TARGET and command.startswith(b"UID SEARCH") for folder, command in commands
    )
    assert client.cleanup.tokens.inspect(token, "move_to_folder").bounded_recovery is True


def test_destination_recovery_refuses_unbounded_append_interval(socket_mailbox, monkeypatch):
    client, peer, _ = socket_mailbox()
    token = approve(client)
    copyuid = cleanup_imap._copyuid_map

    def missing(connection, response, uids):
        copyuid(connection, response, uids)
        peer.next_uid[TARGET] += 500
        return {}

    monkeypatch.setattr(cleanup_imap, "_copyuid_map", missing)
    start = len(peer.commands)
    result = client.move_to_folder(token)
    commands = peer.commands[start:]
    # Both left INBOX, but without a bounded read no copy is confirmed in the
    # destination: they stay uncertain instead of counting as done.
    assert result["count"] == 0 and result["uncertain"] == 2 and result["partial"]
    assert all(entry["status"] == "uncertain" for entry in result["messages"])
    assert all(entry["uid_after"] is None for entry in result["messages"])
    assert all("left its source folder" in row["reason"] for row in result["mismatched"])
    assert not any(command.startswith(b"UID SEARCH") for _, command in commands)
    assert not any(
        folder == TARGET and command.startswith(b"UID FETCH") for folder, command in commands
    )
    assert len(mutations(commands)) == 1


def test_destination_append_boundary_is_immutable_in_execution_progress():
    original = {
        "action": "move_to_folder",
        "log_id": "synthetic-boundary-record",
        "status": "in_progress",
        "messages": [{"destination_uidnext": "502", "status": "not_attempted"}],
    }
    changed = {
        "action": "execute_progress",
        "action_log_id": original["log_id"],
        "index": 0,
        "message": {"destination_uidnext": "501", "status": "done"},
    }
    with pytest.raises(MailError, match="invalid execution identity"):
        replay_records([original, changed])
