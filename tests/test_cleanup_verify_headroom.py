"""Slow confirmation stays within one approval using only local socket peers."""

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from action_records import execution_log
from action_records import execution_records as replay_records
from bridge_sim import Msg, hdr, mutations
from slow_cleanup import SlowCleanupPeer
from test_cleanup_mutations import uid_values

from proton_mail_mcp import mail
from proton_mail_mcp.mail import MailValidationError


@pytest.fixture
def headroom_peer(tmp_path, monkeypatch):
    peers = []

    def forbid_network(*args, **kwargs):
        pytest.fail("Headroom checks require local socket pairs")

    monkeypatch.setattr(socket, "create_connection", forbid_network)

    def build(*, inventory_seconds=9.3, count=140, mutation_seconds=1.5, timeout=120):
        peer = SlowCleanupPeer(mutation_seconds=mutation_seconds)
        peer.inventory_seconds = inventory_seconds
        peers.append(peer)
        monkeypatch.setattr(mail, "time", peer.clock)
        items = peer.populate(count)
        outside = [
            peer.add(
                Msg(
                    hdr(
                        f"<outside-{index}@example.invalid>",
                        from_="other@example.invalid",
                        subject="Unrelated",
                    )
                )
            )
            for index in range(360)
        ]
        client = peer.client(tmp_path / str(len(peers)), timeout=timeout)
        client.cleanup.tokens.clock = peer.clock.monotonic
        return SimpleNamespace(
            peer=peer,
            client=client,
            service=client.cleanup,
            items=items,
            outside=outside,
            clock=peer.clock,
            path=Path(client.settings.action_log_path),
            uids={str(peer.uid_of("INBOX", item.pid)) for item in items},
        )

    yield build
    for peer in peers:
        peer.close()
        assert not peer.errors and not peer.violations


def approve(case):
    started = case.clock.elapsed
    preview = case.service.preview_action(
        "move_to_folder",
        # Keep this execution-headroom calibration's source candidates bounded
        # while unrelated INBOX rows still participate in membership discovery.
        # Local-only sender queries' full source costs have separate speed tests.
        query={
            "folder": "INBOX",
            "from_": "notifications@example.invalid",
            "subject": "Hello",
            "since": "2026-09-01",
        },
        params={"folder": "Folders/Reviewed"},
    )
    elapsed = case.clock.elapsed - started
    print(
        f"preview_seconds={elapsed:.3f} inventories={case.peer.inventory_commands} "
        f"estimated_calls={preview.get('estimated_calls')}"
    )
    assert preview["token"] and preview["count"] == len(case.items)
    case.preview_seconds = elapsed
    return preview


def records(case):
    return replay_records([json.loads(line) for line in case.path.read_text().splitlines()])


