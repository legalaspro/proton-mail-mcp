"""Partial undo retries retain exact journal identities and never replay writes."""

from dataclasses import replace

import pytest
from test_cleanup_chunks import approve, mutations, records
from test_cleanup_chunks import chunk_mailbox as chunk_mailbox
from test_mail import message

from proton_mail_mcp import cleanup_imap
from proton_mail_mcp.action_log import LogTransaction
from proton_mail_mcp.mail import CallDeadlineExceeded, MailError, MailValidationError


def expire_before_second_undo(case, monkeypatch):
    """Spend a tiny undo budget before the second message's first write."""
    case.mailbox.settings = replace(case.mailbox.settings, execute_timeout=0.25)
    perform = case.service.backend._perform
    attempts = []

    def expire(connection, deadline, action, target, row, record, **kwargs):
        if kwargs.get("stage") == "undo":
            attempts.append(row["uid"])
            if len(attempts) == 2:
                case.clock.elapsed = deadline.end
        return perform(connection, deadline, action, target, row, record, **kwargs)

    monkeypatch.setattr(case.service.backend, "_perform", expire)
    return perform


def test_partial_undo_retries_only_untouched_entries_then_consumes_id(chunk_mailbox, monkeypatch):
    case = chunk_mailbox(count=3, move_seconds=0.1)
    originals = {id(item) for item in case.fake.folders["INBOX"].values()}
    completed = case.service.execute("move_to_trash", approve(case))
    forward = len(mutations(case))  # One batched MOVE for the chunk.
    log_id = completed["log_ids"][0]
    original_record = records(case)[0]
    initial_log = case.log.read_bytes()
    perform = expire_before_second_undo(case, monkeypatch)

    first = case.service.undo_action(log_id)
    assert first["count"] == 1 and first["partial"]
    assert [row["status"] for row in first["messages"]] == [
        "done",
        "not_attempted",
        "not_attempted",
    ]
    assert [row["original_index"] for row in first["messages"]] == [0, 1, 2]
    assert len(case.fake.folders["Trash"]) == 2
    assert case.log.read_bytes().startswith(initial_log)
    first_log = case.log.read_bytes()

    monkeypatch.setattr(case.service.backend, "_perform", perform)
    case.mailbox.settings = replace(case.mailbox.settings, execute_timeout=300)
    second = case.service.undo_action(log_id)
    assert second["count"] == 2 and not second["partial"]
    assert [row["original_index"] for row in second["messages"]] == [1, 2]
    assert all(row["status"] == "done" for row in second["messages"])
    assert not case.fake.folders["Trash"]
    assert {id(item) for item in case.fake.folders["INBOX"].values()} == originals
    assert case.log.read_bytes().startswith(first_log)
    journal = records(case)
    assert journal[0] == original_record
    assert [record["action"] for record in journal] == [
        "move_to_trash",
        "undo_claim",
        "undo_action",
        "undo_claim",
        "undo_action",
    ]
    for claim, outcome in ((journal[1], journal[2]), (journal[3], journal[4])):
        assert claim["undo_of"] == outcome["undo_of"] == log_id
        assert claim["outcome_log_id"] == outcome["log_id"]
    assert journal[2]["partial"] and not journal[4]["partial"]
    inverse_moves = mutations(case)[forward:]
    assert [call[1] for call in inverse_moves] == [
        row["uid_after"] for row in original_record["messages"]
    ]
    assert all(call[0] == "MOVE" and call[2] == '"INBOX"' for call in inverse_moves)
    final_log = case.log.read_bytes()
    final_mutations = mutations(case)
    with pytest.raises(MailValidationError, match="already been undone"):
        case.service.undo_action(log_id)
    assert case.log.read_bytes() == final_log
    assert mutations(case) == final_mutations


