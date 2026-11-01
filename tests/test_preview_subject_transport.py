"""Strict SEARCH framing and bounded subject previews over local sockets."""

import json
from dataclasses import replace

import pytest
from action_records import execution_log
from bridge_costs import ZERO_COSTS, CommandClock, WorkCounters
from bridge_sim import BridgeSim, Msg, hdr, make_client
from test_preview_speed import (
    assert_option_a_costs,
    assert_readonly_headers,
    assert_rules_costs,
    command_counts,
    complete_rules_preview,
)

from proton_mail_mcp import mail
from proton_mail_mcp.config import Settings
from proton_mail_mcp.folder_rules import FolderRulesService
from proton_mail_mcp.mail import MailError, MailValidationError


@pytest.fixture
def strict_mailbox(tmp_path, monkeypatch):
    peers = []

    def build(
        *, count=200, filler=3000, subject="staff", identities=None, literal_plus=False, latency=0.2
    ):
        clock = CommandClock()
        monkeypatch.setattr(mail, "time", clock)
        capabilities = ("IMAP4rev1", "STARTTLS", "MOVE", "UIDPLUS")
        if literal_plus:
            capabilities += ("LITERAL+",)
        sim = BridgeSim(
            capabilities=capabilities,
            strict_search=True,
            clock=clock,
            command_latency=latency,
            work=WorkCounters(clock, replace(ZERO_COSTS, db_row=0.0017)),
        )
        peers.append(sim)
        for index in range(filler):
            for folder in ("INBOX", "Archive"):
                item = Msg(
                    hdr(
                        f"<background-{folder}-{index}@example.invalid>",
                        subject="Background" if subject == "status" else "Other mail",
                    ),
                    folder=folder,
                    labels={"Labels/Keep"},
                )
                sim.messages[item.pid] = item
        identities = identities or [f"<match-{index}@example.invalid>" for index in range(count)]
        for identifier in identities:
            item = Msg(hdr(identifier, subject=subject))
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


def accepted_preview(client, query):
    try:
        result = client.preview_action("move_to_trash", query=query)
    except MailError:
        assert False, "Valid synthetic SEARCH terms must use accepted IMAP transport"
    assert result["token"] and not result["partial"], result.get("reason")
    return result


def subject_searches(sim):
    return [
        command
        for _, folder, _, command in sim.commands
        if folder == "INBOX" and command.startswith("UID SEARCH") and " SUBJECT " in command
    ]


def profile(sim, clock, case):
    counts = command_counts(sim)
    facts = {
        "case": case,
        "fetch_commands": counts["UID FETCH"],
        "fetched_rows": sim.work.db_rows_fetch,
        "raw_rows": sim.work.literal_loads_fetch,
        "total_commands": sum(counts.values()),
        "elapsed_seconds": round(clock.elapsed, 3),
    }
    print(json.dumps(facts, sort_keys=True))
    return facts


@pytest.mark.slow
@pytest.mark.parametrize("subject", ["staff", "status", "u", "queued", "café Straße"])
def test_subject_only_preview_has_linear_discovery_bounds(strict_mailbox, subject):
    profiles = []
    for filler in (20, 3000):
        sim, client, clock = strict_mailbox(subject=subject, filler=filler)
        result = accepted_preview(client, {"folder": "INBOX", "subject": subject})
        assert result["count"] == 200 and not result["blocked"]
        searches = subject_searches(sim)
        assert searches and all(
            " SUBJECT " in command and not command.endswith(" ALL") for command in searches
        )
        facts = profile(sim, clock, f"subject:{subject}:filler={filler}")
        # Option A replaces #12's 3 FETCH / 600 DB rows / 60 commands with
        # mailbox-linear limits. At M=6,200: measured 23 / 19,000 / 65 versus
        # bounds 26 / 22,100 / 76; at M=240: 7 / 1,120 / 49. Raw reads remain
        # candidate-only; DB rows include every discovery envelope.
        assert_option_a_costs(sim, 200)
        assert facts["raw_rows"] == 600
        assert clock.elapsed < client.settings.call_timeout * 0.5
        assert not sim.search_rejections
        if not subject.isascii():
            assert all("CHARSET UTF-8" in command and "\x00LIT" in command for command in searches)
            assert sim.literal_modes and not any(sim.literal_modes)
        assert_readonly_headers(sim)
        profiles.append(facts)
    assert profiles[0]["raw_rows"] == profiles[1]["raw_rows"]


