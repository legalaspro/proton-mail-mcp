"""The optional index accelerates discovery without changing search results."""

import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
from gluon_search import GluonSearchImap
from test_mail import FakeImap, client, message
from test_mail import settings as settings
from windows_skips import has_mode

from proton_mail_mcp import localfs


@pytest.fixture(autouse=True)
def canonical_handles(monkeypatch):
    # Isolate index behavior from canonical folder discovery. Real copy and
    # duplicate safety are covered by test_canonical_identity.py.
    def resolve(client, connection, deadline, handles, **kwargs):
        return [{**handle, "folder": "INBOX"} for handle in handles]

    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_views", resolve)


class CoverageImap(GluonSearchImap):
    def status(self, folder, fields):
        from test_mail import unquote

        assert fields == "(MESSAGES UIDNEXT UIDVALIDITY)"
        self.calls.append(("STATUS", folder, fields))
        name = unquote(folder)
        uids = self.folders[name]
        following = max([self.next_uids.get(name, 1), *(int(uid) + 1 for uid in uids)])
        self.next_uids[name] = following
        return "OK", [
            f"{folder} (MESSAGES {len(uids)} UIDNEXT {following} "
            f"UIDVALIDITY {self.uidvalidity})".encode()
        ]


def header_uids(fake):
    return {
        uid
        for call in fake.calls
        if call[0] == "FETCH" and "HEADER.FIELDS" in call[2]
        for uid in call[1].split(",")
    }


def stored_handles(index):
    with closing(sqlite3.connect(index.path)) as connection, connection:
        return set(connection.execute("SELECT folder, uidvalidity, uid FROM messages"))


@pytest.mark.parametrize("folder", ["INBOX", "All Mail"])
def test_search_completeness_depends_on_requested_folder_coverage(settings, folder):
    from types import SimpleNamespace

    settings = replace(settings, search_index_catchup_max_messages=0)
    fake = CoverageImap(
        {
            "INBOX": {"10": message("<inbox@example.com>")},
            "All Mail": {"1": message("<all@example.com>")},
        },
        uidnext=True,
    )
    reader = client(settings, fake)

    def check(expected_partial):
        result = reader.search_mail(folder=folder, include_snippets=False)
        assert result["partial"] is expected_partial
        assert result["total_is_exact"] is not expected_partial
        assert result["total"] == 1
        assert result.get("partial_reason") == ("no_resumable_cursor" if expected_partial else None)
        assert not {"search_index", "search_index_details", "retryable"} & result.keys()

    check(folder == "All Mail")
    reader.rebuild_search_index("All Mail")
    check(False)
    # Replacing a UID with a different one preserves the count, but not coverage.
    fake.folders["All Mail"] = {"2": message("<arrival@example.com>")}
    check(folder == "All Mail")
    # The previous scan warmed the replacement inventory; rebuilding does not
    # make this complete result partial.
    reader._reindex_service = SimpleNamespace(running=True)
    check(False)


def test_background_reindex_rate_configuration():
    from proton_mail_mcp.config import ConfigError, Settings

    env = {"PROTON_MCP_TOKEN": "x" * 32, "PROTON_BRIDGE_USER": "example"}
    defaults = Settings.from_env(env)
    assert (defaults.reindex_batch_size, defaults.reindex_batch_sleep) == (100, 0.25)
    settings = Settings.from_env(
        {**env, "PROTON_MCP_REINDEX_BATCH_SIZE": "20", "PROTON_MCP_REINDEX_BATCH_SLEEP": "1.5"}
    )
    assert (settings.reindex_batch_size, settings.reindex_batch_sleep) == (20, 1.5)
    for value in ("0", "201", "1.5", "invalid"):
        with pytest.raises(ConfigError):
            Settings.from_env({**env, "PROTON_MCP_REINDEX_BATCH_SIZE": value})
    for value in ("0", "-1", "61", "nan", "inf", "invalid"):
        with pytest.raises(ConfigError):
            Settings.from_env({**env, "PROTON_MCP_REINDEX_BATCH_SLEEP": value})
    for params in ({"reindex_batch_size": True}, {"reindex_batch_sleep": True}):
        with pytest.raises(ConfigError):
            replace(defaults, **params)


