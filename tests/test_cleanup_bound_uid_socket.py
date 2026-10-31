"""Bound confirmation reads use exact UID sets through the real wire parser."""

import imaplib
import re
from dataclasses import replace

import pytest
from action_records import execution_log
from budget_assertions import SocketBudgetAudit
from test_cleanup_socket import mutations
from test_cleanup_socket import socket_mailbox as socket_mailbox
from test_mail import message
from test_preview_proton_shape import WorkClock

from proton_mail_mcp import mail
from proton_mail_mcp.guarded import _fingerprint
from proton_mail_mcp.mail import MailValidationError


def test_bound_verification_fetches_only_approved_uids_over_socketpair(socket_mailbox, monkeypatch):
    client, peer, log_path = socket_mailbox()
    preview = client.preview_action(
        "move_to_folder",
        query={"folder": "INBOX"},
        params={"folder": "Folders/Reviewed"},
    )
    assert preview["count"] == 2 and preview["token"]
    grant = client.cleanup.tokens.inspect(preview["token"], "move_to_folder")
    approved = {}
    for row in grant.messages:
        for membership in [row, *row["memberships"]]:
            approved.setdefault(membership["folder"], set()).add(membership["uid"])

    # This arrival must neither widen the approval nor require reading its headers.
    peer.folders["INBOX"]["9"] = message("<later@example.invalid>").headers
    original_uid = imaplib.IMAP4.uid
    fetch_calls = []

    def uid(connection, command, *args):
        if command.upper() == "FETCH":
            # Recent stdlib versions unpack FETCH into this exact argument pair.
            assert len(args) == 2
            fetch_calls.append(args)
        return original_uid(connection, command, *args)

    monkeypatch.setattr(imaplib.IMAP4, "uid", uid)
    command_offset = len(peer.commands)
    deadline = client._deadline(execute=True)
    with client._session(deadline=deadline) as (connection, _):
        rows, blocked = client.cleanup._verified_chunk(connection, deadline, grant, grant.messages)

    assert not blocked and _fingerprint(rows) == grant.fingerprint
    commands = peer.commands[command_offset:]
    assert fetch_calls
    assert not any(command.startswith(b"UID SEARCH") for _, command in commands)
    for folder, command in commands:
        if command.startswith(b"UID FETCH"):
            requested = set(command.split()[2].decode("ascii").split(","))
            assert requested and requested <= approved[folder]
            assert b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SENDER REPLY-TO)]" in command
    assert not mutations(commands) and not execution_log(log_path)
    assert "9" in peer.folders["INBOX"]
    client.cleanup.tokens.inspect(preview["token"], "move_to_folder")


@pytest.mark.parametrize("change", ["added", "balanced", "uidvalidity", "new_folder"])
def test_bound_verification_refuses_changed_label_membership_before_claim(socket_mailbox, change):
    client, peer, log_path = socket_mailbox()
    label = "Labels/Review"
    if change == "balanced":
        peer.folders[label]["500"] = message("<unrelated@example.invalid>").headers
    preview = client.preview_action(
        "move_to_folder",
        query={"folder": "INBOX"},
        params={"folder": "Folders/Reviewed"},
    )
    assert preview["count"] == 2 and preview["token"]
    if change == "new_folder":
        label = "Labels/Later"
        peer.folders[label] = {}
        peer.uidvalidities[label] = "700"
        peer.next_uid[label] = 501
        peer.deleted[label] = set()
    if change == "uidvalidity":
        peer.uidvalidities[label] = "701"
    else:
        peer.folders[label].clear()
        peer.folders[label]["501"] = peer.folders["INBOX"]["7"]
        peer.next_uid[label] = 502
    command_offset = len(peer.commands)
    with pytest.raises(MailValidationError):
        client.move_to_folder(preview["token"])
    assert not mutations(peer.commands[command_offset:]) and not execution_log(log_path)
    client.cleanup.tokens.inspect(preview["token"], "move_to_folder")


