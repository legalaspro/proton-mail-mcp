"""Expensive identity SEARCH regression using real imaplib and AF_UNIX peers.

Timing is a deterministic workload model, not a live-server measurement. A
HEADER identity SEARCH visits/decrypts every mailbox row at 6 ms per row; an
indexed selector costs one round trip. ENVELOPE rows cost 20 us, and bounded
raw header FETCH costs 3 ms per returned row. The large mailbox really contains
50,000 rows, including irrelevant old messages, rather than merely charging a
fixed sleep when the client sends a disallowed command.
"""

import json
import re
import time
from collections import Counter
from dataclasses import replace

import pytest
from bridge_costs import ZERO_COSTS, CommandClock, WorkCounters, envelope_from_headers
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations
from test_mail import PLAIN

from proton_mail_mcp import mail
from proton_mail_mcp.config import Settings
from proton_mail_mcp.folder_rules import CALIBRATION_ROWS, FolderRulesService
from proton_mail_mcp.mail import MailError

SENDER = "notifications@github.com"
TARGET = "Folders/GitHub"
# Option A keeps full-mailbox copy discovery without HEADER SEARCH. At 50k
# rows the 3/45/200-match cases measure about 16.4/16.9/18.8 modeled seconds;
# 22 s allows 17% over the largest case and stays well below the 120 s budget.
OPTION_A_50K_PREVIEW_SECONDS = 22
ACTIONS = (
    "move_to_folder",
    "move_to_trash",
    "archive",
    "mark_read",
    "mark_unread",
    "add_label",
    "remove_label",
)


class IdentityCostSim(BridgeSim):
    """Efficient metadata responses for a genuinely large synthetic mailbox."""

    def __init__(self, clock):
        super().__init__(
            clock=clock,
            command_latency=0.1,
            folders=(TARGET,),
            labels=("Labels/Review",),
            work=WorkCounters(clock, ZERO_COSTS),
        )
        self.envelope_rows = 0
        self.raw_rows = 0
        self.header_search_rows = 0

    def handle(self, tag, op, uid_op, words, session, command):
        if op == "SEARCH" and re.search(rb"\bHEADER\s+Message-ID\b", command, re.I):
            rows = len(self.uidmap[session.selected])
            self.header_search_rows += rows
            self.clock.elapsed += rows * 0.006
        if op == "SEARCH" and words[2:3] == ["UID"]:
            found = self._uids(words[3], self.uidmap[session.selected])
            return (
                b"* SEARCH " + " ".join(map(str, found)).encode() + b"\r\n" + tag + b" OK done\r\n"
            )
        if op == "FETCH":
            match = re.fullmatch(rb"UID FETCH (\S+) \((.*)\)", command, re.I)
            spec, query = match.group(1).decode(), match.group(2)
            mapping = self.uidmap[session.selected]
            uids = self._uids(spec, mapping)
            if query == b"BODYSTRUCTURE":
                return (
                    b"* 1 FETCH (UID "
                    + spec.encode()
                    + b" BODYSTRUCTURE "
                    + PLAIN
                    + b")\r\n"
                    + tag
                    + b" OK done\r\n"
                )
            if query == b"UID ENVELOPE":
                # Identity inventories: avoid the parent fake's linear
                # sequence-number lookup per UID. Other metadata reads (such as
                # outcome checks with FLAGS or sizes) use the full parent reply.
                self.envelope_rows += len(uids)
                self.clock.elapsed += len(uids) * 0.00002
                self.work.charge("FETCH", query, len(uids))
                rows = []
                for sequence, uid in enumerate(uids, 1):
                    raw = self.messages[mapping[uid]].headers
                    rows.append(
                        f"* {sequence} FETCH (UID {uid} ENVELOPE ".encode()
                        + envelope_from_headers(raw)
                        + b")\r\n"
                    )
                return b"".join(rows) + tag + b" OK done\r\n"
            self.raw_rows += len(uids)
            self.clock.elapsed += len(uids) * 0.003
        return super().handle(tag, op, uid_op, words, session, command)


@pytest.fixture
def identity_cost_mailbox(tmp_path, monkeypatch):
    peers = []

    def build(count=45, *, large=True):
        clock = CommandClock()
        monkeypatch.setattr(mail, "time", clock)
        sim = IdentityCostSim(clock)
        peers.append(sim)
        # Shared immutable bytes keep fake-server parsing fast; each row retains
        # its own physical identity and UID in the mailbox and All Mail views.
        filler = hdr(
            "<unrelated-old@example.invalid>",
            from_="other@example.invalid",
            date="Sat, 01 Jan 2022 00:00:00 +0000",
        )
        for _ in range(50_000 - count if large else 0):
            item = Msg(filler, folder="Archive")
            sim.messages[item.pid] = item
        for index in range(count):
            item = Msg(
                hdr(f"<wanted-{index}@example.invalid>", from_=SENDER),
                labels={"Labels/Review"},
            )
            sim.messages[item.pid] = item
        sim.sync()
        client = make_client(sim, tmp_path)
        defaults = Settings.__dataclass_fields__
        client.settings = replace(
            client.settings,
            call_timeout=defaults["call_timeout"].default,
            execute_timeout=defaults["execute_timeout"].default,
        )
        rules = tmp_path / "identity-cost-rules.json"
        rules.write_text(
            json.dumps(
                {"version": 1, "rules": [{"field": "from", "contains": SENDER, "folder": TARGET}]}
            )
        )
        return sim, client, clock, FolderRulesService(client.cleanup, rules)

    yield build
    for sim in peers:
        sim.close()
        assert not sim.errors and not sim.violations
        assert all(not worker.is_alive() for _, _, worker in sim.peers)


