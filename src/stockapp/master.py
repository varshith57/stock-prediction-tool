"""Company master: a time-aware map from (symbol, date) to a stable ``company_id``.

Symbols change (ZOMATO -> ETERNAL in 2025) and are sometimes reused by a different company after a
change (KPIT -> BSOFT in 2019). So each symbol's history is cut into segments at its change dates,
and NSE's symbol-change list links the old symbol's segment before a change to the new symbol's
segment from the change date. Linked segments are one company; its ``company_id`` is the symbol of
its latest segment (the name it trades under now, or last traded under).

Only main-board series (EQ, BE, BZ) are considered, matching the universe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import duckdb
import polars as pl

from stockapp.ingest.prices import EQUITY_ISIN_SQL, MAIN_BOARD_SERIES, combined_prices_sql
from stockapp.lake import Lake

DATASET = "company_symbol_map"
OPEN_START, OPEN_END = date(1900, 1, 1), date(9999, 12, 31)


@dataclass(frozen=True)
class Segment:
    symbol: str
    start: date  # inclusive
    end: date  # inclusive


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[Segment, Segment] = {}

    def find(self, x: Segment) -> Segment:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: Segment, b: Segment) -> None:
        self.parent[self.find(a)] = self.find(b)


def build_symbol_map(observed: pl.DataFrame, changes: pl.DataFrame) -> pl.DataFrame:
    """``observed``: symbol, first_seen, last_seen (from prices). ``changes``: old_symbol,
    new_symbol, change_date. Returns one row per segment with its company_id."""
    seen = {r["symbol"]: (r["first_seen"], r["last_seen"]) for r in observed.iter_rows(named=True)}
    changes = changes.filter(
        pl.col("old_symbol").is_in(list(seen)) | pl.col("new_symbol").is_in(list(seen))
    ).filter(pl.col("old_symbol") != pl.col("new_symbol"))

    cuts: dict[str, set[date]] = {s: set() for s in seen}
    for r in changes.iter_rows(named=True):
        cuts.setdefault(r["old_symbol"], set()).add(r["change_date"])
        cuts.setdefault(r["new_symbol"], set()).add(r["change_date"])

    segments: dict[str, list[Segment]] = {}
    for sym, dates in cuts.items():
        bounds = sorted(dates)
        starts = [OPEN_START, *bounds]
        ends = [*(d - timedelta(days=1) for d in bounds), OPEN_END]
        segments[sym] = [Segment(sym, s, e) for s, e in zip(starts, ends, strict=True)]

    def segment_at(sym: str, day: date) -> Segment:
        return next(s for s in segments[sym] if s.start <= day <= s.end)

    uf = _UnionFind()
    for segs in segments.values():
        for s in segs:
            uf.find(s)
    for r in changes.sort("change_date").iter_rows(named=True):
        d = r["change_date"]
        uf.union(segment_at(r["old_symbol"], d - timedelta(days=1)), segment_at(r["new_symbol"], d))

    # keep only segments that actually traded; name each company after its latest traded segment
    def traded(s: Segment) -> tuple[date, date] | None:
        if s.symbol not in seen:
            return None
        first, last = seen[s.symbol]
        lo, hi = max(first, s.start), min(last, s.end)
        return (lo, hi) if lo <= hi else None

    groups: dict[Segment, list[tuple[Segment, tuple[date, date]]]] = {}
    for segs in segments.values():
        for s in segs:
            span = traded(s)
            if span is not None:
                groups.setdefault(uf.find(s), []).append((s, span))

    rows = []
    for members in groups.values():
        latest = max(members, key=lambda m: (m[1][1], m[0].start))[0]
        for s, (lo, hi) in members:
            rows.append((latest.symbol, s.symbol, s.start, s.end, lo, hi))
    return pl.DataFrame(
        rows,
        schema={
            "company_id": pl.String,
            "symbol": pl.String,
            "valid_from": pl.Date,
            "valid_to": pl.Date,
            "first_traded": pl.Date,
            "last_traded": pl.Date,
        },
        orient="row",
    ).sort("company_id", "valid_from")


def observed_symbols(lake: Lake) -> pl.DataFrame:
    series = ", ".join(f"'{s}'" for s in MAIN_BOARD_SERIES)
    return duckdb.sql(
        f"""SELECT symbol, min(trade_date) AS first_seen, max(trade_date) AS last_seen
            FROM ({combined_prices_sql(lake)}) WHERE series IN ({series}) GROUP BY 1"""
    ).pl()


def latest_symbol_changes(lake: Lake) -> pl.DataFrame:
    df = lake.scan("silver", "nse_symbol_changes").collect()
    if df.is_empty():
        raise ValueError(
            "no symbol-change snapshot loaded; run: stockapp ingest nse-symbol-changes"
        )
    latest = df["snapshot_date"].max()
    return df.filter(pl.col("snapshot_date") == latest).select(
        "old_symbol", "new_symbol", "change_date"
    )


def build_company_master(lake: Lake, as_of: date) -> pl.DataFrame:
    df = build_symbol_map(observed_symbols(lake), latest_symbol_changes(lake))
    lake.write_partition("silver", DATASET, "built", as_of.isoformat(), df)
    return df


def company_map_sql(lake: Lake) -> str:
    """The latest company map, for joins: ``p.symbol = m.symbol AND p.trade_date BETWEEN
    m.valid_from AND m.valid_to``."""
    glob = lake.duckdb_glob("silver", DATASET)
    return (
        f"SELECT * FROM read_parquet('{glob}', hive_partitioning = true) "
        f"WHERE built = (SELECT max(built) FROM read_parquet('{glob}', hive_partitioning = true))"
    )


def company_prices_sql(lake: Lake) -> str:
    """One main-board equity row per company and day (EQ preferred over BE over BZ), with
    company_id. ETFs and fund units (ISIN prefix INF) are excluded: they aren't companies."""
    series = ", ".join(f"'{s}'" for s in MAIN_BOARD_SERIES)
    return f"""
    SELECT m.company_id, p.symbol, p.series, p.isin, p.trade_date, p.open, p.high, p.low,
           p.close, p.last, p.prev_close, p.volume, p.value_inr, p.trades, p.price_source
    FROM ({combined_prices_sql(lake)}) p
    JOIN ({company_map_sql(lake)}) m
      ON m.symbol = p.symbol AND p.trade_date BETWEEN m.valid_from AND m.valid_to
    WHERE p.series IN ({series})
      AND {EQUITY_ISIN_SQL}  -- isin is unambiguous here (only p has it)
    QUALIFY row_number() OVER (
        PARTITION BY m.company_id, p.trade_date
        ORDER BY CASE p.series WHEN 'EQ' THEN 0 WHEN 'BE' THEN 1 ELSE 2 END) = 1
    """


