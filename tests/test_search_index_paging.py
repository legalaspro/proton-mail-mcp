"""Immutable metadata enables indexed pages and mailbox-local header coverage."""

import sqlite3
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

from test_mail import message
from test_mail import settings as settings
from test_search_index import CoverageImap

from proton_mail_mcp import canonical
from proton_mail_mcp.search_index import SearchIndex, Unplaced


def envelope(identifier):
    return [None] * 9 + [identifier.encode() if identifier is not None else None]


def indexed_message(uid, *, subject="Topic", recipient="reader@example.invalid", stamp=1.0):
    return {
        "headers": {
            "Message-ID": f"<{uid}@example.invalid>",
            "Subject": subject,
            "From": "Sender <sender@example.invalid>",
            "To": recipient,
        },
        "internaldate": stamp,
    }


def test_large_match_page_counts_and_orders_in_sql_beyond_sqlite_variable_limit(
    settings, monkeypatch
):
    index = SearchIndex(settings)
    uids = [str(uid) for uid in range(1, 1504)]
    index.observe(
        "All Mail",
        "1",
        {uid: indexed_message(uid, stamp=float(int(uid) // 2)) for uid in uids},
    )
    original_connect = index._connect

    def connect():
        database = original_connect()
        database.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        return database

    monkeypatch.setattr(index, "_connect", connect)
    conditions = [("to", "READER@example.invalid")]
    first = index.ordered_page("All Mail", "1", uids, conditions, 5)
    assert first == (
        1503,
        [(str(uid), float(uid // 2)) for uid in range(1503, 1497, -1)],
    )
    second = index.ordered_page("All Mail", "1", uids, conditions, 5, last=(749.0, 1499))
    assert second == (
        1503,
        [(str(uid), float(uid // 2)) for uid in range(1498, 1492, -1)],
    )
    # Candidate UIDs already carry the cursor's stable maximum-UID cutoff.
    assert index.ordered_page("All Mail", "1", ["1", "2"], conditions, 5) == (
        2,
        [("2", 1.0), ("1", 0.0)],
    )


def test_ordered_page_uses_nested_header_filters_and_rejects_unknown_evidence(settings):
    index = SearchIndex(settings)
    index.observe(
        "INBOX",
        "1",
        {
            "1": indexed_message("1", subject="Keep Straße", stamp=3.0),
            "2": indexed_message("2", subject="Drop Straße", stamp=2.0),
            "3": indexed_message("3", subject="Keep another", stamp=1.0),
        },
    )
    conditions = [
        (
            "and",
            (
                ("or", (("subject", "STRASSE"), ("subject", "another"))),
                ("not", ("subject", "drop")),
            ),
        )
    ]
    assert index.ordered_page("INBOX", "1", ["1", "2", "3"], conditions, 5) == (
        2,
        [("1", 3.0), ("3", 1.0)],
    )
    assert index.ordered_page("INBOX", "1", ["1", "99"], [], 5) is None
    assert index.ordered_page("INBOX", "2", ["1"], [], 5) is None
    assert index.ordered_page("INBOX", "1", ["1"], [("text", "body")], 5) is None
    index.observe("INBOX", "1", {"4": {"headers": {"Subject": "No date"}}})
    assert index.ordered_page("INBOX", "1", ["1", "4"], [], 5) is None
    index.observe("INBOX", "1", {"5": {"internaldate": 5.0}})
    assert index.ordered_page("INBOX", "1", ["5"], [], 5) is None


def test_header_cache_enriches_missing_metadata_without_replacing_headers(settings):
    index = SearchIndex(settings)
    index.observe("All Mail", "1", {"1": indexed_message("1")})
    with sqlite3.connect(index.path) as database:
        database.execute("DELETE FROM search_metadata")
    index = SearchIndex(settings)
    assert index.enabled and index.metadata_uids("All Mail", "1") == set()
    assert index.ordered_page("All Mail", "1", ["1"], [], 5) is None

    index.observe(
        "All Mail",
        "1",
        {"1": {"internaldate": 123.0, "envelope": envelope("1@example.invalid")}},
    )
    assert index.metadata_uids("All Mail", "1") == {"1"}
    assert index.cached_headers("All Mail", "1", ["1"])["1"]["Subject"] == "topic"
    assert index.ordered_page("All Mail", "1", ["1"], [("subject", "topic")], 5) == (
        1,
        [("1", 123.0)],
    )
    # A later partial metadata fetch does not erase immutable evidence.
    index.observe("All Mail", "1", {"1": {"internaldate": 123.0}})
    index.observe("All Mail", "1", {"1": {"envelope": envelope("<1@example.invalid>")}})
    assert index.cached_metadata("All Mail", "1", ["1", "missing"]) == {
        "1": {"internaldate": 123.0, "message_id": "<1@example.invalid>"}
    }


def test_ordinary_metadata_does_not_create_message_rows_for_browsed_views(settings):
    index = SearchIndex(settings)
    fetched = {"101": {"internaldate": 1.0, "envelope": envelope("<1@example.invalid>")}}
    index.observe("Labels/Browsed", "1", fetched)
    assert not index.path.exists()

    index.observe("All Mail", "1", {"1": indexed_message("1")})
    index.observe("Labels/Browsed", "1", fetched)
    assert not index.has_mailbox("Labels/Browsed")
    assert index.cached_metadata("Labels/Browsed", "1", ["101"]) == {}
    with sqlite3.connect(index.path) as database:
        assert database.execute("SELECT folder, uid FROM messages").fetchall() == [
            ("All Mail", "1")
        ]


def test_explicit_membership_warming_persists_before_mapping_full_inventory(settings):
    index = SearchIndex(settings)
    index.observe("All Mail", "1", {"1": indexed_message("1")})
    members = {"101": {"internaldate": 1.0, "envelope": envelope("<1@example.invalid>")}}
    index.observe("INBOX", "9", members, membership=True)
    restarted = SearchIndex(settings)
    assert restarted.metadata_uids("INBOX", "9") == {"101"}
    assert restarted.known_uids("INBOX", "9") == set()

    cached = restarted.cached_metadata("INBOX", "9", ["101"])
    assert restarted.map_headers("All Mail", "1", "INBOX", "9", cached) == {"101"}
    assert restarted.known_uids("INBOX", "9") == {"101"}
    assert restarted.cached_headers("INBOX", "9", ["101"])["101"]["Subject"] == "topic"


def test_folder_mapping_filters_large_membership_with_folder_local_uids(settings):
    index = SearchIndex(settings)
    index.observe(
        "All Mail",
        "1",
        {
            str(uid): indexed_message(
                str(uid), subject="Needle" if uid % 200 == 0 else "Other", stamp=float(uid)
            )
            for uid in range(1, 1504)
        },
    )
    members = {
        str(uid + 10000): {
            "envelope": envelope(f"<{uid}@example.invalid>"),
            "internaldate": float(uid),
        }
        for uid in range(1, 1202)
    }
    assert index.map_headers("All Mail", "1", "INBOX", "9", members) == set(members)
    assert index.covers("INBOX", "9", list(members))
    selected = index.candidates("INBOX", "9", list(members), [("subject", "needle")])
    assert selected == [str(uid + 10000) for uid in range(200, 1201, 200)]
    assert index.ordered_page("INBOX", "9", selected, [("subject", "needle")], 2) == (
        6,
        [("11200", 1200.0), ("11000", 1000.0), ("10800", 800.0)],
    )
    assert index.known_uids("All Mail", "1") == {str(uid) for uid in range(1, 1504)}
    assert not index.covers("INBOX", "1", list(members))


def test_mapping_retains_unknown_missing_and_duplicate_message_ids(settings):
    index = SearchIndex(settings)
    first, duplicate = indexed_message("1"), indexed_message("1", subject="Different")
    index.observe(
        "All Mail",
        "1",
        {"1": first, "2": duplicate, "3": indexed_message("3"), "4": indexed_message("4")},
    )
    members = {
        "101": {"envelope": envelope("<1@example.invalid>"), "internaldate": 1.0},
        "102": {"envelope": envelope("<3@example.invalid>"), "internaldate": 2.0},
        "103": {"envelope": envelope("<3@example.invalid>"), "internaldate": 3.0},
        "104": {"envelope": envelope("<4@example.invalid>"), "internaldate": 4.0},
        "105": {"envelope": envelope(None), "internaldate": 5.0},
        "106": {"envelope": envelope("<missing@example.invalid>"), "internaldate": 6.0},
    }
    assert index.map_headers("All Mail", "1", "INBOX", "1", members) == {"104"}
    assert index.known_uids("INBOX", "1") == {"104"}
    assert index.candidates("INBOX", "1", list(members), [("subject", "absent")]) == [
        "101",
        "102",
        "103",
        "105",
        "106",
    ]
    assert index.map_headers("All Mail", "2", "Archive", "1", members) == set()


def test_metadata_does_not_accept_duplicate_or_normalized_away_raw_header_ids(settings):
    index = SearchIndex(settings)
    duplicate = BytesParser(policy=policy.default).parsebytes(
        b"Message-ID: <one@example.invalid>\r\nMessage-ID: <two@example.invalid>\r\n\r\n"
    )
    opaque = BytesParser(policy=policy.default).parsebytes(b"Message-ID: <a@b@c>\r\n\r\n")
    index.observe(
        "All Mail",
        "1",
        {
            "1": {"headers": duplicate, "internaldate": 1.0},
            "2": {"headers": opaque, "internaldate": 2.0},
        },
    )
    assert index.cached_metadata("All Mail", "1", ["1", "2"]) == {
        "1": {"internaldate": 1.0, "message_id": None},
        "2": {"internaldate": 2.0, "message_id": "<a@b@c>"},
    }
    assert index.metadata_uids("All Mail", "1") == {"1", "2"}


def test_known_absent_message_id_does_not_require_repeated_legacy_hydration(settings):
    index = SearchIndex(settings)
    index.observe("All Mail", "1", {"1": {"headers": {"Subject": "No ID"}}})
    assert index.metadata_uids("All Mail", "1") == set()
    index.observe("All Mail", "1", {"1": {"envelope": envelope(None), "internaldate": 1.0}})
    assert index.metadata_uids("All Mail", "1") == {"1"}
    assert SearchIndex(settings).metadata_uids("All Mail", "1") == {"1"}
    assert (
        index.map_headers(
            "All Mail",
            "1",
            "INBOX",
            "1",
            {"101": {"envelope": envelope(None), "internaldate": 1.0}},
        )
        == set()
    )


def test_header_mapping_rejects_same_id_with_different_cached_envelopes(settings):
    index = SearchIndex(settings)
    source = envelope("<1@example.invalid>")
    source[1] = b"Original subject"
    target = list(source)
    target[1] = b"Different subject"
    index.observe("All Mail", "1", {"1": {**indexed_message("1"), "envelope": source}})
    members = {"101": {"envelope": target, "internaldate": 1.0}}
    assert index.map_headers("All Mail", "1", "INBOX", "1", members) == set()
    assert index.candidates("INBOX", "1", ["101"], [("subject", "different")]) == ["101"]


def test_removals_reconcile_and_epoch_changes_remove_additive_metadata(settings):
    index = SearchIndex(settings)
    index.observe("All Mail", "1", {str(uid): indexed_message(str(uid)) for uid in range(1, 5)})
    index.remove("All Mail", "1", ["1"])
    assert index.metadata_uids("All Mail", "1") == {"2", "3", "4"}
    index.reconcile("All Mail", "1", ["2", "4"])
    assert index.metadata_uids("All Mail", "1") == {"2", "4"}
    index.select("All Mail", "2")
    assert index.metadata_uids("All Mail", "1") == set()
    assert index.cached_metadata("All Mail", "2", ["2", "4"]) == {}


def canonical_index(settings):
    index = SearchIndex(settings)
    first = {
        **indexed_message("first", stamp=1.0),
        "envelope": envelope("<first@example.invalid>"),
        "size": 100,
    }
    second = {
        **indexed_message("second", stamp=2.0),
        "envelope": envelope("<second@example.invalid>"),
        "size": 200,
    }
    inventories = {
        "All Mail": {"1": first, "2": second},
        "INBOX": {"101": first},
        "Archive": {"201": second},
        "Empty": {},
    }
    epochs = {}
    for folder, records in inventories.items():
        index.observe(folder, "1", records)
        index.select(folder, "1")
        state = {"exists": len(records), "uidnext": str(max(map(int, records), default=0) + 1)}
        assert index.record_coverage(
            folder, "1", state, list(records), account=folder == "All Mail"
        )
        epochs[folder] = ("1", state["exists"], state["uidnext"])
    return index, inventories, epochs


def test_complete_fingerprint_index_resolves_unique_canonical_handles(settings):
    index, inventories, epochs = canonical_index(settings)
    handles = [canonical.handle("All Mail", "1", "2"), canonical.handle("All Mail", "1", "1")]
    real = {"INBOX", "Archive", "Empty"}
    expected = [canonical.handle("Archive", "1", "201"), canonical.handle("INBOX", "1", "101")]
    assert index.indexed_locations(handles, real, "All Mail", epochs) == expected

    # The fast resolver uses STATUS proofs and SQLite only, with no FETCH or
    # date-scoped SEARCH even when many mail share the page's calendar day, and
    # needs no proof from a folder that holds none of these messages.
    fake = CoverageImap(
        {
            folder: {uid: message(f"<{uid}@example.invalid>") for uid in records}
            for folder, records in inventories.items()
        },
        uidnext=True,
    )
    reader = SimpleNamespace(search_index=index)
    deadline = SimpleNamespace(check=lambda connection: None)
    assert (
        canonical.resolve_views(reader, fake, deadline, handles, real=real, all_mail="All Mail")
        == expected
    )
    assert sorted(call[1] for call in fake.calls) == sorted(
        2 * ['"All Mail"', '"INBOX"', '"Archive"']
    )
    assert all(call[0] == "STATUS" for call in fake.calls)


def test_fingerprint_index_needs_all_mail_and_the_holding_folder_only(settings):
    index, inventories, epochs = canonical_index(settings)
    handles = [canonical.handle("All Mail", "1", "1")]
    real = {"INBOX", "Archive", "Empty"}
    inbox_copy = [canonical.handle("INBOX", "1", "101")]
    assert (
        index.indexed_locations(handles, real, "All Mail", {**epochs, "Empty": ("2", 0, "1")})
        is None
    )
    # A folder holding no copy is not asked, however stale or incomplete it is.
    only_holder = {name: epochs[name] for name in ("All Mail", "INBOX")}
    assert index.indexed_locations(handles, real, "All Mail", only_holder) == inbox_copy
    with sqlite3.connect(index.path) as database:
        database.execute("UPDATE search_metadata SET size = NULL WHERE folder = 'Archive'")
    assert index.indexed_locations(handles, real, "All Mail", epochs) == inbox_copy
    # An unproven holder never gives a handle; it only names where to scan.
    [unplaced] = index.indexed_locations(
        handles, real, "All Mail", {"All Mail": epochs["All Mail"]}
    )
    assert isinstance(unplaced, Unplaced) and unplaced.holders == ("INBOX",)
    # All Mail is what proves the copy unique, so it needs every fingerprint.
    with sqlite3.connect(index.path) as database:
        database.execute("UPDATE search_metadata SET size = NULL WHERE uid = '2'")
    assert index.indexed_locations(handles, real, "All Mail", epochs) is None
    index.observe("All Mail", "1", {"2": {"size": 200, "internaldate": 2.0}})
    assert index.indexed_locations(handles, real, "All Mail", epochs) == inbox_copy
    # A legacy writer's unknown extra row must not count, even with a matching
    # fingerprint: stale fingerprints cannot invent a live copy.
    index.observe("Archive", "1", {"202": inventories["INBOX"]["101"]})
    with sqlite3.connect(index.path) as database:
        database.execute(
            "UPDATE messages SET headers_known = 0 WHERE folder = 'Archive' AND uid = '202'"
        )
    assert index.indexed_locations(handles, real, "All Mail", epochs) == inbox_copy


def test_duplicate_fingerprints_remain_unresolved_in_complete_canonical_index(settings):
    index, inventories, epochs = canonical_index(settings)
    duplicate = inventories["INBOX"]["101"]
    index.observe("Archive", "1", {"202": duplicate})
    assert index.record_coverage("Archive", "1", {"exists": 2, "uidnext": "203"}, ["201", "202"])
    epochs["Archive"] = ("1", 2, "203")
    handle = canonical.handle("All Mail", "1", "1")
    assert index.indexed_locations([handle], {"INBOX", "Archive", "Empty"}, "All Mail", epochs) == [
        None
    ]
    index.remove("Archive", "1", ["202"])
    assert index.record_coverage("Archive", "1", {"exists": 1, "uidnext": "203"}, ["201"])
    epochs["Archive"] = ("1", 1, "203")
    index.observe("All Mail", "1", {"3": duplicate})
    assert index.record_coverage("All Mail", "1", {"exists": 3, "uidnext": "4"}, ["1", "2", "3"])
    epochs["All Mail"] = ("1", 3, "4")
    assert index.indexed_locations([handle], {"INBOX", "Archive", "Empty"}, "All Mail", epochs) == [
        None
    ]


def test_cached_canonical_proofs_need_only_the_holder_to_match_live_status(settings):
    index, inventories, epochs = canonical_index(settings)
    fake = CoverageImap(
        {
            folder: {uid: message(f"<{uid}@example.invalid>") for uid in records}
            for folder, records in inventories.items()
        },
        uidnext=True,
    )
    reader = SimpleNamespace(search_index=index)
    deadline = SimpleNamespace(check=lambda connection: None)
    handles = [canonical.handle("All Mail", "1", "1")]
    real = {"INBOX", "Archive", "Empty"}
    # A folder holding none of these messages can change without any effect.
    fake.folders["Empty"]["1"] = message("<arrival@example.invalid>")
    assert canonical.resolve_indexed_views(
        reader, fake, deadline, handles, real=real, all_mail="All Mail"
    ).locations == [canonical.handle("INBOX", "1", "101")]
    # The holder cannot give a handle once it no longer matches its proof.
    fake.folders["INBOX"]["102"] = message("<arrival@example.invalid>")
    indexed = canonical.resolve_indexed_views(
        reader, fake, deadline, handles, real=real, all_mail="All Mail"
    )
    assert indexed.locations == [None] and list(indexed.pending) == [0]


def test_account_counts_keep_all_mail_role_when_a_newer_real_folder_has_more_mail(settings):
    index = SearchIndex(settings)
    index.observe("All Mail", "1", {"1": indexed_message("1"), "2": indexed_message("2")})
    assert index.record_coverage("All Mail", "1", {"exists": 2, "uidnext": "3"}, ["1", "2"])
    index.observe("INBOX", "9", {str(uid): indexed_message(str(uid)) for uid in range(101, 105)})
    assert index.record_coverage(
        "INBOX", "9", {"exists": 4, "uidnext": "105"}, ["101", "102", "103", "104"], account=False
    )
    # All Mail's saved inventory can lag a real folder. Its counters retain
    # that explicit inventory even after its freshness proof is invalidated.
    index.remove("All Mail", "1", ["2"])
    assert index.coverage("All Mail") is None
    assert index.reporting_counts() == {"all_mail_count": 2, "indexed_count": 1}
    assert SearchIndex(settings).reporting_counts() == {"all_mail_count": 2, "indexed_count": 1}


def test_account_counts_drop_old_role_before_replacement_has_complete_headers(settings):
    index = SearchIndex(settings)
    index.observe("Old All Mail", "1", {"1": indexed_message("1")})
    assert index.record_coverage("Old All Mail", "1", {"exists": 1, "uidnext": "2"}, ["1"])
    assert index.reporting_counts() == {"all_mail_count": 1, "indexed_count": 1}

    index.set_all_mail("New All Mail")
    assert index.reporting_counts() is None
    assert SearchIndex(settings).reporting_counts() is None
    assert index.coverage("Old All Mail") == ("1", 1, "2")

    index.observe("New All Mail", "9", {"101": indexed_message("1")})
    assert index.record_coverage("New All Mail", "9", {"exists": 1, "uidnext": "102"}, ["101"])
    assert index.reporting_counts() == {"all_mail_count": 1, "indexed_count": 1}
    index.set_all_mail(None)
    assert index.reporting_counts() is None
    assert SearchIndex(settings).reporting_counts() is None
