"""Finished raw audits accelerate apply without supplying approval or policy evidence."""

import json
from copy import deepcopy

import pytest
from gluon_search import GluonSearchImap
from test_cleanup_mutations import MUTATIONS, MutatingImap, uid_values
from test_folder_rules import mailbox as mailbox
from test_folder_rules import rule
from test_mail import message
from test_rules_finishing_batch import (
    BATCH,
    MATCHES,
    RANGE,
    REVIEW,
    ROW,
    SETUP,
    WALK_PER_CALL,
    first_token,
    retest_case,
)
from test_rules_matched_membership import forbid_full_all_mail_search

from proton_mail_mcp import mail
from proton_mail_mcp.mail import MailValidationError


class AuditedMutatingImap(MutatingImap, GluonSearchImap):
    """The mutation fake with Gluon searches and SELECT allocation evidence."""


def finished_preview(mailbox, *, count=2, confirmation=False, token_clock=None):
    case = mailbox(count=count)
    case.fake = AuditedMutatingImap(case.fake.folders)
    case.fake.reports_uidnext = True
    # Keep membership policy separate from the source so either can drift.
    case.fake.folders["All Mail"]["1"] = deepcopy(case.fake.folders["INBOX"]["1"])
    case.fake.folders["All Mail"]["1000"] = message("<unrelated@example.test>")
    case.cleanup.client.imap_factory = lambda *args, **kwargs: case.fake
    case.cleanup.tokens.confirmation_required = confirmation
    if token_clock is not None:
        case.cleanup.tokens.clock = lambda: token_clock[0]
    preview = case.rules.preview_rules()
    assert preview["token"] and preview["count"] == count and not preview["partial"]
    inventories = [
        entry["inventory"]
        for entry in case.cleanup.client._rules_raw_identity_snapshots.values()
        if entry["inventory"].get("snapshot", {}).get("folder") == "All Mail"
    ]
    assert len(inventories) == 1
    assert inventories[0]["next_uid"] == int(inventories[0]["snapshot"]["uidnext"])
    assert {row["uid"] for row in inventories[0]["rows"]} == {
        *map(str, range(1, count + 1)),
        "1000",
    }
    case.fake.calls.clear()
    return case, preview


def assert_untouched(case, before):
    assert case.fake.folders == before
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)
    assert not case.cleanup.backend.log.path.exists()


def test_apply_reuses_finished_audit_but_reads_matching_occurrences_fresh(mailbox, monkeypatch):
    case, preview = finished_preview(mailbox)
    metadata = case.cleanup.client._metadata
    fresh = []

    def record(connection, uids, **options):
        if options.get("identity_headers_only"):
            fresh.append((case.fake.current, tuple(uids)))
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.cleanup.client, "_metadata", record)
    result = case.rules.apply_rules(preview["token"])

    assert result["done"] == 2 and result["token_consumed"] and not result["partial"]
    assert not case.fake.folders["INBOX"]
    all_mail = [set(uids) for folder, uids in fresh if folder == "All Mail"]
    assert all_mail and set.union(*all_mail) == {"1", "2"}
    assert not any("1000" in uids for _, uids in fresh)
    assert not any(
        call[0] == "SEARCH" and any(atom in call for atom in ("FROM", "SUBJECT", "TO"))
        for call in case.fake.calls
    )


def test_accepted_tradeoff_reuses_audit_after_same_uid_identity_drift(mailbox, monkeypatch):
    case, preview = finished_preview(mailbox)
    all_mail = case.fake.folders["All Mail"]
    allocation = (set(all_mail), len(all_mail), case.fake.next_uids["All Mail"])
    unrelated = all_mail["1000"]
    # Accepted trade-off: apply does not re-read an unrelated kept UID if its
    # content changes in place. Bridge/Gluon assigns a new UID to edited mail.
    unrelated.headers = unrelated.headers[:-2] + b"Message-ID: <rule-1@example.test>\r\n\r\n"
    assert unrelated.headers.count(b"Message-ID:") == 2
    assert (set(all_mail), len(all_mail), case.fake.next_uids["All Mail"]) == allocation
    approved = {id(item) for item in case.fake.folders["INBOX"].values()}
    metadata = case.cleanup.client._metadata
    reads = []
    searches = forbid_full_all_mail_search(case, monkeypatch)

    def record(connection, uids, **options):
        if case.fake.current == "All Mail":
            reads.append(tuple(uids))
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.cleanup.client, "_metadata", record)
    result = case.rules.apply_rules(preview["token"])

    assert result["done"] == 2 and result["token_consumed"] and not result["partial"]
    assert not case.fake.folders["INBOX"]
    assert {id(item) for item in case.fake.folders["Folders/Review"].values()} == approved
    assert case.fake.folders["All Mail"]["1000"] is unrelated
    assert reads and set().union(*map(set, reads)) == {"1", "2"}
    assert not searches


