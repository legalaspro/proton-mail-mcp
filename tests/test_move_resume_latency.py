"""Certified source chunks avoid repeated mailbox inventories over real local sockets."""

import json
import socket
from pathlib import Path

import pytest
from action_records import execution_records as replay_records
from bridge_sim import Msg, hdr, mutations
from slow_cleanup import SlowCleanupPeer
from test_cleanup_mutations import uid_values

from proton_mail_mcp import guarded, mail
from proton_mail_mcp.guarded import EXECUTE_RESPONSE_BUDGET_S


def identity(row):
    return tuple(row[key] for key in ("folder", "uidvalidity", "uid", "message_id"))


@pytest.mark.parametrize("source", ["explicit", "implicit"])
@pytest.mark.parametrize("outside_count", [360, 420], ids=["mailbox-515", "mailbox-575"])
def test_certified_slow_server_verification_stays_proportional_to_chunk(
    tmp_path, monkeypatch, outside_count, source
):
    def forbidden(*args, **kwargs):
        pytest.fail("Synthetic regression requires local socket pairs")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    # Continuations of 50-message batched chunks, one per call (limit=50).
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
    # Preserve this continuation-focused preview's calibration through subject
    # narrowing; unrelated INBOX rows still test the exact approved UID bounds.
    # Sender-only source scan costs are measured by test_preview_speed.
    query = {
        "folder": "INBOX",
        "from_": "notifications@example.invalid",
        "subject": "Hello",
        "since": "2026-09-01",
    }
    if source == "implicit":
        query.pop("folder")
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
        verifies, preparations, chunks, moved = [], [], [], []
        unexpected_fetches, apply_searches = [], []
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
                if not fetched or not set(fetched) <= allowed.get(folder, set()):
                    unexpected_fetches.append((folder, fetched))
                served.setdefault(folder, set()).update(fetched)
            assert served == allowed, "Verification did not serve exactly the bound identities"
            return result

        def apply(connection, deadline, action, params, snapshots, **kwargs):
            started = peer.clock.elapsed
            command_offset = len(peer.commands)
            fetch_offset = len(peer.fetch_batches)
            initial_size = len(snapshots)
            allowed = {}
            for row in snapshots:
                for member in [row, *row.get("memberships", [])]:
                    allowed.setdefault(member["folder"], set()).add(
                        (str(member["uid"]), row["message_id"])
                    )
            before_chunk = kwargs["before_chunk"]

            def prepared():
                duration = peer.clock.elapsed - started
                preparations.append((initial_size, round(duration, 6)))
                before_chunk()

            kwargs["before_chunk"] = prepared
            result = original_apply(connection, deadline, action, params, snapshots, **kwargs)
            apply_searches.extend(
                command
                for _, _, _, command in peer.commands[command_offset:]
                if command.startswith("UID SEARCH")
            )
            # Outcome verification may read exactly the moved rows' new UIDs.
            for entry in result["messages"]:
                if entry.get("uid_after"):
                    allowed.setdefault(entry["destination_folder"], set()).add(
                        (entry["uid_after"], entry["message_id"])
                    )
            # Each FETCH names only this chunk's handles. The post-MOVE source
            # check names the moved UIDs and, finding them gone, returns nothing.
            for _, folder, _, command in peer.commands[command_offset:]:
                named = uid_values(command.split()[2]) if command.startswith("UID FETCH") else []
                if not set(named) <= {uid for uid, _ in allowed.get(folder, set())}:
                    unexpected_fetches.append((folder, command))
            for folder, fetched in peer.fetch_batches[fetch_offset:]:
                if not set(fetched) <= allowed.get(folder, set()):
                    unexpected_fetches.append((folder, fetched))
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
        resume_expiry = None
        while token:
            chunk_offset = len(chunks)
            started = peer.clock.elapsed
            result = service.execute("move_to_folder", token, limit=50)
            duration = peer.clock.elapsed - started
            results.append(result)
            assert result["expires_at"] == results[0]["expires_at"]
            durations.append(round(duration, 6))
            first_chunks.append(chunks[chunk_offset])
            print(
                f"source={source} mailbox={155 + outside_count} "
                f"call={len(results)} seconds={duration:.6f} done={result['done']} "
                f"remaining={result['remaining']} verifies={verifies} "
                f"preparations={preparations} searches_in_apply={len(apply_searches)} "
                f"chunks={chunks}"
            )
            assert not apply_searches, "Mutation preparation repeated a mailbox inventory"
            assert not unexpected_fetches, "Fetched identities outside the approved chunk"
            assert all(cost <= 0.03 + size * 0.004 for size, cost in preparations)
            assert duration <= EXECUTE_RESPONSE_BUDGET_S
            assert duration < 50
            assert {"partial", "run_id", "done", "remaining", "uncertain"} <= (result.keys())
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
            assert {(row["uid_before"], row["message_id"]) for row in logged} == {
                (row[2], row[3]) for row in moved
            }
            assert len(journal) == len(chunks)
            token = result["continuation_token"]
            if token:
                assert result["partial"] and result["resume"] and result["error"]
                resumed = service.tokens.inspect(token, "move_to_folder")
                assert {identity(row) for row in resumed.messages} == original - set(moved)
                if resume_expiry is None:
                    resume_expiry = resumed.expires
                    assert resume_expiry > grant.expires
                assert resumed.expires == resume_expiry
                assert resumed.expires_at == result["expires_at"]
            else:
                assert not result["partial"] and not result["resume"]
            assert len(results) <= preview["estimated_calls"]
            # A returned call has no deferred mutation work. All commands and
            # source membership already agree with the durable result.
            assert len(mutations(peer)) == len(results)  # One batched MOVE per call.
            assert sum(item.folder == "Folders/Reviewed" for item in approved) == len(moved)
        assert len(results) > 1 and len(moved) == 155 and set(moved) == original
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
        assert min(first_chunks[:-1]) >= max(first_chunks[:-1]) - 1
        assert grant.verification_seconds < 0.02, "One-off inventory inflated per-row verification"
        print(
            f"preview_seconds={preview_duration:.6f} estimated_calls={preview['estimated_calls']} "
            f"actual_calls={len(results)} call_seconds={durations} "
            f"verification_size_seconds_searches={verifies} "
            f"preparation_size_seconds={preparations} mutation_chunks={chunks}"
        )
    finally:
        peer.close()
        assert not peer.errors and not peer.violations
        assert all(not worker.is_alive() for _, _, worker in peer.peers)
