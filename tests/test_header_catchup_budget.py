"""Virtual setup latency exercises catch-up's real search_mail budget gate."""

import logging
from dataclasses import replace

import pytest
from gluon_search import GluonSearchImap
from test_mail import client, message
from test_mail import settings as settings


def header_fetches(fake):
    return [call for call in fake.calls if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]]


def assert_read_only(fake):
    assert all(call[2] is True for call in fake.calls if call[0] == "EXAMINE")
    assert not {"SELECT", "STORE", "APPEND", "EXPUNGE"}.intersection(call[0] for call in fake.calls)
    assert all("BODY.PEEK[HEADER.FIELDS" in call[2] for call in header_fetches(fake))
    assert all("BODY[" not in call[2] for call in fake.calls if call[0] == "FETCH")


def test_search_mail_catches_up_after_setup_consumes_old_admission_window(settings, monkeypatch):
    clock = [1000.0]

    class SlowSelectionImap(GluonSearchImap):
        setup_seconds = 0.0

        def select(self, folder, *, readonly):
            result = super().select(folder, readonly=readonly)
            clock[0] += self.setup_seconds
            return result

    old = message("<old@test.invalid>", subject="needle", internaldate="01-Jan-2020 00:00:00 +0000")
    fake = SlowSelectionImap({"All Mail": {"1": old}}, uidnext=True)
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    fake.folders["All Mail"]["2"] = message(
        "<arrival@test.invalid>", subject="needle", internaldate="02-Jan-2020 00:00:00 +0000"
    )
    fake.calls.clear()
    fake.setup_seconds = 4.0
    monkeypatch.setattr("proton_mail_mcp.search.time.monotonic", lambda: clock[0])
    deadline = reader._deadline()
    monkeypatch.setattr(reader, "_deadline", lambda: deadline)
    original_metadata = reader._metadata
    optional_budgets = []

    def metadata(connection, uids, **kwargs):
        if kwargs.get("headers") and kwargs.get("envelope") and list(uids) == ["2"]:
            optional_budgets.append(deadline.check(connection))
        return original_metadata(connection, uids, **kwargs)

    def resolve(client, connection, deadline, handles, **kwargs):
        return [{**handle, "folder": "INBOX"} for handle in handles]

    monkeypatch.setattr(reader, "_metadata", metadata)
    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_views", resolve)

    result = reader.search_mail(folder="All Mail", subject="needle", include_snippets=False)

    # Main's full-socket-timeout gate refuses catch-up after this EXAMINE,
    # excluding both old matching messages through its partial 30-day window.
    assert result["total"] == 2
    assert {item["message_id"] for item in result["messages"]} == {
        "<old@test.invalid>",
        "<arrival@test.invalid>",
    }
    assert "partial_reason" not in result
    assert not result["partial"] and result["total_is_exact"]
    assert "applied_since" not in result
    assert reader.search_index.coverage("All Mail") == ("1", 2, "3")
    # Catch-up for the arrival, then the page, which also stores its fingerprints.
    assert [call[1] for call in header_fetches(fake) if "ENVELOPE" in call[2]] == ["2", "2,1"]
    assert optional_budgets == [9.0]
    assert deadline.end == 1020.0
    assert deadline.io_end - clock[0] == 14.0
    assert_read_only(fake)


def test_search_mail_logs_exhausted_optional_budget_at_production_level(
    settings, monkeypatch, caplog
):
    clock = [1000.0]

    class SlowSelectionImap(GluonSearchImap):
        setup_seconds = 0.0

        def select(self, folder, *, readonly):
            result = super().select(folder, readonly=readonly)
            clock[0] += self.setup_seconds
            return result

    old = message("<old@test.invalid>", internaldate="01-Jan-2020 00:00:00 +0000")
    fake = SlowSelectionImap({"All Mail": {"1": old}}, uidnext=True)
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    fake.folders["All Mail"]["2"] = message(
        "<arrival@test.invalid>", internaldate="02-Jan-2020 00:00:00 +0000"
    )
    fake.calls.clear()
    fake.setup_seconds = 12.5
    monkeypatch.setattr("proton_mail_mcp.search.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-09-27")
    caplog.set_level(logging.WARNING)

    def resolve(client, connection, deadline, handles, **kwargs):
        return [{**handle, "folder": "INBOX"} for handle in handles]

    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_views", resolve)

    result = reader.search_mail(folder="All Mail", subject="absent", include_snippets=False)

    assert result["partial"] and not result["total_is_exact"]
    assert result["applied_since"] == "2026-09-27"
    assert result["partial_reason"] == "no_resumable_cursor"
    assert reader.search_index.coverage("All Mail") == ("1", 1, "2")
    assert "reason=budget" in caplog.text
    assert all(record.levelno >= logging.WARNING for record in caplog.records)
    assert not [call for call in fake.calls if call[0] == "SEARCH" and call[3:4] == ("UID",)]
    assert_read_only(fake)


@pytest.mark.parametrize("cache", ["unbuilt", "observed_without_epoch", "disabled"])
def test_search_mail_keeps_unbuilt_and_disabled_catchup_quiet(settings, monkeypatch, caplog, cache):
    settings = replace(settings, search_index_catchup_max_messages=1)
    old = message("<old@test.invalid>", internaldate="01-Jan-2020 00:00:00 +0000")
    fake = GluonSearchImap({"All Mail": {str(uid): old for uid in (1, 2, 3)}}, uidnext=True)
    reader = client(settings, fake)
    if cache == "observed_without_epoch":
        reader.search_index.observe("All Mail", "1", {"1": {"headers": {}}})
    elif cache == "disabled":
        reader.search_index.enabled = False
    caplog.set_level(logging.WARNING)
    caplog.clear()
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-09-27")

    result = reader.search_mail(folder="All Mail", subject="needle", include_snippets=False)

    assert result["partial"] and result["applied_since"] == "2026-09-27"
    assert not [record for record in caplog.records if record.message.startswith("Header catch-up")]
    assert_read_only(fake)
