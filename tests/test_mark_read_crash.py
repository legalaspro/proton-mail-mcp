"""A read-state STORE and its subsequent MOVE have separate durable intents."""

import re

import pytest
from bridge_sim import mutations
from test_duplicate_msgid import DESTINATION, duplicates, logged, preview_move
from test_duplicate_msgid import duplicate_mailbox as duplicate_mailbox
from test_mark_read import mixed

from proton_mail_mcp import cleanup_imap
from proton_mail_mcp.action_log import LogTransaction
from proton_mail_mcp.mail import MailError, MailValidationError


class ProcessKilled(BaseException):
    pass


@pytest.mark.parametrize("crash", [False, True])
def test_seen_store_without_durable_move_intent_undo_restores_only_flags(
    duplicate_mailbox, monkeypatch, crash
):
    sim, client = duplicate_mailbox()
    approved = mixed(sim)
    # The first approved message must actually change Seen before the failure.
    approved[0].flags.discard(r"\Seen")
    before = [set(item.flags) for item in approved]
    preview = preview_move(client, mark_read=True)
    append = LogTransaction.append
    killed = False

    def fail_move_intent(transaction, record):
        nonlocal killed
        if killed:
            raise ProcessKilled()
        if record["action"] == "execute_progress" and record["message"].get("stage") == "move":
            if crash:
                killed = True
                raise ProcessKilled()
            raise MailError("Synthetic move intent failure")
        return append(transaction, record)

    with monkeypatch.context() as patch:
        patch.setattr(LogTransaction, "append", fail_move_intent)
        if crash:
            with pytest.raises(ProcessKilled):
                client.move_to_folder(preview["token"])
        else:
            result = client.move_to_folder(preview["token"])
            assert result["partial"] and result["count"] == 0
    original = logged(client)[0]
    assert original["messages"][0]["stage"] == "seen"
    assert original["messages"][0]["seen_before"] is False
    assert len(mutations(sim)) == 1 and r"\Seen" in approved[0].flags
    assert all(item.folder == "INBOX" for item in approved)

    restored = client.undo_action(original["log_id"])

    assert restored["count"] == 1 and not restored["partial"]
    assert [item.flags for item in approved] == before
    assert all(item.folder == "INBOX" for item in approved)
    assert all("UID STORE " in command for _, _, _, command in mutations(sim))


@pytest.mark.parametrize("after_move", [False, True])
def test_lost_move_reply_restores_source_flags_or_refuses_unmapped_destination(
    duplicate_mailbox, monkeypatch, after_move
):
    sim, client = duplicate_mailbox(copyuid=False)
    approved = mixed(sim)
    approved[0].flags.discard(r"\Seen")
    before = [set(item.flags) for item in approved]
    preview = preview_move(client, mark_read=True)
    append = LogTransaction.append
    uid = client.imap_factory.uid
    killed = False

    def cannot_log_after_crash(transaction, record):
        if killed:
            raise ProcessKilled()
        return append(transaction, record)

    def lose_reply(connection, command, *args):
        nonlocal killed
        if command.upper() == "MOVE":
            if after_move:
                uid(connection, command, *args)
            killed = True
            raise ProcessKilled()
        return uid(connection, command, *args)

    with monkeypatch.context() as patch:
        patch.setattr(LogTransaction, "append", cannot_log_after_crash)
        patch.setattr(client.imap_factory, "uid", lose_reply)
        with pytest.raises(ProcessKilled):
            client.move_to_folder(preview["token"])
    original = logged(client)[0]
    assert original["messages"][0]["stage"] == "move"
    assert original["messages"][0]["status"] == "uncertain"
    assert original["messages"][0]["uid_after"] is None
    changes = mutations(sim)

    restored = client.undo_action(original["log_id"])

    if after_move:
        assert restored["count"] == 0 and restored["partial"]
        assert "manual handling" in restored["missing"][0]["reason"]
        assert approved[0].folder == DESTINATION and r"\Seen" in approved[0].flags
        assert mutations(sim) == changes
    else:
        assert restored["count"] == 1 and not restored["partial"]
        assert [item.flags for item in approved] == before
        assert all(item.folder == "INBOX" for item in approved)
        assert all("UID STORE " in command for _, _, _, command in mutations(sim))


