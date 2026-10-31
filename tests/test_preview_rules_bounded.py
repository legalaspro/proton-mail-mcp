"""Bounded, resumable rules previews use only synthetic in-memory mailboxes."""

from dataclasses import replace
from email import policy
from email.parser import BytesParser

import pytest
from test_cleanup_mutations import MUTATIONS
from test_folder_rules import mailbox as mailbox
from test_mail import message

from proton_mail_mcp import mail, rules_preview
from proton_mail_mcp.folder_rules import CALIBRATION_ROWS, FolderRulesService
from proton_mail_mcp.mail import MailValidationError


def assert_read_only(fake):
    assert not any(call[0] in MUTATIONS or call[0] == "SELECT" for call in fake.calls)
    assert all(call[2] is True for call in fake.calls if call[0] == "EXAMINE")
    for call in fake.calls:
        if call[0] == "FETCH":
            assert "BODY[" not in call[2]
            assert "BODYSTRUCTURE" not in call[2]
    assert all(
        r"\Seen" not in getattr(item, "flags", ())
        for folder in fake.folders.values()
        for item in folder.values()
    )


def discovery_fetch(options):
    return (
        options.get("envelope", False)
        and not options.get("headers", True)
        and options.get("internaldate", True)
    )


def interrupt_source(case, monkeypatch):
    """Finish one metadata batch, then exhaust the per-preview time budget."""
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    metadata = case.cleanup.client._metadata
    completed, attempted = [], []
    interrupted = False

    def fetch(connection, uids, **options):
        nonlocal interrupted
        if discovery_fetch(options):
            attempted.append(tuple(uids))
            if len(attempted) == 2 and not interrupted:
                interrupted = True
                clock[0] += 21
                options["deadline"].check(connection)
                raise AssertionError("Rules preview deadline must be below 21 seconds")
            result = metadata(connection, uids, **options)
            completed.extend(uids)
            return result
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.cleanup.client, "_metadata", fetch)
    first = case.rules.preview_rules(limit=1)
    assert first["partial"] and not first["total_is_exact"]
    assert first["token"] is None and first["action_hash"] is None
    assert first["next_cursor"] and "deadline" in first["reason"].lower()
    assert completed and len(attempted) == 2
    assert_read_only(case.fake)
    return first, clock, completed, attempted


def interrupt_membership(case, monkeypatch):
    """Keep one completed All Mail raw identity batch across a deadline."""
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    metadata = case.cleanup.client._metadata
    completed, attempted = [], []
    interrupted = False

    def fetch(connection, uids, **options):
        nonlocal interrupted
        saved = getattr(connection, "_rules_preview_state", {}).get("inventories", {})
        known = {row["uid"] for row in saved.get("All Mail", {}).get("rows", [])}
        audit = (
            case.fake.current == "All Mail"
            and options.get("identity_headers_only", False)
            and (len(uids) > 1 or any(uid not in known for uid in uids))
        )
        if audit:
            attempted.append(tuple(uids))
            if len(attempted) == 2 and not interrupted:
                interrupted = True
                clock[0] += 21
                options["deadline"].check(connection)
                raise AssertionError("Rules preview deadline must be below 21 seconds")
        result = metadata(connection, uids, **options)
        if audit:
            completed.extend(uids)
        return result

    monkeypatch.setattr(case.cleanup.client, "_metadata", fetch)
    first = case.rules.preview_rules(limit=1)
    assert first["partial"] and first["token"] is None
    assert first["next_cursor"] and "deadline" in first["reason"].lower()
    assert completed and len(attempted) == 2
    assert_read_only(case.fake)
    return first, clock, completed, attempted


