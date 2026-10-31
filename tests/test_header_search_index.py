"""Complete header indexes filter whole folders before bounded mailbox work."""

from dataclasses import replace

import pytest
from test_mail import client, message
from test_mail import settings as settings
from test_search_index import CoverageImap, header_uids


@pytest.fixture(autouse=True)
def canonical_handles(monkeypatch):
    # These unit tests isolate header matching and metadata fetch volume. Real
    # canonical resolution, including duplicate safety, has its own simulator
    # coverage in test_canonical_identity.py.
    def resolve(client, connection, deadline, handles, **kwargs):
        return [{**handle, "folder": "INBOX"} for handle in handles]

    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_views", resolve)


def assert_handles(result):
    assert all(
        all(row.get(field) is not None for field in ("folder", "uidvalidity", "uid"))
        for row in result["messages"]
    )


def indexed_message(identifier, *, sender="info@studio.example", recipient=None, **kwargs):
    item = message(identifier, **kwargs)
    item.headers = item.headers.replace(b"Sender <sender@example.com>", sender.encode())
    if recipient is not None:
        item.headers = item.headers.replace(b"reader@example.com", recipient.encode())
    return item


def metadata_uids(fake):
    return {
        uid
        for call in fake.calls
        if call[0] == "FETCH" and "INTERNALDATE" in call[2]
        for uid in call[1].split(",")
    }


def complete_reader(settings, mailbox):
    fake = CoverageImap({"All Mail": mailbox}, uidnext=True)
    assert client(settings, fake).rebuild_search_index("All Mail")["indexed"] == len(mailbox)
    fake.calls.clear()
    # Exercise persisted coverage and decoded headers, not just in-memory state.
    return client(settings, fake), fake


@pytest.mark.parametrize("sender", ["INFO@STUDIO.EXAMPLE", "STUDIO.EXAMPLE"])
def test_complete_header_index_finds_only_old_sender_matches_beyond_1000(settings, sender):
    background = indexed_message(
        "<background@example.invalid>", sender="other@elsewhere.example", subject="Background"
    )
    mailbox = {str(uid): background for uid in range(8, 1211)}
    mailbox.update(
        {
            str(uid): indexed_message(
                f"<old-{uid}@example.invalid>",
                internaldate="01-Jan-2020 09:00:00 +0000",
            )
            for uid in range(1, 8)
        }
    )
    reader, fake = complete_reader(settings, mailbox)

    result = reader.search_mail(folder="All Mail", from_=sender, limit=100, include_snippets=False)

    assert_handles(result)
    assert [row["alias"]["uid"] for row in result["messages"]] == list(map(str, range(7, 0, -1)))
    assert result["total"] == 7 and result["total_is_exact"] is True
    assert result["partial"] is False and result["next_cursor"] is None
    assert "partial_reason" not in result
    assert "applied_since" not in result
    assert metadata_uids(fake) == set(map(str, range(1, 8)))


@pytest.mark.parametrize("term", ["fomc", "OM"])
def test_complete_subject_index_filters_all_headers_before_metadata(settings, term):
    background = indexed_message("<background@example.invalid>", subject="Background")
    mailbox = {str(uid): background for uid in range(6, 1206)}
    mailbox.update(
        {
            str(uid): indexed_message(
                f"<fomc-{uid}@example.invalid>",
                subject="FOMC minutes",
                internaldate="01-Jan-2020 09:00:00 +0000",
            )
            for uid in range(1, 6)
        }
    )
    reader, fake = complete_reader(settings, mailbox)

    result = reader.search_mail(folder="All Mail", subject=term, include_snippets=False)

    assert_handles(result)
    assert {row["message_id"] for row in result["messages"]} == {
        f"<fomc-{uid}@example.invalid>" for uid in range(1, 6)
    }
    assert result["total"] == 5 and result["total_is_exact"] is True
    assert result["partial"] is False and result["next_cursor"] is None
    assert "partial_reason" not in result
    # Short substrings also filter nonmatches without expensive per-message
    # dates/flags or header reads.
    assert metadata_uids(fake) == set(map(str, range(1, 6)))


def test_complete_header_filtered_matches_above_cap_keep_full_cursor_paging(settings):
    background = indexed_message(
        "<background@example.invalid>", sender="other@elsewhere.example", subject="Background"
    )
    hit = indexed_message(
        "<fomc@example.invalid>", subject="FOMC", internaldate="01-Jan-2020 09:00:00 +0000"
    )
    mailbox = {str(uid): hit for uid in range(1, 1004)}
    mailbox.update({str(uid): background for uid in range(1004, 2204)})
    reader, fake = complete_reader(settings, mailbox)
    filters = dict(folder="All Mail", from_="studio.example", subject="fomc", limit=100)

    result = reader.search_mail(include_snippets=False, **filters)
    found = []
    for page_number in range(11):
        assert_handles(result)
        assert result["total"] == 1003 and result["total_is_exact"] is True
        assert result["partial"] is False
        assert "partial_reason" not in result
        found.extend(row["alias"]["uid"] for row in result["messages"])
        cursor = result["next_cursor"]
        if page_number == 10:
            assert cursor is None
        else:
            assert cursor, "Every remaining indexed match must be reachable by cursor"
            result = reader.search_mail(cursor=cursor, include_snippets=False, **filters)

    assert found == list(map(str, range(1003, 0, -1)))
    assert metadata_uids(fake) == set(map(str, range(1, 1004)))


