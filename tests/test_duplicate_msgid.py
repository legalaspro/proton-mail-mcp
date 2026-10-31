"""One source folder binds physical UIDs, even when a sender reuses Message-ID."""

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from action_records import execution_records as replay_records
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations
from resume_assertions import assert_bound_progress, identity
from test_cleanup_mutations import uid_values
from test_rules_timer import baseline

from proton_mail_mcp import guarded
from proton_mail_mcp.folder_rules import FolderRulesService
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.rules_timer import RulesTimer

DUPLICATE = "<example/repo/pull/1/before/abc/after/def@github.example>"
SENDER = "notifications@github.example"
DESTINATION = "Folders/Reviewed"


@pytest.fixture
def duplicate_mailbox(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Duplicate and Seen tests must use the socketpair simulator")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    simulations = []

    def build(**options):
        sim = BridgeSim(**options)
        simulations.append(sim)
        return sim, make_client(sim, tmp_path, call_timeout=300)

    yield build
    for sim in simulations:
        sim.close()
        assert not sim.errors and not sim.violations
        assert all(not worker.is_alive() for _, _, worker in sim.peers)


def duplicates(sim, count=3, *, identical=False, folder="INBOX"):
    return [
        sim.add(
            Msg(
                hdr(
                    DUPLICATE,
                    from_=SENDER,
                    date=f"Fri, 16 Oct 2026 09:{0 if identical else index % 60:02d}:00 +0000",
                ),
                folder=folder,
            )
        )
        for index in range(count)
    ]


def preview_move(client, action="move_to_folder", **params):
    return client.preview_action(
        action,
        query={"folder": "INBOX", "from_": SENDER, "since": "2026-10-01"},
        params={"folder": DESTINATION, **params} if action == "move_to_folder" else None,
    )


def rules_service(client, tmp_path):
    path = tmp_path / "duplicate-rules.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [{"field": "from", "contains": SENDER, "folder": DESTINATION}],
            }
        )
    )
    return FolderRulesService(client.cleanup, path)


def logged(client):
    return replay_records(
        [
            json.loads(line)
            for line in Path(client.settings.action_log_path).read_text().splitlines()
        ]
    )


class ObservedMutations(list):
    """Capture approved source identity independently before each socket mutation."""


def observe_sim_mutations(sim, grant, *, operation):
    actual = ObservedMutations()
    approved = {identity(row) for row in grant.messages}

    def observe(session, folder, command_operation, command):
        if command_operation != operation:
            return
        # A batched command names a UID set (ranges); bind every UID in it.
        for uid in uid_values(command.split()[2]):
            item = sim.messages[sim.uidmap[folder][int(uid)]]
            bound = (folder, str(sim.uidvalidity[folder]), uid, item.message_id())
            assert bound in approved and bound not in actual
            actual.append(bound)

    actual.hook = observe
    sim.hooks.append(observe)
    return actual


def undo_sim_chunks(sim, client, chunks, items):
    """Each actual journal restores its exact done rows once, with no extras."""
    by_destination = {str(sim.uid_of(item.folder, item.pid)): item for item in items}
    for chunk in reversed(chunks):
        entries = [row for row in chunk["messages"] if row["status"] == "done"]
        expected = {row["uid_after"] for row in entries}
        before = len(mutations(sim))
        restored = client.undo_action(chunk["log_id"])
        assert restored["count"] == len(entries) and not restored["partial"]
        commands = mutations(sim)[before:]
        assert len(commands) == len(expected)
        assert {command.split()[2] for _, _, _, command in commands} == expected
        assert all(by_destination[uid].folder == "INBOX" for uid in expected)
        before_retry = mutations(sim)
        with pytest.raises(MailValidationError, match="already.*undone"):
            client.undo_action(chunk["log_id"])
        assert mutations(sim) == before_retry