@pytest.mark.parametrize("subject", ["staff", "u", "café"])
@pytest.mark.parametrize("filler", [20, pytest.param(3000, marks=pytest.mark.slow)])
def test_subject_rule_preview_is_bounded_on_inbox(strict_mailbox, tmp_path, subject, filler):
    sim, client, clock = strict_mailbox(subject=subject, filler=filler)
    path = tmp_path / "subject-rules.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [{"field": "subject", "contains": subject, "folder": "Folders/Reviewed"}],
            }
        )
    )
    service = FolderRulesService(client.cleanup, path)
    try:
        batches, calls, partials = complete_rules_preview(
            sim, client, clock, tmp_path, service=service, mark_read=False
        )
    except MailError:
        assert False, "A valid subject rule must use accepted IMAP SEARCH transport"
    assert len(batches) <= 3 and sum(batch["count"] for batch in batches) == 200
    assert not any(batch["blocked"] for batch in batches)
    searches = subject_searches(sim)
    assert searches and all(
        " SUBJECT " in command and not command.endswith(" ALL") for command in searches
    )
    profile(sim, clock, f"rules:{subject}:filler={filler}")
    # Strict raw identity discovery continues across bounded preview calls.
    assert_rules_costs(sim, 200, calls, partials, literal_search=True)
    assert not sim.search_rejections
    assert_readonly_headers(sim, rules=True)