@pytest.mark.parametrize("trigger", ["post_preflight", "inventory"])
def test_slow_140_move_first_call_progress(headroom_peer, trigger, monkeypatch):
    case = headroom_peer()
    preview = approve(case)
    assert 40 <= case.preview_seconds <= 60
    assert case.client.settings.execute_timeout == 120
    approved_ids = {item.message_id() for item in case.items}
    approved_handles = {
        (row["folder"], row["uidvalidity"], row["uid"], row["message_id"])
        for row in case.service.tokens.inspect(preview["token"], "move_to_folder").messages
    }
    original_verify = case.service._verified_chunk
    verified_commands = []
    verification_durations = []

    def verify(connection, deadline, grant, expected, **kwargs):
        start = len(case.peer.commands)
        verification_started = case.clock.elapsed
        previous_cost = case.peer.inventory_seconds
        if trigger == "inventory":
            case.peer.inventory_seconds = 49
        try:
            result = original_verify(connection, deadline, grant, expected, **kwargs)
        finally:
            case.peer.inventory_seconds = previous_cost
            verification_durations.append(case.clock.elapsed - verification_started)
        assert case.clock.elapsed - verification_started < case.preview_seconds, (
            "Bound verification spent a full inventory's simulated time"
        )
        commands = case.peer.commands[start:]
        # Every execute verification command runs through real IMAP4.uid.
        # The approved source and membership handles bound all fetched UIDs.
        permitted = {}
        for row in expected:
            for member in [row, *row.get("memberships", [])]:
                permitted.setdefault(member["folder"], set()).add(str(member["uid"]))
        for _, folder, _, command in commands:
            assert not command.startswith("UID SEARCH"), "Verification repeated an inventory"
            if command.startswith("UID FETCH"):
                actual = {
                    str(uid)
                    for uid in case.peer._uids(command.split()[2], list(case.peer.uidmap[folder]))
                }
                assert actual and actual <= permitted.get(folder, set())
        verified_commands.extend(commands)
        return result

    monkeypatch.setattr(case.service, "_verified_chunk", verify)
    original = case.service.backend._preflight

    def preflight(*args, **kwargs):
        result = original(*args, **kwargs)
        case.clock.elapsed += 0.5 if trigger == "post_preflight" else 0
        return result

    monkeypatch.setattr(case.service.backend, "_preflight", preflight)
    arrivals = []

    def arrive(session, folder, operation, command):
        if operation != "MOVE":
            return None
        raw = [json.loads(line) for line in case.path.read_text().splitlines()]
        assert any(row["action"] == "run_start" for row in raw)
        intents = {
            row["uid_before"]
            for record in replay_records(raw)
            for row in record["messages"]
            if row["status"] == "uncertain"
        }
        assert set(uid_values(command.split()[2])) <= intents, (
            "Every mutation requires its frozen chunk journal first"
        )
        if not arrivals:
            arrivals.append(
                case.peer.add(
                    Msg(hdr("<arrival@example.invalid>", from_="notifications@example.invalid"))
                )
            )
        return None

    case.peer.arrival_hook = arrive
    token = preview["token"]
    results = []
    for _ in range(preview["estimated_calls"]):
        started = case.clock.elapsed
        try:
            result = case.service.execute("move_to_folder", token)
        finally:
            # Assert outside the mailbox session, which sanitizes unexpected
            # callback exceptions. Preserve the actual simulated timing failure.
            assert max(verification_durations, default=0) < case.preview_seconds, (
                "Bound verification spent a full inventory's simulated time"
            )
        results.append(result)
        assert case.clock.elapsed - started < 115
        assert result["done"] > 0
        before = list(mutations(case.peer))
        if result["resume"]:
            retry = case.service.tokens.inspect(token, "move_to_folder")
            continuation = case.service.tokens.inspect(
                result["continuation_token"], "move_to_folder"
            )
            assert retry.messages == continuation.messages
            assert len(retry.messages) == result["remaining"]
            completed = {
                (
                    row["source_folder"],
                    row["uidvalidity_before"],
                    row["uid_before"],
                    row["message_id"],
                )
                for record in records(case)
                for row in record["messages"]
                if row["status"] == "done"
            }
            assert len(completed) == len(before) == sum(row["done"] for row in results)
            assert {
                (row["folder"], row["uidvalidity"], row["uid"], row["message_id"])
                for row in retry.messages
            } == approved_handles - completed
        else:
            with pytest.raises(MailValidationError, match="already used"):
                case.service.execute("move_to_folder", token)
        assert mutations(case.peer) == before
        if not result["resume"]:
            break
        token = result["continuation_token"]
    print(
        f"trigger={trigger} calls={len(results)} first_done={results[0]['done']} "
        f"done={sum(row['done'] for row in results)}"
    )
    assert verified_commands
    assert sum(row["done"] for row in results) == result["run_done"] == 140
    assert result["remaining"] == result["uncertain"] == 0 and not result["partial"]
    assert len(results) <= preview["estimated_calls"]
    assert all(item.folder == "Folders/Reviewed" for item in case.items)
    assert all(item.folder == "INBOX" for item in case.outside + arrivals)
    assert {
        uid for _, _, _, command in mutations(case.peer) for uid in uid_values(command.split()[2])
    } == case.uids
    journal = records(case)
    assert sum(len(record["messages"]) for record in journal) == 140
    assert {row["message_id"] for record in journal for row in record["messages"]} == approved_ids
    assert {row["uid_before"] for record in journal for row in record["messages"]} == case.uids
    assert all(row["status"] == "done" for record in journal for row in record["messages"])
    run = case.client.list_actions(token=preview["token"])["runs"][0]
    assert run["status"] == "completed" and run["done"] == run["approved_total"] == 140
    assert run["remaining"] == 0
    assert set(run["log_ids"]) == {record["log_id"] for record in journal}


@pytest.mark.parametrize(
    "change",
    ["moved", "uidvalidity", "message_id", "duplicate_identity", "fingerprint", "protected"],
)
def test_changed_bound_uid_refused_before_claim_or_journal(headroom_peer, monkeypatch, change):
    case = headroom_peer(count=4, inventory_seconds=0, mutation_seconds=0)
    preview = approve(case)
    pending = case.items[0]
    if change == "moved":
        pending.folder = "Archive"
        case.peer.sync()
    elif change == "uidvalidity":
        case.peer.uidvalidity["INBOX"] += 1
    elif change == "message_id":
        pending.headers = pending.headers.replace(b"approved-000", b"changed--000")
    elif change == "duplicate_identity":
        pending.headers = b"Message-ID: <duplicate@example.invalid>\r\n" + pending.headers
    elif change == "fingerprint":
        pending.headers = pending.headers.replace(b"Subject: Hello", b"Subject: Changed")
    else:
        pending.headers = pending.headers.replace(
            b"notifications@example.invalid", b"person@gmail.com"
        )
    claims = []
    original = case.service.tokens.claim

    def claim(*args, **kwargs):
        claims.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(case.service.tokens, "claim", claim)
    with pytest.raises(MailValidationError):
        case.service.execute("move_to_folder", preview["token"])
    assert not claims and not mutations(case.peer) and not execution_log(case.path)
    case.service.tokens.inspect(preview["token"], "move_to_folder")