@pytest.mark.parametrize(
    "filters,expected",
    [
        pytest.param({"exclude_subject": "Background"}, {"1", "2", "3"}, id="exclusion-only"),
        pytest.param(
            {"query": {"not": {"subject": "Background"}}},
            {"1", "2", "3"},
            id="query-not",
        ),
        pytest.param({"to": "TARGET.EXAMPLE"}, {"1", "2"}, id="recipient-domain"),
        pytest.param({"subject": "STRASSE"}, {"3"}, id="unicode-casefold"),
        pytest.param({"subject": "%_"}, {"1"}, id="literal-like-wildcards"),
        pytest.param(
            {"query": {"or": [{"subject": "%_"}, {"subject": "STRASSE"}]}},
            {"1", "3"},
            id="query-or",
        ),
    ],
)
def test_all_header_filter_forms_prune_before_metadata(settings, filters, expected):
    background = indexed_message("<background@example.invalid>", subject="Background")
    mailbox = {str(uid): background for uid in range(4, 1204)}
    mailbox.update(
        {
            "1": indexed_message(
                "<wildcard@example.invalid>",
                subject="FOMC 100%_final",
                recipient="a@target.example",
            ),
            "2": indexed_message(
                "<other@example.invalid>", subject="FOMC 100xxfinal", recipient="b@target.example"
            ),
            "3": indexed_message("<unicode@example.invalid>", subject="Die Straße"),
        }
    )
    reader, fake = complete_reader(settings, mailbox)

    result = reader.search_mail(folder="All Mail", include_snippets=False, **filters)

    assert_handles(result)
    assert {row["alias"]["uid"] for row in result["messages"]} == expected
    assert result["total"] == len(expected) and result["total_is_exact"] is True
    assert result["partial"] is False
    assert metadata_uids(fake) == expected


def test_indexed_header_matches_still_apply_live_unread_and_internaldate_bounds(settings):
    background = indexed_message(
        "<background@example.invalid>",
        subject="Background",
        internaldate="09-Jan-2020 09:00:00 +0000",
    )
    mailbox = {str(uid): background for uid in range(6, 1206)}
    mailbox.update(
        {
            str(uid): indexed_message(
                f"<fomc-{uid}@example.invalid>",
                recipient="Team <alerts@target.example>",
                subject="FOMC cancelled" if uid == 5 else "FOMC minutes",
                internaldate=f"{day:02d}-Jan-2020 09:00:00 +0000",
                flags={r"\Seen"} if uid == 1 else set(),
            )
            for uid, day in ((1, 9), (2, 9), (3, 1), (4, 20), (5, 9))
        }
    )
    reader, fake = complete_reader(settings, mailbox)
    # Headers are immutable for this epoch, while flags can change after indexing.
    mailbox["1"].flags.clear()
    mailbox["2"].flags.add(r"\Seen")

    result = reader.search_mail(
        folder="All Mail",
        from_="STUDIO.EXAMPLE",
        to="TARGET.EXAMPLE",
        subject="fomc",
        exclude_subject="CANCELLED",
        unread_only=True,
        since="2020-01-05",
        before="2020-01-15",
        include_snippets=False,
    )

    assert_handles(result)
    assert [row["message_id"] for row in result["messages"]] == ["<fomc-1@example.invalid>"]
    assert result["messages"][0]["seen"] is False
    assert result["total"] == 1 and result["total_is_exact"] is True
    assert result["partial"] is False and result["next_cursor"] is None
    assert metadata_uids(fake) == {"1"}


@pytest.mark.parametrize("incomplete", [False, True], ids=["stale", "incomplete"])
def test_stale_or_incomplete_headers_keep_capped_fallback(settings, monkeypatch, incomplete):
    settings = replace(settings, search_index_catchup_max_messages=0)
    background = indexed_message("<background@example.invalid>", subject="Background")
    fake = CoverageImap({"All Mail": {"1": background}}, uidnext=True)
    reader = client(settings, fake)
    if incomplete:
        reader.search_index.observe("All Mail", "1", {"1": {"headers": {"Subject": "Background"}}})
    else:
        reader.rebuild_search_index("All Mail")
    hit = indexed_message("<new-fomc@example.invalid>", subject="FOMC")
    fake.folders["All Mail"].update({str(uid): hit for uid in range(2, 1004)})
    fake.calls.clear()
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-09-17")

    result = reader.search_mail(folder="All Mail", subject="FOMC", include_snippets=False)

    assert_handles(result)
    assert result["total"] == 1000 and result["total_is_exact"] is False
    assert result["partial"] is True and result["next_cursor"]
    assert result["partial_reason"] == "candidate_cap"
    assert header_uids(fake) == set(map(str, range(4, 1004)))


def test_fresh_all_mail_headers_map_onto_the_folders_own_inventory(settings):
    mailbox = {
        str(uid): indexed_message(
            f"<background-{uid}@example.invalid>",
            sender="other@elsewhere.example",
            subject="Background",
        )
        for uid in range(2, 1202)
    }
    mailbox["1"] = indexed_message("<old-sender@example.invalid>")
    inbox = {str(int(uid) + 4000): item for uid, item in mailbox.items()}
    fake = CoverageImap({"All Mail": mailbox, "INBOX": inbox}, uidnext=True)
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    fake.calls.clear()

    result = reader.search_mail(from_="studio.example", include_snippets=False)

    assert_handles(result)
    [hit] = result["messages"]
    assert (hit["folder"], hit["uidvalidity"], hit["uid"]) == ("INBOX", "1", "4001")
    assert "alias" not in hit
    assert result["partial"] is False
    assert result["total"] == 1 and result["total_is_exact"] is True
    assert result["next_cursor"] is None
    assert "partial_reason" not in result
    assert header_uids(fake) == {"4001"}