@pytest.mark.parametrize(
    "action,inverse_commands",
    [("add_label", ["STORE", "EXPUNGE"]), ("remove_label", ["COPY"])],
)
def test_partial_label_undo_retries_only_untouched_memberships(
    chunk_mailbox, monkeypatch, action, inverse_commands
):
    case = chunk_mailbox(count=3, move_seconds=0.1)
    originals = dict(case.fake.folders["INBOX"])
    if action == "remove_label":
        case.fake.folders["Labels/News"].update(originals)
    preview = case.service.preview_action(action, ids=case.identifiers, params={"label": "News"})
    assert preview["count"] == 3 and preview["token"]
    completed = case.service.execute(action, preview["token"])
    log_id = completed["log_ids"][0]
    execute_changes = len(mutations(case))
    perform = expire_before_second_undo(case, monkeypatch)
    partial = case.service.undo_action(log_id)
    assert partial["count"] == 1 and partial["partial"]
    assert [row["status"] for row in partial["messages"]] == [
        "done",
        "not_attempted",
        "not_attempted",
    ]
    first_log = case.log.read_bytes()
    monkeypatch.setattr(case.service.backend, "_perform", perform)
    case.mailbox.settings = replace(case.mailbox.settings, execute_timeout=300)
    retry = case.service.undo_action(log_id)
    assert retry["count"] == 2 and not retry["partial"]
    assert [row["original_index"] for row in retry["messages"]] == [1, 2]
    assert case.log.read_bytes().startswith(first_log)
    assert [call[0] for call in mutations(case)[execute_changes:]] == inverse_commands * 3
    labels = case.fake.folders["Labels/News"]
    assert len(labels) == (3 if action == "remove_label" else 0)
    if labels:
        assert {id(item) for item in labels.values()} == {id(item) for item in originals.values()}
    assert case.fake.folders["INBOX"] == case.fake.folders["All Mail"] == originals
    assert not case.fake.deleted
    assert [record["action"] for record in records(case)].count("undo_claim") == 2
    with pytest.raises(MailValidationError, match="already been undone"):
        case.service.undo_action(log_id)


def test_partial_flag_restoration_retry_preserves_other_label_memberships(
    chunk_mailbox, monkeypatch
):
    case = chunk_mailbox(count=3)
    originals = dict(case.fake.folders["INBOX"])
    case.fake.folders["Labels/News"].update(originals)
    preview = case.service.preview_action(
        "remove_label", ids=case.identifiers, params={"label": "News"}
    )
    checked = cleanup_imap._checked

    def expire_after_label_flag(reply):
        result = checked(reply)
        if case.fake.deleted:
            # Expire after the STORE acknowledgement returns through the
            # session proxy, immediately before the following deadline check.
            case.clock.elapsed += 300
        return result

    monkeypatch.setattr(cleanup_imap, "_checked", expire_after_label_flag)
    completed = case.service.execute("remove_label", preview["token"])
    log_id = completed["log_ids"][0]
    assert [row["status"] for row in records(case)[0]["messages"]] == [
        "flagged",
        "not_attempted",
        "not_attempted",
    ]
    assert case.fake.deleted == {("Labels/News", "1")}
    monkeypatch.setattr(cleanup_imap, "_checked", checked)
    perform = case.service.backend._perform
    case.mailbox.settings = replace(case.mailbox.settings, execute_timeout=0.25)

    def expire_before_flag_restore(connection, deadline, action, target, row, record, **kwargs):
        assert action == "restore_label_flag"
        case.clock.elapsed = deadline.end
        return perform(connection, deadline, action, target, row, record, **kwargs)

    monkeypatch.setattr(case.service.backend, "_perform", expire_before_flag_restore)
    partial = case.service.undo_action(log_id)
    assert partial["partial"] and partial["count"] == 0
    assert [row["status"] for row in partial["messages"]] == ["not_attempted"]
    assert case.fake.deleted == {("Labels/News", "1")}
    first_log = case.log.read_bytes()
    monkeypatch.setattr(case.service.backend, "_perform", perform)
    case.mailbox.settings = replace(case.mailbox.settings, execute_timeout=300)
    retry = case.service.undo_action(log_id)
    assert retry["count"] == 1 and not retry["partial"]
    assert retry["messages"][0]["original_index"] == 0
    assert mutations(case) == [
        ("STORE", "1", "+FLAGS.SILENT", r"(\Deleted)"),
        ("STORE", "1", "-FLAGS.SILENT", r"(\Deleted)"),
    ]
    assert not case.fake.deleted
    assert case.fake.folders["Labels/News"] == originals
    assert case.fake.folders["INBOX"] == case.fake.folders["All Mail"] == originals
    assert case.log.read_bytes().startswith(first_log)
    with pytest.raises(MailValidationError, match="already been undone"):
        case.service.undo_action(log_id)


