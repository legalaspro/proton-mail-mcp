"""Uncertain MOVE recovery requires one matching physical copy, in Trash."""

import json
from pathlib import Path

import pytest
from action_records import execution_records as replay_records
from bridge_sim import Msg, hdr, mutations
from test_cleanup_regressions import act, assert_missing
from test_cleanup_regressions import bridge as bridge

from proton_mail_mcp.action_log import LogTransaction
from proton_mail_mcp.mail import MailError, MailValidationError

CUSTOM_FOLDERS = ("Folders/Reviewed", "Folders/Keep", "Folders/O'Reilly", "Folders/ Review")


class ProcessKilled(BaseException):
    pass


def interrupt_move(client, monkeypatch, *, after_move=False):
    preview = client.preview_action("move_to_trash", query={"folder": "INBOX"})
    assert preview["count"] == 1 and preview["token"] and not preview["blocked"]
    append = LogTransaction.append
    killed = False

    def crash(transaction, record):
        nonlocal killed
        if killed:
            raise ProcessKilled()
        target = "done" if after_move else "uncertain"
        if record["action"] == "execute_progress" and record["message"]["status"] == target:
            if not after_move:
                append(transaction, record)
            killed = True
            raise ProcessKilled()
        return append(transaction, record)

    with monkeypatch.context() as patch:
        patch.setattr(LogTransaction, "append", crash)
        with pytest.raises(ProcessKilled):
            client.move_to_trash(preview["token"])
    path = Path(client.settings.action_log_path)
    record = replay_records([json.loads(line) for line in path.read_text().splitlines()])[0]
    entry = record["messages"][0]
    assert entry["status"] == "uncertain"
    assert entry["uid_after"] is None and entry["uidvalidity_after"] is None
    return record


def assert_manual(result, sim, before, reason):
    assert mutations(sim) == before
    assert result["count"] == 0 and result["partial"]
    assert len(result["missing"]) == 1
    assert result["messages"] == []
    assert "manual handling required" in result["missing"][0]["reason"]
    assert reason in result["missing"][0]["reason"]


@pytest.mark.parametrize(
    "folder", ["Archive", "Folders/Reviewed", "Folders/O'Reilly", "Folders/ Review"]
)
@pytest.mark.parametrize("after_move", [False, True])
def test_uncertain_undo_refuses_preexisting_folder_collision(
    bridge, monkeypatch, folder, after_move
):
    sim, client = bridge(show_all_mail=False, folders=CUSTOM_FOLDERS)
    approved = sim.add(Msg(hdr("<known-copy@example.test>"), body=b"approved body\r\n"))
    collision = sim.add(Msg(approved.headers, folder=folder, body=b"outsider body\r\n"))
    assert approved.body != collision.body and len(approved.body) == len(collision.body)
    record = interrupt_move(client, monkeypatch, after_move=after_move)
    assert len(mutations(sim)) == int(after_move)

    # Independent mailbox activity puts the wrong physical object in Trash.
    approved.folder = "Folders/Keep"
    collision.folder = "Trash"
    before = sim.snapshot()
    moves = mutations(sim)
    result = client.undo_action(record["log_id"])

    assert_manual(result, sim, moves, "found 2 matching physical copies")
    assert sim.snapshot() == before
    assert approved.folder == "Folders/Keep" and collision.folder == "Trash"
    with pytest.raises(MailValidationError, match="unresolved"):
        client.undo_action(record["log_id"])
    assert mutations(sim) == moves


@pytest.mark.parametrize(
    "folder", ["Archive", "Folders/Reviewed", "Folders/O'Reilly", "Folders/ Review"]
)
def test_uncertain_undo_searches_identity_evidence_outside_move_allowlist(
    bridge, monkeypatch, folder
):
    sim, client = bridge(show_all_mail=False, folders=CUSTOM_FOLDERS)
    approved = sim.add(Msg(hdr("<account-wide@example.test>")))
    record = interrupt_move(client, monkeypatch)
    approved.folder = folder
    sim.add(Msg(approved.headers, folder="Trash"))
    before = sim.snapshot()

    result = client.undo_action(record["log_id"])

    assert_manual(result, sim, [], "found 2 matching physical copies")
    assert sim.snapshot() == before


