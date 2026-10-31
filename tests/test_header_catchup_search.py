"""Search-level header catch-up resumes and preserves the requested mailbox."""

from dataclasses import replace

from gluon_search import GluonSearchImap
from test_header_catchup_incremental import assert_read_only, header_fetches, range_searches
from test_mail import client, message
from test_mail import settings as settings


def test_search_mail_incomplete_growth_uses_window_then_resumes_without_refetch(
    settings, monkeypatch
):
    settings = replace(settings, search_index_catchup_max_messages=1)
    old_hit = message(
        "<old-match@test.invalid>",
        subject="Historical match",
        internaldate="01-Jan-2020 00:00:00 +0000",
    )
    fake = GluonSearchImap({"All Mail": {"1": old_hit}}, uidnext=True)
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    fake.folders["All Mail"].update(
        {
            str(uid): message(
                f"<arrival-{uid}@test.invalid>",
                subject="Other topic",
                internaldate="01-Jan-2020 00:00:00 +0000",
            )
            for uid in (2, 3)
        }
    )
    fake.calls.clear()
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-09-29")

    def resolve(client, connection, deadline, handles, **kwargs):
        return [{**handle, "folder": "INBOX"} for handle in handles]

    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_views", resolve)

    first = reader.search_mail(folder="All Mail", subject="match", include_snippets=False)

    assert first["messages"] == []
    assert first["partial"] is True and first["total_is_exact"] is False
    assert first["applied_since"] == "2026-09-29"
    assert first["partial_reason"] == "no_resumable_cursor"
    assert "2/3 messages indexed (66.7%)" in first["index_note"]
    assert first["index_status"]["state"] == "incomplete"
    assert first["index_status"]["indexed_count"] == 2
    assert first["index_status"]["target"] == 3
    assert first["index_status"]["percent"] == 66.7
    assert reader.search_index.coverage_epoch("All Mail") == ("1", 1, "2")
    assert reader.search_index.known_uids("All Mail", "1") == {"1", "2"}
    assert [call[1] for call in header_fetches(fake)] == ["2"]
    assert [call[3:] for call in fake.calls if call[0] == "SEARCH"] == [
        ("UID", "2:3"),
        ("SINCE", "29-Sep-2026"),
    ]
    assert_read_only(fake)
    fake.calls.clear()

    resumed = reader.search_mail(folder="All Mail", subject="match", include_snippets=False)

    assert [item["message_id"] for item in resumed["messages"]] == ["<old-match@test.invalid>"]
    assert resumed["partial"] is False and resumed["total_is_exact"] is True
    assert "applied_since" not in resumed
    assert "partial_reason" not in resumed
    assert "index_note" not in resumed and "index_status" not in resumed
    assert reader.search_index.coverage("All Mail") == ("1", 3, "4")
    assert range_searches(fake) == [("SEARCH", "CHARSET", "UTF-8", "UID", "2:3")]
    # The historical match needs headers for its response summary, while the
    # catch-up only fetches UID 3. UID 2's retained progress is never fetched again.
    fetched = header_fetches(fake)
    # The arrival's catch-up, then the page, which also stores its fingerprint.
    assert [call[1] for call in fetched if "ENVELOPE" in call[2]] == ["3", "1"]
    assert "2" not in {uid for call in fetched for uid in call[1].split(",")}
    assert not any("SINCE" in call for call in fake.calls if call[0] == "SEARCH")
    assert_read_only(fake, enriched=False)


class SelectionImap(GluonSearchImap):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commands_by_folder = []

    def uid(self, command, *args):
        assert self._search_selection == (self.current, self.uidvalidity)
        self.commands_by_folder.append((self.current, command, *args))
        return super().uid(command, *args)


def test_inbox_header_search_catches_up_growing_all_mail_and_restores_selection(settings):
    old = message("<old@test.invalid>", subject="Indexed old mail")
    fake = SelectionImap({"All Mail": {"1": old}, "INBOX": {"10": old}}, uidnext=True)
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    arrival = message("<arrival@test.invalid>", subject="New matching mail")
    fake.folders["All Mail"]["2"] = arrival
    fake.folders["INBOX"]["20"] = arrival
    fake.calls.clear()
    fake.commands_by_folder.clear()

    found = reader.search_mail(folder="INBOX", subject="matching", include_snippets=False)

    assert [item["message_id"] for item in found["messages"]] == ["<arrival@test.invalid>"]
    assert found["messages"][0]["folder"] == "INBOX"
    assert found["messages"][0]["uid"] == "20"
    assert "partial_reason" not in found
    assert found["partial"] is False and found["total_is_exact"] is True
    assert reader.search_index.coverage("All Mail") == ("1", 2, "3")
    assert [call[1] for call in fake.calls if call[0] == "EXAMINE"] == [
        '"INBOX"',
        '"All Mail"',
        '"INBOX"',
    ]
    assert fake.current == "INBOX" and fake._search_selection == ("INBOX", "1")
    assert [call for call in fake.commands_by_folder if call[1] == "SEARCH"] == [
        ("All Mail", "SEARCH", "CHARSET", "UTF-8", "UID", "2:2"),
        ("INBOX", "SEARCH", "CHARSET", "UTF-8", "ALL"),
    ]
    # Both the resumed folder inventory and result metadata use INBOX's UID
    # space; the catch-up's enriched header fetch stays in All Mail, and the
    # page's own fetch stores its fingerprint in INBOX.
    enriched = [
        call
        for call in fake.commands_by_folder
        if call[1] == "FETCH" and "HEADER.FIELDS" in call[3] and "ENVELOPE" in call[3]
    ]
    assert [(call[0], call[2]) for call in enriched] == [("All Mail", "2"), ("INBOX", "20")]
    assert_read_only(fake, enriched=False)
