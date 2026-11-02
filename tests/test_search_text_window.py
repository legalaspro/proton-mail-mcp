"""A large folder's narrow date window answers text conditions without Bridge's whole-folder scan.

Gluon loads every message's literal for one TEXT key, whatever date keys share the
command, so this fake refuses TEXT in a large folder the way the live Bridge times out.
"""

import asyncio
import base64
import imaplib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from email.utils import parsedate_to_datetime
from types import SimpleNamespace

import pytest
from bridge_sim import BridgeSim, Msg, hdr, make_client
from gluon_search import GluonSearchImap
from test_mail import PLAIN, client, message, parsed_headers
from test_mail import settings as settings

from proton_mail_mcp import canonical, search, tool_docs
from proton_mail_mcp.config import Settings
from proton_mail_mcp.mail import CallDeadlineExceeded, MailValidationError
from proton_mail_mcp.server import create_mcp

SINCE = "2026-10-25"
OLD = "Mon, 05 Oct 2026 10:00:00 +0000"
NEW = "Thu, 29 Oct 2026 10:00:00 +0000"


def body_message(identifier, body, *, date=NEW, subject="Hello"):
    payload = body.encode("utf-8")
    structure = PLAIN.replace(b"24 2", str(len(payload)).encode() + b" 1")
    return message(
        identifier, date=date, subject=subject, structure=structure, sections={"1": payload}
    )


class TextBridge(GluonSearchImap):
    """Serve BODY.PEEK[] partials; TEXT over a large folder stalls until the socket gives up."""

    def __init__(self, *args, clock=None, literal_cost=0.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.clock, self.literal_cost = clock, literal_cost
        self.literal_reads = []
        self.unreadable = set()

    def uid(self, command, *args):
        if command == "SEARCH" and any(
            key in (b"TEXT", "TEXT") or (isinstance(key, bytes) and b"TEXT" in key) for key in args
        ):
            self.text_scans = getattr(self, "text_scans", 0) + 1
            if getattr(self, "stall_text", False):
                raise TimeoutError
        if command == "FETCH" and args[1].startswith("(BODY.PEEK[]<0."):
            uid, section, rest = args[0], *args[1].split(" ", 1)
            limit = int(section.removeprefix("(BODY.PEEK[]<0.").removesuffix(">"))
            if uid in self.unreadable:
                self.calls.append((command, *args))
                return "NO", [b"unavailable"]
            self.literal_reads.append(uid)
            if self.clock is not None:
                self.clock[0] += self.literal_cost
            # The base fake records this call and answers the fields beside the raw message.
            status, data = super().uid(command, uid, "(" + rest)
            self.calls[-1] = (command, *args)
            item = self.folders[self.current].get(uid)
            if item is None:
                return status, data
            raw = (item.headers + b"\r\n" + b"".join(item.sections.values()))[:limit]
            fields, *end = data
            return status, [
                (b"1 (BODY[]<0> {" + str(len(raw)).encode() + b"}", raw),
                (fields[0].removeprefix(b"1 ("), fields[1]),
                *end,
            ]
        return super().uid(command, *args)


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: now[0])
    return now


@pytest.fixture
def large_folder(monkeypatch):
    """Make a few fake messages count as a folder too large for Bridge's TEXT scan."""
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)


def old_and_recent(recent, first=10):
    items = {
        str(uid): body_message(f"<old-{uid}@example.test>", "needle in old mail", date=OLD)
        for uid in range(1, 7)
    }
    for offset, (name, body) in enumerate(
        (recent or {"hit": "the needle is here", "miss": "nothing to see"}).items()
    ):
        items[str(first + offset)] = body_message(f"<{name}@example.test>", body)
    return items


def mailbox(settings, *, recent=None, first=10, **options):
    """Six old messages outside the window, plus the recent ones."""
    fake = TextBridge({"INBOX": old_and_recent(recent, first)}, uidnext=True, **options)
    fake.stall_text = True
    return client(settings, fake), fake


def all_mail_box(settings, *, recent=None, **options):
    """The same mail in All Mail, a view whose rows report their real folder as canonical."""
    items = old_and_recent(recent or {"a_stuck": "x", "b_hit": "needle", "c_hit": "needle"})
    fake = TextBridge({"All Mail": items, "INBOX": dict(items)}, uidnext=True, **options)
    fake.stall_text = True
    return client(settings, fake), fake


def ids(result):
    return [item["message_id"] for item in result["messages"]]


# The reported failure: narrow windows timed out whichever way text was asked for.
@pytest.mark.parametrize(
    "condition",
    [{"text": "needle"}, {"query": {"text": "needle"}}],
    ids=["plain_field", "query_object"],
)
def test_narrow_window_finds_text_without_a_bridge_text_scan(settings, large_folder, condition):
    reader, fake = mailbox(settings)

    result = reader.search_mail(since=SINCE, include_snippets=False, **condition)

    assert ids(result) == ["<hit@example.test>"]
    assert result["partial"] is False and result["total"] == 1 and result["total_is_exact"] is True
    assert "timed_out" not in result
    assert not fake.search_literals and getattr(fake, "text_scans", 0) == 0
    # Only the window's messages were read, and all reads leave messages unread.
    assert sorted(fake.literal_reads) == ["10", "11"]
    assert all(call[2] is True for call in fake.calls if call[0] == "EXAMINE")
    assert all(
        "PEEK" in call[2] or "BODY" not in call[2] for call in fake.calls if call[0] == "FETCH"
    )


@pytest.mark.parametrize(
    "conditions,expected",
    [
        ({"text": "NEEDLE"}, ["<hit@example.test>"]),
        ({"exclude_text": "needle"}, ["<miss@example.test>"]),
        ({"text": "sender@example.com"}, ["<hit@example.test>", "<miss@example.test>"]),
        ({"query": {"not": {"text": "needle"}}}, ["<miss@example.test>"]),
        ({"query": {"or": [{"text": "needle"}, {"text": "nothing"}]}}, None),
        ({"query": {"or": [{"subject": "absent"}, {"text": "needle"}]}}, ["<hit@example.test>"]),
        ({"text": "needle", "subject": "absent"}, []),
        ({"text": "nèedle"}, []),
    ],
)
def test_local_comparison_answers_as_bridge_text_does(settings, monkeypatch, conditions, expected):
    """The same query gets the same rows from the server path and the local one."""

    def rows(limit):
        monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", limit)
        reader, fake = mailbox(settings)
        fake.stall_text = False
        result = reader.search_mail(since=SINCE, include_snippets=False, **conditions)
        return sorted(ids(result)), fake

    bridge, by_bridge = rows(1000)
    local, by_scan = rows(5)

    assert local == bridge
    if expected is not None:
        assert local == sorted(expected)
    assert not by_bridge.literal_reads and getattr(by_bridge, "text_scans", 0) >= 1
    assert getattr(by_scan, "text_scans", 0) == 0