@pytest.mark.parametrize("order,expected", [("oldest", "1"), ("newest", "10000")])
def test_limit_one_large_mailbox_selects_before_rule_safety(mailbox, monkeypatch, order, expected):
    case = mailbox(count=10_000)
    monkeypatch.setattr(mail.time, "monotonic", lambda: 100.0)
    plan = case.rules._plan
    selected = []
    source_batches = []
    metadata = case.cleanup.client._metadata

    def record_plan(*args, **options):
        assert options.get("source_rows") is not None
        selected.extend(options["source_rows"])
        assert len(selected) <= 1
        return plan(*args, **options)

    def record_metadata(connection, uids, **options):
        if discovery_fetch(options):
            source_batches.append(tuple(uids))
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.rules, "_plan", record_plan)
    monkeypatch.setattr(case.cleanup.client, "_metadata", record_metadata)
    result = case.rules.preview_rules(limit=1, order=order)

    assert result["token"] and result["count"] == result["batch_count"] == 1
    assert result["total_matches"] == 10_000 and result["remaining"] == 9_999
    assert result["total_is_exact"] and not result["partial"]
    assert [row["uid"] for row in selected] == [expected]
    assert len(source_batches) > 1 and max(map(len, source_batches)) <= 1000
    assert_read_only(case.fake)


def test_deadline_returns_partial_cursor_and_resumes_completed_source_batches(mailbox, monkeypatch):
    case = mailbox(count=2001)
    first, clock, completed, attempted = interrupt_source(case, monkeypatch)
    already_scanned = set(completed)
    initial_attempts = len(attempted)
    clock[0] += 100

    # MailClient recreates this service for every tool invocation. Continuation
    # state must therefore live with the client/cleanup service, not this object.
    service = FolderRulesService(case.cleanup, case.path)
    resumed = service.preview_rules(limit=1, cursor=first["next_cursor"])

    assert resumed["token"] and resumed["count"] == resumed["batch_count"] == 1
    assert resumed["total_matches"] == 2001 and resumed["remaining"] == 2000
    assert resumed["total_is_exact"] and not resumed["partial"]
    assert not already_scanned.intersection(
        uid for batch in attempted[initial_attempts:] for uid in batch
    )
    assert set(completed) == set(case.fake.folders["INBOX"])
    assert_read_only(case.fake)


def test_deadline_in_raw_source_resumes_completed_fetch_batches(mailbox, monkeypatch):
    case = mailbox(count=401)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    metadata = case.cleanup.client._metadata
    plan = case.rules._plan
    reviewing = False
    interrupted = False
    completed, attempted = [], []

    def fetch(connection, uids, **options):
        nonlocal interrupted
        if not reviewing and options.get("identity_headers_only"):
            attempted.append(tuple(uids))
            if len(attempted) == 2 and not interrupted:
                interrupted = True
                clock[0] += 21
                options["deadline"].check(connection)
            rows = metadata(connection, uids, **options)
            completed.extend(uids)
            return rows
        return metadata(connection, uids, **options)

    def review(*args, **options):
        nonlocal reviewing
        reviewing = True
        return plan(*args, **options)

    monkeypatch.setattr(case.cleanup.client, "_metadata", fetch)
    monkeypatch.setattr(case.rules, "_plan", review)
    first = case.rules.preview_rules(limit=1)
    assert first["partial"] and first["next_cursor"] and first["token"] is None
    previous = set(completed)
    initial_attempts = len(attempted)
    assert previous
    clock[0] += 100

    resumed = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])

    assert resumed["token"] and resumed["count"] == 1
    assert resumed["total_matches"] == 401 and not resumed["partial"]
    assert not previous.intersection(uid for batch in attempted[initial_attempts:] for uid in batch)
    assert_read_only(case.fake)


def test_completed_preview_cursor_reviews_next_batch_without_widening_approval(mailbox):
    case = mailbox(count=3)
    cursor, approved = None, []
    for _ in range(3):
        result = case.rules.preview_rules(limit=1, cursor=cursor)
        assert result["token"] and result["count"] == 1
        grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
        assert len(grant.messages) == len(grant.selector["reviewed_rows"]) == 1
        approved.extend(row["uid"] for row in grant.messages)
        cursor = result["next_cursor"]

    assert approved == ["1", "2", "3"]
    assert cursor is None
    assert len(case.fake.folders["INBOX"]) == 3
    assert_read_only(case.fake)


