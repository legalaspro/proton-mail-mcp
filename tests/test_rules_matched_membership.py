"""All Mail raw identity audits resume in bounded UID batches."""

from copy import deepcopy
from dataclasses import replace

import pytest
from gluon_search import GluonSearchImap
from test_folder_rules import mailbox as mailbox
from test_folder_rules import rule
from test_mail import message
from test_preview_rules_bounded import assert_read_only

from proton_mail_mcp import cleanup_lookup, mail
from proton_mail_mcp.mail import MailValidationError


def matched_case(mailbox, *, count=46):
    case = mailbox(count=count, rules=[rule(contains="notifications@github.com")])
    for item in case.fake.folders["INBOX"].values():
        item.headers = item.headers.replace(b"sender@example.com", b"notifications@github.com")
    # A sparse unrelated allocation must be audited without a full UID SEARCH.
    case.fake.folders["All Mail"]["1000"] = message("<unrelated@example.test>")
    fake = GluonSearchImap(case.fake.folders, uidnext=True)
    fake.capabilities = case.fake.capabilities
    case.fake = fake
    case.cleanup.client.imap_factory = lambda *args, **kwargs: fake
    return case


def forbid_full_all_mail_search(case, monkeypatch):
    uid = case.fake.uid
    searches = []

    def record(command, *args):
        if command == "SEARCH" and case.fake.current == "All Mail":
            criteria = tuple(args[2:])
            atoms = {str(atom).casefold() for atom in criteria}
            assert "all" not in atoms, "All Mail membership must not enumerate the folder"
            assert "header" not in atoms, "Raw membership audits must not use HEADER SEARCH"
            searches.append(criteria)
        return uid(command, *args)

    monkeypatch.setattr(case.fake, "uid", record)
    return searches


