"""Saved custom rules run in the reviewed rules engine like packaged rules; offline only."""

import asyncio
import hashlib
import json
import socket
from types import SimpleNamespace

import pytest
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations
from mcp.server.fastmcp.exceptions import ToolError
from test_send import Host

from proton_mail_mcp.folder_rules import FolderRulesService, SavedRule, engine_rules
from proton_mail_mcp.mail import MailValidationError
from proton_mail_mcp.rules_store import LocalRulesBackend, SavedRulesError, store_path
from proton_mail_mcp.rules_timer import RulesTimer
from proton_mail_mcp.server import create_mcp
from proton_mail_mcp.sieve import filter_spec, render_script

INVOICES = {"field": "subject", "contains": "Invoice", "folder": "Folders/Reviewed"}


@pytest.fixture
def engine(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Saved rules engine tests use the socketpair simulator only")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    simulations = []

    def build(*, packaged=(), folders=("Folders/Reviewed", "Folders/Other"), **options):
        sim = BridgeSim(folders=folders, **options)
        simulations.append(sim)
        directory = tmp_path / f"case-{len(simulations)}"
        directory.mkdir()
        client = make_client(sim, directory, call_timeout=300, enable_rules=True)
        rules = directory / "packaged-rules.json"
        rules.write_text(json.dumps({"version": 1, "rules": list(packaged)}))
        return SimpleNamespace(
            sim=sim,
            client=client,
            rules=rules,
            store=store_path(client.settings),
            service=FolderRulesService(client.cleanup, rules),
            directory=directory,
        )

    yield build
    for sim in simulations:
        sim.close()
        assert not sim.errors and not sim.violations


def add(case, mid, sender, subject="Hello", *, to="reader@example.test", seen=False):
    message = Msg(hdr(mid, from_=sender, subject=subject, to=to), flags={r"\Seen"} if seen else ())
    return case.sim.add(message)


def save(case, name, folder, conditions, **options):
    spec = filter_spec(name, folder, conditions, **options)
    LocalRulesBackend(case.store).create_filter(name, render_script(spec), timeout=5)
    return spec


def approved_ids(case, preview):
    if preview["token"] is None:
        return set()
    grant = case.client.cleanup.tokens.inspect(preview["token"], "apply_rules")
    return {row["message_id"] for row in grant.messages}


def searches(case):
    return [command for *_, command in case.sim.commands if command.startswith("UID SEARCH")]


def test_saved_rules_follow_packaged_rules_and_the_first_match_wins(engine):
    case = engine(packaged=[INVOICES])
    invoice = add(case, "<a@x.test>", "Shop <billing@shop.example>", "Invoice 12")
    news = add(case, "<b@x.test>", "Shop <news@shop.example>", "Weekly news")
    other = add(case, "<c@x.test>", "Friend <friend@elsewhere.example>", "Hi")
    save(case, "Shop", "Folders/Other", [{"field": "from", "contains": "shop.example"}])
    preview = case.service.preview_rules()
    packaged, saved = preview["rules"]
    assert (packaged["rule"], packaged["field"], packaged["contains"]) == (1, "subject", "Invoice")
    assert "name" not in packaged and "mark_read" not in packaged
    assert saved["rule"] == 2 and saved["target"] == "Folders/Other"
    assert {key: saved[key] for key in ("name", "conditions", "match", "mark_read")} == {
        "name": "Shop",
        "conditions": [{"field": "from", "contains": "shop.example"}],
        "match": "all",
        "mark_read": False,
    }
    # The invoice matches both rules; the packaged rule, first, takes it.
    assert (packaged["count"], saved["count"]) == (1, 1) and preview["count"] == 2
    result = case.service.apply_rules(preview["token"])
    assert result["done"] == 2 and not result["partial"]
    assert (invoice.folder, news.folder, other.folder) == (
        "Folders/Reviewed",
        "Folders/Other",
        "INBOX",
    )
    assert all("UID STORE" not in command for *_, command in mutations(case.sim))
    for log_id in result["log_ids"]:
        assert case.client.undo_action(log_id)["partial"] is False
    assert (invoice.folder, news.folder) == ("INBOX", "INBOX")


MAILBOX = [
    ("<m1@x.test>", "GitHub <notifications@github.example>", "PR merged", "me@example.test"),
    # A display-name domain is searchable but never an address-rule match.
    # Address-impersonating names are covered by the guarded safety tests.
    (
        "<m2@x.test>",
        '"github.example notifications" <news@elsewhere.example>',
        "PR merged",
        "me@example.test",
    ),
    ("<m3@x.test>", "Team <team@example.test>", "Weekly report", "Me <ME@EXAMPLE.TEST>, b@c.test"),
    ("<m4@x.test>", "Billing <billing@shop.example>", "Invoice", "reader@example.test"),
    ("<m5@x.test>", "Shop <x@y.example>", "invoice 12", "reader@example.test"),
]


@pytest.mark.parametrize(
    "conditions,match,expected",
    [
        ([{"field": "from", "contains": "github.example"}], "all", {"m1"}),
        ([{"field": "from", "is": "NOTIFICATIONS@github.example"}], "all", {"m1"}),
        ([{"field": "from", "is": "github.example"}], "all", set()),
        ([{"field": "to", "is": "me@example.test"}], "all", {"m1", "m2", "m3"}),
        ([{"field": "to", "contains": "b@c.test"}], "all", {"m3"}),
        ([{"field": "subject", "is": "invoice"}], "all", {"m4"}),
        ([{"field": "subject", "contains": "INVOICE"}], "all", {"m4", "m5"}),
        (
            [{"field": "from", "contains": "shop.example"}, {"field": "subject", "is": "Invoice"}],
            "all",
            {"m4"},
        ),
        # m2 passes both server keys (its raw From names github) but not the address.
        (
            [
                {"field": "from", "contains": "github.example"},
                {"field": "subject", "is": "PR merged"},
            ],
            "all",
            {"m1"},
        ),
        (
            [
                {"field": "from", "contains": "github.example"},
                {"field": "subject", "is": "Weekly report"},
            ],
            "any",
            {"m1", "m3"},
        ),
        # No word to search for: one full scan, decided locally.
        ([{"field": "from", "contains": "@"}, {"field": "subject", "is": "x"}], "any", None),
    ],
)
def test_saved_rule_conditions_decide_like_their_sieve_script(engine, conditions, match, expected):
    case = engine()
    for mid, sender, subject, to in MAILBOX:
        add(case, mid, sender, subject, to=to)
    save(case, "Rule", "Folders/Other", conditions, match=match)
    preview = case.service.preview_rules()
    expected = {mid[1:3] for mid, *_ in MAILBOX} if expected is None else expected
    assert approved_ids(case, preview) == {f"<{name}@x.test>" for name in expected}
    assert preview["rules"][0]["matched_count"] == len(expected)
    # One search narrows each rule; a packaged rule would have taken m2 by its name.
    [search] = searches(case)[:1]
    assert search.startswith("UID SEARCH CHARSET UTF-8 ")
    assert not mutations(case.sim)


def criteria(conditions, match="all"):
    return SavedRule(filter_spec("Rule", "Folders/Other", conditions, match=match)).criteria()


def address_keys(header, word):
    return ["OR", "OR", header, f'"{word}"'.encode(), header, b'"\\\\"', header, b'"=?"']


def test_saved_rule_search_keys_are_a_superset_with_at_most_one_literal():
    github = {"field": "from", "is": "notifications@github.com"}
    subject = {"field": "subject", "contains": "Weekly"}
    assert criteria([github]) == (address_keys("FROM", "notifications"), None)
    both = address_keys("FROM", "notifications") + ["SUBJECT", b'"Weekly"']
    assert criteria([github, subject]) == (both, None)
    assert criteria([github, subject], "any") == (["OR", *both], None)
    to = {"field": "to", "contains": "me@x.test"}
    # The longest word of the address narrows (here "test").
    three = ["OR", *address_keys("FROM", "notifications"), "OR", *address_keys("TO", "test")]
    assert criteria([github, to, subject], "any") == (three + ["SUBJECT", b'"Weekly"'], None)
    wide = {"field": "subject", "is": "Café"}
    second = {"field": "subject", "contains": "über"}
    # all: one non-ASCII value narrows as the literal, last; the other decides locally.
    assert criteria([wide, github, second]) == (
        address_keys("FROM", "notifications") + ["SUBJECT"],
        "Café".encode(),
    )
    # any: its one non-ASCII disjunct goes last; two cannot share one command.
    assert criteria([wide, github], "any") == (
        ["OR", *address_keys("FROM", "notifications"), "SUBJECT"],
        "Café".encode(),
    )
    assert criteria([wide, second], "any") == ([], None)
    # A value without a word cannot narrow: all drops it, any scans everything.
    symbol = {"field": "from", "contains": "@"}
    assert criteria([symbol, subject]) == (["SUBJECT", b'"Weekly"'], None)
    assert criteria([symbol, subject], "any") == ([], None)


def test_a_saved_rule_marks_only_its_own_matches_read_and_undo_restores(engine):
    case = engine(packaged=[INVOICES])
    invoice = add(case, "<a@x.test>", "Shop <billing@shop.example>", "Invoice 12")
    github = add(case, "<b@x.test>", "GitHub <notifications@github.example>", "PR merged")
    save(
        case,
        "GitHub",
        "Folders/Other",
        [{"field": "from", "is": "notifications@github.example"}],
        mark_read=True,
    )
    preview = case.service.preview_rules()
    assert preview["mark_read"] is False and preview["rules"][1]["mark_read"] is True
    uid = case.sim.uid_of("INBOX", github.pid)
    result = case.service.apply_rules(preview["token"])
    assert result["done"] == 2 and not result["partial"]
    assert (invoice.folder, invoice.flags) == ("Folders/Reviewed", set())
    assert (github.folder, github.flags) == ("Folders/Other", {r"\Seen"})
    stores = [command for *_, command in mutations(case.sim) if "STORE" in command]
    assert stores == [f"UID STORE {uid} +FLAGS.SILENT (\\Seen)"]
    for log_id in result["log_ids"]:
        assert case.client.undo_action(log_id)["partial"] is False
    assert (github.folder, github.flags) == ("INBOX", set())
    assert (invoice.folder, invoice.flags) == ("INBOX", set())


def test_saving_a_rule_invalidates_outstanding_rule_previews(engine):
    case = engine(packaged=[INVOICES])
    add(case, "<a@x.test>", "Shop <billing@shop.example>", "Invoice 12")
    packaged_digest = hashlib.sha256(case.rules.read_bytes()).hexdigest()
    assert engine_rules(case.rules, case.store)[1] == packaged_digest
    preview = case.service.preview_rules()
    grant = case.client.cleanup.tokens.inspect(preview["token"], "apply_rules")
    # Without saved rules a token binds the packaged file's digest, as before.
    assert grant.params["rules_digest"] == packaged_digest
    save(case, "Shop", "Folders/Other", [{"field": "from", "contains": "shop.example"}])
    assert engine_rules(case.rules, case.store)[1] != packaged_digest
    with pytest.raises(MailValidationError, match="Folder rules changed after preview"):
        case.service.apply_rules(preview["token"])
    assert not mutations(case.sim)


def test_an_invalid_saved_store_refuses_every_rules_path_before_mail(engine):
    case = engine(packaged=[INVOICES])
    add(case, "<a@x.test>", "Shop <billing@shop.example>", "Invoice 12")
    token = case.service.preview_rules()["token"]
    commands = len(case.sim.commands)
    case.store.write_text("{")
    case.store.chmod(0o600)
    with pytest.raises(SavedRulesError, match="Cannot load valid saved rules"):
        case.service.preview_rules()
    with pytest.raises(SavedRulesError):
        case.service.apply_rules(token)
    with pytest.raises(SavedRulesError):
        FolderRulesService(case.client.cleanup, case.rules)
    timer = RulesTimer(case.client.cleanup, rules_path=case.rules, state_path=case.directory / "s")
    with pytest.raises(SavedRulesError):
        timer.run()
    assert len(case.sim.commands) == commands and not mutations(case.sim)
    mcp = create_mcp(case.client.settings, case.client)
    with pytest.raises(ToolError, match="Validation error: Cannot load valid saved rules"):
        asyncio.run(mcp._tool_manager.call_tool("preview_rules", {}))


def test_the_timer_applies_saved_rules_but_never_marks_read(engine):
    case = engine()
    github = add(case, "<a@x.test>", "GitHub <notifications@github.example>", "PR merged")
    other = add(case, "<b@x.test>", "Friend <friend@elsewhere.example>", "Hi")
    save(
        case,
        "GitHub",
        "Folders/Other",
        [{"field": "from", "is": "notifications@github.example"}],
        mark_read=True,
    )
    timer = RulesTimer(
        case.client.cleanup, rules_path=case.rules, state_path=case.directory / "state.json"
    )
    result = timer.run()
    assert result["count"] == 1 and result["status"] == "initialized"
    assert (github.folder, github.flags) == ("Folders/Other", set())
    assert other.folder == "INBOX"
    assert all("STORE" not in command for *_, command in mutations(case.sim))
    with pytest.raises(MailValidationError, match="distinct paths"):
        RulesTimer(case.client.cleanup, rules_path=case.rules, state_path=case.store)


def test_protected_senders_and_batch_limits_apply_to_saved_rules(engine):
    case = engine()
    # The default policy protects bank-like senders whichever rule matches them.
    bank = add(case, "<bank@x.test>", "Alerts <alerts@mybank.example>", "Statement ready")
    first = add(case, "<one@x.test>", "Shop <a@shop.example>", "Statement ready")
    second = add(case, "<two@x.test>", "Shop <b@shop.example>", "Statement ready")
    save(case, "Statements", "Folders/Other", [{"field": "subject", "is": "statement ready"}])
    preview = case.service.preview_rules(limit=2)
    [report] = preview["rules"]
    assert report["matched_count"] == preview["total_matches"] == 3
    assert preview["batch_count"] == 2 and preview["remaining"] == 1
    assert len(report["blocked"]) == 1
    assert report["blocked"][0]["reason"].startswith("Protected financial, insurance or govern")
    assert report["blocked"][0]["message_id"] == "<bank@x.test>" and report["count"] == 1
    result = case.service.apply_rules(preview["token"])
    assert result["done"] == 1 and bank.folder == "INBOX"
    assert {first.folder, second.folder} == {"Folders/Other", "INBOX"}
    later = case.service.preview_rules()
    assert later["count"] == 1 and len(later["rules"][0]["blocked"]) == 1
    rest = case.service.apply_rules(later["token"], limit=1)
    assert rest["done"] == 1 and not rest["partial"]
    assert (first.folder, second.folder, bank.folder) == (
        "Folders/Other",
        "Folders/Other",
        "INBOX",
    )


def test_the_rules_confirmation_names_saved_rules_and_their_mark_read(engine):
    case = engine()
    add(case, "<a@x.test>", "GitHub <notifications@github.example>", "PR merged")
    save(
        case,
        "GitHub",
        "Folders/Other",
        [{"field": "from", "is": "notifications@github.example"}],
        mark_read=True,
    )
    mcp = create_mcp(case.client.settings, case.client)

    def call(tool, arguments, host=None):
        return asyncio.run(mcp._tool_manager.call_tool(tool, arguments, context=host))

    preview = call("preview_rules", {})
    host = Host("decline")
    with pytest.raises(ToolError, match="User confirmation was not accepted"):
        call("apply_rules", {"token": preview["token"]}, host)
    sentence, details = host.prompts[0].split("\n", 1)
    assert "by the reviewed folder rules, marking those of mark_read rules read." in sentence
    [shown] = json.loads(details)["rules"]
    assert shown == {
        "rule": 1,
        "name": "GitHub",
        "conditions": [{"field": "from", "is": "notifications@github.example"}],
        "match": "all",
        "mark_read": True,
        "target": "Folders/Other",
        "count": 1,
    }
    assert not mutations(case.sim)