@pytest.mark.parametrize("location", [None, "INBOX", "Archive", "Folders/O'Reilly", "Trash"])
def test_uncertain_undo_refuses_zero_outside_or_multiple_matches(bridge, monkeypatch, location):
    sim, client = bridge(show_all_mail=False, folders=("Folders/O'Reilly",))
    approved = sim.add(Msg(hdr("<physical-count@example.test>")))
    record = interrupt_move(client, monkeypatch)
    approved.folder = location
    if location == "Trash":
        sim.add(Msg(approved.headers, folder="Trash"))
    before = sim.snapshot()

    result = client.undo_action(record["log_id"])

    reason = (
        "found 0 matching physical copies"
        if location is None
        else "found 2 matching physical copies"
        if location == "Trash"
        else "matching physical copy is outside the required Trash destination"
    )
    assert_manual(result, sim, [], reason)
    assert sim.snapshot() == before


@pytest.mark.parametrize("failure", ["missing_fetch", "invalid_identity"])
def test_uncertain_undo_refuses_incomplete_account_inventory(bridge, monkeypatch, failure):
    folder = "Folders/O'Reilly"
    sim, client = bridge(show_all_mail=False, folders=(folder,))
    approved = sim.add(Msg(hdr("<incomplete-account@example.test>")))
    record = interrupt_move(client, monkeypatch, after_move=True)
    collision = sim.add(Msg(approved.headers, folder=folder))
    if failure == "missing_fetch":
        sim.fetch_omit.add((folder, collision.pid))
    else:
        collision.headers = hdr(approved.message_id() + " (added by relay)")
    before = sim.snapshot()
    moves = mutations(sim)

    result = client.undo_action(record["log_id"])

    assert_missing(result, sim, moves)
    assert "manual handling required" in result["missing"][0]["reason"]
    assert result["messages"] == []
    assert sim.snapshot() == before


def test_uncertain_undo_search_failure_moves_nothing_and_preserves_retry(bridge, monkeypatch):
    folder = "Folders/O'Reilly"
    sim, client = bridge(show_all_mail=False, folders=(folder,))
    approved = sim.add(Msg(hdr("<search-failure@example.test>")))
    record = interrupt_move(client, monkeypatch, after_move=True)
    # Empty mailboxes need no enumeration; keep this failure peer nonempty.
    sim.add(Msg(hdr("<unrelated-inventory@example.test>"), folder=folder))

    def fail_search(sid, selected, operation, command):
        if selected == folder and operation == "SEARCH":
            return "NO", "Synthetic inventory failure"
        return None

    sim.hooks.append(fail_search)
    before = sim.snapshot()
    moves = mutations(sim)
    log = Path(client.settings.action_log_path)
    logged = log.read_bytes()

    with pytest.raises(MailError, match="Mailbox operation failed"):
        client.undo_action(record["log_id"])

    assert sim.snapshot() == before and mutations(sim) == moves
    assert log.read_bytes() == logged
    sim.hooks.remove(fail_search)
    result = client.undo_action(record["log_id"])
    assert result["count"] == 1 and not result["partial"]
    assert approved.folder == "INBOX" and len(mutations(sim)) == len(moves) + 1


@pytest.mark.parametrize("view", ["All Mail", "Labels/Review"])
def test_uncertain_undo_refuses_extra_identity_visible_only_in_virtual_view(
    bridge, monkeypatch, view
):
    labels = {view} if view.startswith("Labels/") else ()
    sim, client = bridge(show_all_mail=view == "All Mail")
    approved = sim.add(Msg(hdr("<virtual-ambiguity@example.test>"), labels=labels))
    record = interrupt_move(client, monkeypatch, after_move=True)
    sim.add(Msg(approved.headers, folder=None, labels=labels))
    before = sim.snapshot()
    moves = mutations(sim)

    result = client.undo_action(record["log_id"])

    assert_manual(result, sim, moves, f'ambiguous virtual view during undo in "{view}"')
    assert sim.snapshot() == before


