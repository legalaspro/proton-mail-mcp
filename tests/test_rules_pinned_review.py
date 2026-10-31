"""One slow review per call serves a whole batch; a bad message or failed call keeps the chain."""

import copy
import json

import pytest
import test_rules_finishing_batch as harness
from test_folder_rules import mailbox as mailbox
from test_folder_rules import rule
from test_preview_exclusions import DESTINATION, SENDER
from test_preview_exclusions import github_mailbox as github_mailbox
from test_preview_rules_bounded import assert_read_only
from test_rules_finishing_batch import MATCHES, first_token, retest_case

from proton_mail_mcp import mail
from proton_mail_mcp.cleanup_lookup import _names_requested_identity, _raw_identity_candidates
from proton_mail_mcp.folder_rules import FolderRulesService
from proton_mail_mcp.mail import CallDeadlineExceeded, MailError, MailValidationError


def peer_copy(case, uid, old, new):
    """An All Mail row with the same Message-ID as an INBOX row and changed headers."""
    peer = copy.deepcopy(case.fake.folders["INBOX"][str(uid)])
    peer.headers = peer.headers.replace(old, new)
    case.fake.folders["All Mail"][str(MATCHES + uid)] = peer


def other_sender(case, uid):
    peer_copy(
        case,
        uid,
        b"From: Sender <notifications@github.com>",
        b"From: Someone Else <notifications@github.com>",
    )


def slow_case(mailbox, monkeypatch, review=14.0):
    monkeypatch.setattr(harness, "REVIEW", review)
    walk_per_call = int((18 - harness.SETUP - review) / harness.RANGE)
    return retest_case(mailbox, monkeypatch, ranges=walk_per_call + 4)


ODD, EVEN = (
    [str(uid) for uid in range(1, MATCHES + 1, 2)],
    [str(uid) for uid in range(2, MATCHES + 1, 2)],
)


def second_rule(case, uids):
    """These INBOX rows now match a second rule with its own destination."""
    specs = [
        rule(contains="notifications@github.com"),
        rule(contains="updates@github.com", folder="Folders/Other"),
    ]
    case.path.write_text(json.dumps({"version": 1, "rules": specs}))
    for uid in uids:
        item = case.fake.folders["INBOX"][uid]
        item.headers = item.headers.replace(b"notifications@github.com", b"updates@github.com")


def approved_uids(case, result):
    grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
    return [row["uid"] for row in grant.messages]


def first_batch_reply(case):
    """Follow one cursor past the audit's calls to the first reply with a reviewed batch."""
    cursor = None
    for _ in range(30):
        reply = case.rules.preview_rules(cursor=cursor)
        if reply["batch_count"]:
            return reply
        cursor = reply["next_cursor"]
    pytest.fail("no reviewed batch in 30 calls")


@pytest.mark.parametrize("review", [8.5, 9.0, 12.0, 14.0, 15.0, 16.0])
def test_review_too_slow_for_two_puts_every_match_in_one_batch(mailbox, monkeypatch, review):
    case = slow_case(mailbox, monkeypatch, review)

    result = first_token(case)[-1]

    assert case.sizes == [MATCHES]
    assert result["batch_count"] == result["count"] == MATCHES
    assert result["remaining"] == 0 and result["next_cursor"] is None
    assert_read_only(case.fake)


def test_review_cheap_enough_for_two_still_secures_one_row_first(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch, 8.0)

    result = first_token(case)[-1]

    assert case.sizes == [1, MATCHES]
    assert result["batch_count"] == MATCHES and result["next_cursor"] is None


def test_review_longer_than_the_time_left_still_runs_one_row(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch)
    first_token(case)
    monkeypatch.setattr(harness, "REVIEW", 17.5)
    case.cleanup._rules_warm_review = 17.5

    case.sizes.clear()
    result = case.rules.preview_rules()

    assert case.sizes[0] == 1
    assert result["batch_count"] == 1 and result["remaining"] == MATCHES - 1


def test_new_chain_on_a_finished_audit_gets_its_whole_batch_at_once(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch)
    first_token(case)

    case.sizes.clear()
    result = case.rules.preview_rules()

    assert case.sizes == [MATCHES]
    assert result["batch_count"] == MATCHES and result["next_cursor"] is None