def preview(client, action="move_to_folder"):
    params = None
    if action == "move_to_folder":
        params = {"folder": TARGET, "mark_read": True}
    elif action in {"add_label", "remove_label"}:
        params = {"label": "Review"}
    return client.preview_action(
        action,
        query={
            "folder": "Labels/Review" if action == "remove_label" else "INBOX",
            "from": SENDER,
            "since": "2026-10-16",
        },
        params=params,
    )


def evidence(sim, clock):
    commands = [item[3] for item in sim.commands]
    return {
        "commands": len(commands),
        "search_kinds": dict(
            Counter(
                "HEADER Message-ID"
                if "HEADER Message-ID" in command
                else "UID range"
                if re.search(r"SEARCH (?:CHARSET UTF-8 )?UID ", command)
                else "selector"
                for command in commands
                if command.startswith("UID SEARCH ")
            )
        ),
        "envelope_rows": sim.envelope_rows,
        "raw_rows": sim.raw_rows,
        "header_search_rows": sim.header_search_rows,
        "simulated_seconds": round(clock.elapsed, 4),
    }


def assert_no_identity_search(sim):
    assert not any(
        re.search(r"\bSEARCH\b.*\bHEADER\s+Message-ID\b", command, re.I)
        for _, _, _, command in sim.commands
    )


@pytest.mark.slow
@pytest.mark.parametrize("count", [3, 45, 200])
def test_live_move_preview_avoids_mailbox_sized_header_search(identity_cost_mailbox, count):
    sim, client, clock, _ = identity_cost_mailbox(count)
    result = preview(client)
    print("IDENTITY_COST", json.dumps({"matches": count, **evidence(sim, clock)}))
    assert result["token"], result.get("reason")
    assert result["count"] == count and not result["partial"]
    assert clock.elapsed < OPTION_A_50K_PREVIEW_SECONDS
    assert clock.elapsed < client.settings.call_timeout * 0.8
    assert sim.envelope_rows >= 50_000
    assert sim.raw_rows <= 5 * count
    assert_no_identity_search(sim)
    assert not mutations(sim)
    assert all(not row.flags for row in sim.messages.values())


@pytest.mark.parametrize("action", ACTIONS)
def test_no_identity_header_search_during_preview_execute_and_undo(identity_cost_mailbox, action):
    sim, client, _, _ = identity_cost_mailbox(3, large=False)
    if action == "add_label":
        for item in sim.messages.values():
            item.labels.clear()
        sim.sync()
    result = preview(client, action)
    assert result["token"], result
    completed = getattr(client, action)(result["token"])
    assert completed["count"] == 3 and not completed["partial"]
    restored = client.undo_action(completed["log_ids"][0])
    assert restored["count"] == 3 and not restored["partial"]
    assert_no_identity_search(sim)


def test_545_rules_matches_reach_full_batches_after_calibration(identity_cost_mailbox):
    sim, _, clock, rules = identity_cost_mailbox(545, large=False)
    cursor, batches = None, []
    for _ in range(4):
        started = clock.elapsed
        result = rules.preview_rules(mark_read=True, cursor=cursor)
        print("IDENTITY_COST", json.dumps({"rules_matches": 545, **evidence(sim, clock)}))
        if batches:
            assert result["token"] and not result["partial"], result.get("reason")
        else:
            assert result["partial"] and result["token"] is None
        assert result["count"] == result["batch_count"] and result["total_matches"] == 545
        assert clock.elapsed - started < 20
        batches.append(result["batch_count"])
        cursor = result["next_cursor"]
    # The finishing audit holds its small batch; the next call's warm singleton
    # lets the probe reach the full limit without spending an approval on it.
    assert batches == [0, 200, 200, 145]
    assert cursor is None and result["remaining"] == 0
    assert_no_identity_search(sim)


