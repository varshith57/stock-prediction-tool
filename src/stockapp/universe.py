"""Point-in-time trading universe (amendment AM3): a liquidity-ranked stand-in for the Nifty 500.

For each month, the members are the top ``size`` NSE symbols by median daily traded value over the
``WINDOW`` sessions *before* the month's first session, counting only symbols that traded on at
least ``MIN_TRADED_SHARE`` of those sessions. Main-board series only (EQ, BE, BZ; SME is excluded).

Built from NSE's own daily files, which include companies that later delisted, so history has no
survivorship bias. Membership uses only data known before the month starts.

Known limit until the company master (M3): a symbol rename breaks the trading history, so a renamed
stock can drop out of the universe for up to about six months.
"""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import polars as pl

from stockapp.ingest.prices import combined_prices_sql
from stockapp.ingest.registry import pipeline_version
from stockapp.lake import Lake

DATASET = "universe_top500"
WINDOW = 126  # sessions, about 6 months
MIN_TRADED_SHARE = 0.9
MAIN_BOARD_SERIES = ("EQ", "BE", "BZ")


def universe_sql(
    prices_sql: str,
    size: int = 500,
    window: int = WINDOW,
    min_traded_share: float = MIN_TRADED_SHARE,
) -> str:
    series = ", ".join(f"'{s}'" for s in MAIN_BOARD_SERIES)
    min_traded = int(window * min_traded_share)
    return f"""
    WITH p AS (
        SELECT trade_date, symbol, sum(value_inr) AS value_inr
        FROM ({prices_sql}) WHERE series IN ({series})
        GROUP BY 1, 2
    ),
    s AS (
        SELECT trade_date, row_number() OVER (ORDER BY trade_date) AS n
        FROM (SELECT DISTINCT trade_date FROM p)
    ),
    m AS (
        SELECT strftime(min(s.trade_date), '%Y-%m') AS month, min(s.n) AS n0
        FROM s GROUP BY date_trunc('month', s.trade_date)
    ),
    win AS (
        SELECT m.month, p.symbol, p.value_inr, s.trade_date
        FROM m
        JOIN s ON s.n BETWEEN m.n0 - {window} AND m.n0 - 1
        JOIN p ON p.trade_date = s.trade_date
        WHERE m.n0 > {window}
    ),
    agg AS (
        SELECT month, symbol, median(value_inr) AS median_value_inr,
               count(*) AS sessions_traded, max(trade_date) AS as_of
        FROM win GROUP BY 1, 2
        HAVING count(*) >= {min_traded}
    )
    SELECT month, symbol, median_value_inr, sessions_traded, as_of,
           row_number() OVER (PARTITION BY month ORDER BY median_value_inr DESC, symbol) AS rank
    FROM agg
    QUALIFY rank <= {size}
    ORDER BY month, rank
    """


def build_universe(lake: Lake, size: int = 500, window: int = WINDOW) -> pl.DataFrame:
    """Rebuild every month's membership and write one silver partition per month."""
    df = duckdb.sql(universe_sql(combined_prices_sql(lake), size, window)).pl()
    built = datetime.now(UTC)
    version = pipeline_version()
    for (month,), part in df.partition_by("month", as_dict=True).items():
        part = part.with_columns(
            pl.lit(built).alias("_built_at"), pl.lit(version).alias("_pipeline_version")
        )
        lake.write_partition("silver", DATASET, "month", str(month), part)
    return df
