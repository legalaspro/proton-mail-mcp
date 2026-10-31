"""Bulk pack (P-D): batched UID STORE/MOVE, folder creation in one approval, bounded runs.

Every peer is a local socketpair Bridge model driven by the real imaplib client,
with a deterministic command clock; nothing opens a network connection.
"""

import copy
import hashlib
import json
import math
import re
import socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from action_records import execution_log
from action_records import execution_records as replay_records
from bridge_sim import Msg, hdr, mutations
from slow_cleanup import SlowCleanupPeer
from test_cleanup_mutations import uid_values

from proton_mail_mcp import guarded, mail
from proton_mail_mcp.cleanup_imap import _copyuid_map, _uid_set
from proton_mail_mcp.cleanup_undo import remaining_undo
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.guarded import TokenStore, approval_hash
from proton_mail_mcp.mail import MailError, MailValidationError, _decode_folder
from proton_mail_mcp.run_journal import replay_runs, run_event

TARGET = "Folders/Reviewed"
QUERY = {"folder": "INBOX"}


class BulkPeer(SlowCleanupPeer):
    """The slow cleanup peer, plus CREATE of an approved Folders/ mailbox."""

    def __init__(self, **options):
        super().__init__(**options)
        self.created = []

    def handle(self, tag, op, uid_op, words, session, command):
        if op == "CREATE":
            name = _decode_folder(words[1].encode("ascii"))
            assert name.startswith("Folders/") and name not in self.mailbox_names()
            with self.lock:
                self.custom_folders.append(name)
                self.sync()
            self.created.append(name)
            return tag + b" OK created\r\n"
        return super().handle(tag, op, uid_op, words, session, command)


@pytest.fixture
def bulk(tmp_path, monkeypatch):
    peers = []

    def forbid_network(*args, **kwargs):
        pytest.fail("Bulk pack tests use local socket pairs only")

    monkeypatch.setattr(socket, "create_connection", forbid_network)

    def build(count, *, mutation_seconds=0.0, timeout=120, bound=None):
        peer = BulkPeer(mutation_seconds=mutation_seconds)
        peers.append(peer)
        monkeypatch.setattr(mail, "time", peer.clock)
        items = peer.populate(count)
        client = peer.client(tmp_path / f"case-{len(peers)}", timeout=timeout)
        if bound is not None:
            client.settings = replace(client.settings, approval_max_messages=bound)
        client.cleanup.tokens.clock = peer.clock.monotonic
        return SimpleNamespace(
            peer=peer,
            client=client,
            service=client.cleanup,
            items=items,
            log=Path(client.settings.action_log_path),
        )

    yield build
    for peer in peers:
        peer.close()
        assert not peer.errors and not peer.violations


def preview_move(case, *, params=None, **options):
    return case.client.preview_action(
        "move_to_folder", query=QUERY, params=params or {"folder": TARGET}, **options
    )


def commands(peer, operation):
    """(folder, UID set) of each mutation command of one kind, in order."""
    found = []
    for _, folder, _, command in mutations(peer):
        words = command.split()
        if words[1] == operation:
            found.append((folder, words[2]))
    return found


def journal(case, action="move_to_folder"):
    raw = [json.loads(line) for line in case.log.read_text().splitlines()]
    return [record for record in replay_records(raw) if record.get("action") == action]


def finish(case, token, action="move_to_folder", **options):
    results = []
    while token:
        results.append(getattr(case.client, action)(token, **options))
        token = results[-1]["continuation_token"]
        assert len(results) <= 10
    return results


def approved_uids(case, token, action="move_to_folder"):
    return {row["uid"] for row in case.service.tokens.inspect(token, action).messages}


@pytest.mark.parametrize("count", [199, 200, 201, 450])
def test_moves_send_one_uid_move_per_chunk_of_at_most_200(bulk, count):
    case = bulk(count)
    preview = preview_move(case, **({"limit": count} if count > 200 else {}))
    assert preview["token"] and preview["count"] == count
    assert preview["approval_max_messages"] == 500
    approved = approved_uids(case, preview["token"])
    grant = case.service.tokens.inspect(preview["token"], "move_to_folder")
    assert ("max_messages" in grant.selector) is (count > 200)
    results = finish(case, preview["token"])
    assert len(results) == 1 and results[0]["done"] == count and not results[0]["partial"]
    moves = commands(case.peer, "MOVE")
    assert len(moves) == math.ceil(count / 200)
    named = [uid for _, spec in moves for uid in uid_values(spec)]
    assert [len(uid_values(spec)) for _, spec in moves] == [
        min(200, count - offset) for offset in range(0, count, 200)
    ]
    assert len(named) == len(set(named)) == count and set(named) == approved
    # Consecutive approved UIDs are named as one compact range per command.
    assert all(re.fullmatch(r"[1-9][0-9]*(?::[1-9][0-9]*)?", spec) for _, spec in moves)
    chunks = journal(case)
    assert [
        sorted((row["uid_before"] for row in chunk["messages"]), key=int) for chunk in chunks
    ] == [sorted(uid_values(spec), key=int) for _, spec in moves]
    destination = case.peer.uidmap[TARGET]
    for chunk in chunks:
        for row in chunk["messages"]:
            assert row["status"] == "done" and "verification" not in row
            moved = case.peer.messages[destination[int(row["uid_after"])]]
            assert moved.message_id() == row["message_id"]
            assert row["uidvalidity_after"] == str(case.peer.uidvalidity[TARGET])
    assert all(item.folder == TARGET for item in case.items)
    run = case.client.list_actions(token=preview["token"])["runs"][0]
    assert run["status"] == "completed" and run["approved_total"] == run["done"] == count
    assert len(run["log_ids"]) == len(moves)


@pytest.mark.parametrize("action", ["mark_read", "mark_unread"])
def test_read_state_changes_send_one_uid_store_per_chunk(bulk, action):
    case = bulk(201)
    if action == "mark_unread":
        for item in case.items:
            item.flags.add(r"\Seen")
    preview = case.client.preview_action(action, query=QUERY, limit=201)
    assert preview["token"] and preview["count"] == 201
    results = finish(case, preview["token"], action)
    assert sum(result["done"] for result in results) == 201
    stores = commands(case.peer, "STORE")
    assert [len(uid_values(spec)) for _, spec in stores] == [200, 1]
    assert not commands(case.peer, "MOVE")
    assert all((r"\Seen" in item.flags) is (action == "mark_read") for item in case.items)
    rows = [row for chunk in journal(case, action) for row in chunk["messages"]]
    assert len(rows) == 201 and all(row["status"] == "done" for row in rows)
    assert all(row["uid_after"] == row["uid_before"] for row in rows)


@pytest.mark.parametrize("count", [30, 200])
def test_slow_batch_completes_in_one_call_without_a_headroom_split(bulk, count):
    # G-B: every STORE and MOVE command takes 1.5 s; the default 120 s call budget
    # gives the 48 s execution ceiling. The previous two-second per-message
    # allowance fitted at most 21 messages in that window, so 30 needed two calls.
    assert math.floor((48 - 4.8 - 1) / guarded.EXECUTE_MUTATION_SECONDS_PER_MESSAGE) < count
    case = bulk(count, mutation_seconds=1.5)
    preview = preview_move(case, params={"folder": TARGET, "mark_read": True})
    assert preview["token"] and preview["estimated_calls"] == 1
    started = case.peer.clock.elapsed
    result = case.client.move_to_folder(preview["token"])
    assert case.peer.clock.elapsed - started < 48
    assert result["done"] == count and result["remaining"] == result["uncertain"] == 0
    assert not result["partial"] and not result["resume"] and result["stop_reason"] is None
    assert "headroom" not in json.dumps(result)
    assert len(commands(case.peer, "STORE")) == len(commands(case.peer, "MOVE")) == 1
    assert all(item.folder == TARGET and r"\Seen" in item.flags for item in case.items)