@pytest.mark.parametrize("change", ["identity", "duplicate_identity", "protected", "fingerprint"])
def test_bound_verification_rechecks_approved_label_copy(socket_mailbox, change):
    client, peer, log_path = socket_mailbox()
    original = peer.folders["INBOX"]["7"]
    peer.folders["Labels/Review"]["500"] = original
    preview = client.preview_action(
        "move_to_folder",
        query={"folder": "INBOX"},
        params={"folder": "Folders/Reviewed"},
    )
    assert preview["token"]
    if change == "identity":
        changed = original.replace(b"newsletter-1", b"newsletter-3")
    elif change == "duplicate_identity":
        changed = b"Message-ID: <another@example.invalid>\r\n" + original
    elif change == "protected":
        changed = original.replace(b"sender@example.com", b"sender@gmail.com")
    else:
        changed = original.replace(b"Oct", b"Nov")
    assert changed != original
    peer.folders["Labels/Review"]["500"] = changed
    with pytest.raises(MailValidationError):
        client.move_to_folder(preview["token"])
    assert not mutations(peer.commands) and not execution_log(log_path)
    client.cleanup.tokens.inspect(preview["token"], "move_to_folder")


def test_bound_verification_without_label_uidnext_keeps_inventory_guard(socket_mailbox):
    client, peer, log_path = socket_mailbox()

    def omit_uidnext(folder, operation, command, response):
        if folder == "Labels/Review":
            return re.sub(rb"\* OK \[UIDNEXT [0-9]+\][^\r\n]*\r\n", b"", response)
        return response

    peer.response_hook = omit_uidnext
    preview = client.preview_action(
        "move_to_folder",
        query={"folder": "INBOX"},
        params={"folder": "Folders/Reviewed"},
    )
    assert preview["token"]
    peer.folders["Labels/Review"]["501"] = peer.folders["INBOX"]["7"]
    peer.next_uid["Labels/Review"] = 502
    command_offset = len(peer.commands)
    with pytest.raises(MailValidationError):
        client.move_to_folder(preview["token"])
    commands = peer.commands[command_offset:]
    assert any(command.startswith(b"UID SEARCH") for _, command in commands)
    assert not mutations(commands) and not execution_log(log_path)
    client.cleanup.tokens.inspect(preview["token"], "move_to_folder")


@pytest.mark.parametrize("change", ["duplicate_identity", "fingerprint", "uidvalidity"])
def test_bound_resolver_itself_refuses_invalid_evidence(socket_mailbox, change):
    client, peer, _ = socket_mailbox()
    original = peer.folders["INBOX"]["7"]
    original = original.replace(
        b"\r\n\r\n", b"\r\nX-Pad-Data: <newsletter-1@updates.example.test>\r\n\r\n"
    )
    peer.folders["INBOX"]["7"] = peer.folders["All Mail"]["101"] = original
    preview = client.preview_action(
        "move_to_folder",
        query={"folder": "INBOX"},
        params={"folder": "Folders/Reviewed"},
    )
    assert preview["token"]
    grant = client.cleanup.tokens.inspect(preview["token"], "move_to_folder")
    if change == "duplicate_identity":
        changed = original.replace(b"X-Pad-Data:", b"Message-ID:")
        assert len(changed) == len(original)
        peer.folders["INBOX"]["7"] = changed
    elif change == "fingerprint":
        peer.folders["INBOX"]["7"] = original.replace(b"Oct", b"Nov")
    else:
        peer.uidvalidities["INBOX"] = "999"
    deadline = client._deadline(execute=True)
    with client._session(deadline=deadline) as (connection, _):
        folders = client._folders(connection)
        with pytest.raises(MailValidationError):
            client.cleanup._resolve_bound(
                connection, deadline, grant.messages, grant.action, grant.params, folders
            )
    assert not mutations(peer.commands)


def test_id_selector_retains_ambiguity_check_for_new_physical_copy(socket_mailbox):
    client, peer, log_path = socket_mailbox()
    preview = client.preview_action(
        "move_to_folder",
        ids=["<newsletter-1@updates.example.test>"],
        params={"folder": "Folders/Reviewed"},
    )
    assert preview["token"] and preview["count"] == 1
    peer.folders["Archive"]["501"] = peer.folders["INBOX"]["7"]
    peer.next_uid["Archive"] = 502
    with pytest.raises(MailValidationError, match="ambiguous"):
        client.move_to_folder(preview["token"])
    assert not mutations(peer.commands) and not execution_log(log_path)
    client.cleanup.tokens.inspect(preview["token"], "move_to_folder")


