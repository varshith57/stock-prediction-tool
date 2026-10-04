"""Daily data quality score (0-100), one per session, weighted by domain (PRD section 6).

Components, each scaled to 0..1:

* prices (35%): share of universe members with a price row; 1 at 100%, 0 at or below 95%.
* gates (25%): share of members with no BLOCK flag covering the day; 1 at 100%, 0 at or below 90%.
* actions (15%): the session's corporate-action month is loaded (1) or not (0).
* delivery (15%): share of priced members with delivery data (EQ from NSE's file, BE/BZ 100% by
  the trade-for-trade rule); 1 at 100%, 0 at or below 90%.
* index (10%): Nifty 50, Nifty 500 and India VIX closes present (a third each).

Fundamentals, flows and event flags join the score when those domains are built (P1/P2). Below
``config.data.min_quality_score`` (default 90) the app shows NO SIGNAL with the reason.
"""

from __future__ import annotations

from datetime import date

import duckdb
import polars as pl

from stockapp.lake import Lake
from stockapp.master import company_delivery_sql, company_prices_sql
from stockapp.quality.gates import flags_sql
from stockapp.universe import DATASET as UNIVERSE

DATASET = "quality_daily"
WEIGHTS = {"prices": 0.35, "gates": 0.25, "actions": 0.15, "delivery": 0.15, "index": 0.10}


def _scale(col: str, floor: float) -> str:
    return f"greatest(0.0, least(1.0, ({col} - {floor}) / (1.0 - {floor})))"


def build_quality_scores(lake: Lake, as_of: date) -> pl.DataFrame:
    uni = lake.duckdb_glob("silver", UNIVERSE)
    idx = lake.duckdb_glob("silver", "nse_index_close")
    actions = lake.duckdb_glob("silver", "nse_corporate_actions")
    weighted = " + ".join(f"{w} * c_{k}" for k, w in WEIGHTS.items())
    df = duckdb.sql(
        f"""
        WITH p AS (SELECT trade_date, company_id, symbol FROM ({company_prices_sql(lake)})),
        sess AS (SELECT DISTINCT trade_date FROM p),
        u AS (SELECT month, company_id FROM read_parquet('{uni}', hive_partitioning = true)),
        members AS (SELECT s.trade_date, u.company_id FROM sess s
                    JOIN u ON u.month = strftime(s.trade_date, '%Y-%m')),
        blocked AS (SELECT DISTINCT m.trade_date, m.company_id FROM members m
                    JOIN ({flags_sql(lake)}) f ON f.company_id = m.company_id
                     AND f.severity = 'BLOCK'
                     AND m.trade_date BETWEEN f.from_date AND f.to_date),
        d AS (SELECT trade_date, company_id FROM ({company_delivery_sql(lake)})
              WHERE delivery_source IS NOT NULL),
        per_day AS (
            SELECT m.trade_date,
                   count(*) AS members,
                   count(p.company_id) AS priced,
                   count(b.company_id) AS blocked,
                   count(d.company_id) AS with_delivery
            FROM members m
            LEFT JOIN p ON p.trade_date = m.trade_date AND p.company_id = m.company_id
            LEFT JOIN blocked b ON b.trade_date = m.trade_date AND b.company_id = m.company_id
            LEFT JOIN d ON d.trade_date = m.trade_date AND d.company_id = p.company_id
            GROUP BY 1
        ),
        ix AS (SELECT trade_date, count(DISTINCT lower(index_name)) AS core
               FROM read_parquet('{idx}', hive_partitioning = true)
               WHERE lower(index_name) IN ('nifty 50', 'nifty 500', 'india vix')
                 AND close IS NOT NULL GROUP BY 1),
        am AS (SELECT DISTINCT month FROM read_parquet('{actions}', hive_partitioning = true)),
        comp AS (
            SELECT pd.*, coalesce(ix.core, 0) AS core_indices,
                   {_scale("priced / members", 0.95)} AS c_prices,
                   {_scale("1 - blocked / members", 0.90)} AS c_gates,
                   CASE WHEN am.month IS NOT NULL THEN 1.0 ELSE 0.0 END AS c_actions,
                   {_scale("CASE WHEN priced = 0 THEN 0 ELSE with_delivery / priced END", 0.90)}
                       AS c_delivery,
                   coalesce(ix.core, 0) / 3.0 AS c_index
            FROM per_day pd
            LEFT JOIN ix USING (trade_date)
            LEFT JOIN am ON am.month = strftime(pd.trade_date, '%Y-%m')
        )
        SELECT *, round(100 * ({weighted}), 2) AS score FROM comp ORDER BY trade_date
        """
    ).pl()
    lake.write_partition("gold", DATASET, "built", as_of.isoformat(), df)
    return df