def test_uid_set_ranges_and_copyuid_mapping_are_exact():
    assert _uid_set(["7"]) == "7"
    assert _uid_set(["9", "7", "8", "12", "14", "13", "20"]) == "7:9,12:14,20"
    for bad in ([], ["0"], ["*"], ["1:3"], ["07a"]):
        with pytest.raises(MailValidationError):
            _uid_set(bad)

    class Responses:
        def __init__(self, extra=()):
            self.extra = list(extra)

        def response(self, name):
            assert name == "COPYUID"
            return name, self.extra

    uids = ["7", "8", "9", "12"]
    tagged = [b"[COPYUID 55 7:9,12 101:104] done"]
    assert _copyuid_map(Responses(), tagged, uids) == {
        "7": ("55", "101"),
        "8": ("55", "102"),
        "9": ("55", "103"),
        "12": ("55", "104"),
    }
    # Untagged OK responses are honoured; listed order maps one to one.
    assert _copyuid_map(Responses([b"56 12,7 201,202"]), [b"moved"], uids) == {
        "12": ("56", "201"),
        "7": ("56", "202"),
    }
    for junk in (
        b"[COPYUID 55 7:9 101:102] done",  # Unequal set sizes.
        b"[COPYUID 55 7,99 101,102] done",  # A UID outside this command.
        b"[COPYUID 55 7,7 101,102] done",  # Duplicate source.
        b"[COPYUID 55 7,8 101,101] done",  # Two sources, one destination.
        b"[COPYUID 55 7:* 101:104] done",  # Never "*".
        b"[COPYUID 55 1:4294967296 1:4294967296] done",  # Excess range.
    ):
        assert _copyuid_map(Responses(), [junk], uids) == {}
    # Conflicting codes leave the conflicting UID unmapped.
    conflict = [b"[COPYUID 55 7,8 101,102] done", b"[COPYUID 55 7 109] done"]
    assert _copyuid_map(Responses(), conflict, uids) == {"8": ("55", "102")}


def drop_uid_from(peer, monkeypatch, operation, uid):
    """Make the peer acknowledge one batched command without applying it to uid."""
    original = peer.handle

    def handle(tag, op, uid_op, words, session, command):
        if op == operation:
            kept = [value for value in uid_values(words[2]) if value != uid]
            words = [*words[:2], ",".join(kept), *words[3:]]
        return original(tag, op, uid_op, words, session, command)

    monkeypatch.setattr(peer, "handle", handle)


def test_uid_left_in_source_after_move_is_reported_uncertain(bulk, monkeypatch):
    case = bulk(5)
    preview = preview_move(case)
    skipped = case.items[2]
    skipped_uid = str(case.peer.uid_of("INBOX", skipped.pid))
    drop_uid_from(case.peer, monkeypatch, "MOVE", skipped_uid)
    result = case.client.move_to_folder(preview["token"])
    assert result["done"] == 4 and result["uncertain"] == 1 and result["remaining"] == 0
    assert result["partial"] and not result["resume"] and result["continuation_token"] is None
    assert [(row["uid"], row["status"]) for row in result["mismatched"]] == [
        (skipped_uid, "uncertain")
    ]
    assert "remained in its source folder" in result["mismatched"][0]["reason"]
    assert "verification" in result["error"]
    assert skipped.folder == "INBOX"
    assert all(item.folder == TARGET for item in case.items if item is not skipped)
    rows = {row["uid_before"]: row for row in journal(case)[0]["messages"]}
    assert rows[skipped_uid]["status"] == "uncertain" and rows[skipped_uid]["uid_after"] is None
    assert "remained in its source folder" in rows[skipped_uid]["verification"]
    assert sum(row["status"] == "done" for row in rows.values()) == 4
    run = case.client.list_actions(token=preview["token"])["runs"][0]
    assert run["status"] == "partial" and run["done"] == 4 and run["uncertain"] == 1
    compact = case.client.list_actions(token=preview["token"])["actions"][0]
    assert compact["uncertain"] == 1 and compact["done"] == 4


@pytest.mark.parametrize("action", ["mark_read", "move_to_folder"])
def test_unverified_seen_store_is_reported_and_never_moved(bulk, monkeypatch, action):
    case = bulk(5)
    if action == "mark_read":
        preview = case.client.preview_action("mark_read", query=QUERY)
    else:
        preview = preview_move(case, params={"folder": TARGET, "mark_read": True})
    skipped = case.items[1]
    skipped_uid = str(case.peer.uid_of("INBOX", skipped.pid))
    drop_uid_from(case.peer, monkeypatch, "STORE", skipped_uid)
    result = getattr(case.client, action)(preview["token"])
    assert result["done"] == 4 and result["uncertain"] == 1 and result["partial"]
    assert [row["uid"] for row in result["mismatched"]] == [skipped_uid]
    assert "Seen state could not be verified" in result["mismatched"][0]["reason"]
    assert skipped.folder == "INBOX" and r"\Seen" not in skipped.flags
    moved = commands(case.peer, "MOVE")
    assert skipped_uid not in {uid for _, spec in moved for uid in uid_values(spec)}
    assert len(moved) == (1 if action == "move_to_folder" else 0)
    entry = next(
        row for row in journal(case, action)[0]["messages"] if row["uid_before"] == skipped_uid
    )
    assert entry["status"] == "uncertain" and entry["stage"] == "seen"


def claim_kept_uid_moved(peer, monkeypatch, kept_uid):
    """Make each MOVE skip kept_uid, while its COPYUID still names it as moved."""
    original = peer.handle

    def handle(tag, op, uid_op, words, session, command):
        if op != "MOVE":
            return original(tag, op, uid_op, words, session, command)
        moved = ",".join(value for value in uid_values(words[2]) if value != kept_uid)
        response = original(tag, op, uid_op, [*words[:2], moved, *words[3:]], session, command)

        def add_kept(match):
            # COPYUID also names the untouched UID, with an unused destination UID.
            return b"COPYUID %s %s,%s %s,99999" % (match[1], match[2], kept_uid.encode(), match[3])

        return re.sub(rb"COPYUID (\d+) (\S+) ([0-9:,]+)", add_kept, response)

    monkeypatch.setattr(peer, "handle", handle)


def test_copyuid_naming_a_uid_left_in_its_source_is_not_trusted(bulk, monkeypatch):
    case = bulk(4)
    preview = preview_move(case)
    kept = case.items[1]
    kept_uid = str(case.peer.uid_of("INBOX", kept.pid))
    claim_kept_uid_moved(case.peer, monkeypatch, kept_uid)
    result = case.client.move_to_folder(preview["token"])
    assert result["done"] == 3 and result["uncertain"] == 1 and result["remaining"] == 0
    assert result["partial"] and result["continuation_token"] is None
    assert [(row["uid"], row["status"]) for row in result["mismatched"]] == [
        (kept_uid, "uncertain")
    ]
    assert "remained in its source folder" in result["mismatched"][0]["reason"]
    assert kept.folder == "INBOX"
    assert all(item.folder == TARGET for item in case.items if item is not kept)
    rows = {row["uid_before"]: row for row in journal(case)[0]["messages"]}
    assert rows[kept_uid]["status"] == "uncertain" and rows[kept_uid]["uid_after"] is None
    assert all(
        row["status"] == "done" and row["uid_after"] for uid, row in rows.items() if uid != kept_uid
    )


def test_failed_source_check_after_move_counts_no_uid_done(bulk, monkeypatch):
    # COPYUID names all three UIDs, one of them untouched, and then the source
    # read that settles each outcome fails: COPYUID alone counts nothing as done,
    # and every UID is reported with the failed check.
    case = bulk(3)
    preview = preview_move(case)
    approved = approved_uids(case, preview["token"])
    kept_uid = str(case.peer.uid_of("INBOX", case.items[1].pid))
    claim_kept_uid_moved(case.peer, monkeypatch, kept_uid)
    moved = []

    def refuse_source_read(sid, folder, operation, command):
        if operation == "MOVE":
            moved.append(folder)
        elif operation == "FETCH" and moved:
            return ("NO", "busy")

    case.peer.hooks.append(refuse_source_read)
    result = case.client.move_to_folder(preview["token"])
    assert moved == ["INBOX"]
    assert result["done"] == 0 and result["uncertain"] == 3 and result["remaining"] == 0
    assert result["partial"] and not result["resume"] and result["continuation_token"] is None
    assert "move verification" in result["stop_reason"]
    assert {row["uid"] for row in result["mismatched"]} == approved
    assert {row["status"] for row in result["mismatched"]} == {"uncertain"}
    assert all(
        row["reason"].startswith("Source folder could not be verified after MOVE")
        for row in result["mismatched"]
    )
    assert [item.folder for item in case.items] == [TARGET, "INBOX", TARGET]
    rows = journal(case)[0]["messages"]
    assert all(row["status"] == "uncertain" and row["uid_after"] is None for row in rows)
    assert all(row["verification"].startswith("Source folder could not") for row in rows)
    run = case.client.list_actions(token=preview["token"])["runs"][0]
    assert run["status"] == "partial" and run["done"] == 0 and run["uncertain"] == 3


