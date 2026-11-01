"""Bounded preview round trips with deterministic AF_UNIX server latency."""

import re
from collections import Counter
from dataclasses import replace
from math import ceil

import pytest
from bridge_costs import ZERO_COSTS, CommandClock, WorkCounters
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations
from mock_imap_cleanup import CleanupIMAPPeer
from test_duplicate_msgid import rules_service

from proton_mail_mcp import mail
from proton_mail_mcp.config import Settings
from proton_mail_mcp.folder_rules import CALIBRATION_ROWS

ACTIONS = (
    "move_to_folder",
    "mark_read",
    "mark_unread",
    "move_to_trash",
    "archive",
    "preview_rules",
)
SENDER = "notifications@github.example"

# Option A: full-mailbox discovery retains main's old/protected-copy and
# ambiguity guards without HEADER SEARCH. M is physical mailbox size, including
# candidates. Local sender matching reads the date-filtered source's S raw
# headers, while membership discovery still uses cheap envelopes (raw identity
# audits for rules). The overlapping views cost 3*M + S + candidate DB rows
# (3*M + S - candidate for rules). With S=(M+candidate)/2 in these fixtures,
# the existing 3.5*M + 2*candidate row bound still holds unchanged. Ordinary
# membership discovery batches 1,000 envelopes; source reads and rules audit
# raw fields in batches of 200 to detect hidden second fields.
OPTION_A_DB_ROWS_PER_MESSAGE = 3.5
OPTION_A_FETCHES_PER_CHUNK = 3.45
OPTION_A_LIVE45_SECONDS = 50  # Measured 49.759 s; retains the existing bound.


def option_a_bounds(mailbox_size, candidate_count, source_count, *, rules=False):
    """One linear bound for both small and large synthetic mailboxes."""
    chunk = 200 if rules else 1000
    # Retain the membership allowance, adding only batches needed to parse
    # source rows that a server FROM search previously filtered. At M=6,200,
    # S=3,200: measured 38/108 FETCH and 85/155 commands (ordinary/rules).
    source_batches = max(0, ceil(source_count / 200) - ceil(candidate_count / 200))
    fetches = (
        max(8, ceil(OPTION_A_FETCHES_PER_CHUNK * mailbox_size / chunk + (0 if rules else 4)))
        + source_batches
    )
    return {
        "db_rows": ceil(OPTION_A_DB_ROWS_PER_MESSAGE * mailbox_size)
        + (0 if rules else 2 * candidate_count),
        "fetches": fetches,
        "commands": fetches + (52 if rules else 50),
    }


def assert_option_a_costs(sim, candidate_count, *, rules=False):
    bounds = option_a_bounds(len(sim.messages), candidate_count, source_rows(sim), rules=rules)
    counts = command_counts(sim)
    assert sim.work.db_rows_fetch <= bounds["db_rows"]
    assert counts["UID FETCH"] <= bounds["fetches"]
    assert sum(counts.values()) <= bounds["commands"]


def source_rows(sim):
    # Every synthetic source row falls within the query's date interval.
    return sum(item.folder == "INBOX" for item in sim.messages.values())


@pytest.fixture
def speed_mailbox(tmp_path, monkeypatch):
    peers = []

    def build(count=45, *, filler=3000, latency=0.2, metadata_latency=0):
        clock = CommandClock()
        monkeypatch.setattr(mail, "time", clock)
        sim = BridgeSim(
            clock=clock,
            command_latency=latency,
            work=WorkCounters(clock, replace(ZERO_COSTS, db_row=metadata_latency)),
        )
        peers.append(sim)
        # Bulk population avoids timing the fake's setup rather than the client.
        for index in range(filler):
            for folder in ("INBOX", "Archive"):
                item = Msg(
                    hdr(
                        f"<filler-{folder}-{index}@example.invalid>", from_="other@example.invalid"
                    ),
                    folder=folder,
                    labels={"Labels/Keep"},
                )
                sim.messages[item.pid] = item
        for index in range(count):
            item = Msg(hdr(f"<match-{index}@example.invalid>", from_=SENDER))
            sim.messages[item.pid] = item
        sim.sync()
        client = make_client(sim, tmp_path)
        defaults = Settings.__dataclass_fields__
        client.settings = replace(
            client.settings,
            call_timeout=defaults["call_timeout"].default,
            execute_timeout=defaults["execute_timeout"].default,
        )
        return sim, client, clock

    yield build
    for sim in peers:
        sim.close()
        assert not sim.errors and not sim.violations
        assert all(not worker.is_alive() for _, _, worker in sim.peers)


def preview(client, tmp_path, action, *, query=None):
    if action == "preview_rules":
        return rules_service(client, tmp_path).preview_rules(mark_read=True)
    return client.preview_action(
        action,
        query=query or {"folder": "INBOX", "from_": SENDER, "since": "2026-10-01"},
        params={"folder": "Folders/Reviewed", "mark_read": True}
        if action == "move_to_folder"
        else None,
    )


