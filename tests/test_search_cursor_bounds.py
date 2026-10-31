"""Bounded search continuations use only the in-memory IMAP double."""

import base64
import imaplib
import json

import pytest
from test_mail import FakeImap, client, message
from test_mail import settings as settings

from proton_mail_mcp import canonical, search, search_sync
from proton_mail_mcp.mail import MailError, MailValidationError


def cursor_payload(cursor):
    return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))


def assert_no_resumable_cursor(result):
    assert result["partial"] is True
    assert result["next_cursor"] is None
    assert result["partial_reason"] == "no_resumable_cursor"
    assert "narrow" in result["hint"].lower()
    assert 'reindex(op="preview")' in result["hint"]
    assert "approval" in result["hint"].lower()
    assert 'reindex(op="execute")' in result["hint"]


def test_many_indexed_pages_keep_cursor_size_and_uid_state_bounded(settings):
    items = {
        str(uid): message(
            f"<bounded-{uid}@example.invalid>", subject="Match" if uid % 2 else "Other"
        )
        for uid in range(1, 410)
    }
    reader = client(settings, FakeImap({"INBOX": items}, uidnext=True))
    reader.rebuild_search_index("INBOX")
    filters = dict(subject="Match", limit=2, include_snippets=False)
    found = []
    cursor = None
    pages = 0
    while True:
        result = reader.search_mail(cursor=cursor, **filters)
        pages += 1
        assert not result["partial"]
        assert result["total_is_exact"]
        found.extend(row["uid"] for row in result["messages"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
        assert len(cursor) <= search._MAX_CURSOR_CHARS == 2048
        payload = cursor_payload(cursor)
        assert payload["v"] == 3
        assert len(payload["emitted"]) <= filters["limit"]
        assert payload["rejected"] == []
        assert pages < len(items), "Continuation must advance through the snapshot"

    assert pages > 100
    assert found == [str(uid) for uid in range(409, 0, -2)]


def test_indexed_nonmatches_do_not_fill_partial_cursor_rejected_set(settings, monkeypatch):
    items = {
        str(uid): message(
            f"<filtered-{uid}@example.invalid>", subject="Match" if uid % 2 else "Other"
        )
        for uid in range(1, 8)
    }
    reader = client(settings, FakeImap({"INBOX": items}, uidnext=True))
    monkeypatch.setattr(search, "_MAX_SEARCH_CANDIDATES", 4)

    result = reader.search_mail(subject="Match", limit=1, include_snippets=False)

    assert result["partial"]
    assert result["next_cursor"]
    assert {"4", "6"} <= set(reader.search_index.cached_headers("INBOX", "1", list(items)))
    assert cursor_payload(result["next_cursor"])["rejected"] == []


def test_missing_metadata_exhausts_cursor_retry_budget(settings, monkeypatch):
    items = {
        str(uid): message(f"<missing-{uid}@example.invalid>", subject="Other")
        for uid in range(1, 6)
    }
    reader = client(settings, FakeImap({"INBOX": items}, uidnext=True))
    monkeypatch.setattr(search, "_MAX_SEARCH_CANDIDATES", 2)
    metadata = reader._metadata
    attempts = 0

    def missing_metadata(connection, uids, **kwargs):
        nonlocal attempts
        attempts += 1
        assert "5" in uids
        # Omit this row before the real helper observes it in the index, so it
        # remains an unknown candidate on every attempt.
        return metadata(connection, [uid for uid in uids if uid != "5"], **kwargs)

    monkeypatch.setattr(reader, "_metadata", missing_metadata)
    cursor = None
    for call in range(search._MAX_CURSOR_RETRIES):
        result = reader.search_mail(subject="Match", include_snippets=False, cursor=cursor)
        assert result["partial_reason"] in {"metadata_missing", "no_resumable_cursor"}
        assert result["messages"] == []
        cursor = result["next_cursor"]
        if cursor is None:
            break
        assert cursor_payload(cursor)["retries"] == call + 1
    else:
        pytest.fail("Permanently missing metadata must exhaust the cursor retry budget")

    assert attempts == search._MAX_CURSOR_RETRIES
    assert_no_resumable_cursor(result)


@pytest.mark.parametrize("bounded", [False, True], ids=["default-window", "candidate-cap"])
def test_dropped_handles_override_scope_limits(settings, monkeypatch, bounded):
    items = {str(uid): message(f"<dropped-{uid}@example.invalid>") for uid in range(1, 22)}
    reader = client(settings, FakeImap({"All Mail": items}, uidnext=True))
    monkeypatch.setattr(search, "_default_since", lambda: "2026-10-01")
    monkeypatch.setattr(search, "_MAX_SEARCH_CANDIDATES", 20)

    def resolve(client, connection, deadline, handles, **kwargs):
        return [
            None if handle["uid"] == "11" else {**handle, "folder": "INBOX"} for handle in handles
        ]

    monkeypatch.setattr(canonical, "resolve_views", resolve)

    result = reader.search_mail(
        folder="All Mail",
        subject="Hello" if bounded else None,
        since="2026-10-01" if bounded else None,
        limit=20,
        include_snippets=False,
    )

    assert len(result["messages"]) == 19
    assert [row["alias"]["uid"] for row in result["messages"]] == [
        str(uid) for uid in range(21, 1, -1) if uid != 11
    ]
    assert result["partial"] and not result["total_is_exact"]
    assert result["partial_reason"] == "unresolved_handles"
    assert result["next_cursor"]
    assert result["total"] == (20 if bounded else 21)
    if not bounded:
        assert result["applied_since"] == "2026-10-01"


def test_interrupted_all_mail_catchup_adds_date_window_to_complete_fallback_page(
    settings, monkeypatch
):
    items = {str(uid): message(f"<fallback-{uid}@example.invalid>") for uid in range(1, 4)}
    fake = FakeImap({"All Mail": items}, uidnext=True)
    reader = client(settings, fake)
    monkeypatch.setattr(search, "_default_since", lambda: "2026-10-01")
    interrupted = []

    def broken_catchup(*args, **kwargs):
        interrupted.append(True)
        return search_sync.HeaderSync(timed_out=True, stream_broken=True)

    monkeypatch.setattr(search_sync, "sync_growing_headers", broken_catchup)
    monkeypatch.setattr(
        canonical,
        "resolve_views",
        lambda client, connection, deadline, handles, **kwargs: [
            {**handle, "folder": "INBOX"} for handle in handles
        ],
    )

    result = reader.search_mail(folder="All Mail", limit=1, include_snippets=False)

    assert interrupted == [True]
    assert sum(call[0] == "LOGIN" for call in fake.calls) == 2
    assert [row["alias"]["uid"] for row in result["messages"]] == ["3"]
    assert result["total"] == 3 and not result["total_is_exact"]
    assert result["partial"] and result["next_cursor"]
    assert result["partial_reason"] == "date_window"
    assert result["applied_since"] == "2026-10-01"
    assert not result.get("timed_out")


def test_successful_capped_index_warming_does_not_spend_retry_budget(settings, monkeypatch):
    items = {
        str(uid): message(f"<warming-{uid}@example.invalid>", subject="Other")
        for uid in range(1, 212)
    }
    reader = client(settings, FakeImap({"INBOX": items}, uidnext=True))
    monkeypatch.setattr(search, "_MAX_SEARCH_CANDIDATES", 2)
    cursor = None
    partial_pages = 0
    for _ in range(len(items)):
        result = reader.search_mail(subject="Match", include_snippets=False, cursor=cursor)
        assert result["messages"] == []
        cursor = result["next_cursor"]
        if cursor is None:
            break
        partial_pages += 1
        assert result["partial"] and result["partial_reason"] == "candidate_cap"
        assert len(cursor) <= search._MAX_CURSOR_CHARS
        payload = cursor_payload(cursor)
        assert payload["retries"] == 0
        assert payload["emitted"] == payload["rejected"] == []
    else:
        pytest.fail("Successful capped scans must eventually finish warming the index")

    assert partial_pages > 100
    assert result["partial"] is False and result["total_is_exact"] is True
    assert result["total"] == 0


@pytest.mark.parametrize("indexed", [True, False], ids=["indexed", "disabled-index"])
def test_unresolved_row_advances_past_resolved_prefix_and_trims_emitted(
    settings, monkeypatch, indexed
):
    items = {str(uid): message(f"<prefix-{uid}@example.invalid>") for uid in range(1, 11)}
    fake = FakeImap({"All Mail": items}, uidnext=True)
    reader = client(settings, fake)
    if indexed:
        reader.rebuild_search_index("All Mail")
    else:
        reader.search_index.enabled = False
    unresolved = True

    def resolve(client, connection, deadline, handles, **kwargs):
        return [
            None if unresolved and handle["uid"] == "8" else {**handle, "folder": "INBOX"}
            for handle in handles
        ]

    monkeypatch.setattr(canonical, "resolve_views", resolve)
    filters = dict(folder="All Mail", since="2026-01-01", limit=5, include_snippets=False)
    first = reader.search_mail(**filters)

    assert [row["alias"]["uid"] for row in first["messages"]] == ["10", "9", "7", "6"]
    assert first["partial"] and first["next_cursor"]
    payload = cursor_payload(first["next_cursor"])
    assert payload["last"][1] == 9
    assert payload["emitted"] == [6, 7]

    unresolved = False
    fake.calls.clear()
    second = reader.search_mail(cursor=first["next_cursor"], **filters)
    assert [row["alias"]["uid"] for row in second["messages"]] == ["8", "5", "4"]
    assert not second["partial"] and second["next_cursor"]
    assert cursor_payload(second["next_cursor"])["emitted"] == []
    if not indexed:
        assert any(
            call[0] == "FETCH"
            and set(call[1].split(",")) == {"6", "7"}
            and "INTERNALDATE" in call[2]
            and "BODY.PEEK" not in call[2]
            for call in fake.calls
        ), "A disabled index recovers emitted sort keys without fetching mail content"
    last = reader.search_mail(cursor=second["next_cursor"], **filters)
    assert [row["alias"]["uid"] for row in last["messages"]] == ["3", "2", "1"]
    assert last["next_cursor"] is None and not last["partial"]


def test_same_boundary_retries_drop_previously_delivered_pages(settings, monkeypatch):
    items = {str(uid): message(f"<retry-{uid}@example.invalid>") for uid in range(100001, 100421)}
    reader = client(settings, FakeImap({"All Mail": items}, uidnext=True))
    reader.search_index.enabled = False
    unresolved = True

    def resolve(client, connection, deadline, handles, **kwargs):
        return [
            None if unresolved and handle["uid"] == "100120" else {**handle, "folder": "INBOX"}
            for handle in handles
        ]

    monkeypatch.setattr(canonical, "resolve_views", resolve)
    filters = dict(folder="All Mail", since="2026-01-01", limit=100, include_snippets=False)
    cursor = None
    found = []
    for _ in range(3):
        result = reader.search_mail(cursor=cursor, **filters)
        assert not result["partial"]
        assert len(result["messages"]) == filters["limit"]
        found.extend(row["alias"]["uid"] for row in result["messages"])
        cursor = result["next_cursor"]
        assert cursor
    boundary = cursor_payload(cursor)["last"]
    earlier = set(map(int, found))

    for _ in range(2):
        result = reader.search_mail(cursor=cursor, **filters)
        found.extend(row["alias"]["uid"] for row in result["messages"])
        assert result["partial"]
        cursor = result["next_cursor"]
        assert cursor and len(cursor) < 2048
        payload = cursor_payload(cursor)
        assert payload["last"] == boundary
        assert len(payload["emitted"]) <= filters["limit"]
        assert not earlier.intersection(payload["emitted"])

    unresolved = False
    for _ in range(5):
        result = reader.search_mail(cursor=cursor, **filters)
        found.extend(row["alias"]["uid"] for row in result["messages"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
        assert len(cursor) < 2048
    else:
        pytest.fail("Resolving a retried row must allow the snapshot to finish")

    assert not result["partial"]
    assert len(found) == len(set(found))
    assert set(found) == set(items)


def test_disabled_index_nonmatches_do_not_cost_complete_page_its_cursor(settings):
    items = {
        str(uid): message(
            f"<unindexed-{uid}@example.invalid>",
            subject="Match" if uid <= 100200 else "Other",
        )
        for uid in range(100001, 100801)
    }
    reader = client(settings, FakeImap({"INBOX": items}, uidnext=True))
    reader.search_index.enabled = False
    filters = dict(subject="Match", since="2026-01-01", limit=100, include_snippets=False)

    first = reader.search_mail(**filters)

    assert not first["partial"]
    assert len(first["messages"]) == 100
    assert first["next_cursor"] and len(first["next_cursor"]) < 2048
    assert cursor_payload(first["next_cursor"])["rejected"] == []
    second = reader.search_mail(cursor=first["next_cursor"], **filters)
    assert not second["partial"] and second["next_cursor"] is None
    found = [row["uid"] for row in first["messages"] + second["messages"]]
    assert found == [str(uid) for uid in range(100200, 100000, -1)]


@pytest.mark.parametrize("indexed", [True, False], ids=["indexed", "disabled-index"])
def test_permanent_unresolved_handle_does_not_stop_other_rows(settings, monkeypatch, indexed):
    items = {str(uid): message(f"<unresolved-{uid}@example.invalid>") for uid in range(1, 11)}
    reader = client(settings, FakeImap({"All Mail": items}, uidnext=True))
    if indexed:
        reader.rebuild_search_index("All Mail")
    else:
        reader.search_index.enabled = False

    def resolve(client, connection, deadline, handles, **kwargs):
        return [
            None if handle["uid"] == "8" else {**handle, "folder": "INBOX"} for handle in handles
        ]

    monkeypatch.setattr(canonical, "resolve_views", resolve)
    cursor = None
    found = []
    skipped = False
    for _ in range(search._MAX_CURSOR_RETRIES + 4):
        result = reader.search_mail(
            folder="All Mail", since="2026-01-01", limit=3, include_snippets=False, cursor=cursor
        )
        found.extend(row["alias"]["uid"] for row in result["messages"])
        if result.get("partial_reason") == "unresolved_handles_skipped":
            assert result["partial"]
            skipped = True
        cursor = result["next_cursor"]
        if cursor is None:
            break
        assert len(cursor) <= search._MAX_CURSOR_CHARS
        assert 0 <= cursor_payload(cursor)["retries"] < search._MAX_CURSOR_RETRIES
    else:
        pytest.fail("Paging must continue past a permanently unresolved handle")

    assert skipped
    assert set(found) == set(items) - {"8"}
    assert len(found) == len(set(found)), "Retrying a handle must not duplicate resolved rows"
    assert not result["partial"]


@pytest.mark.parametrize("indexed", [True, False], ids=["indexed", "disabled-index"])
def test_canonical_lookup_errors_never_skip_page_rows(settings, monkeypatch, indexed):
    items = {str(uid): message(f"<lookup-{uid}@example.invalid>") for uid in range(1, 8)}
    reader = client(settings, FakeImap({"All Mail": items}, uidnext=True))
    if indexed:
        reader.rebuild_search_index("All Mail")
    else:
        reader.search_index.enabled = False
    lookup_failed = True
    attempts = 0

    def resolve(client, connection, deadline, handles, **kwargs):
        nonlocal attempts
        if lookup_failed:
            attempts += 1
            raise MailError("Mailbox changed during canonical lookup")
        return [{**handle, "folder": "INBOX"} for handle in handles]

    monkeypatch.setattr(canonical, "resolve_views", resolve)
    filters = dict(folder="All Mail", since="2026-01-01", limit=3, include_snippets=False)
    cursor = None
    for attempt in range(search._MAX_CURSOR_RETRIES):
        incoming_cursor = cursor
        result = reader.search_mail(cursor=cursor, **filters)
        assert result["messages"] == []
        assert result["partial"]
        assert result["partial_reason"] in {"canonical_unresolved", "no_resumable_cursor"}
        cursor = result["next_cursor"]
        if attempt < search._MAX_CURSOR_RETRIES - 1:
            assert result["partial_reason"] == "canonical_unresolved"
            assert cursor
            assert cursor_payload(cursor)["emitted"] == []
        else:
            assert_no_resumable_cursor(result)
    assert attempts == 3

    lookup_failed = False
    cursor = incoming_cursor
    found = []
    for _ in range(len(items)):
        result = reader.search_mail(cursor=cursor, **filters)
        assert not result["partial"]
        found.extend(row["alias"]["uid"] for row in result["messages"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("Retrying the saved incoming cursor must recover every page row")
    assert found == [str(uid) for uid in range(7, 0, -1)]


def test_snippet_stream_error_spends_view_page_retry_budget(settings, monkeypatch):
    items = {str(uid): message(f"<snippet-{uid}@example.invalid>") for uid in range(1, 8)}
    reader = client(settings, FakeImap({"All Mail": items}, uidnext=True))
    attempts = 0

    def broken_body(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise imaplib.IMAP4.abort("Snippet stream interrupted")

    def forbidden_lookup(*args, **kwargs):
        pytest.fail("Never resolve canonical handles on a broken snippet stream")

    monkeypatch.setattr(reader, "_body", broken_body)
    monkeypatch.setattr(canonical, "resolve_views", forbidden_lookup)
    cursor = None
    for attempt in range(search._MAX_CURSOR_RETRIES):
        result = reader.search_mail(folder="All Mail", since="2026-01-01", limit=3, cursor=cursor)
        assert result["messages"] == []
        assert result["partial"]
        assert result["partial_reason"] in {"metadata_missing", "no_resumable_cursor"}
        assert result.get("partial_reason") != "unresolved_handles_skipped"
        cursor = result["next_cursor"]
        if attempt < search._MAX_CURSOR_RETRIES - 1:
            assert cursor
            payload = cursor_payload(cursor)
            assert payload["retries"] == attempt + 1
            assert payload["last"] is None
            assert payload["emitted"] == []
        else:
            assert_no_resumable_cursor(result)
    assert attempts == 3


@pytest.mark.parametrize("stuck", [None, "7"], ids=["malformed-date", "with-stuck-row"])
def test_disabled_index_keeps_malformed_dates_rejected_across_pages(settings, monkeypatch, stuck):
    items = {str(uid): message(f"<invalid-{uid}@example.invalid>") for uid in range(1, 12)}
    items["11"].internaldate = "malformed"
    fake = FakeImap({"All Mail": items}, uidnext=True)
    reader = client(settings, fake)
    reader.search_index.enabled = False

    def resolve(client, connection, deadline, handles, **kwargs):
        return [
            None if handle["uid"] == stuck else {**handle, "folder": "INBOX"} for handle in handles
        ]

    monkeypatch.setattr(canonical, "resolve_views", resolve)
    cursor = None
    found = []
    skipped = False
    for page in range(search._MAX_CURSOR_RETRIES + len(items)):
        result = reader.search_mail(
            folder="All Mail", since="2026-01-01", limit=3, include_snippets=False, cursor=cursor
        )
        found.extend(row["alias"]["uid"] for row in result["messages"])
        skipped |= result.get("partial_reason") == "unresolved_handles_skipped"
        if page and result["partial"]:
            assert stuck and result["partial_reason"] in {
                "unresolved_handles",
                "unresolved_handles_skipped",
            }
        cursor = result["next_cursor"]
        if cursor is None:
            assert not result["partial"]
            break
        assert 11 in cursor_payload(cursor)["rejected"]
    else:
        pytest.fail("A malformed date must not stop traversal past a stuck handle")

    assert page > 2
    assert skipped is (stuck is not None)
    assert found == [str(uid) for uid in range(10, 0, -1) if str(uid) != stuck]
    assert (
        sum(
            call[0] == "FETCH" and "11" in call[1].split(",") and "INTERNALDATE" in call[2]
            for call in fake.calls
        )
        == 1
    )


def test_overlong_cursor_is_rejected_before_decode_or_connection(settings, monkeypatch):
    fake = FakeImap()
    reader = client(settings, fake)

    def forbidden(*args, **kwargs):
        pytest.fail("An oversized cursor must be rejected before decoding")

    monkeypatch.setattr(search.base64, "b64decode", forbidden)
    monkeypatch.setattr(search.json, "loads", forbidden)
    with pytest.raises(MailValidationError, match="Invalid search cursor"):
        reader.search_mail(cursor="a" * (search._MAX_CURSOR_CHARS + 1), since="2026-10-01")
    assert fake.calls == []


def test_encode_size_cap_keeps_incomplete_result_explicit(settings, monkeypatch):
    fake = FakeImap(
        {"INBOX": {str(uid): message(f"<cap-{uid}@example.invalid>") for uid in range(1, 4)}}
    )
    monkeypatch.setattr(search, "_MAX_CURSOR_CHARS", 64)

    result = client(settings, fake).search_mail(limit=1, include_snippets=False)

    assert [row["uid"] for row in result["messages"]] == ["3"]
    assert_no_resumable_cursor(result)