@pytest.mark.parametrize("copyuid", [True, False], ids=["copyuid", "no_copyuid"])
def test_uid_named_by_an_incomplete_source_read_is_not_done_by_an_identical_copy(
    bulk, monkeypatch, copyuid
):
    # MOVE skips UID 2 while another client files an identical copy of it in the
    # destination. The source read after MOVE names UID 2 without any other item,
    # FLAGS included: UID 2 is still in its source, so the new copy, though it
    # matches exactly, must not count it as done.
    case = bulk(3)
    case.peer.copyuid = copyuid
    preview = preview_move(case)
    assert approved_uids(case, preview["token"]) == {"1", "2", "3"}
    kept = case.items[1]
    kept_uid = str(case.peer.uid_of("INBOX", kept.pid))
    assert kept_uid == "2"
    drop_uid_from(case.peer, monkeypatch, "MOVE", kept_uid)
    original = case.peer.handle
    copies, replies = [], []

    def handle(tag, op, uid_op, words, session, command):
        if op == "MOVE":
            copies.append(case.peer.add(Msg(kept.headers, folder=TARGET, body=kept.body)))
        response = original(tag, op, uid_op, words, session, command)
        if op == "FETCH" and session.selected == "INBOX" and copies and not replies:
            # A successful reply whose records carry each UID and nothing else.
            response = re.sub(rb"\(UID (\d+) [^\r\n]*\)\r\n", rb"(UID \1)\r\n", response)
            replies.append(response)
        return response

    monkeypatch.setattr(case.peer, "handle", handle)
    result = case.client.move_to_folder(preview["token"])
    assert len(copies) == 1 and len(replies) == 1
    assert re.findall(rb"\* \d+ FETCH \(([^)]*)\)", replies[0]) == [b"UID 2"]
    assert [item.folder for item in case.items] == [TARGET, "INBOX", TARGET]
    assert copies[0].folder == TARGET
    assert result["done"] == 2 and result["uncertain"] == 1 and result["remaining"] == 0
    assert result["partial"] and not result["resume"] and result["continuation_token"] is None
    assert [(row["uid"], row["status"]) for row in result["mismatched"]] == [
        (kept_uid, "uncertain")
    ]
    reason = result["mismatched"][0]["reason"]
    assert "remained in its source folder" in reason
    rows = {row["uid_before"]: row for row in journal(case)[0]["messages"]}
    assert rows[kept_uid]["status"] == "uncertain" and rows[kept_uid]["uid_after"] is None
    assert rows[kept_uid]["verification"] == reason
    destination = case.peer.uidmap[TARGET]
    for uid, row in rows.items():
        if uid != kept_uid:
            assert row["status"] == "done" and "verification" not in row
            moved = case.peer.messages[destination[int(row["uid_after"])]]
            assert moved.message_id() == row["message_id"]
    run = case.client.list_actions(token=preview["token"])["runs"][0]
    assert run["status"] == "partial" and run["done"] == 2 and run["uncertain"] == 1


def depart_at_move(case, item, folder):
    """Another client moves item to folder just before the server applies UID MOVE."""

    def depart(sid, selected, operation, command):
        if operation == "MOVE" and item.folder == "INBOX":
            item.folder = folder

    case.peer.arrival_hook = depart


@pytest.mark.parametrize("copyuid", [True, False], ids=["copyuid", "no_copyuid"])
@pytest.mark.parametrize("elsewhere", ["Archive", TARGET])
def test_message_moved_away_at_move_time_is_done_only_with_its_destination_copy(
    bulk, copyuid, elsewhere
):
    # The server skips the vanished UID and omits it from COPYUID, so it is absent
    # from its source like the moved ones. Only a copy in the approved destination
    # can count it as done; moved to Archive instead, it stays uncertain.
    case = bulk(3)
    case.peer.copyuid = copyuid
    preview = preview_move(case)
    middle = case.items[1]
    middle_uid = str(case.peer.uid_of("INBOX", middle.pid))
    depart_at_move(case, middle, elsewhere)
    result = case.client.move_to_folder(preview["token"])
    moves = commands(case.peer, "MOVE")
    assert len(moves) == 1 and middle_uid in uid_values(moves[0][1])
    assert [item.folder for item in case.items] == [TARGET, elsewhere, TARGET]
    rows = {row["uid_before"]: row for row in journal(case)[0]["messages"]}
    destination = case.peer.uidmap[TARGET]
    pinned = rows if elsewhere == TARGET else {u: r for u, r in rows.items() if u != middle_uid}
    for row in pinned.values():
        assert row["status"] == "done" and "verification" not in row
        moved = case.peer.messages[destination[int(row["uid_after"])]]
        assert moved.message_id() == row["message_id"]
    if elsewhere == TARGET:
        assert result["done"] == 3 and not result["partial"] and not result["mismatched"]
        return
    assert result["done"] == 2 and result["uncertain"] == 1 and result["remaining"] == 0
    assert result["partial"] and not result["resume"] and result["continuation_token"] is None
    assert [(row["uid"], row["status"]) for row in result["mismatched"]] == [
        (middle_uid, "uncertain")
    ]
    reason = result["mismatched"][0]["reason"]
    assert "left its source folder" in reason and json.dumps(TARGET) in reason
    assert rows[middle_uid]["status"] == "uncertain" and rows[middle_uid]["uid_after"] is None
    assert rows[middle_uid]["verification"] == reason
    run = case.client.list_actions(token=preview["token"])["runs"][0]
    assert run["status"] == "partial" and run["done"] == 2 and run["uncertain"] == 1


def test_failed_destination_read_keeps_only_pinned_copies_done(bulk):
    # COPYUID pins the two messages that moved. The read that would pin the third,
    # moved elsewhere at MOVE time and so absent from COPYUID, is refused: the
    # pinned copies remain exact evidence, the third stays uncertain with a reason.
    case = bulk(3)
    preview = preview_move(case)
    middle = case.items[1]
    middle_uid = str(case.peer.uid_of("INBOX", middle.pid))
    depart_at_move(case, middle, "Archive")
    reads = []

    def refuse_pin_read(sid, folder, operation, command):
        if operation == "MOVE":
            reads.append(command)
        elif reads and operation == "FETCH" and folder == TARGET:
            # The first read confirms the COPYUID handles; the next would pin the rest.
            reads.append(command)
            if len(reads) == 3:
                return ("NO", "busy")

    case.peer.hooks.append(refuse_pin_read)
    result = case.client.move_to_folder(preview["token"])
    assert len(reads) == 3
    assert result["done"] == 2 and result["uncertain"] == 1 and result["partial"]
    assert "move verification" in result["stop_reason"]
    assert [(row["uid"], row["status"]) for row in result["mismatched"]] == [
        (middle_uid, "uncertain")
    ]
    assert "left its source folder" in result["mismatched"][0]["reason"]
    rows = {row["uid_before"]: row for row in journal(case)[0]["messages"]}
    destination = case.peer.uidmap[TARGET]
    for uid, row in rows.items():
        if uid == middle_uid:
            assert row["status"] == "uncertain" and row["uid_after"] is None
            continue
        assert row["status"] == "done" and "verification" not in row
        moved = case.peer.messages[destination[int(row["uid_after"])]]
        assert moved.message_id() == row["message_id"]


def hide_uidnext(peer, monkeypatch, mailbox):
    """Omit UIDNEXT when mailbox is selected, so no destination append boundary exists."""
    original = peer.handle

    def handle(tag, op, uid_op, words, session, command):
        response = original(tag, op, uid_op, words, session, command)
        if op in {"SELECT", "EXAMINE"} and session.selected == mailbox:
            response = re.sub(rb"\* OK \[UIDNEXT \d+\] next\r\n", b"", response)
        return response

    monkeypatch.setattr(peer, "handle", handle)