@pytest.mark.parametrize("limit", [1, 200])
def test_46_matches_finish_with_monotone_deadline_limited_progress(mailbox, monkeypatch, limit):
    case = matched_case(mailbox)
    searches = forbid_full_all_mail_search(case, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    metadata = case.cleanup.client._metadata
    completed = set()
    attempts = []

    def slow_membership(connection, uids, **options):
        membership = case.fake.current == "All Mail" and options.get("identity_headers_only")
        if membership:
            attempts.append(tuple(uids))
            # Small batches fit, but a speculative full-match FETCH cannot fit
            # a single call. Model the watchdog stopping at the I/O deadline.
            deadline = options["deadline"]
            live_rows = sum(uid in case.fake.folders["All Mail"] for uid in uids)
            clock[0] += min(0.75 * live_rows, deadline.io_end - clock[0])
        rows = metadata(connection, uids, **options)
        if membership:
            completed.update(uids)
        return rows

    monkeypatch.setattr(case.cleanup.client, "_metadata", slow_membership)
    cursor, remaining = None, 46
    approved = set()
    batches = []
    for _ in range(46 + 8):
        completed.clear()
        started = clock[0]
        result = case.rules.preview_rules(limit=limit, cursor=cursor)
        assert clock[0] - started <= 20
        if not result["token"]:
            assert result["partial"] and result["batch_count"] == 0
            assert result["remaining"] == remaining and result["next_cursor"]
            cursor = result["next_cursor"]
            continue
        assert result["batch_count"] > 0, result.get("reason")
        assert result["count"] == result["batch_count"]
        assert result["total_matches"] == 46
        assert result["remaining"] == remaining - result["batch_count"]
        grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
        batch = {row["uid"] for row in grant.messages}
        # A saved raw audit, or a timed-out FETCH, cannot replace
        # fresh raw evidence for any message approved in this call.
        assert batch <= completed
        assert len(batch) == result["batch_count"]
        assert not approved.intersection(batch)
        approved.update(batch)
        batches.append(result["batch_count"])
        remaining, cursor = result["remaining"], result["next_cursor"]
        assert (cursor is not None) == bool(remaining)
        if remaining == 0:
            break

    assert len(batches) > 1
    assert remaining == 0 and approved == set(case.fake.folders["INBOX"])
    assert not searches and attempts
    assert_read_only(case.fake)


@pytest.mark.parametrize("completed_overrun", [False, True])
def test_46_match_chain_completes_after_one_all_mail_command_overruns_window(
    mailbox, monkeypatch, completed_overrun
):
    case = matched_case(mailbox)
    for uid in range(47, 448):
        case.fake.folders["All Mail"][str(uid)] = message(f"<unrelated-{uid}@example.test>")
    searches = forbid_full_all_mail_search(case, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    uid, select, metadata = case.fake.uid, case.fake.select, case.cleanup.client._metadata
    deadline = [None]
    raw_batches, overrun = [], []
    fresh = set()

    def charge_command(command, *args):
        result = uid(command, *args)
        clock[0] += 0.05
        if command == "FETCH" and case.fake.current == "All Mail":
            requested = tuple(args[0].split(","))
            if len(requested) > 1:
                raw_batches.append(requested)
                if len(raw_batches) == 2:
                    # One actual command exceeds the entire window. Its rows
                    # cannot authorize, while the preceding batch is retained.
                    overrun.append(requested)
                    if not completed_overrun:
                        clock[0] += 21
                        deadline[0].check(case.fake)
        return result

    def charge_select(*args, **kwargs):
        clock[0] += 0.05
        return select(*args, **kwargs)

    def record_metadata(connection, uids, **options):
        deadline[0] = options["deadline"]
        rows = metadata(connection, uids, **options)
        if case.fake.current == "All Mail" and options.get("identity_headers_only"):
            fresh.update(rows)
            if completed_overrun and overrun and tuple(uids) == overrun[0]:
                # Model a complete response whose last processing step crossed
                # the deadline, after the transport returned all its rows.
                clock[0] += 21
        return rows

    monkeypatch.setattr(case.fake, "uid", charge_command)
    monkeypatch.setattr(case.fake, "select", charge_select)
    monkeypatch.setattr(case.cleanup.client, "_metadata", record_metadata)
    cursor, approved, progress = None, set(), []
    for invocation in range(12):
        fresh.clear()
        started = clock[0]
        result = case.rules.preview_rules(cursor=cursor)
        assert result["total_matches"] == 46
        if invocation == 0:
            assert overrun and clock[0] - started > 20
            assert result["partial"] and result["token"] is None
            saved = case.cleanup._rules_preview_states[result["next_cursor"]]
            inventory = saved["inventories"]["All Mail"]
            assert inventory["next_uid"] == (401 if completed_overrun else 201)
            saved_rows = inventory["rows"]
            assert {row["uid"] for row in saved_rows} >= set(case.fake.folders["INBOX"])
            if completed_overrun:
                assert {row["uid"] for row in saved_rows} >= set(overrun[0])
        if result["token"]:
            grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
            batch = {row["uid"] for row in grant.messages}
            assert batch <= fresh and not approved.intersection(batch)
            approved.update(batch)
        else:
            assert result["partial"] and result["next_cursor"]
        progress.append(len(approved))
        assert result["remaining"] == 46 - len(approved)
        cursor = result["next_cursor"]
        if cursor is None:
            break

    assert approved == set(case.fake.folders["INBOX"])
    assert len(overrun) == 1 and len(progress) > 1 and progress == sorted(progress)
    assert raw_batches.count(raw_batches[0]) == 1
    if completed_overrun:
        assert raw_batches.count(overrun[0]) == 1
    assert not searches and not case.fake.rejected
    assert_read_only(case.fake)


def test_all_mail_membership_audits_uid_ranges_without_search_all(mailbox, monkeypatch):
    case = matched_case(mailbox, count=2)
    searches = forbid_full_all_mail_search(case, monkeypatch)
    metadata = case.cleanup.client._metadata
    fetched = set()

    def record(connection, uids, **options):
        if case.fake.current == "All Mail":
            fetched.update(uids)
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.cleanup.client, "_metadata", record)
    result = case.rules.preview_rules()

    assert result["token"] and result["batch_count"] == 2
    assert not searches and {"1", "2", "1000"} <= fetched
    assert_read_only(case.fake)


def interrupt_matched_fetch(case, monkeypatch):
    searches = forbid_full_all_mail_search(case, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    metadata = case.cleanup.client._metadata
    completed = []
    costs = {"search": 0, "fetch": 21}
    interrupted = False

    def slow_fetch(connection, uids, **options):
        nonlocal interrupted
        membership = case.fake.current == "All Mail" and options.get("identity_headers_only")
        if membership and not interrupted:
            interrupted = True
            clock[0] += costs["fetch"]
            options["deadline"].check(connection)
        rows = metadata(connection, uids, **options)
        if membership:
            completed.extend(uid for uid in uids if uid in rows)
        return rows

    monkeypatch.setattr(case.cleanup.client, "_metadata", slow_fetch)
    first = case.rules.preview_rules(limit=1)
    assert first["partial"] and first["next_cursor"]
    assert first["token"] is first["action_hash"] is None
    assert first["batch_count"] == 0 and first["remaining"] == 1
    assert not searches and not completed
    return first, searches, completed, costs


def test_membership_raw_audit_resumes_after_timed_out_fetch(mailbox, monkeypatch):
    case = matched_case(mailbox, count=1)
    first, searches, completed, _ = interrupt_matched_fetch(case, monkeypatch)

    result = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])

    assert result["token"] and result["batch_count"] == 1
    assert result["remaining"] == 0 and result["next_cursor"] is None
    assert not searches
    assert {"1", "1000"} <= set(completed)
    assert_read_only(case.fake)


