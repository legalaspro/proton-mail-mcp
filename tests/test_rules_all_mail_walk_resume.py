"""The All Mail audit keeps its place between preview_rules calls."""

import re

import pytest
from test_background_sync import ExistsImap
from test_folder_rules import mailbox as mailbox
from test_mail import message
from test_preview_rules_bounded import assert_read_only
from test_rules_matched_membership import forbid_full_all_mail_search, matched_case

from proton_mail_mcp import cleanup_lookup, folder_rules, mail, rules_preview
from proton_mail_mcp.mail import CallDeadlineExceeded, MailError, MailValidationError

ROWS, BATCH = 600, 20  # 30 batches, the walk's shape at a tenth of the live 238.
OVERHEAD = 3.8  # Live: everything before All Mail in a later call of the 18 s window.
RAW_ROW = 3.5 / BATCH  # So about 4 cold header batches fit one call.
UID_ROW = 1.0 / BATCH  # A UID-only read of an old range; about 0.3 of cold, as modelled.
MATCHES = 5


def walk_case(mailbox, monkeypatch):
    monkeypatch.setattr(cleanup_lookup, "_IDENTITY_BATCH_SIZE", BATCH)
    case = matched_case(mailbox, count=MATCHES)
    del case.fake.folders["All Mail"]["1000"]
    for uid in range(MATCHES + 1, ROWS + 1):
        case.fake.folders["All Mail"][str(uid)] = message(f"<unrelated-{uid}@example.test>")
    forbid_full_all_mail_search(case, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: clock[0])
    reader, login = case.cleanup.client, case.fake.login
    metadata = reader._metadata
    case.uid_only, case.raw, case.during_fetch = [], [], None

    def charged_login(*args):
        clock[0] += OVERHEAD
        return login(*args)

    def charged_metadata(connection, uids, **options):
        if case.fake.current == "All Mail":
            uid_only = not options.get("headers", True)
            (case.uid_only if uid_only else case.raw).append(tuple(uids))
            if uid_only or options.get("identity_headers_only"):
                # A command that would pass the window is cut by the watchdog.
                deadline = options["deadline"]
                cost = (UID_ROW if uid_only else RAW_ROW) * len(uids)
                clock[0] += min(cost, max(0.0, deadline.io_end - clock[0]))
            if case.during_fetch and not uid_only:
                case.during_fetch(tuple(uids))
        return metadata(connection, uids, **options)

    monkeypatch.setattr(case.fake, "login", charged_login)
    monkeypatch.setattr(reader, "_metadata", charged_metadata)
    case.next_arrival = ROWS + 1
    return case


def arrival(case):
    """One unrelated message reaches INBOX and All Mail; no rule matches it."""
    uid = case.next_arrival
    case.next_arrival += 1
    item = message(f"<arrival-{uid}@example.test>")
    case.fake.folders["All Mail"][str(uid)] = item
    case.fake.folders["INBOX"][str(uid)] = item
    return str(uid)


def audit(case, cursor):
    return case.cleanup._rules_preview_states[cursor]["inventories"]["All Mail"]


def assert_like_the_retest(result):
    assert result["partial"] and result["next_cursor"]
    assert result["token"] is None and result["batch_count"] == 0 and result["count"] == 0
    assert result["remaining"] == MATCHES
    assert result["reason"].startswith(
        'Rules preview deadline reached during membership check in "All Mail"; '
        "continue preview_rules with next_cursor; "
    )
    assert re.search(r"; all_mail_audit=[1-9][0-9]*/[1-9][0-9]* rewinds=[0-9]+;", result["reason"])


def assert_audit_progress(case, result, *, rewinds=0):
    saved = audit(case, result["next_cursor"])
    assert (
        f"all_mail_audit={saved['next_uid']}/{saved['snapshot']['uidnext']} rewinds={rewinds}"
        in result["reason"]
    )
    state = case.cleanup._rules_preview_states[result["next_cursor"]]
    assert state.get("all_mail_rewinds", 0) == rewinds


