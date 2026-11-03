"""Offline regressions for prompt, bounded reads of newsletters and drafts."""

import time
from dataclasses import replace
from threading import Event, Thread

import pytest
from test_mail import ATTACHMENT, HTML, PLAIN, client, message
from test_mail_safety import ReadOnlyImap

from proton_mail_mcp.config import Settings
from proton_mail_mcp.threading import MAX_THREAD_BODY_BYTES, MAX_THREAD_HEADER_BYTES


class ForbiddenIndex:
    """Only the bounded seed hint may touch the cache during a direct read."""

    def __init__(self):
        self.calls = []

    def seed_uid(self, folder, validity, message_id):
        return None

    def coverage(self, folder):
        return None

    def coverage_epoch(self, folder):
        return None

    def thread_uids(self, folder, validity, state, identifiers):
        return None

    def reporting_count(self, folder, validity):
        return None

    def __getattr__(self, name):
        def forbidden(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"read_thread touched the optional index: {name}")

        return forbidden


@pytest.fixture
def settings():
    return Settings(
        token="a-test-token-that-is-at-least-32-characters", bridge_user="reader@example.com"
    )


@pytest.mark.parametrize("kind", ["newsletter", "draft"])
def test_direct_read_skips_index_maintenance_and_thread_work(settings, kind):
    identifier = f"<{kind}@example.com>"
    if kind == "newsletter":
        payload = b"<p>Newsletter " + b"x" * (MAX_THREAD_BODY_BYTES + 1000) + b"</p>"
        structure = (
            b"("
            + HTML.replace(b"90 1", str(len(payload)).encode() + b" 1")
            + b" "
            + ATTACHMENT.replace(b"400", b"100000000")
            + b' "MIXED" NIL NIL NIL)'
        )
        folder, uid, flags = "INBOX", "7", set()
    else:
        payload, structure = b"Saved draft body", PLAIN
        folder, uid, flags = "Drafts", "144", {r"\Draft", r"\Seen"}
    requested = message(
        identifier, structure=structure, sections={"1": payload}, flags=flags.copy()
    )
    fake = ReadOnlyImap(
        {
            folder: {uid: requested},
            "All Mail": {
                "501": requested,
                "502": message("<related@example.com>", reply=identifier),
            },
        },
        uidnext=True,
    )
    mailbox = client(settings, fake)
    index = mailbox.search_index = ForbiddenIndex()

    result = mailbox.read_thread(identifier, include_thread=False)

    assert result["partial"] is False
    [item] = result["messages"]
    assert item["message_id"] == identifier
    assert (item["folder"], item["uidvalidity"], item["uid"]) == (None, None, None)
    assert item["alias"] == {"folder": "All Mail", "uidvalidity": "1", "uid": "501"}
    assert "timed_out" not in result
    assert item["text"].startswith("Newsletter" if kind == "newsletter" else "Saved draft body")
    assert item["body_fetched_bytes"] <= MAX_THREAD_BODY_BYTES
    assert item["truncated"] is (kind == "newsletter")
    if kind == "newsletter":
        assert item["attachments"][0]["size"] == 100000000
    assert requested.flags == flags
    assert index.calls == []
    header_fetches = [
        call for call in fake.calls if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]
    ]
    assert len(header_fetches) == 1
    assert "BODYSTRUCTURE" not in header_fetches[0][2]
    assert f"]<0.{MAX_THREAD_HEADER_BYTES}>" in header_fetches[0][2]
    assert [
        call[1] for call in fake.calls if call[0] == "FETCH" and call[2] == "(BODYSTRUCTURE)"
    ] == ["501"]
    header_searches = [call for call in fake.calls if call[0] == "SEARCH" and "HEADER" in call]
    assert len(header_searches) == 1
    assert header_searches[0][3:5] == ("HEADER", "Message-ID")
    assert not any(call[0] == "SEARCH" and "ON" in call for call in fake.calls)
    # The read-only fake rejects non-PEEK sections and any writable SELECT or STORE.
    assert fake.appended == []