@pytest.mark.parametrize("virtual_memberships", [False, True])
def test_lost_move_outcome_restores_original_with_virtual_memberships(
    bridge, monkeypatch, virtual_memberships
):
    sim, client = bridge(show_all_mail=virtual_memberships)
    approved = sim.add(
        Msg(
            hdr("<lost-move@example.test>"),
            labels={"Labels/Review"} if virtual_memberships else (),
            starred=virtual_memberships,
        )
    )
    original = sim.snapshot()
    record = interrupt_move(client, monkeypatch, after_move=True)
    assert len(mutations(sim)) == 1 and approved.folder == "Trash"
    if virtual_memberships:
        assert set(record["messages"][0]["folders_before"]) == {
            "INBOX",
            "All Mail",
            "Labels/Review",
            "Starred",
        }

    # Exercise read-only enumeration in every view while empty views are now
    # correctly skipped. These unrelated messages remain unchanged by recovery.
    for index, folder in enumerate(sim.mailbox_names()):
        if not sim.members(folder):
            filler = sim.add(
                Msg(
                    hdr(f"<unrelated-recovery-{index}@example.test>"),
                    folder=folder if sim.is_folder(folder) else None,
                    labels={folder} if sim.is_user_label(folder) else (),
                    starred=folder == "Starred",
                )
            )
            original[filler.pid] = sim.snapshot()[filler.pid]

    start = len(sim.commands)
    result = client.undo_action(record["log_id"])

    assert result["count"] == 1 and not result["partial"] and not result["missing"]
    assert len(mutations(sim)) == 2
    assert sim.snapshot() == original
    assert approved.folder == "INBOX"
    searches = [
        (folder, readonly)
        for _, folder, readonly, command in sim.commands[start:]
        if command.startswith("UID SEARCH ")
    ]
    assert {folder for folder, _ in searches} == set(sim.mailbox_names())
    assert all(readonly for _, readonly in searches)


@pytest.mark.parametrize("different", ["size", "header_digest"])
def test_lost_move_ignores_same_id_copy_with_different_logged_identity(
    bridge, monkeypatch, different
):
    sim, client = bridge(show_all_mail=False)
    approved = sim.add(Msg(hdr("<different-identity@example.test>")))
    collision = sim.add(
        Msg(
            hdr(approved.message_id(), subject="Other")
            if different == "header_digest"
            else approved.headers,
            folder="Archive",
            body=b"Different length body\r\n" if different == "size" else approved.body,
        )
    )
    original = sim.snapshot()
    record = interrupt_move(client, monkeypatch, after_move=True)
    assert approved.folder == "Trash" and len(mutations(sim)) == 1

    result = client.undo_action(record["log_id"])

    assert result["count"] == 1 and not result["partial"] and not result["missing"]
    assert len(mutations(sim)) == 2 and sim.snapshot() == original
    assert approved.folder == "INBOX" and collision.folder == "Archive"


def test_unnarrowed_query_refuses_duplicate_before_approval(bridge):
    sim, client = bridge()
    approved = sim.add(Msg(hdr("<visible-copy@example.test>"), body=b"approved body\r\n"))
    sim.add(Msg(approved.headers, folder="Archive", body=b"outsider body\r\n"))
    original = sim.snapshot()

    result = client.preview_action("move_to_trash", query={})
    assert result["token"] is None and result["excluded_ids"] == [approved.message_id()]
    assert result["excluded_reasons"] == {"ambiguous_membership": 1}

    assert not mutations(sim)
    assert sim.snapshot() == original


@pytest.mark.parametrize("folder", ["Archive", "Folders/O'Reilly", "Folders/ Review"])
def test_pinned_destination_restores_original_despite_known_real_folder_copy(bridge, folder):
    sim, client = bridge(show_all_mail=False, folders=CUSTOM_FOLDERS)
    approved = sim.add(Msg(hdr("<pinned-copy@example.test>"), body=b"approved body\r\n"))
    collision = sim.add(Msg(approved.headers, folder=folder, body=b"outsider body\r\n"))
    completed = act(client)
    entry = completed["messages"][0]
    assert entry["uid_after"] == str(sim.uid_of("Trash", approved.pid))
    assert entry["uidvalidity_after"] == str(sim.uidvalidity["Trash"])
    collision.folder = "Trash"
    sim.sync()
    assert str(sim.uid_of("Trash", collision.pid)) != entry["uid_after"]
    before = mutations(sim)

    result = client.undo_action(completed["log_ids"][0])

    assert result["count"] == 1 and not result["partial"] and not result["missing"]
    assert len(mutations(sim)) == len(before) + 1
    assert approved.folder == "INBOX" and collision.folder == "Trash"