def chain(case, *, cursor=None, arrive_every=None, calls=40, rewinds=0):
    """Follow one cursor to its first token; return next_uid per call, and the token call."""
    trace = []
    for number in range(1, calls + 1):
        if arrive_every and number > 1 and (number - 1) % arrive_every == 0:
            arrival(case)
        result = case.rules.preview_rules(cursor=cursor)
        if result["token"]:
            return trace, number, result
        assert_like_the_retest(result)
        assert_audit_progress(case, result, rewinds=rewinds)
        cursor = result["next_cursor"]
        trace.append(audit(case, cursor)["next_uid"])
    return trace, None, result


def test_quiet_mailbox_resumes_exactly_where_it_stopped(mailbox, monkeypatch):
    case = walk_case(mailbox, monkeypatch)

    trace, finished, result = chain(case)

    assert trace == sorted(set(trace)), "the saved position must only move forward"
    raw = [batch for batch in case.raw if len(batch) == BATCH]
    assert len(raw) == len(set(raw)) + len(trace), "only each call's cut batch is read twice"
    assert not case.uid_only
    assert finished is not None and result["batch_count"] > 0
    assert_read_only(case.fake)


def test_replies_show_that_the_audit_moved(mailbox, monkeypatch):
    case = walk_case(mailbox, monkeypatch)
    first = case.rules.preview_rules()
    before = audit(case, first["next_cursor"])["next_uid"]
    second = case.rules.preview_rules(cursor=first["next_cursor"])
    after = audit(case, second["next_cursor"])["next_uid"]
    assert after > before

    def public(result):
        reason = re.sub(r"stage_ms=\(.*\)", "stage_ms=(...)", result["reason"])
        return dict(result, next_cursor=None, reason=reason)

    assert public(first) != public(second), (
        f"the walk advanced from UID {before} to UID {after}, yet the two replies are "
        "identical apart from the cursor and the timings"
    )


def test_one_arrival_between_calls_does_not_rewind_the_walk(mailbox, monkeypatch):
    case = walk_case(mailbox, monkeypatch)
    first = case.rules.preview_rules()
    stopped_at = audit(case, first["next_cursor"])["next_uid"]
    assert stopped_at > 1
    arrival(case)

    second = case.rules.preview_rules(cursor=first["next_cursor"])

    assert_like_the_retest(second)
    assert_audit_progress(case, second)
    assert case.uid_only == [], (
        f"the walk had stopped at UID {stopped_at}, and one new message sent it back to UID "
        f"{case.uid_only[0][0]}: {len(case.uid_only)} old ranges were read again"
    )
    assert audit(case, second["next_cursor"])["next_uid"] > stopped_at


@pytest.mark.parametrize("arrive_every", [1, 2])
def test_chain_finishes_while_mail_keeps_arriving(mailbox, monkeypatch, arrive_every):
    case = walk_case(mailbox, monkeypatch)

    trace, finished, result = chain(case, arrive_every=arrive_every)

    assert finished is not None, (
        f"no token in {len(trace)} calls with one new message every {arrive_every} call(s); "
        f"saved next_uid per call: {trace}"
    )
    assert result["count"] == result["batch_count"] > 0
    assert_read_only(case.fake)


def test_mail_arriving_mid_call_waits_for_the_next_call(mailbox, monkeypatch):
    case = walk_case(mailbox, monkeypatch)
    case.fake.__class__ = ExistsImap
    arrived = []

    def during_fetch(uids):
        if not arrived:
            arrived.append(arrival(case))
            case.fake.queued_exists = len(case.fake.folders["All Mail"])

    case.during_fetch = during_fetch
    first = case.rules.preview_rules()
    assert_like_the_retest(first)
    assert arrived and arrived[0] not in [uid for batch in case.raw for uid in batch]
    case.during_fetch = None

    trace, finished, result = chain(case, cursor=first["next_cursor"])

    assert finished is not None and result["token"]
    assert arrived[0] in [uid for batch in case.raw for uid in batch]