def test_search_keeps_completed_results_after_an_index_swap(settings, monkeypatch):
    fake = FakeImap(
        {"All Mail": {str(uid): message(f"<{uid}@example.com>") for uid in range(1, 101)}},
        uidnext=True,
    )
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    index = reader.search_index
    candidate = index.create_candidate(settings, "a" * 32)
    candidate.observe(
        "All Mail", "1", {str(uid): {"headers": {"Subject": "Hello"}} for uid in range(2, 101)}
    )
    original_candidates = index.candidates

    def swap_then_filter(*args):
        index.replace_with(candidate)
        return original_candidates(*args)

    monkeypatch.setattr(index, "candidates", swap_then_filter)
    try:
        result = reader.search_mail(folder="All Mail", include_snippets=False)
        assert result["total"] == 100 and result["total_is_exact"] and not result["partial"]
        assert "search_index" not in result
    finally:
        candidate.discard()


def test_absent_index_stays_lazy_until_message_data_is_written(settings):
    from proton_mail_mcp.search_index import SearchIndex

    index = SearchIndex(settings)
    state = index.path.parent.parent
    assert not state.exists()
    for shared_directory in (False, True):
        if shared_directory:
            index.path.parent.mkdir(mode=0o700, parents=True)
            index = SearchIndex(settings)
        assert index.candidates("INBOX", "1", ["2", "1"], [("subject", "invoice")]) == [
            "2",
            "1",
        ]
        index.select("INBOX", "1")
        index.reconcile("INBOX", "1", ["1"])
        index.remove("INBOX", "1", ["2"])
        index.observe("INBOX", "1", {})
        index.observe("INBOX", "1", {"1": {"headers": None}})
        if shared_directory:
            assert list(index.path.parent.iterdir()) == []
        else:
            assert not state.exists()

    index.observe("INBOX", "1", {"1": {"headers": {"Subject": "Other topic"}}})
    assert index.enabled
    assert stored_handles(index) == {("INBOX", "1", "1")}
    assert has_mode(index.path, 0o600)
    assert has_mode(index.path.parent, 0o700)
    assert index.candidates("INBOX", "1", ["1", "2"], [("subject", "invoice")]) == ["2"]


def test_index_query_preserves_substrings_and_reduces_warm_header_fetches(settings):
    hit = message("<hit@example.com>", subject="Die Straße zum Büro")
    hit.headers = hit.headers.replace(
        b"To: reader@example.com\r\n",
        b"To: Reader <reader@example.com>\r\nCc: Team <team@example.com>\r\n",
    )
    fake = FakeImap(
        {
            "INBOX": {
                "1": hit,
                "2": message("<miss@example.com>", subject="Other topic"),
            }
        },
        uidnext=True,
    )
    reader = client(settings, fake)
    query = {"subject": "rASSE", "from_": "NDER@exam", "to": "ADER <rea"}
    cold = reader.search_mail(**query)
    assert [item["uid"] for item in cold["messages"]] == ["1"]
    assert header_uids(fake) == {"1", "2"}
    fake.calls.clear()

    warm = reader.search_mail(**query)

    assert warm == cold
    assert warm["total_is_exact"] and not warm["partial"]
    assert header_uids(fake) == {"1"}
    with closing(sqlite3.connect(reader.search_index.path)) as connection, connection:
        row = connection.execute("SELECT cc, body FROM messages WHERE uid = '1'").fetchone()
    assert "team@example.com" in row[0]
    assert row[1] == "", "Snippet and body fetches must not persist body text"


def test_rebuild_then_incremental_add_and_external_expunge(settings):
    fake = FakeImap(
        {
            "INBOX": {
                "1": message("<old@example.com>", subject="Invoice old"),
                "2": message("<other@example.com>", subject="Other topic"),
            }
        },
        uidnext=True,
    )
    reader = client(settings, fake)
    assert reader.rebuild_search_index() == {"folder": "INBOX", "indexed": 2, "available": True}
    assert stored_handles(reader.search_index) == {("INBOX", "1", "1"), ("INBOX", "1", "2")}
    fake.folders["INBOX"]["3"] = message("<new@example.com>", subject="Invoice new")
    del fake.folders["INBOX"]["1"]
    fake.calls.clear()

    found = reader.search_mail(subject="invoice", include_snippets=False)

    assert [item["uid"] for item in found["messages"]] == ["3"]
    assert found["total"] == 1 and found["total_is_exact"] is True
    assert header_uids(fake) == {"3"}
    assert stored_handles(reader.search_index) == {("INBOX", "1", "2"), ("INBOX", "1", "3")}

    # Freshly observed headers replace the indexed text; a separate folder's
    # membership must survive removing the source handle, even with the same UID.
    fake.folders["INBOX"]["2"] = message("<other@example.com>", subject="Invoice updated")
    fake.folders["Archive"] = {"3": fake.folders["INBOX"]["3"]}
    deadline = reader._deadline()
    with reader._session(deadline=deadline) as (connection, _):
        for folder, uid in (("INBOX", "2"), ("Archive", "3")):
            reader._select(connection, folder)
            reader._metadata(
                connection, [uid], deadline=deadline, bodystructure=False, internaldate=False
            )
    updated = reader.search_mail(subject="invoice", include_snippets=False)
    assert {item["uid"] for item in updated["messages"]} == {"2", "3"}
    del fake.folders["INBOX"]["3"]
    reader.search_index.remove("INBOX", "1", ["3"])
    assert stored_handles(reader.search_index) == {("INBOX", "1", "2"), ("Archive", "1", "3")}


