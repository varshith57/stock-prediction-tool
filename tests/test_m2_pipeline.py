"""M2 end to end against a real Postgres and a temp lake: backfill, holidays, universe, coverage."""

from __future__ import annotations

import re
from datetime import date, timedelta

import duckdb
import httpx
import polars as pl
import psycopg
import pytest
import respx
from synthetic import corp_actions_json, index_csv, legacy_zip, mto_bytes, udiff_zip

from stockapp.coverage import build_coverage
from stockapp.ingest.backfill import backfill
from stockapp.ingest.calendar import infer_holidays, record_session
from stockapp.ingest.http import PoliteClient
from stockapp.ingest.nse_index import NseIndexClose
from stockapp.ingest.nse_legacy import _MONTHS, NseLegacyBhavcopy
from stockapp.ingest.nse_mto import NseMtoDelivery
from stockapp.ingest.nse_udiff import NseUdiffBhavcopy
from stockapp.ingest.prices import combined_prices_sql
from stockapp.lake import Lake
from stockapp.universe import build_universe, universe_sql

_MON = {m: i + 1 for i, m in enumerate(_MONTHS)}


@pytest.fixture
def small_files(monkeypatch: pytest.MonkeyPatch) -> None:
    for cls in (NseLegacyBhavcopy, NseUdiffBhavcopy, NseMtoDelivery, NseIndexClose):
        monkeypatch.setattr(cls, "min_rows", 3)


def _client() -> PoliteClient:
    return PoliteClient(min_interval_s=0, backoff_s=0, retries=0, sleep=lambda _: None)


class FakeNse:
    """Serves synthetic files for weekdays in ``sessions``; 404 otherwise. Counts requests."""

    def __init__(self, sessions: set[date], legacy_until: date = date(2024, 7, 5)):
        self.sessions, self.legacy_until = sessions, legacy_until
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if m := re.search(r"cm(\d{2})([A-Z]{3})(\d{4})bhav", url):
            day = date(int(m[3]), _MON[m[2]], int(m[1]))
            ok = day in self.sessions and day <= self.legacy_until
            return httpx.Response(200, content=legacy_zip(day)) if ok else httpx.Response(404)
        if m := re.search(r"BhavCopy_NSE_CM_0_0_0_(\d{8})_", url):
            day = date(int(m[1][:4]), int(m[1][4:6]), int(m[1][6:]))
            ok = day in self.sessions and day >= date(2024, 1, 1)
            return httpx.Response(200, content=udiff_zip(day)) if ok else httpx.Response(404)
        if m := re.search(r"MTO_(\d{2})(\d{2})(\d{4})", url):
            day = date(int(m[3]), int(m[2]), int(m[1]))
            return (
                httpx.Response(200, content=mto_bytes(day))
                if day in self.sessions
                else httpx.Response(404)
            )
        if m := re.search(r"ind_close_all_(\d{2})(\d{2})(\d{4})", url):
            day = date(int(m[3]), int(m[2]), int(m[1]))
            return (
                httpx.Response(200, content=index_csv(day))
                if day in self.sessions
                else httpx.Response(404)
            )
        if m := re.search(r"from_date=\d{2}-(\d{2})-(\d{4})", url):
            return httpx.Response(200, content=corp_actions_json(date(int(m[2]), int(m[1]), 1)))
        return httpx.Response(404)


# backfill -------------------------------------------------------------------------------------

SESSIONS = {date(2024, 7, 4), date(2024, 7, 5), date(2024, 7, 8)}  # Thu, Fri, Mon (cutover)


