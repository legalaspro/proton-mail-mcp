"""Interrupted MOVE recovery must establish more than a matching header."""

import json

import pytest
from action_records import execution_records as replay_records
from test_cleanup_chunks import approve, mutations
from test_cleanup_chunks import chunk_mailbox as chunk_mailbox
from test_mail import message

from proton_mail_mcp.action_log import LogTransaction
from proton_mail_mcp.mail import MailValidationError


class ProcessKilled(BaseException):
    pass


def crash_before_move(case, monkeypatch, token):
    append, uid = LogTransaction.append, case.fake.uid
    killed = False

    def refuse_after_kill(transaction, record):
        if killed:
            raise ProcessKilled()
        return append(transaction, record)

    def crash(command, *args):
        nonlocal killed
        if command == "MOVE":
            killed = True
            raise ProcessKilled()
        return uid(command, *args)

    with monkeypatch.context() as patch:
        patch.setattr(LogTransaction, "append", refuse_after_kill)
        patch.setattr(case.fake, "uid", crash)
        with pytest.raises(ProcessKilled):
            case.mailbox.move_to_trash(token)
    assert not mutations(case)
    actions = replay_records(case.mailbox.list_actions(token=token)["actions"])
    assert len(actions) == 1
    return actions[0]["log_id"]


@pytest.mark.parametrize("changed_uid", [False, True])
@pytest.mark.parametrize("same_metadata", [False, True])
def test_crashed_move_never_restores_preexisting_destination_collision(
    chunk_mailbox, monkeypatch, changed_uid, same_metadata
):
    case = chunk_mailbox(count=1)
    approved = case.fake.folders["INBOX"]["1"]
    unrelated = message(
        case.identifiers[0],
        sections=approved.sections if same_metadata else {"1": b"different unrelated content"},
    )
    case.fake.folders["Trash"]["901"] = unrelated
    preview = case.mailbox.preview_action("move_to_trash", query={"folder": "INBOX"})
    assert preview["token"] and preview["count"] == 1 and not preview["blocked"]
    log_id = crash_before_move(case, monkeypatch, preview["token"])

    # Independent activity can archive the source and give the colliding copy
    # a new UID without changing UIDVALIDITY. Neither action was our MOVE.
    case.fake.folders["Archive"]["401"] = case.fake.folders["INBOX"].pop("1")
    if changed_uid:
        case.fake.folders["Trash"]["902"] = case.fake.folders["Trash"].pop("901")
    result = case.mailbox.undo_action(log_id)

    assert result["count"] == 0 and result["partial"]
    assert "manual handling required" in result["missing"][0]["reason"]
    expected = (
        "found 2 matching physical copies"
        if same_metadata
        else "matching physical copy is outside the required Trash destination"
    )
    assert expected in result["missing"][0]["reason"]
    assert not mutations(case)
    assert not case.fake.folders["INBOX"]
    assert list(case.fake.folders["Trash"].values()) == [unrelated]
    assert case.fake.folders["Archive"]["401"] is approved


@pytest.mark.parametrize("changed", ["size", "internaldate"])
@pytest.mark.parametrize("original_remains", [False, True])
def test_crashed_move_rejects_new_header_only_match(
    chunk_mailbox, monkeypatch, changed, original_remains
):
    case = chunk_mailbox(count=1)
    approved = case.fake.folders["INBOX"]["1"]
    log_id = crash_before_move(case, monkeypatch, approve(case))
    case.fake.folders["INBOX"].pop("1")
    if original_remains:
        case.fake.folders["Archive"]["401"] = approved
    else:
        case.fake.folders["All Mail"].clear()
    unrelated = message(
        case.identifiers[0],
        sections={"1": b"different unrelated content"} if changed == "size" else approved.sections,
        internaldate="15-Oct-2026 10:00:00 +0000" if changed == "internaldate" else None,
    )
    assert unrelated.headers == approved.headers
    case.fake.folders["Trash"]["901"] = unrelated

    result = case.mailbox.undo_action(log_id)

    assert result["count"] == 0 and result["partial"]
    assert "manual handling required" in result["missing"][0]["reason"]
    reason = (
        "matching physical copy is outside the required Trash destination"
        if original_remains
        else "found 0 matching physical copies"
    )
    assert reason in result["missing"][0]["reason"]
    assert not mutations(case)
    assert not case.fake.folders["INBOX"]
    assert case.fake.folders["Trash"]["901"] is unrelated
    assert list(case.fake.folders["Archive"].values()) == ([approved] if original_remains else [])


