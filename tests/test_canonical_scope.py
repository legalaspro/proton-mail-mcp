"""Canonical lookup needs proofs only from All Mail and the folders holding the thread.

A fake clock charges Bridge's slow commands, so a whole account's live day scan cannot
fit in read_thread's 3-second canonical cap while a STATUS proof, or a live scan of one
folder, can.
"""

import logging
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from bridge_sim import BridgeSim, Msg, hdr, make_client, mutations
from test_canonical_index import warm_folder
from test_index_proof_restore import ALL_INVENTORY, account, restart_clock
from test_index_proof_restore import clock as clock
from test_mail import message
from test_mail import settings as settings

from proton_mail_mcp.mail import CallDeadlineExceeded, MailError

# conftest forbids subprocess.run during tests; this one runs local Python only.
REAL_RUN = subprocess.run
ROOT = "<root@example.invalid>"
REPLY = "<reply@example.invalid>"


def uncached_arrival(reader, fake, clock, folder="INBOX", uid="99"):
    """All Mail caught up on new mail; this folder's own UID space did not.

    Its proof no longer matches STATUS, and with a live UID's headers uncached no
    UID inventory can certify it again, so only a live scan can read this folder.
    """
    arrival = message(f"<arrival-{folder}-{uid}@example.invalid>")
    fake.folders["All Mail"][str(1000 + int(uid))] = arrival
    warm_folder(reader, "All Mail")
    fake.folders[folder][uid] = arrival
    restart_clock(fake, clock)


def thread_mail():
    root = message(ROOT)
    return root, message(REPLY, reply=ROOT)


def handles(result):
    return {item["message_id"]: (item["folder"], item["uid"]) for item in result["messages"]}


def forget_fingerprint(reader, uid):
    # A row a plain page cached before pages stored its envelope and size.
    with closing(sqlite3.connect(reader.search_index.path)) as database, database:
        database.execute(
            "UPDATE search_metadata SET envelope_digest = NULL, size = NULL "
            "WHERE folder = 'INBOX' AND uid = ?",
            (uid,),
        )


def clear_proof(reader, fake, how="reconcile"):
    index = reader.search_index
    if how == "reconcile":
        # The expunge cleanup with no state, as every inventory used to leave it.
        index.reconcile("INBOX", "1", list(fake.folders["INBOX"]))
    elif how == "remove":
        index.remove("INBOX", "1", ["404"])
    else:
        assert not index.record_coverage(
            "INBOX", "1", {"exists": 9, "uidnext": "99"}, ["1"], account=False
        )
    assert index.coverage("INBOX") is None


# (a) INBOX is not complete, but the thread lives in other folders.
@pytest.mark.parametrize("how", ["reconcile", "remove", "unfinished_save", "uncached_arrival"])
def test_thread_in_other_folders_resolves_from_the_index_without_touching_inbox(
    settings, clock, how
):
    root, reply = thread_mail()
    other = message("<other@example.invalid>")
    reader, fake = account(
        settings,
        clock,
        {
            "All Mail": {"70": root, "71": reply, "72": other},
            "INBOX": {"1": other},
            "Archive": {"5": root},
            "Sent": {"6": reply},
            "Trash": {},
        },
    )
    if how == "uncached_arrival":
        uncached_arrival(reader, fake, clock)
    else:
        clear_proof(reader, fake, how)

    result = reader.read_thread(ROOT)

    assert result["partial"] is False and "partial_reason" not in result
    assert handles(result) == {ROOT: ("Archive", "5"), REPLY: ("Sent", "6")}
    assert all(item["alias"]["folder"] == "All Mail" for item in result["messages"])
    assert all(item["text"] == "Hello plain text" for item in result["messages"])
    for folder in ("INBOX", "Trash"):
        assert fake.touched(folder) == []
    assert fake.day_scans() == []
    # All Mail's EXAMINE and the bodies; the whole canonical proof costs STATUS alone.
    assert clock[0] - 100.0 < 1.5


def test_all_mail_search_page_needs_no_proof_from_an_unrelated_folder(settings, clock):
    root, reply = thread_mail()
    reader, fake = account(
        settings,
        clock,
        {
            "All Mail": {"70": root, "71": reply},
            "INBOX": {"1": root},
            "Archive": {"5": reply},
            "Trash": {},
        },
    )
    fake.folders["Trash"]["9"] = message("<other@example.invalid>")

    result = reader.search_mail(folder="All Mail", include_snippets=False)

    assert handles(result) == {ROOT: ("INBOX", "1"), REPLY: ("Archive", "5")}
    assert result["partial"] is False and fake.day_scans() == []
    assert fake.touched("Trash") == []


