"""Partial search continuations use in-memory peers, never a real mailbox."""

import base64
import json

import pytest
from test_mail import FakeImap, client, message
from test_mail import settings as settings
from test_search_paging import chronological_mailbox

from proton_mail_mcp.mail import CallDeadlineExceeded, MailValidationError
from proton_mail_mcp.search import _MAX_CURSOR_RETRIES, _encode_cursor


@pytest.mark.parametrize("missing_index", [0, 1, 2])
@pytest.mark.parametrize("incoming_page", [False, True])
def test_all_mail_partial_cursor_recovers_unresolved_without_duplicates_or_skips(
    settings, monkeypatch, missing_index, incoming_page
):
    fake = chronological_mailbox()
    fake.folders["All Mail"] = fake.folders["INBOX"].copy()
    reader = client(settings, fake)
    filters = dict(
        to=settings.bridge_user,
        folder="All Mail",
        since="2026-10-01",
        include_snippets=False,
    )
    expected = ["14187", "10357", "2894", "20101", "7"]
    pending = None

    def resolve(client, connection, deadline, handles, **kwargs):
        # Canonical and view UIDs differ: only the source UID can checkpoint
        # this search. Return resolved rows on both sides of the missing row.
        return [
            None
            if handle["uid"] == pending
            else {**handle, "folder": "INBOX", "uid": str(int(handle["uid"]) + 100_000)}
            for handle in handles
        ]

    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_views", resolve)
    cursor = None
    delivered = []
    if incoming_page:
        first = reader.search_mail(limit=1, **filters)
        delivered = [item["alias"]["uid"] for item in first["messages"]]
        cursor = first["next_cursor"]
    pending = expected[len(delivered) + missing_index]
    partial = reader.search_mail(limit=3, cursor=cursor, **filters)
    assert partial["partial"] and partial["next_cursor"]
    assert partial["partial_reason"] == "unresolved_handles"
    assert len(partial["messages"]) == 2 and partial["total"] == 5
    delivered.extend(item["alias"]["uid"] for item in partial["messages"])

    # A second interrupted lookup must also preserve progress. New higher UIDs
    # cannot shift the original snapshot, even with a newer INTERNALDATE.
    fake.folders["All Mail"]["999999"] = message(
        "<arrival@example.invalid>", internaldate="28-Oct-2026 10:00:00 +0000"
    )
    retry = reader.search_mail(limit=1, cursor=partial["next_cursor"], **filters)
    assert retry["messages"] == [] and retry["partial"] and retry["next_cursor"]
    pending = None
    cursor = retry["next_cursor"]
    for _ in range(4):
        page = reader.search_mail(limit=2, cursor=cursor, **filters)
        assert page["total"] == 5
        delivered.extend(item["alias"]["uid"] for item in page["messages"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert len(delivered) == len(set(delivered)) == 5
    assert set(delivered) == set(expected)
    assert all(call[2] is True for call in fake.calls if call[0] == "EXAMINE")
    assert not any(call[0] in {"STORE", "EXPUNGE", "COPY", "MOVE"} for call in fake.calls)
    assert all("BODY[" not in call[2] for call in fake.calls if call[0] == "FETCH")


@pytest.mark.parametrize("index_enabled", [False, True])
def test_capped_scan_cursor_continues_past_nonmatches_and_retains_older_delivered_rows(
    settings, monkeypatch, index_enabled
):
    # The first capped UID sample contains the oldest hit and a nonmatch.
    # Later samples contain newer hits, so a chronological checkpoint here
    # would skip them. Rejected nonmatches must not block a disabled index.
    fake = FakeImap(
        {
            "INBOX": {
                "5": message(
                    "<old@example.invalid>",
                    subject="Hit",
                    internaldate="01-Oct-2026 00:00:00 +0000",
                ),
                "4": message("<miss@example.invalid>", subject="Miss"),
                "3": message(
                    "<new@example.invalid>",
                    subject="Hit",
                    internaldate="27-Oct-2026 00:00:00 +0000",
                ),
                "2": message("<middle@example.invalid>", subject="Hit"),
                "1": message("<other-miss@example.invalid>", subject="Miss"),
            }
        }
    )
    monkeypatch.setattr("proton_mail_mcp.search._MAX_SEARCH_CANDIDATES", 2)
    reader = client(settings, fake)
    if not index_enabled:
        monkeypatch.setattr(reader.search_index, "enabled", False)
    page = reader.search_mail(subject="Hit", limit=1, include_snippets=False)
    assert page["partial"] and page["next_cursor"]
    assert page["partial_reason"] == "candidate_cap"
    assert [item["uid"] for item in page["messages"]] == ["5"]
    delivered = ["5"]
    for _ in range(5):
        page = reader.search_mail(
            subject="Hit", limit=1, include_snippets=False, cursor=page["next_cursor"]
        )
        delivered.extend(item["uid"] for item in page["messages"])
        if page["next_cursor"] is None:
            break
    assert page["next_cursor"] is None and not page["partial"]
    assert page["total"] == 3 and page["total_is_exact"]
    assert delivered == ["5", "3", "2"]


def test_timeout_before_search_enumeration_returns_a_start_cursor(settings, monkeypatch):
    fake = chronological_mailbox()
    fake.reports_uidnext = True
    reader = client(settings, fake)
    search = reader._search

    def timeout(*args, **kwargs):
        raise CallDeadlineExceeded("Simulated SEARCH timeout")

    monkeypatch.setattr(reader, "_search", timeout)
    first = reader.search_mail(limit=2, include_snippets=False)
    assert first["partial"] and first["timed_out"] and first["next_cursor"]
    assert first["partial_reason"] == "timed_out"
    assert first["messages"] == []
    fake.folders["INBOX"]["999999"] = message(
        "<arrival@example.invalid>", internaldate="28-Oct-2026 10:00:00 +0000"
    )
    monkeypatch.setattr(reader, "_search", search)
    resumed = reader.search_mail(limit=2, include_snippets=False, cursor=first["next_cursor"])
    assert [item["uid"] for item in resumed["messages"]] == ["14187", "10357"]


def test_timeout_before_selection_cursor_still_applies_all_mail_default_scope(
    settings, monkeypatch
):
    fake = FakeImap({"All Mail": {"1": message("<hit@example.invalid>")}})
    reader = client(settings, fake)
    select = reader._select

    def timeout(*args):
        raise CallDeadlineExceeded("Simulated EXAMINE timeout")

    monkeypatch.setattr(reader, "_select", timeout)
    first = reader.search_mail(folder="All Mail", include_snippets=False)
    assert first["partial"] and first["next_cursor"]
    monkeypatch.setattr(reader, "_select", select)
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-10-01")
    monkeypatch.setattr(
        "proton_mail_mcp.canonical.resolve_views",
        lambda client, connection, deadline, handles, **kwargs: [
            {**handle, "folder": "INBOX"} for handle in handles
        ],
    )
    resumed = reader.search_mail(
        folder="All Mail", include_snippets=False, cursor=first["next_cursor"]
    )
    assert resumed["applied_since"] == "2026-10-01"
    assert resumed["partial"] and resumed["next_cursor"] is None
    assert resumed["partial_reason"] == "no_resumable_cursor"
    hint = resumed["hint"]
    assert (
        hint.index('reindex(op="preview")')
        < hint.index("explicit user approval")
        < hint.index('reindex(op="execute")')
    )
    assert [item["alias"]["uid"] for item in resumed["messages"]] == ["1"]


def test_complete_cursor_can_continue_when_index_loss_forces_a_capped_scan(settings, monkeypatch):
    fake = FakeImap(
        {"INBOX": {str(uid): message(f"<{uid}@example.invalid>") for uid in range(1, 6)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    reader.rebuild_search_index("INBOX")
    first = reader.search_mail(subject="Hello", limit=2, include_snippets=False)
    assert not first["partial"]
    assert [item["uid"] for item in first["messages"]] == ["5", "4"]
    monkeypatch.setattr(reader.search_index, "enabled", False)
    monkeypatch.setattr("proton_mail_mcp.search._MAX_SEARCH_CANDIDATES", 2)
    cursor = first["next_cursor"]
    delivered = ["5", "4"]
    for _ in range(5):
        page = reader.search_mail(subject="Hello", limit=2, include_snippets=False, cursor=cursor)
        delivered.extend(item["uid"] for item in page["messages"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None and delivered == ["5", "4", "3", "2", "1"]
    assert page["total"] == 5 and page["total_is_exact"]


@pytest.mark.parametrize("failure", ["scan-timeout", "page-missing"])
def test_interrupted_metadata_cursor_recovers_every_matching_row(settings, monkeypatch, failure):
    fake = chronological_mailbox()
    reader = client(settings, fake)
    metadata = reader._metadata
    attempts = 0
    monkeypatch.setattr("proton_mail_mcp.search._METADATA_BATCH_SIZE", 2)

    def interrupted(connection, uids, **kwargs):
        nonlocal attempts
        attempts += 1
        if failure == "scan-timeout" and attempts == 2:
            raise CallDeadlineExceeded("Simulated metadata timeout")
        found = metadata(connection, uids, **kwargs)
        if failure == "page-missing" and kwargs.get("bodystructure"):
            found.pop("14187", None)
        return found

    monkeypatch.setattr(reader, "_metadata", interrupted)
    first = reader.search_mail(subject="Hello", limit=2)
    assert first["partial"] and first["next_cursor"]
    delivered = [item["uid"] for item in first["messages"]]
    monkeypatch.setattr(reader, "_metadata", metadata)
    page = reader.search_mail(
        subject="Hello", limit=100, include_snippets=False, cursor=first["next_cursor"]
    )
    delivered.extend(item["uid"] for item in page["messages"])
    assert page["next_cursor"] is None and not page["partial"]
    assert len(delivered) == len(set(delivered)) == 5
    assert set(delivered) == {"14187", "10357", "2894", "20101", "7"}


def test_timeouts_warming_uncached_candidates_do_not_spend_retry_budget(settings, monkeypatch):
    items = {
        str(uid): message(f"<timeout-{uid}@example.invalid>", subject="Hit" if uid % 2 else "Miss")
        for uid in range(1, 10)
    }
    reader = client(settings, FakeImap({"INBOX": items}, uidnext=True))
    monkeypatch.setattr("proton_mail_mcp.search._METADATA_BATCH_SIZE", 1)
    monkeypatch.setattr("proton_mail_mcp.search._MAX_SEARCH_CANDIDATES", 2)
    metadata = reader._metadata
    header_fetches = 0

    def interrupted(connection, uids, **kwargs):
        nonlocal header_fetches
        if kwargs.get("headers"):
            header_fetches += 1
            if header_fetches == 2:
                raise CallDeadlineExceeded("Simulated metadata timeout")
        return metadata(connection, uids, **kwargs)

    monkeypatch.setattr(reader, "_metadata", interrupted)
    cursor = None
    delivered = []
    timed_out_pages = 0
    for _ in range(len(items)):
        header_fetches = 0
        page = reader.search_mail(subject="Hit", limit=2, include_snippets=False, cursor=cursor)
        delivered.extend(item["uid"] for item in page["messages"])
        cursor = page["next_cursor"]
        if not page.get("timed_out"):
            break
        timed_out_pages += 1
        assert page["partial"] and cursor
        state = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        assert state["retries"] == 0

    assert timed_out_pages == len(items) - 1
    assert cursor is None and not page["partial"] and page["total_is_exact"]
    assert page["total"] == 5
    assert delivered == ["9", "7", "5", "3", "1"]


def test_canonical_timeout_spends_retry_budget_only_after_header_progress_stops(
    settings, monkeypatch
):
    items = {str(uid): message(f"<lookup-timeout-{uid}@example.invalid>") for uid in range(1, 4)}
    reader = client(settings, FakeImap({"All Mail": items}, uidnext=True))

    def timeout(*args, **kwargs):
        raise CallDeadlineExceeded("Simulated canonical lookup timeout")

    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_views", timeout)
    cursor = None
    for attempt in range(_MAX_CURSOR_RETRIES + 1):
        page = reader.search_mail(
            folder="All Mail",
            since="2026-01-01",
            subject="Hello",
            limit=2,
            include_snippets=False,
            cursor=cursor,
        )
        assert page["partial"] and page["timed_out"] and page["messages"] == []
        assert page["partial_reason"] in {"canonical_unresolved", "no_resumable_cursor"}
        cursor = page["next_cursor"]
        if attempt < _MAX_CURSOR_RETRIES:
            assert page["partial_reason"] == "canonical_unresolved" and cursor
            state = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            assert state["retries"] == attempt and state["last"] is None
        else:
            assert cursor is None and page["partial_reason"] == "no_resumable_cursor"


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("failure", ["scan", "page-fetch"])
def test_cached_date_becoming_invalid_is_skipped_until_cursor_exhaustion(
    settings, monkeypatch, indexed, failure
):
    class CorruptedDateImap(FakeImap):
        damage_enabled = False
        invalid_fetches = 0

        def uid(self, command, *args):
            if (
                self.damage_enabled
                and command == "FETCH"
                and "99" in args[0].split(",")
                and "INTERNALDATE" in args[1]
                and (failure == "scan" or self.invalid_fetches or "FLAGS" not in args[1])
            ):
                # In the page-fetch case the first date/flags FETCH succeeds;
                # every later FETCH of this UID returns the malformed date.
                self.folders["INBOX"]["99"].internaldate = "not a date"
                self.invalid_fetches += 1
            return super().uid(command, *args)

    fake = CorruptedDateImap(
        {
            "INBOX": {
                "99": message("<bad@example.invalid>", internaldate="20-Oct-2026 00:00:00 +0000"),
                "42": message(
                    "<middle@example.invalid>", internaldate="17-Oct-2026 00:00:00 +0000"
                ),
                "7": message("<old@example.invalid>", internaldate="01-Oct-2026 00:00:00 +0000"),
            }
        },
        uidnext=True,
    )
    reader = client(settings, fake)
    reader.rebuild_search_index("INBOX")
    assert reader.search_index.ordered_page("INBOX", "1", ["99", "42", "7"], [], 2)
    if not indexed:
        monkeypatch.setattr(reader.search_index, "enabled", False)
    fake.damage_enabled = True
    page = reader.search_mail(limit=2, include_snippets=False)
    assert page["partial"] and not page["total_is_exact"] and page["next_cursor"]
    assert page["total"] == 2
    assert page["partial_reason"] == "metadata_missing"
    state = json.loads(
        base64.urlsafe_b64decode(page["next_cursor"] + "=" * (-len(page["next_cursor"]) % 4))
    )
    assert state["rejected"] == [99]
    delivered = [item["uid"] for item in page["messages"]]
    for _ in range(3):
        page = reader.search_mail(limit=2, include_snippets=False, cursor=page["next_cursor"])
        delivered.extend(item["uid"] for item in page["messages"])
        if page["next_cursor"] is None:
            break
    assert page["next_cursor"] is None
    assert page["total"] == 2
    assert delivered == ["42", "7"]
    assert fake.invalid_fetches == 1


@pytest.mark.parametrize("valid_first", [False, True])
def test_split_metadata_valid_date_prevents_rejecting_the_uid(settings, valid_first):
    class SplitDateImap(FakeImap):
        def uid(self, command, *args):
            assert command == "FETCH"
            records = [
                b'1 (UID 42 INTERNALDATE "not a date")',
                b'1 (UID 42 INTERNALDATE "17-Oct-2026 09:00:00 +0000")',
            ]
            return "OK", list(reversed(records)) if valid_first else records

    fake = SplitDateImap()
    reader = client(settings, fake)
    invalid_dates = set()
    metadata = reader._metadata(
        fake,
        ["42"],
        deadline=reader._deadline(),
        headers=False,
        bodystructure=False,
        invalid_dates=invalid_dates,
    )
    assert metadata["42"]["internaldate"] > 0
    assert invalid_dates == set()


def test_missing_internaldate_is_retryable_and_can_recover_without_duplicates(settings):
    class MissingDateImap(FakeImap):
        omit_date = True

        def uid(self, command, *args):
            status, records = super().uid(command, *args)
            if self.omit_date and command == "FETCH" and "INTERNALDATE" in args[1]:

                def without_date(raw):
                    if b"UID 99 " in raw:
                        return raw.replace(b' INTERNALDATE "17-Oct-2026 09:00:00 +0000"', b"")
                    return raw

                records = [
                    (without_date(record[0]), record[1])
                    if isinstance(record, tuple)
                    else without_date(record)
                    for record in records
                ]
            return status, records

    fake = MissingDateImap(
        {
            "INBOX": {
                "42": message("<valid@example.invalid>"),
                "99": message(
                    "<missing@example.invalid>", internaldate="17-Oct-2026 09:00:00 +0000"
                ),
            }
        }
    )
    reader = client(settings, fake)
    first = reader.search_mail(include_snippets=False)
    assert first["partial"] and first["next_cursor"]
    assert [item["uid"] for item in first["messages"]] == ["42"]
    state = json.loads(
        base64.urlsafe_b64decode(first["next_cursor"] + "=" * (-len(first["next_cursor"]) % 4))
    )
    assert state["rejected"] == []
    fake.omit_date = False
    recovered = reader.search_mail(include_snippets=False, cursor=first["next_cursor"])
    assert [item["uid"] for item in recovered["messages"]] == ["99"]
    assert not recovered["partial"] and recovered["next_cursor"] is None


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_chronological_cursors_still_resume(settings, version):
    reader = client(settings, chronological_mailbox())
    first = reader.search_mail(limit=2, include_snippets=False)
    value = json.loads(
        base64.urlsafe_b64decode(first["next_cursor"] + "=" * (-len(first["next_cursor"]) % 4))
    )
    value.update(v=version)
    del value["retries"]
    if version == 1:
        del value["emitted"], value["rejected"]
    legacy = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
    second = reader.search_mail(limit=2, include_snippets=False, cursor=legacy)
    assert [item["uid"] for item in second["messages"]] == ["2894", "20101"]


def test_oversized_continuation_state_cannot_be_encoded():
    assert _encode_cursor("query", "1", None, 150_000, set(), set(range(1, 150_001))) is None


@pytest.mark.parametrize(
    "field,value",
    [("emitted", [True]), ("emitted", [999999]), ("rejected", "bad"), ("max_uid", -1)],
)
def test_partial_cursor_rejects_invalid_retry_state_before_connecting(settings, field, value):
    fake = chronological_mailbox()
    reader = client(settings, fake)
    first = reader.search_mail(limit=2, include_snippets=False)
    cursor = first["next_cursor"]
    state = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    state[field] = value
    invalid = base64.urlsafe_b64encode(json.dumps(state).encode()).decode()
    calls = len(fake.calls)
    with pytest.raises(MailValidationError):
        reader.search_mail(cursor=invalid)
    assert len(fake.calls) == calls
