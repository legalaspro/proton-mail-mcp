"""Completed review checkpoints survive mailbox changes and speculative overruns."""

import pytest
from test_folder_rules import mailbox as mailbox
from test_mail import message
from test_preview_rules_bounded import assert_read_only

from proton_mail_mcp import mail


def approved_uids(case, result):
    grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
    approved = [row["uid"] for row in grant.messages]
    assert approved == [row["uid"] for row in grant.selector["reviewed_rows"]]
    return approved


@pytest.mark.parametrize("inbox_arrival", [True, False])
def test_arrival_between_singleton_and_larger_review_keeps_token_and_cursor(
    mailbox, monkeypatch, inbox_arrival
):
    case = mailbox(count=6)
    monkeypatch.setattr(mail.time, "monotonic", lambda: 100.0)
    plan = case.rules._plan
    sizes = []
    arrival = message("<arrival-between-strict-reviews@example.test>")

    def arrive_after_singleton(*args, **options):
        sizes.append(len(options["source_rows"]))
        result = plan(*args, **options)
        if len(sizes) == 1:
            assert sizes == [1]
            for folder in ("INBOX", "All Mail") if inbox_arrival else ("All Mail",):
                case.fake.folders[folder]["901"] = arrival
        return result

    monkeypatch.setattr(case.rules, "_plan", arrive_after_singleton)
    first = case.rules.preview_rules(limit=6)

    assert len(sizes) == 2 and sizes[1] > 1
    assert first["partial"] and first["token"] and first["next_cursor"]
    assert first["batch_count"] == 1 and first["remaining"] == 5
    approved = approved_uids(case, first)
    assert approved == ["1"]
    assert "changed" in first["reason"].lower()
    cursor = first["next_cursor"]
    for _ in range(6):
        result = case.rules.preview_rules(limit=6, cursor=cursor)
        assert result["token"], result.get("reason")
        selected = approved_uids(case, result)
        assert not set(approved).intersection(selected)
        approved.extend(selected)
        cursor = result["next_cursor"]
        if cursor is None:
            break

    assert cursor is None
    assert approved == ["1", "2", "3", "4", "5", "6"] + (["901"] if inbox_arrival else [])
    assert_read_only(case.fake)


def test_repeated_larger_review_overruns_shrink_until_multirow_progress(mailbox, monkeypatch):
    case = mailbox(count=100)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    plan = case.rules._plan
    slower = False
    overruns = []
    reserves = []

    def timed_review(connection, deadline, *args, **options):
        size = len(options["source_rows"])
        reserves.append((size, deadline.response_reserve))
        if slower and size > 4:
            # A watchdog ends each speculative review at the ordinary I/O
            # cutoff. Its completed singleton must still receive approval.
            overruns.append(size)
            clock[0] = deadline.io_end
            deadline.check(connection)
            raise AssertionError("An overrun must stop at the I/O cutoff")
        result = plan(connection, deadline, *args, **options)
        clock[0] += size * (2 if slower else 0.01)
        deadline.check(connection)
        return result

    monkeypatch.setattr(case.rules, "_plan", timed_review)
    # Successful warm samples permit a fitted review much larger than four
    # rows. The next calls encounter the slower account with the same cursor.
    warm = case.rules.preview_rules(limit=32)
    assert warm["token"] and warm["next_cursor"]
    warm_state = case.cleanup._rules_preview_states[warm["next_cursor"]]
    assert 1 not in warm_state["review_costs"]
    approved = approved_uids(case, warm)
    cursor = warm["next_cursor"]
    slower = True
    results = []
    for _ in range(8):
        started = clock[0]
        result = case.rules.preview_rules(limit=32, cursor=cursor)
        assert result["token"] and result["next_cursor"], result.get("reason")
        assert clock[0] - started <= 18
        selected = approved_uids(case, result)
        assert not set(approved).intersection(selected)
        approved.extend(selected)
        results.append(result)
        cursor = result["next_cursor"]
        state = case.cleanup._rules_preview_states[cursor]
        assert 1 not in state["review_costs"]
        if result["batch_count"] == 1:
            assert state["review_lower_bounds"][overruns[-1]] == pytest.approx(16)
            assert state["review_cap"] < overruns[-1]
            assert state["review_overruns"] == len(overruns)
        if result["batch_count"] > 1:
            assert state["review_overruns"] == 0
            break

    assert len(overruns) >= 2
    assert all(after < before for before, after in zip(overruns, overruns[1:]))
    assert all(result["batch_count"] == 1 for result in results[:-1])
    assert results[-1]["batch_count"] > 1
    assert all(reserve == 0 for size, reserve in reserves if size == 1)
    assert all(reserve > 0 for size, reserve in reserves if size > 1)
    assert_read_only(case.fake)


