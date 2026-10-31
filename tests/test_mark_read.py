"""Seen changes are approved, journaled and reversible per exact physical UID."""

import re

import pytest
from bridge_sim import Msg, hdr, mutations
from resume_assertions import assert_bound_progress
from test_cleanup_mutations import uid_values
from test_duplicate_msgid import (
    DESTINATION,
    duplicates,
    logged,
    observe_sim_mutations,
    preview_move,
    rules_service,
)
from test_duplicate_msgid import duplicate_mailbox as duplicate_mailbox

from proton_mail_mcp import guarded
from proton_mail_mcp.action_log import ActionLog, LogTransaction
from proton_mail_mcp.mail import MailError, MailValidationError


def mixed(sim):
    items = duplicates(sim, identical=True)
    items[0].flags = {r"\Seen", r"\Answered", "custom-keyword"}
    items[1].flags = {r"\Flagged", "custom-keyword"}
    return items


def seen_stores(sim):
    """Every UID named by a Seen STORE (batched commands name exact UID sets)."""
    commands = [command for _, _, _, command in mutations(sim)]
    assert commands
    uid = r"[1-9][0-9]*(?::[1-9][0-9]*)?"
    assert all(
        re.fullmatch(rf"UID STORE {uid}(?:,{uid})* [+-]FLAGS\.SILENT \(\\Seen\)", command)
        for command in commands
    )
    return [value for command in commands for value in uid_values(command.split()[2])]


@pytest.mark.parametrize("action", ["mark_read", "mark_unread"])
def test_seen_preview_confirm_single_use_and_mixed_state_undo(duplicate_mailbox, action):
    sim, client = duplicate_mailbox()
    approved = mixed(sim)
    outsider = sim.add(Msg(hdr("<outside@example.test>", from_="other@example.test")))
    before = [set(item.flags) for item in approved]
    preview = client.preview_action(action, query={"folder": "INBOX", "from_": "github.example"})
    assert preview["count"] == 3 and preview["token"] and not preview["blocked"]
    assert not mutations(sim) and [item.flags for item in approved] == before
    result = getattr(client, action)(preview["token"])
    assert result["count"] == 3 and not result["partial"] and result["token_consumed"]
    for item, flags in zip(approved, before, strict=True):
        assert item.flags == (flags | {r"\Seen"} if action == "mark_read" else flags - {r"\Seen"})
        assert item.folder == "INBOX"
    assert not outsider.flags and outsider.folder == "INBOX"
    seen_stores(sim)
    assert (
        client.list_actions(token=preview["token"])["actions"][0]["log_id"] == result["log_ids"][0]
    )
    with pytest.raises(MailValidationError, match="already used"):
        getattr(client, action)(preview["token"])
    restored = client.undo_action(result["log_ids"][0])
    assert restored["count"] == 3 and not restored["partial"]
    assert [item.flags for item in approved] == before
    seen_stores(sim)
    before_retry = mutations(sim)
    with pytest.raises(MailValidationError, match="already.*undone"):
        client.undo_action(result["log_ids"][0])
    assert mutations(sim) == before_retry


@pytest.mark.parametrize("action", ["mark_read", "mark_unread"])
def test_seen_journal_intent_precedes_every_store(duplicate_mailbox, action):
    sim, client = duplicate_mailbox()
    items = mixed(sim)
    expected = {str(sim.uid_of("INBOX", item.pid)): r"\Seen" in item.flags for item in items}
    observed = []

    def check_intent(sid, folder, operation, command):
        if operation == "STORE":
            entries = [entry for record in logged(client) for entry in record.get("messages", [])]
            # Every UID in the batched STORE has its durable intent first.
            for uid in uid_values(command.split()[2]):
                entry = next(entry for entry in entries if entry["uid_before"] == uid)
                assert entry["status"] == "uncertain"
                assert entry["seen_before"] is expected[uid]
                observed.append(uid)

    sim.hooks.append(check_intent)
    token = client.preview_action(action, query={"folder": "INBOX"})["token"]
    result = getattr(client, action)(token)
    assert result["count"] == 3 and set(observed) == set(expected)