@pytest.mark.parametrize("failure", ["label_copy", "copyuid"])
def test_partial_label_restoration_never_repeats_successful_folder_move(
    chunk_mailbox, monkeypatch, failure
):
    case = chunk_mailbox(count=3)
    originals = list(case.fake.folders["INBOX"].values())
    case.fake.folders["Labels/News"].update(case.fake.folders["INBOX"])
    case.fake.strip_labels_on_trash = True
    completed = case.service.execute("move_to_trash", approve(case))
    log_id = completed["log_ids"][0]
    original_record = records(case)[0]
    assert not case.fake.folders["Labels/News"]
    uid = case.fake.uid
    copyuid = cleanup_imap._copyuid

    def fail_label_copy(command, *args):
        if command == "COPY":
            raise CallDeadlineExceeded("Synthetic label restoration interruption")
        return uid(command, *args)

    def fail_copyuid(*_):
        raise ValueError("Synthetic malformed COPYUID response")

    if failure == "label_copy":
        monkeypatch.setattr(case.fake, "uid", fail_label_copy)
    else:
        monkeypatch.setattr(cleanup_imap, "_copyuid", fail_copyuid)
    partial = case.service.undo_action(log_id)
    assert partial["partial"] and partial["count"] == 0
    assert [row["status"] for row in partial["messages"]] == [
        "moved",
        "not_attempted",
        "not_attempted",
    ]
    if failure == "label_copy":
        assert partial["messages"][0]["restored_labels"][0]["status"] == "uncertain"
    first_log = case.log.read_bytes()
    monkeypatch.setattr(case.fake, "uid", uid)
    monkeypatch.setattr(cleanup_imap, "_copyuid", copyuid)
    retry = case.service.undo_action(log_id)
    assert retry["partial"] and retry["count"] == 2
    assert retry["unresolved"]
    assert all(row["log_id"] == partial["log_id"] for row in retry["unresolved"])
    assert all("outcome_log_id" not in row for row in retry["unresolved"])
    assert [row["original_index"] for row in retry["messages"]] == [1, 2]
    undo_moves = [call for call in mutations(case) if call[0] == "MOVE" and call[2] == '"INBOX"']
    assert [call[1] for call in undo_moves] == [
        row["uid_after"] for row in original_record["messages"]
    ]
    assert {id(item) for item in case.fake.folders["INBOX"].values()} == {
        id(item) for item in originals
    }
    assert not case.fake.folders["Trash"]
    assert {id(item) for item in case.fake.folders["Labels/News"].values()} == {
        id(item) for item in originals[1:]
    }
    assert case.log.read_bytes().startswith(first_log)
    with pytest.raises(MailValidationError, match="unresolved"):
        case.service.undo_action(log_id)


