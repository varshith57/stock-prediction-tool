"""NSE trading calendar.

Two kinds of evidence, never mixed up:

* **Sessions**: dates for which NSE actually published a daily file. Authoritative for the past and
  the only way to see weekend special sessions (e.g. a Saturday Budget-day session).
* **Holidays**: NSE's published list (current year only), plus past weekdays later confirmed to have
  no file, which M2 records with source ``inferred_no_file``.

So for any date we can say *why* it is or isn't a trading day, and a "missing day" is never confused
with "no data because holiday".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

import psycopg

from stockapp.ingest import registry as reg
from stockapp.ingest.http import FetchError, PoliteClient
from stockapp.lake import Lake

EXCHANGE, SEGMENT = "NSE", "CM"
HOLIDAY_SOURCE_ID = "nse_holidays"
HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
HOLIDAY_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/resources/exchange-communication-holidays",
}

DayStatus = Literal["session", "weekend", "holiday", "expected"]


def record_session(conn: psycopg.Connection, day: date, *, evidence: str) -> None:
    conn.execute(
        """INSERT INTO trading_sessions (exchange, segment, session_date, evidence)
           VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
        (EXCHANGE, SEGMENT, day, evidence),
    )


def parse_holidays(payload: dict) -> list[tuple[date, str]]:
    """Capital-market (CM) holidays from NSE's holiday-master JSON."""
    rows = payload.get(SEGMENT)
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"holiday payload has no {SEGMENT!r} list; keys={list(payload)[:20]}")
    out = []
    for r in rows:
        day = datetime.strptime(r["tradingDate"].strip(), "%d-%b-%Y").date()
        out.append((day, str(r.get("description", "")).strip()))
    return sorted(out)


def refresh_holidays(
    conn: psycopg.Connection, lake: Lake, http: PoliteClient, *, today: date | None = None
) -> int:
    """Fetch the current list, archive it raw, and make the table match it for those years."""
    key = (today or date.today()).isoformat()
    job_id = reg.start_job(conn, f"ingest:{HOLIDAY_SOURCE_ID}", HOLIDAY_SOURCE_ID, key)
    try:
        content = http.get(HOLIDAY_URL, headers=HOLIDAY_HEADERS).content
    except FetchError as exc:
        reg.record_failure(conn, HOLIDAY_SOURCE_ID, "degraded", str(exc))
        reg.finish_job(conn, job_id, "failed", message=str(exc))
        raise

    raw = lake.write_raw(HOLIDAY_SOURCE_ID, key, "holiday-master.json", content)
    file_id = reg.upsert_source_file(
        conn,
        source_id=HOLIDAY_SOURCE_ID,
        partition_key=key,
        url=HOLIDAY_URL,
        raw=raw,
        lake_root=lake.root,
        schema_fingerprint=None,
        job_run_id=job_id,
    )
    try:
        holidays = parse_holidays(json.loads(content))
    except (ValueError, KeyError, TypeError) as exc:
        reg.quarantine(
            conn,
            source_id=HOLIDAY_SOURCE_ID,
            partition_key=key,
            severity="BLOCK",
            reason="parse_error",
            detail={"error": repr(exc)[:500]},
            source_file_id=file_id,
        )
        reg.set_source_file_status(conn, file_id, "quarantined")
        reg.record_failure(conn, HOLIDAY_SOURCE_ID, "format_changed", "parse_error")
        reg.finish_job(conn, job_id, "failed", message="parse_error")
        raise

    years = sorted({d.year for d, _ in holidays})
    with conn.transaction():
        conn.execute(
            """DELETE FROM trading_holidays WHERE exchange = %s AND segment = %s
                   AND source = %s AND extract(year FROM holiday_date) = ANY(%s)""",
            (EXCHANGE, SEGMENT, HOLIDAY_SOURCE_ID, years),
        )
        for day, desc in holidays:
            conn.execute(
                """INSERT INTO trading_holidays
                       (exchange, segment, holiday_date, description, source)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (exchange, segment, holiday_date) DO UPDATE
                       SET description = EXCLUDED.description, source = EXCLUDED.source,
                           recorded_at = now()""",
                (EXCHANGE, SEGMENT, day, desc, HOLIDAY_SOURCE_ID),
            )
    reg.set_source_file_status(conn, file_id, "loaded")
    reg.record_success(conn, HOLIDAY_SOURCE_ID)
    reg.finish_job(conn, job_id, "success", rows_loaded=len(holidays))
    return len(holidays)


@dataclass(frozen=True)
class DayInfo:
    day: date
    status: DayStatus
    is_trading_day: bool
    note: str = ""


class TradingCalendar:
    """Calendar view over the sessions and holidays tables for a date range."""

    def __init__(self, conn: psycopg.Connection, start: date, end: date):
        self.start, self.end = start, end
        self._sessions = {
            r["session_date"]
            for r in conn.execute(
                """SELECT session_date FROM trading_sessions WHERE exchange = %s AND segment = %s
                       AND session_date BETWEEN %s AND %s""",
                (EXCHANGE, SEGMENT, start, end),
            )
        }
        self._holidays = {
            r["holiday_date"]: f"{r['description']} ({r['source']})"
            for r in conn.execute(
                """SELECT holiday_date, description, source FROM trading_holidays
                   WHERE exchange = %s AND segment = %s AND holiday_date BETWEEN %s AND %s""",
                (EXCHANGE, SEGMENT, start, end),
            )
        }

    def info(self, day: date) -> DayInfo:
        if not self.start <= day <= self.end:
            raise ValueError(f"{day} is outside the loaded range {self.start}..{self.end}")
        if day in self._sessions:
            note = "weekend special session" if day.weekday() >= 5 else ""
            return DayInfo(day, "session", True, note)
        if day in self._holidays:
            return DayInfo(day, "holiday", False, self._holidays[day])
        if day.weekday() >= 5:
            return DayInfo(day, "weekend", False)
        return DayInfo(day, "expected", True, "weekday, not a listed holiday, no file recorded yet")

    def trading_days(self) -> list[date]:
        days, d = [], self.start
        while d <= self.end:
            if self.info(d).is_trading_day:
                days.append(d)
            d += timedelta(days=1)
        return days
