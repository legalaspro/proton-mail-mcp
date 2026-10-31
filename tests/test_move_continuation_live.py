"""The 94-row slow-peer approval: one batched call, or bound, productive continuations."""

import json
from pathlib import Path

import pytest
from action_records import execution_records as replay_records
from bridge_sim import Msg, hdr, mutations
from slow_cleanup import SlowCleanupPeer
from test_cleanup_mutations import uid_values

from proton_mail_mcp import mail
from proton_mail_mcp.guarded import EXECUTE_RESPONSE_BUDGET_S

ACTION = "move_to_folder"
TARGET = "Folders/Reviewed"


def identity(row):
    return tuple(row[key] for key in ("folder", "uidvalidity", "uid", "message_id"))


def journal_identity(row):
    return tuple(
        row[key] for key in ("source_folder", "uidvalidity_before", "uid_before", "message_id")
    )


def moved_uids(peer):
    return [uid for _, _, _, command in mutations(peer) for uid in uid_values(command.split()[2])]


@pytest.mark.parametrize("source", ["explicit", "implicit"])
def test_94_approved_rows_complete_in_one_batched_call(tmp_path, monkeypatch, source):
    # G-B: the operator's 94 approved rows on this synthetic slow peer used to
    # split 19/75 across calls. One batched UID MOVE now completes them at once.
    peer = SlowCleanupPeer(command_seconds=0.15, mutation_seconds=1.5)
    monkeypatch.setattr(mail, "time", peer.clock)
    items = peer.populate(94)
    client = peer.client(tmp_path, timeout=120)
    service = client.cleanup
    service.tokens.clock = peer.clock.monotonic
    query = {"from_": "notifications@example.invalid"}
    if source == "explicit":
        query["folder"] = "INBOX"
    try:
        preview = service.preview_action(ACTION, query=query, params={"folder": TARGET})
        assert preview["token"] and preview["count"] == 94
        assert preview["estimated_calls"] == 1
        approved = {
            identity(row) for row in service.tokens.inspect(preview["token"], ACTION).messages
        }
        command_offset = len(peer.commands)
        began = peer.clock.elapsed
        result = service.execute(ACTION, preview["token"])
        assert peer.clock.elapsed - began <= EXECUTE_RESPONSE_BUDGET_S
        assert result["done"] == result["run_done"] == 94 and not result["partial"]
        assert result["remaining"] == result["uncertain"] == 0
        assert result["stop_reason"] is None and result["continuation_token"] is None
        # No mailbox inventory: source reads name only approved UIDs.
        uids = {row[2] for row in approved}
        for _, folder, _, command in peer.commands[command_offset:]:
            assert not command.startswith("UID SEARCH")
            if folder == "INBOX" and command.startswith("UID FETCH"):
                assert set(command.split()[2].split(",")) <= uids
        assert len(mutations(peer)) == 1 and len(set(moved_uids(peer))) == 94
        assert {journal_identity(row) for row in result["messages"]} == approved
        assert all(item.folder == TARGET for item in items)
    finally:
        peer.close()
        assert not peer.errors and not peer.violations
        assert all(not worker.is_alive() for _, _, worker in peer.peers)