@pytest.mark.parametrize("identical", [False, True])
@pytest.mark.parametrize("copyuid", [False, True])
@pytest.mark.parametrize("action", ["move_to_folder", "move_to_trash", "archive"])
def test_narrowed_live_repro_moves_every_bound_duplicate_only(
    duplicate_mailbox, identical, copyuid, action
):
    sim, client = duplicate_mailbox(copyuid=copyuid)
    approved = duplicates(sim, identical=identical)
    target = {"move_to_folder": DESTINATION, "move_to_trash": "Trash", "archive": "Archive"}[action]
    preexisting = duplicates(sim, count=1, identical=identical, folder=target)[0]
    unrelated = sim.add(Msg(hdr("<unrelated@example.test>", from_="other@example.test")))
    preview = preview_move(client, action)
    assert preview["count"] == 3 and preview["token"] and not preview["blocked"]
    assert not mutations(sim)
    grant = client.cleanup.tokens.inspect(preview["token"], action)
    assert {row["uid"] for row in grant.messages} == {
        str(sim.uid_of("INBOX", item.pid)) for item in approved
    }
    assert {row["uidvalidity"] for row in grant.messages} == {str(sim.uidvalidity["INBOX"])}
    completed = getattr(client, action)(preview["token"])
    assert completed["count"] == 3 and not completed["partial"]
    assert all(item.folder == target for item in approved)
    assert unrelated.folder == "INBOX" and preexisting.folder == target
    entries = logged(client)[0]["messages"]
    assert len({entry["uid_before"] for entry in entries}) == 3
    assert {entry["source_folder"] for entry in entries} == {"INBOX"}
    assert {entry["uid_after"] for entry in entries} == {
        str(sim.uid_of(target, item.pid)) for item in approved
    }
    assert all(entry["header_digest"] and isinstance(entry["size"], int) for entry in entries)
    restored = client.undo_action(completed["log_ids"][0])
    assert restored["count"] == 3 and not restored["partial"]
    assert all(item.folder == "INBOX" for item in approved)
    assert preexisting.folder == target and unrelated.folder == "INBOX"
    assert all("UID MOVE " in item[3] for item in mutations(sim))


@pytest.mark.parametrize("selector", [{"ids": [DUPLICATE]}, {"query": {"from_": SENDER}}])
def test_unnarrowed_cross_folder_duplicates_still_refused(duplicate_mailbox, selector):
    sim, client = duplicate_mailbox()
    duplicates(sim)
    duplicates(sim, count=1, folder="Archive")
    result = client.preview_action("move_to_trash", **selector)
    assert result["token"] is None and result["excluded_ids"] == [DUPLICATE]
    assert result["excluded_count"] == 1
    assert not mutations(sim)


@pytest.mark.parametrize("change", ["uidvalidity", "missing", "message_id", "date", "size"])
def test_confirm_drift_refuses_whole_duplicate_batch(duplicate_mailbox, change):
    sim, client = duplicate_mailbox()
    approved = duplicates(sim)
    token = preview_move(client)["token"]
    assert token
    if change == "uidvalidity":
        sim.reset_uidvalidity("INBOX", renumber=False)
    elif change == "missing":
        del sim.messages[approved[-1].pid]
    elif change == "message_id":
        approved[-1].headers = approved[-1].headers.replace(b"before/abc", b"before/xyz")
    elif change == "date":
        approved[-1].headers = approved[-1].headers.replace(b"09:02:00", b"10:02:00")
    else:
        approved[-1].body += b"size changed"
    with pytest.raises(MailValidationError, match="changed|missing|not found"):
        client.move_to_folder(token)
    assert not mutations(sim)
    assert all(item.folder == "INBOX" for item in approved)
    # Even if a pre-mutation refusal preserves the capability, it must never
    # silently authorize a replacement selection on a subsequent attempt.
    with pytest.raises(MailValidationError):
        client.move_to_folder(token)
    assert not mutations(sim)


def test_new_duplicate_is_ignored_by_confirmation(duplicate_mailbox):
    sim, client = duplicate_mailbox()
    approved = duplicates(sim)
    token = preview_move(client)["token"]
    newcomer = duplicates(sim, count=1)[0]
    result = client.move_to_folder(token)
    assert result["count"] == 3 and not result["partial"]
    assert newcomer.folder == "INBOX"
    assert all(item.folder == DESTINATION for item in approved)


def test_identical_duplicates_with_ambiguous_label_membership_refuse(duplicate_mailbox):
    sim, client = duplicate_mailbox()
    approved = duplicates(sim, identical=True)
    approved[0].labels.add("Labels/Review")
    result = preview_move(client)
    assert result["token"] is None and result["excluded_ids"] == [DUPLICATE]
    assert result["excluded_reasons"] == {"ambiguous_label_membership": 1}
    assert not mutations(sim) and all(item.folder == "INBOX" for item in approved)


@pytest.mark.parametrize("folder", [DESTINATION, "Folders/O'Reilly", "Folders/ Review"])
def test_preexisting_destination_label_cannot_be_assigned_to_source_duplicate(
    duplicate_mailbox, folder
):
    sim, client = duplicate_mailbox(folders=(DESTINATION, "Folders/O'Reilly", "Folders/ Review"))
    approved = duplicates(sim, count=1, identical=True)[0]
    preexisting = duplicates(sim, count=1, identical=True, folder=folder)[0]
    preexisting.labels.add("Labels/Review")
    result = preview_move(client)
    assert result["token"] is None and result["excluded_ids"] == [DUPLICATE]
    assert result["excluded_reasons"] == {"ambiguous_label_membership": 1}
    assert not mutations(sim)
    assert approved.folder == "INBOX" and not approved.labels
    assert preexisting.folder == folder and preexisting.labels == {"Labels/Review"}