@respx.mock
def test_backfill_across_the_cutover(db: psycopg.Connection, lake: Lake, small_files):
    fake = FakeNse(SESSIONS)
    respx.route().mock(side_effect=fake)
    stats = backfill(
        db, lake, _client(), _client(), date(2024, 7, 4), date(2024, 7, 8),
        today=date(2024, 7, 10), log=lambda _: None,
    )  # fmt: skip
    c = stats.counts
    assert c[("nse_legacy_bhavcopy", "success")] == 2
    assert c[("nse_legacy_bhavcopy", "not_available")] == 2  # the weekend
    assert c[("nse_udiff_bhavcopy", "success")] == 3  # 2 overlap days + Monday
    assert c[("nse_mto_delivery", "success")] == 3 and c[("nse_index_close", "success")] == 3
    assert c[("nse_corporate_actions", "success")] == 1
    assert not any("MTO_06072024" in u or "MTO_07072024" in u for u in fake.requests)

    by_source = duckdb.sql(
        f"SELECT price_source, count(DISTINCT trade_date) AS d FROM ({combined_prices_sql(lake)}) "
        "GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert by_source == [("nse_legacy_bhavcopy", 2), ("nse_udiff_bhavcopy", 1)]

    # rerun: nothing refetched except the recent corporate-actions month
    fake.requests.clear()
    backfill(
        db, lake, _client(), _client(), date(2024, 7, 4), date(2024, 7, 8),
        today=date(2024, 7, 10), log=lambda _: None,
    )  # fmt: skip
    assert len(fake.requests) == 3  # corporate actions July + the two weekend days (no file yet)
    assert all("corporateActions" in u or "bhav" in u for u in fake.requests)


@respx.mock
def test_backfill_skips_old_action_months_on_rerun(db: psycopg.Connection, lake: Lake, small_files):
    fake = FakeNse({date(2024, 7, 4)})
    respx.route().mock(side_effect=fake)
    args = (db, lake, _client(), _client(), date(2024, 7, 4), date(2024, 7, 4))
    backfill(*args, today=date(2024, 12, 1), log=lambda _: None)
    fake.requests.clear()
    backfill(*args, today=date(2024, 12, 1), log=lambda _: None)
    assert fake.requests == []


# inferred holidays ----------------------------------------------------------------------------


def _job(db: psycopg.Connection, source: str, day: date, status: str) -> None:
    db.execute(
        """INSERT INTO job_runs (job, source_id, partition_key, status, pipeline_version)
           VALUES ('t', %s, %s, %s, 'test')""",
        (source, day.isoformat(), status),
    )


def test_infer_holidays_only_for_clean_404_weekdays(db: psycopg.Connection):
    L = "nse_legacy_bhavcopy"
    holiday, failed, weekend, listed = (
        date(2016, 1, 26),
        date(2016, 1, 27),
        date(2016, 1, 30),
        date(2016, 3, 7),
    )
    for d in (holiday, weekend, listed):
        _job(db, L, d, "not_available")
    _job(db, L, failed, "failed")
    db.execute(
        "INSERT INTO trading_holidays VALUES ('NSE','CM',%s,'Mahashivratri','nse_holidays', now())",
        (listed,),
    )
    after_latest = date(2016, 4, 1)
    _job(db, L, after_latest, "not_available")
    record_session(db, date(2016, 3, 31), evidence="t")

    days = infer_holidays(db, date(2016, 1, 1), date(2016, 12, 31), price_sources=(L,))
    assert days == [holiday]
    # a later successful retry of the same day means it was not a holiday
    _job(db, L, holiday, "success")
    db.execute("DELETE FROM trading_holidays WHERE source = 'inferred_no_file'")
    assert infer_holidays(db, date(2016, 1, 1), date(2016, 12, 31), price_sources=(L,)) == []


# universe -------------------------------------------------------------------------------------


def _weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _write_prices(lake: Lake, rows_by_day: dict[date, list[tuple[str, str, float]]]) -> None:
    for day, rows in rows_by_day.items():
        df = pl.DataFrame(
            {
                "trade_date": [day] * len(rows),
                "symbol": [r[0] for r in rows],
                "series": [r[1] for r in rows],
                "isin": ["X"] * len(rows),
                **{
                    c: [1.0] * len(rows)
                    for c in ("open", "high", "low", "close", "last", "prev_close")
                },
                "volume": [1] * len(rows),
                "value_inr": [r[2] for r in rows],
                "trades": [1] * len(rows),
            }
        )
        lake.write_partition("silver", "nse_cm_bhavcopy_legacy", "trade_date", day.isoformat(), df)


def test_universe_is_point_in_time_liquidity_ranked(lake: Lake):
    days = _weekdays(date(2016, 1, 1), date(2016, 3, 31))
    rows: dict[date, list[tuple[str, str, float]]] = {}
    for i, d in enumerate(days):
        r = [("AAA", "EQ", 100.0), ("BBB", "BE", 50.0), ("SME", "SM", 10_000.0)]
        if i % 2 == 0:
            r.append(("GAPPY", "EQ", 5_000.0))  # big but trades only every other day
        if d.month >= 2:
            r.append(("NEWBIG", "EQ", 1_000.0))  # appears in February
        rows[d] = r
    _write_prices(lake, rows)

    df = build_universe(lake, size=2, window=10)
    feb = df.filter(pl.col("month") == "2016-02")["symbol"].to_list()
    mar = df.filter(pl.col("month") == "2016-03")["symbol"].to_list()
    assert feb == ["AAA", "BBB"]  # NEWBIG's February trading can't affect February's universe
    assert mar == ["NEWBIG", "AAA"]  # SME series and GAPPY (too few sessions) never qualify
    assert df.filter(pl.col("month") == "2016-03")["as_of"].max() == date(2016, 2, 29)
    assert lake.scan("silver", "universe_top500").collect().height == df.height


def test_universe_sql_has_no_month_before_a_full_window(lake: Lake):
    _write_prices(
        lake, {d: [("AAA", "EQ", 1.0)] for d in _weekdays(date(2016, 1, 1), date(2016, 1, 29))}
    )
    sql = universe_sql(combined_prices_sql(lake), size=5, window=30)
    assert duckdb.sql(sql).fetchall() == []


# coverage -------------------------------------------------------------------------------------


@respx.mock
def test_coverage_gate(db: psycopg.Connection, lake: Lake, small_files):
    sessions = set(_weekdays(date(2016, 1, 1), date(2016, 2, 29)))
    respx.route().mock(side_effect=FakeNse(sessions))
    backfill(db, lake, _client(), _client(), date(2016, 1, 1), date(2016, 2, 29),
             today=date(2016, 3, 1), log=lambda _: None)  # fmt: skip
    build_universe(lake, size=3, window=5)

    result = build_coverage(db, lake, today=date(2016, 3, 1))
    assert result.daily.height == 21  # February sessions with a universe
    assert result.daily["pct"].min() == 100.0
    assert not result.gate_passed  # earliest dates not recorded yet
    missing_line = result.markdown.split("recorded earliest date: ")[1].splitlines()[0]
    assert "nse_legacy_bhavcopy" in missing_line

    db.execute("UPDATE source_registry SET earliest_date = '2010-01-04'")
    assert build_coverage(db, lake, today=date(2016, 3, 1)).gate_passed

    # a member missing a day fails the gate
    feb10 = date(2016, 2, 10)
    df = (
        lake.scan("silver", "nse_cm_bhavcopy_legacy")
        .filter(pl.col("trade_date") == feb10)
        .collect()
    )
    without_alpha = df.filter(pl.col("symbol") != "ALPHAIND")
    lake.write_partition(
        "silver", "nse_cm_bhavcopy_legacy", "trade_date", "2016-02-10", without_alpha
    )
    failing = build_coverage(db, lake, today=date(2016, 3, 1))
    assert not failing.gate_passed
    assert failing.daily.filter(pl.col("trade_date") == feb10)["pct"].item() < 98
