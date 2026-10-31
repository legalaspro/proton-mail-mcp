"""Archive and trash certificates preserve identity and recovery guards."""

import re
from dataclasses import replace

import pytest
from action_records import execution_log
from resume_assertions import identity, journal_identity
from test_cleanup_mutations import uid_values
from test_cleanup_socket import mutations
from test_cleanup_socket import socket_mailbox as socket_mailbox

from proton_mail_mcp import cleanup_imap
from proton_mail_mcp.mail import CallDeadlineExceeded, MailValidationError


@pytest.mark.parametrize("action,target", [("archive", "Archive"), ("move_to_trash", "Trash")])
@pytest.mark.parametrize("explicit_source", [True, False])
def test_certified_cleanup_moves_only_fetch_exact_approved_identities(
    socket_mailbox, action, target, explicit_source
):
    client, peer, _ = socket_mailbox()
    query = {"subject": "October newsletter"}
    if explicit_source:
        query["folder"] = "INBOX"
    preview = client.preview_action(action, query=query)
    grant = client.cleanup.tokens.inspect(preview["token"], action)
    assert grant.bounded_recovery and all("label_states" in row for row in grant.messages)
    approved = {identity(row) for row in grant.messages}
    original = dict(peer.folders["INBOX"])
    allowed = {}
    for row in grant.messages:
        for member in [row, *row["memberships"]]:
            allowed.setdefault(member["folder"], set()).add(member["uid"])
    before = len(peer.commands)
    result = client.cleanup.execute(action, preview["token"])
    commands = peer.commands[before:]
    assert result["done"] == 2 and not result["partial"]
    assert not any(command.startswith(b"UID SEARCH") for _, command in commands)
    # Outcome verification reads only the new destination UIDs named by COPYUID.
    allowed[target] = {row["uid_after"] for row in result["messages"]}
    served = {}
    for folder, command in commands:
        if command.startswith(b"UID FETCH"):
            requested = set(command.split()[2].decode("ascii").split(","))
            assert requested and requested <= allowed[folder]
            served.setdefault(folder, set()).update(requested)
    assert served == allowed
    assert len(mutations(commands)) == 1  # One batched MOVE for the chunk.
    actual = [
        (folder, uid)
        for folder, command in mutations(commands)
        for uid in uid_values(command.split()[2])
    ]
    assert len(actual) == len(set(actual)) == 2
    assert set(actual) == {(item[0], item[2]) for item in approved}
    assert {journal_identity(row) for row in result["messages"]} == approved
    assert not peer.folders["INBOX"]
    assert set(peer.folders[target].items()) == {
        (row["uid_after"], original[row["uid_before"]]) for row in result["messages"]
    }
    with client.cleanup.backend.log.transaction(readonly=True) as transaction:
        logged = [row for record in transaction.records() for row in record["messages"]]
    assert {journal_identity(row) for row in logged} == approved and len(logged) == 2
    with pytest.raises(MailValidationError, match="already used"):
        client.cleanup.execute(action, preview["token"])
    restored = client.undo_action(result["log_ids"][0])
    assert restored["count"] == 2 and not restored["partial"]
    assert sorted(peer.folders["INBOX"].values()) == sorted(original.values())
    assert not peer.folders[target]


@pytest.mark.parametrize("action", ["archive", "move_to_trash"])
def test_missing_label_certificate_keeps_cleanup_inventory_refusal(socket_mailbox, action):
    client, peer, log_path = socket_mailbox()

    def omit_uidnext(folder, operation, command, response):
        if folder == "Labels/Review":
            return re.sub(rb"\* OK \[UIDNEXT [0-9]+\][^\r\n]*\r\n", b"", response)
        return response

    peer.response_hook = omit_uidnext
    preview = client.preview_action(action, query={"folder": "INBOX"})
    grant = client.cleanup.tokens.inspect(preview["token"], action)
    assert not grant.bounded_recovery
    assert all("label_states" not in row for row in grant.messages)
    original = dict(peer.folders["INBOX"])
    peer.folders["Labels/Review"]["501"] = original["7"]
    peer.next_uid["Labels/Review"] = 502
    before = len(peer.commands)
    with pytest.raises(MailValidationError, match="changed"):
        client.cleanup.execute(action, preview["token"])
    commands = peer.commands[before:]
    assert any(command.startswith(b"UID SEARCH") for _, command in commands)
    assert not mutations(commands) and not execution_log(log_path)
    assert peer.folders["INBOX"] == original
    client.cleanup.tokens.inspect(preview["token"], action)