def company_delivery_sql(lake: Lake) -> str:
    """Delivery per company-day. EQ rows come from NSE's MTO file; BE and BZ are trade-for-trade
    series where every trade settles by delivery, and the MTO file never lists them (0 of 508k
    rows, 2016-2026), so for those delivery is 100% of traded quantity *by exchange rule*. The
    ``delivery_source`` column says which, so derived values are never mistaken for reported ones.
    """
    mto = lake.duckdb_glob("silver", "nse_cm_delivery")
    return f"""
    SELECT p.company_id, p.symbol, p.series, p.trade_date, p.volume,
           CASE WHEN p.series IN ('BE', 'BZ') THEN p.volume ELSE d.deliverable_qty END
               AS deliverable_qty,
           CASE WHEN p.series IN ('BE', 'BZ') THEN 100.0 ELSE d.delivery_pct END AS delivery_pct,
           CASE WHEN p.series IN ('BE', 'BZ') THEN 'rule_trade_for_trade'
                WHEN d.symbol IS NOT NULL THEN 'nse_mto' END AS delivery_source
    FROM ({company_prices_sql(lake)}) p
    LEFT JOIN read_parquet('{mto}', hive_partitioning = true) d
      ON d.trade_date = p.trade_date AND d.symbol = p.symbol AND d.series = p.series
    """
