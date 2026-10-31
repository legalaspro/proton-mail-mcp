"""MOVE admission uses authenticated capabilities parsed by real imaplib offline."""

import copy
from dataclasses import replace

import pytest
from action_records import execution_log
from test_cleanup_mutations import uid_values
from test_cleanup_socket import mutations
from test_cleanup_socket import socket_mailbox as socket_mailbox
from test_mail import message

from proton_mail_mcp.mail import MailValidationError

PRE_AUTH = ("IMAP4rev1", "STARTTLS", "UIDPLUS")


def approve(backend, peer):
    before = copy.deepcopy(peer.folders)
    preview = backend.preview_action("move_to_trash", query={"folder": "INBOX"})
    assert preview["count"] == 2 and preview["blocked"] == [] and preview["token"]
    assert peer.folders == before and not mutations(peer.commands)
    return preview["token"]


def assert_only_uid_moves(peer, count=2):
    commands = mutations(peer.commands)
    # One batched UID MOVE names every approved UID of the folder.
    assert sum(len(uid_values(command.split()[2])) for _, command in commands) == count
    assert all(
        folder == "INBOX" and command.startswith(b"UID MOVE ") for folder, command in commands
    )
    assert all(command.endswith(b' "Trash"') for _, command in commands)
    assert not any(b"COPY" in command or b"EXPUNGE" in command for _, command in peer.commands)


@pytest.mark.parametrize("move_atom", ["MOVE", "move"])
def test_post_login_capability_refresh_allows_uid_move(socket_mailbox, move_atom):
    backend, peer, _ = socket_mailbox(
        pre_auth_capabilities=PRE_AUTH,
        capabilities=(*PRE_AUTH, move_atom),
    )
    original = peer.identifiers("INBOX")
    token = approve(backend, peer)
    completed = backend.move_to_trash(token)
    assert completed["count"] == completed["done"] == 2
    assert completed["partial"] is False and completed["remaining"] == 0
    assert peer.identifiers("Trash") == original and not peer.folders["INBOX"]
    assert_only_uid_moves(peer)
    commands = [command for _, command in peer.commands]
    logins = [index for index, command in enumerate(commands) if command == b"LOGIN"]
    assert logins
    assert all(commands[index + 1] == b"CAPABILITY" for index in logins)
    assert "UID MOVE" not in peer.capabilities


@pytest.mark.parametrize("response", ["tagged", "untagged"])
@pytest.mark.parametrize("move_atom", ["MOVE", "move"])
def test_login_capability_response_allows_uid_move_without_extra_command(
    socket_mailbox, response, move_atom
):
    backend, peer, _ = socket_mailbox(
        pre_auth_capabilities=PRE_AUTH,
        capabilities=(*PRE_AUTH, move_atom),
        login_capability_response=response,
    )
    original = peer.identifiers("INBOX")
    token = approve(backend, peer)
    completed = backend.move_to_trash(token)
    assert completed["count"] == 2 and completed["partial"] is False
    assert peer.identifiers("Trash") == original and not peer.folders["INBOX"]
    assert_only_uid_moves(peer)
    commands = [command for _, command in peer.commands]
    # Every connection asks before LOGIN, but the authenticated response is
    # enough: no extra CAPABILITY command is needed after LOGIN.
    assert commands.count(b"CAPABILITY") == commands.count(b"LOGIN")
    assert all(
        commands[index - 1] != b"LOGIN"
        for index, command in enumerate(commands)
        if command == b"CAPABILITY" and index
    )


@pytest.mark.parametrize("response", [None, "tagged", "untagged"])
@pytest.mark.parametrize("pre_auth_move", [False, True])
def test_missing_authenticated_move_refuses_without_any_mutation(
    socket_mailbox, response, pre_auth_move
):
    backend, peer, log_path = socket_mailbox(
        pre_auth_capabilities=(*PRE_AUTH, "MOVE") if pre_auth_move else PRE_AUTH,
        capabilities=PRE_AUTH,
        login_capability_response=response,
    )
    before = copy.deepcopy(peer.folders)
    token = approve(backend, peer)
    with pytest.raises(MailValidationError, match="lacks MOVE capability") as refused:
        backend.move_to_trash(token)
    assert "UID MOVE" not in str(refused.value)
    assert peer.folders == before and peer.mutations == 0
    assert mutations(peer.commands) == []
    assert not execution_log(log_path)
    backend.cleanup.tokens.inspect(token, "move_to_trash")


