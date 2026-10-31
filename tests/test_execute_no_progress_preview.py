"""Preview exclusions and slow membership discovery must still permit execution."""

import json
import math
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from action_records import execution_log
from action_records import execution_records as replay_records
from bridge_costs import CommandClock, WorkCounters
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations
from test_cleanup_mutations import uid_values

from proton_mail_mcp import mail
from proton_mail_mcp.guarded import (
    EXECUTE_LOG_RESERVE_SECONDS,
    EXECUTE_MUTATION_SECONDS_PER_MESSAGE,
    EXECUTE_RESPONSE_BUDGET_S,
)
from proton_mail_mcp.mail import MailValidationError

SENDER = "notifications@github.com"
DESTINATION = "Folders/Reviewed"
QUERY = {"folder": "INBOX", "from_": SENDER, "before": "2026-10-01"}


class MembershipPreviewPeer(BridgeSim):
    """Charge only the first All Mail inventory, with optional missing certificates."""

    def __init__(self, *, membership_seconds=0.0, label_uidnext=True):
        clock = CommandClock()
        super().__init__(clock=clock, command_latency=0.001, work=WorkCounters(clock))
        self.membership_seconds = membership_seconds
        self.label_uidnext = label_uidnext
        self.membership_delays = []
        self.hooks.append(self._membership_delay)

    def _membership_delay(self, session, folder, operation, command):
        if (
            folder == "All Mail"
            and operation == "FETCH"
            and "ENVELOPE" in command
            and not self.membership_delays
        ):
            self.clock.elapsed += self.membership_seconds
            self.membership_delays.append(self.membership_seconds)

    def handle(self, tag, operation, uid_operation, words, session, command):
        response = super().handle(tag, operation, uid_operation, words, session, command)
        response = response.replace(b'INTERNALDATE "17-Oct-2026', b'INTERNALDATE "20-Sep-2026')
        if not self.label_uidnext and session.selected and session.selected.startswith("Labels/"):
            response = re.sub(rb"\* OK \[UIDNEXT [0-9]+\][^\r\n]*\r\n", b"", response)
        return response


@pytest.fixture
def github_preview_peer(tmp_path, monkeypatch):
    peers = []

    def build(**options):
        peer = MembershipPreviewPeer(**options)
        peers.append(peer)
        monkeypatch.setattr(mail, "time", peer.clock)
        sources = [
            peer.add(
                Msg(
                    hdr(
                        f"<sample-owner/sample-repo/pull/{index}/before/aaa/after/bbb@github.com>",
                        from_=SENDER,
                        date="Sun, 20 Sep 2026 09:00:00 +0000",
                    )
                )
            )
            for index in range(1, 36)
        ]
        duplicate = peer.add(
            Msg(
                sources[0].headers.replace(SENDER.encode(), b"GitHub <notifications@github.com>"),
                folder=None,
            )
        )
        client = make_client(peer, tmp_path / str(len(peers)), call_timeout=120)
        client.cleanup.tokens.clock = peer.clock.monotonic
        return SimpleNamespace(
            peer=peer,
            client=client,
            service=client.cleanup,
            sources=sources,
            duplicate=duplicate,
            path=Path(client.settings.action_log_path),
        )

    yield build
    for peer in peers:
        peer.close()
        assert not peer.errors and not peer.violations
        assert all(not worker.is_alive() for _, _, worker in peer.peers)


def approve(case):
    started = case.peer.clock.elapsed
    preview = case.client.preview_action(
        "move_to_folder", query=QUERY, params={"folder": DESTINATION}, limit=30
    )
    assert preview["count"] == 30 and preview["token"] and not preview["blocked"]
    assert preview["excluded_count"] == 1
    assert preview["excluded_ids"] == [case.sources[0].message_id()]
    assert preview["excluded_reasons"] == {"inconsistent_sender_headers": 1}
    grant = case.service.tokens.inspect(preview["token"], "move_to_folder")
    assert [row["message_id"] for row in grant.messages] == [
        item.message_id() for item in case.sources[1:31]
    ]
    assert not mutations(case.peer)
    return preview, grant, case.peer.clock.elapsed - started