# (b) The thread lives in the incomplete folder.
def test_thread_in_a_changed_folder_reads_only_its_rows(settings, clock):
    root, reply = thread_mail()
    reader, fake = account(
        settings,
        clock,
        {"All Mail": {"70": root, "71": reply}, "INBOX": {"1": root, "2": reply}, "Archive": {}},
    )
    uncached_arrival(reader, fake, clock)

    result = reader.read_thread(ROOT)

    assert result["partial"] is False
    assert handles(result) == {ROOT: ("INBOX", "1"), REPLY: ("INBOX", "2")}
    assert fake.day_scans() == []
    # Its rows answer, so what arrived is not read.
    assert ("SEARCH", "CHARSET", "UTF-8", "UID", "3:99") not in fake.calls
    # The first folder read holds both, so no other folder is even asked.
    assert fake.touched("Archive") == []


def envelope_fetches(fake):
    return [call for call in fake.calls if call[0] == "FETCH" and "ENVELOPE" in call[2]]


def test_a_cached_row_settles_it_without_reading_many_arrivals(settings, clock):
    root = message(ROOT)
    reader, fake = account(settings, clock, {"All Mail": {"70": root}, "INBOX": {"1": root}})
    for number in range(2000):
        arrival = message(f"<arrival-{number}@example.invalid>")
        fake.folders["All Mail"][str(1000 + number)] = arrival
        fake.folders["INBOX"][str(100 + number)] = arrival
    warm_folder(reader, "All Mail")
    restart_clock(fake, clock)
    fake.costs.update(EXAMINE=0.1, SEARCH=0.1, FETCH=0.8)

    result = reader.read_thread(ROOT)

    assert result["partial"] is False and handles(result) == {ROOT: ("INBOX", "1")}
    assert [call[1] for call in envelope_fetches(fake) if "1" == call[1]] == ["1"]
    assert not any(call[0] == "SEARCH" and "UID" in call for call in fake.calls)


def test_a_row_that_left_is_found_again_among_the_arrivals(settings, clock):
    root, reply = thread_mail()
    reader, fake = account(
        settings, clock, {"All Mail": {"70": root, "71": reply}, "INBOX": {"1": root, "2": reply}}
    )
    fake.folders["INBOX"]["9"] = fake.folders["INBOX"].pop("2")
    restart_clock(fake, clock)

    result = reader.read_thread(ROOT)

    assert result["partial"] is False
    assert handles(result) == {ROOT: ("INBOX", "1"), REPLY: ("INBOX", "9")}
    assert ("SEARCH", "CHARSET", "UTF-8", "UID", "3:9") in fake.calls
    # The rows came first, then what arrived, under the same EXAMINE.
    rows, arrived = envelope_fetches(fake)[-2:]
    assert (rows[1], arrived[1]) == ("1,2", "9")
    between = fake.calls[fake.calls.index(rows) : fake.calls.index(arrived)]
    assert not any(call[0] == "EXAMINE" for call in between)


def test_rows_in_every_changed_folder_come_before_any_arrivals(settings, clock):
    root, reply = thread_mail()
    reader, fake = account(
        settings,
        clock,
        {"All Mail": {"70": root, "71": reply}, "INBOX": {"1": root}, "Archive": {"5": reply}},
    )
    for folder, uid in (("INBOX", "2"), ("Archive", "6")):
        fake.folders[folder][uid] = message(f"<arrival-{folder}@example.invalid>")
    restart_clock(fake, clock)

    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: ("INBOX", "1"), REPLY: ("Archive", "5")}
    assert not any(call[0] == "SEARCH" and "UID" in call for call in fake.calls)


def test_a_lookup_that_runs_out_among_the_arrivals_keeps_what_the_rows_placed(settings, clock):
    root, reply = thread_mail()
    reader, fake = account(
        settings, clock, {"All Mail": {"70": root, "71": reply}, "INBOX": {"1": root, "2": reply}}
    )
    fake.folders["INBOX"]["9"] = fake.folders["INBOX"].pop("2")
    restart_clock(fake, clock)
    fake.costs["SEARCH"] = 5.0

    result = reader.read_thread(ROOT)

    assert result["partial"] is False and "partial_reason" not in result
    assert handles(result) == {ROOT: ("INBOX", "1"), REPLY: (None, None)}


def test_changed_folder_resolves_when_a_day_scan_could_not_fit_the_limit(settings, clock):
    # The reported shape: a large folder's day scan costs more than the 3-second cap.
    root, reply = thread_mail()
    reader, fake = account(
        settings, clock, {"All Mail": {"70": root, "71": reply}, "INBOX": {"1": root, "2": reply}}
    )
    uncached_arrival(reader, fake, clock)
    original = fake.uid

    def day_scan_is_slow(command, *args):
        if command == "SEARCH" and "ON" in args:
            clock[0] += 15.0
        return original(command, *args)

    fake.uid = day_scan_is_slow

    result = reader.read_thread(ROOT)

    assert result["partial"] is False and "partial_reason" not in result
    assert handles(result) == {ROOT: ("INBOX", "1"), REPLY: ("INBOX", "2")}


def test_new_message_with_no_cached_row_is_found_among_the_arrivals(settings, clock):
    root = message(ROOT)
    reader, fake = account(settings, clock, {"All Mail": {"70": root}, "INBOX": {"1": root}})
    uncached_arrival(reader, fake, clock)

    result = reader.read_thread("<arrival-INBOX-99@example.invalid>")

    assert result["partial"] is False
    assert handles(result) == {"<arrival-INBOX-99@example.invalid>": ("INBOX", "99")}
    assert fake.day_scans() == []