def test_ascii_text_ignores_case_in_the_message_too(settings, large_folder):
    reader, _ = mailbox(settings, recent={"hit": "the NEEDLE is here", "miss": "plain"})

    result = reader.search_mail(text="needle", since=SINCE, include_snippets=False)

    assert ids(result) == ["<hit@example.test>"]


def test_non_ascii_text_ignores_case_like_bridge(settings, large_folder):
    reader, fake = mailbox(settings, recent={"hit": "Prüfung BESTÄTIGT heute", "miss": "plain"})

    result = reader.search_mail(text="bestätigt", since=SINCE, include_snippets=False)

    assert ids(result) == ["<hit@example.test>"]
    assert fake.search_literals == []


def test_cached_header_conditions_rule_messages_out_before_any_body_read(settings, large_folder):
    reader, fake = mailbox(settings)
    reader.search_mail(subject="Hello", since=SINCE, include_snippets=False)
    fake.literal_reads.clear()

    assert ids(reader.search_mail(subject="absent", text="needle", since=SINCE)) == []
    assert fake.literal_reads == []


def test_unread_only_narrows_the_window_with_flags(settings, large_folder):
    reader, fake = mailbox(settings)
    fake.folders["INBOX"]["10"].flags = {"\\Seen"}

    result = reader.search_mail(text="needle", since=SINCE, unread_only=True)

    assert ids(result) == [] and fake.literal_reads == ["11"]


def test_message_text_never_reaches_the_index(settings, large_folder):
    reader, fake = mailbox(settings, recent={"hit": "needle-only-in-this-body"})

    reader.search_mail(text="needle-only", since=SINCE, include_snippets=False)

    path = reader.search_index.path
    if path.exists():
        with closing(sqlite3.connect(path)) as database:
            assert list(database.execute("PRAGMA integrity_check")) == [("ok",)]
            database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert b"needle-only-in-this-body" not in path.read_bytes()


# Where the local path does not apply, Bridge's scan and its timeout stay as they were.
def test_small_folder_keeps_the_single_server_search(settings, monkeypatch):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 1000)
    reader, fake = mailbox(settings)
    fake.stall_text = False

    assert ids(reader.search_mail(text="needle", since=SINCE, include_snippets=False)) == [
        "<hit@example.test>"
    ]
    assert fake.text_scans == 1 and not fake.literal_reads


def test_missing_window_stays_a_bridge_scan_and_times_out(settings, monkeypatch):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    reader, fake = mailbox(settings)

    result = reader.search_mail(text="needle", include_snippets=False)

    assert result["partial"] is True and result["partial_reason"] == "timed_out"
    assert result["timed_out"] is True and "narrow the date range" in result["error"]
    assert fake.text_scans == 1 and not fake.literal_reads


# Honest partial answers.
def test_budget_stop_returns_what_it_found_and_a_cursor_reads_the_rest(
    settings, monkeypatch, clock
):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {f"m{uid}": ("needle" if uid % 2 else "nothing") for uid in range(1, 13)}
    # About five reads fit each call's 20 seconds, leaving the reserve for the page.
    reader, fake = mailbox(settings, recent=recent, clock=clock, literal_cost=2.5)

    seen, cursor, pages = [], None, 0
    while True:
        clock[0] = 100.0
        page = reader.search_mail(text="needle", since=SINCE, include_snippets=False, cursor=cursor)
        pages += 1
        seen.extend(ids(page))
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert page["partial"] is True and page["partial_reason"] == "text_scan_limited"
        assert page["total_is_exact"] is False and "timed_out" not in page

    assert pages > 1 and page["partial"] is False
    expected = {f"<m{uid}@example.test>" for uid in range(1, 13) if uid % 2}
    assert len(seen) == len(set(seen)) and set(seen) == expected
    # No message was read twice, so every call made progress.
    assert len(fake.literal_reads) == len(set(fake.literal_reads)) == 12


def test_a_cursor_too_long_to_hold_the_progress_says_so(settings, monkeypatch, clock):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    monkeypatch.setattr(search, "_MAX_CURSOR_CHARS", 100)
    recent = {f"m{uid}": "nothing" for uid in range(1, 20)}
    reader, _ = mailbox(settings, recent=recent, clock=clock, literal_cost=2.5)

    page = reader.search_mail(text="needle", since=SINCE, include_snippets=False)

    assert page["partial"] is True and page["next_cursor"] is None
    assert page["partial_reason"] == "no_resumable_cursor" and "Narrow" in page["hint"]


def test_long_message_with_no_match_in_the_part_read_is_undecided(settings, monkeypatch):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    monkeypatch.setattr(search, "_TEXT_SCAN_MAX_BYTES", 4096)
    reader, _ = mailbox(
        settings,
        recent={
            "early": "needle " + "x" * 8000,
            "late": "x" * 8000 + " needle",
            "short": "nothing",
        },
    )

    result = reader.search_mail(text="needle", since=SINCE, include_snippets=False)

    assert ids(result) == ["<early@example.test>"]
    assert result["partial"] is True and result["partial_reason"] == "text_scan_limited"
    assert result["total_is_exact"] is False
    # Nothing is left to read, and the answer says so rather than looking like a stop.
    assert result["next_cursor"] is None and "Every message" in result["hint"]


def test_long_message_excluded_only_beyond_the_part_read_is_undecided(settings, monkeypatch):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    monkeypatch.setattr(search, "_TEXT_SCAN_MAX_BYTES", 4096)
    reader, _ = mailbox(settings, recent={"late": "x" * 8000 + " needle"})

    result = reader.search_mail(exclude_text="needle", since=SINCE, include_snippets=False)

    assert result["partial"] is True and result["partial_reason"] == "text_scan_limited"


