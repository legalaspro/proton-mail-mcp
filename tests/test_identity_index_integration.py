"""Certified warm identity discovery retains raw-field and mailbox-copy guards."""

import json
import re
import time
from dataclasses import replace
from math import ceil
from pathlib import Path

import pytest
from bridge_costs import ZERO_COSTS, CommandClock, WorkCounters
from bridge_sim import Msg, hdr, make_client, mutations
from certified_index_peer import CertifiedIndexPeer
from test_preview_speed import OPTION_A_LIVE45_SECONDS, SENDER, command_counts, option_a_bounds

from proton_mail_mcp import identity_index, mail
from proton_mail_mcp.cleanup_lookup import fetch_index_rows
from proton_mail_mcp.folder_rules import FolderRulesService
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.search import _uidvalidity

# Warm certification still enumerates mailbox UID sets. Membership fetches
# depend only on the selected batch; sender matching separately reads every
# source candidate. Rules discover envelopes before the strict selected-source
# fetch, adding one DB row per candidate to the three fresh raw fetches.
# At 3,000 unrelated source rows, ordinary previews measure 69 commands and
# 25.360 s (17.406 s for live45). Keep the original membership-only allowances
# below, adding just the explicit source-row and source-batch costs.
WARM_FETCH_ROWS_PER_CANDIDATE = 3
WARM_FETCH_COMMAND_LIMIT = 3
WARM_COMMAND_LIMIT = 85
WARM_LIVE45_SECONDS = 15
WARM_SUBJECT_SECONDS = 20
WARM_ORDINARY_SECONDS = 25
# A cold certified index adds bounded STATUS/UID-set reconciliation per folder.
# The Option A bounds account separately for source scans and memberships.
CERTIFIED_COLD_EXTRA_COMMANDS_PER_FOLDER = 6


def source_scan_allowance(sim, count):
    """Account only for source rows that server FROM previously filtered out."""
    source_count = len(sim.uidmap["INBOX"])
    rows = source_count - count
    batches = ceil(source_count / 200) - ceil(count / 200)
    seconds = rows * sim.work.costs.db_row + batches * sim.command_latency
    return source_count, rows, batches, seconds


@pytest.fixture
def indexed_mailbox(tmp_path, monkeypatch):
    peers = []

    def build(count=200, *, filler=20, latency=0.2, metadata_latency=0.0017):
        clock = CommandClock()
        monkeypatch.setattr(mail, "time", clock)
        sim = CertifiedIndexPeer(
            clock=clock,
            command_latency=latency,
            strict_search=True,
            work=WorkCounters(clock, replace(ZERO_COSTS, db_row=metadata_latency)),
        )
        peers.append(sim)
        for index in range(filler):
            for folder in ("INBOX", "Archive"):
                item = Msg(
                    hdr(
                        f"<index-filler-{folder}-{index}@example.invalid>",
                        from_="background@example.invalid",
                        subject="Background",
                    ),
                    folder=folder,
                    labels={"Labels/Keep"},
                )
                sim.messages[item.pid] = item
        selected = []
        for index in range(count):
            item = Msg(
                hdr(f"<index-wanted-{index}@example.invalid>", from_=SENDER, subject="staff")
            )
            sim.messages[item.pid] = item
            selected.append(item)
        sim.sync()
        state = tmp_path / f"case-{len(peers)}"
        state.mkdir()
        client = make_client(sim, state, call_timeout=120)
        client.settings = replace(
            client.settings, identity_index_enabled=True, identity_index_dir=str(state / "index")
        )
        rules = state / "rules.json"
        rules.write_text(
            json.dumps(
                {
                    "version": 1,
                    "rules": [
                        {"field": "subject", "contains": "staff", "folder": "Folders/Reviewed"}
                    ],
                }
            )
        )
        service = FolderRulesService(client.cleanup, rules)
        return sim, client, service, clock, selected

    yield build
    for sim in peers:
        sim.close()
        assert not sim.errors and not sim.violations
        assert all(not worker.is_alive() for _, _, worker in sim.peers)


def indexed_preview(client, service, *, rules=False):
    if rules:
        return service.preview_rules(folder="INBOX")
    return client.preview_action(
        "move_to_folder",
        query={"folder": "INBOX", "from_": SENDER, "since": "2026-10-01"},
        params={"folder": "Folders/Reviewed", "mark_read": True},
    )


def reset_measurements(sim, clock):
    sim.commands.clear()
    sim.work = WorkCounters(clock, sim.work.costs)
    clock.elapsed = 0


