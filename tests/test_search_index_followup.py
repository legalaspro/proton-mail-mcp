"""Reported large-account searches, using only read-only in-memory IMAP."""

from email import policy
from email.parser import BytesParser

import pytest
from test_header_search_index import indexed_message
from test_mail import client
from test_mail import settings as settings
from test_search_index import CoverageImap, header_uids

from proton_mail_mcp import canonical
from proton_mail_mcp.mail import CallDeadlineExceeded, MailError


def warm_real(reader, fake, folder="INBOX"):
    deadline = reader._deadline()
    with reader._session(deadline=deadline) as (connection, _):
        state = reader._select(connection, folder)
        uids = reader._search(connection, ["ALL"])
        list(reader._index_header_batches(connection, uids, deadline=deadline, batch_size=500))
        assert reader.search_index.record_coverage(folder, "1", state, uids, account=False)
    fake.calls.clear()


@pytest.mark.parametrize("folder", ["INBOX", "Folders/Reviewed"])
def test_all_mail_headers_filter_large_folder_membership_and_page_own_uids(settings, folder):
    all_mail = {
        str(uid): indexed_message(
            f"<message-{uid}@example.invalid>",
            sender="info@studio.example.com" if uid <= 9 else "other@example.invalid",
        )
        for uid in range(1, 1206)
    }
    # One matching All Mail message is in Archive, outside INBOX's inventory.
    inbox = {str(int(uid) + 4000): item for uid, item in all_mail.items() if uid != "9"}
    fake = CoverageImap(
        {"All Mail": all_mail, folder: inbox, "Archive": {"91": all_mail["9"]}},
        uidnext=True,
    )
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    fake.calls.clear()
    filters = dict(from_="info@studio.example.com", limit=5, include_snippets=False)
    if folder != "INBOX":
        filters["folder"] = folder

    first = reader.search_mail(**filters)
    assert first["total"] == 8 and first["total_is_exact"] and not first["partial"]
    assert [row["uid"] for row in first["messages"]] == list(map(str, range(4008, 4003, -1)))
    assert first["next_cursor"]
    assert header_uids(fake) == set(map(str, range(4004, 4009)))
    assert all(row["folder"] == folder and row["uidvalidity"] == "1" for row in first["messages"])

    fake.calls.clear()
    last = reader.search_mail(cursor=first["next_cursor"], **filters)
    assert [row["uid"] for row in last["messages"]] == ["4003", "4002", "4001"]
    assert last["total"] == 8 and not last["partial"] and last["next_cursor"] is None
    assert header_uids(fake) == {"4001", "4002", "4003"}


def test_large_recipient_match_selects_page_before_fetch_and_resolves_handles(settings):
    all_mail = {
        str(uid): indexed_message(
            f"<recipient-{uid}@example.invalid>",
            recipient="recipient@example.com",
            internaldate=(
                "14-Oct-2026 10:00:00 +0000" if uid <= 5 else "01-Jan-2020 09:00:00 +0000"
            ),
        )
        for uid in range(1, 1506)
    }
    fake = CoverageImap(
        {
            "All Mail": all_mail,
            "INBOX": {str(int(uid) + 5000): item for uid, item in all_mail.items()},
        },
        uidnext=True,
    )
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    warm_real(reader, fake)
    filters = dict(folder="All Mail", to="recipient@example.com", limit=5, include_snippets=False)

    first = reader.search_mail(**filters)
    assert first["total"] == 1505 and first["total_is_exact"] and not first["partial"]
    assert [row["alias"]["uid"] for row in first["messages"]] == ["5", "4", "3", "2", "1"]
    assert [row["uid"] for row in first["messages"]] == ["5005", "5004", "5003", "5002", "5001"]
    assert all(row["folder"] == "INBOX" and row["uidvalidity"] == "1" for row in first["messages"])
    fetched = {uid for call in fake.calls if call[0] == "FETCH" for uid in call[1].split(",")}
    assert fetched == {"1", "2", "3", "4", "5"}
    assert first["next_cursor"]

    fake.calls.clear()
    second = reader.search_mail(cursor=first["next_cursor"], **filters)
    assert [row["alias"]["uid"] for row in second["messages"]] == list(
        map(str, range(1505, 1500, -1))
    )
    assert second["total"] == 1505 and second["total_is_exact"] and not second["partial"]
    fetched = {uid for call in fake.calls if call[0] == "FETCH" for uid in call[1].split(",")}
    assert fetched == set(map(str, range(1501, 1506)))