def test_expunge_hidden_by_arrivals_still_reenumerates(mailbox, monkeypatch):
    case = walk_case(mailbox, monkeypatch)
    first = case.rules.preview_rules()
    assert audit(case, first["next_cursor"])["next_uid"] > 21
    # UID 21 was walked. EXISTS grows by 1 and UIDNEXT by 2: the two cannot agree.
    del case.fake.folders["All Mail"]["21"]
    arrival(case)
    arrival(case)

    second = case.rules.preview_rules(cursor=first["next_cursor"])

    assert second["next_cursor"] and case.uid_only[0][0] == "1"
    assert_audit_progress(case, second, rewinds=1)
    trace, finished, result = chain(case, cursor=second["next_cursor"], rewinds=1)
    assert finished is not None and result["token"]
    snapshots = case.cleanup.client._rules_raw_identity_snapshots
    rows = [row["uid"] for entry in snapshots.values() for row in entry["inventory"]["rows"]]
    assert "21" not in rows


def test_same_count_update_still_reenumerates(mailbox, monkeypatch):
    # Gluon gives an edited message a new UID: EXISTS is unchanged, UIDNEXT moves.
    case = walk_case(mailbox, monkeypatch)
    first = case.rules.preview_rules()
    cursor = first["next_cursor"]
    for rewinds, removed in enumerate(("21", "22"), start=1):
        del case.fake.folders["All Mail"][removed]
        arrival(case)
        case.uid_only.clear()

        result = case.rules.preview_rules(cursor=cursor)

        assert result["next_cursor"] and case.uid_only[0][0] == "1"
        assert_audit_progress(case, result, rewinds=rewinds)
        cursor = result["next_cursor"]


def test_arrival_with_a_hidden_duplicate_identity_is_still_refused(mailbox, monkeypatch):
    case = walk_case(mailbox, monkeypatch)
    first = case.rules.preview_rules()
    peer = message("<arrival@example.test>")
    peer.headers = peer.headers[:-2] + b"Message-ID: <rule-1@example.test>\r\n\r\n"
    case.fake.folders["All Mail"][str(ROWS + 1)] = peer

    def unexpected_issue(*args, **kwargs):
        pytest.fail("A hidden duplicate in new mail must not be approved")

    monkeypatch.setattr(case.cleanup.tokens, "issue", unexpected_issue)
    cursor = first["next_cursor"]
    with pytest.raises(MailValidationError, match="unresolved|ambiguous|Message-ID"):
        for _ in range(40):
            cursor = case.rules.preview_rules(cursor=cursor)["next_cursor"]
            assert cursor


def test_arrival_during_the_finishing_call_keeps_the_cursor(mailbox, monkeypatch):
    case = walk_case(mailbox, monkeypatch)
    arrived = []

    def during_fetch(uids):
        if str(ROWS) in uids and not arrived:
            arrived.append(arrival(case))

    case.during_fetch = during_fetch
    cursor = None
    for _ in range(40):
        result = case.rules.preview_rules(cursor=cursor)
        cursor = result["next_cursor"]
        if arrived:
            break
        assert result["partial"] and cursor
    assert arrived and result["partial"] and cursor is not None
    assert result["token"] is None and not result["total_is_exact"]
    assert result["count"] == result["batch_count"] == 0
    assert result["remaining"] == MATCHES
    assert "changed" in result["reason"] and "continue preview_rules" in result["reason"]
    assert arrived[0] not in [uid for batch in case.raw for uid in batch]

    trace, finished, result = chain(case, cursor=cursor)

    assert finished is not None and result["token"]
    assert arrived[0] in [uid for batch in case.raw for uid in batch]
    assert not case.uid_only
    assert_read_only(case.fake)