def test_many_populated_rules_share_membership_review_within_deadline(mailbox, monkeypatch):
    rules = [
        {"field": "subject", "contains": f"Bucket-{index:02d}", "folder": "Folders/Review"}
        for index in range(1, 21)
    ]
    case = mailbox(count=20, rules=rules)
    for uid, item in case.fake.folders["INBOX"].items():
        item.headers = item.headers.replace(
            b"Subject: Hello", f"Subject: Bucket-{int(uid):02d}".encode()
        )
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    metadata = case.cleanup.client._metadata
    fetches = []

    def slow_fetch(connection, uids, **options):
        fetches.append(tuple(uids))
        rows = metadata(connection, uids, **options)
        clock[0] += 0.05
        return rows

    monkeypatch.setattr(case.cleanup.client, "_metadata", slow_fetch)
    result = case.rules.preview_rules(limit=20)

    assert result["token"] and result["count"] == result["batch_count"] == 20
    assert not result["partial"] and result["total_is_exact"]
    assert [report["count"] for report in result["rules"]] == [1] * 20
    assert len(fetches) < 15  # Calibration plus one shared review, rather than 20 reviews.
    assert clock[0] - 100 < 18
    assert_read_only(case.fake)


def test_slow_review_closes_first_batch_and_advances_cursor_without_partial_token(
    mailbox, monkeypatch
):
    case = mailbox(count=38)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    metadata = case.cleanup.client._metadata
    discovery = []
    interrupted = False

    def slow_fetch(connection, uids, **options):
        nonlocal interrupted
        if discovery_fetch(options):
            discovery.extend(uids)
        if interrupted:
            interrupted = False
            clock[0] += 21
            options["deadline"].check(connection)
        clock[0] += 0.1 * len(uids)
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.cleanup.client, "_metadata", slow_fetch)
    result = case.rules.preview_rules()
    assert result["partial"] and result["token"] is None and result["batch_count"] == 0
    assert clock[0] - 100 < 18
    approved = set()
    cursor = result["next_cursor"]
    assert cursor

    # An interrupted continuation must retain its position without authorizing
    # its unfinished batch or revisiting completed source discovery.
    interrupted = True
    result = case.rules.preview_rules(cursor=cursor)
    assert result["partial"] and result["token"] is result["action_hash"] is None
    assert result["next_cursor"] and result["next_cursor"] != cursor
    cursor = result["next_cursor"]
    for _ in range(38):
        started = clock[0]
        result = case.rules.preview_rules(cursor=cursor)
        assert not result["partial"] and result["batch_count"] >= 1
        assert clock[0] - started < 18
        grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
        selected = {row["uid"] for row in grant.messages}
        assert not approved.intersection(selected)
        approved.update(selected)
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert cursor is None and approved == set(case.fake.folders["INBOX"])
    assert discovery == list(case.fake.folders["INBOX"])
    assert_read_only(case.fake)


