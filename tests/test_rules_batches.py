"""Bounded rules batches use synthetic mail and a manually advanced timer clock."""

from copy import deepcopy

import pytest
from test_cleanup_mutations import MUTATIONS, uid_values
from test_folder_rules import mailbox as mailbox
from test_folder_rules import rule
from test_mail import message
from test_rules_inbox_attributes import inbox_reply
from test_rules_timer import changed_uids, mark
from test_rules_timer import timer_case as timer_case

from proton_mail_mcp.mail import MailValidationError


def approved(case, preview):
    grant = case.cleanup.tokens.inspect(preview["token"], "apply_rules")
    assert len(grant.messages) <= 200 and len(grant.selector["reviewed_rows"]) <= 200
    return {row["uid"] for row in grant.messages}


@pytest.fixture
def still_clock(monkeypatch):
    """Hold the clock still while whole batches are applied.

    Windows keeps fsync real, and the journal flushes one line per row and stage: a loaded
    runner spent the 48 s execute budget on the disk alone. Which rows move is not a question
    of time here.
    """
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: 1000.0)


@pytest.mark.parametrize("order", ["oldest", "newest"])
def test_545_matches_issue_exact_200_uid_batch_with_345_remaining(mailbox, order):
    case = mailbox(count=545)
    preview = case.rules.preview_rules(order=order)
    expected = range(1, 201) if order == "oldest" else range(346, 546)
    assert preview["token"] and preview["count"] == preview["batch_count"] == 200
    assert preview["total_matches"] == preview["matched_count"] == 545
    assert preview["remaining"] == 345 and "more runs" in preview["reason"]
    assert approved(case, preview) == {str(uid) for uid in expected}
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)


@pytest.mark.parametrize("order", ["oldest", "newest"])
def test_order_uses_internaldate_then_numeric_uid_across_rules(mailbox, order):
    case = mailbox(count=12, rules=[rule("subject", "First"), rule(folder="Folders/Other")])
    for uid, item in case.fake.folders["INBOX"].items():
        # The Date header remains identical; only INTERNALDATE governs order.
        item.internaldate = (
            "13-Oct-2026 09:00:00 +0000" if int(uid) % 2 else "15-Oct-2026 09:00:00 +0000"
        )
        if int(uid) > 6:
            item.headers = item.headers.replace(b"Subject: Hello", b"Subject: First")
    preview = case.rules.preview_rules(limit=3, order=order)
    assert approved(case, preview) == ({"1", "3", "5"} if order == "oldest" else {"8", "10", "12"})
    assert preview["batch_count"] == preview["count"] == 3
    assert preview["total_matches"] == 12 and preview["remaining"] == 9


@pytest.mark.usefixtures("still_clock")
@pytest.mark.parametrize("order", ["oldest", "newest"])
def test_batches_drain_200_200_145_then_zero(mailbox, monkeypatch, order):
    case = mailbox(count=545)
    mark_read = order == "newest"
    if mark_read:
        inbox_reply(case, monkeypatch, "INBOX", b'(\\HasNoChildren) "/" "INBOX"')
    for count, total, remaining in [(200, 545, 345), (200, 345, 145), (145, 145, 0)]:
        preview = case.rules.preview_rules(order=order, mark_read=mark_read)
        assert (preview["batch_count"], preview["total_matches"], preview["remaining"]) == (
            count,
            total,
            remaining,
        )
        selected = approved(case, preview)
        previous = set(case.fake.folders["INBOX"])
        selected_items = [case.fake.folders["INBOX"][uid] for uid in selected]
        offset = len(case.fake.calls)
        assert case.rules.apply_rules(preview["token"])["count"] == count
        assert set(case.fake.folders["INBOX"]) == previous - selected
        stores = {
            uid
            for call in case.fake.calls[offset:]
            if call[0] == "STORE"
            for uid in uid_values(call[1])
        }
        assert stores == (selected if mark_read else set())
        assert all(
            (r"\Seen" in getattr(item, "flags", set())) == mark_read for item in selected_items
        )
        with pytest.raises(MailValidationError):
            case.rules.apply_rules(preview["token"])
    preview = case.rules.preview_rules(order=order)
    assert preview["token"] is None
    assert preview["count"] == preview["batch_count"] == preview["total_matches"] == 0
    assert preview["remaining"] == 0