@pytest.mark.parametrize("action", ["archive", "move_to_trash"])
def test_cleanup_copy_outside_bound_memberships_keeps_protection_discovery(socket_mailbox, action):
    client, peer, log_path = socket_mailbox()
    other = peer.folders["INBOX"]["7"].replace(b"October newsletter", b"Earlier copy")
    peer.folders["All Mail"]["500"] = other
    preview = client.preview_action(action, query={"folder": "INBOX"})
    assert preview["token"] and preview["count"] == 2
    grant = client.cleanup.tokens.inspect(preview["token"], action)
    assert not grant.bounded_recovery
    assert all("label_states" not in row for row in grant.messages)
    peer.folders["All Mail"]["500"] = other.replace(b"sender@example.com", b"person@gmail.com")
    original = dict(peer.folders["INBOX"])
    before = len(peer.commands)
    with pytest.raises(MailValidationError, match="protected"):
        client.cleanup.execute(action, preview["token"])
    commands = peer.commands[before:]
    assert any(command.startswith(b"UID SEARCH") for _, command in commands)
    assert not mutations(commands) and not execution_log(log_path)
    assert peer.folders["INBOX"] == original
    client.cleanup.tokens.inspect(preview["token"], action)


@pytest.mark.parametrize("action", ["archive", "move_to_trash"])
def test_implicit_cleanup_source_still_refuses_new_same_identity_copy(socket_mailbox, action):
    client, peer, log_path = socket_mailbox()
    preview = client.preview_action(action, query={"subject": "October newsletter"})
    grant = client.cleanup.tokens.inspect(preview["token"], action)
    assert grant.bounded_recovery
    assert all(row["implicit_source_states"] for row in grant.messages)
    original = dict(peer.folders["INBOX"])
    peer.folders["Folders/Reviewed"]["501"] = original["7"]
    peer.next_uid["Folders/Reviewed"] = 502
    before = len(peer.commands)
    with pytest.raises(MailValidationError, match="ambiguous"):
        client.cleanup.execute(action, preview["token"])
    commands = peer.commands[before:]
    assert any(command.startswith(b"UID SEARCH") for _, command in commands)
    assert not mutations(commands) and not execution_log(log_path)
    assert peer.folders["INBOX"] == original
    client.cleanup.tokens.inspect(preview["token"], action)