@pytest.mark.parametrize("limit", [1, 200])
@pytest.mark.parametrize("order", ["oldest", "newest"])
def test_slow_first_item_uses_response_reserve_and_cursor_eventually_finishes(
    mailbox, monkeypatch, limit, order
):
    case = mailbox(count=5)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    # Finish discovery and the reusable raw identity audits before modeling a
    # slow fresh membership FETCH. Those audits never authorize a message.
    warm = case.rules.preview_rules()
    assert warm["token"] and warm["count"] == 5
    metadata, plan = case.cleanup.client._metadata, case.rules._plan
    reviewing = False
    fetched = []

    def review(*args, **options):
        nonlocal reviewing
        reviewing = True
        try:
            return plan(*args, **options)
        finally:
            reviewing = False

    def slow_fetch(connection, uids, **options):
        if reviewing and case.fake.current == "All Mail" and options.get("identity_headers_only"):
            # One fully reviewed item fits the hard 20-second cap but crosses
            # the usual 18-second I/O cutoff. A full calibration batch cannot
            # finish, and a completed singleton must survive the final check.
            fetched.append(tuple(uids))
            clock[0] += 18.5 * len(uids)
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.rules, "_plan", review)
    monkeypatch.setattr(case.cleanup.client, "_metadata", slow_fetch)
    cursor, approved, remaining = None, [], 5
    for _ in range(5):
        started = clock[0]
        result = case.rules.preview_rules(limit=limit, order=order, cursor=cursor)
        assert result["token"] and not result["partial"], result.get("reason")
        assert result["count"] == result["batch_count"] == 1
        assert result["total_is_exact"] and result["total_matches"] == 5
        assert result["remaining"] == remaining - result["batch_count"]
        assert 18 < clock[0] - started <= 20
        grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
        assert len(grant.messages) == len(grant.selector["reviewed_rows"]) == 1
        selected = [row["uid"] for row in grant.messages]
        assert not set(approved).intersection(selected)
        approved.extend(selected)
        remaining = result["remaining"]
        previous, cursor = cursor, result["next_cursor"]
        assert (cursor is not None) == bool(remaining)
        if cursor:
            assert cursor != previous

    assert cursor is None and remaining == 0
    expected = [str(uid) for uid in range(1, 6)]
    assert approved == (expected if order == "oldest" else expected[::-1])
    assert fetched == [(uid,) for uid in approved]
    assert_read_only(case.fake)


def test_first_singleton_survives_slow_calibration_batch(mailbox, monkeypatch):
    case = mailbox(count=5)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    assert case.rules.preview_rules()["count"] == 5
    metadata, plan = case.cleanup.client._metadata, case.rules._plan
    reviewing = False
    attempted = []

    def review(*args, **options):
        nonlocal reviewing
        reviewing = True
        try:
            return plan(*args, **options)
        finally:
            reviewing = False

    def slow_fetch(connection, uids, **options):
        if reviewing and case.fake.current == "All Mail" and options.get("identity_headers_only"):
            attempted.append(tuple(uids))
            # Model the command watchdog interrupting a larger FETCH at the
            # normal I/O cutoff, after the six-second singleton was completed.
            deadline = options["deadline"]
            clock[0] += min(6 * len(uids), deadline.io_end - clock[0])
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.rules, "_plan", review)
    monkeypatch.setattr(case.cleanup.client, "_metadata", slow_fetch)
    result = case.rules.preview_rules()

    assert len(attempted[0]) == 1 and len(attempted[1]) == CALIBRATION_ROWS
    assert result["token"] and not result["partial"], result.get("reason")
    assert result["count"] == result["batch_count"] == 1
    assert result["remaining"] == 4 and result["next_cursor"]
    assert clock[0] - 100 == 18
    grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
    assert [row["uid"] for row in grant.messages] == ["1"]
    assert [row["uid"] for row in grant.selector["reviewed_rows"]] == ["1"]
    assert_read_only(case.fake)


def test_review_budget_includes_large_raw_audits_and_refreshes_on_continuation(
    mailbox, monkeypatch
):
    # A cheap, selective subject SEARCH yields only 38 matches from 380 source
    # rows. The two whole-folder raw audits cost nearly the complete budget;
    # timing the 38 ENVELOPE rows alone would admit an unfinished full batch.
    case = mailbox(
        count=380,
        rules=[{"field": "subject", "contains": "Hello", "folder": "Folders/Review"}],
    )
    for uid, item in case.fake.folders["INBOX"].items():
        if int(uid) > 38:
            item.headers = item.headers.replace(b"Subject: Hello", b"Subject: Other")
    clock, raw_seconds = [100.0], [0.022]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    metadata = case.cleanup.client._metadata
    discovered, raw = [], []

    def fetch(connection, uids, **options):
        if discovery_fetch(options):
            discovered.extend(uids)
        if options.get("identity_headers_only"):
            raw.extend(uids)
            clock[0] += raw_seconds[0] * len(uids)
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.cleanup.client, "_metadata", fetch)
    first = case.rules.preview_rules()
    # The calibration batch completes both audits. Their time is not charged
    # per row, so the batch expands into the little time left.
    assert first["token"] and CALIBRATION_ROWS < first["batch_count"] < 38
    assert not first["partial"] and first["total_matches"] == 38
    assert first["remaining"] == 38 - first["batch_count"]
    assert first["next_cursor"] and clock[0] - 100 < 18
    assert len(discovered) == 38
    assert "time budget" in first["reason"] and "hard cap" not in first["reason"]

    # Subsequent calls retain the audits and size batches from the complete
    # review times of earlier calls, rather than discovery's cheap FETCH.
    # These slower raw reads still finish within the deadline.
    raw_seconds[0] = 0.12
    audited, started = len(raw), clock[0]
    second = case.rules.preview_rules(cursor=first["next_cursor"])
    assert second["token"] and not second["partial"] and second["remaining"] == 0
    assert second["batch_count"] == first["remaining"] and clock[0] - started < 18
    assert len(raw) - audited < 380 and len(discovered) == 38
    assert_read_only(case.fake)