@pytest.mark.parametrize("include_thread", [False, True])
@pytest.mark.parametrize("thread_seconds,canonical_seconds", [(2.0, 0.2)])
def test_all_mail_day_scan_cannot_delay_completed_body(
    settings, monkeypatch, include_thread, thread_seconds, canonical_seconds
):
    monkeypatch.setattr("proton_mail_mcp.threading.MAX_THREAD_SECONDS", thread_seconds)
    monkeypatch.setattr("proton_mail_mcp.threading.MAX_CANONICAL_SECONDS", canonical_seconds)
    identifier = "<example-org/example-repo/pull/49@github.com>"
    payload = b"x" * 46139
    saved = message(
        identifier,
        structure=PLAIN.replace(b"24 2", str(len(payload)).encode() + b" 1"),
        sections={"1": payload},
    )
    searching, release, cleaned_up = Event(), Event(), Event()
    issued = []

    class StalledAllMailImap(ReadOnlyImap):
        def uid(self, command, *args):
            issued.append(time.monotonic())
            if command == "SEARCH" and self.current == "All Mail" and "ON" in args:
                searching.set()
                # Simulate a Bridge call that no socket watchdog can interrupt.
                release.wait(5)
            return super().uid(command, *args)

        def logout(self):
            result = super().logout()
            cleaned_up.set()
            return result

        def shutdown(self):
            self.calls.append(("SHUTDOWN",))
            cleaned_up.set()

    fake = StalledAllMailImap(
        {"INBOX": {"20431": saved}, "All Mail": {"47520": saved}}, uidnext=True
    )
    fresh = ReadOnlyImap({"INBOX": {"20431": saved}}, uidnext=True)
    mailbox = client(settings, fake)
    mailbox.search_index = ForbiddenIndex()
    sessions = iter([fake, fresh])
    mailbox.imap_factory = lambda *args, **kwargs: next(sessions)

    try:
        result = mailbox.read_thread(identifier, include_thread=include_thread)
        # The canonical cap starts once the body is read, so count from the last command: the
        # stalled day scan when there is one. A loaded host's setup and body read are not the scan.
        held = time.monotonic() - issued[-1]
        assert held < 1.25, "Canonical lookup must remain bounded"
        assert searching.is_set() is include_thread
        assert not release.is_set()
        assert "timed_out" not in result and "error" not in result
        assert result["partial"] is False
        assert "partial_reason" not in result
        if not include_thread:
            assert not any(call[0] == "SEARCH" and "ON" in call for call in fake.calls)
        [item] = result["messages"]
        assert item["message_id"] == identifier
        assert (item["folder"], item["uidvalidity"], item["uid"]) == (None, None, None)
        assert item["alias"] == {"folder": "All Mail", "uidvalidity": "1", "uid": "47520"}
        assert item["text"] == payload.decode()
        assert item["body_fetched_bytes"] == len(payload)
        assert item["truncated"] is False
    finally:
        release.set()
        cleaned_up.wait(1)
    assert cleaned_up.is_set(), "The abandoned session must finish cleanup after release"

    next_result = mailbox.read_thread(identifier, include_thread=False)
    assert next_result["partial"] is False
    assert next_result["messages"][0]["alias"]["uid"] == "20431"
    assert next_result["messages"][0]["text"] == payload.decode()
    assert fresh.calls[-1] == ("LOGOUT",)
    assert mailbox.search_index.calls == []


