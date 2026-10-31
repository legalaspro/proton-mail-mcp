"""Source snapshots tolerate only proven append-only growth over real IMAP sockets."""

import re

import pytest
from action_records import execution_log
from test_cleanup_socket import mutations
from test_cleanup_socket import socket_mailbox as socket_mailbox
from test_mail import message
from test_preview_proton_shape import WorkClock

from proton_mail_mcp import mail
from proton_mail_mcp.cleanup_lookup import folder_inventory
from proton_mail_mcp.mail import MailValidationError


def append_message(peer, *, same_sender=True):
    uid = str(peer.next_uid["INBOX"])
    peer.next_uid["INBOX"] += 1
    raw = message(
        f"<arrival-{uid}@example.invalid>",
        date="Mon, 19 Oct 2026 09:00:00 +0000",
    ).headers
    if not same_sender:
        raw = raw.replace(b"sender@example.com", b"other@example.invalid")
    peer.folders["INBOX"][uid] = raw
    peer.folders["All Mail"][uid] = raw
    return uid


@pytest.mark.parametrize("stage", ["SEARCH", "FETCH"])
@pytest.mark.parametrize("same_sender", [False, True])
@pytest.mark.parametrize("unsolicited", [False, True])
def test_source_append_during_slow_scan_preserves_approved_snapshot(
    socket_mailbox, monkeypatch, stage, same_sender, unsolicited
):
    clock = WorkClock()
    monkeypatch.setattr(mail, "time", clock)
    backend, peer, log_path = socket_mailbox(clock=clock, command_latency=0.002)
    arrived = []

    def arrival(folder, operation, command):
        if folder == "INBOX" and operation == stage and not arrived:
            arrived.append(append_message(peer, same_sender=same_sender))

    peer.command_hook = arrival
    if unsolicited:
        announced = []

        def announce(folder, operation, command, response):
            if folder == "INBOX" and operation == stage and arrived and not announced:
                announced.append(1)
                return (
                    f"* {len(peer.folders[folder])} EXISTS\r\n"
                    f"* OK [UIDNEXT {peer.next_uid[folder]}] appended\r\n"
                ).encode() + response
            return response

        peer.response_hook = announce
    preview = backend.preview_action(
        "move_to_trash",
        query={"folder": "INBOX", "from_": "sender@example.com", "before": "2026-10-18"},
    )
    assert arrived and preview["token"] and preview["count"] == 2
    record = backend.cleanup.tokens.inspect(preview["token"], "move_to_trash")
    assert {row["uid"] for row in record.messages} == {"7", "8"}
    assert not mutations(peer.commands) and not execution_log(log_path)
    assert not any(b"HEADER" in command for _, command in peer.commands if b"SEARCH" in command)
    assert any(
        re.search(rb"UID SEARCH .*UID 1:500(?: |$)", command) for _, command in peer.commands
    )


def test_source_second_visit_accepts_only_new_high_uids(socket_mailbox):
    backend, peer, _ = socket_mailbox()
    deadline = backend._deadline()
    with backend._session(deadline=deadline) as (connection, _):
        first = folder_inventory(backend, connection, deadline, "INBOX", criteria=[])
        append_message(peer)
        second = folder_inventory(backend, connection, deadline, "INBOX", criteria=[])
        assert {row["uid"] for row in second} == {row["uid"] for row in first} == {"7", "8"}


def test_source_later_membership_visit_proves_growth_and_rechecks_full_inventory(socket_mailbox):
    backend, peer, _ = socket_mailbox()
    deadline = backend._deadline()
    with backend._session(deadline=deadline) as (connection, _):
        first = folder_inventory(backend, connection, deadline, "INBOX", criteria=[])
        arrival = append_message(peer)
        second = folder_inventory(
            backend,
            connection,
            deadline,
            "INBOX",
            stage="membership",
            identifiers=[row["message_id"] for row in first],
        )
        assert {row["uid"] for row in second} == {"7", "8"}
    assert any(
        folder == "INBOX" and command.startswith(b"UID FETCH") and arrival.encode() in command
        for folder, command in peer.commands
    )


def test_source_move_out_retries_from_scratch(socket_mailbox):
    backend, peer, _ = socket_mailbox()
    removed = []

    def move_out(folder, operation, command):
        if folder == "INBOX" and operation == "FETCH" and not removed:
            removed.append(peer.folders[folder].pop("7"))

    peer.command_hook = move_out
    preview = backend.preview_action("move_to_trash", query={"folder": "INBOX"})
    assert preview["token"] and preview["count"] == 1
    record = backend.cleanup.tokens.inspect(preview["token"], "move_to_trash")
    assert {row["uid"] for row in record.messages} == {"8"}
    assert len([command for _, command in peer.commands if command == b'EXAMINE "INBOX"']) >= 3


def test_source_missing_low_uid_with_balanced_append_cannot_reuse_snapshot(socket_mailbox):
    backend, peer, _ = socket_mailbox()
    deadline = backend._deadline()
    with backend._session(deadline=deadline) as (connection, _):
        folder_inventory(backend, connection, deadline, "INBOX", criteria=[])
        peer.folders["INBOX"].pop("7")
        append_message(peer)
        append_message(peer)
        with pytest.raises(MailValidationError, match="Message set changed"):
            folder_inventory(backend, connection, deadline, "INBOX", stage="membership")


def test_source_continuously_changing_mailbox_stops_after_bounded_retries(socket_mailbox):
    backend, peer, log_path = socket_mailbox()
    attempts = []

    def move_out(folder, operation, command):
        if folder == "INBOX" and operation == "FETCH":
            attempts.append(1)
            peer.folders[folder].pop(next(iter(peer.folders[folder])))
            append_message(peer)

    peer.command_hook = move_out
    with pytest.raises(MailValidationError, match="mailbox is changing; retry"):
        backend.preview_action("move_to_trash", query={"folder": "INBOX"})
    assert len(attempts) == 3
    assert not mutations(peer.commands) and not execution_log(log_path)


@pytest.mark.parametrize("transient", [False, True])
@pytest.mark.parametrize("direct", [False, True])
def test_source_uidvalidity_change_never_retries_or_issues_token(socket_mailbox, transient, direct):
    backend, peer, log_path = socket_mailbox()
    changed = []

    def change(folder, operation, command, response):
        if folder == "INBOX" and operation == "FETCH" and not changed:
            changed.append(1)
            if not transient:
                peer.uidvalidities[folder] = "999"
            return b"* OK [UIDVALIDITY 999] changed\r\n" + response
        return response

    peer.response_hook = change
    with pytest.raises(MailValidationError, match="Message set changed"):
        if direct:
            deadline = backend._deadline()
            with backend._session(deadline=deadline) as (connection, _):
                folder_inventory(backend, connection, deadline, "INBOX", criteria=[])
        else:
            backend.preview_action("move_to_trash", query={"folder": "INBOX"})
    assert changed and not mutations(peer.commands) and not execution_log(log_path)