def test_arrival_during_discovery_keeps_completed_rows_and_the_cursor(mailbox, monkeypatch):
    case = matched_case(mailbox, count=MATCHES)
    reader = case.cleanup.client
    metadata = reader._metadata
    discovered, arrived = [], []

    def during_discovery(connection, uids, **options):
        rows = metadata(connection, uids, **options)
        if case.fake.current == "INBOX" and not options.get("headers", True):
            discovered.extend(uids)
            if not arrived:
                item = message("<discovery-arrival@example.test>")
                uid = str(MATCHES + 1)
                arrived.append(uid)
                case.fake.folders["INBOX"][uid] = item
                case.fake.folders["All Mail"][uid] = item
        return rows

    monkeypatch.setattr(reader, "_metadata", during_discovery)
    first = case.rules.preview_rules()

    assert arrived and first["partial"] and not first["total_is_exact"]
    assert first["next_cursor"] and first["token"] is None
    assert first["count"] == first["batch_count"] == 0
    assert first["remaining"] == MATCHES
    assert "changed during source discovery" in first["reason"]
    assert "continue preview_rules" in first["reason"]
    assert discovered == [str(uid) for uid in range(1, MATCHES + 1)]
    state = case.cleanup._rules_preview_states[first["next_cursor"]]
    assert state["source"]["offset"] == MATCHES

    second = case.rules.preview_rules(cursor=first["next_cursor"])

    assert second["token"] and second["count"] == second["batch_count"] == MATCHES
    assert discovered == [str(uid) for uid in range(1, MATCHES + 2)]
    assert_read_only(case.fake)


def test_repeated_membership_changes_are_bounded_by_the_stall_guard(mailbox, monkeypatch):
    case = matched_case(mailbox, count=MATCHES)
    attempts = []

    def changed(*args):
        attempts.append(True)
        raise cleanup_lookup._PreviewInventoryGrew(
            'Message set changed during rules discovery in "INBOX"'
        )

    monkeypatch.setattr(rules_preview, "_verify_inventory", changed)
    cursor = None
    for _ in range(folder_rules.MAX_STALLED_PREVIEWS + 2):
        result = case.rules.preview_rules(cursor=cursor)
        assert result["partial"] and result["token"] is None
        cursor = result["next_cursor"]
        if cursor is None:
            break
        assert "changed during source discovery" in result["reason"]
        assert "continue preview_rules" in result["reason"]

    # Discovery earns progress on its first call; repeatedly failing the final
    # verification after that adds no work and must end the cursor chain.
    assert len(attempts) == folder_rules.MAX_STALLED_PREVIEWS + 1
    assert cursor is None and "cannot make progress" in result["reason"]
    assert "continue" not in result["reason"]
    assert not case.cleanup._rules_preview_states
    assert_read_only(case.fake)


def test_apply_after_a_finished_walk_fits_its_call(mailbox, monkeypatch):
    case = walk_case(mailbox, monkeypatch)
    cursor = None
    for _ in range(40):
        result = case.rules.preview_rules(cursor=cursor)
        if result["token"]:
            break
        assert result["partial"] and result["next_cursor"]
        cursor = result["next_cursor"]
    else:
        pytest.fail("preview did not finish its audit")
    assert result["batch_count"] == MATCHES
    case.raw.clear()
    case.uid_only.clear()
    started = mail.time.monotonic()

    try:
        applied = case.rules.apply_rules(result["token"])
    except CallDeadlineExceeded as error:
        rows = sum(map(len, case.raw))
        pytest.fail(f"{error}: apply asked for {rows} All Mail rows that the preview had audited")
    except MailError:
        pass  # This fake has no MOVE, so reaching it means the audit fitted.
    else:
        # A backend refusal must not count as a successful budget check.
        assert applied["done"] + applied["uncertain"] == MATCHES
    assert mail.time.monotonic() - started <= 43.2
    assert case.raw and {uid for batch in case.raw for uid in batch} <= {
        str(uid) for uid in range(1, MATCHES + 1)
    }
    assert not case.uid_only
