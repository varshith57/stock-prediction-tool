"""Price quality gates (PRD section 6) on company-level, corporate-action-adjusted prices.

Every flag covers a date range for one company (``from_date``..``to_date``, inclusive):

BLOCK (the company is excluded from signals for any date the range covers)
* ``ohlc_integrity``: non-positive price, high below low, open or close outside the day's range,
  negative volume. One day.
* ``unreconciled_adjustment``: an event's adjusted move failed the continuity test, so adjusted
  prices *before* the ex-date are suspect. Covers the company's history up to the day before.
* ``possible_missing_action``: a drop matching a typical split/bonus ratio (1/2, 1/3, 1/4, 1/5,
  1/10, 2/5) with no event or break: probably an action missing from NSE's feed. Covers history
  up to the day before.
* ``unparsed_action``: a split/bonus/rights/consolidation subject that didn't parse. Covers
  history up to the day before its ex-date.

WARN (shown, counted in the score, not blocking)
* ``large_move``: an adjusted daily move beyond the 20% band with no event or break. Real moves
  this big happen (stocks without a band, results surprises); circuit-band data comes later.
* ``trading_gap``: more than 15 calendar days between consecutive trades (suspension, illiquidity).
* ``unparsed_dividend``: affects only the total-return series.
"""

from __future__ import annotations

import json
from datetime import date

import duckdb
import polars as pl

from stockapp.adjust import BREAKS, EVENTS, FACTORS, _latest, adjusted_prices_sql, continuity_check
from stockapp.lake import Lake

DATASET = "price_quality_flags"
MOVE_LIMIT = 0.20
TICK_SLACK = 0.10  # rupees
SPLIT_RATIOS = (1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 10, 2 / 5)
SPLIT_RATIO_TOLERANCE = 0.03  # relative
MIN_PRICE_FOR_SPLIT_CHECK = 10.0  # below this, one-tick moves look like ratios
GAP_DAYS = 15
SCHEMA = {
    "company_id": pl.String,
    "from_date": pl.Date,
    "to_date": pl.Date,
    "check": pl.String,
    "severity": pl.String,
    "detail": pl.String,
}