@pytest.mark.parametrize("lookup_seconds", [8.0, 10.5])
def test_slow_seed_fallback_completes_and_requested_body_keeps_thread_budget(
    settings, monkeypatch, lookup_seconds
):
    clock = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: clock[0])
    saved = message("<seed@example.com>")
    fake = ReadOnlyImap({"INBOX": {"7": saved}, "All Mail": {"501": saved}}, uidnext=True)
    mailbox = client(settings, fake)
    original_search, original_body = mailbox._search, mailbox._body
    seed_deadlines = []

    def slow_seed_search(connection, criteria, *args, **kwargs):
        result = original_search(connection, criteria, *args, **kwargs)
        if "HEADER" in criteria and "OR" not in criteria:
            seed_deadlines.append(connection.deadline.end)
            clock[0] += lookup_seconds
        return result

    def requested_body(connection, uid, **kwargs):
        seed_deadlines.append(kwargs["deadline"].end)
        return original_body(connection, uid, **kwargs)

    monkeypatch.setattr(mailbox, "_search", slow_seed_search)
    monkeypatch.setattr(mailbox, "_body", requested_body)

    result = mailbox.read_thread("<seed@example.com>")

    assert seed_deadlines == [112.0, 115.0]
    assert result["partial"] is False
    assert "timed_out" not in result and "error" not in result
    [item] = result["messages"]
    assert item["message_id"] == "<seed@example.com>"
    assert item["text"] == "Hello plain text" and item["truncated"] is False


@pytest.mark.parametrize("command_seconds", [0.5, 30.0])
def test_seed_command_watchdog_uses_lookup_budget_and_preserves_shorter_settings(
    settings, monkeypatch, command_seconds
):
    clock = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: clock[0])
    saved = message("<seed@example.com>")
    fake = ReadOnlyImap({"All Mail": {"501": saved}}, uidnext=True)
    mailbox = client(replace(settings, timeout=command_seconds), fake)
    original_search, original_body = mailbox._search, mailbox._body
    observed = []

    def seed_search(connection, criteria, *args, **kwargs):
        observed.append(connection.deadline.check())
        return original_search(connection, criteria, *args, **kwargs)

    def requested_body(connection, uid, **kwargs):
        observed.append(connection.deadline.check())
        return original_body(connection, uid, **kwargs)

    monkeypatch.setattr(mailbox, "_search", seed_search)
    monkeypatch.setattr(mailbox, "_body", requested_body)

    result = mailbox.read_thread("<seed@example.com>", include_thread=False)

    assert observed == pytest.approx([min(command_seconds, 10.8), min(command_seconds, 10.0)])
    assert result["partial"] is False
    assert result["messages"][0]["text"] == "Hello plain text"


def test_missing_seed_lookup_limit_reports_partial_without_content_timeout(settings, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: clock[0])
    fake = ReadOnlyImap({"INBOX": {"201": message("<other@example.com>")}}, uidnext=True)
    mailbox = client(settings, fake)
    original_search = mailbox._search
    seed_deadlines = []

    def slow_seed_search(connection, criteria, *args, **kwargs):
        result = original_search(connection, criteria, *args, **kwargs)
        seed_deadlines.append(connection.deadline.end)
        clock[0] = connection.deadline.io_end
        return result

    monkeypatch.setattr(mailbox, "_search", slow_seed_search)

    result = mailbox.read_thread("<missing@example.com>")

    assert seed_deadlines == [112.0]
    assert set(result) == {"messages", "partial", "partial_reason", "index_note", "index_status"}
    assert "0/1 messages indexed (0%)" in result["index_note"]
    assert result["index_status"]["state"] == "incomplete"
    assert result["messages"] == [] and result["partial"] is True
    assert "Search index" in result["partial_reason"]
    assert "lookup incomplete" in result["partial_reason"]