def test_partial_undo_retry_rechecks_protected_sender_before_claim(chunk_mailbox, monkeypatch):
    case = chunk_mailbox(count=3, move_seconds=0.1)
    completed = case.service.execute("move_to_trash", approve(case))
    log_id = completed["log_ids"][0]
    perform = expire_before_second_undo(case, monkeypatch)
    partial = case.service.undo_action(log_id)
    assert partial["partial"] and partial["count"] == 1
    # New protection applies to the original identity without changing its
    # digest; changed headers would instead be refused as identity drift.
    case.service.backend.policy.rules["domains"] += ("example.com",)
    first_log = case.log.read_bytes()
    changes = mutations(case)
    monkeypatch.setattr(case.service.backend, "_perform", perform)
    case.mailbox.settings = replace(case.mailbox.settings, execute_timeout=300)
    with pytest.raises(MailValidationError, match="Protected sender"):
        case.service.undo_action(log_id)
    assert mutations(case) == changes
    assert case.log.read_bytes() == first_log


@pytest.mark.parametrize("write_happened", [False, True])
def test_partial_undo_never_retries_uncertain_move(chunk_mailbox, monkeypatch, write_happened):
    case = chunk_mailbox(count=3)
    originals = list(case.fake.folders["INBOX"].values())
    completed = case.service.execute("move_to_trash", approve(case))
    log_id = completed["log_ids"][0]
    original_record = records(case)[0]
    uid = case.fake.uid
    attempts = []

    def lose_acknowledgement(command, *args):
        if command == "MOVE":
            attempts.append(args[0])
            if len(attempts) == 2:
                if write_happened:
                    uid(command, *args)
                raise CallDeadlineExceeded("Synthetic lost undo acknowledgement")
        return uid(command, *args)

    monkeypatch.setattr(case.fake, "uid", lose_acknowledgement)
    first = case.service.undo_action(log_id)
    assert first["count"] == 1 and first["partial"]
    assert [row["status"] for row in first["messages"]] == ["done", "uncertain", "not_attempted"]
    first_log = case.log.read_bytes()
    monkeypatch.setattr(case.fake, "uid", uid)
    # A new copy sharing the uncertain message's Message-ID must not broaden
    # recovery, whether or not the original uncertain MOVE actually happened.
    replacement = message(case.identifiers[1])
    case.fake.folders["Trash"]["9999"] = replacement
    changes_before_retry = len(mutations(case))
    second = case.service.undo_action(log_id)
    assert second["count"] == 1 and second["partial"]
    assert [row["original_index"] for row in second["messages"]] == [2]
    assert second["messages"][0]["status"] == "done"
    assert mutations(case)[changes_before_retry:] == [
        ("MOVE", original_record["messages"][2]["uid_after"], '"INBOX"')
    ]
    assert case.fake.folders["Trash"]["9999"] is replacement
    assert any(item is originals[0] for item in case.fake.folders["INBOX"].values())
    assert any(item is originals[2] for item in case.fake.folders["INBOX"].values())
    assert (
        any(item is originals[1] for item in case.fake.folders["INBOX"].values()) is write_happened
    )
    assert case.log.read_bytes().startswith(first_log)
    journal = records(case)
    assert [record["action"] for record in journal].count("undo_claim") == 2
    assert journal[2]["messages"][1]["status"] == "uncertain"
    with pytest.raises(MailValidationError, match="unresolved"):
        case.service.undo_action(log_id)