def test_uidvalidity_change_discards_old_contents_before_reusing_uid(settings):
    fake = FakeImap(
        {
            "INBOX": {
                "1": message("<old@example.com>", subject="Old topic"),
                "2": message("<gone@example.com>", subject="Gone topic"),
            }
        },
        uidnext=True,
    )
    reader = client(settings, fake)
    assert reader.search_mail(subject="replacement", include_snippets=False)["total"] == 0
    assert len(stored_handles(reader.search_index)) == 2
    fake.uidvalidity = "2"
    fake.folders["INBOX"] = {"1": message("<replacement@example.com>", subject="Replacement")}
    fake.calls.clear()

    found = reader.search_mail(subject="replacement", include_snippets=False)

    assert [(item["uidvalidity"], item["uid"]) for item in found["messages"]] == [("2", "1")]
    assert header_uids(fake) == {"1"}
    assert stored_handles(reader.search_index) == {("INBOX", "2", "1")}


@pytest.mark.parametrize("failure", ["unavailable", "corrupt", "missing"])
def test_index_failure_falls_back_with_identical_results_and_one_log(
    settings, monkeypatch, caplog, failure
):
    from proton_mail_mcp import search_index

    fake = FakeImap(
        {
            "INBOX": {
                "1": message("<hit@example.com>", subject="Invoice"),
                "2": message("<miss@example.com>", subject="Other topic"),
            }
        },
        uidnext=True,
    )
    reader = client(settings, fake)
    expected = reader.search_mail(subject="invoice", include_snippets=False)
    if failure != "missing":
        caplog.clear()
    if failure == "unavailable":

        def unavailable(self):
            raise PermissionError("Storage unavailable")

        monkeypatch.setattr(search_index.SearchIndex, "_connect", unavailable)
        reader = client(settings, fake)
    elif failure == "corrupt":
        reader.search_index.path.write_bytes(b"not a sqlite database")
        reader = client(settings, fake)
    else:
        reader.search_index.path.unlink()

    for _ in range(2):
        fake.calls.clear()
        result = reader.search_mail(subject="invoice", include_snippets=False)
        assert result == expected
        assert header_uids(fake) == {"1", "2"}
    assert reader.search_index.enabled is False
    assert len([record for record in caplog.records if record.name == search_index.__name__]) == 1


@pytest.mark.parametrize("field", ["subject", "from_"])
def test_rebuilt_all_mail_index_survives_restart_and_searches_all_dates(settings, field):
    hit = message("<hit@example.com>", subject="Manning", internaldate="01-Jan-2020 00:00:00 +0000")
    hit.headers = hit.headers.replace(
        b"Sender <sender@example.com>", b"Manning <books@example.com>"
    )
    fake = GluonSearchImap(
        {"All Mail": {"1": hit, "2": message("<miss@example.com>", subject="Other topic")}},
        uidnext=True,
    )
    assert client(settings, fake).rebuild_search_index("All Mail")["indexed"] == 2
    reader = client(settings, fake)
    fake.calls.clear()

    result = reader.search_mail(folder="All Mail", include_snippets=False, **{field: "Manning"})

    assert [item["message_id"] for item in result["messages"]] == ["<hit@example.com>"]
    assert result["total"] == 1 and result["total_is_exact"] is True
    assert result["partial"] is False and "applied_since" not in result
    assert not {"search_index", "search_index_details", "retryable"} & result.keys()
    assert header_uids(fake) == {"1"}
    assert not any(call[0] == "SEARCH" for call in fake.calls)