def test_folder_with_no_proof_to_measure_from_still_gets_its_day_scan(settings, clock):
    root = message(ROOT)
    reader, fake = account(settings, clock, {"All Mail": {"70": root}, "INBOX": {}})
    # An arrival's row was never cached here, and this folder's proof was cleared.
    fake.folders["INBOX"]["1"] = root
    reader.search_index.reconcile("INBOX", "1", [])
    assert reader.search_index.coverage("INBOX") is None
    restart_clock(fake, clock)
    fake.costs.update(EXAMINE=0.1, SEARCH=0.1)

    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: ("INBOX", "1")}
    assert fake.day_scans() == ["INBOX"]


def test_saved_proof_of_another_uidvalidity_measures_nothing(settings, clock):
    root = message(ROOT)
    reader, fake = account(settings, clock, {"All Mail": {"70": root}, "INBOX": {"1": root}})
    # Bridge renumbered INBOX, so the UIDs its saved proof covered mean nothing now.
    with closing(sqlite3.connect(reader.search_index.path)) as database, database:
        for table in ("coverage", "mailboxes", "messages", "search_metadata"):
            database.execute(f"UPDATE {table} SET uidvalidity = '9' WHERE folder = 'INBOX'")
    assert reader.search_index.coverage("INBOX")[0] == "9"
    fake.costs.update(EXAMINE=0.1, SEARCH=0.1)

    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: ("INBOX", "1")}
    assert result["messages"][0]["uidvalidity"] == "1"
    assert fake.day_scans() == ["INBOX"]


def test_copy_read_twice_in_one_folder_counts_once(settings, clock):
    root, reply = thread_mail()
    reader, fake = account(settings, clock, {"All Mail": {"70": root}, "INBOX": {"1": root}})
    fake.folders["All Mail"]["71"] = reply
    fake.folders["INBOX"]["2"] = reply
    # With no proof, INBOX confirms the root's row and then day-scans for the reply.
    reader.search_index.reconcile("INBOX", "1", ["1"])
    assert reader.search_index.coverage("INBOX") is None
    restart_clock(fake, clock)
    fake.costs.update(EXAMINE=0.1, SEARCH=0.1)

    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: ("INBOX", "1"), REPLY: ("INBOX", "2")}
    assert fake.day_scans() == ["INBOX"]


def test_refresh_that_cannot_certify_keeps_the_saved_proof(settings, clock):
    root = message(ROOT)
    reader, fake = account(settings, clock, {"All Mail": {"70": root}, "INBOX": {"1": root}})
    epoch = reader.search_index.coverage("INBOX")
    # One left and one arrived, so the count alone allows a refresh that fails.
    del fake.folders["INBOX"]["1"]
    uncached_arrival(reader, fake, clock, "INBOX", "2")

    reader.read_thread(ROOT)

    assert fake.calls.count(ALL_INVENTORY) == 1
    assert reader.search_index.coverage("INBOX") == epoch


def test_slow_unproven_folder_leaves_the_handles_null_without_a_warning(settings, clock):
    root, reply = thread_mail()
    reader, fake = account(
        settings,
        clock,
        {"All Mail": {"70": root, "71": reply}, "INBOX": {"1": root, "2": reply}},
    )
    uncached_arrival(reader, fake, clock)
    # With the rows gone, only the slow read of what arrived could place the messages.
    for uid in ("1", "2"):
        fake.folders["INBOX"][str(50 + int(uid))] = fake.folders["INBOX"].pop(uid)
    fake.costs["SEARCH"] = 5.0

    result = reader.read_thread(ROOT)

    assert result["partial"] is False and "partial_reason" not in result
    assert "timed_out" not in result and "error" not in result
    assert handles(result) == {ROOT: (None, None), REPLY: (None, None)}
    assert {item["canonical_reason"] for item in result["messages"]} == {"lookup_unfinished"}
    assert {item["alias"]["uid"] for item in result["messages"]} == {"70", "71"}
    assert all(item["text"] == "Hello plain text" for item in result["messages"])


def test_changed_holder_with_cached_headers_recertifies_instead_of_scanning(settings, clock):
    root, reply = thread_mail()
    reader, fake = account(
        settings,
        clock,
        {"All Mail": {"70": root, "71": reply}, "INBOX": {"1": root, "2": reply}},
    )
    clear_proof(reader, fake)

    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: ("INBOX", "1"), REPLY: ("INBOX", "2")}
    assert result["partial"] is False and fake.day_scans() == []
    assert fake.calls.count(ALL_INVENTORY) == 1
    assert reader.search_index.coverage("INBOX") == ("1", 2, "3")


