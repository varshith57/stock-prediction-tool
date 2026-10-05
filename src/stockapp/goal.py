"""Goal feasibility (PRD 8.4): how often a diversified Indian equity portfolio (the Nifty 500)
has returned at least the target in a month (21 sessions), and the band:
Realistic up to 2% a month, Stretch 2-5%, Unrealistic above 5%. Recommendations are never
changed to chase a target; this is information only."""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

from stockapp.lake import Lake


@dataclass(frozen=True)
class Feasibility:
    target: float
    band: str
    probability: float | None
    months: int
    yearly_equivalent: float


def band(target: float) -> str:
    if target <= 0.02:
        return "Realistic"
    if target <= 0.05:
        return "Stretch"
    return "Unrealistic"


def feasibility(lake: Lake, target: float) -> Feasibility:
    yearly = (1 + target) ** 12 - 1
    if not lake.has_table("silver", "nse_index_close"):
        return Feasibility(target, band(target), None, 0, yearly)
    glob = lake.duckdb_glob("silver", "nse_index_close")
    row = duckdb.sql(
        f"""
        WITH n AS (SELECT trade_date, close FROM read_parquet('{glob}', hive_partitioning = true)
                   WHERE lower(index_name) = 'nifty 500'),
        r AS (SELECT close / lag(close, 21) OVER (ORDER BY trade_date) - 1 AS ret FROM n)
        SELECT avg(CASE WHEN ret >= {float(target)} THEN 1.0 ELSE 0.0 END), count(*)
        FROM r WHERE ret IS NOT NULL
        """
    ).fetchone()
    prob, n = (row[0], int(row[1] / 21)) if row and row[1] else (None, 0)
    return Feasibility(target, band(target), prob, n, yearly)