def test_unreadable_message_is_undecided_not_a_silent_miss(settings, large_folder):
    reader, fake = mailbox(settings)
    fake.unreadable = {"11"}

    result = reader.search_mail(text="needle", since=SINCE, include_snippets=False)

    assert ids(result) == ["<hit@example.test>"]
    assert result["partial"] is True and result["partial_reason"] == "text_scan_limited"
    assert result["total_is_exact"] is False


def test_message_gone_since_the_date_search_is_undecided(settings, large_folder):
    reader, fake = mailbox(settings)
    original = fake.uid

    def gone(command, *args):
        if command == "FETCH" and args[0] == "11":
            fake.folders["INBOX"].pop("11", None)
        return original(command, *args)

    fake.uid = gone

    result = reader.search_mail(text="needle", since=SINCE, include_snippets=False)

    assert ids(result) == ["<hit@example.test>"] and result["partial"] is True


def test_stalled_read_ends_as_a_timeout_on_a_closed_stream(settings, large_folder):
    reader, fake = mailbox(settings)
    original = fake.uid

    def stalled(command, *args):
        if command == "FETCH" and args[1].startswith("(BODY.PEEK[]<0."):
            raise TimeoutError
        return original(command, *args)

    fake.uid = stalled

    result = reader.search_mail(text="needle", since=SINCE, include_snippets=False)

    assert result["partial"] is True and result["timed_out"] is True
    assert result["partial_reason"] == "timed_out" and result["messages"] == []
    assert ("LOGOUT",) not in fake.calls


def test_window_scan_over_real_imaplib(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 1)
    sim = BridgeSim()
    try:
        reader = make_client(sim, tmp_path)
        sim.add_many(
            [
                Msg(hdr("<one@example.test>"), body=b"the needle is here\r\n"),
                Msg(hdr("<two@example.test>"), body=b"nothing here\r\n"),
            ]
        )

        result = reader.search_mail(text="needle", since=SINCE, include_snippets=False)

        assert ids(result) == ["<one@example.test>"]
        assert result["partial"] is False
        assert not sim.errors and not sim.violations
    finally:
        sim.close()


def test_parsed_fixture_keeps_its_subject(settings):
    # Guards the fixture above: the raw message holds the headers TEXT also sees.
    assert (
        parsed_headers(body_message("<a@example.test>", "x", subject="Hi").headers)["Subject"]
        == "Hi"
    )


# Following next_cursor always ends: each call moves on, or counts towards the retry limit.
def cursor_state(cursor):
    return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))


def follow(reader, clock, limit=20, max_pages=120, **conditions):
    """Every call gets a fresh budget. A repeated cursor would repeat forever, so it fails here."""
    pages, seen, cursors = [], [], set()
    cursor = None
    while True:
        clock[0] = 100.0
        page = reader.search_mail(
            since=SINCE, include_snippets=False, limit=limit, cursor=cursor, **conditions
        )
        pages.append(page)
        seen.extend(ids(page))
        cursor = page["next_cursor"]
        if cursor is None:
            return pages, seen
        assert cursor not in cursors, "The cursor stopped moving"
        cursors.add(cursor)
        assert len(pages) < max_pages, "The chain did not end"


def text_window(settings, monkeypatch, clock, *, window, matches, cost, first=10):
    """Real window and cursor limits, with a small folder that only counts as large."""
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {f"m{n:04d}": ("needle" if n < matches else "nothing") for n in range(window)}
    reader, fake = mailbox(settings, recent=recent, clock=clock, literal_cost=cost, first=first)
    expected = {f"<m{n:04d}@example.test>" for n in range(matches)}
    return reader, fake, expected


# Window, matches, read cost, limit. Each stopped or looped before the cursor was fixed.
@pytest.mark.parametrize(
    "window,matches,cost,limit",
    [
        (400, 301, 0.06, 20),
        (150, 101, 0.2, 5),
        (18, 6, 2.5, 1),
        (60, 60, 0.3, 20),
        (100, 50, 0, 20),
    ],
)
def test_following_the_cursor_delivers_every_match_once(
    settings, monkeypatch, clock, window, matches, cost, limit
):
    reader, _, expected = text_window(
        settings, monkeypatch, clock, window=window, matches=matches, cost=cost
    )

    pages, seen = follow(reader, clock, limit=limit, text="needle")

    assert sorted(seen) == sorted(expected)
    assert pages[-1]["partial"] is False and pages[-1]["total"] == matches
    assert pages[-1]["total_is_exact"] is True


def test_a_finished_page_does_not_read_delivered_rows_again(settings, monkeypatch, clock):
    reader, fake, _ = text_window(settings, monkeypatch, clock, window=60, matches=60, cost=0.3)

    pages, seen = follow(reader, clock, text="needle")

    assert len(seen) == 60 and len(pages) == 3
    # It was 22 pages and 901 reads when each finished page forgot what it had delivered.
    assert len(fake.literal_reads) < 120


def test_limit_one_over_budget_stops_delivers_each_row_once(settings, monkeypatch, clock):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {f"m{uid}": ("needle" if uid % 2 else "nothing") for uid in range(1, 13)}
    reader, _ = mailbox(settings, recent=recent, clock=clock, literal_cost=2.5)

    pages, seen = follow(reader, clock, limit=1, text="needle")

    assert sorted(seen) == sorted(f"<m{uid}@example.test>" for uid in range(1, 13) if uid % 2)
    assert all(len(page["messages"]) <= 1 for page in pages)


@pytest.mark.parametrize("window,cost", [(400, 0.06), (30, 2.5)])
def test_exclude_text_with_a_rare_value_keeps_moving(settings, monkeypatch, clock, window, cost):
    reader, _, expected = text_window(
        settings, monkeypatch, clock, window=window, matches=window, cost=cost
    )

    pages, seen = follow(reader, clock, limit=10, exclude_text="absent-value")

    assert len(seen) == len(set(seen)) and set(seen) <= expected
    last = pages[-1]
    assert set(seen) == expected or last["partial_reason"] == "no_resumable_cursor"