def test_scan_starts_at_the_folder_with_a_cached_copy_and_stops_at_the_first_copy(settings, clock):
    root = message(ROOT)
    reader, fake = account(
        settings,
        clock,
        {"All Mail": {"70": root}, "INBOX": {}, "Archive": {"5": root}, "Sent": {}},
    )
    uncached_arrival(reader, fake, clock, "INBOX", "1")
    uncached_arrival(reader, fake, clock, "Archive", "6")

    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: ("Archive", "5")}
    assert fake.day_scans() == []
    assert fake.touched("INBOX") == [] and fake.touched("Sent") == []


# (c) Part of the thread is proven and part is not.
def mixed_account(settings, clock):
    root, reply = thread_mail()
    reader, fake = account(
        settings,
        clock,
        {
            "All Mail": {"70": root, "71": reply},
            "INBOX": {"1": reply},
            "Archive": {"5": root},
            "Sent": {},
        },
    )
    uncached_arrival(reader, fake, clock)
    return reader, fake


def test_mixed_thread_uses_the_index_and_scans_only_the_failing_folder(settings, clock):
    reader, fake = mixed_account(settings, clock)

    result = reader.read_thread(ROOT)

    assert result["partial"] is False
    assert handles(result) == {ROOT: ("Archive", "5"), REPLY: ("INBOX", "1")}
    assert fake.day_scans() == []
    assert "EXAMINE" not in fake.touched("Archive")


def test_mixed_thread_keeps_the_proven_handle_when_the_live_scan_runs_out(settings, clock):
    reader, fake = mixed_account(settings, clock)
    # With the row gone, only the slow read of what arrived could place the message.
    fake.folders["INBOX"]["98"] = fake.folders["INBOX"].pop("1")
    fake.costs["SEARCH"] = 5.0

    result = reader.read_thread(ROOT)

    assert result["partial"] is False and "partial_reason" not in result
    assert handles(result) == {ROOT: ("Archive", "5"), REPLY: (None, None)}
    assert all(item["text"] == "Hello plain text" for item in result["messages"])


def test_mixed_search_page_keeps_every_row_in_its_own_real_folder(settings, clock):
    reader, fake = mixed_account(settings, clock)

    result = reader.search_mail(folder="All Mail", include_snippets=False)

    assert handles(result) == {
        ROOT: ("Archive", "5"),
        REPLY: ("INBOX", "1"),
        "<arrival-INBOX-99@example.invalid>": ("INBOX", "99"),
    }
    assert result["partial"] is False and fake.day_scans() == []


# Correctness: a handle never comes from a folder whose index is not proven.
@pytest.mark.parametrize("archive", ["proven", "unproven"])
def test_stale_row_in_an_unproven_folder_never_gives_the_handle(settings, clock, archive):
    root = message(ROOT)
    reader, fake = account(
        settings, clock, {"All Mail": {"70": root}, "INBOX": {"1": root}, "Archive": {}}
    )
    # Another client moved the message; INBOX's cache still lists it, unproven.
    del fake.folders["INBOX"]["1"]
    uncached_arrival(reader, fake, clock, "INBOX", "2")
    fake.folders["Archive"]["5"] = root
    if archive == "proven":
        warm_folder(reader, "Archive")
    restart_clock(fake, clock)
    fake.costs.update(EXAMINE=0.1, SEARCH=0.1)

    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: ("Archive", "5")}
    assert result["partial"] is False
    assert fake.day_scans() == []


def test_stale_row_with_no_live_copy_stays_unresolved(settings, clock):
    root = message(ROOT)
    reader, fake = account(settings, clock, {"All Mail": {"70": root}, "INBOX": {"1": root}})
    # The message now has no real folder at all, while INBOX still has a stale row.
    del fake.folders["INBOX"]["1"]
    uncached_arrival(reader, fake, clock, "INBOX", "2")

    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: (None, None)}
    assert result["messages"][0]["alias"]["uid"] == "70"
    assert fake.day_scans() == []


def test_message_with_no_real_folder_needs_no_scan_when_every_folder_is_proven(settings, clock):
    root = message(ROOT)
    reader, fake = account(
        settings,
        clock,
        {"All Mail": {"70": root}, "INBOX": {"1": message("<other@example.invalid>")}},
    )

    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: (None, None)}
    assert result["partial"] is False and fake.day_scans() == []
    assert fake.touched("INBOX") == ["STATUS"]


def test_duplicate_fingerprints_stay_unresolved_with_no_scan(settings, clock):
    root = message(ROOT)
    twin = message(ROOT)
    reader, fake = account(
        settings,
        clock,
        {
            "All Mail": {"70": root, "71": twin},
            "INBOX": {"1": root},
            "Archive": {"5": twin},
        },
    )

    result = reader.read_thread(ROOT)

    assert handles(result)[ROOT] == (None, None)
    assert result["partial"] is False and fake.day_scans() == []


def test_two_proven_copies_of_one_all_mail_message_stay_unresolved(settings, clock):
    root = message(ROOT)
    reader, fake = account(
        settings,
        clock,
        {"All Mail": {"70": root}, "INBOX": {"1": root}, "Archive": {"5": root}},
    )

    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: (None, None)}
    assert result["messages"][0]["canonical_reason"] == "no_single_copy"
    assert result["partial"] is False and fake.day_scans() == []


