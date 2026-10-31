"""Search pages use only the in-memory IMAP fixture and bounded PEEK reads."""

import base64
import json
from datetime import datetime

import pytest
from bridge_costs import DEFAULT_COSTS
from gluon_search import GluonSearchImap
from test_mail import PLAIN, FakeImap, client, message
from test_mail import settings as settings

from proton_mail_mcp.mail import CallDeadlineExceeded, MailValidationError


@pytest.fixture(autouse=True)
def canonical_handles(monkeypatch):
    # Paging tests use view-only fixtures; canonical copy proof has separate
    # simulator coverage in test_canonical_identity.py.
    def resolve(client, connection, deadline, handles, **kwargs):
        return [{**handle, "folder": "INBOX"} for handle in handles]

    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_views", resolve)


class DateIndexedAllMail(FakeImap):
    """Date searches select cheap indexed candidates before reading headers."""

    def uid(self, command, *args):
        if command != "SEARCH" or "SINCE" not in args:
            return super().uid(command, *args)
        bound = datetime.strptime(args[args.index("SINCE") + 1], "%d-%b-%Y").date()
        original = self.folders[self.current]
        dates = {
            item.internaldate: datetime.strptime(item.internaldate, "%d-%b-%Y %H:%M:%S %z").date()
            for item in {id(item): item for item in original.values()}.values()
        }
        self.folders[self.current] = {
            uid: item for uid, item in original.items() if dates[item.internaldate] >= bound
        }
        try:
            return super().uid(command, *args)
        finally:
            self.folders[self.current] = original


def test_all_mail_without_dates_uses_visible_default_window_and_returns_newest_page(
    settings, monkeypatch
):
    old = message("<old@example.invalid>", internaldate="01-Jan-2020 09:00:00 +0000")
    mailbox = {str(uid): old for uid in range(100, 20_100)}
    # UIDs deliberately run opposite to chronology.
    mailbox.update(
        {
            str(uid): message(
                f"<recent-{uid}@example.invalid>",
                internaldate=f"{28 - uid:02d}-Sep-2026 09:00:00 +0000",
            )
            for uid in range(1, 12)
        }
    )
    fake = DateIndexedAllMail({"All Mail": mailbox})
    reader = client(settings, fake)
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-09-17")
    now = [100.0]
    monkeypatch.setattr("proton_mail_mcp.mail.time.monotonic", lambda: now[0])
    metadata = reader._metadata

    def charged_metadata(connection, uids, **kwargs):
        # Use the same DB/literal costs as the socket-level calibrated model.
        # The legacy watchdog overhead is modeled in that suite, not here.
        row_cost = DEFAULT_COSTS.db_row
        if kwargs.get("headers"):
            row_cost += DEFAULT_COSTS.literal_load
        now[0] += len(uids) * row_cost
        return metadata(connection, uids, **kwargs)

    monkeypatch.setattr(reader, "_metadata", charged_metadata)
    result = reader.search_mail(folder="All Mail", limit=3, include_snippets=False)
    # Canonical discovery is stubbed above; paging still uses All Mail's own UIDs.
    assert [item["alias"]["uid"] for item in result["messages"]] == ["1", "2", "3"]
    assert {item["folder"] for item in result["messages"]} == {"INBOX"}
    assert all(
        item["uidvalidity"] is not None and item["uid"] is not None for item in result["messages"]
    )
    assert result["total"] == 11
    assert result["partial"] is True and result["total_is_exact"] is False
    assert result["partial_reason"] == "date_window"
    assert result["applied_since"] == "2026-09-17"
    assert result["next_cursor"]
    assert now[0] - 100 < 1
    assert next(call for call in fake.calls if call[0] == "SEARCH") == (
        "SEARCH",
        "CHARSET",
        "UTF-8",
        "SINCE",
        "17-Sep-2026",
    )
    assert all("HEADER" not in call for call in fake.calls if call[0] == "SEARCH")
    second = reader.search_mail(
        folder="All Mail", limit=3, include_snippets=False, cursor=result["next_cursor"]
    )
    assert [item["alias"]["uid"] for item in second["messages"]] == ["4", "5", "6"]