def test_38_rules_matches_finish_in_one_call_and_keep_a_batch_when_bridge_slows(
    identity_cost_mailbox, monkeypatch
):
    # CoS: a 38-match rule returned batch_count 0, remaining 38 and "Rules preview
    # deadline reached" at limit 1 and at limit 200.
    sim, _, clock, rules = identity_cost_mailbox(38, large=False)
    started = clock.elapsed
    result = rules.preview_rules(mark_read=True)
    assert result["token"] and not result["partial"], result.get("reason")
    assert result["batch_count"] == result["count"] == 38 and result["remaining"] == 0
    assert result["next_cursor"] is None and clock.elapsed - started < 20

    cursor = None
    for remaining in (37, 36):
        started = clock.elapsed
        result = rules.preview_rules(mark_read=True, limit=1, cursor=cursor)
        assert result["token"] and not result["partial"], result.get("reason")
        assert result["batch_count"] == 1 and result["remaining"] == remaining
        assert clock.elapsed - started < 20
        cursor = result["next_cursor"]

    # Bridge slows while a new chain grows its batch. The larger review misses
    # the deadline; the calibration batch already reviewed is still approved.
    plan, sizes = FolderRulesService._plan, []

    def slows_after_calibration(self, *args, **kwargs):
        sizes.append(len(kwargs["source_rows"]))
        if len(sizes) == 3:
            sim.command_latency = 1.0
        return plan(self, *args, **kwargs)

    monkeypatch.setattr(FolderRulesService, "_plan", slows_after_calibration)
    started = clock.elapsed
    result = rules.preview_rules(mark_read=True)
    assert sizes[:2] == [1, CALIBRATION_ROWS] and sizes[2] > CALIBRATION_ROWS
    assert len(sizes) == 3
    assert result["token"] and not result["partial"], result.get("reason")
    assert result["batch_count"] == result["count"] == CALIBRATION_ROWS
    assert result["remaining"] == 38 - CALIBRATION_ROWS and result["next_cursor"]
    assert clock.elapsed - started < 20
    assert_no_identity_search(sim)


def test_rules_apply_and_undo_never_search_identity_headers(identity_cost_mailbox):
    sim, client, _, rules = identity_cost_mailbox(3, large=False)
    result = rules.preview_rules(mark_read=True)
    completed = rules.apply_rules(result["token"])
    assert completed["count"] == 3 and not completed["partial"]
    for log_id in completed["log_ids"]:
        restored = client.undo_action(log_id)
        assert restored["count"] == 3 and not restored["partial"]
    assert_no_identity_search(sim)


def test_local_identity_matching_excludes_casefold_and_substring_decoys(identity_cost_mailbox):
    sim, client, _, _ = identity_cost_mailbox(1, large=False)
    wanted = next(iter(sim.messages.values()))
    for identifier in ("<WANTED-0@example.invalid>", "<prefix-wanted-0@example.invalid>"):
        sim.add(Msg(hdr(identifier), folder="Archive"))
    # Select the exact identity from one physical source. SEARCH similarity or
    # an unfiltered envelope inventory cannot add other physical messages.
    try:
        result = preview(client, "mark_read")
    except MailError:
        assert False, "Unrelated identities must not contaminate exact local identity matching"
    assert result["count"] == 1 and result["token"]
    grant = client.cleanup.tokens.inspect(result["token"], "mark_read")
    assert {row["uid"] for row in grant.messages} == {str(sim.uid_of("INBOX", wanted.pid))}
    assert_no_identity_search(sim)


def test_unsubscribe_preview_and_confirm_never_search_identity_headers(
    identity_cost_mailbox, monkeypatch
):
    sim, client, _, _ = identity_cost_mailbox(1, large=False)
    item = next(iter(sim.messages.values()))
    item.headers = (
        b"List-Unsubscribe: <https://github.com/unsubscribe>\r\n"
        b"List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n" + item.headers
    )
    monkeypatch.setattr("proton_mail_mcp.unsubscribe_http.perform", lambda *args, **kwargs: 204)
    result = preview(client, "unsubscribe")
    assert result["token"], result
    completed = client.unsubscribe(result["token"])
    assert completed["count"] == 1 and not completed["partial"]
    assert_no_identity_search(sim)
    assert not mutations(sim)


def test_blocking_preview_search_returns_clean_refusal_before_call_budget(tmp_path):
    sim = BridgeSim(folders=(TARGET,))
    try:
        sim.add(Msg(hdr("<blocked-command@example.invalid>", from_=SENDER)))
        client = make_client(sim, tmp_path, call_timeout=0.8, timeout=5)
        sim.hooks.append(lambda sid, folder, op, command: "stall" if op == "SEARCH" else None)
        started = time.monotonic()
        result = preview(client)
        elapsed = time.monotonic() - started
        print("BLOCKING_PREVIEW", json.dumps({"wall_seconds": elapsed, "budget": 0.8}))
        assert result["token"] is None and result["partial"] is True
        assert "deadline" in result["reason"].lower()
        assert elapsed < client.settings.call_timeout
        assert not mutations(sim)
        assert not any(command.startswith("LOGOUT") for _, _, _, command in sim.commands)
        assert sim.peers[0][0].fileno() == -1
    finally:
        sim.close()