@pytest.mark.parametrize("change", ["append", "expunge", "replace"])
def test_changed_all_mail_allocation_rechecks_raw_audit(mailbox, monkeypatch, change):
    case = matched_case(mailbox, count=1)
    first, searches, _, costs = interrupt_matched_fetch(case, monkeypatch)
    costs.update(search=0, fetch=0)
    if change in {"append", "replace"}:
        case.fake.folders["All Mail"]["1001"] = message("<new-unrelated@example.test>")
    if change in {"expunge", "replace"}:
        del case.fake.folders["All Mail"]["1000"]
    result = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])

    assert result["token"] and result["batch_count"] == 1 and result["remaining"] == 0
    assert not searches
    assert_read_only(case.fake)


def test_changed_all_mail_uidvalidity_refuses_saved_raw_audit(mailbox, monkeypatch):
    case = matched_case(mailbox, count=1)
    first, searches, _, costs = interrupt_matched_fetch(case, monkeypatch)
    costs.update(search=0, fetch=0)
    select = case.fake.select

    def reset_validity(folder, *, readonly):
        result = select(folder, readonly=readonly)
        case.fake.uidvalidity = "2" if case.fake.current == "All Mail" else "1"
        return result

    monkeypatch.setattr(case.fake, "select", reset_validity)
    with pytest.raises(MailValidationError, match="mailbox changed"):
        case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
    assert not searches
    assert_read_only(case.fake)


def test_forged_cursor_cannot_reuse_saved_raw_audit(mailbox, monkeypatch):
    case = matched_case(mailbox, count=1)
    first, _, _, _ = interrupt_matched_fetch(case, monkeypatch)
    before = len(case.fake.calls)
    with pytest.raises(MailValidationError, match="Invalid, expired or evicted"):
        case.rules.preview_rules(limit=1, cursor=first["next_cursor"] + "tampered")
    assert len(case.fake.calls) == before