def complete_rules_preview(sim, client, clock, tmp_path, *, service=None, mark_read=True):
    """Follow read-only cursors through every batch without applying any approval."""
    cursor, partials, batches = None, 0, []
    for calls in range(1, 11):
        started = clock.elapsed
        current = service if service is not None else rules_service(client, tmp_path)
        result = current.preview_rules(mark_read=mark_read, cursor=cursor)
        assert clock.elapsed - started < 20
        assert not mutations(sim)
        if result["partial"]:
            partials += 1
            assert result["token"] is None and not result["total_is_exact"]
            assert result["next_cursor"] and "deadline" in result["reason"].lower()
        else:
            assert result["token"], result.get("reason")
            batches.append(result)
        cursor = result["next_cursor"]
        if cursor is None:
            assert batches and result["remaining"] == 0
            assert sum(batch["batch_count"] for batch in batches) == result["total_matches"]
            return batches, calls, partials
    pytest.fail("Rules preview did not finish within ten bounded calls")


def assert_rules_costs(sim, candidate_count, calls, partials, *, literal_search=False):
    # Source discovery reads cheap metadata and raw From headers once. Strict
    # raw identity audits retain complete mailbox coverage, including labels.
    # Each call refreshes three selected candidate views, including a singleton
    # checkpoint and calibration before growing its batch. A failed source
    # batch may repeat its 200 metadata and 200 raw header rows.
    source = source_rows(sim)
    audited = sum(len(uids) for uids in sim.uidmap.values())
    # Include both warm calibration sizes (4 and 8) as well as the singleton.
    fresh = 3 * (candidate_count + 3 * CALIBRATION_ROWS + 1) * calls
    assert sim.work.db_rows_fetch <= audited + 2 * source + fresh + 400 * partials
    assert sim.work.literal_loads_fetch <= audited + source + fresh + 200 * partials
    if not literal_search:
        assert sim.work.literal_loads_search == 0
    fetches = [command for _, _, _, command in sim.commands if command.startswith("UID FETCH")]
    assert all(len(command.split()[2].split(",")) <= 200 for command in fetches)


def command_counts(sim):
    return Counter(
        " ".join(parts[:2]) if parts[0] == "UID" else parts[0]
        for _, _, _, command in sim.commands
        for parts in [command.split()]
    )


def assert_readonly_headers(sim, *, rules=False):
    assert not mutations(sim)
    assert all(not item.flags for item in sim.messages.values())
    assert not any(command.startswith("SELECT ") for _, _, _, command in sim.commands)
    fetches = [command for _, _, _, command in sim.commands if command.startswith("UID FETCH")]
    assert fetches
    for command in fetches:
        if "BODY.PEEK[HEADER.FIELDS (" not in command:
            if rules:
                assert command.endswith(
                    ("(UID ENVELOPE)", "(UID INTERNALDATE RFC822.SIZE ENVELOPE)")
                )
            else:
                assert command.endswith("(UID ENVELOPE)")
            continue
        assert not re.search(r"BODY\[|BODY\.PEEK\[(?!HEADER\.FIELDS)|RFC822(?!\.SIZE)", command)
        assert "RFC822.SIZE" in command and "INTERNALDATE" in command


@pytest.mark.slow
@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("count", [45, 200])
def test_large_folder_preview_is_batched_with_headroom(speed_mailbox, tmp_path, action, count):
    sim, client, clock = speed_mailbox(count)
    if action == "preview_rules":
        batches, calls, partials = complete_rules_preview(sim, client, clock, tmp_path)
        assert_rules_costs(sim, count, calls, partials)
        # A cold singleton may need its own approval before two comparable
        # warm samples and the fitted batch. Small rules still finish below.
        assert len(batches) <= 4 and not any(batch["blocked"] for batch in batches)
        result = {**batches[-1], "count": sum(batch["count"] for batch in batches)}
    else:
        result = preview(client, tmp_path, action)
        assert_option_a_costs(sim, count)
    counts = command_counts(sim)
    if action != "preview_rules":
        assert sum(counts.values()) <= 90  # Keep #12's still-met command bound.
        assert sim.work.literal_loads <= source_rows(sim) + count * 4
    assert result["count"] == count and result["token"] and not result["partial"]
    assert not result["blocked"]
    if action != "preview_rules":
        assert clock.elapsed < client.settings.call_timeout * 0.8
    assert_readonly_headers(sim, rules=action == "preview_rules")