@pytest.mark.parametrize("identical", [False, True])
def test_duplicate_batch_crosses_execution_chunk_boundary(
    duplicate_mailbox, monkeypatch, identical
):
    # One batched chunk would hold all 51; smaller chunks keep the boundary.
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 21)
    sim, client = duplicate_mailbox()
    approved = duplicates(sim, 51, identical=identical)
    preview = preview_move(client)
    grant = client.cleanup.tokens.inspect(preview["token"], "move_to_folder")
    actual = observe_sim_mutations(sim, grant, operation="MOVE")
    token, results = preview["token"], []
    while token:
        result = client.move_to_folder(token)
        results.append(result)
        assert_bound_progress(client.cleanup, grant, results, actual)
        assert len(results) <= preview["estimated_calls"]
        token = result["continuation_token"]
    assert sum(result["done"] for result in results) == 51 and not result["partial"]
    assert all(item.folder == DESTINATION for item in approved)
    chunks = logged(client)
    assert len(chunks) > 1 and all(0 < len(chunk["messages"]) <= 21 for chunk in chunks)
    assert {entry["uid_after"] for chunk in chunks for entry in chunk["messages"]} == {
        str(sim.uid_of(DESTINATION, item.pid)) for item in approved
    }
    before_retry = mutations(sim)
    with pytest.raises(MailValidationError, match="already used"):
        client.move_to_folder(preview["token"])
    assert mutations(sim) == before_retry
    sim.hooks.remove(actual.hook)
    undo_sim_chunks(sim, client, chunks, approved)
    assert all(item.folder == "INBOX" for item in approved)


@pytest.mark.parametrize("identical", [False, True])
def test_rules_preview_apply_and_undo_duplicate_batch(duplicate_mailbox, tmp_path, identical):
    sim, client = duplicate_mailbox()
    approved = duplicates(sim, identical=identical)
    existing = duplicates(sim, count=1, identical=identical, folder=DESTINATION)[0]
    rules = rules_service(client, tmp_path)
    preview = rules.preview_rules()
    assert preview["count"] == 3 and preview["token"]
    result = rules.apply_rules(preview["token"])
    assert result["count"] == 3 and not result["partial"]
    assert all(item.folder == DESTINATION for item in approved)
    assert client.undo_action(result["log_ids"][0])["count"] == 3
    assert all(item.folder == "INBOX" for item in approved)
    assert existing.folder == DESTINATION


@pytest.mark.parametrize("copyuid", [False, True])
def test_rules_first_match_tracks_uids_not_shared_message_id(duplicate_mailbox, tmp_path, copyuid):
    sim, client = duplicate_mailbox(copyuid=copyuid)
    approved = duplicates(sim, count=2, identical=True)
    approved[0].headers = approved[0].headers.replace(b"Subject: Hello", b"Subject: First")
    rules = rules_service(client, tmp_path)
    rules.path.write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [
                    {"field": "subject", "contains": "First", "folder": DESTINATION},
                    {"field": "from", "contains": SENDER, "folder": "Folders/Keep"},
                ],
            }
        )
    )
    preview = rules.preview_rules()
    assert preview["count"] == 2 and [rule["count"] for rule in preview["rules"]] == [1, 1]
    result = rules.apply_rules(preview["token"])
    assert result["count"] == 2 and not result["partial"]
    assert approved[0].folder == DESTINATION
    assert all(item.folder == "Folders/Keep" for item in approved[1:])
    for log_id in result["log_ids"]:
        assert client.undo_action(log_id)["count"] > 0
    assert all(item.folder == "INBOX" for item in approved)


@pytest.mark.parametrize("copyuid", [False, True])
@pytest.mark.parametrize("new_count", [1, 3])
def test_timer_duplicates_above_mark_preserve_flags_and_backlog(
    duplicate_mailbox, tmp_path, copyuid, new_count
):
    sim, client = duplicate_mailbox(copyuid=copyuid)
    old = duplicates(sim, count=1, identical=True)[0]
    rules = rules_service(client, tmp_path)
    timer = RulesTimer(client.cleanup, rules_path=rules.path, state_path=tmp_path / "state.json")
    assert (
        baseline(SimpleNamespace(timer=timer, state=timer.state_path, sim=sim, old=old))["count"]
        == 0
    )
    approved = duplicates(sim, count=new_count, identical=True)
    approved[0].flags.add(r"\Seen")
    before = [set(item.flags) for item in approved]
    result = timer.run()
    assert result["count"] == new_count and old.folder == "INBOX"
    assert all(item.folder == DESTINATION for item in approved)
    assert [item.flags for item in approved] == before
    assert timer.run()["count"] == 0
    for log_id in result["log_ids"]:
        assert client.undo_action(log_id)["count"] == new_count
    assert [item.flags for item in approved] == before
    assert all(item.folder == "INBOX" for item in approved)
    assert all("UID MOVE " in item[3] for item in mutations(sim))