def self_sent_account(settings, clock):
    root = message(ROOT)
    return account(
        settings,
        clock,
        {"All Mail": {"70": root}, "INBOX": {"1": root}, "Sent": {"6": root}},
    )


def test_self_sent_message_takes_its_inbox_copy_from_the_index_with_no_scan(settings, clock):
    reader, fake = self_sent_account(settings, clock)

    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: ("INBOX", "1")}
    assert "canonical_reason" not in result["messages"][0]
    assert result["partial"] is False and fake.day_scans() == []


def test_self_sent_message_waits_for_a_changed_inbox_instead_of_taking_sent(settings, clock):
    reader, fake = self_sent_account(settings, clock)
    uncached_arrival(reader, fake, clock)
    assert reader.search_index.coverage("Sent") is not None

    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: ("INBOX", "1")}
    assert "canonical_reason" not in result["messages"][0]
    assert fake.day_scans() == []


def test_proven_sent_copy_stands_when_the_cached_inbox_row_is_gone(settings, clock):
    reader, fake = self_sent_account(settings, clock)
    del fake.folders["INBOX"]["1"]
    uncached_arrival(reader, fake, clock)

    assert handles(reader.read_thread(ROOT)) == {ROOT: ("Sent", "6")}


def test_proven_sent_copy_stands_when_the_inbox_read_runs_out(settings, clock):
    reader, fake = self_sent_account(settings, clock)
    uncached_arrival(reader, fake, clock)
    fake.costs["EXAMINE"] = 5.0

    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: ("Sent", "6")}
    assert "canonical_reason" not in result["messages"][0]


def test_two_changed_folders_scanned_for_a_reply_keep_the_self_sent_inbox_copy(settings, clock):
    root, reply = thread_mail()
    reader, fake = account(
        settings,
        clock,
        {
            "All Mail": {"70": root, "71": reply},
            "INBOX": {"1": root},
            "Sent": {"6": root, "7": reply},
        },
    )
    uncached_arrival(reader, fake, clock)
    uncached_arrival(reader, fake, clock, "Sent", "98")

    assert handles(reader.read_thread(ROOT)) == {ROOT: ("INBOX", "1"), REPLY: ("Sent", "7")}


def test_two_live_copies_found_in_a_changed_folder_stay_unresolved(settings, clock):
    root = message(ROOT)
    reader, fake = account(settings, clock, {"All Mail": {"70": root}, "INBOX": {"1": root}})
    del fake.folders["INBOX"]["1"]
    fake.folders["INBOX"]["2"] = fake.folders["INBOX"]["3"] = root
    restart_clock(fake, clock)

    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: (None, None)}
    assert fake.day_scans() == []


def test_stale_all_mail_catches_up_so_the_lookup_and_expansion_stay_local(settings, clock):
    root, reply = thread_mail()
    other = message("<other@example.invalid>")
    reader, fake = account(
        settings,
        clock,
        {"All Mail": {"70": root, "71": other}, "INBOX": {"1": root}, "Archive": {"5": other}},
    )
    fake.folders["All Mail"]["72"] = reply
    fake.folders["INBOX"]["2"] = reply

    result = reader.read_thread(ROOT)

    assert result["partial"] is False and "timed_out" not in result
    assert handles(result) == {ROOT: ("INBOX", "1"), REPLY: ("INBOX", "2")}
    assert fake.day_scans() == []
    assert not any(call[0] == "SEARCH" and "HEADER" in call for call in fake.calls)
    # Only the arrival's headers are fetched, and the next read finds the proof current.
    assert reader.search_index.coverage("All Mail") == ("1", 3, "73")
    assert ("SEARCH", "CHARSET", "UTF-8", "UID", "72:72") in fake.calls
    restart_clock(fake, clock)
    assert handles(reader.read_thread(ROOT)) == handles(result)
    assert ("SEARCH", "CHARSET", "UTF-8", "UID", "72:72") not in fake.calls


def test_stale_all_mail_that_cannot_catch_up_keeps_the_whole_lookup_live(settings, clock):
    settings = replace(settings, search_index_catchup_max_messages=0)
    root, reply = thread_mail()
    other = message("<other@example.invalid>")
    reader, fake = account(
        settings,
        clock,
        {"All Mail": {"70": root, "71": other}, "INBOX": {"1": root}, "Archive": {"5": other}},
    )
    fake.folders["All Mail"]["72"] = reply
    fake.folders["INBOX"]["2"] = reply
    fake.costs.update(EXAMINE=0.01, SEARCH=0.01)

    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: ("INBOX", "1"), REPLY: ("INBOX", "2")}
    assert fake.day_scans() == ["All Mail", "INBOX", "Archive"]


