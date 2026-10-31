"""Completed physical folder checks survive bounded rules preview calls."""

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


def slow_copy_checks(case, monkeypatch, *, seconds=12):
    """One physical folder fits; checking every folder never fits one call."""
    case.fake.reports_uidnext = True
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    original = case.cleanup._folder_rows
    work = SimpleNamespace(seconds=seconds, attempted=[], completed=[])

    def checked(connection, deadline, folder, identifiers, *, stage, **kwargs):
        if stage == "physical copy check":
            work.attempted.append((folder, tuple(identifiers)))
            clock[0] += work.seconds
            deadline.check(connection)
        rows = original(connection, deadline, folder, identifiers, stage=stage, **kwargs)
        if stage == "physical copy check":
            work.completed.append((folder, tuple(identifiers)))
        return rows

    monkeypatch.setattr(case.cleanup, "_folder_rows", checked)
    return work


@pytest.mark.parametrize("limit", [1, 200])
def test_copycheck_cursor_emits_only_verified_batches_and_finishes(mailbox, monkeypatch, limit):
    case = mailbox(count=38, rules=[rule(contains="notifications@github.com")])
    for item in case.fake.folders["INBOX"].values():
        item.headers = item.headers.replace(b"sender@example.com", b"notifications@github.com")
    work = slow_copy_checks(case, monkeypatch)
    physical = set(case.fake.folders) - {"All Mail"}
    remaining, cursor, since_batch = 38, None, 0
    approved = set()
    results = []

    for call in range(38 * (len(physical) + 1)):
        # LIST order is allowed to vary while naming the same physical folders.
        names = list(case.fake.folders)
        offset = call % len(names)
        case.fake.folders = {
            name: case.fake.folders[name] for name in names[offset:] + names[:offset]
        }
        result = case.rules.preview_rules(limit=limit, cursor=cursor)
        results.append(result)
        since_batch += 1
        assert result["remaining"] <= remaining
        if result["token"]:
            grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
            batch = {row["message_id"] for row in grant.messages}
            assert not approved.intersection(batch)
            for identifier in batch:
                verified = {
                    folder for folder, identifiers in work.completed if identifier in identifiers
                }
                assert verified == physical
            assert result["count"] == result["batch_count"] == len(batch)
            assert result["remaining"] == remaining - len(batch)
            approved.update(batch)
            since_batch = 0
        else:
            assert result["partial"]
            assert result["count"] == result["batch_count"] == 0
            assert result["action_hash"] is None
            assert result["remaining"] == remaining
        # Each completed folder buys progress toward a batch, even though the
        # next folder runs out of time. Repeating the first folder fails here.
        assert since_batch <= len(physical)
        remaining, cursor = result["remaining"], result["next_cursor"]
        if not cursor:
            break

    assert results[0]["partial"] and results[0]["remaining"] == 38
    assert len(work.attempted) > len(work.completed)
    assert cursor is None and remaining == 0
    assert len(approved) == 38
    assert not result["partial"]
    assert_read_only(case.fake)