@pytest.mark.parametrize("literal_plus", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
def test_nonascii_identities_never_enter_search_transport(strict_mailbox, mixed, literal_plus):
    identifiers = ["<café@example.invalid>", "<雪@example.invalid>"]
    if mixed:
        identifiers += [f"<ascii-{index}@example.invalid>" for index in range(198)]
    sim, client, clock = strict_mailbox(
        identities=identifiers, filler=20, literal_plus=literal_plus
    )
    result = accepted_preview(client, {"folder": "INBOX", "subject": "staff"})
    assert result["count"] == len(identifiers)
    assert not sim.search_rejections
    literal_searches = [command for _, _, _, command in sim.commands if "\x00LIT" in command]
    assert not literal_searches and not sim.literal_modes
    assert not any("HEADER Message-ID" in command for _, _, _, command in sim.commands)
    facts = profile(sim, clock, f"identities:mixed={mixed}:literal_plus={literal_plus}")
    # Option A full discovery: measured 7 FETCH / 130 or 1,120 DB rows;
    # linear bounds are 8 FETCH / 151 or 1,240 rows (~11-16% headroom).
    assert_option_a_costs(sim, len(identifiers))
    assert facts["raw_rows"] == len(identifiers) * 3
    assert facts["total_commands"] <= 85
    assert clock.elapsed < client.settings.call_timeout * 0.5
    assert_readonly_headers(sim)


@pytest.mark.parametrize("selector", ["ids", "query"])
def test_nonascii_cross_folder_duplicate_remains_refused(strict_mailbox, selector):
    identifier = "<café@example.invalid>"
    sim, client, _ = strict_mailbox(identities=[identifier], filler=0)
    sim.add(Msg(hdr(identifier, subject="staff"), folder="Archive"))
    args = {"ids": [identifier]} if selector == "ids" else {"query": {"subject": "staff"}}
    before = sim.snapshot()
    preview = client.preview_action("move_to_trash", **args)
    assert preview["count"] == 0 and preview["token"] is None
    assert preview["expires_at"] is None and not client.cleanup.tokens._grants
    assert preview["excluded_count"] == 1 and preview["excluded_ids"] == [identifier]
    reason = "ambiguous_source" if selector == "ids" else "ambiguous_membership"
    assert preview["excluded_reasons"] == {reason: 1}
    assert not execution_log(client.cleanup.backend.log.path)
    assert sim.snapshot() == before
    assert not sim.search_rejections
    assert_readonly_headers(sim)


def test_nonascii_subject_with_other_criteria_keeps_subject_narrowing(strict_mailbox):
    sim, client, _ = strict_mailbox(subject="café", count=2, filler=100)
    result = accepted_preview(
        client, {"folder": "INBOX", "subject": "café", "from_": "sender@", "since": "2026-10-01"}
    )
    assert result["count"] == 2
    searches = subject_searches(sim)
    assert searches and all(
        " SUBJECT " in command and " FROM " not in command and " SINCE " in command
        for command in searches
    )
    # Option A: 610 DB rows replaces #12's six-row bound because old copies
    # anywhere must remain discoverable. The linear bound is 711 (~17% margin).
    assert_option_a_costs(sim, 2)
    assert sim.work.literal_loads_fetch == 6
    assert not sim.search_rejections
    assert_readonly_headers(sim)


def test_maximum_nonascii_identity_set_remains_bounded(strict_mailbox):
    identifiers = [f"<café-{index}@example.invalid>" for index in range(200)]
    sim, client, clock = strict_mailbox(identities=identifiers, filler=0, latency=0.005)
    result = accepted_preview(client, {"folder": "INBOX", "subject": "staff"})
    assert result["count"] == 200
    counts = command_counts(sim)
    assert counts["UID SEARCH"] <= 1 + len(sim.mailbox_names()) * 200
    # Option A adds two envelope scans: measured 5 FETCH / 1,000 DB rows,
    # replacing #12's 3 / 600 with 6 / 1,100 (20% / 10% margin).
    assert counts["UID FETCH"] <= 6 and sim.work.db_rows_fetch <= 1100
    assert sim.work.literal_loads_fetch == 600
    assert clock.elapsed < client.settings.call_timeout * 0.8
    assert not sim.search_rejections
    assert_readonly_headers(sim)


def test_distinct_unicode_subject_rules_do_not_share_cached_candidates(strict_mailbox, tmp_path):
    sim, client, _ = strict_mailbox(count=1, subject="café", filler=0)
    sim.add(Msg(hdr("<second@example.invalid>", subject="雪")))
    path = tmp_path / "distinct-subject-rules.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [
                    {"field": "subject", "contains": "café", "folder": "Folders/Reviewed"},
                    {"field": "subject", "contains": "雪", "folder": "Folders/Keep"},
                ],
            }
        )
    )
    result = FolderRulesService(client.cleanup, path).preview_rules(folder="INBOX")
    assert result["token"] and result["count"] == 2
    assert [rule["matched_count"] for rule in result["rules"]] == [1, 1]
    assert len(subject_searches(sim)) == 2
    assert not sim.search_rejections
    assert_readonly_headers(sim, rules=True)


@pytest.mark.parametrize("rules", [False, True])
def test_unencodable_subject_refuses_without_untrusted_diagnostics(strict_mailbox, tmp_path, rules):
    sim, client, _ = strict_mailbox(count=1, filler=0)
    subject = "untrusted-\ud800"
    if rules:
        path = tmp_path / "invalid-subject-rules.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "rules": [
                        {"field": "subject", "contains": subject, "folder": "Folders/Reviewed"}
                    ],
                }
            )
        )
        with pytest.raises(MailValidationError) as exc:
            FolderRulesService(client.cleanup, path).preview_rules(folder="INBOX")
        diagnostic = str(exc.value)
    else:
        with pytest.raises(MailValidationError) as exc:
            client.preview_action("move_to_trash", query={"folder": "INBOX", "subject": subject})
        diagnostic = str(exc.value)
    assert "untrusted" not in diagnostic and "UTF-8" in diagnostic
    assert not any(command.startswith("UID FETCH") for _, _, _, command in sim.commands)


def test_unencodable_identity_refuses_before_connection(strict_mailbox):
    sim, client, _ = strict_mailbox(count=1, filler=0)
    with pytest.raises(MailValidationError) as exc:
        client.preview_action("move_to_trash", ids=["<untrusted-\ud800@example.invalid>"])
    assert "UTF-8" in str(exc.value) and "untrusted" not in str(exc.value)
    assert not sim.commands
