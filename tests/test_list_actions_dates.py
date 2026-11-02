"""list_actions takes plain dates as whole local days, and timestamps as before."""

import time
from datetime import datetime, timedelta, timezone

import pytest

from proton_mail_mcp import tool_docs
from proton_mail_mcp.action_log import ActionLog
from proton_mail_mcp.mail import MailValidationError

# US Eastern ended daylight saving on 1 November 2026, so that day has 25 hours.
EASTERN = "EST5EDT,M3.2.0,M11.1.0"
NOVEMBER_START = "2026-11-01T00:00:00-04:00"
NOVEMBER_END = "2026-11-02T00:00:00-05:00"


def log_with(tmp_path, *timestamps):
    log = ActionLog(tmp_path / "synthetic.jsonl")
    with log.transaction() as transaction:
        for number, timestamp in enumerate(timestamps):
            transaction.append(
                {
                    "log_id": f"{number:032x}",
                    "timestamp": timestamp,
                    "action": "reindex_preview",
                    "run_id": "run",
                }
            )
    return log


def stamps(log, **bounds):
    return sorted(item["timestamp"] for item in log.list_actions(**bounds)["actions"])


def moment(value, seconds=0):
    return (datetime.fromisoformat(value) + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def eastern(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("Needs time.tzset to set the local timezone")
    monkeypatch.setenv("TZ", EASTERN)
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_since_date_starts_at_local_midnight_and_includes_it(tmp_path, eastern):
    before_start = moment(NOVEMBER_START, -1)
    log = log_with(tmp_path, before_start, NOVEMBER_START)

    assert stamps(log, since="2026-11-01") == [NOVEMBER_START]


def test_before_date_includes_its_whole_local_day(tmp_path, eastern):
    last_second = moment(NOVEMBER_END, -1)
    log = log_with(tmp_path, last_second, NOVEMBER_END)

    assert stamps(log, before="2026-11-01") == [last_second]


def test_one_date_in_both_bounds_is_that_day_even_with_25_hours(tmp_path, eastern):
    inside = [NOVEMBER_START, moment(NOVEMBER_END, -1)]
    log = log_with(tmp_path, moment(NOVEMBER_START, -1), *inside, NOVEMBER_END)

    # 2026-11-01T00:00-04:00 to 2026-11-02T00:00-05:00 is 25 hours.
    assert datetime.fromisoformat(NOVEMBER_END) - datetime.fromisoformat(
        NOVEMBER_START
    ) == timedelta(hours=25)
    assert stamps(log, since="2026-11-01", before="2026-11-01") == sorted(inside)


def test_dates_follow_the_local_timezone_not_utc(tmp_path, eastern):
    # 01:00 UTC on 2 November is still 1 November in US Eastern.
    late = datetime(2026, 11, 2, 1, tzinfo=timezone.utc).isoformat()
    log = log_with(tmp_path, late)

    assert stamps(log, since="2026-11-01", before="2026-11-01") == [late]
    assert stamps(log, since="2026-11-02") == []


def test_dates_use_the_platform_local_timezone(tmp_path):
    # Whatever this host's timezone is, including on Windows, where tzset is absent.
    start = datetime(2026, 6, 10).astimezone()
    end = datetime(2026, 6, 11).astimezone()
    inside = [start.isoformat(), (end - timedelta(seconds=1)).isoformat()]
    log = log_with(tmp_path, (start - timedelta(seconds=1)).isoformat(), *inside, end.isoformat())

    assert stamps(log, since="2026-06-10", before="2026-06-10") == sorted(inside)


def test_full_timestamps_keep_their_exact_inclusive_and_exclusive_bounds(tmp_path):
    first, second = "2026-11-01T10:00:00+00:00", "2026-11-01T11:00:00+00:00"
    log = log_with(tmp_path, first, second)

    assert stamps(log, since=first, before=second) == [first]
    assert stamps(log, since="2026-11-01T10:00:00Z") == [first, second]


def test_date_and_timestamp_mix(tmp_path, eastern):
    log = log_with(tmp_path, NOVEMBER_START, NOVEMBER_END)

    assert stamps(log, since="2026-11-01", before=NOVEMBER_END) == [NOVEMBER_START]


@pytest.mark.parametrize(
    "bounds",
    [
        {"since": "2026-13-01"},
        {"since": "2026-02-30"},
        {"before": "2026-1-1"},
        {"before": "20261101"},
        {"since": "2026-11-01T10:00:00"},
        {"since": "٢٠٢٦-١١-٠١"},
        {"since": "yesterday"},
        {"since": "0001-01-01"},
        {"before": "9999-12-31"},
    ],
)
def test_other_values_are_refused_without_repeating_them(tmp_path, bounds):
    # The earliest and latest dates are not refused everywhere, so they must not crash.
    log = log_with(tmp_path)

    try:
        log.list_actions(**bounds)
    except MailValidationError as error:
        assert "YYYY-MM-DD" in str(error)
        assert next(iter(bounds.values())) not in str(error)
    else:
        assert next(iter(bounds.values())) in {"0001-01-01"}


def test_later_since_than_before_is_refused(tmp_path):
    log = log_with(tmp_path)

    with pytest.raises(MailValidationError, match="must precede"):
        log.list_actions(since="2026-11-02", before="2026-11-01")


def test_the_schema_describes_plain_dates():
    for argument in (tool_docs.LookupSince, tool_docs.LookupBefore):
        assert "YYYY-MM-DD" in argument.__metadata__[0].description
    assert "all of that day" in tool_docs.LookupBefore.__metadata__[0].description
