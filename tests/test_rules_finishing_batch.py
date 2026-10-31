"""The first token of a chain holds a real batch, not what an audit's call left over."""

import pytest
from test_folder_rules import mailbox as mailbox
from test_mail import message
from test_preview_rules_bounded import assert_read_only
from test_rules_matched_membership import forbid_full_all_mail_search, matched_case

from proton_mail_mcp import cleanup_lookup, mail

MATCHES, BATCH = 60, 20
RANGE = 0.5  # Live: about 0.48 s for each of the 238 ranges of 200 UIDs.
ROW = 0.00335  # tests/bridge_costs.py: one raw header row.
REVIEW = 4.0  # Cheap enough for two per call; test_rules_pinned_review.py charges more.
SETUP = 1.0  # Live: connection setup and source discovery of a later call.
WALK_PER_CALL = 26  # (18 - SETUP - REVIEW) / RANGE: ranges that fit one call.


def retest_case(mailbox, monkeypatch, *, ranges):
    monkeypatch.setattr(cleanup_lookup, "_IDENTITY_BATCH_SIZE", BATCH)
    case = matched_case(mailbox, count=MATCHES)
    del case.fake.folders["All Mail"]["1000"]
    for uid in range(MATCHES + 1, ranges * BATCH + 1):
        case.fake.folders["All Mail"][str(uid)] = message(f"<unrelated-{uid}@example.test>")
    forbid_full_all_mail_search(case, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    reader, plan, login = case.cleanup.client, case.rules._plan, case.fake.login
    metadata = reader._metadata
    case.sizes, case.row = [], ROW

    def charged_login(*args):
        clock[0] += SETUP
        return login(*args)

    def timed_review(connection, deadline, *args, **options):
        # The fixed folder checks run before the walk: All Mail is always last.
        case.sizes.append(len(options["source_rows"]))
        clock[0] = min(clock[0] + REVIEW, deadline.io_end)
        deadline.check(connection)
        return plan(connection, deadline, *args, **options)

    def timed_fetch(connection, uids, **options):
        if case.fake.current == "All Mail" and options.get("identity_headers_only"):
            saved = connection._rules_preview_state.get("inventories", {}).get("All Mail")
            walking = saved and saved["next_uid"] < int(saved["snapshot"]["uidnext"])
            cost = RANGE / BATCH if walking else case.row
            clock[0] += min(cost * len(uids), max(0.0, options["deadline"].io_end - clock[0]))
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.fake, "login", charged_login)
    monkeypatch.setattr(case.rules, "_plan", timed_review)
    monkeypatch.setattr(reader, "_metadata", timed_fetch)
    return case


def first_token(case, *, calls=30, **options):
    """Follow one cursor to its first token, keeping every reply."""
    cursor, replies = None, []
    for _ in range(calls):
        case.sizes.clear()
        replies.append(case.rules.preview_rules(cursor=cursor, **options))
        if replies[-1]["token"]:
            return replies
        assert replies[-1]["partial"] and replies[-1]["next_cursor"]
        cursor = replies[-1]["next_cursor"]
    pytest.fail(f"no token in {calls} calls")


def held_reply(case):
    """Follow one cursor until it holds a batch; any token before that fails."""
    cursor = None
    for _ in range(30):
        result = case.rules.preview_rules(cursor=cursor)
        assert result["token"] is None, f"token for {result['batch_count']} of {MATCHES}"
        cursor = result["next_cursor"]
        state = case.cleanup._rules_preview_states[cursor]
        if state.get("batch_held"):
            return result, state
    pytest.fail("no batch was held in 30 calls")


@pytest.mark.parametrize(
    "ranges",
    [
        WALK_PER_CALL + 4,  # The finishing call has 2 s of walk left.
        WALK_PER_CALL + 14,  # 7 s: half the walk window.
        WALK_PER_CALL + 24,  # 12 s: nearly all of it, as in the retest.
    ],
)
def test_first_token_holds_every_match(mailbox, monkeypatch, ranges):
    case = retest_case(mailbox, monkeypatch, ranges=ranges)

    replies = first_token(case)

    result = replies[-1]
    assert result["batch_count"] == result["count"] == MATCHES, (
        f"the first token holds {result['batch_count']} of {MATCHES} after {len(replies)} calls; "
        f"reason: {result['reason']}"
    )
    assert result["remaining"] == 0 and result["next_cursor"] is None
    assert result["total_is_exact"] and not result["partial"]
    assert case.sizes == [1, MATCHES]
    assert_read_only(case.fake)


def test_held_reply_is_an_ordinary_partial_reply(mailbox, monkeypatch):
    case = retest_case(mailbox, monkeypatch, ranges=WALK_PER_CALL + 24)

    held, state = held_reply(case)

    assert held["token"] is held["expires_at"] is held["action_hash"] is None
    assert held["partial"] and not held["total_is_exact"]
    assert held["total_matches"] == held["matched_count"] == held["remaining"] == MATCHES
    assert held["count"] == held["batch_count"] == 0 and held["rules"] == []
    assert "finished the rules audit" in held["reason"]
    assert "continue preview_rules with next_cursor" in held["reason"]
    assert "stage_ms=(" in held["reason"]
    assert state.get("batch_scale", 1) == 1 and state["stalled"] == 0
    assert not state["served"]


def test_batch_is_held_once_and_a_failed_probe_keeps_its_singleton(mailbox, monkeypatch):
    case = retest_case(mailbox, monkeypatch, ranges=WALK_PER_CALL + 24)
    held, _ = held_reply(case)
    case.row = 0.5  # A probe of every match cannot fit now.

    case.sizes.clear()
    probed = case.rules.preview_rules(cursor=held["next_cursor"])

    assert case.sizes == [1, MATCHES]
    assert probed["token"] and probed["batch_count"] == 1 and probed["remaining"] == MATCHES - 1
    state = case.cleanup._rules_preview_states[probed["next_cursor"]]
    assert state["review_cap"] == MATCHES // 2
    approved, cursor = 1, probed["next_cursor"]
    for _ in range(MATCHES):
        result = case.rules.preview_rules(cursor=cursor)
        assert result["token"], result.get("reason")
        approved += result["batch_count"]
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert approved == MATCHES and cursor is None


def test_limit_one_keeps_its_token_on_the_finishing_call(mailbox, monkeypatch):
    case = retest_case(mailbox, monkeypatch, ranges=WALK_PER_CALL + 24)

    replies = first_token(case, limit=1)

    assert len(replies) == 2
    assert replies[-1]["batch_count"] == 1 and replies[-1]["remaining"] == MATCHES - 1


def test_the_next_chain_reuses_the_finished_audit(mailbox, monkeypatch):
    case = retest_case(mailbox, monkeypatch, ranges=WALK_PER_CALL + 4)
    first_token(case)
    seen = []
    metadata = case.cleanup.client._metadata

    def counted(connection, uids, **options):
        if case.fake.current == "All Mail" and options.get("identity_headers_only"):
            seen.extend(uids)
        return metadata(connection, uids, **options)

    case.cleanup.client._metadata = counted
    assert case.rules.preview_rules()["token"]
    assert set(seen) <= {str(uid) for uid in range(1, MATCHES + 1)}, "only candidates are read"
