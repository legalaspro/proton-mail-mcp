"""Logical cleanup identities remain independent of folder-local UID collisions."""

import json
from pathlib import Path

import pytest
from action_records import execution_records as replay_records
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations

from proton_mail_mcp.mail import MailValidationError


@pytest.fixture
def mailbox(tmp_path):
    sim = BridgeSim()
    client = make_client(sim, tmp_path)
    yield sim, client
    sim.close()
    assert not sim.errors and not sim.violations
    assert all(not worker.is_alive() for _, _, worker in sim.peers)


def pair(row):
    return row["message_id"], row["folder"]


def test_colliding_uids_keep_preview_token_log_and_undo_identities_distinct(mailbox):
    sim, client = mailbox
    first = sim.add(Msg(hdr("<inbox-copy@example.invalid>"), folder="INBOX"))
    second = sim.add(
        Msg(hdr("<spam-copy@example.invalid>"), folder="Spam", labels={"Labels/Review"})
    )
    assert sim.uid_of("INBOX", first.pid) == sim.uid_of("Spam", second.pid) == 1
    assert sim.uid_of("Labels/Review", second.pid) == 1
    # UIDVALIDITY is also scoped to a mailbox; equal values do not join copies.
    for folder in sim.uidvalidity:
        sim.uidvalidity[folder] = 777
    identifiers = [first.message_id(), second.message_id()]
    expected = {(first.message_id(), "INBOX"), (second.message_id(), "Spam")}

    preview = client.preview_action("move_to_trash", ids=identifiers)
    assert preview["count"] == 2 and preview["token"] and not preview["partial"]
    assert {pair(row) for row in preview["samples"]} == expected
    assert all(row["id"] == row["message_id"] for row in preview["samples"])
    # Samples expose the full canonical triple, never a bare UID. Equal UID and
    # UIDVALIDITY values stay distinct through the folder and match the token.
    triples = {
        tuple(row[key] for key in ("folder", "uidvalidity", "uid")) for row in preview["samples"]
    }
    assert triples == {("INBOX", "777", "1"), ("Spam", "777", "1")}
    assert all("alias" not in row for row in preview["samples"])
    grant = next(iter(client.cleanup.tokens._grants.values()))
    assert {pair(row) for row in grant.messages} == expected
    assert {
        tuple(row[key] for key in ("folder", "uidvalidity", "uid")) for row in grant.messages
    } == triples
    for row in grant.messages:
        assert {pair(copy) for copy in row["memberships"]} == {
            (row["message_id"], folder) for folder in row["folders"]
        }
    assert grant.messages[0]["uid"] == grant.messages[1]["uid"] == "1"

    completed = client.move_to_trash(preview["token"])
    assert completed["count"] == 2 and not completed["partial"]
    assert {pair(row) for row in completed["messages"]} == expected
    log = Path(client.settings.action_log_path)
    recorded = replay_records([json.loads(line) for line in log.read_text().splitlines()])[0]
    assert {pair(row) for row in recorded["messages"]} == expected
    for row in recorded["messages"]:
        assert row["source_folder"] == row["folder"]
        assert all(copy["message_id"] == row["message_id"] for copy in row["memberships_before"])

    restored = client.undo_action(completed["log_ids"][0])
    assert restored["count"] == 2 and not restored["partial"]
    assert (first.folder, second.folder) == ("INBOX", "Spam")
    assert second.labels == {"Labels/Review"}
    assert {pair(row) for row in restored["messages"]} == {
        (identifier, "Trash") for identifier in identifiers
    }


def test_protected_row_with_colliding_uid_names_its_own_message_and_folder(mailbox):
    sim, client = mailbox
    safe = sim.add(Msg(hdr("<safe@example.invalid>"), folder="INBOX"))
    protected = sim.add(
        Msg(
            hdr("<protected@example.invalid>", from_="Alerts <alerts@broker.example.org>"),
            folder="Spam",
        )
    )
    assert sim.uid_of("INBOX", safe.pid) == sim.uid_of("Spam", protected.pid) == 1
    preview = client.preview_action(
        "move_to_trash", ids=[safe.message_id(), protected.message_id()]
    )
    assert preview["count"] == 2 and preview["token"] is None
    assert {pair(row) for row in preview["blocked"]} == {(protected.message_id(), "Spam")}
    # A colliding UID is reported only inside its own folder's canonical triple.
    assert [(row["folder"], row["uid"]) for row in preview["blocked"]] == [("Spam", "1")]
    assert preview["blocked"][0]["uidvalidity"] == str(sim.uidvalidity["Spam"])
    assert mutations(sim) == []


def test_same_message_id_in_two_sources_is_ambiguous_even_when_uids_collide(mailbox):
    sim, client = mailbox
    first = sim.add(Msg(hdr("<ambiguous@example.invalid>"), folder="INBOX"))
    second = sim.add(Msg(hdr(first.message_id()), folder="Spam"))
    assert sim.uid_of("INBOX", first.pid) == sim.uid_of("Spam", second.pid) == 1
    result = client.preview_action("move_to_trash", ids=[first.message_id()])
    assert result["token"] is None and result["excluded_ids"] == [first.message_id()]
    assert result["excluded_reasons"] == {"ambiguous_source": 1}
    assert mutations(sim) == []