def test_review_budget_deducts_slow_discovery_search_before_raw_header_review(mailbox, monkeypatch):
    case = mailbox(
        count=38,
        rules=[{"field": "subject", "contains": "Hello", "folder": "Folders/Review"}],
    )
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    search, metadata = case.cleanup.client._search, case.cleanup.client._metadata

    def slow_search(connection, criteria, literal=None):
        if "SUBJECT" in criteria:
            clock[0] += 12
        return search(connection, criteria, literal=literal)

    def raw_fetch(connection, uids, **options):
        if options.get("identity_headers_only"):
            clock[0] += 0.04 * len(uids)
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.cleanup.client, "_search", slow_search)
    monkeypatch.setattr(case.cleanup.client, "_metadata", raw_fetch)
    result = case.rules.preview_rules()
    assert result["token"] and CALIBRATION_ROWS < result["batch_count"] < 38
    assert not result["partial"] and result["next_cursor"]
    assert result["remaining"] == 38 - result["batch_count"]
    assert 12 < clock[0] - 100 < 18
    assert_read_only(case.fake)


def test_deadline_in_raw_memberships_resumes_completed_audit_batches(mailbox, monkeypatch):
    case = mailbox(count=401)
    first, clock, completed, attempted = interrupt_membership(case, monkeypatch)
    previous = set(completed)
    initial_attempts = len(attempted)
    clock[0] += 100

    result = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])

    assert result["token"] and result["count"] == result["batch_count"] == 1
    assert result["total_matches"] == 401 and not result["partial"]
    assert not previous.intersection(uid for batch in attempted[initial_attempts:] for uid in batch)
    assert_read_only(case.fake)


def test_header_index_narrows_source_without_inline_catchup(mailbox, monkeypatch):
    case = mailbox(count=1001)
    for uid, item in case.fake.folders["INBOX"].items():
        if uid != "1":
            item.headers = item.headers.replace(b"sender@example.com", b"other@example.invalid")
    case.cleanup.client.search_index.observe(
        "INBOX",
        "1",
        {
            uid: {"headers": BytesParser(policy=policy.default).parsebytes(item.headers)}
            for uid, item in case.fake.folders["INBOX"].items()
        },
    )
    metadata = case.cleanup.client._metadata
    scanned = set()

    def fetch(connection, uids, **options):
        if discovery_fetch(options):
            scanned.update(uids)
        return metadata(connection, uids, **options)

    def no_catchup(*args, **kwargs):
        raise AssertionError("A rules preview must not catch up the search index inline")

    monkeypatch.setattr(case.cleanup.client, "_metadata", fetch)
    monkeypatch.setattr(case.cleanup.client, "_index_header_batches", no_catchup)
    result = case.rules.preview_rules(limit=1)

    assert result["token"] and result["count"] == result["total_matches"] == 1
    assert result["total_is_exact"] and not result["partial"]
    assert scanned <= {"1"}
    assert_read_only(case.fake)