@pytest.mark.parametrize("source", ["explicit", "implicit-arrival", "slower-final-check"])
@pytest.mark.parametrize("held_token", ["latest", "original-lost-response"])
def test_94_approved_rows_resume_without_inventory_or_lost_capability(
    tmp_path, monkeypatch, source, held_token
):
    # All timings are synthetic. Each call is limited to 19 rows, the operator's
    # 19/75 first split; a later unrelated arrival makes old whole-inventory
    # fallback visible without assuming anything about an actual mailbox.
    peer = SlowCleanupPeer(command_seconds=0.15, mutation_seconds=1.5)
    monkeypatch.setattr(mail, "time", peer.clock)
    items = peer.populate(94)
    client = peer.client(tmp_path, timeout=120)
    service = client.cleanup
    service.tokens.clock = peer.clock.monotonic
    query = {"from_": "notifications@example.invalid"}
    if source != "implicit-arrival":
        query["folder"] = "INBOX"
    try:
        preview = service.preview_action(ACTION, query=query, params={"folder": TARGET})
        assert preview["token"] and preview["count"] == 94
        assert preview["estimated_calls"] == 1
        original = service.tokens.inspect(preview["token"], ACTION)
        approved = {identity(row): row for row in original.messages}
        moved, claims, chunks, verified, claimed_chunks = [], [], [], [], []
        original_claim = service.tokens.claim
        original_apply = service.backend.apply
        original_verify = service._verified_chunk

        def claim(token, action, **kwargs):
            granted = original_claim(token, action, **kwargs)
            claims.append({identity(row) for row in granted.messages})
            return granted

        def apply(connection, deadline, action, params, snapshots, **kwargs):
            before_mutation = kwargs["before_mutation"]

            def claimed_chunk():
                before_mutation()
                claimed_chunks.append({identity(row) for row in snapshots})

            kwargs["before_mutation"] = claimed_chunk
            result = original_apply(connection, deadline, action, params, snapshots, **kwargs)
            chunks.append(result["count"])
            return result

        def verify(connection, deadline, grant, expected, **kwargs):
            verified.append({identity(row) for row in expected})
            if len(verified) == 1:
                assert {identity(row) for row in expected} == set(approved)
            else:
                assert len(expected) <= 21
            return original_verify(connection, deadline, grant, expected, **kwargs)

        def capture(session, folder, operation, command):
            if operation == "MOVE":
                for uid in uid_values(command.split()[2]):
                    item = peer.messages[peer.uidmap[folder][int(uid)]]
                    moved.append((folder, str(peer.uidvalidity[folder]), uid, item.message_id()))

        monkeypatch.setattr(service.tokens, "claim", claim)
        monkeypatch.setattr(service.backend, "apply", apply)
        monkeypatch.setattr(service, "_verified_chunk", verify)
        peer.arrival_hook = capture
        token, results, durations, outsiders = preview["token"], [], [], []
        while token:
            pending = set(approved) - set(moved)
            previous_chunks = len(chunks)
            previous_claims = len(claims)
            command_offset = len(peer.commands)
            inventory_count = peer.inventory_commands
            began = peer.clock.elapsed
            result = service.execute(ACTION, token, limit=19)
            duration = peer.clock.elapsed - began
            results.append(result)
            durations.append(round(duration, 6))
            print(
                f"source={source} held={held_token} call={len(results)} "
                f"seconds={duration:.6f} done={result['done']} remaining={result['remaining']} "
                f"token={bool(result['continuation_token'])} "
                f"rich_bytes={len(json.dumps(result).encode())}"
            )
            assert duration <= EXECUTE_RESPONSE_BUDGET_S and duration < 50
            assert result["done"] > 0, "A continuation must complete at least one chunk"
            assert len(chunks) > previous_chunks and all(chunks[previous_chunks:])
            assert len(claims) == previous_claims + 1 and claims[-1] == pending
            assert not result["uncertain"] and not result["blocked"]
            assert result["approved_total"] == 94
            assert len(moved) == len(set(moved)) == sum(call["done"] for call in results)
            assert set(moved) <= set(approved)
            assert result["run_done"] == len(moved)
            assert result["remaining"] == len(set(approved) - set(moved))
            assert peer.inventory_commands == inventory_count
            assert not any(
                command.startswith("UID SEARCH") and "ALL" in command.split()
                for _, _, _, command in peer.commands[command_offset:]
            ), "Continuation repeated whole-mailbox UID enumeration"
            assert all(rows <= set(approved) for rows in verified)
            records = replay_records(
                [
                    json.loads(line)
                    for line in Path(client.settings.action_log_path).read_text().splitlines()
                ]
            )
            journaled = [row for record in records for row in record["messages"]]
            assert len(journaled) == len(moved) and all(
                row["status"] == "done" for row in journaled
            )
            assert {journal_identity(row) for row in journaled} == set(moved)
            assert set().union(*claimed_chunks) == set(moved)
            assert sum(len(chunk) for chunk in claimed_chunks) == len(journaled) == len(moved)
            assert {journal_identity(row) for row in result["messages"]} == (
                pending - (set(approved) - set(moved))
            )
            assert len(records) == len(chunks)
            assert len(mutations(peer)) == len(results)  # One batched MOVE per call.
            assert len(moved_uids(peer)) == len(moved)
            assert sum(item.folder == TARGET for item in items) == len(moved)
            continuation = result["continuation_token"]
            if result["remaining"]:
                assert continuation, "Remaining approved rows require a usable continuation"
                resumed = service.tokens.inspect(continuation, ACTION)
                assert {identity(row) for row in resumed.messages} == set(approved) - set(moved)
                assert all(row == approved[identity(row)] for row in resumed.messages)
                assert resumed.expires > original.expires
                assert resumed.expires_at == results[0]["expires_at"]
                assert resumed.fingerprint != original.fingerprint
                assert service.tokens.inspect(preview["token"], ACTION) == resumed
            else:
                assert continuation is None and not result["partial"]
            if len(results) == 1:
                assert result["done"] == 19
                assert result["remaining"] == 94 - result["done"]
                outsiders.append(
                    peer.add(
                        Msg(hdr("<new-unapproved@example.invalid>", from_="other@example.invalid"))
                    )
                )
                # A full inventory now costs 20 seconds per large mailbox. Bound
                # checks and a small UIDNEXT delta remain cheap on this server.
                peer.inventory_seconds = 20
                if source == "slower-final-check":
                    peer.command_latency = 0.22
                    peer.mutation_seconds = 1.2
            assert all(item.folder == "INBOX" for item in outsiders)
            assert len(results) < 12
            token = (
                preview["token"]
                if continuation and held_token == "original-lost-response"
                else continuation
            )
        assert len(results) > 1 and set(moved) == set(approved)
        assert sum(chunks) == sum(result["done"] for result in results) == len(journaled) == 94
        assert len({result["run_id"] for result in results}) == 1
        print(f"source={source} held={held_token} total=94 per_call_seconds={durations}")
    finally:
        peer.close()
        assert not peer.errors and not peer.violations
        assert all(not worker.is_alive() for _, _, worker in peer.peers)