@pytest.mark.parametrize("cost", [0.06, 2.5])
def test_real_size_uids_end_all_delivered_or_honestly(settings, monkeypatch, clock, cost):
    # Five-digit UIDs fill the cursor far sooner than the fake's small ones.
    reader, _, expected = text_window(
        settings, monkeypatch, clock, window=400, matches=301, cost=cost, first=40000
    )

    pages, seen = follow(reader, clock, text="needle")

    assert sorted(seen) == sorted(expected)
    assert pages[-1]["partial"] is False and pages[-1]["total"] == 301


def test_rows_that_outgrow_the_cursor_still_arrive_when_every_read_fits(
    settings, monkeypatch, clock
):
    # The finished scans drop delivered rows from the cursor once they no longer fit it.
    reader, _, expected = text_window(
        settings, monkeypatch, clock, window=400, matches=301, cost=0, first=40000
    )

    pages, seen = follow(reader, clock, text="needle")

    assert sorted(seen) == sorted(expected)
    assert pages[-1]["partial"] is False and pages[-1]["total"] == 301


def test_a_window_of_any_size_is_read_to_its_end_on_a_small_cursor(settings, monkeypatch, clock):
    # The cursor used to fill after about 230 messages, and 500 was the window's limit.
    reader, fake, expected = text_window(
        settings, monkeypatch, clock, window=1200, matches=40, cost=0.2, first=40000
    )

    pages, seen = follow(reader, clock, text="needle")

    assert sorted(seen) == sorted(expected) and len(pages) > 10
    assert pages[-1]["partial"] is False and pages[-1]["total"] == 40
    assert pages[-1]["total_is_exact"] is True
    assert all(len(page["next_cursor"]) < 1024 for page in pages[:-1])
    # A message ruled out on one page is never read again on a later one.
    misses = [uid for uid in fake.literal_reads if int(uid) >= 40040]
    assert len(misses) == len(set(misses)) == 1160


def test_a_wide_window_with_the_default_snippets_reaches_its_end(settings, monkeypatch, clock):
    reader, _, expected = text_window(
        settings, monkeypatch, clock, window=300, matches=30, cost=0.2, first=40000
    )

    seen, cursor, pages = [], None, 0
    while True:
        clock[0] = 100.0
        page = reader.search_mail(text="needle", since=SINCE, cursor=cursor)
        seen.extend(ids(page))
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert pages < 40

    assert sorted(seen) == sorted(expected) and page["partial"] is False


def test_snippets_that_time_out_on_every_call_do_not_end_the_chain(settings, monkeypatch, clock):
    # Each call delivers rows and then runs out of time, which is not a call with no progress.
    reader, _, expected = text_window(settings, monkeypatch, clock, window=40, matches=40, cost=2.5)

    def no_snippet(*args, **kwargs):
        raise CallDeadlineExceeded("Mailbox call timed out")

    monkeypatch.setattr(reader, "_body", no_snippet)
    seen, cursor, pages = [], None, 0
    while True:
        clock[0] = 100.0
        page = reader.search_mail(text="needle", since=SINCE, cursor=cursor)
        assert page["timed_out"] is True
        seen.extend(ids(page))
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert pages < 40

    assert sorted(seen) == sorted(expected)


def test_dates_against_uid_order_skip_and_repeat_nothing(settings, monkeypatch, clock):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    # Dates run against UID order here, as they do for messages moved into a folder.
    items = {
        str(10 + n): body_message(
            f"<m{n}@example.test>", "needle", date=f"{31 - n % 6} Oct 2026 {n % 24:02d}:00:00 +0000"
        )
        for n in range(30)
    }
    fake = TextBridge({"INBOX": items}, uidnext=True, clock=clock, literal_cost=2.5)
    fake.stall_text = True
    reader = client(settings, fake)

    pages, seen = follow(reader, clock, limit=3, text="needle")

    assert sorted(seen) == sorted(f"<m{n}@example.test>" for n in range(30))
    for page in pages:
        dates = [item["date"] for item in page["messages"]]
        assert dates == sorted(dates, key=parsedate_to_datetime, reverse=True)
        # Every delivered row of a stopped scan is behind the floor, so none stays in the cursor.
        if page.get("partial_reason") == "text_scan_limited":
            assert cursor_state(page["next_cursor"])["emitted"] == []


def test_rows_ruled_out_below_a_waiting_row_may_be_read_again_but_the_chain_ends(
    settings, monkeypatch, clock
):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    # Two hits come first, so the second waits with the misses read after it in the cursor.
    recent = {f"m{n:04d}": ("needle" if n >= 598 else "nothing") for n in range(600)}
    reader, _ = mailbox(settings, recent=recent, clock=clock, literal_cost=0.03, first=40000)

    pages, seen = follow(reader, clock, limit=1, text="needle")

    assert sorted(seen) == ["<m0598@example.test>", "<m0599@example.test>"]
    assert pages[-1]["partial"] is False and pages[-1]["total"] == 2


def slow_date_search(fake, clock, seconds=12.5, calls=None):
    original = fake.uid
    remaining = [calls]

    def uid(command, *args):
        result = original(command, *args)
        if command == "SEARCH" and remaining[0] != 0:
            clock[0] += seconds  # Setup plus a date search near its 10-second limit.
            if remaining[0] is not None:
                remaining[0] -= 1
        return result

    fake.uid = uid


def test_a_stop_with_nothing_read_ends_honestly(settings, monkeypatch, clock):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    # Not even the first read fits what the date search left.
    reader, fake = mailbox(settings, clock=clock, literal_cost=6.0)
    slow_date_search(fake, clock)

    pages, seen = follow(reader, clock, text="needle")

    assert seen == []
    assert len(pages) == search._MAX_CURSOR_RETRIES
    assert [cursor_state(page["next_cursor"])["retries"] for page in pages[:-1]] == [1, 2]
    last = pages[-1]
    assert last["partial_reason"] == "no_resumable_cursor" and last["next_cursor"] is None
    assert "Narrow" in last["hint"] and "reindex" not in last["hint"]


def test_a_call_with_time_for_one_read_resumes_when_time_returns(settings, monkeypatch, clock):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    reader, fake = mailbox(settings, clock=clock)
    slow_date_search(fake, clock, calls=1)

    pages, seen = follow(reader, clock, limit=1, text="needle")

    assert pages[0]["messages"] == [] and pages[0]["partial_reason"] == "text_scan_limited"
    assert seen == ["<hit@example.test>"] and pages[-1]["partial"] is False