@pytest.mark.parametrize("logged_uid", [False, True])
@pytest.mark.parametrize("missing", ["size", "internaldate"])
def test_legacy_log_needs_destination_handle_without_message_metadata(
    chunk_mailbox, logged_uid, missing
):
    case = chunk_mailbox(count=1)
    completed = case.mailbox.move_to_trash(approve(case))
    record = replay_records([json.loads(line) for line in case.log.read_text().splitlines()])[0]
    entry = record["messages"][0]
    del entry[missing]
    if not logged_uid:
        entry["uid_after"] = entry["uidvalidity_after"] = None
    case.log.write_text(json.dumps(record) + "\n")
    before = mutations(case)

    result = case.mailbox.undo_action(completed["log_ids"][0])

    assert result["count"] == int(logged_uid)
    assert result["partial"] is not logged_uid
    assert len(mutations(case)) - len(before) == int(logged_uid)
    if not logged_uid:
        assert result["missing"][0]["reason"] == (
            "no logged message size or internal date; manual handling required "
            'during undo in "Trash"'
        )
        assert len(case.fake.folders["Trash"]) == 1


@pytest.mark.parametrize("changed", ["size", "internaldate"])
def test_recovery_metadata_is_bound_to_execute_approval(chunk_mailbox, changed):
    case = chunk_mailbox(count=1)
    token = approve(case)
    original = case.fake.folders["INBOX"]["1"]
    if changed == "size":
        original.sections = {"1": b"changed body size"}
    else:
        original.internaldate = "15-Oct-2026 10:00:00 +0000"

    with pytest.raises(MailValidationError, match="changed"):
        case.mailbox.move_to_trash(token)

    assert not mutations(case)
    assert not case.claims


@pytest.mark.parametrize("changed", ["size", "internaldate"])
def test_fallback_identity_is_rechecked_before_undo_move(chunk_mailbox, monkeypatch, changed):
    case = chunk_mailbox(count=1)
    response = case.fake.response
    monkeypatch.setattr(
        case.fake,
        "response",
        lambda name: (name, [None]) if name == "COPYUID" else response(name),
    )
    # Nor is the moved copy confirmed in Trash: the move stays uncertain without
    # a destination UID, so undo relies on the logged fallback identity.
    monkeypatch.setattr(case.service.backend, "_pin_destinations", lambda *args: None)
    completed = case.mailbox.move_to_trash(approve(case))
    assert completed["uncertain"] == 1 and completed["messages"][0]["uid_after"] is None
    perform = case.service.backend._perform

    def change_before_verification(connection, deadline, action, target, row, record, **kwargs):
        if kwargs.get("stage") == "undo":
            item = case.fake.folders[row["folder"]][row["uid"]]
            if changed == "size":
                item.sections = {"1": b"changed body size"}
            else:
                item.internaldate = "15-Oct-2026 10:00:00 +0000"
        return perform(connection, deadline, action, target, row, record, **kwargs)

    monkeypatch.setattr(case.service.backend, "_perform", change_before_verification)
    before = mutations(case)

    result = case.mailbox.undo_action(completed["log_ids"][0])

    assert result["count"] == 0 and result["partial"]
    assert result["messages"][0]["status"] == "not_attempted"
    assert "message size or internal date changed" in result["missing"][0]["reason"]
    assert mutations(case) == before
    assert len(case.fake.folders["Trash"]) == 1