@pytest.mark.parametrize("envelope_from", [None, [[None, None, b"other", b"example.invalid"]]])
def test_source_envelope_miss_cannot_hide_matching_raw_sender(mailbox, monkeypatch, envelope_from):
    case = mailbox(count=2)
    metadata = case.cleanup.client._metadata

    def contradictory_envelope(connection, uids, **options):
        rows = metadata(connection, uids, **options)
        if connection.current == "INBOX" and "2" in rows and options.get("envelope"):
            rows["2"]["envelope"][2] = envelope_from
        return rows

    def unexpected_issue(*args, **kwargs):
        pytest.fail("A matching raw sender with contradictory envelope cannot authorize a batch")

    monkeypatch.setattr(case.cleanup.client, "_metadata", contradictory_envelope)
    monkeypatch.setattr(case.cleanup.tokens, "issue", unexpected_issue)
    with pytest.raises(MailValidationError, match="cannot verify protected memberships"):
        case.rules.preview_rules()

    assert_read_only(case.fake)


def test_preview_cursor_refuses_uidvalidity_drift(mailbox, monkeypatch):
    case = mailbox(count=2001)
    first, clock, _, _ = interrupt_source(case, monkeypatch)
    case.fake.uidvalidity = "2"
    clock[0] += 100

    with pytest.raises(MailValidationError):
        case.rules.preview_rules(limit=1, cursor=first["next_cursor"])

    assert_read_only(case.fake)


def test_preview_cursor_reconciles_membership_snapshot_drift(mailbox, monkeypatch):
    case = mailbox(count=401)
    first, clock, completed, attempted = interrupt_membership(case, monkeypatch)
    previous = set(completed)
    initial_attempts = len(attempted)
    case.fake.folders["All Mail"]["3001"] = message("<new-copy@example.test>")
    del case.fake.folders["All Mail"]["2"]
    clock[0] += 100

    result = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])

    assert result["token"] and result["count"] == 1 and not result["partial"]
    assert result["total_matches"] == 401
    assert not previous.intersection(uid for batch in attempted[initial_attempts:] for uid in batch)
    assert "3001" in completed
    saved = case.cleanup._rules_preview_states[result["next_cursor"]]["inventories"]["All Mail"]
    assert {row["uid"] for row in saved["rows"]} == set(case.fake.folders["All Mail"])

    assert_read_only(case.fake)


@pytest.mark.parametrize("stage", ["source", "membership"])
@pytest.mark.parametrize("order,expected", [("oldest", "3"), ("newest", "402")])
def test_bridge_preview_completes_with_arrivals_and_removals_between_calls(
    mailbox, monkeypatch, stage, order, expected
):
    case = mailbox(count=401)
    assert b"CONDSTORE" not in case.fake.capabilities
    # Helpers interrupt an oldest scan; selecting the order up front keeps the
    # cursor's immutable options unchanged for the newest case too.
    preview = case.rules.preview_rules
    monkeypatch.setattr(
        case.rules, "preview_rules", lambda **options: preview(order=order, **options)
    )
    interrupt = interrupt_source if stage == "source" else interrupt_membership
    first, clock, completed, attempted = interrupt(case, monkeypatch)
    scanned = set(completed)
    attempts = len(attempted)
    arrival = message("<arrival-between-preview-calls@example.test>")
    for folder in ("INBOX", "All Mail"):
        del case.fake.folders[folder]["1"]
        del case.fake.folders[folder]["2"]
        case.fake.folders[folder]["402"] = arrival
    clock[0] += 100

    result = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])

    assert result["token"] and not result["partial"] and result["total_is_exact"]
    assert result["total_matches"] == 400 and result["remaining"] == 399
    grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
    assert [row["uid"] for row in grant.selector["reviewed_rows"]] == [expected]
    assert not scanned.intersection(uid for batch in attempted[attempts:] for uid in batch)
    assert "402" in completed
    assert_read_only(case.fake)