def test_complete_header_index_keeps_full_bridge_text_semantics_without_window(settings):
    fake = GluonSearchImap(
        {
            "All Mail": {
                "1": message(
                    "<body@example.com>",
                    subject="Old body hit",
                    internaldate="01-Jan-2020 00:00:00 +0000",
                    sections={"1": b"Manning only in the body"},
                ),
                "2": message("<subject@example.com>", subject="Manning"),
                "3": message("<miss@example.com>", subject="Other topic"),
            }
        },
        uidnext=True,
    )
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    fake.calls.clear()

    result = reader.search_mail(folder="All Mail", text="Manning", include_snippets=False)

    assert {item["message_id"] for item in result["messages"]} == {
        "<body@example.com>",
        "<subject@example.com>",
    }
    assert result["total"] == 2 and result["total_is_exact"] is True
    assert result["partial"] is False and "applied_since" not in result
    searches = [call for call in fake.calls if call[0] == "SEARCH"]
    assert any("TEXT" in call for call in searches)
    assert not any("SINCE" in call for call in searches)
    combined = reader.search_mail(
        folder="All Mail",
        query={"or": [{"subject": "Other"}, {"text": "Manning"}]},
        include_snippets=False,
    )
    assert combined["total"] == 3 and combined["total_is_exact"] is True


@pytest.mark.parametrize("change", ["uidvalidity", "replace"])
def test_index_coverage_requires_current_validity_and_every_live_uid(settings, monkeypatch, change):
    fake = GluonSearchImap(
        {"All Mail": {"1": message("<old@example.com>", subject="Manning")}}, uidnext=True
    )
    reader = client(replace(settings, search_index_catchup_max_messages=0), fake)
    reader.rebuild_search_index("All Mail")
    if change == "uidvalidity":
        fake.uidvalidity = "2"
    else:
        # Equal message counts do not establish coverage after an expunge/arrival.
        fake.folders["All Mail"] = {"2": message("<new@example.com>", subject="Manning")}
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-09-27")

    result = reader.search_mail(folder="All Mail", subject="Manning", include_snippets=False)

    assert result["applied_since"] == "2026-09-27"
    assert result["partial"] is True and result["total_is_exact"] is False
    assert result["partial_reason"] == "no_resumable_cursor"


@pytest.mark.parametrize("replace_uid", [False, True])
def test_all_mail_catches_up_missing_headers_before_searching_all_dates(settings, replace_uid):
    settings = replace(settings, search_index_catchup_max_messages=1)
    fake = GluonSearchImap(
        {
            "All Mail": {
                "1": message(
                    "<old@example.com>",
                    subject="Manning",
                    internaldate="01-Jan-2020 00:00:00 +0000",
                ),
                "2": message("<other@example.com>", subject="Other topic"),
            }
        },
        uidnext=True,
    )
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    if replace_uid:
        # Preserve the count while changing UID membership.
        del fake.folders["All Mail"]["2"]
    fake.folders["All Mail"]["3"] = message("<new@example.com>", subject="Manning")
    fake.calls.clear()

    result = reader.search_mail(folder="All Mail", subject="Manning", include_snippets=False)

    assert {item["message_id"] for item in result["messages"]} == {
        "<old@example.com>",
        "<new@example.com>",
    }
    assert result["total"] == 2 and result["total_is_exact"] is True
    assert result["partial"] is False and "applied_since" not in result
    assert "partial_reason" not in result
    assert reader.search_index.known_uids("All Mail", "1") == set(fake.folders["All Mail"])
    assert [call[3:] for call in fake.calls if call[0] == "SEARCH"] == [
        ("UID", "3:3"),
        *([("ALL",)] if replace_uid else []),
    ]
    first_fetch = next(call for call in fake.calls if call[0] == "FETCH")
    assert first_fetch[1] == "3" and "HEADER.FIELDS" in first_fetch[2]
    assert "BODYSTRUCTURE" not in first_fetch[2] and "INTERNALDATE" in first_fetch[2]