def prepare_certified_indexes(client, *, source_headers=False):
    """Warm the synthetic account outside a measured, read-only rules preview."""
    deadline = client._deadline()
    with client._session(deadline=deadline) as (connection, _):
        for folder in client._folders(connection):
            client._select(connection, folder)
            assert (
                identity_index.certified_inventory(
                    client,
                    connection,
                    deadline,
                    folder,
                    _uidvalidity(connection),
                    fetch_rows=fetch_index_rows,
                )
                is not None
            )
            if source_headers and folder == "INBOX":
                uids = client._search(connection, ["ALL"])
                for offset in range(0, len(uids), 200):
                    client._metadata(
                        connection,
                        uids[offset : offset + 200],
                        deadline=deadline,
                        headers=True,
                        bodystructure=False,
                        internaldate=True,
                        envelope=True,
                        size=True,
                    )


def index_files(client):
    return {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in Path(client.settings.identity_index_dir).glob("*.json")
    }


def cost_facts(sim, clock, *, case, temperature):
    counts = command_counts(sim)
    facts = {
        "case": case,
        "temperature": temperature,
        "mailbox_size": len(sim.messages),
        "commands": sum(counts.values()),
        "fetch_commands": counts["UID FETCH"],
        "db_rows_fetch": sim.work.db_rows_fetch,
        "raw_rows_fetch": sim.work.literal_loads_fetch,
        "modeled_seconds": round(clock.elapsed, 6),
    }
    print("INDEX_COST", json.dumps(facts, sort_keys=True))
    return facts


def assert_no_mutations_or_header_search(sim):
    assert not mutations(sim)
    assert not any(
        re.search(r"\bSEARCH\b.*\bHEADER\b", command, re.I) for *_, command in sim.commands
    )
    assert not any(command.startswith("SELECT ") for *_, command in sim.commands)


def assert_refuses(client, service, *, rules=False):
    try:
        result = indexed_preview(client, service, rules=rules)
    except MailValidationError:
        return
    assert result["token"] is None
    assert result["blocked"] or result["partial"] or result.get("reason")


@pytest.mark.parametrize("filler", [20, 3000])
def test_certified_warm_memberships_keep_candidate_proportional_fetch_cost(indexed_mailbox, filler):
    sim, client, service, clock, _ = indexed_mailbox(filler=filler)
    cold = indexed_preview(client, service)
    assert cold["token"] and cold["count"] == 200 and not cold["partial"], cold.get("reason")
    cold_facts = cost_facts(sim, clock, case="ordinary", temperature="cold")
    source_count, source_rows, source_batches, source_seconds = source_scan_allowance(sim, 200)
    cold_bounds = option_a_bounds(len(sim.messages), 200, source_count)
    # Complete index creation additionally rechecks matching candidates. The
    # existing ordinary Option A row cap includes those two candidate copies.
    assert cold_facts["db_rows_fetch"] <= cold_bounds["db_rows"]
    assert cold_facts["fetch_commands"] <= cold_bounds["fetches"]
    assert cold_facts["commands"] <= (
        cold_bounds["commands"]
        + CERTIFIED_COLD_EXTRA_COMMANDS_PER_FOLDER * len(sim.mailbox_names())
    )
    assert cold_facts["modeled_seconds"] < client.settings.call_timeout * 0.8
    reset_measurements(sim, clock)
    warm = indexed_preview(client, service)
    assert warm["token"] and warm["count"] == 200 and not warm["partial"], warm.get("reason")
    facts = cost_facts(sim, clock, case="ordinary", temperature="warm")
    # The raw source scan is separate from certified membership discovery.
    # Subtract its extra rows/batches so the existing membership limits stay
    # exact, rather than permitting unrelated mailbox copies to be fetched.
    assert facts["db_rows_fetch"] - source_rows == 200 * WARM_FETCH_ROWS_PER_CANDIDATE
    assert facts["raw_rows_fetch"] - source_rows == 200 * WARM_FETCH_ROWS_PER_CANDIDATE
    assert facts["fetch_commands"] - source_batches <= WARM_FETCH_COMMAND_LIMIT
    assert facts["commands"] <= WARM_COMMAND_LIMIT
    assert facts["modeled_seconds"] - source_seconds < WARM_ORDINARY_SECONDS
    assert facts["modeled_seconds"] < client.settings.call_timeout * 0.5
    assert_no_mutations_or_header_search(sim)


