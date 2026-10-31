"""Offline regressions for sender-search bounds and account index maintenance."""

from dataclasses import replace

import pytest
from test_mail import client, message
from test_mail import settings as settings
from test_search_index import CoverageImap, header_uids

from proton_mail_mcp.mail import CallDeadlineExceeded
from proton_mail_mcp.search_index import SearchIndex


@pytest.fixture(autouse=True)
def canonical_handles(monkeypatch):
    # Keep these sender/index regressions independent of real-folder discovery;
    # test_canonical_identity.py exercises actual copy resolution.
    def resolve(client, connection, deadline, handles, **kwargs):
        return [{**handle, "folder": "INBOX"} for handle in handles]

    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_views", resolve)


def studio_message(identifier, *, sender="info@studio.example", **kwargs):
    item = message(identifier, **kwargs)
    item.headers = item.headers.replace(b"sender@example.com", sender.encode())
    return item


@pytest.mark.parametrize("sender", ["info@studio.example", "studio.example"])
def test_from_only_after_restart_uses_fresh_index_before_bounded_fetch(
    settings, monkeypatch, sender
):
    fake = CoverageImap(
        {
            "All Mail": {
                str(uid): studio_message(
                    f"<{uid}@example.invalid>",
                    sender="info@studio.example" if uid == 1 else "other@elsewhere.example",
                )
                for uid in range(1, 1201)
            }
        },
        uidnext=True,
    )
    client(settings, fake).rebuild_search_index("All Mail")
    reader = client(settings, fake)
    fake.calls.clear()
    # Pruning must happen before this bound, even if the only hit has the lowest UID.
    monkeypatch.setattr("proton_mail_mcp.search._MAX_SEARCH_CANDIDATES", 5)
    result = reader.search_mail(folder="All Mail", from_=sender, limit=1, include_snippets=False)
    assert [row["message_id"] for row in result["messages"]] == ["<1@example.invalid>"]
    assert result["total"] == 1 and result["total_is_exact"] and not result["partial"]
    assert header_uids(fake) == {"1"}
    assert not any(call[0] == "SEARCH" for call in fake.calls)
    assert sum(call[0] == "STATUS" for call in fake.calls) == 1