def test_unresolvable_search_rows_are_dropped_with_explicit_partial_flag(settings):
    fake = CoverageImap(
        {"All Mail": {"7": indexed_message("<orphan@example.invalid>")}}, uidnext=True
    )
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")

    result = reader.search_mail(folder="All Mail", include_snippets=False)

    assert result["messages"] == [] and result["partial"]
    assert result["total"] == 1 and result["total_is_exact"]
    assert result["next_cursor"]
    assert result["partial_reason"] == "unresolved_handles"
    assert "retryable" not in result


@pytest.mark.parametrize("folder", ["All Mail", "Labels/Reviewed", "Starred"])
@pytest.mark.parametrize("later_page", [False, True], ids=["first-page", "cursor-page"])
@pytest.mark.parametrize(
    "failure", [MailError, CallDeadlineExceeded], ids=["lookup-error", "timeout"]
)
def test_unresolved_view_page_explicitly_retries_the_same_request(
    settings, monkeypatch, folder, later_page, failure
):
    items = {str(uid): indexed_message(f"<retry-{uid}@example.invalid>") for uid in range(1, 5)}
    folders = {
        "All Mail": items,
        "INBOX": {str(int(uid) + 4000): item for uid, item in items.items()},
    }
    if folder != "All Mail":
        folders[folder] = items
    fake = CoverageImap(folders, folder_flags={"Starred": r"\Flagged"}, uidnext=True)
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    warm_real(reader, fake)
    filters = dict(folder=folder, limit=1, include_snippets=False)
    expected = "4"
    if later_page:
        first = reader.search_mail(**filters)
        filters["cursor"] = first["next_cursor"]
        assert filters["cursor"]
        expected = "3"

    with monkeypatch.context() as failed:

        def fail_lookup(*args, **kwargs):
            raise failure("Simulated canonical lookup failure")

        failed.setattr(canonical, "resolve_views", fail_lookup)
        incomplete = reader.search_mail(**filters)

    assert incomplete["messages"] == []
    assert incomplete["partial"]
    assert "retryable" not in incomplete
    assert incomplete["next_cursor"]
    assert incomplete["total"] == 4 and incomplete["total_is_exact"]
    assert incomplete["partial_reason"] == "canonical_unresolved"
    assert bool(incomplete.get("timed_out")) is (failure is CallDeadlineExceeded)

    retried = reader.search_mail(**filters)
    assert not retried["partial"]
    assert [item["alias"]["uid"] for item in retried["messages"]] == [expected]
    assert retried["next_cursor"]


def test_legacy_complete_index_warms_ordering_once_and_then_fetches_only_page(settings):
    items = {str(uid): indexed_message(f"<legacy-{uid}@example.invalid>") for uid in range(1, 1206)}
    fake = CoverageImap(
        {
            "All Mail": items,
            "INBOX": {str(int(uid) + 5000): item for uid, item in items.items()},
        },
        uidnext=True,
    )
    reader = client(settings, fake)
    reader.search_index.observe(
        "All Mail",
        "1",
        {
            uid: {"headers": BytesParser(policy=policy.default).parsebytes(item.headers)}
            for uid, item in items.items()
        },
    )
    assert reader.search_index.record_coverage(
        "All Mail", "1", {"exists": 1205, "uidnext": "1206"}, list(items)
    )
    warm_real(reader, fake)
    filters = dict(folder="All Mail", to="reader@example.com", limit=5, include_snippets=False)

    first = reader.search_mail(**filters)
    assert first["total"] == 1205 and first["total_is_exact"] and not first["partial"]
    assert first["next_cursor"]
    assert len(first["messages"]) == 5
    assert len(reader.search_index.cached_metadata("All Mail", "1", list(items))) == 1205

    fake.calls.clear()
    second = reader.search_mail(cursor=first["next_cursor"], **filters)
    assert second["total"] == 1205 and second["total_is_exact"] and not second["partial"]
    assert [row["uid"] for row in second["messages"]] == list(map(str, range(6200, 6195, -1)))
    fetched = {uid for call in fake.calls if call[0] == "FETCH" for uid in call[1].split(",")}
    assert fetched == set(map(str, range(1196, 1201)))
