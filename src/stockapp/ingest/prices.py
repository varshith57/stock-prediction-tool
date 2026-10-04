"""NSE daily equity prices: shared validation and the combined price history.

Two sources, each the only writer of its own silver dataset:

* ``nse_legacy_bhavcopy`` -> ``nse_cm_bhavcopy_legacy``: old format, last published 2024-07-05.
* ``nse_udiff_bhavcopy`` -> ``nse_cm_bhavcopy``: UDiFF format, available from at least 2024-01-02.

``combined_prices_sql`` stitches them at ``UDIFF_CUTOVER`` so every date has exactly one source. The
overlap (Jan to Jul 2024) is loaded from both and cross-checked in the coverage report.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from stockapp.ingest.base import Issue
from stockapp.lake import Lake

UDIFF_CUTOVER = date(2024, 7, 8)  # first session with no legacy file (probed 2026-10-04)
PRICE_COLUMNS = (
    "trade_date", "symbol", "series", "isin", "open", "high", "low", "close", "last",
    "prev_close", "volume", "value_inr", "trades",
)  # fmt: skip
REQUIRED = ("trade_date", "symbol", "series", "open", "high", "low", "close", "volume")


def validate_price_frame(df: pl.DataFrame, day: date, min_rows: int) -> list[Issue]:
    issues: list[Issue] = []
    if df.height < min_rows:
        issues.append(Issue("BLOCK", "too_few_rows", {"rows": df.height, "min": min_rows}))
    dates = df["trade_date"].unique().to_list()
    if dates != [day]:
        issues.append(Issue("BLOCK", "date_mismatch", {"requested": day, "found": dates[:5]}))
    nulls = {c: n for c in REQUIRED if (n := df[c].null_count())}
    if nulls:
        issues.append(Issue("BLOCK", "required_nulls", nulls))
    dups = df.group_by("symbol", "series").len().filter(pl.col("len") > 1)
    if dups.height:
        issues.append(
            Issue("BLOCK", "duplicate_keys", {"count": dups.height, "sample": dups.head(5).rows()})
        )
    return issues


def combined_prices_sql(lake: Lake) -> str:
    """DuckDB SELECT over the full price history, one source per date."""
    cols = ", ".join(PRICE_COLUMNS)
    cut = UDIFF_CUTOVER.isoformat()
    parts = []
    for source, dataset, where in (
        ("nse_legacy_bhavcopy", "nse_cm_bhavcopy_legacy", f"trade_date < DATE '{cut}'"),
        ("nse_udiff_bhavcopy", "nse_cm_bhavcopy", f"trade_date >= DATE '{cut}'"),
    ):
        if lake.has_table("silver", dataset):
            glob = lake.duckdb_glob("silver", dataset)
            parts.append(
                f"SELECT {cols}, '{source}' AS price_source "
                f"FROM read_parquet('{glob}', hive_partitioning = true, union_by_name = true) "
                f"WHERE {where}"
            )
    if not parts:
        raise ValueError("no price data in the lake yet; run a backfill first")
    return " UNION ALL ".join(parts)