def test_without_copyuid_or_append_boundary_each_copy_is_pinned_by_identity(bulk, monkeypatch):
    # One identity inventory of the destination pins every moved copy; the message
    # another client moved to Trash meanwhile has no copy in Archive.
    case = bulk(3)
    case.peer.copyuid = False
    hide_uidnext(case.peer, monkeypatch, "Archive")
    preview = case.client.preview_action("archive", query=QUERY)
    assert not case.service.tokens.inspect(preview["token"], "archive").bounded_recovery
    middle = case.items[1]
    middle_uid = str(case.peer.uid_of("INBOX", middle.pid))
    depart_at_move(case, middle, "Trash")
    result = case.client.archive(preview["token"])
    assert [item.folder for item in case.items] == ["Archive", "Trash", "Archive"]
    assert result["done"] == 2 and result["uncertain"] == 1 and result["partial"]
    assert [(row["uid"], row["status"]) for row in result["mismatched"]] == [
        (middle_uid, "uncertain")
    ]
    assert "left its source folder" in result["mismatched"][0]["reason"]
    rows = {row["uid_before"]: row for row in journal(case, "archive")[0]["messages"]}
    # Plain archive rows record no destination identity: none existed before.
    assert not any("destination_uidvalidity" in row for row in rows.values())
    destination, validity = case.peer.uidmap["Archive"], str(case.peer.uidvalidity["Archive"])
    for uid, row in rows.items():
        if uid == middle_uid:
            assert row["status"] == "uncertain" and row["uid_after"] is None
            continue
        assert row["status"] == "done" and row["uidvalidity_after"] == validity
        moved = case.peer.messages[destination[int(row["uid_after"])]]
        assert moved.message_id() == row["message_id"]


def test_row_changed_before_the_batch_command_stays_untouched_and_is_blocked_next(
    bulk, monkeypatch
):
    case = bulk(5)
    preview = preview_move(case)
    changed = case.items[3]
    changed_uid = str(case.peer.uid_of("INBOX", changed.pid))
    edits = []

    def change_at_writable_select(sid, folder, operation, command):
        # After preflight, at the writable SELECT preceding the final FETCH.
        if operation == "SELECT" and not edits:
            changed.headers = changed.headers.replace(b"Subject: Hello", b"Subject: Edited")
            edits.append(True)

    case.peer.hooks.append(change_at_writable_select)
    first = case.client.move_to_folder(preview["token"])
    assert edits and first["done"] == 4 and first["uncertain"] == 0 and first["remaining"] == 1
    assert [(row["uid"], row["status"]) for row in first["mismatched"]] == [
        (changed_uid, "not_attempted")
    ]
    assert "fingerprint changed" in first["mismatched"][0]["reason"]
    named = {uid for _, spec in commands(case.peer, "MOVE") for uid in uid_values(spec)}
    assert changed_uid not in named and len(named) == 4
    assert first["resume"] and first["continuation_token"]
    before = mutations(case.peer)
    second = case.client.move_to_folder(first["continuation_token"])
    assert second["done"] == 0 and [row["uid"] for row in second["blocked"]] == [changed_uid]
    assert mutations(case.peer) == before and changed.folder == "INBOX"


def test_wrong_copyuid_mapping_is_rejected_and_destination_pinned_by_identity(bulk, monkeypatch):
    case = bulk(4)
    preview = preview_move(case)
    original = case.peer.handle

    def reversed_copyuid(tag, op, uid_op, words, session, command):
        response = original(tag, op, uid_op, words, session, command)
        if op != "MOVE":
            return response

        def swap(match):
            destinations = match[3].decode().split(",")
            return (
                b"COPYUID "
                + match[1]
                + b" "
                + match[2]
                + b" "
                + ",".join(reversed(destinations)).encode()
            )

        return re.sub(rb"COPYUID (\d+) (\S+) ([0-9,]+)", swap, response)

    monkeypatch.setattr(case.peer, "handle", reversed_copyuid)
    result = case.client.move_to_folder(preview["token"])
    assert result["done"] == 4 and not result["partial"]
    destination = case.peer.uidmap[TARGET]
    for row in journal(case)[0]["messages"]:
        # The swapped mapping fails the envelope check; the append boundary pins
        # each moved message to its own new destination UID instead.
        assert (
            case.peer.messages[destination[int(row["uid_after"])]].message_id()
            == (row["message_id"])
        )


def test_missing_copyuid_on_trash_pins_each_copy_before_counting_it_done(bulk):
    case = bulk(3)
    case.peer.copyuid = False
    preview = case.client.preview_action("move_to_trash", query=QUERY)
    result = case.client.move_to_trash(preview["token"])
    assert result["done"] == 3 and not result["partial"]
    destination = case.peer.uidmap["Trash"]
    for row in journal(case, "move_to_trash")[0]["messages"]:
        # Leaving INBOX is not enough: the append boundary pins each new copy.
        assert row["status"] == "done"
        moved = case.peer.messages[destination[int(row["uid_after"])]]
        assert moved.message_id() == row["message_id"]
    assert all(item.folder == "Trash" for item in case.items)


NEW = "Folders/New Arrivals"
CREATE = {"folder": NEW, "create_folder_if_missing": True}


def test_move_preview_can_include_creating_its_missing_folder(bulk):
    case = bulk(3)
    with pytest.raises(MailValidationError, match="does not exist"):
        preview_move(case, params={"folder": NEW})
    preview = preview_move(case, params=CREATE)
    assert preview["folder_creation"] == {"name": NEW, "exists": False, "create": True}
    assert preview["token"] and preview["target"] == NEW and preview["count"] == 3
    # The preview creates nothing.
    assert not case.peer.created and NEW not in case.peer.mailbox_names()
    assert not any(command.startswith("CREATE") for *_, command in case.peer.commands)
    grant = case.service.tokens.inspect(preview["token"], "move_to_folder")
    assert grant.params == {"folder": NEW, "mark_read": False, "create_folder_if_missing": True}
    assert preview["action_hash"] == approval_hash(
        "move_to_folder", grant.params, grant.selector, grant.messages
    )
    # The one token binds the folder creation as well as the move set.
    plain = {"folder": NEW, "mark_read": False}
    assert preview["action_hash"] != approval_hash(
        "move_to_folder", plain, grant.selector, grant.messages
    )
    for params in (
        {**CREATE, "create_folder_if_missing": "yes"},
        {**CREATE, "folder": "Folders/a*b"},
        {**CREATE, "folder": "Labels/News"},
    ):
        with pytest.raises(MailValidationError):
            preview_move(case, params=params)
    with pytest.raises(MailValidationError):
        case.client.preview_action(
            "archive", query=QUERY, params={"create_folder_if_missing": True}
        )


def test_confirm_creates_the_folder_then_moves_with_journal_first(bulk):
    case = bulk(3)
    preview = preview_move(case, params=CREATE)
    order = []

    def watch(sid, folder, operation, command):
        if operation in {"CREATE", "MOVE"}:
            raw = [json.loads(line) for line in case.log.read_text().splitlines()]
            order.append((operation, raw))
            if operation == "CREATE":
                # The approval is claimed and the intent durable before CREATE.
                with pytest.raises(MailValidationError, match="already used"):
                    case.service.tokens.inspect(preview["token"], "move_to_folder")

    case.peer.hooks.append(watch)
    result = case.client.move_to_folder(preview["token"])
    assert [operation for operation, _ in order] == ["CREATE", "MOVE"]
    before_create = order[0][1]
    assert [row["action"] for row in before_create[-3:]] == [
        "create_folder",
        "run_start",
        "execute_progress",
    ]
    assert before_create[-1]["message"] == {
        "folder": NEW,
        "status": "uncertain",
        "result": "uncertain",
    }
    assert (
        before_create[-3]["token_digest"]
        == hashlib.sha256(preview["token"].encode("ascii")).hexdigest()
    )
    # The creation names its approval's run as its parent, never as one of its chunks.
    assert before_create[-3]["parent_run_id"] == before_create[-2]["run_id"] == result["run_id"]
    assert "run_id" not in before_create[-3]
    assert result["done"] == 3 and not result["partial"] and result["token_consumed"]
    assert result["folder_creation"]["created"] is True
    assert result["folder_creation"]["exists"] is False
    assert case.peer.created == [NEW] and all(item.folder == NEW for item in case.items)
    created = journal(case, "create_folder")
    assert len(created) == 1 and created[0]["status"] == "completed"
    assert created[0]["messages"][0]["result"] == "created"
    assert created[0]["log_id"] == result["folder_creation"]["log_id"]
    lookup = case.client.list_actions(token=preview["token"])
    assert {action["action"] for action in replay_records(lookup["actions"])} == {
        "create_folder",
        "move_to_folder",
    }
    assert lookup["runs"][0]["status"] == "completed" and lookup["runs"][0]["done"] == 3
    assert lookup["runs"][0]["log_ids"] == result["log_ids"]
    with pytest.raises(MailValidationError, match="already used"):
        case.client.move_to_folder(preview["token"])
    with pytest.raises(MailValidationError, match="cannot be undone"):
        case.client.undo_action(created[0]["log_id"])
    # Undo returns the messages to INBOX; the created folder stays (manual removal).
    undo = case.client.undo_action(result["log_ids"][0])
    assert undo["count"] == 3 and all(item.folder == "INBOX" for item in case.items)
    assert NEW in case.peer.mailbox_names() and case.peer.created == [NEW]