def test_page_row_without_a_fingerprint_is_scanned_even_in_a_proven_folder(settings, clock):
    root, reply = thread_mail()
    reader, fake = account(
        settings,
        clock,
        {"All Mail": {"70": root, "71": reply}, "INBOX": {"1": root}, "Archive": {"5": reply}},
    )
    # An older cache has a new INBOX row's headers and date, with no envelope or
    # size. Its proof is valid, but fingerprints cannot find this row.
    arrival = message("<arrival@example.invalid>", reply=REPLY)
    fake.folders["All Mail"]["72"] = arrival
    warm_folder(reader, "All Mail")
    fake.folders["INBOX"]["2"] = arrival
    assert reader.search_mail(limit=1, include_snippets=False)["messages"][0]["uid"] == "2"
    assert reader.search_index.coverage("INBOX") == ("1", 2, "3")
    forget_fingerprint(reader, "2")
    restart_clock(fake, clock)

    result = reader.read_thread(ROOT)

    assert result["partial"] is False
    assert handles(result) == {
        ROOT: ("INBOX", "1"),
        REPLY: ("Archive", "5"),
        "<arrival@example.invalid>": ("INBOX", "2"),
    }
    assert fake.day_scans() == ["INBOX"]


def test_search_scan_warms_the_missing_fingerprint_and_read_thread_stays_read_only(settings, clock):
    root = message(ROOT)
    reader, fake = account(settings, clock, {"All Mail": {"70": root}, "INBOX": {}})
    arrival = message("<arrival@example.invalid>")
    fake.folders["All Mail"]["71"] = arrival
    warm_folder(reader, "All Mail")
    fake.folders["INBOX"]["1"] = arrival
    reader.search_mail(limit=1, include_snippets=False)
    forget_fingerprint(reader, "1")
    restart_clock(fake, clock)

    for _ in range(2):
        assert handles(reader.read_thread("<arrival@example.invalid>")) == {
            "<arrival@example.invalid>": ("INBOX", "1")
        }
    assert fake.day_scans() == ["INBOX", "INBOX"]

    page = reader.search_mail(folder="All Mail", include_snippets=False)
    assert handles(page)["<arrival@example.invalid>"] == ("INBOX", "1")
    restart_clock(fake, clock)

    assert handles(reader.read_thread("<arrival@example.invalid>")) == {
        "<arrival@example.invalid>": ("INBOX", "1")
    }
    assert fake.day_scans() == []


def test_plain_page_stores_the_fingerprint_so_a_thread_needs_no_scan(settings, clock):
    root = message(ROOT)
    reader, fake = account(settings, clock, {"All Mail": {"70": root}, "INBOX": {}})
    arrival = message("<arrival@example.invalid>")
    fake.folders["All Mail"]["71"] = arrival
    warm_folder(reader, "All Mail")
    fake.folders["INBOX"]["1"] = arrival
    reader.search_mail(limit=1, include_snippets=False)
    restart_clock(fake, clock)

    result = reader.read_thread("<arrival@example.invalid>")

    assert result["partial"] is False
    assert handles(result) == {"<arrival@example.invalid>": ("INBOX", "1")}
    assert fake.day_scans() == []
    assert "EXAMINE" not in fake.touched("INBOX")


def test_showing_an_older_row_on_a_page_heals_its_fingerprint(settings, clock):
    root = message(ROOT)
    reader, fake = account(settings, clock, {"All Mail": {"70": root}, "INBOX": {"1": root}})
    forget_fingerprint(reader, "1")

    reader.search_mail(include_snippets=False)
    restart_clock(fake, clock)

    assert handles(reader.read_thread(ROOT)) == {ROOT: ("INBOX", "1")}
    assert fake.day_scans() == []


@pytest.mark.parametrize("stamp,scanned", [(True, ["INBOX"]), (False, [])])
def test_proven_folder_is_scanned_only_for_a_row_of_the_wanted_date(
    settings, clock, stamp, scanned
):
    # The message has no real folder, so a row with no fingerprint decides the scan.
    root = message(ROOT)
    other = message(
        "<other@example.invalid>",
        internaldate=None if stamp else "13-Oct-2026 10:00:00 +0000",
        date="Wed, 14 Oct 2026 10:00:00 +0000",
    )
    reader, fake = account(
        settings, clock, {"All Mail": {"70": root, "71": other}, "INBOX": {"1": other}}
    )
    with closing(sqlite3.connect(reader.search_index.path)) as database, database:
        database.execute("UPDATE search_metadata SET envelope_digest = NULL WHERE folder = 'INBOX'")

    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: (None, None)}
    assert fake.day_scans() == scanned


def test_search_then_read_thread_take_the_fast_path_after_one_reconcile(settings, clock):
    root, reply = thread_mail()
    reader, fake = account(
        settings,
        clock,
        {"All Mail": {"70": root, "71": reply}, "INBOX": {"1": root, "2": reply}},
    )
    clear_proof(reader, fake)

    reader.search_mail(include_snippets=False)
    fake.calls.clear()
    result = reader.read_thread(ROOT)

    assert handles(result) == {ROOT: ("INBOX", "1"), REPLY: ("INBOX", "2")}
    assert fake.touched("INBOX") == ["STATUS", "STATUS", "STATUS", "STATUS"]
    assert ALL_INVENTORY not in fake.calls and fake.day_scans() == []


