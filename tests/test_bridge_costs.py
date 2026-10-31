"""Gluon literal work is independent of requested header/body section size."""

import pytest
from bridge_costs import DEFAULT_COSTS, WorkCounters


@pytest.mark.parametrize(
    "spec",
    [
        b"(UID BODY[])",
        b"(UID BODY.PEEK[])",
        b"(UID BODY.PEEK[HEADER])",
        b"(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
        b"(UID BODY.PEEK[1]<0.1>)",
        b"(UID RFC822)",
        b"(UID RFC822.HEADER)",
        b"(UID RFC822.TEXT)",
        b"(UID BODY.PEEK[HEADER] BODY.PEEK[TEXT])",
    ],
)
def test_every_literal_fetch_costs_one_whole_load_per_message(spec):
    work = WorkCounters()
    work.charge("FETCH", spec, 500)
    assert work.literal_loads_fetch == 500 and work.db_rows_fetch == 500


@pytest.mark.parametrize(
    "spec",
    [
        b"(UID)",
        b"(UID FLAGS)",
        b"(UID ENVELOPE)",
        b"(UID INTERNALDATE)",
        b"(UID RFC822.SIZE)",
        b"(UID BODYSTRUCTURE)",
    ],
)
def test_db_fetch_items_never_load_literals(spec):
    work = WorkCounters()
    work.charge("FETCH", spec, 500)
    assert work.literal_loads == 0 and work.db_rows_fetch == 500


@pytest.mark.parametrize(
    "key",
    [
        "HEADER Message-ID",
        "FROM",
        "TO",
        "CC",
        "BCC",
        "SUBJECT",
        "BODY",
        "TEXT",
        "SENTBEFORE",
        "SENTON",
        "SENTSINCE",
    ],
)
def test_literal_search_costs_one_load_per_visited_message(key):
    work = WorkCounters()
    work.charge("SEARCH", f'UID SEARCH {key} "value"'.encode(), 20_027)
    assert work.literal_loads_search == 20_027


@pytest.mark.parametrize(
    "criteria,db_rows",
    [
        ("ALL", 0),
        ("UNSEEN", 0),
        ("UID 1:500", 0),
        ("SINCE 17-Oct-2026", 500),
        ("BEFORE 17-Oct-2026", 500),
        ("LARGER 200", 500),
    ],
)
def test_snapshot_and_date_searches_never_load_literals(criteria, db_rows):
    work = WorkCounters()
    work.charge("SEARCH", f"UID SEARCH {criteria}".encode(), 500)
    assert work.literal_loads == 0 and work.db_rows_search == db_rows


def test_search_and_fetch_share_literal_price():
    class Clock:
        elapsed = 0.0

    fetch_clock, search_clock = Clock(), Clock()
    fetch_work, search_work = WorkCounters(fetch_clock), WorkCounters(search_clock)
    fetch_work.charge("FETCH", b"(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])", 500)
    search_work.charge("SEARCH", b'UID SEARCH HEADER Message-ID "value"', 500)
    assert fetch_clock.elapsed - search_clock.elapsed == pytest.approx(500 * DEFAULT_COSTS.db_row)


def test_envelope_model_includes_client_cpu():
    class Clock:
        elapsed = 0.0

    clock = Clock()
    work = WorkCounters(clock)
    work.charge("FETCH", b"(UID ENVELOPE)", 500)
    assert work.client_cpu_seconds == pytest.approx(500 * DEFAULT_COSTS.envelope_client_row)
    assert clock.elapsed == pytest.approx(
        DEFAULT_COSTS.command + 500 * (DEFAULT_COSTS.db_row + DEFAULT_COSTS.envelope_client_row)
    )
