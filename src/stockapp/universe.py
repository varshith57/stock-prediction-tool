"""Point-in-time trading universe (amendment AM3): a liquidity-ranked stand-in for the Nifty 500.

For each month, the members are the top ``size`` companies by median daily traded value over the
``WINDOW`` sessions *before* the month's first session, counting only companies that traded on at
least ``MIN_TRADED_SHARE`` of those sessions. Main-board equities only (EQ, BE, BZ; no SME series,
no ETFs or fund units).

Built from NSE's own daily files, which include companies that later delisted, so history has no
survivorship bias. Membership uses only data known before the month starts. Companies come from the
company master, so a renamed stock keeps its trading history (ZOMATO and ETERNAL are one company).
"""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import polars as pl

from stockapp.ingest.registry import pipeline_version
from stockapp.lake import Lake
from stockapp.master import company_prices_sql

DATASET = "universe_top500"
WINDOW = 126  # sessions, about 6 months
MIN_TRADED_SHARE = 0.9


def universe_sql(
    company_prices: str,
    size: int = 500,
    window: int = WINDOW,
    min_traded_share: float = MIN_TRADED_SHARE,
) -> str:
    """``company_prices``: one row per company and day with company_id, symbol, trade_date,
    value_inr (see ``master.company_prices_sql``)."""
    min_traded = int(window * min_traded_share)
    return f"""
    WITH p AS (SELECT trade_date, company_id, symbol, value_inr FROM ({company_prices})),
    s AS (
        SELECT trade_date, row_number() OVER (ORDER BY trade_date) AS n
        FROM (SELECT DISTINCT trade_date FROM p)
    ),
    m AS (
        SELECT strftime(min(s.trade_date), '%Y-%m') AS month, min(s.n) AS n0
        FROM s GROUP BY date_trunc('month', s.trade_date)
    ),
    win AS (
        SELECT m.month, p.company_id, p.symbol, p.value_inr, s.trade_date
        FROM m
        JOIN s ON s.n BETWEEN m.n0 - {window} AND m.n0 - 1
        JOIN p ON p.trade_date = s.trade_date
        WHERE m.n0 > {window}
    ),
    agg AS (
        SELECT month, company_id, arg_max(symbol, trade_date) AS symbol,
               median(value_inr) AS median_value_inr,
               count(*) AS sessions_traded, max(trade_date) AS as_of
        FROM win GROUP BY 1, 2
        HAVING count(*) >= {min_traded}
    )
    SELECT month, company_id, symbol, median_value_inr, sessions_traded, as_of,
           row_number() OVER (PARTITION BY month ORDER BY median_value_inr DESC, company_id)
               AS rank
    FROM agg
    QUALIFY rank <= {size}
    ORDER BY month, rank
    """


def build_universe(lake: Lake, size: int = 500, window: int = WINDOW) -> pl.DataFrame:
    """Rebuild every month's membership and write one silver partition per month.
    ``symbol`` is the symbol the company last traded under before the month (for display)."""
    df = duckdb.sql(universe_sql(company_prices_sql(lake), size, window)).pl()
    built = datetime.now(UTC)
    version = pipeline_version()
    for (month,), part in df.partition_by("month", as_dict=True).items():
        part = part.with_columns(
            pl.lit(built).alias("_built_at"), pl.lit(version).alias("_pipeline_version")
        )
        lake.write_partition("silver", DATASET, "month", str(month), part)
    return df