@pytest.mark.parametrize(
    "error", [CallDeadlineExceeded("Simulated interrupted scan"), MailError("No")]
)
def test_failed_live_scan_keeps_every_index_handle(settings, clock, monkeypatch, error):
    reader, fake = mixed_account(settings, clock)
    original = fake.uid

    def refuse_inbox_scan(command, *args):
        if command == "FETCH" and "ENVELOPE" in args[1] and fake.current == "INBOX":
            raise error
        return original(command, *args)

    monkeypatch.setattr(fake, "uid", refuse_inbox_scan)

    result = reader.read_thread(ROOT)

    assert result["partial"] is False and "partial_reason" not in result
    assert handles(result) == {ROOT: ("Archive", "5"), REPLY: (None, None)}


def test_scan_keeps_what_it_placed_when_a_later_folder_runs_out(settings, clock, monkeypatch):
    root, reply = thread_mail()
    reader, fake = account(
        settings,
        clock,
        # The reply has no real folder, so the scan goes on after placing the root.
        {"All Mail": {"70": root, "71": reply}, "INBOX": {"1": root}, "Archive": {}},
    )
    uncached_arrival(reader, fake, clock, "INBOX", "2")
    uncached_arrival(reader, fake, clock, "Archive", "6")
    fake.costs.update(EXAMINE=0.1, SEARCH=0.1)
    original = fake.select

    def slow_archive(folder, *, readonly):
        if folder == '"Archive"':
            clock[0] += 5.0
        return original(folder, readonly=readonly)

    monkeypatch.setattr(fake, "select", slow_archive)

    result = reader.read_thread(ROOT)

    assert result["partial"] is False and "partial_reason" not in result
    assert handles(result) == {ROOT: ("INBOX", "1"), REPLY: (None, None)}


@pytest.mark.parametrize("row", ["no_envelope", "no_metadata"])
def test_all_mail_row_with_no_fingerprint_is_fetched_instead_of_scanning(settings, clock, row):
    # A filter search stores a row with no envelope, and a draft's reply lookup one with no date.
    root, reply = thread_mail()
    other = message("<other@example.invalid>")
    reader, fake = account(
        settings,
        clock,
        {"All Mail": {"70": root, "71": reply, "72": other}, "INBOX": {"1": root, "2": reply}},
    )
    with closing(sqlite3.connect(reader.search_index.path)) as database, database:
        if row == "no_envelope":
            database.execute(
                "UPDATE search_metadata SET envelope_digest = NULL, size = NULL "
                "WHERE folder = 'All Mail' AND uid = '72'"
            )
        else:
            database.execute("DELETE FROM search_metadata WHERE folder = 'All Mail' AND uid = '72'")
    assert reader.search_index.unfingerprinted("All Mail", "1", 10) == ["72"]
    restart_clock(fake, clock)

    result = reader.read_thread(ROOT)

    assert result["partial"] is False and fake.day_scans() == []
    assert handles(result) == {ROOT: ("INBOX", "1"), REPLY: ("INBOX", "2")}
    assert reader.search_index.unfingerprinted("All Mail", "1", 10) == []


def test_large_all_mail_with_no_index_proof_starts_no_scan_that_cannot_finish(
    settings, clock, monkeypatch
):
    root, reply = thread_mail()
    reader, fake = account(
        settings, clock, {"All Mail": {"70": root, "71": reply}, "INBOX": {"1": root, "2": reply}}
    )
    monkeypatch.setattr("proton_mail_mcp.threading._LIVE_CANONICAL_SCAN_MAX_MESSAGES", 1)
    monkeypatch.setattr("proton_mail_mcp.canonical.resolve_indexed_views", lambda *a, **k: None)
    restart_clock(fake, clock)

    result = reader.read_thread(ROOT)

    assert result["partial"] is False and "partial_reason" not in result
    assert "timed_out" not in result and "error" not in result
    assert handles(result) == {ROOT: (None, None), REPLY: (None, None)}
    assert fake.day_scans() == []
    assert all(item["text"] == "Hello plain text" for item in result["messages"])


@pytest.fixture
def simulated(tmp_path):
    sim = BridgeSim()
    yield sim, make_client(sim, tmp_path)
    sim.close()
    assert not sim.errors and not sim.violations