@pytest.mark.parametrize("held_token", ["latest", "original-lost-response"])
def test_unavoidable_final_check_timeout_returns_explicit_error_and_usable_remainder(
    tmp_path, monkeypatch, held_token
):
    peer = SlowCleanupPeer(command_seconds=0.15, mutation_seconds=1.5)
    monkeypatch.setattr(mail, "time", peer.clock)
    items = peer.populate(94)
    client = peer.client(tmp_path, timeout=120)
    service = client.cleanup
    service.tokens.clock = peer.clock.monotonic
    try:
        preview = service.preview_action(
            ACTION,
            query={"folder": "INBOX", "from_": "notifications@example.invalid"},
            params={"folder": TARGET},
        )
        approved = service.tokens.inspect(preview["token"], ACTION)
        first = service.execute(ACTION, preview["token"], limit=19)
        assert first["done"] == 19 and first["continuation_token"]
        token = first["continuation_token"] if held_token == "latest" else preview["token"]
        pending = service.tokens.inspect(token, ACTION)
        before_mutations = list(mutations(peer))
        final_check = service.backend._final_batch_check
        injected = []

        def delayed_final_identity_read(connection, deadline, *args, **kwargs):
            original_metadata = client._metadata

            def metadata(*metadata_args, **metadata_kwargs):
                result = original_metadata(*metadata_args, **metadata_kwargs)
                # A real final UID fetch has run, but the read-only command
                # exhausts its watchdog allowance before authorization. Nothing
                # can safely mutate in this call, including a single-row chunk.
                if not injected:
                    injected.append(True)
                    peer.clock.elapsed = deadline.io_end + 0.01
                return result

            with monkeypatch.context() as patch:
                patch.setattr(client, "_metadata", metadata)
                return final_check(connection, deadline, *args, **kwargs)

        monkeypatch.setattr(service.backend, "_final_batch_check", delayed_final_identity_read)
        started = peer.clock.elapsed
        refusal = service.execute(ACTION, token)
        assert peer.clock.elapsed - started <= EXECUTE_RESPONSE_BUDGET_S
        assert injected and mutations(peer) == before_mutations
        assert refusal["done"] == refusal["uncertain"] == 0
        assert refusal["run_done"] == first["done"]
        assert refusal["remaining"] == len(pending.messages)
        assert refusal["error_code"] == "first_chunk_unavailable"
        assert "No approved chunk could complete" in refusal["error"]
        assert not refusal["available"] and not refusal["resume"]
        assert refusal["continuation_token"] is None
        assert not refusal["token_consumed"]
        assert service.tokens.inspect(token, ACTION) == pending
        assert all(row["status"] == "not_attempted" for row in refusal["messages"])
        monkeypatch.setattr(service.backend, "_final_batch_check", final_check)
        done = first["done"]
        attempts = 0
        while token:
            attempts += 1
            assert attempts <= 10
            started = peer.clock.elapsed
            result = service.execute(ACTION, token)
            assert peer.clock.elapsed - started <= EXECUTE_RESPONSE_BUDGET_S
            assert result["done"] > 0 and result["uncertain"] == 0
            done += result["done"]
            assert result["run_id"] == first["run_id"]
            assert result["run_done"] == done
            assert result["remaining"] == 94 - done
            token = result["continuation_token"]
            assert bool(token) is (result["remaining"] > 0)
            if token:
                resumed = service.tokens.inspect(token, ACTION)
                assert resumed.expires == pending.expires
                assert resumed.expires_at == first["expires_at"]
                assert {row["message_id"] for row in resumed.messages} == {
                    item.message_id() for item in items if item.folder == "INBOX"
                }
        raw = [
            json.loads(line)
            for line in Path(client.settings.action_log_path).read_text().splitlines()
        ]
        outcomes = [
            row
            for record in replay_records(raw)
            for row in record["messages"]
            if row["status"] != "not_attempted"
        ]
        assert len(outcomes) == len(moved_uids(peer)) == done == 94
        assert {journal_identity(row) for row in outcomes} == {
            identity(row) for row in approved.messages
        }
        assert all(row["status"] == "done" for row in outcomes)
        assert all(item.folder == TARGET for item in items)
    finally:
        peer.close()
        assert not peer.errors and not peer.violations
        assert all(not worker.is_alive() for _, _, worker in peer.peers)