@pytest.mark.parametrize("replace_uids", [False, True])
def test_all_mail_over_catchup_cap_retains_partial_window(settings, monkeypatch, replace_uids):
    settings = replace(settings, search_index_catchup_max_messages=1)
    old = message("<old@example.com>", internaldate="01-Jan-2020 00:00:00 +0000")
    fake = GluonSearchImap({"All Mail": {"1": old, "2": old}}, uidnext=True)
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    if replace_uids:
        # Equal counts trigger ALL, but actual missing membership exceeds cap.
        fake.folders["All Mail"].clear()
    fake.folders["All Mail"].update({"3": old, "4": old})
    fake.calls.clear()
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-09-27")

    result = reader.search_mail(folder="All Mail", include_snippets=False)

    assert result["applied_since"] == "2026-09-27"
    assert result["partial"] is True and result["total_is_exact"] is False
    assert result["partial_reason"] == "no_resumable_cursor"
    assert header_uids(fake) == (set() if replace_uids else {"3"})
    searches = [call[3:] for call in fake.calls if call[0] == "SEARCH"]
    assert searches == [
        ("UID", "3:4"),
        *([("ALL",)] if replace_uids else []),
        ("SINCE", "27-Sep-2026"),
    ]


def test_all_mail_catchup_timeout_returns_partial_without_reusing_stream(settings, monkeypatch):
    from proton_mail_mcp.mail import CallDeadlineExceeded

    fake = GluonSearchImap({"All Mail": {"1": message("<old@example.com>")}}, uidnext=True)
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    fake.folders["All Mail"]["2"] = message("<new@example.com>")
    fake.calls.clear()
    monkeypatch.setattr("proton_mail_mcp.search._default_since", lambda: "2026-09-27")
    attempts = []

    def timed_out(connection, uids, **kwargs):
        attempts.append((connection, uids))
        raise CallDeadlineExceeded("Mailbox call timed out")

    monkeypatch.setattr(reader, "_metadata", timed_out)
    result = reader.search_mail(folder="All Mail", include_snippets=False)

    assert [uids for _, uids in attempts] == [["2"], ["2", "1"]]
    assert attempts[0][0] is not attempts[1][0]
    assert result["messages"] == [] and result["next_cursor"]
    assert result["partial"] is True and result["total_is_exact"] is False
    assert result["applied_since"] == "2026-09-27"
    assert result["partial_reason"] == "timed_out"
    assert [call[3:] for call in fake.calls if call[0] == "SEARCH"] == [
        ("UID", "2:2"),
        ("SINCE", "27-Sep-2026"),
    ]


def test_all_mail_catchup_requires_successful_index_write(settings, monkeypatch):
    fake = GluonSearchImap({"All Mail": {"1": message("<old@example.com>")}}, uidnext=True)
    reader = client(settings, fake)
    reader.rebuild_search_index("All Mail")
    fake.folders["All Mail"]["2"] = message("<new@example.com>")
    monkeypatch.setattr(reader.search_index, "observe", lambda *args: None)

    result = reader.search_mail(folder="All Mail", include_snippets=False)

    assert result["partial"] is True and result["total_is_exact"] is False
    assert "applied_since" in result
    assert result["partial_reason"] == "no_resumable_cursor"


def test_search_index_catchup_configuration():
    from proton_mail_mcp.config import ConfigError, Settings

    env = {"PROTON_MCP_TOKEN": "x" * 32, "PROTON_BRIDGE_USER": "example"}
    name = "PROTON_MCP_SEARCH_INDEX_CATCHUP_MAX_MESSAGES"
    assert Settings.from_env(env).search_index_catchup_max_messages == 2000
    for value in ("0", "17", "10000"):
        assert Settings.from_env({**env, name: value}).search_index_catchup_max_messages == int(
            value
        )
    for value in ("-1", "10001", "1.5", "invalid"):
        with pytest.raises(ConfigError):
            Settings.from_env({**env, name: value})
    with pytest.raises(ConfigError):
        replace(Settings.from_env(env), search_index_catchup_max_messages=True)
    with pytest.raises(ConfigError):
        replace(Settings.from_env(env), search_index_catchup_max_messages=10001)


def test_startup_reports_safe_index_failure_and_retries_transient_lock(settings):
    from proton_mail_mcp.search_index import SearchIndex

    index = SearchIndex(settings, rebuild=True)
    index.observe("All Mail", "1", {"1": {"headers": {"Subject": "Manning"}}})
    with index.path.with_suffix(".sqlite3.lock").open("rb") as lock:
        localfs.lock(lock.fileno(), exclusive=False)
        reopened = SearchIndex(settings)
        assert reopened.known_uids("All Mail", "1") is None
        assert reopened.reason == "busy" and reopened.enabled
        blocked_rebuild = SearchIndex(settings, rebuild=True)
        assert blocked_rebuild.reason == "busy" and not blocked_rebuild.enabled
    assert reopened.known_uids("All Mail", "1") == {"1"}
    assert reopened.reason is None