def execute_clean_approval(case, preview):
    token = preview["token"]
    results = []
    while token:
        before = case.peer.clock.elapsed
        result = case.client.move_to_folder(token)
        results.append(result)
        print(
            f"execute_call={len(results)} seconds={case.peer.clock.elapsed - before:.6f} "
            f"done={result['done']} remaining={result['remaining']} "
            f"token_consumed={result['token_consumed']} stop_reason={result['stop_reason']}"
        )
        assert result["done"] > 0, (
            "An unchanged approval must never return a no-progress continuation"
        )
        assert result["uncertain"] == 0 and not result["blocked"]
        assert result["log_ids"], "Every successful execute call must journal its moved chunk"
        assert case.peer.clock.elapsed - before < EXECUTE_RESPONSE_BUDGET_S
        token = result["continuation_token"]
        assert len(results) <= 3
    assert sum(result["done"] for result in results) == 30
    assert result["run_done"] == 30 and result["remaining"] == 0 and not result["partial"]
    assert all(item.folder == DESTINATION for item in case.sources[1:31])
    assert all(
        item.folder == "INBOX" and not item.flags for item in [case.sources[0], *case.sources[31:]]
    )
    assert case.duplicate.folder is None and not case.duplicate.flags
    journal = replay_records([json.loads(line) for line in case.path.read_text().splitlines()])
    rows = [row for chunk in journal for row in chunk["messages"]]
    assert len(rows) == 30 and all(row["status"] == "done" for row in rows)
    assert {row["message_id"] for row in rows} == {item.message_id() for item in case.sources[1:31]}
    # One batched UID MOVE names all 30 approved UIDs.
    assert len(mutations(case.peer)) == 1
    assert len(uid_values(mutations(case.peer)[0][3].split()[2])) == 30
    return results


def test_github_35_candidates_exclusion_limit_30_executes_clean_approval(github_preview_peer):
    case = github_preview_peer()
    preview, _, elapsed = approve(case)
    print(f"github_candidates=35 limit=30 excluded=1 preview_seconds={elapsed:.6f}")
    execute_clean_approval(case, preview)


def test_slow_membership_preview_does_not_deny_first_execute_chunk(
    github_preview_peer, monkeypatch
):
    # Missing label UIDNEXT is an ordinary strict-discovery fallback. The
    # synthetic pause represents a one-off slow preview, not slow mutations.
    case = github_preview_peer(membership_seconds=44, label_uidnext=False)
    preview, grant, elapsed = approve(case)
    assert 44 <= elapsed < 45
    assert not grant.bounded_recovery
    assert all("label_states" not in row for row in grant.messages)
    verifications = []
    verify = case.service._verified_chunk

    def observed_verify(*args, **kwargs):
        started = case.peer.clock.elapsed
        result = verify(*args, **kwargs)
        verifications.append(case.peer.clock.elapsed - started)
        return result

    monkeypatch.setattr(case.service, "_verified_chunk", observed_verify)
    old_available = (
        EXECUTE_RESPONSE_BUDGET_S
        - min(5, EXECUTE_RESPONSE_BUDGET_S * 0.1)
        - EXECUTE_LOG_RESERVE_SECONDS
    )
    old_first_capacity = max(
        0, math.floor((old_available - elapsed) / EXECUTE_MUTATION_SECONDS_PER_MESSAGE)
    )
    assert old_first_capacity == 0
    print(
        f"preview_seconds={elapsed:.6f} inventory_seconds={grant.inventory_seconds:.6f} "
        f"verification_seconds={grant.verification_seconds:.6f} "
        f"old_available={old_available:.6f} old_first_capacity={old_first_capacity} "
        f"bounded_recovery={grant.bounded_recovery}"
    )
    try:
        execute_clean_approval(case, preview)
    finally:
        print(f"verification_calls={len(verifications)} verification_seconds={verifications}")
    assert verifications and max(verifications) < 1


@pytest.mark.parametrize("limit", [None, 1])
def test_changed_last_approved_row_refuses_whole_batch_before_first_chunk(
    github_preview_peer, monkeypatch, limit
):
    case = github_preview_peer()
    preview, grant, _ = approve(case)
    # The final approved row lies beyond both a one-row execute request and
    # the ordinary first chunk. It must be checked before either can mutate.
    case.sources[30].headers = case.sources[30].headers.replace(
        b"Subject: Hello", b"Subject: Changed"
    )
    claims = []
    claim = case.service.tokens.claim

    def observed_claim(*args, **kwargs):
        claims.append(args)
        return claim(*args, **kwargs)

    monkeypatch.setattr(case.service.tokens, "claim", observed_claim)
    with pytest.raises(MailValidationError, match="changed"):
        case.client.move_to_folder(preview["token"], limit=limit)
    assert not claims and not mutations(case.peer) and not execution_log(case.path)
    assert (
        case.service.tokens.inspect(preview["token"], "move_to_folder").messages == grant.messages
    )
    assert all(item.folder == "INBOX" and not item.flags for item in case.sources)
    assert case.duplicate.folder is None and not case.duplicate.flags