@pytest.mark.parametrize("order,first_uid", [("oldest", "1"), ("newest", "3")])
def test_completed_cursor_tracks_reviewed_uids_when_matches_reorder(mailbox, order, first_uid):
    case = mailbox(count=3)
    first = case.rules.preview_rules(limit=1, order=order)
    grant = case.cleanup.tokens.inspect(first["token"], "apply_rules")
    assert grant.messages[0]["uid"] == first_uid
    arrival = message("<arrival-before-next-batch@example.test>")
    for folder in ("INBOX", "All Mail"):
        del case.fake.folders[folder][first_uid]
        case.fake.folders[folder]["4"] = arrival
    cursor, reviewed = first["next_cursor"], []
    for _ in range(3):
        result = case.rules.preview_rules(limit=1, order=order, cursor=cursor)
        assert result["token"] and result["total_matches"] == 3
        grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
        reviewed.append(grant.messages[0]["uid"])
        cursor = result["next_cursor"]
    assert reviewed == (["2", "3", "4"] if order == "oldest" else ["4", "2", "1"])
    assert cursor is None

    assert_read_only(case.fake)


@pytest.mark.parametrize("change", [{"limit": 2}, {"order": "newest"}, {"mark_read": True}])
def test_preview_cursor_cannot_change_review_options(mailbox, monkeypatch, change):
    case = mailbox(count=2001)
    first, clock, _, _ = interrupt_source(case, monkeypatch)
    clock[0] += 100
    options = {"limit": 1, **change, "cursor": first["next_cursor"]}

    with pytest.raises(MailValidationError):
        case.rules.preview_rules(**options)

    assert_read_only(case.fake)


@pytest.mark.parametrize("configured,expected", [(None, 20), (120, 20), (3, 3)])
def test_preview_caps_deadline_and_honors_shorter_configuration(
    mailbox, monkeypatch, configured, expected
):
    case = mailbox()
    case.cleanup.settings = replace(case.cleanup.settings, preview_timeout=configured)
    monkeypatch.setattr(mail.time, "monotonic", lambda: 100.0)
    deadlines = []

    class Stopped(Exception):
        pass

    def session(*, deadline):
        deadlines.append(deadline.end - 100)
        raise Stopped

    monkeypatch.setattr(case.cleanup.client, "_session", session)
    with pytest.raises(Stopped):
        case.rules.preview_rules()

    assert deadlines == [expected]
    assert not case.fake.calls


@pytest.mark.parametrize("stage", ["envelope", "raw"])
@pytest.mark.parametrize("max_rows", [1, 64])
def test_slow_fetch_batches_shrink_until_preview_can_finish(mailbox, monkeypatch, stage, max_rows):
    case = mailbox(
        count=401, rules=[{"field": "subject", "contains": "Hello", "folder": "Folders/Review"}]
    )
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    metadata = case.cleanup.client._metadata
    attempted = []

    def slow(connection, uids, **options):
        selected = (
            discovery_fetch(options)
            if stage == "envelope"
            else options.get("identity_headers_only")
        )
        if selected:
            attempted.append(len(uids))
            if len(uids) > max_rows:
                clock[0] += 21
                options["deadline"].check(connection)
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.cleanup.client, "_metadata", slow)
    cursor = None
    for _ in range(8):
        result = case.rules.preview_rules(limit=1, cursor=cursor)
        if not result["partial"]:
            break
        assert result["next_cursor"] and not result["token"]
        cursor = result["next_cursor"]
    assert result["token"] and result["total_matches"] == 401
    assert max(attempted) > max_rows and any(size <= max_rows for size in attempted)
    assert_read_only(case.fake)