def test_all_mail_default_window_retains_match_count_when_metadata_scan_times_out(
    settings, monkeypatch
):
    fake = FakeImap(
        {"All Mail": {str(uid): message(f"<{uid}@example.invalid>") for uid in range(1, 502)}}
    )
    reader = client(settings, fake)
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-09-17")
    metadata = reader._metadata
    attempts = 0

    def interrupted_metadata(connection, uids, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise CallDeadlineExceeded("Mailbox call timed out")
        assert attempts < 2, "Never reuse a timed-out IMAP connection"
        return metadata(connection, uids, **kwargs)

    monkeypatch.setattr(reader, "_metadata", interrupted_metadata)
    result = reader.search_mail(folder="All Mail", limit=3)
    assert attempts == 2
    # A broken stream cannot resolve a view's canonical handles. The completed
    # match count survives; unattempted lookups do not count as unresolved rows.
    assert result["messages"] == [] and result["total"] == 500
    assert result["partial_reason"] == "timed_out"
    assert result["partial"] is True and result["total_is_exact"] is False
    assert result["applied_since"] == "2026-09-17"
    assert result["next_cursor"]


@pytest.mark.parametrize("bounds", [{"since": "2026-09-17"}, {"before": "2026-11-01"}])
def test_all_mail_explicit_date_bounds_do_not_apply_default_window(settings, bounds):
    fake = FakeImap({"All Mail": {"1": message("<bounded@example.invalid>")}})
    result = client(settings, fake).search_mail(folder="All Mail", include_snippets=False, **bounds)
    assert len(result["messages"]) == 1 and result["total"] == 1
    assert result["partial"] is False and result["total_is_exact"] is True
    assert "applied_since" not in result


@pytest.mark.parametrize(
    "folder,flags,window",
    [
        ("Tous les messages", r"\HasNoChildren \All", True),
        ("Alle Nachrichten", r"\aLl", True),
        ("All Mail", r"\HasNoChildren", False),
        ("Archive", r"\Archive", False),
    ],
)
def test_default_window_uses_list_all_attribute(settings, monkeypatch, folder, flags, window):
    fake = FakeImap(
        {folder: {"1": message("<window@example.invalid>")}}, folder_flags={folder: flags}
    )
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-09-17")
    result = client(settings, fake).search_mail(folder=folder, include_snippets=False)
    assert result["partial"] is window
    assert result["total_is_exact"] is not window
    assert ("applied_since" in result) is window
    searches = [call for call in fake.calls if call[0] == "SEARCH"]
    assert len(searches) == 1
    assert ("SINCE" in searches[0]) is window


def chronological_mailbox():
    # Bridge UIDs do not track message chronology. Header Date can also differ
    # from INTERNALDATE, so exercise both differences explicitly.
    return FakeImap(
        {
            "INBOX": {
                "20101": message("<old@example.com>", internaldate="12-Oct-2026 09:00:00 +0000"),
                "14187": message("<new@example.com>", internaldate="16-Oct-2026 09:00:00 +0000"),
                "10357": message("<middle@example.com>", internaldate="15-Oct-2026 09:00:00 +0000"),
                "2894": message("<tied@example.com>", internaldate="15-Oct-2026 09:00:00 +0000"),
                "7": message("<older@example.com>", internaldate="11-Oct-2026 09:00:00 +0000"),
            }
        }
    )


def test_search_first_next_last_pages_are_chronological(settings):
    fake = chronological_mailbox()
    reader = client(settings, fake)
    first = reader.search_mail(limit=2, include_snippets=False)
    assert [item["uid"] for item in first["messages"]] == ["14187", "10357"]
    assert first["total"] == 5 and first["total_is_exact"] is True
    assert first["partial"] is False and first["next_cursor"]
    second = reader.search_mail(limit=2, cursor=first["next_cursor"], include_snippets=False)
    assert [item["uid"] for item in second["messages"]] == ["2894", "20101"]
    assert second["total"] == 5 and second["next_cursor"]
    last = reader.search_mail(limit=2, cursor=second["next_cursor"], include_snippets=False)
    assert [item["uid"] for item in last["messages"]] == ["7"]
    assert last["total"] == 5 and last["next_cursor"] is None
    assert all("snippet" not in item for item in first["messages"])


@pytest.mark.parametrize(
    "folder,filters,window",
    [
        pytest.param("INBOX", {}, False, id="plain"),
        pytest.param("INBOX", {"since": "2026-09-17"}, False, id="date-only"),
        pytest.param("INBOX", {"unread_only": True}, False, id="unread-only"),
        pytest.param("All Mail", {}, True, id="all-mail-default-window"),
    ],
)
def test_search_large_folders_keep_complete_cursor_paging(
    settings, monkeypatch, folder, filters, window
):
    # Shared immutable payloads keep this fixture cheap; its distinct UIDs
    # exercise more matches than the expensive-header candidate limit.
    recent = message("<recent@example.invalid>", internaldate="14-Oct-2026 10:00:00 +0000")
    mailbox = {str(uid): recent for uid in range(1, 1004)}
    if "since" in filters or window:
        mailbox["1004"] = message(
            "<old@example.invalid>", internaldate="01-Jan-2020 09:00:00 +0000"
        )
    if filters.get("unread_only"):
        mailbox["1004"] = message(
            "<seen@example.invalid>",
            internaldate="14-Oct-2026 10:00:00 +0000",
            flags={r"\Seen"},
        )
    fake = GluonSearchImap({folder: mailbox})
    reader = client(settings, fake)
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-09-17")

    result = reader.search_mail(folder=folder, limit=100, include_snippets=False, **filters)
    assert result["next_cursor"], "A large metadata scan must retain chronological paging"
    found = []
    # Eleven pages cover the whole fixture. A repeated cursor must not loop.
    for _ in range(11):
        assert result["total"] == 1003
        assert result["partial"] is window
        assert result["total_is_exact"] is not window
        if not window:
            assert "partial_reason" not in result
        if window:
            assert result["applied_since"] == "2026-09-17"
        found.extend(
            item["uid"] if folder == "INBOX" else item["alias"]["uid"]
            for item in result["messages"]
        )
        if result["next_cursor"] is None:
            break
        result = reader.search_mail(
            folder=folder,
            limit=100,
            include_snippets=False,
            cursor=result["next_cursor"],
            **filters,
        )

    assert result["next_cursor"] is None
    if window:
        assert result["partial_reason"] == "no_resumable_cursor"
        assert 'reindex(op="preview")' in result["hint"]
    assert found == [str(uid) for uid in range(1003, 0, -1)]


def test_search_empty_and_exact_last_page_have_no_cursor(settings):
    empty = client(settings, FakeImap()).search_mail(include_snippets=False)
    assert empty == {
        "messages": [],
        "next_cursor": None,
        "total": 0,
        "partial": False,
        "total_is_exact": True,
    }
    full = client(settings, chronological_mailbox()).search_mail(limit=5, include_snippets=False)
    assert len(full["messages"]) == 5 and full["next_cursor"] is None


@pytest.mark.parametrize("cursor", ["", "!bad", "e30", "null", 3, True])
def test_search_invalid_cursor_rejected_before_connecting(settings, cursor):
    fake = FakeImap()
    with pytest.raises(MailValidationError, match="Invalid search cursor"):
        client(settings, fake).search_mail(cursor=cursor)
    assert fake.calls == []


def test_search_cursor_rejects_changed_filters_and_uidvalidity(settings):
    fake = chronological_mailbox()
    reader = client(settings, fake)
    first = reader.search_mail(limit=1, include_snippets=False)
    calls = len(fake.calls)
    with pytest.raises(MailValidationError, match="Invalid search cursor"):
        reader.search_mail(subject="different", cursor=first["next_cursor"])
    assert len(fake.calls) == calls
    fake.uidvalidity = "2"
    with pytest.raises(MailValidationError, match="UIDVALIDITY changed"):
        reader.search_mail(cursor=first["next_cursor"])
    assert not any(call[0] in {"SEARCH", "FETCH"} for call in fake.calls[calls:])


def test_search_cursor_freezes_new_arrivals(settings):
    fake = chronological_mailbox()
    reader = client(settings, fake)
    first = reader.search_mail(limit=2, include_snippets=False)
    fake.folders["INBOX"]["20102"] = message(
        "<arrival@example.com>", internaldate="13-Oct-2026 09:00:00 +0000"
    )
    second = reader.search_mail(limit=100, cursor=first["next_cursor"], include_snippets=False)
    assert [item["uid"] for item in second["messages"]] == ["2894", "20101", "7"]
    assert second["total"] == 5


def test_search_cursor_tolerates_deletions_between_pages(settings):
    fake = chronological_mailbox()
    reader = client(settings, fake)
    first = reader.search_mail(limit=2, include_snippets=False)
    del fake.folders["INBOX"]["2894"]
    second = reader.search_mail(limit=2, cursor=first["next_cursor"], include_snippets=False)
    assert [item["uid"] for item in second["messages"]] == ["20101", "7"]
    assert second["total"] == 4 and second["total_is_exact"] is True
    assert second["next_cursor"] is None and second["partial"] is False


def test_search_orders_internaldates_by_instant_across_timezone_offsets(settings):
    fake = FakeImap(
        {
            "INBOX": {
                "3": message("<early@example.com>", internaldate="17-Oct-2026 10:00:00 +1400"),
                "2": message("<late@example.com>", internaldate="16-Oct-2026 23:00:00 -1200"),
                "1": message("<middle@example.com>", internaldate="17-Oct-2026 00:00:00 +0000"),
            }
        }
    )
    reader = client(settings, fake)
    first = reader.search_mail(limit=1, include_snippets=False)
    assert [item["uid"] for item in first["messages"]] == ["2"]
    second = reader.search_mail(limit=2, cursor=first["next_cursor"], include_snippets=False)
    assert [item["uid"] for item in second["messages"]] == ["1", "3"]
    assert second["next_cursor"] is None


@pytest.mark.parametrize("mutation", ["infinity", "max_uid"])
def test_search_rejects_impossible_cursor_sort_bounds_before_connecting(settings, mutation):
    fake = chronological_mailbox()
    reader = client(settings, fake)
    cursor = reader.search_mail(limit=1, include_snippets=False)["next_cursor"]
    value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    if mutation == "infinity":
        value["last"][0] = float("inf")
    else:
        value["max_uid"] = value["last"][1] - 1
    invalid = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
    calls = len(fake.calls)
    with pytest.raises(MailValidationError):
        reader.search_mail(cursor=invalid)
    assert len(fake.calls) == calls


def test_search_cursor_rejects_wrong_sort_key_types(settings):
    fake = chronological_mailbox()
    first = client(settings, fake).search_mail(limit=1, include_snippets=False)
    cursor = first["next_cursor"]
    value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    value["last"] = [True, 1]
    invalid = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
    calls = len(fake.calls)
    with pytest.raises(MailValidationError):
        client(settings, fake).search_mail(cursor=invalid)
    assert len(fake.calls) == calls


def test_search_batches_page_headers_structure_and_dates(settings):
    fake = chronological_mailbox()
    found = client(settings, fake).search_mail(limit=3)
    assert len(found["messages"]) == 3
    page_fetches = [
        call for call in fake.calls if call[0] == "FETCH" and "BODYSTRUCTURE" in call[2]
    ]
    assert len(page_fetches) == 1
    assert set(page_fetches[0][1].split(",")) == {"14187", "10357", "2894"}
    assert "BODY.PEEK[HEADER.FIELDS (" in page_fetches[0][2]
    assert "INTERNALDATE" in page_fetches[0][2]
    assert not any(call[0] == "FETCH" and call[2] == "(BODYSTRUCTURE)" for call in fake.calls)
    first_fetch = next(call for call in fake.calls if call[0] == "FETCH")
    assert first_fetch[2] == "(UID INTERNALDATE FLAGS)"


def test_search_reuses_filtered_headers_when_snippets_are_disabled(settings):
    fake = chronological_mailbox()
    result = client(settings, fake).search_mail(subject="Hello", limit=2, include_snippets=False)
    assert len(result["messages"]) == 2 and result["next_cursor"]
    fetches = [call for call in fake.calls if call[0] == "FETCH"]
    assert len(fetches) == 1
    assert "BODY.PEEK[HEADER.FIELDS" in fetches[0][2]
    assert "BODYSTRUCTURE" not in fetches[0][2]


@pytest.mark.parametrize("malformed", ["structure", "base64", "size", "disposition"])
def test_search_malformed_optional_snippet_preserves_page_and_other_snippets(settings, malformed):
    structures = {
        "structure": b"()",
        "base64": PLAIN.replace(b'"7BIT"', b'"BASE64"'),
        "size": PLAIN.replace(b"24 2", b"NIL 2"),
        "disposition": PLAIN.replace(b"2 NIL NIL NIL)", b"2 NIL () NIL)"),
    }
    bad = message(
        "<bad@example.com>",
        structure=structures[malformed],
        sections={"1": b"a!!!"},
    )
    fake = FakeImap({"INBOX": {"3": bad, "2": message("<good@example.com>"), "1": bad}})
    result = client(settings, fake).search_mail(limit=2)
    assert [item["uid"] for item in result["messages"]] == ["3", "2"]
    assert result["next_cursor"] and result["total"] == 3
    assert result["messages"][0]["snippet_omitted"] is True
    assert result["messages"][0]["snippet"] == ""
    assert result["messages"][1]["snippet_omitted"] is False
    assert result["messages"][1]["snippet"] == "Hello plain text"


def test_search_date_window_uses_local_decoded_header_filters(settings):
    fake = FakeImap(
        {
            "INBOX": {
                "1": message("<yes@example.com>", subject="A Café receipt"),
                "2": message("<no@example.com>", subject="Other"),
            }
        }
    )
    found = client(settings, fake).search_mail(
        subject="CAFÉ",
        from_="SENDER@EXAMPLE.COM",
        to="READER@EXAMPLE.COM",
        since="2026-10-01",
        before="2026-11-01",
        include_snippets=False,
    )
    assert [item["uid"] for item in found["messages"]] == ["1"]
    searches = [call for call in fake.calls if call[0] == "SEARCH"]
    assert searches == [
        ("SEARCH", "CHARSET", "UTF-8", "SINCE", "01-Oct-2026", "BEFORE", "01-Nov-2026")
    ]
    assert fake.search_literals == []


def test_search_text_places_dates_first_and_uses_one_unicode_literal(settings):
    fake = FakeImap(
        {"INBOX": {"1": message("<yes@example.com>", sections={"1": "Crème".encode()})}}
    )
    result = client(settings, fake).search_mail(
        text="Crème", since="2026-10-01", include_snippets=False
    )
    assert result["total"] == 1
    assert next(call for call in fake.calls if call[0] == "SEARCH") == (
        "SEARCH",
        "CHARSET",
        "UTF-8",
        "SINCE",
        "01-Oct-2026",
        "TEXT",
    )
    assert fake.search_literals == ["Crème".encode()]


def test_search_deadline_retains_page_headers_and_next_cursor(settings, monkeypatch):
    fake = chronological_mailbox()
    reader = client(settings, fake)

    def deadline_reached(*args, **kwargs):
        raise CallDeadlineExceeded

    monkeypatch.setattr(reader, "_body", deadline_reached)
    result = reader.search_mail(limit=2)
    assert result["partial"] is True
    assert result["partial_reason"] == "timed_out"
    assert [item["uid"] for item in result["messages"]] == ["14187", "10357"]
    assert result["total"] == 5 and result["total_is_exact"] is True
    assert result["next_cursor"]
    assert all(item["snippet_omitted"] for item in result["messages"])


def test_search_deadline_during_page_fetch_retains_locally_filtered_headers(settings, monkeypatch):
    fake = chronological_mailbox()
    reader = client(settings, fake)
    metadata = reader._metadata

    def stop_before_page(connection, uids, **kwargs):
        if kwargs.get("bodystructure"):
            raise CallDeadlineExceeded
        return metadata(connection, uids, **kwargs)

    monkeypatch.setattr(reader, "_metadata", stop_before_page)
    result = reader.search_mail(subject="Hello", limit=2)
    assert result["partial"] is True
    assert result["partial_reason"] == "timed_out"
    assert len(result["messages"]) == 2
    assert result["total"] == 5 and result["total_is_exact"] is True


def test_search_snippet_work_is_capped_and_can_be_disabled(settings):
    fake = FakeImap({"INBOX": {str(uid): message(f"<{uid}@example.com>") for uid in range(1, 26)}})
    result = client(settings, fake).search_mail(limit=25)
    assert len(result["messages"]) == 25
    assert sum(not item["snippet_omitted"] for item in result["messages"]) == 20
    body_fetches = [
        call for call in fake.calls if call[0] == "FETCH" and call[2].startswith("(BODY.PEEK[1]")
    ]
    assert len(body_fetches) == 20
    fake.calls.clear()
    client(settings, fake).search_mail(limit=25, include_snippets=False)
    assert not any(
        call[0] == "FETCH" and ("BODYSTRUCTURE" in call[2] or call[2].startswith("(BODY.PEEK[1]"))
        for call in fake.calls
    )


def test_search_socket_timeout_during_scan_stops_io_and_preserves_cached_matches(
    settings, monkeypatch
):
    fake = FakeImap({"INBOX": {str(uid): message(f"<{uid}@example.com>") for uid in range(1, 502)}})
    reader = client(settings, fake)
    metadata = reader._metadata
    attempts = 0

    def interrupted_scan(connection, uids, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise CallDeadlineExceeded("Mailbox call timed out")
        if attempts > 2:
            raise AssertionError("A timed-out IMAP connection must not be reused")
        return metadata(connection, uids, **kwargs)

    monkeypatch.setattr(reader, "_metadata", interrupted_scan)
    result = reader.search_mail(subject="Hello", limit=2)
    assert attempts == 2
    assert result["partial"] is True and result["total_is_exact"] is False
    assert result["partial_reason"] == "timed_out"
    assert result["total"] == 500
    assert len(result["messages"]) == 2
    assert result["next_cursor"]
    assert all(item["snippet_omitted"] for item in result["messages"])


def test_search_missing_candidate_fetch_records_mark_total_inexact(settings, monkeypatch):
    fake = chronological_mailbox()
    reader = client(settings, fake)
    metadata = reader._metadata

    def missing_candidate(connection, uids, **kwargs):
        result = metadata(connection, uids, **kwargs)
        if not kwargs.get("bodystructure"):
            result.pop("20101", None)
        return result

    monkeypatch.setattr(reader, "_metadata", missing_candidate)
    result = reader.search_mail(limit=2)
    assert result["partial"] is True and result["total_is_exact"] is False
    assert result["partial_reason"] == "metadata_missing"
    assert result["total"] == 4 and len(result["messages"]) == 2
    assert result["next_cursor"]


def test_search_disappearing_page_messages_do_not_claim_complete_page(settings, monkeypatch):
    fake = chronological_mailbox()
    reader = client(settings, fake)
    metadata = reader._metadata

    def missing_page_message(connection, uids, **kwargs):
        result = metadata(connection, uids, **kwargs)
        if kwargs.get("bodystructure"):
            result.pop("14187", None)
        return result

    monkeypatch.setattr(reader, "_metadata", missing_page_message)
    result = reader.search_mail(subject="Hello", limit=2)
    assert result["partial"] is True and result["total_is_exact"] is False
    assert result["partial_reason"] == "metadata_missing"
    assert [item["uid"] for item in result["messages"]] == ["10357"]
    assert result["total"] == 4
    assert result["next_cursor"]