@pytest.mark.parametrize("action", ["mark_read", "mark_unread"])
@pytest.mark.parametrize("failure", ["open", "initial_append", "intent_append"])
def test_unwritable_seen_journal_means_zero_store(
    duplicate_mailbox, tmp_path, monkeypatch, action, failure
):
    sim, client = duplicate_mailbox()
    approved = mixed(sim)
    before = [set(item.flags) for item in approved]
    token = client.preview_action(action, query={"folder": "INBOX"})["token"]
    if failure == "open":
        client.cleanup.backend.log = ActionLog(tmp_path)
    else:
        append = LogTransaction.append

        def refuse(transaction, record):
            if failure == "initial_append" or (
                record["action"] == "execute_progress"
                and record["message"]["status"] == "uncertain"
            ):
                raise MailError("synthetic unwritable journal")
            return append(transaction, record)

        monkeypatch.setattr(LogTransaction, "append", refuse)
    try:
        result = getattr(client, action)(token)
    except MailError:
        pass
    else:
        assert result["count"] == 0 and result["partial"]
    assert not mutations(sim) and [item.flags for item in approved] == before


@pytest.mark.parametrize("action", ["mark_read", "mark_unread"])
@pytest.mark.parametrize("change", ["uidvalidity", "missing", "date", "size"])
def test_seen_confirmation_refuses_drift_without_any_store(duplicate_mailbox, action, change):
    sim, client = duplicate_mailbox()
    approved = mixed(sim)
    token = client.preview_action(action, query={"folder": "INBOX"})["token"]
    if change == "uidvalidity":
        sim.reset_uidvalidity("INBOX", renumber=False)
    elif change == "missing":
        del sim.messages[approved[-1].pid]
    elif change == "date":
        approved[-1].headers = approved[-1].headers.replace(b"09:00:00", b"10:00:00")
    else:
        approved[-1].body += b"additional bytes"
    with pytest.raises(MailValidationError):
        getattr(client, action)(token)
    assert not mutations(sim)


@pytest.mark.parametrize("change", ["uidvalidity", "missing", "date", "size"])
def test_seen_undo_refuses_drift(duplicate_mailbox, change):
    sim, client = duplicate_mailbox()
    approved = mixed(sim)
    token = client.preview_action("mark_read", query={"folder": "INBOX"})["token"]
    result = client.mark_read(token)
    if change == "uidvalidity":
        sim.reset_uidvalidity("INBOX", renumber=False)
    elif change == "missing":
        del sim.messages[approved[-1].pid]
    elif change == "date":
        approved[-1].headers = approved[-1].headers.replace(b"09:00:00", b"10:00:00")
    else:
        approved[-1].body += b"additional bytes"
    before = mutations(sim)
    undone = client.undo_action(result["log_ids"][0])
    assert undone["partial"] and undone["missing"]
    assert mutations(sim) == before and undone["count"] == 0


@pytest.mark.parametrize("action", ["mark_read", "mark_unread"])
def test_seen_cap_201_and_protected_sender_refused(duplicate_mailbox, action):
    sim, client = duplicate_mailbox()
    approved = duplicates(sim, 201)
    preview = client.preview_action(action, query={"folder": "INBOX"})
    assert preview["count"] == 201 and preview["token"] is None and "200" in preview["reason"]
    for item in approved[1:]:
        del sim.messages[item.pid]
    approved[0].headers = hdr("<protected@example.test>", from_="relative@gmail.com")
    protected = client.preview_action(action, query={"folder": "INBOX"})
    assert protected["token"] is None and protected["blocked"]
    assert not mutations(sim)


