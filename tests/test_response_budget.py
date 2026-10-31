"""Slow local IMAP execution stays bounded without repeated mailbox inventories."""

import json
import socket
from pathlib import Path

import pytest
from action_records import execution_records as replay_records
from bridge_sim import Msg, hdr, mutations
from slow_cleanup import SlowCleanupPeer
from test_cleanup_mutations import uid_values

from proton_mail_mcp import guarded, mail


def identity(row):
    return tuple(row[key] for key in ("folder", "uidvalidity", "uid", "message_id"))


@pytest.mark.parametrize(
    "explicit_source", [True, False], ids=["explicit-source", "implicit-source"]
)
@pytest.mark.parametrize("outside_count", [360, 420], ids=["mailbox-515", "mailbox-575"])
def test_live_shape_returns_every_call_before_fifty_seconds(
    tmp_path, monkeypatch, explicit_source, outside_count
):
    def forbidden(*args, **kwargs):
        pytest.fail("Synthetic regression requires local socket pairs")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    # This synthetic peer charges a fixed inventory cost for any FETCH of more
    # than 50 rows; 50-message batched chunks keep chunk reads outside it.
    monkeypatch.setattr(guarded, "EXECUTE_BATCH_SIZE", 50)
    peer = SlowCleanupPeer(mutation_seconds=1.5)
    peer.inventory_seconds = 9.3
    monkeypatch.setattr(mail, "time", peer.clock)
    approved = peer.populate(155)
    outside = [
        peer.add(
            Msg(
                hdr(
                    f"<outside-{i}@example.invalid>",
                    from_="other@example.invalid",
                    subject="Unrelated",
                )
            )
        )
        for i in range(outside_count)
    ]
    client = peer.client(tmp_path, timeout=120)
    service = client.cleanup
    service.tokens.clock = peer.clock.monotonic
    # Bound this execution-focused preview through its subject while retaining
    # unrelated source UIDs for every approval and mutation safety assertion.
    # Sender-only source scanning costs are covered by test_preview_speed.
    query = {
        "from_": "notifications@example.invalid",
        "subject": "Hello",
        "since": "2026-09-01",
    }
    if explicit_source:
        query["folder"] = "INBOX"
    try:
        began = peer.clock.elapsed
        preview = service.preview_action(
            "move_to_folder", query=query, params={"folder": "Folders/Reviewed"}
        )
        preview_duration = peer.clock.elapsed - began
        assert preview["token"] and preview["count"] == 155
        assert 40 <= preview_duration <= 60
        grant = service.tokens.inspect(preview["token"], "move_to_folder")
        original = {identity(row) for row in grant.messages}
        verifies, chunks, moved = [], [], []
        original_verify = service._verified_chunk
        original_apply = service.backend.apply

        def verify(connection, deadline, grant, expected, **kwargs):
            started = peer.clock.elapsed
            command_offset = len(peer.commands)
            fetch_offset = len(peer.fetch_batches)
            result = original_verify(connection, deadline, grant, expected, **kwargs)
            commands = peer.commands[command_offset:]
            duration = peer.clock.elapsed - started
            searches = sum(command.startswith("UID SEARCH") for _, _, _, command in commands)
            verifies.append((len(expected), round(duration, 6), searches))
            allowed = {}
            for row in expected:
                for member in [row, *row.get("memberships", [])]:
                    allowed.setdefault(member["folder"], set()).add(
                        (str(member["uid"]), row["message_id"])
                    )
            served = {}
            for folder, fetched in peer.fetch_batches[fetch_offset:]:
                assert fetched and set(fetched) <= allowed.get(folder, set())
                served.setdefault(folder, set()).update(fetched)
            assert served == allowed, "Verification must read exactly the bound UID identities"
            return result

        def apply(connection, deadline, action, params, snapshots, **kwargs):
            result = original_apply(connection, deadline, action, params, snapshots, **kwargs)
            chunks.append(result["count"])
            return result

        def capture_identity(session, folder, operation, command):
            if operation == "MOVE":
                for uid in uid_values(command.split()[2]):
                    item = peer.messages[peer.uidmap[folder][int(uid)]]
                    moved.append((folder, str(peer.uidvalidity[folder]), uid, item.message_id()))

        peer.arrival_hook = capture_identity
        monkeypatch.setattr(service, "_verified_chunk", verify)
        monkeypatch.setattr(service.backend, "apply", apply)
        token, results, durations, first_chunks = preview["token"], [], [], []
        while token:
            chunk_offset = len(chunks)
            command_offset = len(peer.commands)
            started = peer.clock.elapsed
            result = service.execute("move_to_folder", token)
            duration = peer.clock.elapsed - started
            assert duration < 50, "Every execute call must return before the client timeout"
            assert not any(
                command.startswith("UID SEARCH")
                for _, _, _, command in peer.commands[command_offset:]
            )
            results.append(result)
            durations.append(round(duration, 6))
            first_chunks.append(chunks[chunk_offset])
            print(
                f"mailbox={155 + outside_count} explicit_source={explicit_source} "
                f"call={len(results)} seconds={duration:.6f} done={result['done']} "
                f"remaining={result['remaining']} verifies={verifies} chunks={chunks}"
            )
            assert {"partial", "run_id", "done", "remaining", "uncertain", "stop_reason"} <= (
                result.keys()
            )
            assert result["done"] > 0 and result["uncertain"] == 0
            assert len(moved) == len(set(moved)) == sum(call["done"] for call in results)
            assert set(moved) <= original
            assert result["remaining"] == len(original - set(moved))
            journal = replay_records(
                [
                    json.loads(line)
                    for line in Path(client.settings.action_log_path).read_text().splitlines()
                ]
            )
            logged = [row for chunk in journal for row in chunk["messages"]]
            assert len(logged) == len(moved) and all(row["status"] == "done" for row in logged)
            assert {
                (
                    row["source_folder"],
                    row["uidvalidity_before"],
                    row["uid_before"],
                    row["message_id"],
                )
                for row in logged
            } == set(moved)
            assert {
                (
                    row["source_folder"],
                    row["uidvalidity_before"],
                    row["uid_before"],
                    row["message_id"],
                )
                for call in results
                for row in call["messages"]
            } == set(moved)
            assert len(journal) == len(chunks)
            destination = "Folders/Reviewed"
            assert {
                (str(peer.uidvalidity[destination]), str(uid), peer.messages[pid].message_id())
                for uid, pid in peer.uidmap[destination].items()
            } == {(row["uidvalidity_after"], row["uid_after"], row["message_id"]) for row in logged}
            assert {
                ("INBOX", str(peer.uidvalidity["INBOX"]), str(uid), peer.messages[pid].message_id())
                for uid, pid in peer.uidmap["INBOX"].items()
                if pid in {item.pid for item in approved}
            } == original - set(moved)
            token = result["continuation_token"]
            if token:
                assert result["partial"] and result["resume"] and result["stop_reason"]
                resumed = service.tokens.inspect(token, "move_to_folder")
                assert {identity(row) for row in resumed.messages} == original - set(moved)
                assert resumed.expires > grant.expires
                assert resumed.expires_at == results[0]["expires_at"]
            else:
                assert not result["partial"] and not result["resume"]
            assert len(results) <= preview["estimated_calls"]
            # A returned call has no deferred mutation work. All commands and
            # source membership already agree with the durable result.
            assert sum(
                len(uid_values(command.split()[2])) for _, _, _, command in mutations(peer)
            ) == len(moved)
            assert sum(item.folder == "Folders/Reviewed" for item in approved) == len(moved)
        # Batched 50-message chunks now finish the approval in one bounded call.
        assert len(results) == 1 and len(moved) == 155 and set(moved) == original
        assert chunks == [50, 50, 50, 5]
        assert len({result["run_id"] for result in results}) == 1
        assert all(item.folder == "INBOX" for item in outside)
        assert all(searches == 0 for _, _, searches in verifies)
        # A fixed command overhead plus per-row metadata work accounts for every
        # verification, including the whole initial approval, for both mailbox
        # sizes and every chunk index. Individual FETCHes remain bounded.
        assert all(duration <= 0.02 + size * 0.008 for size, duration, _ in verifies)
        by_size = {}
        for size, duration, _ in verifies:
            by_size.setdefault(size, []).append(duration)
        assert all(max(values) - min(values) < 0.002 for values in by_size.values())
        assert first_chunks == [50]
        assert grant.verification_seconds < 0.02, "One-off inventory inflated per-row verification"
        print(
            f"preview_seconds={preview_duration:.6f} estimated_calls={preview['estimated_calls']} "
            f"actual_calls={len(results)} call_seconds={durations} "
            f"verification_size_seconds_searches={verifies} mutation_chunks={chunks}"
        )
        assert all(duration < 50 for duration in durations)
    finally:
        peer.close()
        assert not peer.errors and not peer.violations
        assert all(not worker.is_alive() for _, _, worker in peer.peers)