def test_finished_all_mail_walk_applies_all_60_rows_within_one_call(mailbox, monkeypatch):
    case = retest_case(mailbox, monkeypatch, ranges=WALK_PER_CALL + 24)
    # Retain the retest's setup clock while adding actual journaled MOVE support.
    charged_preview_login = case.fake.login
    case.fake = AuditedMutatingImap(case.fake.folders)
    case.fake.reports_uidnext = True
    monkeypatch.setattr(case.fake, "login", charged_preview_login)
    case.cleanup.client.imap_factory = lambda *args, **kwargs: case.fake
    searches = forbid_full_all_mail_search(case, monkeypatch)
    preview = first_token(case)[-1]
    assert preview["count"] == preview["batch_count"] == MATCHES == 60

    clock = [mail.time.monotonic()]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    client = case.cleanup.client
    metadata = type(client)._metadata
    plan = type(case.rules)._plan.__get__(case.rules)
    login = AuditedMutatingImap.login.__get__(case.fake)
    uid = case.fake.uid
    approved = {str(uid) for uid in range(1, MATCHES + 1)}
    reads, moved = [], []

    def charged_login(*args):
        clock[0] += SETUP
        return login(*args)

    def charged_review(connection, deadline, *args, **options):
        clock[0] += REVIEW
        deadline.check(connection)
        return plan(connection, deadline, *args, **options)

    def charged_metadata(connection, uids, **options):
        if case.fake.current == "All Mail" and options.get("identity_headers_only"):
            reads.append(tuple(uids))
            cost = ROW if set(uids) <= approved else RANGE / BATCH
            clock[0] += cost * len(uids)
        return metadata(connection, uids, **options)

    def charged_command(command, *args):
        if command == "MOVE":
            batch = uid_values(args[0])
            moved.extend(batch)
            clock[0] += 2 + 0.05 * len(batch)
        return uid(command, *args)

    monkeypatch.setattr(case.fake, "login", charged_login)
    monkeypatch.setattr(case.fake, "uid", charged_command)
    monkeypatch.setattr(case.rules, "_plan", charged_review)
    monkeypatch.setattr(client, "_metadata", charged_metadata)
    case.fake.calls.clear()
    searches.clear()
    started = clock[0]

    result = case.rules.apply_rules(preview["token"])

    assert 0 < clock[0] - started <= 43.2
    assert result["done"] == result["run_done"] == result["approved_total"] == MATCHES
    assert result["token_consumed"] and not result["partial"] and result["remaining"] == 0
    assert result["continuation_token"] is None
    assert len(moved) == MATCHES and set(moved) == approved
    assert not case.fake.folders["INBOX"]
    assert len(case.fake.folders["Folders/Review"]) == MATCHES
    assert reads and set().union(*map(set, reads)) == approved
    assert not searches


@pytest.mark.parametrize("token", [None, "", "forged"])
def test_finished_audit_cannot_replace_a_preview_token(mailbox, token):
    case, _ = finished_preview(mailbox)
    before = deepcopy(case.fake.folders)
    with pytest.raises(MailValidationError, match="Invalid or already used"):
        case.rules.apply_rules(token)
    assert case.fake.calls == []
    assert_untouched(case, before)


def test_finished_audit_cannot_authorize_another_action_token(mailbox):
    case, preview = finished_preview(mailbox)
    grant = case.cleanup.tokens.inspect(preview["token"], "apply_rules")
    token, _ = case.cleanup.tokens.issue(
        "move_to_folder", grant.params, grant.selector, grant.messages
    )
    before = deepcopy(case.fake.folders)
    with pytest.raises(MailValidationError, match="requested action"):
        case.rules.apply_rules(token)
    assert case.fake.calls == []
    assert_untouched(case, before)


@pytest.mark.parametrize("part", ["params", "selector", "messages"])
def test_finished_audit_does_not_bypass_token_binding(mailbox, part):
    case, preview = finished_preview(mailbox)
    stored = next(iter(case.cleanup.tokens._grants.values()))
    if part == "params":
        stored.params["mark_read"] = True
    elif part == "selector":
        stored.selector["reviewed_rows"].pop()
    else:
        stored.messages.pop()
    before = deepcopy(case.fake.folders)
    with pytest.raises(MailValidationError, match="approved action set"):
        case.rules.apply_rules(preview["token"])
    assert case.fake.calls == []
    assert_untouched(case, before)


@pytest.mark.parametrize("when", ["before-apply", "during-preflight"])
def test_finished_audit_does_not_extend_token_expiry(mailbox, monkeypatch, when):
    clock = [100.0]
    case, preview = finished_preview(mailbox, token_clock=clock)
    grant = case.cleanup.tokens.inspect(preview["token"], "apply_rules")
    before = deepcopy(case.fake.folders)
    if when == "before-apply":
        clock[0] = grant.expires
    else:
        metadata = case.cleanup.client._metadata

        def expire(connection, uids, **options):
            rows = metadata(connection, uids, **options)
            if options.get("identity_headers_only"):
                clock[0] = grant.expires
            return rows

        monkeypatch.setattr(case.cleanup.client, "_metadata", expire)
    with pytest.raises(MailValidationError, match="expired"):
        case.rules.apply_rules(preview["token"])
    if when == "before-apply":
        assert case.fake.calls == []
    assert_untouched(case, before)