def test_tampered_or_unconfirmed_creation_token_creates_nothing(bulk):
    case = bulk(2)
    preview = preview_move(case, params=CREATE)
    tokens = case.service.tokens
    digest = hashlib.sha256(preview["token"].encode("ascii")).digest()
    grant = tokens._grants[digest]
    for change in (
        {"folder": "Folders/Elsewhere"},
        {"create_folder_if_missing": False},
        {"mark_read": True},
    ):
        original = copy.deepcopy(grant.params)
        grant.params.update(change)
        with pytest.raises(MailValidationError, match="approved action set"):
            case.client.move_to_folder(preview["token"])
        grant.params.clear()
        grant.params.update(original)
    del grant.params["create_folder_if_missing"]
    with pytest.raises(MailValidationError, match="approved action set"):
        case.client.move_to_folder(preview["token"])
    grant.params["create_folder_if_missing"] = True
    with pytest.raises(MailValidationError, match="requested action"):
        case.client.create_folder(preview["token"])
    tokens.confirmation_required = True
    unconfirmed = preview_move(case, params=CREATE)["token"]
    with pytest.raises(MailValidationError, match="needs the user's confirmation"):
        case.client.move_to_folder(unconfirmed)
    assert not case.peer.created and not mutations(case.peer)
    assert NEW not in case.peer.mailbox_names() and not execution_log(case.log)


def test_move_token_without_creation_never_creates_a_vanished_folder(bulk):
    case = bulk(2)
    case.peer.custom_folders.append(NEW)
    case.peer.sync()
    preview = preview_move(case, params=CREATE)
    assert preview["folder_creation"] == {"name": NEW, "exists": True, "create": False}
    grant = case.service.tokens.inspect(preview["token"], "move_to_folder")
    assert grant.params == {"folder": NEW, "mark_read": False}
    assert preview["action_hash"] == preview_move(case, params={"folder": NEW})["action_hash"]
    case.peer.custom_folders.remove(NEW)
    case.peer.sync()
    with pytest.raises(MailValidationError, match="does not exist"):
        case.client.move_to_folder(preview["token"])
    assert not case.peer.created and not mutations(case.peer)
    case.service.tokens.inspect(preview["token"], "move_to_folder")


@pytest.mark.parametrize("when", ["preview", "confirm"])
def test_existing_folder_is_a_creation_no_op(bulk, when):
    case = bulk(2)
    if when == "preview":
        case.peer.custom_folders.append(NEW)
        case.peer.sync()
    preview = preview_move(case, params=CREATE)
    assert preview["folder_creation"]["create"] is (when == "confirm")
    if when == "confirm":
        # Created elsewhere between preview and confirmation: nothing to create.
        case.peer.custom_folders.append(NEW)
        case.peer.sync()
    result = case.client.move_to_folder(preview["token"])
    assert result["done"] == 2 and not result["partial"]
    if when == "confirm":
        assert result["folder_creation"] == {
            "name": NEW,
            "created": False,
            "exists": True,
            "ready": True,
            "uncertain": False,
        }
    else:
        assert "folder_creation" not in result  # An ordinary move approval.
    assert not case.peer.created and all(item.folder == NEW for item in case.items)
    assert not any(command.startswith("CREATE") for *_, command in case.peer.commands)
    assert not journal(case, "create_folder")


def test_failed_creation_moves_nothing_and_consumes_the_token(bulk):
    case = bulk(2)
    preview = preview_move(case, params=CREATE)
    case.peer.hooks.append(
        lambda sid, folder, operation, command: ("NO", "denied") if operation == "CREATE" else None
    )
    result = case.client.move_to_folder(preview["token"])
    assert result["done"] == 0 and result["partial"] and result["token_consumed"]
    # A refused CREATE is journaled uncertain, as by create_folder: check the folders.
    assert result["folder_creation"]["created"] is False
    assert result["folder_creation"]["uncertain"] is True
    assert "could not be confirmed created" in result["error"]
    assert result["continuation_token"] is None and not mutations(case.peer)
    assert all(item.folder == "INBOX" for item in case.items)
    created = journal(case, "create_folder")[0]
    assert created["partial"] and created["messages"][0]["status"] == "uncertain"
    with pytest.raises(MailValidationError, match="already used"):
        case.client.move_to_folder(preview["token"])


def test_creation_approval_text_names_the_folder(bulk):
    case = bulk(2)
    preview = preview_move(case, params=CREATE)
    from proton_mail_mcp.confirmation import approval_text, describe_approval

    summary = describe_approval(case.service, "move_to_folder", preview["token"])
    assert summary["create_folder"] == NEW and summary["target"] == NEW
    text = approval_text(summary)
    assert f'Create the folder "{NEW}" if it is missing, then move 2 message(s) to it.' in text
    assert preview["action_hash"][:16] in text


def test_host_form_shows_the_creation_and_a_declined_form_creates_nothing(bulk):
    import asyncio

    from mcp.server.fastmcp.exceptions import ToolError
    from test_send import Host

    from proton_mail_mcp.server import create_mcp

    case = bulk(2)
    mcp = create_mcp(case.client.settings, case.client)

    def call(token, host):
        return asyncio.run(
            mcp._tool_manager.call_tool(
                "move", {"target": CREATE["folder"], "token": token}, context=host
            )
        )

    declined = Host("decline")
    with pytest.raises(ToolError, match="User confirmation was not accepted"):
        call(preview_move(case, params=CREATE)["token"], declined)
    assert "Create the folder" in declined.prompts[0]
    assert not case.peer.created and not mutations(case.peer)
    accepted = Host()
    preview = preview_move(case, params=CREATE)
    result = call(preview["token"], accepted)
    assert "Create the folder" in accepted.prompts[0]
    assert preview["action_hash"][:16] in accepted.prompts[0]
    assert result["done"] == 2 and result["folder_creation"]["created"] is True
    assert case.peer.created == [NEW]
    audit = [row for row in case.client.list_actions(action="move_to_folder")["audit"]]
    assert audit[0]["outcome"] == "completed" and audit[0]["counts"]["done"] == 2
    # Recovery lookup shows the creation; the declined call recorded none.
    assert audit[0]["folder_created"] is True and "folder_creation_uncertain" not in audit[0]
    assert len(audit) == 2 and "folder_created" not in audit[1]


def test_refusal_after_creating_the_folder_returns_progress_and_a_continuation(bulk):
    # The approval is claimed and its folder created before the first chunk's own
    # preflight. A refusal there (another client archives a message meanwhile)
    # must return the created folder, the consumed token and a continuation,
    # never escape as an error that hides them.
    case = bulk(3)
    preview = preview_move(case, params=CREATE)

    def archive_one_during_create(sid, folder, operation, command):
        if operation == "CREATE":
            case.items[0].folder = "Archive"

    case.peer.hooks.append(archive_one_during_create)
    first = case.client.move_to_folder(preview["token"])
    assert case.peer.created == [NEW] and not mutations(case.peer)
    assert first["folder_creation"]["created"] is True and first["token_consumed"]
    assert first["done"] == 0 and first["partial"] and first["remaining"] == 3
    assert first["stop_reason"] == "Cleanup interrupted during chunk preparation"
    assert first["resume"] and first["continuation_token"]
    raw = [json.loads(line) for line in case.log.read_text().splitlines()]
    assert [row["action"] for row in raw if row["action"] in {"run_start", "run_end"}] == [
        "run_start",
        "run_end",
    ]
    second = case.client.move_to_folder(first["continuation_token"])
    assert second["done"] == 2 and [row["message_id"] for row in second["blocked"]] == [
        case.items[0].message_id()
    ]
    assert case.peer.created == [NEW] and "folder_creation" in second
    assert second["folder_creation"]["exists"] is True
    assert [item.folder for item in case.items] == ["Archive", NEW, NEW]