@pytest.mark.parametrize("per_row", [0.005, 0.5])
def test_one_warm_sample_probes_full_batch_with_bounded_fallback(mailbox, monkeypatch, per_row):
    case = mailbox(count=200)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    plan, attempts = case.rules._plan, []

    def timed_review(connection, deadline, *args, **options):
        size = len(options["source_rows"])
        attempts.append(size)
        result = plan(connection, deadline, *args, **options)
        # Cold setup secures only one row. Thereafter, fixed folder checks
        # leave room for one warm sample per call, even when rows are cheap.
        seconds = 18.5 if len(attempts) == 1 else 7.4 if size == 1 else 5.8 + size * per_row
        clock[0] = min(clock[0] + seconds, deadline.io_end)
        deadline.check(connection)
        return result

    monkeypatch.setattr(case.rules, "_plan", timed_review)
    cursor, approved, batches = None, [], []
    for _ in range(3):
        started = clock[0]
        result = case.rules.preview_rules(cursor=cursor)
        assert result["token"] and not result["partial"], result.get("reason")
        assert clock[0] - started <= 20
        selected = approved_uids(case, result)
        assert not set(approved).intersection(selected)
        approved.extend(selected)
        batches.append(result["batch_count"])
        cursor = result["next_cursor"]

    assert attempts == [1, 1, 4, 1, 195]
    if per_row == 0.005:
        assert batches == [1, 4, 195] and cursor is None
        assert approved == [str(uid) for uid in range(1, 201)]
    else:
        assert batches == [1, 4, 1] and cursor
        state = case.cleanup._rules_preview_states[cursor]
        assert state["review_cap"] < 195 and state["review_lower_bounds"][195] > 0
        before = len(attempts)
        result = case.rules.preview_rules(cursor=cursor)
        assert result["token"] and result["batch_count"] >= 1
        assert 1 < attempts[before + 1] < 195
        assert not set(approved).intersection(approved_uids(case, result))
    assert_read_only(case.fake)


def test_limit_one_reports_stage_times_for_partial_and_completed_preview(mailbox, monkeypatch):
    case = mailbox(count=1)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    metadata = case.cleanup.client._metadata
    interrupt = True

    def timed_fetch(connection, uids, **options):
        clock[0] += 0.125
        if interrupt and case.fake.current == "All Mail" and options.get("identity_headers_only"):
            clock[0] += 21
            options["deadline"].check(connection)
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.cleanup.client, "_metadata", timed_fetch)
    partial = case.rules.preview_rules(limit=1)
    assert partial["partial"] and partial["token"] is None and partial["next_cursor"]
    assert "deadline reached during membership check" in partial["reason"].lower()
    interrupt = False
    completed = case.rules.preview_rules(limit=1, cursor=partial["next_cursor"])
    assert completed["token"] and completed["batch_count"] == 1 and not completed["partial"]
    assert set(completed) == set(partial)
    for result in (partial, completed):
        assert "stage=membership check" in result["reason"]
        assert "stage_ms=(connection setup:" in result["reason"]
        assert "source discovery:" in result["reason"]
        assert "membership check in" in result["reason"]
        assert "stage_ms" not in result
    assert_read_only(case.fake)


def test_each_strict_review_uses_one_fresh_folder_listing(mailbox, monkeypatch):
    case = mailbox(count=3)
    monkeypatch.setattr(mail.time, "monotonic", lambda: 100.0)
    plan, list_folders = case.rules._plan, case.fake.list
    listings, per_review = [], []

    def observe_listing():
        listings.append(set(case.fake.folders))
        return list_folders()

    def observe_review(*args, **options):
        before = len(listings)
        result = plan(*args, **options)
        per_review.append(listings[before:])
        if len(per_review) == 1:
            case.fake.folders["Folders/Fresh"] = {}
        return result

    monkeypatch.setattr(case.fake, "list", observe_listing)
    monkeypatch.setattr(case.rules, "_plan", observe_review)
    result = case.rules.preview_rules(limit=3)

    assert result["token"] and result["batch_count"] == 3
    assert len(per_review) >= 2 and all(len(listed) == 1 for listed in per_review)
    assert "Folders/Fresh" not in per_review[0][0]
    assert all("Folders/Fresh" in listed[0] for listed in per_review[1:])
    assert_read_only(case.fake)