def test_a_cursor_from_bridges_search_keeps_moving_when_the_window_path_takes_over(
    settings, monkeypatch, clock
):
    # Rows above the checkpoint were delivered by Bridge's search, and are not in the cursor.
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 1000)
    recent = {f"m{uid:02d}": "needle" for uid in range(20)}
    reader, fake = mailbox(settings, recent=recent, clock=clock, literal_cost=2.5)
    fake.stall_text = False
    first = reader.search_mail(text="needle", since=SINCE, include_snippets=False, limit=8)
    assert cursor_state(first["next_cursor"])["emitted"] == []

    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    cursor, seen, cursors = first["next_cursor"], ids(first), set()
    while cursor is not None:
        clock[0] = 100.0
        page = reader.search_mail(
            text="needle", since=SINCE, include_snippets=False, limit=8, cursor=cursor
        )
        seen.extend(ids(page))
        cursor = page["next_cursor"]
        assert cursor not in cursors
        cursors.add(cursor)

    assert sorted(seen) == sorted(f"<{name}@example.test>" for name in recent)


# A read that stalls ends its stream. What the scan decided survives it.
def stall_reads(fake, *uids):
    original = fake.uid
    stalled = []

    def uid(command, *args):
        if command == "FETCH" and args[1].startswith("(BODY.PEEK[]<0.") and args[0] in uids:
            stalled.append(args[0])
            raise TimeoutError
        return original(command, *args)

    fake.uid = uid
    return stalled


def no_second_connection(monkeypatch, reader):
    session = reader._session
    opened = []

    def one_session(*args, **kwargs):
        opened.append(True)
        if len(opened) == 2:
            raise CallDeadlineExceeded("Mailbox connection timed out")
        return session(*args, **kwargs)

    monkeypatch.setattr(reader, "_session", one_session)


def test_a_stalled_read_keeps_the_rows_already_found(settings, monkeypatch):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {"a_stuck": "nothing", "b_hit": "needle", "c_hit": "needle"}
    reader, fake = mailbox(settings, recent=recent)
    stalled = stall_reads(fake, "10")

    result = reader.search_mail(text="needle", since=SINCE, include_snippets=False)

    assert sorted(ids(result)) == ["<b_hit@example.test>", "<c_hit@example.test>"]
    assert stalled == ["10"] and "timed_out" not in result
    assert result["skipped_uids"] == ["10"]
    # A new connection fetched them, with nothing read twice.
    assert fake.literal_reads == ["12", "11"]
    assert result["partial"] is True and result["partial_reason"] == "text_scan_limited"
    assert result["total_is_exact"] is False and result["next_cursor"] is None


def test_a_message_that_always_stalls_does_not_block_the_window(settings, monkeypatch):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {"a_hit": "needle", "b_stuck": "needle", "c_hit": "needle", "d_miss": "nothing"}
    reader, fake = mailbox(settings, recent=recent)
    stalled = stall_reads(fake, "11")

    result = reader.search_mail(text="needle", since=SINCE, include_snippets=False)

    assert sorted(ids(result)) == ["<a_hit@example.test>", "<c_hit@example.test>"]
    assert stalled == ["11"] and sorted(fake.literal_reads) == ["10", "12", "13"]
    assert result["partial"] is True and result["total_is_exact"] is False


def test_a_stall_stays_reported_on_later_pages(settings, monkeypatch, clock):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {f"m{uid}": "needle" for uid in range(6)}
    reader, fake = mailbox(settings, recent=recent, clock=clock)
    stall_reads(fake, "15")

    pages, seen = follow(reader, clock, limit=2, text="needle")

    assert len(seen) == len(set(seen)) == 5 and "<m5@example.test>" not in seen
    assert all(page["partial"] is True and page["total_is_exact"] is False for page in pages)
    assert pages[-1]["partial_reason"] == "text_scan_limited"


@pytest.mark.parametrize("conditions", [{"text": "needle"}, {"exclude_text": "absent"}])
def test_every_read_stalling_moves_through_the_window_and_ends(
    settings, monkeypatch, clock, conditions
):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {f"m{uid}": "needle" for uid in range(6)}
    reader, fake = mailbox(settings, recent=recent, clock=clock)
    stalled = stall_reads(fake, *(str(10 + uid) for uid in range(6)))

    pages, seen = follow(reader, clock, **conditions)

    # One new connection per call, so a call ends after its second stall. The last finds nothing.
    assert seen == [] and sorted(stalled) == [str(10 + uid) for uid in range(6)]
    assert len(pages) == 4
    assert all(page["timed_out"] is True for page in pages[:-1])
    assert pages[-1]["partial"] is True and pages[-1]["total_is_exact"] is False


# A read the call's own budget cuts short says nothing about the message.
@pytest.mark.parametrize("latency", [0.9, 1.3, 1.7])
def test_a_slow_bridge_delivers_every_message_over_real_imaplib(tmp_path, monkeypatch, latency):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 1)
    clock = SimpleNamespace(elapsed=0.0)
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: 1000.0 + clock.elapsed)
    sim = BridgeSim(clock=clock, command_latency=latency)
    try:
        reader = make_client(sim, tmp_path)
        expected = [f"<m{n:02d}@example.test>" for n in range(30)]
        sim.add_many([Msg(hdr(name), body=b"the needle is here\r\n") for name in expected])
        pages, seen, cursors, cursor = [], [], set(), None
        while True:
            page = reader.search_mail(
                text="needle", since=SINCE, include_snippets=False, cursor=cursor
            )
            pages.append(page)
            seen.extend(ids(page))
            cursor = page["next_cursor"]
            if cursor is None:
                break
            assert cursor not in cursors and len(pages) < 40
            cursors.add(cursor)
    finally:
        sim.close()

    assert sorted(seen) == expected
    assert pages[-1]["partial"] is False and pages[-1]["total_is_exact"] is True
    assert not any("skipped_uids" in page for page in pages)


@pytest.mark.parametrize("cost", [1.3, 1.7, 2.3, 3.1, 4.0])
def test_reads_that_cross_the_budget_edge_skip_nothing(settings, monkeypatch, clock, cost):
    reader, _, expected = text_window(
        settings, monkeypatch, clock, window=60, matches=60, cost=cost
    )

    pages, seen = follow(reader, clock, text="needle")

    assert sorted(seen) == sorted(expected)
    assert pages[-1]["partial"] is False and pages[-1]["total_is_exact"] is True
    assert not any("skipped_uids" in page for page in pages)