def test_seen_cap_200_executes_all_uids_across_chunks(duplicate_mailbox, monkeypatch):
    # One batched chunk could hold all 200; keep several chunks to undo separately.
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 21)
    sim, client = duplicate_mailbox()
    approved = duplicates(sim, 200)
    token = client.preview_action("mark_read", query={"folder": "INBOX"})["token"]
    assert token
    original_token = token
    grant = client.cleanup.tokens.inspect(token, "mark_read")
    actual = observe_sim_mutations(sim, grant, operation="STORE")
    results = []
    while token:
        result = client.mark_read(token)
        results.append(result)
        assert_bound_progress(client.cleanup, grant, results, actual)
        token = result["continuation_token"]
    assert sum(result["done"] for result in results) == 200 and not result["partial"]
    assert all(item.flags == {r"\Seen"} and item.folder == "INBOX" for item in approved)
    assert len(seen_stores(sim)) == 200
    assert {row["uid"] for row in grant.messages} == {
        str(sim.uid_of("INBOX", item.pid)) for item in approved
    }
    before_retry = mutations(sim)
    with pytest.raises(MailValidationError, match="already used"):
        client.mark_read(original_token)
    assert mutations(sim) == before_retry
    sim.hooks.remove(actual.hook)
    chunks = logged(client)
    assert all(0 < len(chunk["messages"]) <= 21 for chunk in chunks)
    assert len(mutations(sim)) == len(chunks)  # One batched STORE per chunk.
    restored_uids = set()
    for chunk in chunks:
        expected = {row["uid_before"] for row in chunk["messages"] if row["status"] == "done"}
        before = len(mutations(sim))
        restored = client.undo_action(chunk["log_id"])
        assert restored["count"] == len(expected) and not restored["partial"]
        commands = mutations(sim)[before:]
        assert len(commands) == len(expected)
        assert {command.split()[2] for _, _, _, command in commands} == expected
        assert not restored_uids.intersection(expected)
        restored_uids.update(expected)
        assert {
            str(sim.uid_of("INBOX", item.pid)) for item in approved if not item.flags
        } == restored_uids
        before_retry = mutations(sim)
        with pytest.raises(MailValidationError, match="already.*undone"):
            client.undo_action(chunk["log_id"])
        assert mutations(sim) == before_retry
    assert len(restored_uids) == 200
    assert all(not item.flags and item.folder == "INBOX" for item in approved)
    assert len(seen_stores(sim)) == 400


@pytest.mark.parametrize("action", ["mark_read", "mark_unread"])
def test_seen_requires_correct_unexpired_action_token(duplicate_mailbox, action):
    sim, client = duplicate_mailbox()
    mixed(sim)
    with pytest.raises(MailValidationError, match="Invalid"):
        getattr(client, action)("forged")
    assert not sim.commands
    token = client.preview_action(action, query={"folder": "INBOX"})["token"]
    opposite = "mark_unread" if action == "mark_read" else "mark_read"
    with pytest.raises(MailValidationError, match="action"):
        getattr(client, opposite)(token)
    client.cleanup.tokens.clock = lambda: float("inf")
    with pytest.raises(MailValidationError, match="expired"):
        getattr(client, action)(token)
    assert not mutations(sim)