@pytest.mark.parametrize("change", ["moved", "message_id", "fingerprint", "protected"])
def test_changed_remaining_bound_uid_is_blocked_and_run_stays_partial(headroom_peer, change):
    case = headroom_peer(count=18, inventory_seconds=0, mutation_seconds=2, timeout=24)
    preview = approve(case)
    # One batched chunk would finish all 18; a per-call limit leaves a remainder.
    first = case.service.execute("move_to_folder", preview["token"], limit=5)
    assert first["resume"] and first["done"] == 5
    expiry = case.service.tokens.inspect(first["continuation_token"], "move_to_folder").expires
    pending = next(item for item in case.items if item.folder == "INBOX")
    pending_uid = str(case.peer.uid_of("INBOX", pending.pid))
    if change == "moved":
        pending.folder = "Archive"
        case.peer.sync()
    elif change == "message_id":
        pending.headers = pending.headers.replace(b"approved-", b"replaced-")
    elif change == "fingerprint":
        pending.headers = pending.headers.replace(b"Subject: Hello", b"Subject: Changed")
    else:
        pending.headers = pending.headers.replace(
            b"notifications@example.invalid", b"person@gmail.com"
        )
    before = len(mutations(case.peer))
    token = first["continuation_token"]
    results = [first]
    for _ in range(18):
        assert case.service.tokens.inspect(token, "move_to_folder").expires == expiry
        result = case.service.execute("move_to_folder", token)
        results.append(result)
        with pytest.raises(MailValidationError, match="already used"):
            case.service.execute("move_to_folder", token)
        if not result["resume"]:
            break
        token = result["continuation_token"]
    assert sum(row["done"] for row in results) == result["run_done"] == 17
    assert result["remaining"] == 1 and result["partial"] and not result["resume"]
    assert any(row["blocked"] for row in results)
    assert all(
        pending_uid not in uid_values(command.split()[2])
        for _, _, _, command in mutations(case.peer)[before:]
    )
    assert all(item.folder == "Folders/Reviewed" for item in case.items if item is not pending)
    run = case.client.list_actions(token=preview["token"])["runs"][0]
    assert run["status"] == "partial" and run["done"] == 17 and run["remaining"] == 1


def test_new_label_membership_refused_before_claim_or_mutation(headroom_peer, monkeypatch):
    case = headroom_peer(count=4, inventory_seconds=0, mutation_seconds=0)
    case.peer.user_labels.append("Labels/Review")
    case.peer.sync()
    preview = approve(case)
    case.items[0].labels.add("Labels/Review")
    case.peer.sync()
    monkeypatch.setattr(
        case.service.tokens, "claim", lambda *a, **kw: pytest.fail("Changed row was claimed")
    )
    with pytest.raises(MailValidationError):
        case.service.execute("move_to_folder", preview["token"])
    assert not mutations(case.peer) and not execution_log(case.path)
    case.service.tokens.inspect(preview["token"], "move_to_folder")


def test_continuation_label_change_blocks_remaining_and_keeps_run_partial(headroom_peer):
    case = headroom_peer(count=18, inventory_seconds=0, mutation_seconds=2, timeout=24)
    case.peer.user_labels.append("Labels/Review")
    case.peer.sync()
    preview = approve(case)
    first = case.service.execute("move_to_folder", preview["token"], limit=5)
    assert first["resume"] and first["done"] == 5
    pending = next(item for item in case.items if item.folder == "INBOX")
    pending.labels.add("Labels/Review")
    case.peer.sync()
    before = list(mutations(case.peer))
    token = first["continuation_token"]
    result = case.service.execute("move_to_folder", token)
    assert mutations(case.peer) == before
    assert result["done"] == 0 and result["run_done"] == first["done"]
    assert result["remaining"] == len(result["blocked"]) == 18 - first["done"]
    assert result["partial"] and result["token_consumed"] and not result["resume"]
    assert result["continuation_token"] is None
    with pytest.raises(MailValidationError, match="already used"):
        case.service.execute("move_to_folder", token)
    assert mutations(case.peer) == before
    run = case.client.list_actions(token=preview["token"])["runs"][0]
    assert run["status"] == "partial" and run["done"] == first["done"]
    assert run["remaining"] == run["blocked"] == 18 - first["done"]