@pytest.mark.parametrize("misleading_atom", ["XMOVE", "MOVE=YES", "UIDMOVE"])
def test_move_requires_exact_capability_atom(socket_mailbox, misleading_atom):
    backend, peer, log_path = socket_mailbox(
        pre_auth_capabilities=PRE_AUTH,
        capabilities=(*PRE_AUTH, misleading_atom),
    )
    before = copy.deepcopy(peer.folders)
    token = approve(backend, peer)
    with pytest.raises(MailValidationError, match="lacks MOVE capability"):
        backend.move_to_trash(token)
    assert peer.folders == before and peer.mutations == 0
    assert mutations(peer.commands) == []
    assert not execution_log(log_path)


@pytest.mark.parametrize("response", [None, "tagged"])
@pytest.mark.parametrize("move_disappears", [False, True])
def test_undo_checks_fresh_authenticated_move(socket_mailbox, response, move_disappears):
    backend, peer, log_path = socket_mailbox(
        pre_auth_capabilities=PRE_AUTH,
        capabilities=(*PRE_AUTH, "MOVE"),
        login_capability_response=response,
    )
    original = {folder: peer.identifiers(folder) for folder in peer.folders}
    token = approve(backend, peer)
    completed = backend.move_to_trash(token)
    assert completed["count"] == 2 and completed["partial"] is False
    assert_only_uid_moves(peer)
    if move_disappears:
        before = copy.deepcopy(peer.folders)
        before_log = log_path.read_bytes()
        peer.capabilities = PRE_AUTH
        with pytest.raises(MailValidationError, match="Undo requires MOVE capability"):
            backend.undo_action(completed["log_ids"][0])
        assert peer.folders == before and peer.mutations == 1
        assert log_path.read_bytes() == before_log
        assert_only_uid_moves(peer)
    else:
        restored = backend.undo_action(completed["log_ids"][0])
        assert restored["count"] == 2 and restored["partial"] is False
        assert {folder: peer.identifiers(folder) for folder in peer.folders} == original
        commands = mutations(peer.commands)
        # One batched forward MOVE; undo restores each message by its own handle.
        assert len(commands) == peer.mutations == 3
        assert all(command.startswith(b"UID MOVE ") for _, command in commands)
        assert all(
            folder == "Trash" and command.endswith(b' "INBOX"') for folder, command in commands[1:]
        )


def test_post_login_move_capability_allows_25_explicit_ids(socket_mailbox):
    backend, peer, _ = socket_mailbox(
        pre_auth_capabilities=PRE_AUTH,
        capabilities=(*PRE_AUTH, "MOVE"),
    )
    backend.settings = replace(backend.settings, call_timeout=120, execute_timeout=120)
    identifiers = [f"<capability-{index}@updates.example.test>" for index in range(1, 26)]
    peer.folders["INBOX"] = {
        str(index): message(identifier).headers for index, identifier in enumerate(identifiers, 1)
    }
    peer.folders["All Mail"] = {
        str(index + 100): raw for index, raw in enumerate(peer.folders["INBOX"].values(), 1)
    }
    before = copy.deepcopy(peer.folders)
    preview = backend.preview_action("move_to_trash", ids=identifiers)
    assert preview["count"] == 25 and preview["blocked"] == [] and preview["token"]
    assert peer.folders == before and not mutations(peer.commands)
    completed = backend.move_to_trash(preview["token"])
    assert completed["count"] == completed["done"] == 25
    assert completed["partial"] is False and completed["remaining"] == 0
    assert peer.identifiers("Trash") == set(identifiers) and not peer.folders["INBOX"]
    assert_only_uid_moves(peer, count=25)