@pytest.mark.parametrize("corruption", ["missing_uid", "rows_type", "extra_key"])
def test_tampered_raw_audit_cannot_hide_conflicting_identity(mailbox, monkeypatch, corruption):
    case = matched_case(mailbox, count=1)
    peer = deepcopy(case.fake.folders["All Mail"]["1000"])
    peer.headers = peer.headers[:-2] + b"Message-ID: <rule-1@example.test>\r\n\r\n"
    case.fake.folders["All Mail"]["1000"] = peer
    first, searches, _, costs = interrupt_matched_fetch(case, monkeypatch)
    costs.update(search=0, fetch=0)
    saved = case.cleanup._rules_preview_states[first["next_cursor"]]["inventories"]["All Mail"]
    case.cleanup.client._rules_raw_identity_snapshots.clear()
    if corruption == "missing_uid":
        saved["next_uid"] = 1001
    elif corruption == "rows_type":
        saved["rows"] = {}
    else:
        saved["unchecked"] = True

    def unexpected_issue(*args, **kwargs):
        pytest.fail("Altered membership progress must not hide a conflicting raw identity")

    monkeypatch.setattr(case.cleanup.tokens, "issue", unexpected_issue)
    with pytest.raises(MailValidationError, match="unresolved|ambiguous|Message-ID"):
        case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
    assert not searches
    assert_read_only(case.fake)


def test_matched_all_mail_peer_with_hidden_duplicate_id_is_never_approved(mailbox, monkeypatch):
    case = matched_case(mailbox, count=1)
    peer = deepcopy(case.fake.folders["All Mail"]["1000"])
    peer.headers = peer.headers[:-2] + b"Message-ID: <rule-1@example.test>\r\n\r\n"
    case.fake.folders["All Mail"]["1000"] = peer
    forbid_full_all_mail_search(case, monkeypatch)

    def unexpected_issue(*args, **kwargs):
        pytest.fail("Ambiguous matched raw identity must not be approved")

    monkeypatch.setattr(case.cleanup.tokens, "issue", unexpected_issue)
    with pytest.raises(MailValidationError, match="unresolved|ambiguous|Message-ID"):
        case.rules.preview_rules(limit=1)
    assert_read_only(case.fake)


def test_matched_all_mail_peer_with_protected_sender_is_blocked(mailbox, monkeypatch):
    case = matched_case(mailbox, count=1)
    protected = deepcopy(case.fake.folders["All Mail"]["1"])
    protected.headers = protected.headers.replace(
        b"notifications@github.com", b"relative@gmail.com"
    )
    case.fake.folders["All Mail"]["1"] = protected
    forbid_full_all_mail_search(case, monkeypatch)

    result = case.rules.preview_rules(limit=1)

    assert result["batch_count"] == 1 and result["remaining"] == 0
    assert result["count"] == 0 and result["token"] is None
    assert result["rules"][0]["blocked"]
    assert_read_only(case.fake)


def test_bare_message_id_alias_is_verified_from_raw_fields(mailbox, monkeypatch):
    case = matched_case(mailbox, count=1)
    source = case.fake.folders["INBOX"]["1"]
    source.headers = source.headers.replace(b"<rule-1@example.test>", b"<a@b>")
    peer = deepcopy(source)
    peer.headers = peer.headers.replace(b"<a@b>", b"a@b")
    case.fake.folders["All Mail"]["1"] = peer
    searches = forbid_full_all_mail_search(case, monkeypatch)

    result = case.rules.preview_rules(limit=1)

    assert result["token"] and result["count"] == 1
    grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
    assert grant.messages[0]["message_id"] == "<a@b>"
    assert not searches
    assert_read_only(case.fake)


def test_utf8_opaque_message_id_is_verified_from_raw_fields(mailbox, monkeypatch):
    case = matched_case(mailbox, count=1)
    identifier = "opaque-é-123"
    source = case.fake.folders["INBOX"]["1"]
    source.headers = source.headers.replace(b"<rule-1@example.test>", identifier.encode("utf-8"))
    searches = forbid_full_all_mail_search(case, monkeypatch)

    result = case.rules.preview_rules(limit=1)

    assert result["token"] and result["count"] == 1
    grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
    assert grant.messages[0]["message_id"] == identifier
    assert not searches
    assert not case.fake.search_literals
    assert_read_only(case.fake)