@pytest.mark.parametrize("stage", ["enumerate", "search", "verify", "single_fetch"])
def test_unfinishable_step_stops_returning_continuations(mailbox, monkeypatch, stage):
    case = mailbox(
        count=1, rules=[{"field": "subject", "contains": "Hello", "folder": "Folders/Review"}]
    )
    clock, attempts = [100.0], []
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])

    def slow(connection, deadline):
        attempts.append(True)
        clock[0] += 21
        deadline.check(connection)

    if stage == "enumerate":
        monkeypatch.setattr(
            rules_preview,
            "enumerate_uids",
            lambda client, connection, deadline, *args: slow(connection, deadline),
        )
    elif stage == "search":
        monkeypatch.setattr(
            case.cleanup.client,
            "_search",
            lambda connection, *args, **kwargs: slow(connection, connection.deadline),
        )
    elif stage == "verify":
        # The initial bracketed enumeration uses cleanup_lookup's verifier;
        # this is discovery's final, indivisible verification after all rows.
        monkeypatch.setattr(
            rules_preview,
            "_verify_inventory",
            lambda client, connection, *args: slow(connection, connection.deadline),
        )
    else:
        monkeypatch.setattr(
            case.cleanup.client,
            "_metadata",
            lambda connection, uids, **options: slow(connection, options["deadline"]),
        )
    cursor = None
    for _ in range(9):
        result = case.rules.preview_rules(limit=1, cursor=cursor)
        assert result["partial"] and result["token"] is None
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert 6 <= len(attempts) <= 7
    assert cursor is None and "cannot make progress" in result["reason"]
    assert "continue" not in result["reason"]
    assert not case.cleanup._rules_preview_states
    assert_read_only(case.fake)


def test_cursor_lifetime_renews_on_each_continuation(mailbox, monkeypatch):
    case = mailbox(count=401)
    first, clock, _, _ = interrupt_source(case, monkeypatch)
    for _ in range(2):
        clock[0] += 550

        # Interrupt after discovery has already completed, so no new progress
        # is invented by repeating this same slow review.
        def slow(*args, **kwargs):
            clock[0] += 21
            raise mail.CallDeadlineExceeded("Synthetic review timeout")

        monkeypatch.setattr(case.rules, "_plan", slow)
        first = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
        assert first["partial"] and first["next_cursor"]
    assert clock[0] > 1200
    assert_read_only(case.fake)


def test_repeated_headroom_failure_stops_returning_continuations(mailbox, monkeypatch):
    case = mailbox()
    monkeypatch.setattr(case.cleanup, "_preview_has_headroom", lambda *args, **kwargs: False)
    cursor = None
    for _ in range(8):
        result = case.rules.preview_rules(limit=1, cursor=cursor)
        assert result["count"] == 1 and result["token"] is None
        cursor = result["next_cursor"]
        if cursor is None:
            break
        assert "headroom" in result["reason"]
    assert cursor is None and "cannot make progress" in result["reason"]
    assert "continue" not in result["reason"]
    assert_read_only(case.fake)


def test_expired_partial_reply_does_not_request_continuation(mailbox, monkeypatch):
    case = mailbox()
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])

    def expired(*args, **kwargs):
        clock[0] += 601
        raise mail.CallDeadlineExceeded("Synthetic clock jump")

    monkeypatch.setattr(case.rules, "_plan", expired)
    result = case.rules.preview_rules()
    assert result["partial"] and result["next_cursor"] is None
    assert "continue" not in result["reason"]
    assert "start a fresh preview" in result["reason"]
    assert_read_only(case.fake)


@pytest.mark.parametrize("count", [1, 201])
def test_unindexed_from_discovery_reads_all_candidates_before_first_batch(
    mailbox, monkeypatch, count
):
    case = mailbox(count=count)
    metadata, plan = case.cleanup.client._metadata, case.rules._plan
    discovery_raw = []
    reviewing = False

    def fetch(connection, uids, **options):
        if not reviewing and options.get("identity_headers_only"):
            discovery_raw.extend(uids)
        return metadata(connection, uids, **options)

    def review(*args, **kwargs):
        nonlocal reviewing
        reviewing = True
        assert len(discovery_raw) == count
        return plan(*args, **kwargs)

    monkeypatch.setattr(case.cleanup.client, "_metadata", fetch)
    monkeypatch.setattr(case.rules, "_plan", review)
    result = case.rules.preview_rules(limit=1)
    assert result["token"] and result["total_matches"] == count
    assert set(discovery_raw) == set(case.fake.folders["INBOX"])
    assert_read_only(case.fake)
