"""Message dates in the user's time zone: Bridge writes Date headers in UTC."""

import asyncio
import json
import sys
import zoneinfo
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime

import pytest
from test_mcp_apps_reply import item, reply
from test_server import FakeMail
from view_browser import browser, run_view, tool_result

from proton_mail_mcp import mcp_apps, views
from proton_mail_mcp.config import ConfigError, Settings
from proton_mail_mcp.server import create_mcp

TOKEN = "local-dates-token-" + "x" * 32
UTC_DATE = "Wed, 28 Oct 2026 10:00:00 +0000"
# Windows has no zone database without the tzdata package.
names = pytest.mark.skipif(
    "Europe/Berlin" not in zoneinfo.available_timezones(), reason="needs IANA zone data"
)


def settings(**overrides):
    values = {"token": TOKEN, "bridge_user": "reader@example.test", "confirm_mutations": False}
    return Settings(**{**values, **overrides})


@pytest.mark.parametrize(
    "value,zone,expected",
    [
        (UTC_DATE, timezone(timedelta(hours=1)), "2026-10-28T11:00:00+01:00"),
        (UTC_DATE + " (UTC)", timezone(timedelta(hours=-5)), "2026-10-28T05:00:00-05:00"),
        # "-0000" leaves the sender's zone unknown; the time is still UTC.
        ("28 Oct 2026 10:00:00 -0000", timezone(timedelta(hours=9)), "2026-10-28T19:00:00+09:00"),
        (
            "Wed, 28 Oct 2026 23:30:00 +0000",
            timezone(timedelta(hours=2)),
            "2026-10-29T01:30:00+02:00",
        ),
    ],
)
def test_a_date_header_becomes_the_time_in_the_users_zone(value, zone, expected):
    assert views.local_date(value, zone) == expected


@pytest.mark.parametrize("value", ["", "   ", "not a date", None, 1793626821])
def test_an_unreadable_date_has_no_local_time(value):
    assert views.local_date(value, timezone.utc) is None


def test_without_a_zone_the_time_is_this_computers():
    expected = parsedate_to_datetime(UTC_DATE).astimezone().isoformat()
    assert views.local_date(UTC_DATE, None) == expected


@names
def test_a_named_zone_follows_daylight_saving_time():
    berlin = zoneinfo.ZoneInfo("Europe/Berlin")
    assert views.local_date("Wed, 15 Jul 2026 10:00:00 +0000", berlin) == (
        "2026-07-15T12:00:00+02:00"
    )
    assert views.local_date("Tue, 15 Dec 2026 10:00:00 +0000", berlin) == (
        "2026-12-15T11:00:00+01:00"
    )


def test_each_message_gets_date_local_right_after_date():
    zone = timezone(timedelta(hours=1))
    result = {
        "messages": [
            {"message_id": "<a@example.test>", "date": UTC_DATE, "subject": "A"},
            {"message_id": "<b@example.test>", "date": "garbled", "date_local": "stale"},
            {"message_id": "<c@example.test>"},
            "not a message",
        ],
        "partial": False,
    }
    dated = views.with_local_dates(result, zone)
    first, second, third, fourth = dated["messages"]
    assert list(first) == ["message_id", "date", "date_local", "subject"]
    assert first["date_local"] == "2026-10-28T11:00:00+01:00"
    assert second == {"message_id": "<b@example.test>", "date": "garbled", "date_local": None}
    assert third == {"message_id": "<c@example.test>"} and fourth == "not a message"
    assert dated["partial"] is False
    # The backend's result is not changed in place.
    assert "date_local" not in result["messages"][0]
    assert views.with_local_dates({"count": 1}, zone) == {"count": 1}