@pytest.mark.usefixtures("still_clock")
@pytest.mark.parametrize("order", ["oldest", "newest"])
def test_apply_never_searches_rules_again_or_admits_arrival(mailbox, monkeypatch, order):
    case = mailbox(count=545)
    mark_read = order == "newest"
    if mark_read:
        inbox_reply(case, monkeypatch, "INBOX", b'(\\HasNoChildren) "/" "INBOX"')
    preview = case.rules.preview_rules(order=order, mark_read=mark_read)
    selected = approved(case, preview)
    newcomer = message("<batch-new@example.test>")
    case.fake.folders["INBOX"]["546"] = newcomer
    case.fake.folders["All Mail"]["546"] = newcomer
    search = case.cleanup.client._search
    selectors = []

    def exact_only(connection, criteria, **kwargs):
        selectors.extend({"FROM", "TO", "SUBJECT"}.intersection(criteria))
        return search(connection, criteria, **kwargs)

    monkeypatch.setattr(case.cleanup.client, "_search", exact_only)
    case.fake.calls.clear()
    try:
        result = case.rules.apply_rules(preview["token"])
    finally:
        assert not selectors, "Bound batch confirmation must not rediscover its selector"
    assert result["count"] == 200 and not result["partial"]
    moved = {uid for call in case.fake.calls if call[0] == "MOVE" for uid in uid_values(call[1])}
    assert moved == selected
    stores = {uid for call in case.fake.calls if call[0] == "STORE" for uid in uid_values(call[1])}
    assert stores == (selected if mark_read else set())
    assert case.fake.folders["INBOX"]["546"] is newcomer
    assert r"\Seen" not in getattr(newcomer, "flags", set())
    assert len(case.fake.folders["INBOX"]) == 346


@pytest.mark.parametrize("limit", [0, -1, 201, True, False, 1.5, "200", None])
def test_limit_rejected_before_mail_access(mailbox, limit):
    case = mailbox()
    with pytest.raises(MailValidationError, match="limit"):
        case.rules.preview_rules(limit=limit)
    assert not case.fake.calls


@pytest.mark.parametrize("order", ["OLDest", "", "ascending", None, True, [], {}])
def test_order_rejected_before_mail_access(mailbox, order):
    case = mailbox()
    with pytest.raises(MailValidationError, match="order"):
        case.rules.preview_rules(order=order)
    assert not case.fake.calls


@pytest.mark.parametrize("drift", ["uidvalidity", "message-id", "date", "size"])
def test_bounded_batch_refuses_identity_drift_before_any_mutation(mailbox, drift):
    case = mailbox(count=201)
    preview = case.rules.preview_rules()
    item = case.fake.folders["INBOX"]["1"]
    if drift == "uidvalidity":
        case.fake.uidvalidity = "2"
    elif drift == "message-id":
        item.headers = item.headers.replace(b"<rule-1@example.test>", b"<changed@example.test>")
    elif drift == "date":
        item.headers = item.headers.replace(b"10:00:00", b"11:00:00")
    else:
        item.sections["1"] += b"changed"
    case.fake.calls.clear()
    with pytest.raises(MailValidationError):
        case.rules.apply_rules(preview["token"])
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)


@pytest.mark.usefixtures("still_clock")
def test_large_batch_validates_protected_memberships_before_token(mailbox):
    case = mailbox(count=545)
    peer = deepcopy(case.fake.folders["INBOX"]["1"])
    peer.headers = peer.headers.replace(b"sender@example.com", b"relative@gmail.com")
    case.fake.folders["All Mail"]["1"] = peer
    preview = case.rules.preview_rules()
    assert preview["batch_count"] == 200 and preview["count"] == 199
    assert preview["remaining"] == 345
    assert "1" not in approved(case, preview)
    assert case.cleanup.tokens.inspect(preview["token"], "apply_rules").selector["reviewed_rows"]
    assert case.rules.apply_rules(preview["token"])["count"] == 199
    assert "1" in case.fake.folders["INBOX"]


def test_large_batch_refuses_malformed_cross_folder_identity(mailbox):
    case = mailbox(count=545)
    peer = deepcopy(case.fake.folders["INBOX"]["1"])
    peer.headers = peer.headers[:-2] + b"Message-ID: <hidden@example.test>\r\n\r\n"
    case.fake.folders["All Mail"]["1"] = peer
    with pytest.raises(MailValidationError):
        case.rules.preview_rules()
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)


def test_source_drift_between_count_and_batch_resolution_fails_closed(mailbox, monkeypatch):
    case = mailbox(count=545)
    resolve = case.cleanup._resolve

    def changed(*args, **kwargs):
        if kwargs.get("source_rows"):
            item = case.fake.folders["INBOX"]["1"]
            item.headers = item.headers.replace(b"Subject: Hello", b"Subject: Different")
        return resolve(*args, **kwargs)

    monkeypatch.setattr(case.cleanup, "_resolve", changed)
    with pytest.raises(MailValidationError, match="changed"):
        case.rules.preview_rules()
    assert not any(call[0] in MUTATIONS for call in case.fake.calls)


def test_first_install_timer_drains_545_over_bounded_clocked_runs(timer_case, monkeypatch):
    case = timer_case(count=545)
    clock = [1000.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: clock[0])
    for count, remaining, last in [(200, 345, 200), (200, 145, 400), (145, 0, 545), (0, 0, 545)]:
        before = len(changed_uids(case))
        result = case.timer.run(cap=200)
        assert result["count"] == count
        assert len(changed_uids(case)) - before == count
        assert len(case.fake.folders["INBOX"]) == remaining
        assert mark(case) == {"uidvalidity": "1", "uid": last, "pending": False}
        clock[0] += 300
    assert len(case.fake.folders["Folders/Review"]) == 545
    assert not any(call[0] == "STORE" for call in case.fake.calls)