def test_a_read_slower_than_the_time_left_is_read_again_next_call(settings, monkeypatch, clock):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {f"m{uid}": "needle" for uid in range(8)}
    reader, fake = mailbox(settings, recent=recent, clock=clock, literal_cost=1.0)
    original = fake.uid
    slow = [True]

    def uid(command, *args):
        result = original(command, *args)
        if command == "FETCH" and args[0] == "12" and slow:
            slow.pop()
            clock[0] += 9.0
        return result

    fake.uid = uid

    pages, seen = follow(reader, clock, text="needle")

    assert sorted(seen) == [f"<m{uid}@example.test>" for uid in range(8)]
    assert fake.literal_reads.count("12") == 2
    assert pages[0]["partial_reason"] == "text_scan_limited" and "skipped_uids" not in pages[0]
    assert pages[-1]["partial"] is False


def test_a_stuck_message_reached_late_is_skipped_by_the_next_call(settings, monkeypatch, clock):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {f"m{uid}": "needle" for uid in range(6)}
    reader, fake = mailbox(settings, recent=recent, clock=clock, literal_cost=1.0)
    original = fake.uid
    stalls = []

    def uid(command, *args):
        if command == "FETCH" and args[1].startswith("(BODY.PEEK[]<0.") and args[0] == "10":
            stalls.append(clock[0])
            clock[0] += min(10.0, 113.0 - clock[0])  # Up to the socket timeout or the budget.
            raise TimeoutError
        return original(command, *args)

    fake.uid = uid

    pages, seen = follow(reader, clock, text="needle")

    # The first call has too little left to blame the message, and keeps what it found.
    assert len(pages[0]["messages"]) == 5 and "skipped_uids" not in pages[0]
    assert pages[1]["skipped_uids"] == ["10"] and len(stalls) == 2
    assert len(seen) == len(set(seen)) == 5 and "<m0@example.test>" not in seen
    assert pages[-1]["partial"] is True and pages[-1]["total_is_exact"] is False


def test_a_message_ruled_out_by_its_header_alone_moves_the_cursor(settings, monkeypatch, clock):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {f"m{uid}": "needle" for uid in range(4)}
    reader, fake = mailbox(settings, recent=recent, clock=clock)
    for item in fake.folders["INBOX"].values():
        item.headers = item.headers.replace(b"Subject: Hello", b"Subject: Other")
    slow_date_search(fake, clock)

    # One read per call, each ruled out by the subject once its header is cached.
    pages, seen = follow(reader, clock, text="needle", subject="Hello")

    assert seen == [] and pages[-1]["partial"] is False
    assert sorted(fake.literal_reads) == ["10", "11", "12", "13"]


def test_a_dropped_connection_skips_nothing(settings, monkeypatch):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    reader, fake = mailbox(settings, recent={"a_hit": "needle", "b_hit": "needle"})
    original = fake.uid
    drop = [True]

    def uid(command, *args):
        if command == "FETCH" and args[1].startswith("(BODY.PEEK[]<0.") and drop:
            drop.pop()
            raise imaplib.IMAP4.abort("connection lost")
        return original(command, *args)

    fake.uid = uid

    result = reader.search_mail(text="needle", since=SINCE, include_snippets=False)

    assert sorted(ids(result)) == ["<a_hit@example.test>", "<b_hit@example.test>"]
    assert "skipped_uids" not in result and result["partial"] is False


def test_a_canonical_lookup_that_keeps_timing_out_ends_without_repeating(
    settings, monkeypatch, clock
):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {f"m{uid:02d}": "needle" for uid in range(40)}
    reader, fake = all_mail_box(settings, recent=recent, clock=clock, literal_cost=0.9)

    def timed_out(*args, **kwargs):
        raise CallDeadlineExceeded("Mailbox call timed out")

    monkeypatch.setattr(canonical, "resolve_views", timed_out)

    # follow() fails on an identical cursor.
    pages, seen = follow(reader, clock, text="needle", subject="Hello", folder="All Mail")

    assert seen == [] and len(pages) == search._MAX_CURSOR_RETRIES
    assert [cursor_state(page["next_cursor"])["retries"] for page in pages[:-1]] == [1, 2]
    last = pages[-1]
    assert last["partial_reason"] == "no_resumable_cursor" and last["next_cursor"] is None
    assert "Narrow" in last["hint"] and "reindex" not in last["hint"]


def test_a_timeout_before_the_scan_starts_gets_the_text_hint(settings, monkeypatch, clock):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    reader, fake = mailbox(settings, clock=clock)
    original = fake.uid

    def uid(command, *args):
        if command == "SEARCH":
            raise TimeoutError
        return original(command, *args)

    fake.uid = uid

    pages, _ = follow(reader, clock, text="needle")

    assert pages[-1]["partial_reason"] == "no_resumable_cursor"
    assert "Narrow" in pages[-1]["hint"] and "reindex" not in pages[-1]["hint"]


def test_a_stall_with_no_time_for_a_new_connection_keeps_what_it_decided(settings, monkeypatch):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {"a_stuck": "needle", "b_hit": "needle", "c_miss": "nothing"}
    reader, fake = mailbox(settings, recent=recent)
    stalled = stall_reads(fake, "10")
    no_second_connection(monkeypatch, reader)

    first = reader.search_mail(text="needle", since=SINCE, include_snippets=False)

    assert ids(first) == ["<b_hit@example.test>"] and first["timed_out"] is True
    assert first["partial_reason"] == "timed_out"
    state = cursor_state(first["next_cursor"])
    # The miss and the stalled read are decided, and the hit is delivered.
    assert state["floor"] == 10 and state["found"] == 1 and state["retries"] == 0
    assert state["rejected"] == [] and state["emitted"] == []
    assert state["undecided"] is True

    fake.literal_reads.clear()
    second = reader.search_mail(
        text="needle", since=SINCE, include_snippets=False, cursor=first["next_cursor"]
    )

    assert second["messages"] == [] and fake.literal_reads == []
    assert stalled == ["10"] and second["next_cursor"] is None
    assert second["partial"] is True and second["total_is_exact"] is False