def test_finished_audit_still_requires_confirmation_of_exact_action_hash(mailbox):
    case, preview = finished_preview(mailbox, confirmation=True)
    before = deepcopy(case.fake.folders)
    with pytest.raises(MailValidationError, match="user's confirmation"):
        case.rules.apply_rules(preview["token"])
    with pytest.raises(MailValidationError, match="changed during confirmation"):
        case.cleanup.tokens.confirm(preview["token"], "apply_rules", "0" * 64)
    assert case.fake.calls == []
    assert_untouched(case, before)
    case.cleanup.tokens.confirm(preview["token"], "apply_rules", preview["action_hash"])
    result = case.rules.apply_rules(preview["token"])
    assert result["done"] == 2 and result["token_consumed"] and not result["partial"]


def test_finished_audit_cannot_redeem_a_consumed_token(mailbox):
    case, preview = finished_preview(mailbox)
    assert case.rules.apply_rules(preview["token"])["done"] == 2
    case.fake.calls.clear()
    before = deepcopy(case.fake.folders)
    with pytest.raises(MailValidationError, match="already used"):
        case.rules.apply_rules(preview["token"])
    assert case.fake.calls == []
    assert case.fake.folders == before


@pytest.mark.parametrize("scope", ["selector", "message", "source-folder"])
def test_finished_audit_cannot_widen_source_scope(mailbox, scope):
    case, preview = finished_preview(mailbox)
    grant = case.cleanup.tokens.inspect(preview["token"], "apply_rules")
    if scope == "selector":
        grant.selector["folder"] = "All Mail"
    elif scope == "message":
        grant.messages[0]["folder"] = "All Mail"
    else:
        grant.selector["reviewed_rows"][0]["source_folder"] = "All Mail"
    token, _ = case.cleanup.tokens.issue(
        "apply_rules", grant.params, grant.selector, grant.messages
    )
    before = deepcopy(case.fake.folders)
    with pytest.raises(MailValidationError, match="INBOX"):
        case.rules.apply_rules(token)
    assert case.fake.calls == []
    assert_untouched(case, before)


def test_finished_audit_preserves_execute_limit_and_continuation_scope(mailbox):
    case, preview = finished_preview(mailbox)
    before = deepcopy(case.fake.folders)
    with pytest.raises(MailValidationError, match="cannot widen"):
        case.rules.apply_rules(preview["token"], limit=3)
    assert case.fake.calls == []
    assert_untouched(case, before)

    first = case.rules.apply_rules(preview["token"], limit=1)
    assert first["done"] == 1 and first["remaining"] == 1 and first["partial"]
    assert first["approved_total"] == 2 and first["continuation_token"]
    remainder = case.cleanup.tokens.inspect(first["continuation_token"], "apply_rules")
    assert len(remainder.messages) == len(remainder.selector["reviewed_rows"]) == 1
    second = case.rules.apply_rules(first["continuation_token"])
    assert second["done"] == 1 and second["run_done"] == 2 and not second["partial"]
    assert not case.fake.folders["INBOX"]
    assert len(case.fake.folders["Folders/Review"]) == 2
    with pytest.raises(MailValidationError, match="already used"):
        case.rules.apply_rules(preview["token"])


@pytest.mark.parametrize(
    "change",
    [
        "uidvalidity",
        "source-uid",
        "message-id",
        "date",
        "size",
        "source-protection",
        "membership-protection",
        "hidden-duplicate",
        "rules",
    ],
)
def test_finished_audit_does_not_replace_fresh_refusal_checks(mailbox, change):
    case, preview = finished_preview(mailbox)
    source = case.fake.folders["INBOX"]["1"]
    peer = case.fake.folders["All Mail"]["1"]
    if change == "uidvalidity":
        case.fake.uidvalidity = "2"
    elif change == "source-uid":
        case.fake.folders["INBOX"]["3"] = case.fake.folders["INBOX"].pop("1")
    elif change == "message-id":
        source.headers = source.headers.replace(b"<rule-1@example.test>", b"<changed@example.test>")
    elif change == "date":
        source.headers = source.headers.replace(b"10:00:00", b"11:00:00")
    elif change == "size":
        source.sections["1"] += b"changed"
    elif change == "source-protection":
        source.headers = source.headers.replace(b"sender@example.com", b"relative@gmail.com")
    elif change == "membership-protection":
        peer.headers = peer.headers.replace(b"sender@example.com", b"relative@gmail.com")
    elif change == "hidden-duplicate":
        peer.headers = peer.headers[:-2] + b"Message-ID: <hidden@example.test>\r\n\r\n"
    else:
        case.path.write_text(json.dumps({"version": 1, "rules": [rule(folder="Folders/Other")]}))
    before = deepcopy(case.fake.folders)
    with pytest.raises(MailValidationError):
        case.rules.apply_rules(preview["token"])
    assert_untouched(case, before)
    assert case.cleanup.tokens.inspect(preview["token"], "apply_rules")