@pytest.mark.parametrize("include_thread", [False, True])
def test_cold_index_stalled_lookup_returns_partial_and_next_read_survives(
    settings, monkeypatch, include_thread
):
    monkeypatch.setattr("proton_mail_mcp.threading.MAX_SEED_LOOKUP_SECONDS", 0.1)
    searching, release, cleaned_up = Event(), Event(), Event()
    seed = message("<old@example.com>", flags={r"\Seen"})
    folders = {
        "INBOX": {
            "20431": seed,
            "47744": message("<newest@example.com>"),
        }
    }

    class StalledLookupImap(ReadOnlyImap):
        def uid(self, command, *args):
            if command == "SEARCH" and "HEADER" in args:
                searching.set()
                release.wait(6)
            return super().uid(command, *args)

        def shutdown(self):
            self.calls.append(("SHUTDOWN",))
            cleaned_up.set()

    stalled, fresh = StalledLookupImap(folders, uidnext=True), ReadOnlyImap(folders, uidnext=True)
    mailbox = client(settings, fresh)
    mailbox.search_index = ForbiddenIndex()
    sessions = iter([stalled, fresh])
    mailbox.imap_factory = lambda *args, **kwargs: next(sessions)
    started = time.monotonic()
    try:
        result = mailbox.read_thread("<old@example.com>", include_thread=include_thread)
        elapsed = time.monotonic() - started
        assert searching.is_set() and not release.is_set()
        assert elapsed < 0.75, "A cold index must not consume the full thread timeout"
        assert result["messages"] == [] and result["partial"] is True
        assert "lookup incomplete" in result["partial_reason"]
        assert "timed_out" not in result and "error" not in result
    finally:
        release.set()
        assert cleaned_up.wait(1)

    result = mailbox.read_thread("<old@example.com>", include_thread=False)
    assert result["partial"] is False
    [item] = result["messages"]
    assert item["alias"]["uid"] == "20431" and item["text"] == "Hello plain text"
    assert seed.flags == {r"\Seen"}
    assert mailbox.search_index.calls == []
    assert fresh.calls[-1] == ("LOGOUT",)


def test_cold_index_fallback_folders_share_one_lookup_budget(settings, monkeypatch):
    monkeypatch.setattr("proton_mail_mcp.threading.MAX_SEED_LOOKUP_SECONDS", 3.0)
    clock = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: clock[0])

    class SlowLookupImap(ReadOnlyImap):
        def uid(self, command, *args):
            if command == "SEARCH":
                clock[0] += 1.0
            return super().uid(command, *args)

        def shutdown(self):
            self.calls.append(("SHUTDOWN",))

    fake = SlowLookupImap(
        {name: {"5000": message("<other@example.com>")} for name in ("INBOX", "A", "B", "C")},
        uidnext=True,
    )
    mailbox = client(settings, fake)
    mailbox.search_index = ForbiddenIndex()

    result = mailbox.read_thread("<missing@example.com>")

    assert clock[0] == 103.0
    assert len([call for call in fake.calls if call[0] == "SEARCH"]) == 3
    assert result["messages"] == [] and result["partial"] is True
    assert "lookup incomplete" in result["partial_reason"]
    assert "timed_out" not in result and "error" not in result
    assert fake.calls[-1] == ("SHUTDOWN",)


@pytest.mark.parametrize("setup", ["select", "status"])
def test_fallback_folder_setup_shares_the_seed_lookup_budget(settings, monkeypatch, setup):
    clock = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: clock[0])
    fake = ReadOnlyImap(
        {name: {"201": message("<other@example.com>")} for name in ("INBOX", "A")},
        uidnext=setup != "status",
    )
    mailbox = client(settings, fake)
    observed = []

    def expire(connection):
        observed.append(connection.deadline.end)
        clock[0] = connection.deadline.io_end
        connection.deadline.check(connection)

    if setup == "select":
        original_select = mailbox._select
        selected = []

        def slow_reselect(connection, folder):
            if folder in selected:
                expire(connection)
            selected.append(folder)
            return original_select(connection, folder)

        monkeypatch.setattr(mailbox, "_select", slow_reselect)
    else:
        monkeypatch.setattr(
            "proton_mail_mcp.search._coverage_status", lambda connection, folder: expire(connection)
        )

    result = mailbox.read_thread("<missing@example.com>", include_thread=False)

    assert observed == [112.0]
    assert result["messages"] == [] and result["partial"] is True
    assert "lookup incomplete" in result["partial_reason"]
    assert "timed_out" not in result and "error" not in result
    assert not [call for call in fake.calls if call[0] == "SEARCH"]


def test_seed_fallback_obeys_shorter_configured_call_budget(settings, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: clock[0])
    fake = ReadOnlyImap({"INBOX": {"201": message("<other@example.com>")}}, uidnext=True)
    mailbox = client(replace(settings, call_timeout=2.0), fake)
    observed = []

    def slow_search(connection, criteria, *args, **kwargs):
        observed.append(connection.deadline.end)
        clock[0] = connection.deadline.io_end
        return []

    monkeypatch.setattr(mailbox, "_search", slow_search)

    result = mailbox.read_thread("<missing@example.com>", include_thread=False)

    assert observed == [102.0]
    assert result["partial"] is True and result["messages"] == []
    assert "lookup incomplete" in result["partial_reason"]
    assert "timed_out" not in result and "error" not in result