@pytest.mark.parametrize("replacement", [b"", b" FLAGS NIL", b' FLAGS "\\Seen"'])
def test_seen_preview_requires_complete_flags_metadata(duplicate_mailbox, monkeypatch, replacement):
    sim, client = duplicate_mailbox()
    mixed(sim)
    handle = sim.handle

    def without_flags(*args):
        response = handle(*args)
        if args[1] == "FETCH":
            response = re.sub(rb" FLAGS \([^)]*\)", lambda _: replacement, response)
        return response

    monkeypatch.setattr(sim, "handle", without_flags)
    with pytest.raises((MailValidationError, MailError)):
        client.preview_action("mark_read", query={"folder": "INBOX"})
    assert not mutations(sim) and not client.cleanup.tokens._grants


@pytest.mark.parametrize("destination_count", [0, 201])
def test_single_seen_move_without_copyuid_pins_destination_for_undo(
    duplicate_mailbox, destination_count
):
    sim, client = duplicate_mailbox(copyuid=False)
    approved = duplicates(sim, count=1)[0]
    existing = duplicates(sim, count=destination_count, identical=True, folder=DESTINATION)
    preview = preview_move(client, mark_read=True)
    result = client.move_to_folder(preview["token"])
    assert result["count"] == 1 and not result["partial"]
    assert result["messages"][0]["uid_after"] == str(sim.uid_of(DESTINATION, approved.pid))
    assert client.undo_action(result["log_ids"][0])["count"] == 1
    assert approved.folder == "INBOX" and not approved.flags
    assert all(item.folder == DESTINATION for item in existing)


@pytest.mark.parametrize("mapping", ["preexisting", "earlier_move"])
def test_stale_copyuid_cannot_choose_a_different_duplicate(duplicate_mailbox, monkeypatch, mapping):
    sim, client = duplicate_mailbox()
    approved = duplicates(sim, identical=True)
    preexisting = duplicates(sim, count=1, identical=True, folder=DESTINATION)[0]
    old_uid = str(sim.uid_of(DESTINATION, preexisting.pid))
    copyuid = cleanup_imap._copyuid
    first = None

    def stale(connection, response, uid):
        nonlocal first
        validity, actual = copyuid(connection, response, uid)
        if mapping == "preexisting":
            return validity, old_uid
        if first is None:
            first = actual
        return validity, first

    token = preview_move(client)["token"]
    with monkeypatch.context() as patch:
        patch.setattr(cleanup_imap, "_copyuid", stale)
        result = client.move_to_folder(token)
    assert result["count"] == 3 and not result["partial"]
    assert {entry["uid_after"] for entry in result["messages"]} == {
        str(sim.uid_of(DESTINATION, item.pid)) for item in approved
    }
    restored = client.undo_action(result["log_ids"][0])
    assert restored["count"] == 3 and not restored["partial"]
    assert all(item.folder == "INBOX" for item in approved)
    assert preexisting.folder == DESTINATION


def test_duplicate_undo_without_copyuid_restores_missing_original_labels(duplicate_mailbox):
    sim, client = duplicate_mailbox(copyuid=False)
    approved = duplicates(sim)
    for item in approved:
        item.labels.add("Labels/Review")
    preview = preview_move(client, "move_to_trash")
    result = client.move_to_trash(preview["token"])
    assert result["count"] == 3 and not result["partial"]
    # Bridge may remove labels when moving to Trash; restore only the recorded
    # membership belonging to each distinct message fingerprint.
    for item in approved:
        item.labels.clear()
    sim.sync()

    restored = client.undo_action(result["log_ids"][0])

    assert restored["count"] == 3 and not restored["partial"]
    assert all(item.folder == "INBOX" and item.labels == {"Labels/Review"} for item in approved)
    assert all(
        "UID MOVE " in command or "UID COPY " in command for _, _, _, command in mutations(sim)
    )