@pytest.mark.parametrize("copyuid", [True, False])
def test_partial_undo_retry_never_substitutes_a_new_uid(chunk_mailbox, monkeypatch, copyuid):
    case = chunk_mailbox(count=3, move_seconds=0.1)
    if not copyuid:
        response = case.fake.response

        def omit_copyuid(name):
            if name == "COPYUID":
                case.fake.copyuid = None
                return name, [None]
            return response(name)

        monkeypatch.setattr(case.fake, "response", omit_copyuid)
        # Nor is any moved copy confirmed in Trash: the moves stay uncertain
        # without destination UIDs, and undo must resolve each one itself.
        monkeypatch.setattr(case.service.backend, "_pin_destinations", lambda *args: None)
    completed = case.service.execute("move_to_trash", approve(case))
    log_id = completed["log_ids"][0]
    original_record = records(case)[0]
    perform = expire_before_second_undo(case, monkeypatch)
    partial = case.service.undo_action(log_id)
    assert partial["partial"]
    monkeypatch.setattr(case.service.backend, "_perform", perform)
    case.mailbox.settings = replace(case.mailbox.settings, execute_timeout=300)
    # Without a logged destination UID, the first attempt must pin each
    # resolved UID. Retry must use that handle rather than repeat its lookup.
    assert bool(original_record["messages"][1]["uid_after"]) is copyuid
    missing_uid = partial["messages"][1]["uid_before"]
    case.fake.folders["Trash"].pop(missing_uid)
    replacement = message(case.identifiers[1])
    case.fake.folders["Trash"]["9999"] = replacement
    before = len(mutations(case))
    retry = case.service.undo_action(log_id)
    assert retry["partial"] and retry["count"] == 1
    assert mutations(case)[before:] == [("MOVE", partial["messages"][2]["uid_before"], '"INBOX"')]
    assert case.fake.folders["Trash"] == {"9999": replacement}
    assert retry["missing"][0]["message_id"] == case.identifiers[1]
    assert "not found" in retry["missing"][0]["reason"]


def test_undo_holds_single_journal_claim_during_concurrent_attempt(chunk_mailbox, monkeypatch):
    case = chunk_mailbox(count=2)
    completed = case.service.execute("move_to_trash", approve(case))
    log_id = completed["log_ids"][0]
    perform = case.service.backend._perform
    nested_attempts = []

    def nested(connection, deadline, action, target, row, record, **kwargs):
        nested_attempts.append(True)
        assert [entry["action"] for entry in records(case)] == ["move_to_trash", "undo_claim"]
        with pytest.raises(MailError, match="log is busy"):
            case.service.backend.undo_action(log_id)
        return perform(connection, deadline, action, target, row, record, **kwargs)

    monkeypatch.setattr(case.service.backend, "_perform", nested)
    restored = case.service.undo_action(log_id)
    assert restored["count"] == 2 and not restored["partial"]
    assert len(nested_attempts) == 2
    assert [entry["action"] for entry in records(case)] == [
        "move_to_trash",
        "undo_claim",
        "undo_action",
    ]
    assert len(mutations(case)) == 3  # One batched forward MOVE, two undo MOVEs.


@pytest.mark.parametrize("failure_stage", ["claim", "outcome"])
def test_unpaired_claim_after_crash_blocks_undo_replay(chunk_mailbox, monkeypatch, failure_stage):
    case = chunk_mailbox(count=2)
    completed = case.service.execute("move_to_trash", approve(case))
    log_id = completed["log_ids"][0]
    append = LogTransaction.append

    def crash_after_claim(transaction, record):
        if failure_stage == "outcome" and record["action"] == "undo_action":
            raise RuntimeError("Synthetic process interruption before durable outcome")
        append(transaction, record)
        if failure_stage == "claim" and record["action"] == "undo_claim":
            raise RuntimeError("Synthetic process interruption after durable claim")

    monkeypatch.setattr(LogTransaction, "append", crash_after_claim)
    with pytest.raises(MailError, match="Mailbox operation failed"):
        case.service.undo_action(log_id)
    # One batched forward MOVE; the completed undo adds one MOVE per message.
    mutation_count = 1 if failure_stage == "claim" else 3
    assert len(mutations(case)) == mutation_count
    assert [record["action"] for record in records(case)] == ["move_to_trash", "undo_claim"]
    claim_log = case.log.read_bytes()
    monkeypatch.setattr(LogTransaction, "append", append)
    with pytest.raises(MailValidationError, match="unresolved"):
        case.service.undo_action(log_id)
    assert case.log.read_bytes() == claim_log
    assert len(mutations(case)) == mutation_count
