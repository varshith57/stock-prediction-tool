from datetime import date

import httpx
import psycopg
import pytest
import respx

from stockapp.ingest.calendar import (
    HOLIDAY_URL,
    TradingCalendar,
    parse_holidays,
    record_session,
    refresh_holidays,
)
from stockapp.ingest.http import PoliteClient
from stockapp.lake import Lake

PAYLOAD = {
    "CM": [
        {
            "tradingDate": "02-Oct-2026",
            "weekDay": "Friday",
            "description": "Mahatma Gandhi Jayanti",
        },
        {"tradingDate": "26-Jan-2026", "weekDay": "Monday", "description": "Republic Day"},
    ],
    "FO": [{"tradingDate": "02-Oct-2026", "description": "x"}],
}


def _http() -> PoliteClient:
    return PoliteClient(min_interval_s=0, backoff_s=0, retries=0, sleep=lambda _: None)


def test_parse_holidays_cm_only_sorted():
    assert parse_holidays(PAYLOAD) == [
        (date(2026, 1, 26), "Republic Day"),
        (date(2026, 10, 2), "Mahatma Gandhi Jayanti"),
    ]


def test_parse_holidays_rejects_unexpected_shape():
    with pytest.raises(ValueError):
        parse_holidays({"something": []})


@respx.mock
def test_refresh_loads_and_replaces_the_year(db: psycopg.Connection, lake: Lake):
    route = respx.get(HOLIDAY_URL).mock(return_value=httpx.Response(200, json=PAYLOAD))
    assert refresh_holidays(db, lake, _http(), today=date(2026, 10, 4)) == 2
    assert "Referer" in route.calls.last.request.headers

    # NSE drops a holiday from the list: the table follows the published list for that year
    smaller = {"CM": PAYLOAD["CM"][:1]}
    route.mock(return_value=httpx.Response(200, json=smaller))
    refresh_holidays(db, lake, _http(), today=date(2026, 10, 5))
    rows = db.execute("SELECT holiday_date FROM trading_holidays").fetchall()
    assert [r["holiday_date"] for r in rows] == [date(2026, 10, 2)]


@respx.mock
def test_refresh_quarantines_bad_payload(db: psycopg.Connection, lake: Lake):
    respx.get(HOLIDAY_URL).mock(return_value=httpx.Response(200, json={"oops": 1}))
    with pytest.raises(ValueError):
        refresh_holidays(db, lake, _http(), today=date(2026, 10, 4))
    q = db.execute("SELECT reason FROM quarantine").fetchone()
    assert q is not None and q["reason"] == "parse_error"


@respx.mock
def test_calendar_explains_each_day(db: psycopg.Connection, lake: Lake):
    respx.get(HOLIDAY_URL).mock(return_value=httpx.Response(200, json=PAYLOAD))
    refresh_holidays(db, lake, _http(), today=date(2026, 10, 4))
    record_session(db, date(2026, 10, 1), evidence="test")
    record_session(db, date(2026, 10, 3), evidence="test")  # a Saturday special session

    cal = TradingCalendar(db, date(2026, 9, 30), date(2026, 10, 5))
    assert cal.info(date(2026, 10, 1)).status == "session"
    assert cal.info(date(2026, 10, 2)).status == "holiday"
    sat = cal.info(date(2026, 10, 3))
    assert (sat.status, sat.is_trading_day, sat.note) == (
        "session",
        True,
        "weekend special session",
    )
    assert cal.info(date(2026, 10, 4)).status == "weekend"
    assert cal.info(date(2026, 9, 30)).status == "expected"
    assert cal.trading_days() == [
        date(2026, 9, 30),
        date(2026, 10, 1),
        date(2026, 10, 3),
        date(2026, 10, 5),
    ]
    with pytest.raises(ValueError):
        cal.info(date(2026, 11, 1))