def test_a_read_stopped_before_expansion_is_still_reported_as_incomplete(settings, monkeypatch):
    # The first lookup comes before the relatives are read, so it is not only about handles.
    monkeypatch.setattr("proton_mail_mcp.threading.MAX_THREAD_SECONDS", 0.5)
    reached, release = Event(), Event()

    def stall(*args, **kwargs):
        reached.set()
        release.wait(5)

    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_indexed_views", stall)
    saved = message("<seed@example.com>")
    fake = ReadOnlyImap({"INBOX": {"7": saved}, "All Mail": {"70": saved}}, uidnext=True)
    try:
        result = client(settings, fake).read_thread("<seed@example.com>")
    finally:
        release.set()

    assert reached.is_set()
    assert result["partial"] is True and result["timed_out"] is True


@pytest.mark.parametrize("call_timeout", [2.0, 120.0])
def test_canonical_limit_applies_even_when_thread_budget_is_shorter(
    settings, monkeypatch, call_timeout
):
    clock = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: clock[0])
    saved = message("<seed@example.com>")
    fake = ReadOnlyImap({"INBOX": {"7": saved}, "All Mail": {"501": saved}}, uidnext=True)
    mailbox = client(replace(settings, call_timeout=call_timeout), fake)
    observed = []

    def stalled_lookup(client, connection, deadline, *args, **kwargs):
        observed.append((deadline.end, deadline.response_reserve))
        clock[0] = deadline.io_end
        deadline.check(connection)

    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_views", stalled_lookup)

    result = mailbox.read_thread("<seed@example.com>")

    assert observed == [
        pytest.approx((min(100.0 + call_timeout, 103.0), min(call_timeout, 3) * 0.1))
    ]
    assert result["partial"] is False and "partial_reason" not in result
    assert "timed_out" not in result and "error" not in result
    [item] = result["messages"]
    assert item["text"] == "Hello plain text" and item["truncated"] is False
    assert (item["folder"], item["uidvalidity"], item["uid"]) == (None, None, None)