@pytest.mark.parametrize(
    "phase,elapsed,progress",
    [("verification", 80.01, True), ("setup", 80.01, False), ("verification", 93.1, False)],
)
def test_bound_fetch_and_setup_respect_actual_mutation_headroom(
    socket_mailbox, monkeypatch, phase, elapsed, progress
):
    clock = WorkClock()
    monkeypatch.setattr(mail, "time", clock)
    client, peer, log_path = socket_mailbox(clock=clock)
    client.settings = replace(client.settings, call_timeout=100, execute_timeout=100)
    preview = client.preview_action(
        "move_to_folder",
        query={"folder": "INBOX"},
        params={"folder": "Folders/Reviewed"},
    )
    assert preview["token"]
    delayed = []
    if progress:
        original_bound = client.cleanup._resolve_bound
        remaining_work = [elapsed]

        def cooperative_bound(connection, deadline, *args, **kwargs):
            # Preserve all 80.01 seconds of synthetic verification work across
            # attempts; the read-only operation yields at its actual I/O deadline.
            charged = min(remaining_work[0], max(0, deadline.io_end - clock.elapsed))
            remaining_work[0] -= charged
            delayed.append(charged)
            clock.elapsed += charged
            deadline.check(connection)
            return original_bound(connection, deadline, *args, **kwargs)

        monkeypatch.setattr(client.cleanup, "_resolve_bound", cooperative_bound)
    elif phase == "setup":
        validate_target = client.cleanup.backend.validate_target

        def slow_target(*args, **kwargs):
            target = validate_target(*args, **kwargs)
            if not delayed:
                delayed.append(True)
                clock.elapsed += elapsed
            return target

        monkeypatch.setattr(client.cleanup.backend, "validate_target", slow_target)
    else:

        def slow_fetch(folder, operation, command):
            if operation == "FETCH" and not delayed:
                delayed.append(True)
                clock.elapsed += elapsed

        peer.command_hook = slow_fetch
    if progress:
        refused = client.cleanup.execute("move_to_folder", preview["token"])
        assert refused["error_code"] == "first_chunk_unavailable"
        assert refused["done"] == refused["uncertain"] == 0 and refused["partial"]
        assert not refused["token_consumed"] and not refused["resume"]
        assert refused["continuation_token"] is None
        assert not mutations(peer.commands) and not execution_log(log_path)
        client.cleanup.tokens.inspect(preview["token"], "move_to_folder")
        # A caller may retry the preserved approval after the read-only delay;
        # zero progress itself must never create an automatic continuation.
        audit = SocketBudgetAudit(
            client, peer, clock, preview["token"], "move_to_folder", monkeypatch
        )
        results = audit.finish(preview["token"])
        assert sum(result["done"] for result in results) == 2 and not results[-1]["partial"]
        assert sum(delayed) == pytest.approx(elapsed) and remaining_work[0] == 0
        assert log_path.exists() and len(mutations(peer.commands)) == 1  # One batched MOVE.
        with pytest.raises(MailValidationError, match="already used"):
            client.move_to_folder(preview["token"])
    elif phase == "setup":
        refused = client.move_to_folder(preview["token"])
        assert refused["error_code"] == "first_chunk_unavailable"
        assert refused["done"] == refused["uncertain"] == 0
        assert not refused["token_consumed"] and not refused["resume"]
        assert not mutations(peer.commands) and not execution_log(log_path)
        client.cleanup.tokens.inspect(preview["token"], "move_to_folder")
    else:
        with pytest.raises(MailValidationError, match="execute headroom"):
            client.move_to_folder(preview["token"])
        assert not mutations(peer.commands) and not execution_log(log_path)
        client.cleanup.tokens.inspect(preview["token"], "move_to_folder")
    assert delayed