@pytest.mark.parametrize("use_rules", [False, True])
@pytest.mark.parametrize("mark_read", [False, True])
@pytest.mark.parametrize("copyuid", [False, True])
def test_move_option_preserves_or_sets_seen_and_undo_restores_mixed_state(
    duplicate_mailbox, tmp_path, use_rules, mark_read, copyuid
):
    sim, client = duplicate_mailbox(copyuid=copyuid)
    approved = mixed(sim)
    existing = duplicates(sim, count=1, identical=True, folder=DESTINATION)[0]
    before = [set(item.flags) for item in approved]
    if use_rules:
        rules = rules_service(client, tmp_path)
        preview = rules.preview_rules(mark_read=mark_read)
        execute = rules.apply_rules
    else:
        preview = preview_move(client, mark_read=mark_read)
        execute = client.move_to_folder
    assert preview["mark_read"] is mark_read and preview["count"] == 3
    result = execute(preview["token"])
    assert result["count"] == 3 and not result["partial"]
    assert all(item.folder == DESTINATION for item in approved)
    assert [item.flags for item in approved] == [
        flags | {r"\Seen"} if mark_read else flags for flags in before
    ]
    assert not existing.flags and existing.folder == DESTINATION
    restored = client.undo_action(result["log_ids"][0])
    assert restored["count"] == 3 and not restored["partial"]
    assert [item.flags for item in approved] == before
    assert all(item.folder == "INBOX" for item in approved)
    assert not existing.flags and existing.folder == DESTINATION
    if not mark_read:
        assert all("UID MOVE " in command for _, _, _, command in mutations(sim))
    else:
        assert all(
            "UID MOVE " in command
            or re.fullmatch(r"UID STORE \d+ [+-]FLAGS\.SILENT \(\\Seen\)", command)
            for _, _, _, command in mutations(sim)
        )


@pytest.mark.parametrize("use_rules", [False, True])
@pytest.mark.parametrize("mark_read", [False, True])
def test_move_seen_option_is_not_a_confirmation_argument(
    duplicate_mailbox, tmp_path, use_rules, mark_read
):
    sim, client = duplicate_mailbox()
    mixed(sim)
    if use_rules:
        rules = rules_service(client, tmp_path)
        preview = rules.preview_rules(mark_read=mark_read)
        execute = rules.apply_rules
    else:
        preview = preview_move(client, mark_read=mark_read)
        execute = client.move_to_folder
    with pytest.raises(TypeError, match="mark_read"):
        execute(preview["token"], mark_read=not mark_read)
    assert not mutations(sim)


@pytest.mark.parametrize("use_rules", [False, True])
def test_optional_seen_never_touches_duplicate_arriving_after_preview(
    duplicate_mailbox, tmp_path, use_rules
):
    sim, client = duplicate_mailbox()
    approved = mixed(sim)
    if use_rules:
        rules = rules_service(client, tmp_path)
        preview = rules.preview_rules(mark_read=True)
        execute = rules.apply_rules
    else:
        preview = preview_move(client, mark_read=True)
        execute = client.move_to_folder
    newcomer = duplicates(sim, count=1, identical=True)[0]
    result = execute(preview["token"])
    assert result["count"] == 3 and not result["partial"]
    assert newcomer.folder == "INBOX" and not newcomer.flags
    assert all(item.folder == DESTINATION and r"\Seen" in item.flags for item in approved)


@pytest.mark.parametrize("source", ["Drafts", "Sent", "Archive", "Labels/Review", " INBOX"])
def test_rules_seen_option_keeps_decision_11_source_allowlist(duplicate_mailbox, tmp_path, source):
    sim, client = duplicate_mailbox()
    mixed(sim)
    rules = rules_service(client, tmp_path)
    preview = rules.preview_rules(folder=source, mark_read=True)
    assert preview["token"] is None and preview["blocked"]
    assert not mutations(sim)
    assert not any(command.startswith(("SELECT ", "EXAMINE ")) for _, _, _, command in sim.commands)


@pytest.mark.parametrize("use_rules", [False, True])
def test_optional_seen_reports_read_state_change_after_preview(
    duplicate_mailbox, tmp_path, use_rules
):
    sim, client = duplicate_mailbox()
    approved = mixed(sim)
    if use_rules:
        rules = rules_service(client, tmp_path)
        preview, execute = rules.preview_rules(mark_read=True), rules.apply_rules
    else:
        preview, execute = preview_move(client, mark_read=True), client.move_to_folder
    approved[-1].flags.add(r"\Seen")
    with pytest.raises(MailValidationError, match="[Ss]een|read state|read-state"):
        execute(preview["token"])
    assert not mutations(sim) and all(item.folder == "INBOX" for item in approved)