def test_a_cut_read_with_no_new_connection_returns_its_rows_and_resumes_there(
    settings, monkeypatch, clock
):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {f"m{uid}": ("nothing" if uid == 4 else "needle") for uid in range(6)}
    reader, fake = mailbox(settings, recent=recent, clock=clock)
    slow_date_search(fake, clock, seconds=8.0, calls=1)  # Less than a socket timeout is left.
    original = fake.uid
    cut = []

    def uid(command, *args):
        if (
            command == "FETCH"
            and args[1].startswith("(BODY.PEEK[]<0.")
            and args[0] == "12"
            and not cut
        ):
            cut.append(True)
            raise TimeoutError
        return original(command, *args)

    fake.uid = uid
    no_second_connection(monkeypatch, reader)

    pages, seen = follow(reader, clock, text="needle")

    assert cut and ids(pages[0]) == ["<m5@example.test>", "<m3@example.test>"]
    assert pages[0]["timed_out"] is True and pages[0]["partial_reason"] == "timed_out"
    assert "skipped_uids" not in pages[0]
    # Everything above the cut read is decided, so the next call starts with it.
    state = cursor_state(pages[0]["next_cursor"])
    assert state["floor"] == 13 and state["found"] == 2 and state["retries"] == 0
    assert state["emitted"] == [] and state["rejected"] == []
    assert fake.literal_reads == ["15", "14", "13", "12", "11", "10"]
    assert sorted(seen) == [f"<m{uid}@example.test>" for uid in (0, 1, 2, 3, 5)]
    assert pages[-1]["next_cursor"] is None
    assert pages[-1]["partial"] is False and pages[-1]["total_is_exact"] is True


# Setup costs eight commands, so a cut read leaves no time for a new connection's setup and page.
@pytest.mark.parametrize(
    "latency,extra,slow",
    [(0.5, 3.0, 16), (0.5, 9.0, 16), (0.2, 9.0, 1), (0.7, 6.0, 16), (0.7, 3.0, 1), (1.7, 3.0, 16)],
)
def test_one_slow_message_stops_no_chain_over_real_imaplib(
    tmp_path, monkeypatch, latency, extra, slow
):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 1)
    clock = SimpleNamespace(elapsed=0.0)
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: 1000.0 + clock.elapsed)
    sim = BridgeSim(clock=clock, command_latency=latency)

    def hook(sid, selected, op, shown):
        if "BODY.PEEK[]" in shown and f"FETCH {slow} " in shown:
            clock.elapsed += extra

    sim.hooks.append(hook)
    try:
        reader = make_client(sim, tmp_path, timeout=10)
        expected = [f"<m{n:02d}@example.test>" for n in range(30)]
        sim.add_many([Msg(hdr(name), body=b"the needle is here\r\n") for name in expected])
        pages, seen, cursors, cursor = [], [], set(), None
        while True:
            page = reader.search_mail(
                text="needle", since=SINCE, include_snippets=False, cursor=cursor
            )
            pages.append(page)
            seen.extend(ids(page))
            cursor = page["next_cursor"]
            if cursor is None:
                break
            assert cursor not in cursors and len(pages) < 40
            cursors.add(cursor)
    finally:
        sim.close()

    assert sorted(seen) == expected
    assert pages[-1]["partial"] is False and pages[-1]["total_is_exact"] is True
    assert not any("skipped_uids" in page for page in pages)


def test_a_stall_then_a_lost_page_still_moves_the_cursor(settings, monkeypatch):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {"a_stuck": "needle", "b_hit": "needle", "c_miss": "nothing"}
    reader, fake = mailbox(settings, recent=recent)
    stall_reads(fake, "10")
    metadata = reader._metadata
    stall = []

    def stalled_metadata(*args, **kwargs):
        if fake.literal_reads and not stall:
            stall.append(True)
            raise CallDeadlineExceeded("Mailbox call timed out")
        return metadata(*args, **kwargs)

    monkeypatch.setattr(reader, "_metadata", stalled_metadata)

    page = reader.search_mail(text="needle", since=SINCE, include_snippets=False)

    assert stall and page["timed_out"] is True and page["messages"] == []
    state = cursor_state(page["next_cursor"])
    assert state["floor"] == 10 and state["retries"] == 0 and state["undecided"] is True


def test_a_new_connection_with_another_uidvalidity_uses_nothing_the_scan_decided(
    settings, monkeypatch
):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {"a_stuck": "needle", "b_hit": "needle"}
    reader, fake = mailbox(settings, recent=recent)
    stalled = stall_reads(fake, "10")
    original = search._uidvalidity
    seen = []

    def validity(connection):
        value = original(connection)
        return str(int(value) + 1) if stalled and seen.append(True) is None else value

    monkeypatch.setattr(search, "_uidvalidity", validity)

    # Rows of the old UIDVALIDITY must not be reported as this one's.
    with pytest.raises(MailValidationError, match="identity changed"):
        reader.search_mail(text="needle", since=SINCE, include_snippets=False)


def test_a_page_after_a_retry_keeps_its_full_limit(settings, monkeypatch, clock):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {f"m{uid}": "needle" for uid in range(8)}
    reader, fake = mailbox(settings, recent=recent, clock=clock, literal_cost=2.5)

    first = reader.search_mail(text="needle", since=SINCE, include_snippets=False, limit=3)
    assert len(first["messages"]) == 3

    slow_date_search(fake, clock, seconds=16.0, calls=1)
    clock[0] = 100.0
    idle = reader.search_mail(
        text="needle", since=SINCE, include_snippets=False, limit=3, cursor=first["next_cursor"]
    )
    assert idle["messages"] == [] and cursor_state(idle["next_cursor"])["retries"] == 1

    clock[0] = 100.0
    resumed = reader.search_mail(
        text="needle", since=SINCE, include_snippets=False, limit=3, cursor=idle["next_cursor"]
    )

    assert len(resumed["messages"]) == 3


def test_a_stall_does_not_rule_out_a_row_whose_headers_are_unknown(settings, monkeypatch):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {"a_stuck": "x", "b_hit": "needle", "c_hit": "needle"}
    reader, fake = mailbox(settings, recent=recent)
    stall_reads(fake, "10")
    no_second_connection(monkeypatch, reader)

    page = reader.search_mail(text="needle", subject="Hello", since=SINCE, include_snippets=False)

    # Without cached headers no row can be ruled out yet; only the stalled one is skipped.
    assert ids(page) == ["<c_hit@example.test>", "<b_hit@example.test>"]
    assert cursor_state(page["next_cursor"])["floor"] == 10