@pytest.mark.parametrize("limited_phase", ["expand", "bodies", "bodies_cut"])
@pytest.mark.parametrize("canonical_times_out", [False, True])
def test_expansion_and_bodies_leave_time_for_canonical_lookup(
    settings, monkeypatch, limited_phase, canonical_times_out
):
    clock = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: clock[0])
    saved = message("<seed@example.com>")

    class RecordedImap(ReadOnlyImap):
        def shutdown(self):
            self.calls.append(("SHUTDOWN",))

    fake = RecordedImap(
        {
            "INBOX": {"7": saved},
            "All Mail": {
                "501": saved,
                "502": message("<reply@example.com>", reply="<seed@example.com>"),
                "503": message("<later@example.com>", reply="<seed@example.com>"),
            },
        },
        uidnext=True,
    )
    mailbox = client(settings, fake)
    fresh = ReadOnlyImap(fake.folders, uidnext=True)
    if limited_phase == "bodies_cut":
        sessions = iter([fake, fresh])
        mailbox.imap_factory = lambda *args, **kwargs: next(sessions)
    original_search, original_body = mailbox._search, mailbox._body
    expansion_deadline = []
    bodies, canonical = [], []

    def slow_search(connection, criteria, *args, **kwargs):
        result = original_search(connection, criteria, *args, **kwargs)
        if "OR" in criteria and limited_phase == "expand":
            expansion_deadline.append(connection.deadline.end)
            clock[0] = connection.deadline.io_end
        return result

    def slow_body(connection, uid, **kwargs):
        result = original_body(connection, uid, **kwargs)
        bodies.append(uid)
        if uid == "502" and limited_phase in {"bodies", "bodies_cut"}:
            expansion_deadline.append(kwargs["deadline"].end)
            clock[0] = kwargs["deadline"].io_end
            if limited_phase == "bodies_cut":
                # _body also checks its deadline between commands, outside a
                # command guard. That exception still requires a fresh session.
                kwargs["deadline"].check(connection)
        return result

    def resolve(client, connection, deadline, handles, **kwargs):
        canonical.append((clock[0], deadline.end))
        assert connection.connection is (fresh if limited_phase == "bodies_cut" else fake)
        if limited_phase == "bodies_cut":
            assert fake.calls[-1] == ("SHUTDOWN",)
        if canonical_times_out:
            clock[0] = deadline.io_end
            deadline.check(connection)
        return [{"folder": "INBOX", "uidvalidity": "1", "uid": "7"}] + [None] * (len(handles) - 1)

    monkeypatch.setattr(mailbox, "_search", slow_search)
    monkeypatch.setattr(mailbox, "_body", slow_body)
    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_views", resolve)

    result = mailbox.read_thread("<seed@example.com>")

    assert expansion_deadline == [112.0]
    assert canonical == [(110.5, 113.5)], "Canonical lookup must retain its full 3-second budget"
    assert bodies == (["501"] if limited_phase == "expand" else ["501", "502"])
    assert result["partial"] is True
    assert result["partial_reason"] == "thread expansion limited"
    if limited_phase == "bodies_cut":
        assert result["timed_out"] is True and "Thread read timed out" in result["error"]
        reply = next(
            item for item in result["messages"] if item["message_id"] == "<reply@example.com>"
        )
        assert reply["text"] == "Hello plain text" and reply["truncated"] is True
    else:
        assert "timed_out" not in result and "error" not in result
    seed = next(item for item in result["messages"] if item["message_id"] == "<seed@example.com>")
    assert seed["text"] == "Hello plain text"
    assert seed["folder"] == (None if canonical_times_out else "INBOX")
    if limited_phase in {"bodies", "bodies_cut"}:
        later = next(
            item for item in result["messages"] if item["message_id"] == "<later@example.com>"
        )
        assert later["text"] == "" and later["truncated"] is True


def test_phase_timings_are_debug_only_and_exclude_message_content(settings, monkeypatch, caplog):
    clock = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: clock[0])
    saved = message("<seed@example.com>")
    fake = ReadOnlyImap({"INBOX": {"7": saved}, "All Mail": {"501": saved}}, uidnext=True)
    mailbox = client(settings, fake)
    original_search, original_body = mailbox._search, mailbox._body

    def search(connection, criteria, *args, **kwargs):
        result = original_search(connection, criteria, *args, **kwargs)
        clock[0] += 2.0 if "OR" in criteria else 1.0
        return result

    def body(*args, **kwargs):
        result = original_body(*args, **kwargs)
        clock[0] += 3.0
        return result

    def canonical(*args, **kwargs):
        clock[0] += 0.5
        return [None]

    monkeypatch.setattr(mailbox, "_search", search)
    monkeypatch.setattr(mailbox, "_body", body)
    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_views", canonical)
    with caplog.at_level("DEBUG", logger="proton_mail_mcp.threading"):
        mailbox.read_thread("<seed@example.com>")

    [record] = [record for record in caplog.records if record.name == "proton_mail_mcp.threading"]
    assert record.levelname == "DEBUG"
    assert record.getMessage() == (
        "read_thread timing seed=1.000s expand=2.000s bodies=3.000s canonical=0.500s"
    )


def test_expansion_fetches_structures_only_for_retained_messages(settings, monkeypatch):
    monkeypatch.setattr("proton_mail_mcp.mail.MAX_THREAD_MESSAGES", 3)
    fake = ReadOnlyImap(
        {
            "INBOX": {
                "1": message("<seed@example.com>"),
                **{
                    str(uid): message(f"<reply-{uid}@example.com>", reply="<seed@example.com>")
                    for uid in range(2, 9)
                },
            }
        },
        uidnext=True,
    )
    mailbox = client(settings, fake)
    index = mailbox.search_index = ForbiddenIndex()

    result = mailbox.read_thread("<seed@example.com>")

    assert result["partial"] is True
    assert {item["uid"] for item in result["messages"]} == {"1", "2", "3"}
    assert all(item["text"] == "Hello plain text" for item in result["messages"])
    structures = [
        call[1] for call in fake.calls if call[0] == "FETCH" and "BODYSTRUCTURE" in call[2]
    ]
    assert structures == ["1", "2", "3"]
    assert index.calls == []