def test_batch_too_large_for_the_call_halves_without_stalling(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch)
    first_token(case)  # Reach a timing sample, then make rows expensive.
    case.row = 0.25
    cursor, sizes, approved = None, [], 0
    for _ in range(20):
        case.sizes.clear()
        result = case.rules.preview_rules(cursor=cursor)
        sizes.append(case.sizes[0])
        approved += result["batch_count"]
        cursor = result["next_cursor"]
        state = case.cleanup._rules_preview_states.get(cursor, {})
        assert state.get("batch_scale", 1) == 1 and state.get("stalled", 0) == 0
        if not cursor:
            break
    assert sizes[:3] == [MATCHES, MATCHES // 2, MATCHES // 4]
    assert approved == MATCHES and cursor is None


@pytest.mark.parametrize(
    "order,first_rule,second",
    [("oldest", ODD, EVEN), ("newest", EVEN[::-1], ODD[::-1])],
    ids=["oldest", "newest"],
)
def test_whole_batch_holds_one_rule_and_order_holds_within_it(
    mailbox, monkeypatch, order, first_rule, second
):
    case = slow_case(mailbox, monkeypatch)
    second_rule(case, EVEN)

    first = first_token(case, order=order)[-1]

    assert case.sizes == [MATCHES // 2]
    assert approved_uids(case, first) == first_rule
    assert first["batch_count"] == first["count"] == MATCHES // 2
    assert first["total_matches"] == MATCHES and first["remaining"] == MATCHES // 2
    assert [report["matched_count"] for report in first["rules"]] == [MATCHES // 2] * 2
    assert sorted(report["count"] for report in first["rules"]) == [0, MATCHES // 2]

    case.sizes.clear()
    last = case.rules.preview_rules(order=order, cursor=first["next_cursor"])

    assert case.sizes == [MATCHES // 2]
    assert approved_uids(case, last) == second
    assert last["remaining"] == 0 and last["next_cursor"] is None


def test_limit_caps_a_batch_of_one_rule(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch)
    second_rule(case, EVEN)

    result = first_token(case, limit=10)[-1]

    assert approved_uids(case, result) == ODD[:10]
    assert result["remaining"] == MATCHES - 10


def test_review_cheap_enough_for_two_still_spans_rules(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch, 8.0)
    second_rule(case, EVEN)

    result = first_token(case)[-1]

    assert case.sizes == [1, MATCHES]
    assert [report["count"] for report in result["rules"]] == [MATCHES // 2] * 2


def test_rule_with_one_match_takes_the_one_row_path(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch)
    second_rule(case, ["1"])

    first = first_token(case)[-1]

    assert case.sizes[0] == 1
    assert approved_uids(case, first) == ["1"] and first["remaining"] == MATCHES - 1

    case.sizes.clear()
    last = case.rules.preview_rules(cursor=first["next_cursor"])

    assert case.sizes == [MATCHES - 1]
    assert last["batch_count"] == MATCHES - 1 and last["next_cursor"] is None


def test_rule_of_only_protected_rows_hands_over_to_the_next_rule(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch)
    specs = [
        rule(contains="relative@gmail.com"),
        rule(contains="notifications@github.com", folder="Folders/Other"),
    ]
    case.path.write_text(json.dumps({"version": 1, "rules": specs}))
    for uid in ODD:
        item = case.fake.folders["INBOX"][uid]
        item.headers = item.headers.replace(b"notifications@github.com", b"relative@gmail.com")

    protected = first_batch_reply(case)

    assert protected["token"] is None and protected["count"] == 0
    assert protected["batch_count"] == MATCHES // 2 and protected["remaining"] == MATCHES // 2
    assert len(protected["rules"][0]["blocked"]) == MATCHES // 2

    last = case.rules.preview_rules(cursor=protected["next_cursor"])

    assert approved_uids(case, last) == EVEN
    assert last["remaining"] == 0 and last["next_cursor"] is None


def test_overrun_halves_within_the_rule_and_serves_every_row_once(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch)
    second_rule(case, EVEN)
    first = first_token(case)[-1]
    case.row = 0.25  # The second rule's whole batch cannot fit now.
    approved, sizes, cursor = approved_uids(case, first), [], first["next_cursor"]

    for _ in range(20):
        case.sizes.clear()
        result = case.rules.preview_rules(cursor=cursor)
        sizes.append(case.sizes[0])
        if result["token"]:
            approved.extend(approved_uids(case, result))
        cursor = result["next_cursor"]
        if not cursor:
            break

    assert sizes[:3] == [MATCHES // 2, MATCHES // 4, MATCHES // 8]
    assert cursor is None and sorted(approved, key=int) == sorted(ODD + EVEN, key=int)


def test_inconsistent_copy_in_a_batch_is_excluded_and_the_rest_keep_their_token(
    mailbox, monkeypatch
):
    case = slow_case(mailbox, monkeypatch)
    other_sender(case, 8)

    result = first_token(case)[-1]

    assert case.sizes == [MATCHES]
    assert result["batch_count"] == result["count"] == MATCHES - 1
    assert result["excluded_count"] == 1
    assert result["excluded_ids"] == ["<rule-8@example.test>"]
    assert result["excluded_reasons"] == {"inconsistent_sender_headers": 1}
    assert result["remaining"] == 0 and result["next_cursor"] is None
    assert result["total_matches"] == result["rules"][0]["matched_count"] == MATCHES
    assert result["rules"][0]["count"] == MATCHES - 1
    grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
    assert "8" not in {row["uid"] for row in grant.messages}
    assert "8" not in {row["uid"] for row in grant.selector["reviewed_rows"]}
    assert_read_only(case.fake)


@pytest.mark.parametrize("warm", [None, 10.0], ids=["one-row-first", "whole-batch"])
def test_preview_excludes_and_apply_moves_only_the_clean_rows(github_mailbox, tmp_path, warm):
    sim, client, sources, copies = github_mailbox(4, bad_count=1)
    client.cleanup._rules_warm_review = warm
    path = tmp_path / "synthetic-rules.json"
    path.write_text(
        '{"version": 1, "rules": [{"field": "from", "contains": "%s", "folder": "%s"}]}'
        % (SENDER, DESTINATION)
    )
    rules = FolderRulesService(client.cleanup, path)

    result = rules.preview_rules()
    assert result["count"] == 3 and result["excluded_count"] == 1
    moved = rules.apply_rules(result["token"])

    assert moved["done"] == 3 and not moved["partial"]
    assert [item.folder for item in sources].count("INBOX") == 1
    assert [item.folder for item in sources].count(DESTINATION) == 3

    # The bad message stays, so the next chain finds it again and only it.
    again = rules.preview_rules()
    assert again["token"] is None and again["next_cursor"] is None
    assert again["excluded_count"] == 1 and again["remaining"] == 0
    assert again["reason"] == "All remaining matches were excluded; no rules token issued"


def test_excluded_message_stays_excluded_for_the_whole_chain(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch)
    other_sender(case, 8)

    first = first_token(case, limit=20)[-1]
    assert first["batch_count"] == 19 and first["remaining"] == MATCHES - 20
    cursor, approved = first["next_cursor"], 19
    for _ in range(4):
        result = case.rules.preview_rules(limit=20, cursor=cursor)
        assert result["excluded_ids"] == ["<rule-8@example.test>"]
        approved += result["batch_count"]
        cursor = result["next_cursor"]
        if not cursor:
            break
    assert approved == MATCHES - 1 and cursor is None and result["remaining"] == 0


def test_excluded_message_that_left_the_source_is_no_longer_reported(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch)
    other_sender(case, 8)
    first = first_token(case, limit=20)[-1]
    assert first["excluded_ids"] == ["<rule-8@example.test>"]

    del case.fake.folders["INBOX"]["8"]
    result = case.rules.preview_rules(limit=20, cursor=first["next_cursor"])

    assert result["excluded_count"] == 0 and result["excluded_ids"] == []
    assert result["excluded_reasons"] == {} and result["batch_count"] == 20


def test_row_refused_alone_is_passed_within_the_call_when_time_remains(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch, 4.0)
    other_sender(case, 2)

    first = first_token(case, limit=1)[-1]
    second = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])

    assert first["batch_count"] == 1 and first["remaining"] == MATCHES - 1
    assert second["excluded_count"] == 1 and second["batch_count"] == 1
    assert second["remaining"] == MATCHES - 3
    grant = case.cleanup.tokens.inspect(second["token"], "apply_rules")
    assert [row["uid"] for row in grant.messages] == ["3"]


def test_row_refused_alone_with_no_time_left_replies_with_a_cursor(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch, 14.0)
    other_sender(case, 2)

    first = first_token(case, limit=1)[-1]
    second = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
    third = case.rules.preview_rules(limit=1, cursor=second["next_cursor"])

    assert second["token"] is None and second["batch_count"] == 0
    assert second["excluded_count"] == 1 and second["remaining"] == MATCHES - 2
    assert second["next_cursor"]
    grant = case.cleanup.tokens.inspect(third["token"], "apply_rules")
    assert [row["uid"] for row in grant.messages] == ["3"]


@pytest.mark.parametrize("review", [4.0, 14.0], ids=["one-row-first", "whole-batch"])
def test_every_match_refused_ends_with_no_token_and_no_cursor(mailbox, monkeypatch, review):
    case = slow_case(mailbox, monkeypatch, review)
    for uid in range(1, MATCHES + 1):
        other_sender(case, uid)
    cursor, result = None, None
    for _ in range(8):
        result = case.rules.preview_rules(cursor=cursor)
        assert result["token"] is None
        cursor = result["next_cursor"]
        if not cursor:
            break
    assert cursor is None and result["excluded_count"] == MATCHES and result["remaining"] == 0
    assert "excluded" in result["reason"]


def test_all_excluded_reason_on_a_single_row_call_keeps_its_diagnostic(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch, 4.0)
    for uid in range(1, MATCHES + 1):
        other_sender(case, uid)
    cursor, result = None, None
    for _ in range(MATCHES + 4):
        result = case.rules.preview_rules(limit=1, cursor=cursor)
        cursor = result["next_cursor"]
        if not cursor:
            break
    assert result["reason"].startswith("All remaining matches were excluded")
    assert "stage_ms=(" in result["reason"]


def test_excluded_message_does_not_replace_a_refused_token_reason(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch)
    other_sender(case, 8)
    monkeypatch.setattr(case.cleanup, "_preview_has_headroom", lambda *args, **options: False)
    replies = []
    cursor = None
    for _ in range(30):
        replies.append(case.rules.preview_rules(cursor=cursor))
        cursor = replies[-1]["next_cursor"]
        if replies[-1]["count"]:
            break
    result = replies[-1]
    assert result["token"] is None and result["count"] == MATCHES - 1
    assert result["excluded_count"] == 1
    assert result["reason"] == "Insufficient execute headroom; preview rules again"
    assert result["next_cursor"]


def test_excluded_message_does_not_explain_a_fully_protected_batch(mailbox):
    case = mailbox(rules=[rule("subject", "Hello")], count=2)
    protected = case.fake.folders["INBOX"]["1"]
    protected.headers = protected.headers.replace(b"sender@example.com", b"relative@gmail.com")
    peer = copy.deepcopy(case.fake.folders["INBOX"]["2"])
    peer.headers = peer.headers.replace(b"From: Sender", b"From: Someone Else")
    case.fake.folders["All Mail"]["2"] = peer

    result = case.rules.preview_rules()

    assert result["excluded_reasons"] == {"inconsistent_sender_headers": 1}
    assert result["batch_count"] == 1 and result["count"] == 0
    assert result["token"] is None and result["next_cursor"] is None
    assert len(result["rules"][0]["blocked"]) == 1
    assert "excluded" not in result.get("reason", "")


def test_message_refused_by_a_review_that_then_overran_is_not_remaining(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch, 4.0)
    case.row = 0.5  # The larger review cannot finish All Mail.
    case.fake.folders["Folders/Other"] = {"500": copy.deepcopy(case.fake.folders["INBOX"]["8"])}
    peer = case.fake.folders["Folders/Other"]["500"]
    peer.headers = peer.headers.replace(
        b"From: Sender <notifications@github.com>",
        b"From: Someone Else <notifications@github.com>",
    )

    result = first_token(case)[-1]

    assert case.sizes == [1, MATCHES]
    assert result["batch_count"] == 1 and result["excluded_count"] == 1
    assert result["remaining"] == MATCHES - 2


def test_failed_call_keeps_its_cursor_and_its_place(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch)
    first = first_token(case, limit=20)[-1]
    assert first["batch_count"] == 20
    select, fired = case.fake.select, []

    def drops_once(folder, *, readonly):
        if not fired and "All Mail" in str(folder):
            fired.append(True)
            raise OSError("synthetic connection reset")
        return select(folder, readonly=readonly)

    case.fake.select = drops_once
    with pytest.raises(MailError, match="Mailbox operation failed"):
        case.rules.preview_rules(limit=20, cursor=first["next_cursor"])

    retried = case.rules.preview_rules(limit=20, cursor=first["next_cursor"])
    assert retried["batch_count"] == 20 and retried["remaining"] == MATCHES - 40
    with pytest.raises(MailValidationError, match="Invalid, expired or evicted"):
        case.rules.preview_rules(limit=20, cursor=first["next_cursor"])


def test_changed_options_do_not_consume_the_cursor(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch, 4.0)
    first = first_token(case, limit=20)[-1]
    with pytest.raises(MailValidationError, match="options changed"):
        case.rules.preview_rules(limit=7, cursor=first["next_cursor"])
    assert case.rules.preview_rules(limit=20, cursor=first["next_cursor"])["batch_count"] == 20


@pytest.mark.parametrize("bad", [1, 8])
def test_batch_with_an_unverifiable_row_serves_the_rows_before_it(mailbox, monkeypatch, bad):
    case = slow_case(mailbox, monkeypatch)
    case.fake.folders["All Mail"][str(MATCHES + bad)] = copy.deepcopy(
        case.fake.folders["INBOX"][str(bad)]
    )
    peer = case.fake.folders["All Mail"][str(MATCHES + bad)]
    peer.headers = peer.headers[:-2] + b"Message-ID: <hidden-second@example.test>\r\n\r\n"
    cursor, approved, calls, narrowed = None, 0, 0, []
    with pytest.raises(MailValidationError, match="unresolved"):
        for calls in range(1, 25):
            result = case.rules.preview_rules(cursor=cursor)
            approved += result["batch_count"] if result["token"] else 0
            if "cannot be verified" in result["reason"]:
                narrowed.append(result["reason"])
            cursor = result["next_cursor"]
    assert approved == bad - 1 and calls <= 14
    assert len(narrowed) == (8 if bad == 8 else 0)
    assert not any("deadline" in reason for reason in narrowed)
    with pytest.raises(MailValidationError, match="unresolved"):
        case.rules.preview_rules(cursor=cursor)


def test_unverifiable_row_narrows_within_its_rule(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch)
    second_rule(case, EVEN)
    peer = copy.deepcopy(case.fake.folders["INBOX"]["8"])
    peer.headers = peer.headers[:-2] + b"Message-ID: <hidden-second@example.test>\r\n\r\n"
    case.fake.folders["All Mail"][str(MATCHES + 8)] = peer
    cursor, approved = None, []

    with pytest.raises(MailValidationError, match="unresolved"):
        for _ in range(25):
            result = case.rules.preview_rules(cursor=cursor)
            if result["token"]:
                approved.extend(approved_uids(case, result))
            cursor = result["next_cursor"]

    assert approved == [*ODD, "2", "4", "6"]


def test_overruns_that_end_early_still_reach_the_stall_guard(mailbox, monkeypatch):
    case = slow_case(mailbox, monkeypatch)
    first_token(case)
    sizes = []

    def socket_timeout_after_five_seconds(connection, deadline, *args, **options):
        sizes.append(len(options["source_rows"]))
        ended = mail.time.monotonic() + 5.0
        monkeypatch.setattr(mail.time, "monotonic", lambda: ended)
        deadline.end = min(deadline.end, ended)
        raise CallDeadlineExceeded("Mailbox call timed out")

    monkeypatch.setattr(case.rules, "_plan", socket_timeout_after_five_seconds)
    cursor = None
    for _ in range(30):
        result = case.rules.preview_rules(cursor=cursor)
        assert result["token"] is None
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert cursor is None and "cannot make progress" in result["reason"]
    assert sizes[:6] == [MATCHES, 30, 15, 7, 3, 1]
    assert set(sizes[6:]) == {1}


@pytest.mark.parametrize(
    "value",
    [
        "<a@b.test>",
        "a@b.test",
        "<A@B.TEST>",
        "<a@b.test>, <c@d.test>",
        "<a@b.test>;c@d.test",
        "(<a@b.test>)",
        "<a@b.test> trailing",
        "not-an-id",
        "<bad\x7fid@b.test>",
        "<straße@b.test>",
        "<STRASSE@B.TEST>",
        "A@B.TEST",
        "other-a@b.test <a@b.test>",
        "<a@b.test><c@d.test>",
        "<a@b.test",
        "a@b.test>",
        '"<a@b.test>"',
        "<a@b.test\x00>",
        "a@b@c",
        "<a@b@c>",
        "",
    ],
)
def test_token_first_scan_finds_exactly_what_full_validation_finds(value):
    requested = ["<a@b.test>", "<c@d.test>", "<strasse@b.test>", "<a@b@c>", "<other@b.test>"]
    folded = {identifier.casefold() for identifier in requested}
    assert _names_requested_identity([value], folded) == bool(
        _raw_identity_candidates([value]) & folded
    )