def test_missing_move_capability_after_creation_is_reported_not_raised(bulk):
    case = bulk(2)
    preview = preview_move(case, params=CREATE)
    case.peer.capabilities = [name for name in case.peer.capabilities if name != "MOVE"]
    result = case.client.move_to_folder(preview["token"])
    assert case.peer.created == [NEW] and not mutations(case.peer)
    assert result["folder_creation"]["created"] is True and result["token_consumed"]
    assert result["done"] == 0 and result["continuation_token"]
    assert result["stop_reason"] == "Cleanup interrupted during chunk preparation"
    # The continuation names the refusal before it claims anything.
    with pytest.raises(MailValidationError, match="lacks MOVE capability"):
        case.client.move_to_folder(result["continuation_token"])
    case.service.tokens.inspect(result["continuation_token"], "move_to_folder")


def slow_first_check(case, seconds):
    """Let the first whole-approval check of each call take extra seconds."""
    original = case.service._verified_chunk
    state = {"calls": 0}

    def slow(*args, **kwargs):
        rows = original(*args, **kwargs)
        if not state["calls"]:
            case.peer.clock.elapsed += seconds
        state["calls"] += 1
        return rows

    case.service._verified_chunk = slow
    return state


@pytest.mark.parametrize("seconds", [29.0, 36.0])
def test_creation_fits_its_first_chunk_to_the_remaining_budget(bulk, seconds):
    # A slow first check leaves room for only part of the 200-message chunk; the
    # chunk is sized with the creation's own allowance instead of refusing.
    case = bulk(200)
    preview = preview_move(case, params=CREATE)
    slow_first_check(case, seconds)
    first = case.client.move_to_folder(preview["token"])
    assert case.peer.created == [NEW] and first["token_consumed"]
    assert first["folder_creation"]["created"] is True
    assert 0 < first["done"] < 200 and first["continuation_token"]
    case.service._verified_chunk = type(case.service)._verified_chunk.__get__(case.service)
    rest = finish(case, first["continuation_token"])
    assert first["done"] + sum(result["done"] for result in rest) == 200
    assert all(item.folder == NEW for item in case.items) and case.peer.created == [NEW]


def test_a_stop_before_creation_reports_the_folder_still_missing(bulk):
    # The first check spends the whole budget: nothing is created or claimed, and
    # the response must not claim the folder exists.
    case = bulk(5)
    preview = preview_move(case, params=CREATE)
    slow_first_check(case, 45.0)
    result = case.client.move_to_folder(preview["token"])
    assert not case.peer.created and not result["token_consumed"] and result["done"] == 0
    assert result["folder_creation"] == {
        "name": NEW,
        "created": False,
        "exists": False,
        "ready": False,
        "uncertain": False,
    }
    case.service.tokens.inspect(preview["token"], "move_to_folder")


def test_unconfirmed_or_unlisted_creation_is_reported_as_such(bulk):
    case = bulk(2)
    preview = preview_move(case, params=CREATE)
    case.peer.hooks.append(
        lambda sid, folder, operation, command: (
            "apply-then-stall" if operation == "CREATE" else None
        )
    )
    lost = case.client.move_to_folder(preview["token"])
    assert NEW in case.peer.mailbox_names() and not mutations(case.peer)
    assert lost["folder_creation"]["uncertain"] is True
    assert lost["folder_creation"]["created"] is False and lost["token_consumed"]
    assert "could not be confirmed created" in lost["error"]
    case = bulk(2)
    preview = preview_move(case, params=CREATE)
    listed = {"created": False, "failed": False}

    def list_fails_once_after_create(sid, folder, operation, command):
        if operation == "CREATE":
            listed["created"] = True
        elif operation == "LIST" and listed["created"] and not listed["failed"]:
            listed["failed"] = True
            return ("NO", "busy")

    case.peer.hooks.append(list_fails_once_after_create)
    unlisted = case.client.move_to_folder(preview["token"])
    assert unlisted["folder_creation"]["created"] is True
    assert unlisted["folder_creation"]["ready"] is False
    assert "created but is not ready" in unlisted["error"]
    rest = finish(case, unlisted["continuation_token"])
    assert sum(result["done"] for result in rest) == 2 and case.peer.created == [NEW]


def test_bounded_approval_cap_comes_from_config_and_binds_the_full_set(bulk):
    case = bulk(450, bound=300)
    with pytest.raises(MailValidationError, match="at most 300"):
        preview_move(case, limit=301)
    unbounded = preview_move(case)
    assert unbounded["token"] is None and unbounded["count"] == 450
    assert "set limit (at most 300)" in unbounded["reason"]
    preview = preview_move(case, limit=300)
    assert preview["count"] == 300 and preview["approval_max_messages"] == 300
    grant = case.service.tokens.inspect(preview["token"], "move_to_folder")
    assert grant.selector == {"query": QUERY, "limit": 300, "max_messages": 300}
    assert preview["action_hash"] == approval_hash(
        "move_to_folder", grant.params, grant.selector, grant.messages
    )
    approved = {row["uid"] for row in grant.messages}
    inbox = sorted(case.peer.uidmap["INBOX"])
    assert approved == {str(uid) for uid in inbox[:300]}
    results = finish(case, preview["token"])
    assert sum(result["done"] for result in results) == 300
    moves = commands(case.peer, "MOVE")
    assert [len(uid_values(spec)) for _, spec in moves] == [200, 100]
    assert {uid for _, spec in moves for uid in uid_values(spec)} == approved
    assert len(case.peer.uidmap["INBOX"]) == 150  # Never beyond the bound set.
    run = case.client.list_actions(token=preview["token"])["runs"][0]
    assert run["approved_total"] == run["done"] == 300 and run["status"] == "completed"
    # The store refuses a set larger than the selector's bound, or an invalid bound.
    rows = copy.deepcopy(grant.messages)
    extra = {**rows[0], "uid": "99999"}
    with pytest.raises(MailValidationError, match="between 1 and 300"):
        case.service.tokens.issue("move_to_folder", grant.params, grant.selector, [*rows, extra])
    for bound in (200, 1001, "300", True):
        with pytest.raises(MailValidationError, match="approval bound"):
            case.service.tokens.issue(
                "move_to_folder", grant.params, {**grant.selector, "max_messages": bound}, rows
            )
    with pytest.raises(MailValidationError, match="between 1 and 200"):
        case.service.tokens.issue("move_to_folder", grant.params, {"query": QUERY}, rows)


def test_continuations_never_widen_a_bounded_approval(bulk):
    case = bulk(260)
    preview = preview_move(case, limit=250)
    approved = approved_uids(case, preview["token"])
    first = case.client.move_to_folder(preview["token"], limit=200)
    assert first["done"] == 200 and first["resume"] and first["remaining"] == 50
    continuation = case.service.tokens.inspect(first["continuation_token"], "move_to_folder")
    assert {row["uid"] for row in continuation.messages} <= approved
    assert continuation.approved_total == 250 and continuation.selector["max_messages"] == 500
    outsider_uid = next(str(uid) for uid in case.peer.uidmap["INBOX"] if str(uid) not in approved)
    outsider = {**continuation.messages[0], "uid": outsider_uid}
    with pytest.raises(MailValidationError, match="exceeds the approved message set"):
        case.service.tokens.continue_grant(continuation, [*continuation.messages, outsider])
    ids = [row["message_id"] for row in continuation.messages]
    with pytest.raises(MailValidationError, match="message set"):
        case.service.execute(
            "move_to_folder", first["continuation_token"], ids=[*ids, "<extra@example.invalid>"]
        )
    # A per-call limit stays 1..200 even inside a bounded approval.
    with pytest.raises(MailValidationError, match="at most 200"):
        case.service.execute("move_to_folder", first["continuation_token"], limit=201)
    stored = case.service.tokens._grants[
        hashlib.sha256(first["continuation_token"].encode("ascii")).digest()
    ]
    stored.selector["max_messages"] = 1000
    with pytest.raises(MailValidationError, match="approved action set"):
        case.client.move_to_folder(first["continuation_token"])
    stored.selector["max_messages"] = 500
    arrival = case.peer.add(Msg(hdr("<arrival@example.invalid>")))
    second = case.client.move_to_folder(first["continuation_token"])
    assert second["done"] == 50 and not second["partial"] and second["run_done"] == 250
    named = {uid for _, spec in commands(case.peer, "MOVE") for uid in uid_values(spec)}
    assert named == approved and arrival.folder == "INBOX"
    assert len(case.peer.uidmap["INBOX"]) == 11