@pytest.mark.parametrize("action,target", [("archive", "Archive"), ("move_to_trash", "Trash")])
@pytest.mark.parametrize("mapping", ["missing", "preexisting", "validity", "earlier_move"])
def test_certified_cleanup_recovery_excludes_destination_arrivals(
    socket_mailbox, monkeypatch, action, target, mapping
):
    client, peer, _ = socket_mailbox()
    preview = client.preview_action(action, query={"folder": "INBOX"})
    grant = client.cleanup.tokens.inspect(preview["token"], action)
    assert grant.bounded_recovery
    original = dict(peer.folders["INBOX"])
    peer.folders[target]["501"] = original["7"]
    peer.next_uid[target] = 502
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

    before = len(peer.commands)
    with monkeypatch.context() as patch:
        patch.setattr(cleanup_imap, "_copyuid_map", altered)
        result = client.cleanup.execute(action, preview["token"])
    commands = peer.commands[before:]
    assert result["count"] == 2 and not result["partial"]
    assert all(row["destination_uidnext"] == "502" for row in result["messages"])
    assert {journal_identity(row) for row in result["messages"]} == {
        identity(row) for row in grant.messages
    }
    assert len(mutations(commands)) == 1
    assert uid_values(mutations(commands)[0][1].split()[2]) == ["7", "8"]
    assert not any(command.startswith(b"UID SEARCH") for _, command in commands)
    if mapping == "missing":
        # Without a mapping, leaving INBOX is not enough: the append boundary pins
        # each new copy, never the arrival below it.
        assert all("destination_before" not in row for row in result["messages"])
    assert [row["uid_after"] for row in result["messages"]] == ["502", "503"]
    assert all(
        set(command.split()[2].decode().split(",")) <= {"502", "503"}
        for folder, command in commands
        if folder == target and command.startswith(b"UID FETCH")
    )
    restored = client.undo_action(result["log_ids"][0])
    assert restored["count"] == 2 and not restored["partial"]
    assert peer.folders[target] == {"501": original["7"]}
    assert sorted(peer.folders["INBOX"].values()) == sorted(original.values())
    with pytest.raises(MailValidationError, match="already"):
        client.undo_action(result["log_ids"][0])


@pytest.mark.parametrize("action", ["archive", "move_to_trash"])
def test_short_read_only_timeout_remains_retryable_under_high_configured_budget(
    socket_mailbox, monkeypatch, action
):
    client, peer, log_path = socket_mailbox()
    client.settings = replace(client.settings, call_timeout=120, execute_timeout=120)
    preview = client.preview_action(action, query={"folder": "INBOX"})
    preflight = client.cleanup.backend._preflight

    def timed_out(*args, **kwargs):
        raise CallDeadlineExceeded("Synthetic early read-only timeout")

    monkeypatch.setattr(client.cleanup.backend, "_preflight", timed_out)
    before = len(peer.commands)
    with pytest.raises(CallDeadlineExceeded, match="early read-only timeout"):
        client.cleanup.execute(action, preview["token"])
    assert not mutations(peer.commands[before:])
    assert not execution_log(log_path)
    client.cleanup.tokens.inspect(preview["token"], action)
    monkeypatch.setattr(client.cleanup.backend, "_preflight", preflight)
    result = client.cleanup.execute(action, preview["token"])
    assert result["done"] == 2 and not result["partial"]


def test_digest_only_cleanup_recovery_cannot_select_a_preexisting_destination_uid(
    socket_mailbox, monkeypatch
):
    client, peer, _ = socket_mailbox()
    # Use an explicit source to allow the independently-arriving same-ID copy;
    # its UID must remain outside the resulting recovery authority.
    preview = client.preview_action("move_to_trash", query={"folder": "INBOX"})
    original = dict(peer.folders["INBOX"])
    peer.folders["Trash"]["501"] = original["7"]
    peer.next_uid["Trash"] = 502
    with monkeypatch.context() as patch:
        patch.setattr(cleanup_imap, "_copyuid_map", lambda *args: {})
        # Nor is either moved copy confirmed in Trash: both moves stay uncertain
        # without destination UIDs, so undo can only use their logged identity.
        patch.setattr(client.cleanup.backend, "_pin_destinations", lambda *args: None)
        result = client.move_to_trash(preview["token"])
    assert result["done"] == 0 and result["uncertain"] == 2
    assert all(row["uid_after"] is None for row in result["messages"])
    # Both actual moved copies subsequently disappear. A preexisting copy alone
    # must still never satisfy this move's digest-only recovery.
    peer.folders["Trash"].pop("502")
    peer.folders["Trash"].pop("503")
    before = len(mutations(peer.commands))
    restored = client.undo_action(result["log_ids"][0])
    assert restored["count"] == 0 and restored["partial"]
    assert len(mutations(peer.commands)) == before
    assert peer.folders["Trash"] == {"501": original["7"]}
    assert any("predates the move" in row["reason"] for row in restored["missing"])