def test_read_does_not_wait_for_background_index_lock(settings, tmp_path):
    settings = replace(settings, action_log_path=str(tmp_path / "actions.sqlite3"))
    fake = ReadOnlyImap(
        {"Drafts": {"144": message("<draft@example.com>", flags={r"\Draft"})}}, uidnext=True
    )
    mailbox = client(settings, fake)
    finished = Event()
    results, errors = [], []

    def read():
        try:
            results.append(mailbox.read_thread("<draft@example.com>", include_thread=False))
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    worker = Thread(target=read, daemon=True)
    mailbox.search_index._coverage_lock.acquire()
    try:
        worker.start()
        assert finished.wait(1), "A cache lock must not escape the IMAP deadline"
    finally:
        mailbox.search_index._coverage_lock.release()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors == []
    assert results[0]["partial"] is False
    assert results[0]["messages"][0]["alias"]["uid"] == "144"


@pytest.mark.parametrize("call_timeout,bound", [(2.0, 2.0), (120.0, 15.0)])
@pytest.mark.parametrize("include_thread", [False, True])
def test_structure_deadline_preserves_seed_and_next_read_survives(
    settings, monkeypatch, call_timeout, bound, include_thread
):
    clock = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: clock[0])
    settings = replace(settings, call_timeout=call_timeout)
    saved = message("<draft@example.com>", subject="Saved draft", flags={r"\Draft"})

    class SlowStructureImap(ReadOnlyImap):
        def uid(self, command, *args):
            result = super().uid(command, *args)
            if command == "FETCH" and "BODYSTRUCTURE" in args[1]:
                # Finish this command at the deadline; the timed connection must
                # stop here and preserve headers collected before MIME work.
                clock[0] += bound
            return result

        def shutdown(self):
            self.calls.append(("SHUTDOWN",))

    slow = SlowStructureImap({"Drafts": {"144": saved}})
    fresh = ReadOnlyImap({"Drafts": {"144": saved}})
    mailbox = client(settings, fresh)
    sessions = iter([slow, fresh])
    waits = []

    def factory(host, port, *, timeout):
        waits.append(timeout)
        return next(sessions)

    mailbox.imap_factory = factory

    result = mailbox.read_thread("<draft@example.com>", include_thread=include_thread)

    assert clock[0] - 100 == bound
    assert 0 < waits[0] <= bound
    assert result["partial"] is True
    assert result["partial_reason"] == result["error"]
    assert result["timed_out"] is True and "Thread read timed out" in result["partial_reason"]
    [item] = result["messages"]
    assert (
        (item["uid"] if include_thread else item["alias"]["uid"]),
        item["subject"],
        item["message_id"],
    ) == (
        "144",
        "Saved draft",
        "<draft@example.com>",
    )
    assert item["truncated"] is True and item["text"] == ""
    assert item["headers"]["Subject"] == "Saved draft"
    assert item["size_bytes"] > 0 and item["body_fetched_bytes"] == 0
    structure_position = next(
        position
        for position, call in enumerate(slow.calls)
        if call[0] == "FETCH" and "BODYSTRUCTURE" in call[2]
    )
    assert slow.calls[structure_position + 1 :] == [("SHUTDOWN",)]

    next_result = mailbox.read_thread("<draft@example.com>", include_thread=False)
    assert next_result["partial"] is False
    assert next_result["messages"][0]["text"] == "Hello plain text"
    assert saved.flags == {r"\Draft"}
    assert fresh.calls[-1] == ("LOGOUT",)