def test_bounded_run_resumes_idempotently_after_a_lost_response(bulk):
    case = bulk(450)
    preview = preview_move(case, limit=450)
    approved = approved_uids(case, preview["token"])
    first = case.client.move_to_folder(preview["token"], limit=200)
    assert first["done"] == 200 and first["resume"]
    # The response was lost: the client retries the original token, which names
    # only the same run's untouched remainder; nothing is moved twice.
    retry = case.client.move_to_folder(preview["token"], limit=200)
    assert retry["done"] == 200 and retry["run_id"] == first["run_id"]
    assert retry["run_done"] == 400 and retry["remaining"] == 50
    # The first continuation is an alias of that same remainder.
    last = case.client.move_to_folder(first["continuation_token"])
    assert last["done"] == 50 and not last["partial"] and last["run_done"] == 450
    for token in (preview["token"], first["continuation_token"], retry["continuation_token"]):
        with pytest.raises(MailValidationError, match="already used"):
            case.client.move_to_folder(token)
    named = [uid for _, spec in commands(case.peer, "MOVE") for uid in uid_values(spec)]
    assert len(named) == len(set(named)) == 450 and set(named) == approved
    run = case.client.list_actions(token=preview["token"])["runs"][0]
    assert run["status"] == "completed" and run["done"] == run["approved_total"] == 450
    chunks = journal(case)
    assert [chunk["chunk_index"] for chunk in chunks] == [0, 1, 2]
    assert {chunk["run_id"] for chunk in chunks} == {first["run_id"]}


def test_lowered_configured_bound_refuses_a_larger_pending_approval(bulk):
    case = bulk(260)
    preview = preview_move(case, limit=250)
    case.service.settings = replace(case.service.settings, approval_max_messages=200)
    with pytest.raises(MailValidationError, match="configured approval bound"):
        case.client.move_to_folder(preview["token"])
    assert not mutations(case.peer) and not execution_log(case.log)
    case.service.tokens.inspect(preview["token"], "move_to_folder")


def test_pending_approved_rows_stay_bounded(monkeypatch):
    monkeypatch.setattr(guarded, "MAX_PENDING_ROWS", 3)
    tokens = TokenStore()
    row = {"folder": "INBOX", "uidvalidity": "7", "uid": "1", "message_id": "<a@example.test>"}
    token, _ = tokens.issue("archive", {}, {"query": {}}, [row, {**row, "uid": "2"}])
    tokens.issue("archive", {}, {"query": {}}, [{**row, "uid": "3"}])
    with pytest.raises(MailValidationError, match="pending approved messages"):
        tokens.issue("archive", {}, {"query": {}}, [{**row, "uid": "4"}])
    grant = tokens.claim(token, "archive")
    assert tokens.continue_grant(grant, grant.messages[:1])


def test_run_journal_accepts_bounded_totals_up_to_the_ceiling():
    for total, valid in ((1000, True), (1001, False), (0, False)):
        start = run_event("run_start", "r" * 32, approved_total=total, token_digest="d")
        if valid:
            assert replay_runs([start], [])[0]["approved_total"] == total
        else:
            with pytest.raises(MailError, match="invalid approval"):
                replay_runs([start], [])


def test_approval_bound_configuration():
    base = {"PROTON_MCP_TOKEN": "bulk-pack-config-" + "x" * 32, "PROTON_BRIDGE_USER": "u"}
    assert Settings.from_env(base).approval_max_messages == 500
    for value, expected in (("200", 200), ("1000", 1000), ("750", 750)):
        settings = Settings.from_env({**base, "PROTON_MCP_APPROVAL_MAX_MESSAGES": value})
        assert settings.approval_max_messages == expected
    for value in ("199", "1001", "0", "-1", "nan", "", "5e2"):
        with pytest.raises(ConfigError):
            Settings.from_env({**base, "PROTON_MCP_APPROVAL_MAX_MESSAGES": value})
    with pytest.raises(ConfigError):
        Settings(token=base["PROTON_MCP_TOKEN"], bridge_user="u", approval_max_messages=True)


def test_bound_200_keeps_the_classic_cap(bulk):
    case = bulk(201, bound=200)
    with pytest.raises(MailValidationError, match="at most 200"):
        preview_move(case, limit=201)
    preview = preview_move(case)
    assert preview["token"] is None and preview["approval_max_messages"] == 200
    assert preview["reason"] == "Batch exceeds the hard cap of 200 messages"


def undo_until_restored(case, log_id):
    """Repeat undo_action as its partial budget stops ask; every call must restore."""
    attempts = []
    while True:
        started = case.peer.clock.elapsed
        result = case.client.undo_action(log_id)
        assert case.peer.clock.elapsed - started < case.client.settings.call_timeout
        assert result["count"] > 0 and not result["missing"] and "unresolved" not in result
        attempts.append(result)
        if not result["partial"]:
            return attempts
        assert result["error"].startswith("Undo time budget reached")
        assert len(attempts) < 12


def test_slow_undo_of_a_full_batched_chunk_resumes_until_each_message_is_restored(bulk):
    # Undo restores one message at a time. On this slow peer (0.15 s per command,
    # 1.5 s per MOVE) looking up all 200 messages of one batched chunk used to
    # spend the whole call before the first restore, on every retry. Preparation
    # now keeps half of the call for restores and claims only what it examined.
    case = bulk(200)
    log_id = case.client.move_to_folder(preview_move(case)["token"])["log_ids"][0]
    case.peer.command_latency, case.peer.mutation_seconds = 0.15, 1.5
    attempts = undo_until_restored(case, log_id)
    assert len(attempts) > 1 and any(result.get("deferred") for result in attempts)
    assert sum(result["count"] for result in attempts) == 200
    assert all(item.folder == "INBOX" for item in case.items)
    restored = [spec for folder, spec in commands(case.peer, "MOVE") if folder == TARGET]
    assert len(restored) == len(set(restored)) == 200  # Each copy moved back once.
    with pytest.raises(MailValidationError, match="already been undone"):
        case.client.undo_action(log_id)
    raw = [json.loads(line) for line in case.log.read_text().splitlines()]
    claims = [row["original_indices"] for row in raw if row["action"] == "undo_claim"]
    outcomes = [row for row in raw if row["action"] == "undo_action"]
    assert len(claims) == len(outcomes) == len(attempts)
    pending = list(range(200))
    for claim, outcome, result in zip(claims, outcomes, attempts):
        # Each attempt claims a nonempty prefix of the messages still pending; the
        # unexamined rest is neither claimed nor described, and stays pending.
        assert claim and claim == pending[: len(claim)]
        statuses = {entry["original_index"]: entry["status"] for entry in outcome["messages"]}
        assert set(statuses) == set(claim) and set(statuses.values()) <= {"done", "not_attempted"}
        assert outcome.get("deferred", 0) == result.get("deferred", 0) == len(pending) - len(claim)
        pending = [index for index in claim if statuses[index] == "not_attempted"] + pending[
            len(claim) :
        ]
    assert not pending