def test_token_refuses_folder_swap_when_message_ids_and_uid_values_stay_equal(mailbox):
    sim, client = mailbox
    first = sim.add(Msg(hdr("<first@example.invalid>"), folder="INBOX"))
    second = sim.add(Msg(hdr("<second@example.invalid>"), folder="Spam"))
    identifiers = [first.message_id(), second.message_id()]
    preview = client.preview_action("move_to_trash", ids=identifiers)
    assert preview["token"]
    first.folder, second.folder = second.folder, first.folder
    # Emulate another client moving both copies while their folder-local UID
    # numbers collide. The stable Message-ID/folder pairs still changed.
    sim.uidmap["INBOX"] = {1: second.pid}
    sim.uidmap["Spam"] = {1: first.pid}
    with pytest.raises(MailValidationError, match="changed"):
        client.move_to_trash(preview["token"])
    assert mutations(sim) == []


def test_mutator_refuses_repeated_exact_uid_handle(mailbox):
    sim, client = mailbox
    sim.add(Msg(hdr("<duplicate@example.invalid>"), folder="INBOX"))
    sim.add(Msg(hdr("<duplicate@example.invalid>"), folder="INBOX"))
    mutator = client.cleanup.backend
    with client._session() as (connection, _):
        deadline = client._deadline()
        rows = mutator._fetch(connection, deadline, "INBOX", ["1", "2"])
        assert len(rows) == 2 and rows[0]["uid"] != rows[1]["uid"]
        with pytest.raises(MailValidationError, match="duplicate"):
            mutator.apply(connection, deadline, "move_to_trash", {}, [rows[0], rows[0]])
    assert mutations(sim) == []


def test_missing_undo_row_names_destination_identity_despite_other_folder_uid(mailbox):
    sim, client = mailbox
    original = sim.add(Msg(hdr("<missing@example.invalid>"), folder="INBOX"))
    survivor = sim.add(Msg(hdr("<survivor@example.invalid>"), folder="Spam"))
    preview = client.preview_action("move_to_trash", ids=[original.message_id()])
    completed = client.move_to_trash(preview["token"])
    assert sim.uid_of("Trash", original.pid) == sim.uid_of("Spam", survivor.pid) == 1
    del sim.messages[original.pid]
    before = mutations(sim)
    restored = client.undo_action(completed["log_ids"][0])
    assert restored["count"] == 0 and restored["partial"]
    assert restored["missing"] == [
        {
            "message_id": original.message_id(),
            "folder": "Trash",
            "reason": 'not found during undo in "Trash"',
        }
    ]
    assert survivor.folder == "Spam" and mutations(sim) == before


def test_new_log_identity_mismatch_refuses_undo_without_claim(mailbox):
    sim, client = mailbox
    original = sim.add(Msg(hdr("<log-identity@example.invalid>"), folder="INBOX"))
    preview = client.preview_action("move_to_trash", ids=[original.message_id()])
    completed = client.move_to_trash(preview["token"])
    log = Path(client.settings.action_log_path)
    record = replay_records([json.loads(line) for line in log.read_text().splitlines()])[0]
    record["messages"][0]["folder"] = "Spam"
    log.write_text(json.dumps(record) + "\n")
    before = log.read_bytes()
    with pytest.raises(MailValidationError, match="Logged message folder"):
        client.undo_action(completed["log_ids"][0])
    assert log.read_bytes() == before and original.folder == "Trash"


@pytest.mark.parametrize("action", ["remove_label", "move_to_trash"])
def test_preflight_and_legacy_undo_preserve_identity_without_header_search(mailbox, action):
    sim, client = mailbox
    sim.copyuid = False  # Moving back must use the old-log header-digest fallback.
    original = sim.add(
        Msg(hdr("<bounded-lookup@example.invalid>"), folder="INBOX", labels={"Labels/Review"})
    )
    source = "Labels/Review" if action == "remove_label" else "INBOX"
    params = {"label": "Review"} if action == "remove_label" else {}
    preview = client.preview_action(action, query={"folder": source}, params=params)
    assert preview["token"]
    if action == "move_to_trash":
        # Its copy hidden from verification, the move stays uncertain without a
        # destination UID, as in an old log.
        sim.fetch_omit.add(("Trash", original.pid))
    completed = getattr(client, action)(preview["token"])
    sim.fetch_omit.clear()
    if action == "move_to_trash":
        assert completed["uncertain"] == 1 and completed["messages"][0]["uid_after"] is None
    else:
        assert completed["count"] == 1 and not completed["partial"]
        assert not original.labels and sim.uid_of("All Mail", original.pid)
    restored = client.undo_action(completed["log_ids"][0])
    assert restored["count"] == 1 and not restored["partial"]
    assert original.folder == "INBOX" and original.labels == {"Labels/Review"}
    assert not any(
        "SEARCH" in command and "HEADER Message-ID" in command for _, _, _, command in sim.commands
    )
    assert any(command.endswith("(UID ENVELOPE)") for _, _, _, command in sim.commands)