def build_price_flags(lake: Lake, as_of: date) -> pl.DataFrame:
    con = duckdb.connect()
    con.sql(f"CREATE TEMP TABLE a AS {adjusted_prices_sql(lake)}")
    con.sql(
        "CREATE TEMP TABLE first_day AS SELECT company_id, min(trade_date) AS d FROM a GROUP BY 1"
    )
    breaks, events = _latest(lake, BREAKS), _latest(lake, EVENTS)
    frames: list[pl.DataFrame] = []

    frames.append(
        con.sql(
            """SELECT company_id, trade_date AS from_date, trade_date AS to_date,
                      'ohlc_integrity' AS "check", 'BLOCK' AS severity,
                      json_object('open', open, 'high', high, 'low', low, 'close', close,
                                  'volume', volume)::VARCHAR AS detail
               FROM a
               WHERE least(open, high, low, close) <= 0 OR high < low
                  OR open > high + 1e-6 OR open < low - 1e-6
                  OR close > high + 1e-6 OR close < low - 1e-6 OR volume < 0"""
        ).pl()
    )

    moves = con.sql(
        f"""
        WITH r AS (
            SELECT company_id, symbol, trade_date, close, adj_close,
                   lag(adj_close) OVER w AS prev_adj, lag(close) OVER w AS prev_raw,
                   lag(trade_date) OVER w AS prev_date
            FROM a WINDOW w AS (PARTITION BY company_id ORDER BY trade_date)
        )
        SELECT r.*, adj_close / prev_adj - 1 AS move, close / prev_raw AS raw_ratio,
               trade_date - prev_date AS gap_days
        FROM r
        WHERE prev_adj IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM ({breaks}) b WHERE b.company_id = r.company_id
                          AND b.break_date > r.prev_date AND b.break_date <= r.trade_date)
          AND NOT EXISTS (SELECT 1 FROM ({_latest(lake, FACTORS)}) f
                          WHERE f.company_id = r.company_id AND abs(f.price_factor - 1) > 1e-9
                          AND f.ex_date > r.prev_date AND f.ex_date <= r.trade_date)
          AND (abs(adj_close / prev_adj - 1) > {MOVE_LIMIT} + {TICK_SLACK} / prev_adj
               OR trade_date - prev_date > {GAP_DAYS})
        """
    ).pl()

    big = moves.filter(
        pl.col("move").abs() > MOVE_LIMIT + TICK_SLACK / pl.col("prev_adj")
    ).with_columns(
        (
            (pl.col("raw_ratio") <= 0.55)
            & (pl.col("prev_raw") >= MIN_PRICE_FOR_SPLIT_CHECK)
            & (pl.col("gap_days") <= 10)
            & (
                pl.min_horizontal(*[((pl.col("raw_ratio") - k).abs() / k) for k in SPLIT_RATIOS])
                < SPLIT_RATIO_TOLERANCE
            )
        ).alias("split_like")
    )
    first = con.sql("SELECT * FROM first_day").pl()
    missing = big.filter("split_like").join(first, on="company_id")
    frames.append(
        missing.select(
            "company_id",
            pl.col("d").alias("from_date"),
            (pl.col("trade_date") - pl.duration(days=1)).alias("to_date"),
            pl.lit("possible_missing_action").alias("check"),
            pl.lit("BLOCK").alias("severity"),
            pl.struct("symbol", "trade_date", "prev_raw", "close", "raw_ratio")
            .map_elements(lambda s: json.dumps(s, default=str), return_dtype=pl.String)
            .alias("detail"),
        )
    )
    frames.append(
        big.filter(~pl.col("split_like")).select(
            "company_id",
            pl.col("trade_date").alias("from_date"),
            pl.col("trade_date").alias("to_date"),
            pl.lit("large_move").alias("check"),
            pl.lit("WARN").alias("severity"),
            pl.format("move {} from {} to {}", pl.col("move").round(4), "prev_raw", "close").alias(
                "detail"
            ),
        )
    )
    frames.append(
        moves.filter(pl.col("gap_days") > GAP_DAYS).select(
            "company_id",
            (pl.col("prev_date") + pl.duration(days=1)).alias("from_date"),
            (pl.col("trade_date") - pl.duration(days=1)).alias("to_date"),
            pl.lit("trading_gap").alias("check"),
            pl.lit("WARN").alias("severity"),
            pl.format("{} calendar days without a trade", "gap_days").alias("detail"),
        )
    )

    failed = continuity_check(lake).filter(~pl.col("passed")).join(first, on="company_id")
    frames.append(
        failed.select(
            "company_id",
            pl.col("d").alias("from_date"),
            (pl.col("ex_date") - pl.duration(days=1)).alias("to_date"),
            pl.lit("unreconciled_adjustment").alias("check"),
            pl.lit("BLOCK").alias("severity"),
            pl.format(
                "{} on {}: factor {}, adjusted move {}",
                "kinds",
                "ex_date",
                pl.col("price_factor").round(4),
                pl.col("adjusted_move").round(4),
            ).alias("detail"),
        )
    )

    unparsed = (
        con.sql(
            f"SELECT company_id, ex_date, kind, subject FROM ({events}) WHERE status = 'unparsed'"
        )
        .pl()
        .join(first, on="company_id", how="left")
    )
    price_kinds = ["bonus", "split", "consolidation", "rights", "other"]
    frames.append(
        unparsed.filter(pl.col("kind").is_in(price_kinds)).select(
            "company_id",
            pl.coalesce("d", "ex_date").alias("from_date"),
            (pl.col("ex_date") - pl.duration(days=1)).alias("to_date"),
            pl.lit("unparsed_action").alias("check"),
            pl.lit("BLOCK").alias("severity"),
            pl.col("subject").alias("detail"),
        )
    )
    frames.append(
        unparsed.filter(pl.col("kind") == "dividend").select(
            "company_id",
            pl.col("ex_date").alias("from_date"),
            pl.col("ex_date").alias("to_date"),
            pl.lit("unparsed_dividend").alias("check"),
            pl.lit("WARN").alias("severity"),
            pl.col("subject").alias("detail"),
        )
    )

    flags = pl.concat([f.cast(SCHEMA) for f in frames]).filter(
        pl.col("from_date") <= pl.col("to_date")
    )
    lake.write_partition("silver", DATASET, "built", as_of.isoformat(), flags)
    return flags


def flags_sql(lake: Lake) -> str:
    return _latest(lake, DATASET)