@pytest.mark.parametrize("filler", [20, 3000])
def test_rules_preview_cold_audits_resume_and_warm_index_is_read_only(indexed_mailbox, filler):
    sim, client, service, clock, _ = indexed_mailbox(filler=filler)
    cursor, facts, batches = None, [], []
    for _ in range(12):
        reset_measurements(sim, clock)
        result = service.preview_rules(cursor=cursor)
        facts.append(cost_facts(sim, clock, case="subject-rules", temperature="cold"))
        assert clock.elapsed < 20
        assert not index_files(client)
        assert_no_mutations_or_header_search(sim)
        if result["token"]:
            batches.append(result["count"])
        else:
            assert result["partial"] and not result["total_is_exact"] and result["next_cursor"]
        cursor = result["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("Cold rules audits made no bounded progress")
    # A cold call can secure only its singleton. Two comparable warm sample
    # sizes then precede the fitted batch; the cold singleton is not a sample.
    assert len(batches) <= 4 and sum(batches) == 200 and not result["partial"]
    if filler == 3000:
        assert len(facts) > len(batches)
    # Complete raw audit coverage costs 3*M-C rows in these overlapping views.
    # Discovery adds S envelopes; source and matching copies add 3*C raw rows.
    # Each later call can repeat those C-row checks and one 200-row read.
    mailbox_size, candidates, source_count = len(sim.messages), 200, len(sim.uidmap["INBOX"])
    assert sum(fact["db_rows_fetch"] for fact in facts) <= (
        3 * mailbox_size + 2 * candidates + source_count + (3 * candidates + 200) * (len(facts) - 1)
    )

    prepare_certified_indexes(client, source_headers=True)
    before = index_files(client)
    assert before
    plan, reviews = service._plan, []

    def counted_plan(*args, **options):
        reviews.append(len(options["source_rows"]))
        return plan(*args, **options)

    service._plan = counted_plan
    cursor, warm, batches = None, [], []
    for _ in range(3):
        reset_measurements(sim, clock)
        result = service.preview_rules(folder="INBOX", cursor=cursor)
        assert result["token"] and not result["partial"], result
        facts = cost_facts(sim, clock, case="subject-rules", temperature="warm")
        # One fresh singleton review precedes the batch review; discovery adds
        # one envelope FETCH on the first call.
        assert facts["fetch_commands"] <= 2 * WARM_FETCH_COMMAND_LIMIT + 1
        # The per-call checkpoint takes the rules path to 89 commands.
        assert facts["commands"] <= WARM_COMMAND_LIMIT + 5
        assert facts["modeled_seconds"] < WARM_SUBJECT_SECONDS
        warm.append(facts)
        batches.append(result["count"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert cursor is None and sum(batches) == 200
    # Each candidate envelope is read once. A call may additionally recheck its
    # singleton checkpoint before the completed batch reviews all 200 rows.
    reviewed_rows = sum(reviews)
    assert 200 <= reviewed_rows <= 200 + len(warm)
    assert sum(fact["db_rows_fetch"] for fact in warm) == (
        200 + reviewed_rows * WARM_FETCH_ROWS_PER_CANDIDATE
    )
    assert sum(fact["raw_rows_fetch"] for fact in warm) == (
        reviewed_rows * WARM_FETCH_ROWS_PER_CANDIDATE
    )
    assert index_files(client) == before
    assert_no_mutations_or_header_search(sim)


def test_certified_warm_live45_bounds_memberships_and_explicit_source_scan(indexed_mailbox):
    sim, client, service, clock, _ = indexed_mailbox(count=45, filler=3000, latency=0.1)
    assert indexed_preview(client, service)["token"]
    cold_facts = cost_facts(sim, clock, case="live45", temperature="cold")
    _, source_rows, source_batches, source_seconds = source_scan_allowance(sim, 45)
    assert cold_facts["modeled_seconds"] - source_seconds < OPTION_A_LIVE45_SECONDS
    assert cold_facts["modeled_seconds"] < client.settings.call_timeout * 0.8
    reset_measurements(sim, clock)
    result = indexed_preview(client, service)
    assert result["token"] and result["count"] == 45 and not result["partial"]
    facts = cost_facts(sim, clock, case="live45", temperature="warm")
    assert facts["db_rows_fetch"] - source_rows == 45 * WARM_FETCH_ROWS_PER_CANDIDATE
    assert facts["raw_rows_fetch"] - source_rows == 45 * WARM_FETCH_ROWS_PER_CANDIDATE
    assert facts["fetch_commands"] - source_batches <= WARM_FETCH_COMMAND_LIMIT
    assert facts["commands"] <= WARM_COMMAND_LIMIT
    assert facts["modeled_seconds"] - source_seconds < WARM_LIVE45_SECONDS
    assert facts["modeled_seconds"] < client.settings.call_timeout * 0.5
    assert_no_mutations_or_header_search(sim)


@pytest.mark.parametrize("rules", [False, True])
def test_new_mail_delta_finds_hidden_second_raw_identity(indexed_mailbox, rules):
    sim, client, service, clock, selected = indexed_mailbox(count=1, filler=2)
    if rules:
        prepare_certified_indexes(client)
    assert indexed_preview(client, service, rules=rules)["token"]
    before = index_files(client)
    reset_measurements(sim, clock)
    sim.add(
        Msg(
            hdr(
                "<unrelated-arrival@example.invalid>",
                subject="Background",
            )
            + f"Message-ID: {selected[0].message_id()}\r\n".encode(),
            folder="Archive",
        )
    )
    assert_refuses(client, service, rules=rules)
    assert any("CHANGEDSINCE" in command for *_, command in sim.commands)
    if rules:
        assert index_files(client) == before
    assert_no_mutations_or_header_search(sim)


@pytest.mark.parametrize("rules", [False, True])
def test_modseq_delta_finds_hidden_second_raw_identity(indexed_mailbox, rules):
    sim, client, service, clock, selected = indexed_mailbox(count=1, filler=2)
    if rules:
        prepare_certified_indexes(client)
    assert indexed_preview(client, service, rules=rules)["token"]
    before = index_files(client)
    filler = next(item for item in sim.messages.values() if item.folder == "Archive")
    filler.headers += f"Message-ID: {selected[0].message_id()}\r\n".encode()
    reset_measurements(sim, clock)
    assert_refuses(client, service, rules=rules)
    assert any("CHANGEDSINCE" in command for *_, command in sim.commands)
    if rules:
        assert index_files(client) == before
    assert_no_mutations_or_header_search(sim)


def test_warm_new_candidate_and_flag_deltas_are_reconciled(indexed_mailbox):
    sim, client, service, clock, selected = indexed_mailbox(count=1, filler=2)
    assert indexed_preview(client, service)["token"]
    selected[0].flags.add(r"\Seen")
    sim.add(Msg(hdr("<new-candidate@example.invalid>", from_=SENDER, subject="staff")))
    reset_measurements(sim, clock)
    result = indexed_preview(client, service)
    assert result["token"] and result["count"] == 2 and not result["partial"]
    assert any("CHANGEDSINCE" in command for *_, command in sim.commands)
    assert_no_mutations_or_header_search(sim)


def test_warm_expunge_removes_stale_candidate(indexed_mailbox):
    sim, client, service, clock, selected = indexed_mailbox(count=2, filler=2)
    assert indexed_preview(client, service)["count"] == 2
    del sim.messages[selected[-1].pid]
    reset_measurements(sim, clock)
    result = indexed_preview(client, service)
    assert result["token"] and result["count"] == 1 and not result["partial"]
    assert_no_mutations_or_header_search(sim)


@pytest.mark.parametrize("folder", ["Archive", "Folders/Keep", "Labels/Keep"])
def test_warm_index_finds_old_protected_copy_relocated_into_folder(indexed_mailbox, folder):
    sim, client, service, clock, selected = indexed_mailbox(count=1, filler=2)
    assert indexed_preview(client, service)["token"]
    sim.add(
        Msg(
            hdr(
                selected[0].message_id(),
                from_="security@mail.bank.example.com",
                date="Sat, 01 Jan 2000 00:00:00 +0000",
                subject="Background",
            ),
            folder=folder if not folder.startswith("Labels/") else "Archive",
            labels={folder} if folder.startswith("Labels/") else set(),
        )
    )
    reset_measurements(sim, clock)
    assert_refuses(client, service)
    assert_no_mutations_or_header_search(sim)


def test_uidvalidity_reset_cannot_reuse_previously_certified_identity(indexed_mailbox):
    sim, client, service, clock, selected = indexed_mailbox(count=1, filler=2)
    assert indexed_preview(client, service)["token"]
    filler = next(item for item in sim.messages.values() if item.folder == "Archive")
    filler.headers += f"Message-ID: {selected[0].message_id()}\r\n".encode()
    for folder in sim.mailbox_names():
        sim.reset_uidvalidity(folder)
    reset_measurements(sim, clock)
    assert_refuses(client, service)
    assert_no_mutations_or_header_search(sim)


def test_certified_index_rebuild_obeys_remaining_command_deadline(indexed_mailbox, monkeypatch):
    sim, client, service, _, _ = indexed_mailbox(count=1, filler=2, latency=0)
    monkeypatch.setattr(mail, "time", time)
    client.settings = replace(client.settings, call_timeout=0.8, timeout=5)
    # Initial candidate FETCH succeeds; the cold full-identity audit then stalls.
    fetch_count = 0

    def stall_rebuild(sid, folder, op, command):
        nonlocal fetch_count
        if op == "FETCH":
            fetch_count += 1
            if fetch_count == 2:
                return "stall"
        return None

    sim.hooks.append(stall_rebuild)
    started = time.monotonic()
    result = indexed_preview(client, service)
    elapsed = time.monotonic() - started
    assert result["token"] is None and result["partial"]
    assert "deadline" in result["reason"].lower()
    assert elapsed < client.settings.call_timeout
    assert sim.peers[0][0].fileno() == -1
    assert not any(command.startswith("LOGOUT") for *_, command in sim.commands)
    assert_no_mutations_or_header_search(sim)