def test_undo_retry_accepts_only_a_claimed_prefix(bulk):
    case = bulk(200)
    log_id = case.client.move_to_folder(preview_move(case)["token"])["log_ids"][0]
    case.peer.command_latency, case.peer.mutation_seconds = 0.15, 1.5
    first = case.client.undo_action(log_id)
    assert first["partial"] and first["deferred"] and first["count"]
    with case.service.backend.log.transaction() as transaction:
        records = transaction.records()
    original = next(record for record in records if record.get("log_id") == log_id)
    claim = next(record for record in records if record.get("action") == "undo_claim")
    outcome = next(record for record in records if record.get("action") == "undo_action")
    examined = claim["original_indices"]
    assert examined == list(range(len(examined))) and len(examined) < 200
    remaining, unresolved = remaining_undo(original, records)
    retried = [entry for entry in outcome["messages"] if entry["status"] == "not_attempted"]
    assert not unresolved
    assert [row["original_index"] for row in remaining["messages"]] == [
        entry["original_index"] for entry in retried
    ] + list(range(len(examined), 200))
    for row in remaining["messages"]:
        # Only examined messages carry the earlier attempt's retry handle.
        assert ("undo_retry_handle" in row) is (row["original_index"] in examined)

    def tampered(change):
        changed = copy.deepcopy(records)
        change(next(record for record in changed if record.get("action") == "undo_claim"))
        return changed

    for change in (
        lambda claim: claim.update(original_indices=[]),
        lambda claim: claim.update(original_indices=examined[1:]),
        lambda claim: claim.update(original_indices=[*examined[:-1], 199]),
        lambda claim: claim.update(original_indices=[*examined, examined[0]]),
        # Shrinking a claim below the entries its outcome describes.
        lambda claim: claim["original_indices"].pop(),
    ):
        with pytest.raises(MailValidationError, match="unresolved journal outcomes"):
            remaining_undo(original, tampered(change))


def test_slow_restores_stop_before_one_that_cannot_finish(bulk):
    # Restoring a moved, marked-read message takes a STORE, a MOVE and their
    # checks: about five seconds on this peer, beyond the two-second planning
    # allowance. Each further restore is planned with the slowest one observed
    # in the call, so the deadline never cuts a restore off as uncertain.
    case = bulk(60)
    preview = preview_move(case, params={"folder": TARGET, "mark_read": True})
    log_id = case.client.move_to_folder(preview["token"])["log_ids"][0]
    case.peer.command_latency, case.peer.mutation_seconds = 0.15, 2.0
    attempts = undo_until_restored(case, log_id)
    assert len(attempts) > 1 and sum(result["count"] for result in attempts) == 60
    assert all(item.folder == "INBOX" and r"\Seen" not in item.flags for item in case.items)
    raw = [json.loads(line) for line in case.log.read_text().splitlines()]
    outcomes = [row for row in raw if row["action"] == "undo_action"]
    assert {entry["status"] for row in outcomes for entry in row["messages"]} == {
        "done",
        "not_attempted",
    }


def test_read_state_undo_drift_refuses_every_remaining_restore_after_a_deferral(bulk):
    # Read-state undo refuses all its STOREs on any identity drift. When an
    # earlier attempt deferred lookups, the refusal still covers every message
    # not yet restored, not only those its attempt had examined.
    case = bulk(200)
    log_id = case.client.mark_read(case.client.preview_action("mark_read", query=QUERY)["token"])[
        "log_ids"
    ][0]
    case.peer.command_latency, case.peer.mutation_seconds = 0.15, 1.5
    first = case.client.undo_action(log_id)
    assert first["deferred"] and 0 < first["count"] < 200
    restored = first["count"]
    # The next message to restore leaves INBOX in another client.
    case.items[restored].folder = "Archive"
    case.peer.sync()
    stores = len(commands(case.peer, "STORE"))
    second = case.client.undo_action(log_id)
    assert second["count"] == 0 and len(second["missing"]) == 1 and "deferred" not in second
    assert len(commands(case.peer, "STORE")) == stores
    with pytest.raises(MailValidationError, match="unresolved messages"):
        case.client.undo_action(log_id)
    assert sum(r"\Seen" not in item.flags for item in case.items) == restored


@pytest.mark.parametrize(("seconds", "creates"), [(36.0, True), (39.0, False)])
def test_creation_alone_is_progress_when_no_move_fits_beside_it(bulk, seconds, creates):
    # A bounded approval's first check covers all 450 rows. When it leaves room
    # for the creation but not also for one move, the call creates the folder
    # and continues instead of refusing on every retry; with no room even for
    # that, nothing is created and the token stays valid.
    case = bulk(450)
    preview = preview_move(case, params=CREATE, limit=450)
    slow_first_check(case, seconds)
    first = case.client.move_to_folder(preview["token"])
    assert first["done"] == 0 and not mutations(case.peer)
    assert case.peer.created == ([NEW] if creates else [])
    assert first["token_consumed"] is creates and bool(first["continuation_token"]) is creates
    assert first["folder_creation"]["created"] is creates
    assert first["stop_reason"] == "Execute response budget reached"
    if not creates:
        case.service.tokens.inspect(preview["token"], "move_to_folder")
        return
    # Lookup by the approved tool finds that progress: the creation, with its run.
    lookup = case.client.list_actions(action="move_to_folder")
    assert [(row["action"], row["status"]) for row in replay_records(lookup["actions"])] == [
        ("create_folder", "completed")
    ]
    assert lookup["actions"][0]["log_id"] == first["folder_creation"]["log_id"]
    [run] = lookup["runs"]
    assert run["run_id"] == first["run_id"] and run["done"] == 0 and run["log_ids"] == []
    rest = finish(case, first["continuation_token"])
    assert sum(result["done"] for result in rest) == 450 and case.peer.created == [NEW]
    assert all(item.folder == NEW for item in case.items)
    # A continuation token finds it too; it is never counted as one of the chunks.
    lookup = case.client.list_actions(token=first["continuation_token"], action="move_to_folder")
    chunks = [log_id for result in rest for log_id in result["log_ids"]]
    assert {row["log_id"] for row in replay_records(lookup["actions"])} == {
        first["folder_creation"]["log_id"],
        *chunks,
    }
    [run] = lookup["runs"]
    assert run["log_ids"] == chunks and run["done"] == 450 and run["status"] == "completed"


def test_rows_of_a_refused_batched_command_are_reported_mismatched(bulk):
    case = bulk(5)
    preview = case.client.preview_action("archive", query=QUERY)
    case.peer.hooks.append(
        lambda sid, folder, operation, command: ("NO", "denied") if operation == "MOVE" else None
    )
    result = case.client.archive(preview["token"])
    # The server may have applied part of a refused command: all stay uncertain.
    assert result["done"] == 0 and result["uncertain"] == 5 and result["partial"]
    assert len(result["mismatched"]) == 5
    assert {row["status"] for row in result["mismatched"]} == {"uncertain"}
    assert all(
        row["reason"].startswith("Batched command outcome could not be verified")
        for row in result["mismatched"]
    )
    rows = journal(case, "archive")[0]["messages"]
    assert all(row["status"] == "uncertain" and "verification" in row for row in rows)


def test_audit_marks_an_unconfirmed_folder_creation(bulk):
    import asyncio

    from test_send import Host

    from proton_mail_mcp.server import create_mcp

    case = bulk(2)
    mcp = create_mcp(case.client.settings, case.client)
    preview = preview_move(case, params=CREATE)
    case.peer.hooks.append(
        lambda sid, folder, operation, command: (
            "apply-then-stall" if operation == "CREATE" else None
        )
    )
    result = asyncio.run(
        mcp._tool_manager.call_tool(
            "move", {"target": preview["target"], "token": preview["token"]}, context=Host()
        )
    )
    assert result["folder_creation"]["uncertain"] is True
    raw = [json.loads(line) for line in case.log.read_text().splitlines()]
    audit = [
        row
        for row in raw
        if row["action"] == "audit"
        and row["tool"] == "move_to_folder"
        and row["outcome"] != "started"
    ]
    assert len(audit) == 1 and audit[0]["folder_created"] is False
    assert audit[0]["folder_creation_uncertain"] is True
    # Recovery lookup shows both flags, and lists the creation with its run when
    # filtered by the approved tool, without counting it as one of the run's chunks.
    lookup = case.client.list_actions(action="move_to_folder")
    [entry] = lookup["audit"]
    assert entry["folder_created"] is False and entry["folder_creation_uncertain"] is True
    [created] = replay_records(lookup["actions"])
    assert created["action"] == "create_folder" and created["uncertain"] == 1
    assert created["log_id"] == result["folder_creation"]["log_id"]
    [run] = lookup["runs"]
    assert run["run_id"] == result["run_id"] and run["done"] == 0 and run["log_ids"] == []
