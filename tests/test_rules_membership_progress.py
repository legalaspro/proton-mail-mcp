"""Slow All Mail raw identity audits retain only completed verification work."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_folder_rules import mailbox as mailbox
from test_folder_rules import rule
from test_mail import message
from test_preview_rules_bounded import assert_read_only

from proton_mail_mcp import cleanup_lookup, mail
from proton_mail_mcp.mail import MailValidationError


def github_case(mailbox, *, count=38):
    case = mailbox(count=count, rules=[rule(contains="notifications@github.com")])
    for item in case.fake.folders["INBOX"].values():
        item.headers = item.headers.replace(b"sender@example.com", b"notifications@github.com")
    return case


def slow_all_mail(case, monkeypatch, *, batch_size=10, per_uid_seconds=0):
    """One slow range overruns; later raw batches retain verified progress."""
    case.fake.reports_uidnext = True
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cleanup_lookup, "_IDENTITY_BATCH_SIZE", batch_size)
    metadata = case.cleanup.client._metadata
    work = SimpleNamespace(
        search_seconds=12, batch_seconds=7, scans=[], attempted=[], completed=[], fresh=[]
    )
    setup_charged = False

    def fetch(connection, uids, **options):
        nonlocal setup_charged
        audit = False
        if case.fake.current == "All Mail" and options.get("identity_headers_only"):
            saved = connection._rules_preview_state.get("inventories", {}).get("All Mail", {})
            known = {row["uid"] for row in saved.get("rows", [])}
            actual = [uid for uid in uids if uid in case.fake.folders["All Mail"]]
            audit = any(uid not in known for uid in actual)
            if audit:
                work.scans.append(tuple(actual))
                work.attempted.append(tuple(actual))
                if not setup_charged:
                    setup_charged = True
                    clock[0] += work.search_seconds
                clock[0] += work.batch_seconds + len(actual) * per_uid_seconds
                options["deadline"].check(connection)
            elif actual:
                work.fresh.append(tuple(actual))
        rows = metadata(connection, uids, **options)
        if audit:
            work.completed.extend(rows)
        return rows

    monkeypatch.setattr(case.cleanup.client, "_metadata", fetch)
    return work


@pytest.mark.parametrize("limit", [1, 200])
@pytest.mark.parametrize("with_cursor", [True, False])
def test_slow_all_mail_calls_emit_verified_batches_and_complete(
    mailbox, monkeypatch, limit, with_cursor
):
    case = github_case(mailbox)
    work = slow_all_mail(case, monkeypatch)
    cursor, remaining, since_batch = None, 38, 0
    approved = set()
    saw_partial = False

    for _ in range(38 + 8):
        case.fake.calls.clear()
        result = case.rules.preview_rules(limit=limit, cursor=cursor if with_cursor else None)
        assert_read_only(case.fake)
        since_batch += 1
        saw_partial |= result["partial"]
        assert result["remaining"] <= remaining
        if result["token"]:
            grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
            batch = {row["uid"] for row in grant.messages}
            assert not approved.intersection(batch)
            # A hidden conflicting identity could occur in any unexamined raw
            # row. No candidate may be approved until the whole audit finishes.
            assert set(work.completed) == set(case.fake.folders["All Mail"])
            assert result["count"] == result["batch_count"] == len(batch)
            assert result["remaining"] == remaining - len(batch)
            approved.update(batch)
            since_batch = 0
            if not with_cursor:
                # Model the caller moving its approved batch before starting a
                # new preview. A cursorless preview reviews the live source.
                for uid in batch:
                    del case.fake.folders["INBOX"][uid]
        elif result["remaining"]:
            assert result["partial"]
            assert result["count"] == result["batch_count"] == 0
            assert result["action_hash"] is None
        assert since_batch <= 4
        remaining, cursor = result["remaining"], result["next_cursor"]
        if remaining == 0:
            break

    assert saw_partial
    assert remaining == 0 and len(approved) == 38
    assert work.scans
    assert len(work.attempted) > 4
    assert len(work.completed) == 38
    assert work.fresh


def interrupted_all_mail(mailbox, monkeypatch):
    case = github_case(mailbox)
    work = slow_all_mail(case, monkeypatch)
    first = case.rules.preview_rules(limit=1)
    assert first["partial"] and first["token"] is None and first["next_cursor"]
    assert work.scans and not work.completed
    return case, work, first


@pytest.mark.parametrize("with_cursor", [True, False])
@pytest.mark.parametrize("change", ["append", "expunge", "replace", "uidvalidity"])
def test_stale_all_mail_allocation_is_reenumerated(mailbox, monkeypatch, with_cursor, change):
    case, work, first = interrupted_all_mail(mailbox, monkeypatch)
    # The persisted enumeration is not valid evidence after an allocation
    # change, even if an expunge and append preserve the mailbox's count.
    if change in {"append", "replace"}:
        case.fake.folders["All Mail"]["99"] = message("<new-unrelated@example.test>")
    if change in {"expunge", "replace"}:
        del case.fake.folders["All Mail"]["38"]
    if change == "uidvalidity":
        select = case.fake.select

        def reset_validity(folder, *, readonly):
            result = select(folder, readonly=readonly)
            case.fake.uidvalidity = "2" if case.fake.current == "All Mail" else "1"
            return result

        monkeypatch.setattr(case.fake, "select", reset_validity)
    work.search_seconds = work.batch_seconds = 0
    before = len(work.scans)
    if with_cursor and change == "uidvalidity":
        with pytest.raises(MailValidationError, match="mailbox changed"):
            case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
    else:
        result = case.rules.preview_rules(
            limit=1, cursor=first["next_cursor"] if with_cursor else None
        )
        assert result["token"] and result["batch_count"] == 1
        assert len(work.scans) > before
        assert set(work.completed) == set(case.fake.folders["All Mail"])
    assert_read_only(case.fake)


def test_forged_all_mail_cursor_cannot_authorize_incomplete_audit(mailbox, monkeypatch):
    case, work, first = interrupted_all_mail(mailbox, monkeypatch)
    before = len(case.fake.calls)
    with pytest.raises(MailValidationError, match="Invalid, expired or evicted"):
        case.rules.preview_rules(limit=1, cursor=first["next_cursor"] + "tampered")
    assert len(case.fake.calls) == before
    work.search_seconds = work.batch_seconds = 0
    result = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
    assert result["token"] and result["batch_count"] == 1
    assert set(work.completed) == set(case.fake.folders["All Mail"])
    assert_read_only(case.fake)


def test_resumed_all_mail_audit_refreshes_protected_candidate_sender(mailbox, monkeypatch):
    case, work, first = interrupted_all_mail(mailbox, monkeypatch)
    work.search_seconds = 0
    second = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
    assert second["partial"] and second["token"] is None
    assert "1" in work.completed
    protected = deepcopy(case.fake.folders["All Mail"]["1"])
    protected.headers = protected.headers.replace(
        b"notifications@github.com", b"relative@gmail.com"
    )
    case.fake.folders["All Mail"]["1"] = protected
    work.batch_seconds = 0
    result = case.rules.preview_rules(limit=1, cursor=second["next_cursor"])
    assert result["batch_count"] == 1 and result["remaining"] == 37
    assert result["count"] == 0 and result["token"] is None
    assert result["rules"][0]["blocked"]
    assert "1" in {uid for batch in work.fresh for uid in batch}
    assert_read_only(case.fake)


def partial_raw_audit(case, monkeypatch):
    work = slow_all_mail(case, monkeypatch)
    first = case.rules.preview_rules(limit=1)
    assert first["partial"] and first["token"] is None
    second = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
    assert second["partial"] and second["token"] is None
    assert work.completed and len(work.completed) < len(case.fake.folders["All Mail"])
    return work, second


def cached_all_mail(case):
    return next(
        snapshot["inventory"]
        for snapshot in case.cleanup.client._rules_raw_identity_snapshots.values()
        if snapshot["inventory"]["snapshot"]["folder"] == "All Mail"
    )


@pytest.mark.parametrize("with_cursor", [True, False])
@pytest.mark.parametrize(
    "corruption",
    [
        "missing_uid",
        "changed_identity",
        "duplicate_row",
        "missing_rows",
        "rows_type",
        "snapshot_type",
        "extra_key",
        "uid_type",
        "batch_size",
    ],
)
def test_tampered_all_mail_progress_reaudits_before_refusing_hidden_identity(
    mailbox, monkeypatch, with_cursor, corruption
):
    case = github_case(mailbox)
    # This late raw row hides the first candidate's ID behind another field.
    # The source and its visible envelope remain unchanged.
    peer = deepcopy(case.fake.folders["All Mail"]["38"])
    peer.headers = peer.headers[:-2] + b"Message-ID: <rule-1@example.test>\r\n\r\n"
    case.fake.folders["All Mail"]["38"] = peer
    work, second = partial_raw_audit(case, monkeypatch)
    if with_cursor:
        saved = case.cleanup._rules_preview_states[second["next_cursor"]]["inventories"]["All Mail"]
        # Remove the independent, valid fallback so this case exercises the
        # cursor's altered checkpoint alone.
        case.cleanup.client._rules_raw_identity_snapshots.clear()
    else:
        saved = cached_all_mail(case)
    if corruption == "missing_uid":
        saved["next_uid"] = 39
    elif corruption == "changed_identity":
        saved["rows"][0]["_raw_identity_fields"] = ["<forged@example.test>"]
    elif corruption == "duplicate_row":
        saved["rows"].append(dict(saved["rows"][0]))
    elif corruption == "missing_rows":
        del saved["rows"]
    elif corruption == "rows_type":
        saved["rows"] = {"1": saved["rows"][0]}
    elif corruption == "snapshot_type":
        saved["snapshot"] = ["All Mail", 38, "39"]
    elif corruption == "extra_key":
        saved["unchecked"] = True
    elif corruption == "uid_type":
        saved["live_uids"][0] = 1
    else:
        saved["next_uid"] = 0
    verified_before = set(work.completed)
    reads_before, scans_before = len(work.completed), len(work.scans)
    work.search_seconds = work.batch_seconds = 0

    def unexpected_issue(*args, **kwargs):
        pytest.fail("Altered progress must not hide a conflicting raw identity")

    monkeypatch.setattr(case.cleanup.tokens, "issue", unexpected_issue)
    with pytest.raises(MailValidationError):
        case.rules.preview_rules(limit=1, cursor=second["next_cursor"] if with_cursor else None)
    assert len(work.scans) > scans_before
    assert verified_before <= set(work.completed[reads_before:])
    assert set(work.completed[reads_before:]) == set(case.fake.folders["All Mail"])
    assert_read_only(case.fake)


@pytest.mark.parametrize("with_cursor", [True, False])
def test_all_mail_without_uidnext_uses_status_before_resuming(mailbox, monkeypatch, with_cursor):
    case = github_case(mailbox)
    work = slow_all_mail(case, monkeypatch)
    case.fake.reports_uidnext = False
    cursor = None
    for _ in range(4):
        result = case.rules.preview_rules(limit=1, cursor=cursor if with_cursor else None)
        if result["token"]:
            break
        assert result["partial"] and result["batch_count"] == 0
        assert result["remaining"] == 38
        cursor = result["next_cursor"]
        work.search_seconds = 0
    assert result["token"] and result["batch_count"] == 1
    assert set(work.completed) == set(case.fake.folders["All Mail"])
    assert_read_only(case.fake)


@pytest.mark.parametrize("with_cursor", [True, False])
def test_all_mail_progress_is_tied_to_rules_digest(mailbox, monkeypatch, with_cursor):
    case = github_case(mailbox)
    work, second = partial_raw_audit(case, monkeypatch)
    case.path.write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [rule(contains="notifications@github.com", folder="Folders/Other")],
            }
        )
    )
    before_calls, before_scans, before_reads = (
        len(case.fake.calls),
        len(work.scans),
        len(work.completed),
    )
    work.search_seconds = work.batch_seconds = 0
    if with_cursor:
        with pytest.raises(MailValidationError, match="options changed"):
            case.rules.preview_rules(limit=1, cursor=second["next_cursor"])
        assert len(case.fake.calls) == before_calls
    else:
        result = case.rules.preview_rules(limit=1)
        assert result["token"] and result["batch_count"] == 1
        assert len(work.scans) > before_scans
        assert set(work.completed[before_reads:]) == set(case.fake.folders["All Mail"])
        assert result["rules"][0]["target"] == "Folders/Other"
    assert_read_only(case.fake)


@pytest.mark.parametrize("with_cursor", [True, False])
def test_source_oversize_raw_batch_shrinks_until_a_batch_fits(mailbox, monkeypatch, with_cursor):
    case = github_case(mailbox)
    case.fake.reports_uidnext = True
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cleanup_lookup, "_IDENTITY_BATCH_SIZE", 200)
    fetch_rows = cleanup_lookup.fetch_rows
    attempted, completed = [], []

    def fetch(client, connection, deadline, folder, uids, validity, **options):
        if folder == "INBOX" and options.get("raw_fields"):
            saved = connection._rules_preview_state.get("inventories", {}).get(folder, {})
            known = {row["uid"] for row in saved.get("rows", [])}
            if any(uid not in known for uid in uids):
                attempted.append(tuple(uids))
                clock[0] += 0.8 * len(uids)
                deadline.check(connection)
                rows = fetch_rows(client, connection, deadline, folder, uids, validity, **options)
                completed.extend(uids)
                return rows
        return fetch_rows(client, connection, deadline, folder, uids, validity, **options)

    monkeypatch.setattr(cleanup_lookup, "fetch_rows", fetch)
    cursor = None
    for _ in range(8):
        result = case.rules.preview_rules(limit=1, cursor=cursor if with_cursor else None)
        if result["token"]:
            break
        assert result["partial"] and result["batch_count"] == 0
        assert result["remaining"] == 38
        cursor = result["next_cursor"]
    assert result["token"] and result["batch_count"] == 1
    assert [len(batch) for batch in attempted[:2]] == [38, 38]
    assert any(len(batch) < 38 for batch in attempted[2:])
    assert len(completed) == len(set(completed)) == 38
    assert_read_only(case.fake)