def test_a_stall_on_all_mail_keeps_an_explicit_window(settings, monkeypatch):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    reader, fake = all_mail_box(settings)
    stall_reads(fake, "10")

    result = reader.search_mail(
        folder="All Mail", text="needle", since=SINCE, include_snippets=False
    )

    # The old mail outside the dates holds the word too, and must stay out.
    assert [item["alias"]["uid"] for item in result["messages"]] == ["12", "11"]
    assert "applied_since" not in result and result["partial_reason"] == "text_scan_limited"


def test_a_stall_on_the_default_window_still_reports_the_window(settings, monkeypatch):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    monkeypatch.setattr(search, "_default_since", lambda: SINCE)
    reader, fake = all_mail_box(settings)
    stall_reads(fake, "10")

    result = reader.search_mail(folder="All Mail", text="needle", include_snippets=False)

    assert [item["alias"]["uid"] for item in result["messages"]] == ["12", "11"]
    assert result["applied_since"] == SINCE and result["partial"] is True
    assert result["partial_reason"] == "no_resumable_cursor" and result["next_cursor"] is None
    assert "reindex" not in result["hint"]


def test_a_budget_stop_on_the_default_window_still_reports_the_window(settings, monkeypatch, clock):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    monkeypatch.setattr(search, "_default_since", lambda: SINCE)
    recent = {f"m{uid}": "needle" for uid in range(12)}
    reader, _ = all_mail_box(settings, recent=recent, clock=clock, literal_cost=2.5)

    result = reader.search_mail(folder="All Mail", text="needle", include_snippets=False)

    assert result["partial_reason"] == "text_scan_limited" and result["applied_since"] == SINCE
    assert result["next_cursor"] is not None


def test_an_undecided_row_on_an_earlier_page_stays_reported(settings, monkeypatch):
    monkeypatch.setattr(search, "_SERVER_TEXT_MAX_MESSAGES", 5)
    recent = {"p1": "needle", "p2": "needle", "p3": "nothing"}
    reader, fake = mailbox(settings, recent=recent)
    fake.unreadable = {"12"}

    first = reader.search_mail(text="needle", since=SINCE, include_snippets=False, limit=1)
    second = reader.search_mail(
        text="needle", since=SINCE, include_snippets=False, limit=1, cursor=first["next_cursor"]
    )

    assert sorted(ids(first) + ids(second)) == ["<p1@example.test>", "<p2@example.test>"]
    assert second["next_cursor"] is None
    assert second["partial"] is True and second["total_is_exact"] is False
    assert second["partial_reason"] == "text_scan_limited"


@pytest.mark.parametrize("bad", [False, 1, "true", None])
def test_a_cursor_cannot_carry_another_undecided_value(settings, large_folder, bad):
    reader, fake = mailbox(settings, recent={"p1": "needle", "p2": "needle"})
    fake.unreadable = set()
    first = reader.search_mail(text="needle", since=SINCE, include_snippets=False, limit=1)
    state = cursor_state(first["next_cursor"])
    assert "undecided" not in state  # A clean scan's cursor keeps its earlier form.
    forged = base64.urlsafe_b64encode(json.dumps({**state, "undecided": bad}).encode()).decode()

    with pytest.raises(MailValidationError):
        reader.search_mail(
            text="needle", since=SINCE, include_snippets=False, limit=1, cursor=forged.rstrip("=")
        )


@pytest.mark.parametrize(
    "extra",
    [
        {"floor": 5},
        {"found": 1},
        {"floor": 0, "found": 0},
        {"floor": 10**6, "found": 0},
        {"floor": 5, "found": -1},
        {"floor": True, "found": 0},
        {"floor": 5, "found": "1"},
    ],
)
def test_a_cursor_cannot_carry_a_bad_scan_position(settings, large_folder, extra):
    reader, _ = mailbox(settings, recent={"a": "needle", "b": "needle"})
    first = reader.search_mail(text="needle", since=SINCE, include_snippets=False, limit=1)
    state = cursor_state(first["next_cursor"])
    forged = base64.urlsafe_b64encode(json.dumps({**state, **extra}).encode()).decode()

    with pytest.raises(MailValidationError):
        reader.search_mail(
            text="needle", since=SINCE, include_snippets=False, limit=1, cursor=forged.rstrip("=")
        )


# count_mail always sends Bridge one TEXT search, so narrower dates cannot help it.
def test_count_mail_text_gives_no_advice_about_dates():
    def description(annotation):
        return annotation.__metadata__[0].description

    assert "one by one" in description(tool_docs.TextFilter)
    count = description(tool_docs.CountTextFilter)
    assert "one by one" not in count and "whatever the dates" in count


def test_the_tools_describe_text_each_as_they_answer_it(settings):
    mcp = create_mcp(
        replace(
            Settings(token="t" * 32, bridge_user="reader@example.test"),
            action_log_path=settings.action_log_path,
            identity_index_enabled=False,
        ),
        SimpleNamespace(),
    )
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    def text(name):
        return str(tools[name].inputSchema["properties"]["text"])

    assert "one by one" in text("search_mail") and "one by one" not in text("count_mail")


@pytest.mark.parametrize(
    "conditions", [{"text": "needle"}, {"from_": "someone"}, {"query": {"text": "needle"}}]
)
def test_count_timeout_with_a_condition_points_to_search_mail(settings, large_folder, conditions):
    reader, fake = mailbox(settings)
    original = fake.uid

    def stalled(command, *args):
        if command == "SEARCH":
            raise TimeoutError
        return original(command, *args)

    fake.uid = stalled

    result = reader.count_mail(since=SINCE, **conditions)

    assert result["timed_out"] is True and result["count"] == 0
    assert "search_mail" in result["error"] and "narrow the date" not in result["error"]


def test_count_timeout_on_dates_alone_can_still_narrow_them(settings, large_folder):
    reader, fake = mailbox(settings)
    original = fake.uid

    def stalled(command, *args):
        if command == "SEARCH":
            raise TimeoutError
        return original(command, *args)

    fake.uid = stalled

    result = reader.count_mail(since=SINCE)

    assert result["timed_out"] is True and "narrow the date range" in result["error"]
