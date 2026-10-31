"""A whole-batch token still applies when one strict review fills a preview call.

The real preview_rules and apply_rules run on a fake server whose clock charges each
command, priced so a warm one-row review costs REVIEW seconds. The review cost itself
is inferred from live call times. Which folder holds it is unmeasured, so the tests
that finish a batch run on both models: all EXAMINEs equal, and All Mail twelve times
the others.
"""

import collections
import json
from dataclasses import replace

import pytest
from test_cleanup_mutations import uid_values
from test_folder_rules import mailbox as mailbox
from test_folder_rules import rule
from test_preview_proton_shape import WorkClock
from test_rules_apply_audit_gates import AuditedMutatingImap

from proton_mail_mcp import mail

REVIEW = 14.0
EXECUTE_IO_SECONDS = 43.2  # guarded.py: the 48 s response budget less its 10% reserve.
SETUP = 1.0
ROW = 0.00335  # tests/bridge_costs.py: one raw header row.
MOVE = (2.0, 0.05)  # guarded.py's allowance: per command and per further UID.
FOLDERS = ("Sent", "Drafts", "Archive", "Spam", "Labels/x")
TARGETS = tuple(f"Folders/{name}" for name in "ABCDE")
MODELS = {
    "equal": lambda folder: 1.0,
    "all-mail-heavy": lambda folder: {"All Mail": 12.0, "INBOX": 2.0}.get(folder, 1.0),
}


def priced_case(mailbox, monkeypatch, weight, rules, *, matches=60, unit=0.0):
    """Rows take turns across the rules, so every rule holds a share of each batch."""
    specs = [
        rule(contains=f"sender{index}@example.com", folder=TARGETS[index]) for index in range(rules)
    ]
    case = mailbox(count=matches, rules=specs)
    for uid, item in case.fake.folders["INBOX"].items():
        item.headers = item.headers.replace(
            b"sender@example.com", f"sender{(int(uid) - 1) % rules}@example.com".encode()
        )
    folders = {**case.fake.folders, **{name: {} for name in (*FOLDERS, *TARGETS)}}
    fake = case.fake = AuditedMutatingImap(folders)
    fake.reports_uidnext = True
    clock = case.clock = WorkClock()
    clock.elapsed = 100.0
    monkeypatch.setattr(mail, "time", clock)
    case.cleanup.tokens.clock = lambda: clock.elapsed
    settings = replace(case.cleanup.settings, call_timeout=120, execute_timeout=120)
    case.cleanup.settings = case.cleanup.client.settings = settings
    case.unit, case.reads, case.move = unit, 1.0, MOVE
    case.examined = collections.Counter()
    select, uid, login, listing = fake.select, fake.uid, fake.login, fake.list

    def charged_select(folder, *, readonly):
        name = folder.strip('"')
        case.examined[name] += 1
        clock.elapsed += case.reads * case.unit * weight(name)
        return select(folder, readonly=readonly)

    def charged_uid(command, *args):
        if command == "FETCH":
            rows = len(uid_values(args[0])) if ":" not in args[0] else 1
            clock.elapsed += case.reads * ROW * rows
        elif command == "SEARCH":
            clock.elapsed += case.reads * 0.02
        elif command == "MOVE":
            clock.elapsed += case.move[0] + case.move[1] * (len(uid_values(args[0])) - 1)
        return uid(command, *args)

    def charged_login(*args):
        clock.elapsed += SETUP
        return login(*args)

    def charged_list(*args):
        clock.elapsed += case.reads * 0.02
        return listing(*args)

    fake.select, fake.uid, fake.login, fake.list = (
        charged_select,
        charged_uid,
        charged_login,
        charged_list,
    )
    case.cleanup.client.imap_factory = lambda *args, **kwargs: fake
    return case


def review_unit(mailbox, weight, rules):
    """The price of one EXAMINE: a warm one-row review's total comes to REVIEW."""
    with pytest.MonkeyPatch.context() as local:
        case = priced_case(mailbox, local, weight, rules, matches=4)
        first = case.rules.preview_rules(limit=1)
        plan, review = case.rules._plan, []

        def traced(*args, **options):
            before = collections.Counter(case.examined)
            planned = plan(*args, **options)
            review.append(case.examined - before)
            return planned

        case.rules._plan = traced
        case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
    return REVIEW / sum(weight(name) * count for name, count in review[0].items())


def slow_case(mailbox, monkeypatch, model, rules=1, matches=60):
    weight = MODELS[model]
    unit = review_unit(mailbox, weight, rules)
    return priced_case(mailbox, monkeypatch, weight, rules, matches=matches, unit=unit)


def apply_until_done(case, token):
    """Follow one token's continuations, each within the execute budget and making progress."""
    moved = 0
    while token:
        started = case.clock.elapsed
        result = case.rules.apply_rules(token)
        assert case.clock.elapsed - started <= EXECUTE_IO_SECONDS
        assert result["done"] and not result["uncertain"], result.get("stop_reason")
        moved += result["done"]
        token = result["continuation_token"]
    return moved


def next_token(case, *, limit=200):
    """Follow a fresh cursor chain to its first token."""
    cursor = None
    for _ in range(12):
        preview = case.rules.preview_rules(limit=limit, cursor=cursor)
        if preview["token"]:
            return preview
        cursor = preview["next_cursor"]
        assert cursor, preview["reason"]
    pytest.fail("no token in 12 calls")


