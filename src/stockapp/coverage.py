"""Backfill coverage report and the M2 gate (Gate G1 in the PRD).

Gate: on every session with a universe, at least ``GATE_MIN_PCT`` of universe members have a price
row, and every source has a recorded earliest date. Written as Markdown to ``data/reports/`` (local
only: it lists market data) plus a gold table ``coverage_daily`` for the Data Health page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import duckdb
import polars as pl
import psycopg

from stockapp.ingest.prices import UDIFF_CUTOVER, combined_prices_sql
from stockapp.lake import Lake
from stockapp.universe import DATASET as UNIVERSE
from stockapp.universe import MAIN_BOARD_SERIES

GATE_MIN_PCT = 98.0
NOT_LOADED = "_dataset not loaded yet_\n"
SOURCES = (
    "nse_legacy_bhavcopy",
    "nse_udiff_bhavcopy",
    "nse_mto_delivery",
    "nse_index_close",
    "nse_corporate_actions",
)


@dataclass
class CoverageResult:
    markdown: str
    daily: pl.DataFrame
    gate_passed: bool


def _md_table(df: pl.DataFrame) -> str:
    if df.is_empty():
        return "_none_\n"
    cols = df.columns
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in df.iter_rows():
        lines.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    return "\n".join(lines) + "\n"


def _q(conn: psycopg.Connection, sql: str, params: dict | None = None) -> pl.DataFrame:
    rows = conn.execute(sql, params or {}).fetchall()
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def build_coverage(
    conn: psycopg.Connection, lake: Lake, today: date | None = None
) -> CoverageResult:
    today = today or date.today()
    out: list[str] = [f"# Data coverage report ({today.isoformat()})\n"]
    db = duckdb.connect()
    prices = combined_prices_sql(lake)
    series = ", ".join(f"'{s}'" for s in MAIN_BOARD_SERIES)

    # 1. sources -------------------------------------------------------------------------------
    jobs = _q(
        conn,
        """
        WITH latest AS (
            SELECT DISTINCT ON (source_id, partition_key) source_id, partition_key, status
            FROM job_runs WHERE source_id = ANY(%(s)s)
            ORDER BY source_id, partition_key, started_at DESC, job_run_id DESC
        )
        SELECT source_id, left(partition_key, 4) AS year,
               count(*) FILTER (WHERE status IN ('success', 'skipped')) AS loaded,
               count(*) FILTER (WHERE status = 'not_available') AS no_file,
               count(*) FILTER (WHERE status = 'failed') AS failed
        FROM latest GROUP BY 1, 2 ORDER BY 1, 2
        """,
        {"s": list(SOURCES)},
    )
    out.append("## 1. Partitions by source and year\n")
    out.append(
        "Latest attempt per partition. `no_file` = NSE returned 404 (holiday, weekend, or "
        "outside the source's life).\n"
    )
    out.append(_md_table(jobs))

    registry = _q(
        conn,
        """SELECT source_id, health, earliest_date, earliest_date_note,
                  last_success_at::date AS last_ok
           FROM source_registry ORDER BY source_id""",
    )
    out.append("\n## 2. Source registry\n")
    out.append(_md_table(registry))
    missing_earliest = (
        registry.filter(pl.col("earliest_date").is_null() & pl.col("source_id").is_in(SOURCES))[
            "source_id"
        ].to_list()
        if not registry.is_empty()
        else list(SOURCES)
    )

    quarantine = _q(
        conn,
        """SELECT source_id, severity, reason, count(*) AS n, min(partition_key) AS first,
                  max(partition_key) AS last
           FROM quarantine WHERE resolved_at IS NULL GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""",
    )
    out.append("\n## 3. Open quarantine\n")
    out.append(_md_table(quarantine))

    # 4. calendar ------------------------------------------------------------------------------
    cal = _q(
        conn,
        """
        SELECT y AS year,
               (SELECT count(*) FROM trading_sessions
                 WHERE extract(year FROM session_date) = y) AS sessions,
               (SELECT count(*) FROM trading_sessions WHERE extract(year FROM session_date) = y
                  AND extract(isodow FROM session_date) >= 6) AS weekend_sessions,
               (SELECT count(*) FROM trading_holidays WHERE extract(year FROM holiday_date) = y
                  AND source = 'nse_holidays'
                  AND extract(isodow FROM holiday_date) < 6) AS listed_weekday_holidays,
               (SELECT count(*) FROM trading_holidays WHERE extract(year FROM holiday_date) = y
                  AND source = 'inferred_no_file') AS inferred_holidays
        FROM generate_series(2016, %(y)s) AS y
        """,
        {"y": today.year},
    )
    out.append("\n## 4. Calendar by year\n")
    out.append(_md_table(cal))

    # 5. universe price completeness (the gate) --------------------------------------------------
    uni_glob = lake.duckdb_glob("silver", UNIVERSE)
    has = {
        d: lake.has_table("silver", d)
        for d in (
            UNIVERSE,
            "nse_cm_delivery",
            "nse_index_close",
            "nse_cm_bhavcopy_legacy",
            "nse_cm_bhavcopy",
        )
    }
    daily = pl.DataFrame(
        schema={
            "trade_date": pl.Date,
            "universe_size": pl.Int64,
            "priced": pl.Int64,
            "pct": pl.Float64,
        }
    )
    if has[UNIVERSE]:
        daily = db.sql(
            f"""
        WITH u AS (SELECT month, symbol FROM read_parquet('{uni_glob}', hive_partitioning = true)),
        sess AS (SELECT DISTINCT trade_date FROM ({prices})),
        p AS (SELECT DISTINCT trade_date, symbol FROM ({prices}) WHERE series IN ({series}))
        SELECT sess.trade_date, count(u.symbol) AS universe_size, count(p.symbol) AS priced,
               round(100.0 * count(p.symbol) / count(u.symbol), 2) AS pct
        FROM sess
        JOIN u ON u.month = strftime(sess.trade_date, '%Y-%m')
        LEFT JOIN p ON p.trade_date = sess.trade_date AND p.symbol = u.symbol
        GROUP BY 1 ORDER BY 1
        """
        ).pl()
    by_year = (
        daily.group_by(pl.col("trade_date").dt.year().alias("year"))
        .agg(
            pl.len().alias("sessions"),
            pl.col("pct").min().alias("min_pct"),
            pl.col("pct").mean().round(2).alias("mean_pct"),
            (pl.col("pct") < GATE_MIN_PCT).sum().alias(f"days_below_{GATE_MIN_PCT:g}"),
        )
        .sort("year")
    )
    out.append(f"\n## 5. Universe price completeness (gate: every session >= {GATE_MIN_PCT:g}%)\n")
    out.append(_md_table(by_year))
    worst = daily.sort("pct").head(15)
    out.append("\nWorst sessions:\n\n" + _md_table(worst))

    # 6. delivery and index completeness ---------------------------------------------------------
    deliv_glob = lake.duckdb_glob("silver", "nse_cm_delivery")
    idx_glob = lake.duckdb_glob("silver", "nse_index_close")
    deliv = idx = None
    if has[UNIVERSE] and has["nse_cm_delivery"]:
        deliv = db.sql(
            f"""
        WITH u AS (SELECT month, symbol FROM read_parquet('{uni_glob}', hive_partitioning = true)),
        p AS (SELECT DISTINCT trade_date, symbol FROM ({prices}) WHERE series IN ({series})),
        d AS (SELECT DISTINCT trade_date, symbol
              FROM read_parquet('{deliv_glob}', hive_partitioning = true))
        SELECT year(p.trade_date) AS year, count(*) AS member_days,
               round(100.0 * count(d.symbol) / count(*), 2) AS with_delivery_pct
        FROM p JOIN u ON u.month = strftime(p.trade_date, '%Y-%m') AND u.symbol = p.symbol
        LEFT JOIN d ON d.trade_date = p.trade_date AND d.symbol = p.symbol
        GROUP BY 1 ORDER BY 1
        """
        ).pl()
    if has["nse_index_close"]:
        idx = db.sql(
            f"""
        WITH sess AS (SELECT DISTINCT trade_date FROM ({prices})),
        i AS (SELECT trade_date, lower(index_name) AS name
              FROM read_parquet('{idx_glob}', hive_partitioning = true)
              WHERE close IS NOT NULL)
        SELECT year(s.trade_date) AS year, count(DISTINCT s.trade_date) AS sessions,
               count(DISTINCT CASE WHEN i.name = 'nifty 50' THEN s.trade_date END) AS nifty50,
               count(DISTINCT CASE WHEN i.name = 'nifty 500' THEN s.trade_date END) AS nifty500,
               count(DISTINCT CASE WHEN i.name = 'india vix' THEN s.trade_date END) AS india_vix
        FROM sess s LEFT JOIN i ON i.trade_date = s.trade_date GROUP BY 1 ORDER BY 1
        """
        ).pl()
    out.append("\n## 6. Delivery data for priced universe members\n")
    out.append(_md_table(deliv) if deliv is not None else NOT_LOADED)
    out.append("\n## 7. Index closes per session\n")
    out.append(_md_table(idx) if idx is not None else NOT_LOADED)

    # 8. legacy vs UDiFF cross-check ------------------------------------------------------------
    legacy_glob = lake.duckdb_glob("silver", "nse_cm_bhavcopy_legacy")
    udiff_glob = lake.duckdb_glob("silver", "nse_cm_bhavcopy")
    cross = None
    if has["nse_cm_bhavcopy_legacy"] and has["nse_cm_bhavcopy"]:
        cross = db.sql(
            f"""
        WITH l AS (SELECT * FROM read_parquet('{legacy_glob}', hive_partitioning = true)
                   WHERE trade_date >= DATE '2024-01-01'),
        n AS (SELECT * FROM read_parquet('{udiff_glob}', hive_partitioning = true)
              WHERE trade_date < DATE '{UDIFF_CUTOVER.isoformat()}')
        SELECT count(*) AS matched_rows,
               count(DISTINCT l.trade_date) AS days,
               count(*) FILTER (WHERE abs(l.close - n.close) > 0.005) AS close_mismatch,
               count(*) FILTER (WHERE l.volume <> n.volume) AS volume_mismatch,
               (SELECT count(*) FROM l) AS legacy_rows,
               (SELECT count(*) FROM n) AS udiff_rows
        FROM l JOIN n USING (trade_date, symbol, series)
        """
        ).pl()
    out.append("\n## 8. Legacy vs UDiFF cross-check (Jan to Jul 2024 overlap)\n")
    out.append(_md_table(cross) if cross is not None else NOT_LOADED)

    # gate -----------------------------------------------------------------------------------
    below = daily.filter(pl.col("pct") < GATE_MIN_PCT).height
    passed = daily.height > 0 and below == 0 and not missing_earliest
    out.insert(
        1,
        "## Gate G1\n\n"
        f"- Sessions with a universe: {daily.height}\n"
        f"- Sessions below {GATE_MIN_PCT:g}% priced: {below}\n"
        f"- Sources without a recorded earliest date: {', '.join(missing_earliest) or 'none'}\n"
        f"- **Result: {'PASS' if passed else 'FAIL'}**\n",
    )
    return CoverageResult(markdown="\n".join(out), daily=daily, gate_passed=passed)