def test_copycheck_resume_does_not_restart_slow_inventories_during_membership(mailbox, monkeypatch):
    case = mailbox(count=3)
    case.fake.reports_uidnext = True
    case.fake.folders["Folders/Other"]["10"] = message("<unrelated@example.test>")
    clock, scans, fresh_reads = [100.0], [], []
    current = [0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    enumerate_uids = cleanup_lookup.enumerate_uids
    metadata = case.cleanup.client._metadata

    def slow_inventory(client, connection, deadline, folder, validity, stage):
        if folder in {"INBOX", "Folders/Other"}:
            scans.append((current[0], folder, stage))
            clock[0] += 12
            deadline.check(connection)
        return enumerate_uids(client, connection, deadline, folder, validity, stage)

    def fresh_candidate(connection, uids, **options):
        if options.get("identity_headers_only"):
            fresh_reads.append((current[0], case.fake.current, tuple(uids), options.get("flags")))
        return metadata(connection, uids, **options)

    monkeypatch.setattr(cleanup_lookup, "enumerate_uids", slow_inventory)
    monkeypatch.setattr(case.cleanup.client, "_metadata", fresh_candidate)
    cursor, remaining, since_batch = None, 3, 0
    approved = set()
    for call in range(12):
        current[0] = call
        result = case.rules.preview_rules(limit=1, mark_read=True, cursor=cursor)
        since_batch += 1
        assert result["remaining"] <= remaining
        if call == 0:
            assert result["partial"] and result["batch_count"] == 0
            for item in case.fake.folders["INBOX"].values():
                item.flags = {r"\Seen"}
        if result["token"]:
            grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
            (row,) = grant.messages
            assert row["seen"] is True
            assert row["uid"] not in approved
            approved.add(row["uid"])
            # The source refresh and the reused physical candidate both read
            # current safety fields. Cursor evidence cannot supply old flags.
            reads = [
                flags
                for invocation, folder, uids, flags in fresh_reads
                if invocation == call and folder == "INBOX" and row["uid"] in uids
            ]
            assert len(reads) >= 2 and all(reads)
            assert result["remaining"] == remaining - 1
            since_batch = 0
        else:
            assert result["count"] == result["batch_count"] == 0
        # A Seen change can invalidate the first source fingerprint once. A
        # completed physical scan must then avoid repeating that mailbox-wide
        # scan in membership and permit a batch within three further calls.
        assert since_batch <= 3
        remaining, cursor = result["remaining"], result["next_cursor"]
        if cursor is None:
            break
    assert cursor is None and remaining == 0 and approved == {"1", "2", "3"}
    assert scans and not any(stage == "membership" for _, _, stage in scans)
    assert not any(call[0] in {"MOVE", "COPY", "STORE", "EXPUNGE"} for call in case.fake.calls)


def test_copycheck_resume_refreshes_cached_physical_copy_sender(mailbox, monkeypatch):
    case = mailbox(count=1, rules=[rule(field="subject", contains="Hello")])
    physical_copy = deepcopy(case.fake.folders["INBOX"]["1"])
    case.fake.folders = {
        "Folders/Other": {"10": physical_copy},
        **{name: items for name, items in case.fake.folders.items() if name != "Folders/Other"},
    }
    work = slow_copy_checks(case, monkeypatch)
    first = case.rules.preview_rules(limit=1)
    assert first["partial"] and first["token"] is None
    assert [folder for folder, _ in work.completed] == ["Folders/Other"]
    # The saved UID is a discovery shortcut; its sender still requires a fresh
    # read before approval, even when the allocation snapshot is unchanged.
    physical_copy.headers = physical_copy.headers.replace(
        b"sender@example.com", b"relative@gmail.com"
    )
    work.seconds = 0
    result = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
    assert result["batch_count"] == 1 and result["remaining"] == 0
    assert result["count"] == 0 and result["token"] is None
    assert result["rules"][0]["blocked"]
    assert_read_only(case.fake)


def interrupted_before_source(mailbox, monkeypatch):
    case = mailbox(count=2)
    # Put a non-source folder first so its reusable evidence can become stale
    # independently of source discovery's own UIDVALIDITY checks.
    case.fake.folders["Folders/Other"]["10"] = message("<unrelated@example.test>")
    case.fake.folders = {
        "Folders/Other": case.fake.folders["Folders/Other"],
        **{name: items for name, items in case.fake.folders.items() if name != "Folders/Other"},
    }
    case.fake.folders["Labels/Review"] = {"1": case.fake.folders["INBOX"]["1"]}
    work = slow_copy_checks(case, monkeypatch)
    first = case.rules.preview_rules(limit=1)
    assert first["partial"] and first["token"] is None and first["next_cursor"]
    assert [folder for folder, _ in work.completed] == ["Folders/Other"]
    return case, work, first


@pytest.mark.parametrize("change", ["append", "expunge", "replace", "uidvalidity"])
def test_stale_copycheck_progress_rechecks_changed_folder(mailbox, monkeypatch, change):
    case, work, first = interrupted_before_source(mailbox, monkeypatch)
    if change in {"append", "replace"}:
        # A new identical physical copy makes the label's ownership ambiguous.
        # Stale counts would incorrectly approve the source message.
        case.fake.folders["Folders/Other"]["11"] = deepcopy(case.fake.folders["INBOX"]["1"])
    if change in {"expunge", "replace"}:
        del case.fake.folders["Folders/Other"]["10"]
    if change == "uidvalidity":
        select = case.fake.select

        def changed_validity(folder, *, readonly):
            result = select(folder, readonly=readonly)
            case.fake.uidvalidity = "2" if case.fake.current == "Folders/Other" else "1"
            return result

        monkeypatch.setattr(case.fake, "select", changed_validity)
    before = len(work.attempted)
    work.seconds = 0
    if change == "expunge":
        result = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
        assert result["token"] and result["batch_count"] == 1
    elif change == "uidvalidity":
        with pytest.raises(MailValidationError, match="mailbox changed"):
            case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
    else:
        result = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
        assert result["excluded_reasons"] == {"ambiguous_label_membership": 1}
        grant = case.cleanup.tokens.inspect(result["token"], "apply_rules")
        assert "1" not in {row["uid"] for row in grant.messages}
    assert "Folders/Other" in {folder for folder, _ in work.attempted[before:]}
    assert_read_only(case.fake)


def test_copycheck_membership_refuses_changes_after_cached_folder_validation(mailbox, monkeypatch):
    case, work, first = interrupted_before_source(mailbox, monkeypatch)
    work.seconds = 0
    folder_rows = case.cleanup._folder_rows
    changed = []

    def append_after_validation(connection, deadline, folder, identifiers, *, stage, **kwargs):
        if stage == "physical copy check" and folder == "INBOX" and not changed:
            # Folders/Other's saved counts were just reused after SELECT. An
            # append before its membership visit must invalidate those counts.
            case.fake.folders["Folders/Other"]["11"] = deepcopy(case.fake.folders["INBOX"]["1"])
            changed.append(True)
        return folder_rows(connection, deadline, folder, identifiers, stage=stage, **kwargs)

    def unexpected_issue(*args, **kwargs):
        pytest.fail("A physical copy added after validation must not authorize any message")

    monkeypatch.setattr(case.cleanup, "_folder_rows", append_after_validation)
    monkeypatch.setattr(case.cleanup.tokens, "issue", unexpected_issue)
    with pytest.raises(MailValidationError, match="Message set changed"):
        case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
    assert changed
    assert_read_only(case.fake)


def test_tampered_copycheck_cursor_cannot_supply_progress(mailbox, monkeypatch):
    case, work, first = interrupted_before_source(mailbox, monkeypatch)
    before = len(case.fake.calls)
    with pytest.raises(MailValidationError, match="Invalid, expired or evicted"):
        case.rules.preview_rules(limit=1, cursor=first["next_cursor"] + "tampered")
    assert len(case.fake.calls) == before
    # Refusing the forged handle must not consume the original continuation.
    work.seconds = 0
    result = case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
    assert result["token"] and result["batch_count"] == 1
    assert_read_only(case.fake)


def test_copycheck_without_uidnext_reverifies_instead_of_trusting_progress(mailbox, monkeypatch):
    case = mailbox(count=2)
    work = slow_copy_checks(case, monkeypatch)
    case.fake.reports_uidnext = False
    cursor = None
    for _ in range(2):
        result = case.rules.preview_rules(limit=1, cursor=cursor)
        assert result["partial"] and result["token"] is None
        assert result["count"] == result["batch_count"] == 0
        assert result["remaining"] == 2 and result["next_cursor"]
        cursor = result["next_cursor"]
    assert [folder for folder, _ in work.completed] == ["INBOX", "INBOX"]
    assert_read_only(case.fake)


def test_copycheck_progress_cannot_outlive_its_rules(mailbox, monkeypatch):
    case, _, first = interrupted_before_source(mailbox, monkeypatch)
    case.path.write_text(json.dumps({"version": 1, "rules": [rule(folder="Folders/Other")]}))
    before = len(case.fake.calls)
    with pytest.raises(MailValidationError, match="options changed"):
        case.rules.preview_rules(limit=1, cursor=first["next_cursor"])
    assert len(case.fake.calls) == before
    assert_read_only(case.fake)