def test_relatives_search_that_never_returns_cannot_hold_the_thread_reply(settings, caplog):
    # CoS uid 20432: a GitHub PR notification ran ~60 s to MCP -32001 with no
    # reply. Its body is read before relatives are searched. A relatives search
    # that no socket watchdog can end must not hold the reply past the budget.
    root = "<example-org/example-repo/pull/49@github.com>"
    seed = "<example-org/example-repo/pull/49/review/3412@github.com>"
    caplog.set_level("DEBUG", logger="proton_mail_mcp.threading")
    searching, release = Event(), Event()

    class StalledRelativesImap(ReadOnlyImap):
        def uid(self, command, *args):
            if command == "SEARCH" and "OR" in args:
                searching.set()
                release.wait(6)
            return super().uid(command, *args)

    fake = StalledRelativesImap(
        {"INBOX": {"20432": message(seed, references=root, reply=root)}}, uidnext=True
    )
    mailbox = client(replace(settings, call_timeout=1.0), fake)
    mailbox.search_index = ForbiddenIndex()
    started = time.monotonic()
    try:
        result = mailbox.read_thread(seed)
        elapsed = time.monotonic() - started
        assert searching.is_set() and not release.is_set()
    finally:
        release.set()

    assert elapsed < 1.1, "Expansion must stop before the reserved canonical budget"
    assert result["partial"] is True
    assert result["partial_reason"] == "thread expansion limited"
    assert result["timed_out"] is True and "Thread read timed out" in result["error"]
    [item] = result["messages"]
    assert (item["message_id"], item["folder"], item["uid"]) == (seed, "INBOX", "20432")
    assert item["text"] == "Hello plain text" and item["truncated"] is False
    [timing] = [record for record in caplog.records if record.name == "proton_mail_mcp.threading"]
    assert timing.getMessage().endswith("canonical=0.000s")

    # A step that returns within the cleanup grace was still late. Here the
    # session logout waits on a stream no command watchdog sees until the reply
    # shuts it, and cleanup swallows that cut: the read itself reports no timeout.
    closed = Event()

    class Stream:
        def shutdown(self, how):
            closed.set()

    class LateLogoutImap(ReadOnlyImap):
        def logout(self):
            self.sock = Stream()
            closed.wait(6)
            return super().logout()

    fake = LateLogoutImap(
        {"INBOX": {"20432": message(seed, references=root, reply=root)}}, uidnext=True
    )
    mailbox = client(replace(settings, call_timeout=1.0), fake)
    mailbox.search_index = ForbiddenIndex()
    result = mailbox.read_thread(seed)

    assert closed.is_set() and fake.calls[-1] == ("LOGOUT",)
    assert result["partial"] is True
    assert result["partial_reason"] == result["error"]
    assert result["timed_out"] is True and "Thread read timed out" in result["error"]
    [item] = result["messages"]
    assert item["text"] == "Hello plain text" and item["truncated"] is False


def test_completed_canonical_lookup_does_not_label_a_cleanup_timeout(settings, monkeypatch):
    closed, resolved = Event(), Event()
    saved = message("<seed@example.com>")

    class Stream:
        def shutdown(self, how):
            closed.set()

    class LateLogoutImap(ReadOnlyImap):
        def logout(self):
            assert resolved.is_set()
            self.sock = Stream()
            closed.wait(6)
            return super().logout()

    fake = LateLogoutImap({"INBOX": {"7": saved}, "All Mail": {"501": saved}}, uidnext=True)
    mailbox = client(replace(settings, call_timeout=1.0), fake)

    def resolve(*args, **kwargs):
        resolved.set()
        return [{"folder": "INBOX", "uidvalidity": "1", "uid": "7"}]

    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_views", resolve)

    result = mailbox.read_thread("<seed@example.com>")

    assert closed.is_set() and fake.calls[-1] == ("LOGOUT",)
    assert result["partial"] is True and result["timed_out"] is True
    assert result["partial_reason"] == result["error"]
    assert result["partial_reason"].startswith("Thread read timed out")
    [item] = result["messages"]
    assert (item["folder"], item["uid"]) == ("INBOX", "7")
    assert item["text"] == "Hello plain text" and item["truncated"] is False