def test_sender_matches_are_capped_before_fetch_with_honest_partial_totals(settings, monkeypatch):
    fake = CoverageImap(
        {"INBOX": {str(uid): studio_message(f"<{uid}@example.invalid>") for uid in range(1, 13)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    monkeypatch.setattr("proton_mail_mcp.search._MAX_SEARCH_CANDIDATES", 5)
    result = reader.search_mail(from_="studio.example", limit=2, include_snippets=False)
    assert len(result["messages"]) == 2 and result["total"] == 5
    assert result["partial"] and not result["total_is_exact"]
    assert result["next_cursor"]
    assert header_uids(fake) == {"8", "9", "10", "11", "12"}
    assert result["partial_reason"] == "candidate_cap"


@pytest.mark.parametrize("unknown_count", [0, 3, 8])
def test_sender_cap_only_counts_uncached_headers(settings, monkeypatch, unknown_count):
    mailbox = {
        str(uid): studio_message(f"<{uid}@example.invalid>") for uid in range(1, 13 + unknown_count)
    }
    fake = CoverageImap({"INBOX": mailbox}, uidnext=True)
    reader = client(settings, fake)
    # Lower UIDs remain searchable even when new uncached UIDs exceed the cap.
    reader.search_index.observe(
        "INBOX",
        "1",
        {str(uid): {"headers": {"From": "info@studio.example"}} for uid in range(1, 13)},
    )
    monkeypatch.setattr("proton_mail_mcp.search._MAX_SEARCH_CANDIDATES", 5)
    result = reader.search_mail(from_="studio.example", limit=2, include_snippets=False)
    limited = unknown_count > 5
    assert result["total"] == 12 + min(unknown_count, 5)
    assert result["total_is_exact"] is not limited
    assert result["partial"] is limited
    assert result["next_cursor"]
    # Cached matching rows only need date/flag reads; headers are fetched for
    # uncached rows and any returned page summaries missing their raw headers.
    expected_headers = {str(uid) for uid in range(13, 13 + unknown_count)}
    if limited:
        expected_headers = {str(uid) for uid in range(16, 21)}
        assert result["partial_reason"] == "candidate_cap"
    elif not unknown_count:
        expected_headers = {"11", "12"}
    assert header_uids(fake) == expected_headers


def test_sender_search_timeout_is_clear_and_does_not_issue_header_search(settings, monkeypatch):
    fake = CoverageImap({"INBOX": {"1": studio_message("<one@example.invalid>")}}, uidnext=True)
    reader = client(settings, fake)

    def timeout(connection, criteria, **kwargs):
        assert criteria == ["ALL"]  # Never use Bridge's expensive FROM scan.
        raise CallDeadlineExceeded("test peer stopped")

    monkeypatch.setattr(reader, "_search", timeout)
    result = reader.search_mail(from_="studio.example", include_snippets=False)
    assert result["timed_out"] is True and result["partial"] is True
    assert result["messages"] == [] and not result["total_is_exact"]
    assert result["partial_reason"] == "timed_out"
    assert "Search timed out; narrow the date range" in result["error"]
    assert "test peer stopped" not in str(result)


@pytest.mark.parametrize("folder", ["INBOX", "All Mail"])
def test_every_folder_maintains_account_index_for_arrivals_before_the_request(settings, folder):
    shared = studio_message("<shared@example.invalid>")
    fake = CoverageImap({"INBOX": {"1": shared}, "All Mail": {"1": shared}}, uidnext=True)
    client(settings, fake).rebuild_search_index("All Mail")
    reader = client(settings, fake)
    first = reader.search_mail(folder=folder, include_snippets=False)
    assert first["total"] == 1 and first["total_is_exact"] and not first["partial"]

    fake.folders["All Mail"]["2"] = studio_message("<arrival@example.invalid>")
    result = reader.search_mail(folder=folder, include_snippets=False)

    assert reader.search_index.known_uids("All Mail", "1") == {"1", "2"}
    assert reader.search_index.coverage("All Mail") == ("1", 2, "3")
    expected = {"<shared@example.invalid>"}
    if folder == "All Mail":
        expected.add("<arrival@example.invalid>")
    assert {item["message_id"] for item in result["messages"]} == expected
    assert result["total"] == len(expected) and result["total_is_exact"] and not result["partial"]
    assert not {"search_index", "search_index_details", "retryable"} & result.keys()


def test_concurrent_coverage_proof_does_not_change_window_search_evidence(settings, monkeypatch):
    settings = replace(settings, search_index_catchup_max_messages=0)
    fake = CoverageImap(
        {"All Mail": {str(uid): studio_message(f"<{uid}@example.invalid>") for uid in (1, 2)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    metadata = reader._metadata
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-09-17")

    def prove_during_fetch(*args, **kwargs):
        result = metadata(*args, **kwargs)
        assert reader.search_index.record_coverage(
            "All Mail", "1", {"exists": 2, "uidnext": "3"}, ["1", "2"]
        )
        return result

    monkeypatch.setattr(reader, "_metadata", prove_during_fetch)
    result = reader.search_mail(folder="All Mail", include_snippets=False)
    assert result["partial"] and not result["total_is_exact"]
    assert result["applied_since"] == "2026-09-17"
    assert result["partial_reason"] == "no_resumable_cursor"


@pytest.mark.parametrize("mutation", ["remove", "nul", "epoch", "reconcile"])
def test_persisted_coverage_invalidates_across_clients(settings, mutation):
    fake = CoverageImap({"All Mail": {"1": studio_message("<one@example.invalid>")}}, uidnext=True)
    first = client(settings, fake)
    first.rebuild_search_index("All Mail")
    second = SearchIndex(settings)
    assert second.coverage("All Mail") == ("1", 1, "2")
    if mutation == "remove":
        first.search_index.remove("All Mail", "1", ["1"])
    elif mutation == "nul":
        first.search_index.observe("All Mail", "1", {"1": {"headers": {"From": "bad\x00header"}}})
    elif mutation == "epoch":
        first.search_index.select("All Mail", "2")
    else:
        first.search_index.reconcile("All Mail", "1", [])
    assert second.coverage("All Mail") is None


@pytest.mark.parametrize("folder", ["INBOX", "All Mail"])
def test_missing_epoch_evidence_keeps_complete_pages(settings, folder):
    shared = studio_message("<shared@example.invalid>")
    fake = CoverageImap({"INBOX": {"1": shared}, "All Mail": {"1": shared}}, uidnext=False)
    reader = client(settings, fake)
    reader.search_index.observe("All Mail", "1", {"1": {"headers": {"Subject": "Hello"}}})
    result = reader.search_mail(folder=folder, include_snippets=False)
    assert result["total"] == 1
    assert not result["partial"] and result["total_is_exact"]
    assert "partial_reason" not in result
    assert not {"search_index", "search_index_details", "retryable"} & result.keys()


def test_plain_inbox_fallback_and_warm_cursor_have_the_same_completeness(settings):
    items = {str(uid): studio_message(f"<{uid}@example.invalid>") for uid in (1, 2)}
    reader = client(settings, CoverageImap({"INBOX": items}, uidnext=True))
    first = reader.search_mail(limit=1, include_snippets=False)
    assert first["total"] == 2 and first["total_is_exact"] and not first["partial"]
    assert first["next_cursor"]
    second = reader.search_mail(limit=1, include_snippets=False, cursor=first["next_cursor"])
    assert second["total"] == 2 and second["total_is_exact"] and not second["partial"]
    assert {item["uid"] for item in first["messages"] + second["messages"]} == {"1", "2"}