@pytest.mark.slow
@pytest.mark.parametrize("action", ACTIONS)
def test_discovery_cost_is_bounded_and_only_rules_audit_unrelated_memberships(
    speed_mailbox, tmp_path, action
):
    profiles = []
    for filler in (20, 3000):
        sim, client, clock = speed_mailbox(200, filler=filler, latency=0.1)
        if action == "preview_rules":
            batches, calls, partials = complete_rules_preview(sim, client, clock, tmp_path)
            assert_rules_costs(sim, 200, calls, partials)
            assert len(batches) <= 2
            result = {**batches[-1], "count": sum(batch["count"] for batch in batches)}
        else:
            result = preview(client, tmp_path, action)
            assert_option_a_costs(sim, 200)
        assert result["token"] and result["count"] == 200
        profiles.append(
            (
                len(sim.messages),
                sim.work.db_rows_fetch,
                sim.work.literal_loads_fetch - source_rows(sim),
            )
        )
    if action != "preview_rules":
        # The ordinary row/command bounds cover M=240 and M=6,200. Increasing
        # M by 5,960 adds 20,860 DB rows, within the same linear row bound.
        assert profiles[1][1] - profiles[0][1] <= ceil(
            OPTION_A_DB_ROWS_PER_MESSAGE * (profiles[1][0] - profiles[0][0])
        )
        # Source matching now parses every date-matching From locally; only
        # candidate memberships may load raw headers outside that source read.
        assert profiles[0][2] == profiles[1][2]


def test_live_45_message_headroom_regression(speed_mailbox, tmp_path):
    # 100 ms per command plus 1.7 ms per fetched DB row reproduces the old
    # complete-but-tokenless 45-message preview (>36 s, below its 45 s deadline).
    # Option A inventories all-date copies without HEADER SEARCH. Measured:
    # 21,225 fetched DB rows, 3,135 raw rows, 38 FETCH, 85 commands and 49.759 s.
    # The row bound is 21,248 (linear in M=6,045), and the time bound is 50 s.
    # This 1.7 ms/DB-row calibration differs from the 50k slow-peer fixture.
    sim, client, clock = speed_mailbox(45, latency=0.1, metadata_latency=0.0017)
    result = preview(client, tmp_path, "move_to_folder")
    assert result["count"] == 45 and not result["partial"]
    assert result["token"], result.get("reason")
    assert clock.elapsed < OPTION_A_LIVE45_SECONDS
    assert clock.elapsed < client.settings.call_timeout * 0.8
    assert_option_a_costs(sim, 45)
    assert sim.work.literal_loads <= source_rows(sim) + 45 * 4


@pytest.mark.parametrize("action", ["move_to_folder", "preview_rules"])
def test_slow_server_refuses_token_with_headroom_reason(speed_mailbox, tmp_path, action):
    sim, client, clock = speed_mailbox(45, filler=0, latency=0)
    assert preview(client, tmp_path, action)["token"]
    if action == "preview_rules":
        sim, client, clock = speed_mailbox(45, filler=0, latency=1)
        result = preview(client, tmp_path, action)
        assert result["partial"] and not result["total_is_exact"]
        assert result["token"] is None and result["next_cursor"]
        assert "deadline" in result["reason"].lower()
        assert clock.elapsed < 20 and not mutations(sim)
        return
    # Price each command so the completed preview takes 85% of its real budget.
    before_logout = len(sim.commands) - 1
    sim, client, clock = speed_mailbox(
        45, filler=0, latency=client.settings.call_timeout * 0.85 / before_logout
    )
    result = preview(client, tmp_path, action)
    assert result["count"] == 45 and not result["partial"]
    assert result["token"] is None and "headroom" in result["reason"].lower()
    assert clock.elapsed < client.settings.call_timeout
    assert not mutations(sim)


@pytest.mark.parametrize("action", ["move_to_folder", "preview_rules"])
def test_server_search_widening_is_refiltered(speed_mailbox, tmp_path, monkeypatch, action):
    sim, client, clock = speed_mailbox(2, filler=3)
    monkeypatch.setattr(sim, "_match", lambda message, terms: True)
    if action == "preview_rules":
        batches, _, _ = complete_rules_preview(sim, client, clock, tmp_path)
        assert len(batches) == 1
        assert all(batch["total_matches"] == 2 for batch in batches)
        assert all(not batch["blocked"] for batch in batches)
        approved = [
            row["uid"]
            for batch in batches
            for row in client.cleanup.tokens.inspect(batch["token"], "apply_rules").messages
        ]
        expected = {
            str(uid)
            for uid, pid in sim.uidmap["INBOX"].items()
            if b"<match-" in sim.messages[pid].headers
        }
        assert len(approved) == len(set(approved)) == 2 and set(approved) == expected
        result = {**batches[-1], "count": sum(batch["count"] for batch in batches)}
    else:
        result = preview(client, tmp_path, action)
    assert result["count"] == 2 and result["token"]
    assert not result["blocked"] and not mutations(sim)
    searches = [command for _, _, _, command in sim.commands if command.startswith("UID SEARCH")]
    assert all(" FROM " not in command for command in searches)


def test_cleanup_peer_latency_counts_every_protocol_command(tmp_path, monkeypatch):
    clock = CommandClock()
    monkeypatch.setattr(mail, "time", clock)
    peer = CleanupIMAPPeer(clock=clock, command_latency=0.05)
    try:
        client = make_client(peer, tmp_path, call_timeout=120)
        result = client.preview_action("mark_read", query={"folder": "INBOX"})
        assert result["token"] and result["count"] == 2
        assert clock.elapsed == pytest.approx(len(peer.commands) * 0.05)
    finally:
        peer.close()