@pytest.mark.parametrize("model", MODELS)
def test_one_rule_applies_its_whole_batch_within_the_execute_budget(mailbox, monkeypatch, model):
    case = slow_case(mailbox, monkeypatch, model)
    preview = next_token(case)
    assert preview["batch_count"] == preview["count"] == 60 and preview["remaining"] == 0

    started = case.clock.elapsed
    result = case.rules.apply_rules(preview["token"])

    assert case.clock.elapsed - started <= EXECUTE_IO_SECONDS
    assert result["done"] == result["run_done"] == result["approved_total"] == 60
    assert not result["partial"] and result["continuation_token"] is None
    assert result["uncertain"] == 0 and not case.fake.folders["INBOX"]
    assert len(case.fake.folders["Folders/A"]) == 60


@pytest.mark.parametrize("rules", [3, 5])
@pytest.mark.parametrize("model", MODELS)
def test_rules_each_get_their_own_token_and_all_finish(mailbox, monkeypatch, model, rules):
    case = slow_case(mailbox, monkeypatch, model, rules)
    expected = {name: set() for name in TARGETS[:rules]}
    for uid, item in case.fake.folders["INBOX"].items():
        expected[TARGETS[(int(uid) - 1) % rules]].add(id(item))
    moved, batches = 0, []

    while case.fake.folders["INBOX"] and len(batches) < 60:
        preview = next_token(case)
        grant = case.cleanup.tokens.inspect(preview["token"], "apply_rules")
        assert len({row["rule_index"] for row in grant.selector["reviewed_rows"]}) == 1
        batches.append(preview["batch_count"])
        moved += apply_until_done(case, preview["token"])

    assert moved == 60 and not case.fake.folders["INBOX"]
    assert batches[0] == 60 // rules
    assert {
        name: {id(item) for item in case.fake.folders[name].values()} for name in expected
    } == expected


@pytest.mark.parametrize("model", MODELS)
def test_one_rule_with_a_protected_row_finishes(mailbox, monkeypatch, model):
    case = slow_case(mailbox, monkeypatch, model)
    case.path.write_text(
        json.dumps(
            {"version": 1, "rules": [rule(field="subject", contains="Hello", folder=TARGETS[0])]}
        )
    )
    protected = case.fake.folders["INBOX"]["5"]
    protected.headers = protected.headers.replace(b"sender0@example.com", b"relative@gmail.com")

    preview = next_token(case)
    assert preview["batch_count"] == 60 and preview["count"] == 59
    assert len(preview["rules"][0]["blocked"]) == 1

    assert apply_until_done(case, preview["token"]) == 59
    assert set(case.fake.folders["INBOX"]) == {"5"}
    assert len(case.fake.folders[TARGETS[0]]) == 59


@pytest.mark.parametrize("model", MODELS)
def test_fresh_previews_after_an_apply_can_overrun_and_still_finish(mailbox, monkeypatch, model):
    case = slow_case(mailbox, monkeypatch, model, matches=120)
    batches = []

    while case.fake.folders["INBOX"] and len(batches) < 40:
        preview = next_token(case, limit=60)
        batches.append(preview["batch_count"])
        assert apply_until_done(case, preview["token"]) == preview["batch_count"]

    assert not case.fake.folders["INBOX"] and sum(batches) == 120
    # A new chain's first call costs more and its overrun halves the batch.
    assert batches[:2] == [60, 30]


@pytest.mark.parametrize("model", MODELS)
def test_one_slow_sample_does_not_hold_a_fast_mailbox_at_one_rule_per_token(
    mailbox, monkeypatch, model
):
    case = slow_case(mailbox, monkeypatch, model, rules=3)

    def rules_in(preview):
        grant = case.cleanup.tokens.inspect(preview["token"], "apply_rules")
        return {row["rule_index"] for row in grant.selector["reviewed_rows"]}

    # One slow sample, from a chain whose token is never applied.
    assert rules_in(next_token(case)) == {1}
    case.unit /= 7

    # The sample still picks this call's review, and that review shows the mailbox is fast.
    first = next_token(case)
    assert rules_in(first) == {1} and first["batch_count"] == 20
    assert apply_until_done(case, first["token"]) == 20

    second = next_token(case)
    assert rules_in(second) == {2, 3} and second["batch_count"] == 40
    assert apply_until_done(case, second["token"]) == 40

    assert not case.fake.folders["INBOX"]
    assert [len(case.fake.folders[name]) for name in TARGETS[:3]] == [20, 20, 20]


def test_a_whole_batch_dearer_than_one_row_leaves_the_timing_sample_alone(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch, "equal")
    held = case.rules.preview_rules()
    sample = case.cleanup._rules_warm_review

    preview = case.rules.preview_rules(cursor=held["next_cursor"])

    assert preview["batch_count"] == 60
    assert case.cleanup._rules_warm_review == sample


def test_only_the_round_after_a_chunk_reserves_another_verification(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch, "equal")
    preview = next_token(case)
    plan = case.rules._plan

    def plan_leaving_four_seconds(connection, deadline, *args, **options):
        planned = plan(connection, deadline, *args, **options)
        case.clock.elapsed = deadline.io_end - 4.0
        # From here only the reserve can stop a chunk, and a cheap MOVE leaves time to try.
        # Free reads test the reserve alone. They do not prove that a chunk cut to the time
        # left moves: with reads charged, 4 s left moves 0 rows on every retry.
        case.reads, case.move = 0.0, (0.5, 0.0)
        return planned

    monkeypatch.setattr(case.rules, "_plan", plan_leaving_four_seconds)

    result = case.rules.apply_rules(preview["token"])

    # The rows the plan just verified move, though a preview-sized review no longer fits.
    assert 0 < result["done"] < 60 and result["partial"] and not result["uncertain"]
    # The rest need a review first, so no further chunk starts.
    assert result["stop_reason"].startswith("Insufficient execute headroom for verification")
    assert result["continuation_token"]