@pytest.mark.parametrize("copyuid", [False, True])
def test_undo_ambiguous_duplicate_fallback_never_picks_preexisting(duplicate_mailbox, copyuid):
    sim, client = duplicate_mailbox(copyuid=copyuid)
    approved = duplicates(sim, identical=True)
    preexisting = duplicates(sim, count=1, identical=True, folder=DESTINATION)[0]
    result = client.move_to_folder(preview_move(client)["token"])
    assert result["count"] == 3
    if copyuid:
        sim.reset_uidvalidity(DESTINATION)
    else:
        # Model a lost outcome after MOVE: the durable journal has no proven
        # destination handle, and independent activity added another exact copy.
        record = logged(client)[0]
        for entry in record["messages"]:
            entry["uid_after"] = entry["uidvalidity_after"] = None
        Path(client.settings.action_log_path).write_text(json.dumps(record) + "\n")
        duplicates(sim, count=1, identical=True, folder=DESTINATION)
    before = mutations(sim)
    undone = client.undo_action(result["log_ids"][0])
    assert undone["count"] == 0 and undone["partial"] and undone["missing"]
    assert mutations(sim) == before
    assert all(item.folder == DESTINATION for item in [*approved, preexisting])


@pytest.mark.parametrize("action", ["move_to_folder", "mark_read", "mark_unread"])
def test_omitted_folder_accepts_duplicates_only_in_default_inbox(duplicate_mailbox, action):
    sim, client = duplicate_mailbox()
    approved = duplicates(sim)
    params = {"folder": DESTINATION} if action == "move_to_folder" else None
    preview = client.preview_action(action, query={"from_": SENDER}, params=params)
    assert preview["token"] and preview["count"] == 3
    result = getattr(client, action)(preview["token"])
    assert result["count"] == 3 and not result["partial"]
    assert all(item.folder == (DESTINATION if params else "INBOX") for item in approved)


def test_bound_uidvalidity_refuses_before_fetch_independently_of_snapshot(
    duplicate_mailbox, monkeypatch
):
    sim, client = duplicate_mailbox()
    duplicates(sim)
    grant = client.cleanup.tokens.inspect(preview_move(client)["token"], "move_to_folder")
    sim.reset_uidvalidity("INBOX", renumber=False)

    def forbidden_fetch(*args, **kwargs):
        pytest.fail("Changed UIDVALIDITY must be refused before fetching obsolete UIDs")

    monkeypatch.setattr(client.cleanup, "_fetch", forbidden_fetch)
    # Exercise resolution directly: the whole-row approval digest never runs.
    with client._session() as (connection, _):
        deadline = client._deadline()
        folders = client._folders(connection)
        with pytest.raises(MailValidationError, match="UIDVALIDITY changed"):
            client.cleanup._resolve(
                connection,
                deadline,
                grant.selector,
                grant.action,
                grant.params,
                folders,
                bound_rows=grant.messages,
            )
    assert not mutations(sim)
    assert not any(command.startswith("SELECT ") for _, _, _, command in sim.commands)


@pytest.mark.parametrize("field", ["date", "size"])
def test_per_uid_fingerprint_refuses_independently_of_snapshot(duplicate_mailbox, field):
    from proton_mail_mcp.cleanup_imap import _UndoVerificationError

    sim, client = duplicate_mailbox()
    approved = duplicates(sim)
    grant = client.cleanup.tokens.inspect(preview_move(client)["token"], "move_to_folder")
    row = grant.messages[-1]
    if field == "date":
        approved[-1].headers = approved[-1].headers.replace(b"09:02:00", b"10:02:00")
    else:
        approved[-1].body += b"additional bytes"
    # Call the final per-UID boundary without the aggregate approval digest or
    # undo header digest, so Date and size each have to protect the actual MOVE.
    with client._session() as (connection, _):
        deadline = client._deadline()
        connection._cleanup_moved_handles = set()
        with pytest.raises(_UndoVerificationError, match="fingerprint changed"):
            client.cleanup.backend._perform(
                connection, deadline, "move_to_folder", DESTINATION, row, {}
            )
    assert not mutations(sim)
    assert all(item.folder == "INBOX" for item in approved)