def test_scoped_lookup_and_restored_proof_over_real_imaplib(simulated):
    sim, reader = simulated
    archived = sim.add(Msg(hdr("<archived@example.invalid>"), "Archive"))
    inbox = sim.add(Msg(hdr("<inbox@example.invalid>")))
    index = reader.search_index
    deadline = reader._deadline()
    with reader._session(deadline=deadline) as (connection, _):
        for folder in ("All Mail", "INBOX", "Archive"):
            state = reader._select(connection, folder)
            uids = reader._search(connection, ["ALL"])
            list(reader._index_header_batches(connection, uids, deadline=deadline, batch_size=500))
            assert index.record_coverage(
                folder, connection._search_selection[1], state, uids, account=folder == "All Mail"
            )
    validity = str(sim.uidvalidity["INBOX"])
    inventory = [str(sim.uid_of("INBOX", inbox.pid))]

    def commands(action):
        start = len(sim.commands)
        result = action()
        return result, [command for _, _, _, command in sim.commands[start:]]

    # INBOX loses its proof; a thread in Archive never reads it.
    index.reconcile("INBOX", validity, inventory)
    thread, sent = commands(lambda: reader.read_thread(archived.message_id()))
    assert thread["partial"] is False
    assert thread["messages"][0]["folder"] == "Archive"
    assert not any('"INBOX"' in command for command in sent)
    assert not any(re.search(r" ON [0-9]", command) for command in sent)

    # One inventory restores it, and the next search sends none.
    first, sent = commands(lambda: reader.search_mail(include_snippets=False))
    assert "UID SEARCH CHARSET UTF-8 ALL" in sent
    assert index.coverage("INBOX") == (validity, 1, str(int(inventory[0]) + 1))
    second, sent = commands(lambda: reader.search_mail(include_snippets=False))
    assert "UID SEARCH CHARSET UTF-8 ALL" not in sent
    assert second["messages"] == first["messages"]

    # New mail that All Mail caught up on, but INBOX has not: one scan, of INBOX.
    arrival = sim.add(Msg(hdr("<arrival@example.invalid>")))
    assert reader.search_mail(folder="All Mail", include_snippets=False)["partial"] is False
    thread, sent = commands(lambda: reader.read_thread(arrival.message_id()))
    assert thread["partial"] is False
    assert thread["messages"][0]["folder"] == "INBOX"
    assert thread["messages"][0]["uid"] == str(sim.uid_of("INBOX", arrival.pid))
    assert mutations(sim) == []


def test_thread_after_new_mail_stays_local_over_real_imaplib(simulated):
    sim, reader = simulated
    root = sim.add(Msg(hdr("<root@example.invalid>")))
    deadline = reader._deadline()
    with reader._session(deadline=deadline) as (connection, _):
        for folder in ("All Mail", "INBOX"):
            state = reader._select(connection, folder)
            uids = reader._search(connection, ["ALL"])
            list(reader._index_header_batches(connection, uids, deadline=deadline, batch_size=500))
            assert reader.search_index.record_coverage(
                folder, connection._search_selection[1], state, uids, account=folder == "All Mail"
            )
    reply = sim.add(
        Msg(hdr("<reply@example.invalid>", extra=b"In-Reply-To: <root@example.invalid>\r\n"))
    )
    start = len(sim.commands)

    result = reader.read_thread(root.message_id())

    sent = [command for _, _, _, command in sim.commands[start:]]
    assert result["partial"] is False and "timed_out" not in result
    assert {item["message_id"]: item["folder"] for item in result["messages"]} == {
        root.message_id(): "INBOX",
        reply.message_id(): "INBOX",
    }
    assert not any(re.search(r" ON [0-9]", command) for command in sent)
    assert not any(" HEADER " in command for command in sent)
    assert not mutations(sim)


def test_the_reason_a_lookup_stays_live_reaches_the_log_behind_a_warning_root(tmp_path):
    """create_mcp sets the root to WARNING, so this needs a process pytest has not configured."""
    code = """
import logging
from types import SimpleNamespace
from proton_mail_mcp import canonical
from proton_mail_mcp.config import Settings
from proton_mail_mcp.server import create_mcp

settings = Settings(
    token="t" * 32,
    bridge_user="user@example.test",
    action_log_path=%r,
    identity_index_enabled=False,
)
create_mcp(settings, object())
assert logging.getLogger().level == logging.WARNING
index = SimpleNamespace(coverage=lambda folder: None)
found = canonical.resolve_indexed_views(
    SimpleNamespace(search_index=index),
    SimpleNamespace(_search_index=index),
    None,
    [{"folder": "All Mail"}],
    real={"INBOX"},
    all_mail="All Mail",
)
assert found is None
""" % str(tmp_path / "actions.jsonl")
    source = str(Path(__file__).resolve().parents[1] / "src")
    done = REAL_RUN(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": source},
    )
    assert done.returncode == 0, done.stderr
    assert "Canonical lookup stays live reason=no_all_mail_proof" in done.stderr


def test_a_missing_fingerprint_is_the_reason_the_log_gives(settings, clock, caplog, monkeypatch):
    root = message(ROOT)
    reader, fake = account(settings, clock, {"All Mail": {"70": root}, "INBOX": {"1": root}})
    with closing(sqlite3.connect(reader.search_index.path)) as database, database:
        database.execute(
            "UPDATE search_metadata SET envelope_digest = NULL, size = NULL "
            "WHERE folder = 'All Mail'"
        )
    # A fetch that cannot fill the row in leaves the lookup live, and the log says why.
    monkeypatch.setattr(reader.search_index, "unfingerprinted", lambda *args: [])
    restart_clock(fake, clock)

    with caplog.at_level(logging.INFO, logger="proton_mail_mcp.canonical"):
        reader.read_thread(ROOT)

    assert "Canonical lookup stays live reason=no_fingerprint" in caplog.text