def test_header_substring_false_positive_with_malformed_identity_is_ignored(mailbox, monkeypatch):
    case = matched_case(mailbox, count=1)
    peer = case.fake.folders["All Mail"]["1000"]
    peer.headers = peer.headers.replace(
        b"Message-ID: <unrelated@example.test>\r\n",
        b"Message-ID: <prefix-rule-1@example.test-suffix>\r\n"
        b"Message-ID: <unrelated@example.test>\r\n",
    )
    forbid_full_all_mail_search(case, monkeypatch)
    metadata = case.cleanup.client._metadata
    fetched = set()

    def record(connection, uids, **options):
        if case.fake.current == "All Mail":
            fetched.update(uids)
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.cleanup.client, "_metadata", record)
    result = case.rules.preview_rules(limit=1)

    assert {"1", "1000"} <= fetched
    assert result["token"] and result["count"] == 1
    grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
    assert {row["uid"] for row in grant.messages} == {"1"}
    assert_read_only(case.fake)


def test_missing_uidnext_uses_status_before_resuming_raw_audit(mailbox, monkeypatch):
    case = matched_case(mailbox, count=1)
    case.fake.reports_uidnext = False
    first, searches, _, costs = interrupt_matched_fetch(case, monkeypatch)
    costs.update(search=0, fetch=0)

    result = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])

    assert result["token"] and result["count"] == 1 and result["remaining"] == 0
    assert not searches
    assert_read_only(case.fake)


@pytest.mark.parametrize("with_cursor", [True, False])
@pytest.mark.parametrize("scope", ["account", "endpoint"])
def test_raw_audit_checkpoint_is_scoped_to_account_and_endpoint(
    mailbox, monkeypatch, with_cursor, scope
):
    case = matched_case(mailbox, count=1)
    first, searches, _, costs = interrupt_matched_fetch(case, monkeypatch)
    costs.update(search=0, fetch=0)
    changes = (
        {"bridge_user": "other@example.test"} if scope == "account" else {"imap_host": "127.0.0.2"}
    )
    case.cleanup.client.settings = replace(case.cleanup.client.settings, **changes)
    if scope == "endpoint":

        def changed_endpoint(host, port, *, timeout):
            assert (host, port) == ("127.0.0.2", 1143)
            assert timeout > 0
            return case.fake

        monkeypatch.setattr(case.cleanup.client, "imap_factory", changed_endpoint)

    result = case.rules.preview_rules(limit=1, cursor=first["next_cursor"] if with_cursor else None)

    assert result["token"] and result["count"] == 1 and result["remaining"] == 0
    assert not searches
    assert_read_only(case.fake)


def test_short_message_id_uses_bounded_raw_ranges_without_header_search(mailbox, monkeypatch):
    case = matched_case(mailbox, count=1)
    source = case.fake.folders["INBOX"]["1"]
    source.headers = source.headers.replace(b"<rule-1@example.test>", b"<1>")
    searches = forbid_full_all_mail_search(case, monkeypatch)

    result = case.rules.preview_rules()

    assert result["token"] and result["count"] == 1
    grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
    assert grant.messages[0]["message_id"] == "<1>"
    assert not searches
    assert_read_only(case.fake)


def test_raw_range_checkpoint_size_limit_refuses_without_approval(mailbox, monkeypatch):
    case = matched_case(mailbox, count=2)
    searches = forbid_full_all_mail_search(case, monkeypatch)
    monkeypatch.setattr(cleanup_lookup, "_PREVIEW_RANGE_MAX_BYTES", 512)

    with pytest.raises(MailValidationError, match="size limit"):
        case.rules.preview_rules()

    assert not case.cleanup.tokens._grants and not searches
    assert not any(
        cached["inventory"]["snapshot"]["folder"] == "All Mail"
        for cached in case.cleanup.client._rules_raw_identity_snapshots.values()
    )
    assert_read_only(case.fake)