def test_a_reply_quotes_the_originals_date_in_the_users_zone():
    text, view = reply(zone=timezone(timedelta(hours=-5)))
    assert text["draft"]["body"].startswith(
        "\n\nOn Wed, 28 Oct 2026 05:00:00 -0500, Alice Example <alice@example.test> wrote:\n"
    )
    assert view["draft"]["body"] == text["draft"]["body"]
    assert text["original"]["date"] == UTC_DATE
    assert text["original"]["date_local"] == "2026-10-28T05:00:00-05:00"
    # An unreadable date is quoted as it is.
    garbled, _ = reply(item(date="sometime"), zone=timezone.utc)
    assert garbled["draft"]["body"].startswith("\n\nOn sometime, Alice Example")
    assert garbled["original"]["date_local"] is None


@names
def test_the_zone_setting_takes_an_iana_name():
    env = {"PROTON_MCP_TOKEN": TOKEN, "PROTON_BRIDGE_USER": "reader@example.test"}
    assert Settings.from_env(env).time_zone is None
    assert Settings.from_env(env).display_zone is None
    configured = Settings.from_env({**env, "PROTON_MCP_TIMEZONE": "Europe/Berlin"})
    assert configured.display_zone == zoneinfo.ZoneInfo("Europe/Berlin")
    assert Settings.from_env({**env, "PROTON_MCP_TIMEZONE": ""}).time_zone is None


@pytest.mark.parametrize("value", ["Mars/Olympus_Mons", "../../etc/passwd", "/etc/localtime", 5])
def test_the_zone_setting_refuses_anything_else(value):
    with pytest.raises(ConfigError, match="PROTON_MCP_TIMEZONE must be an IANA time zone"):
        settings(time_zone=value)


class DatedMail(FakeMail):
    def read_thread(self, **kwargs):
        result = super().read_thread(**kwargs)
        result["messages"][0]["date"] = UTC_DATE
        return result


@names
@pytest.mark.parametrize("tool", ["read_thread", "show_thread", "search_mail"])
def test_message_results_carry_the_configured_local_time(tool):
    mcp = create_mcp(settings(time_zone="Asia/Tokyo"), DatedMail())
    arguments = {"message_id": "<one@example.test>"} if tool != "search_mail" else {}
    result = asyncio.run(mcp.call_tool(tool, arguments))
    blocks = result[0] if isinstance(result, tuple) else result.content
    [message] = json.loads(blocks[0].text)["messages"]
    expected = {
        "search_mail": "2026-10-14T19:00:00+09:00",
        "read_thread": "2026-10-28T19:00:00+09:00",
        "show_thread": "2026-10-28T19:00:00+09:00",
    }
    assert message["date_local"] == expected[tool]


WHEN = """
const doc = t.doc;
const when = await t.waitFor(() => doc.querySelector(".message .when"), "the message date");
t.check("when", when.textContent);
"""


def thread_with_date(date):
    result = {
        "messages": [
            {
                "message_id": "<m0@example.test>",
                "from": "Sender <s@example.test>",
                "to": "reader@example.test",
                "subject": "Thread",
                "date": date,
                "text": "Body",
                "attachments": [],
                "truncated": False,
            }
        ],
        "partial": False,
    }
    return tool_result(mcp_apps.view_result(*views.thread_view(result, "<m0@example.test>")))


@browser
@pytest.mark.parametrize("date", [UTC_DATE, UTC_DATE + " (UTC)"])
def test_the_view_shows_dates_in_the_zone_the_host_reports(tmp_path, date):
    report = run_view(
        tmp_path, "thread", thread_with_date(date), WHEN, host_context={"timeZone": "Asia/Tokyo"}
    )
    assert report["checks"]["when"] == "28 Oct 2026, 19:00"


@browser
@pytest.mark.skipif(sys.platform == "win32", reason="Chrome reads TZ on Linux and macOS")
def test_without_a_host_zone_the_view_uses_the_browsers_own(tmp_path):
    report = run_view(
        tmp_path,
        "thread",
        thread_with_date(UTC_DATE),
        WHEN,
        host_context={"timeZone": None},
        browser_tz="America/New_York",
    )
    assert report["checks"]["when"] == "28 Oct 2026, 06:00"
