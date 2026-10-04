"""Portfolio valuation from the ledger and the latest official closes.

Every number shown carries its basis: prices are the NSE official close of ``price_date`` (EOD;
intraday quotes were cut from v1), and a holding is tagged ``stale`` when its last price is older
than the latest session in the data (e.g. a suspended stock), or the data itself is behind the
exchange calendar. New actions are never suggested on stale prices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import duckdb
import polars as pl

from stockapp.adjust import EVENTS, _latest
from stockapp.config import AppConfig
from stockapp.lake import Lake
from stockapp.master import company_map_sql, company_prices_sql
from stockapp.portfolio.costs import estimated_tax, is_long_term, order_charges
from stockapp.portfolio.ledger import Ledger, QuantityEvent


@dataclass
class PortfolioView:
    holdings: pl.DataFrame
    totals: dict[str, float]
    warnings: list[str] = field(default_factory=list)
    data_as_of: date | None = None


def value_portfolio(
    ledger: Ledger,
    prices: pl.DataFrame,  # company_id, symbol, close, price_date, prev_close
    sectors: dict[str, str],
    cfg: AppConfig,
    today: date,
    data_as_of: date | None,
    data_is_stale: bool = False,
) -> PortfolioView:
    price_by = {r["company_id"]: r for r in prices.iter_rows(named=True)}
    per_company: dict[str, dict] = {}
    warnings: list[str] = []
    for lot in ledger.lots:
        p = price_by.get(lot.company_id)
        h = per_company.setdefault(
            lot.company_id,
            {"company_id": lot.company_id, "quantity": 0, "invested": 0.0, "value": None,
             "tax_if_sold": 0.0, "tax_unknown": False, "lots": 0},
        )  # fmt: skip
        h["quantity"] += lot.quantity
        h["invested"] += lot.cost
        h["lots"] += 1
        if p is None:
            continue
        lot_value = lot.quantity * p["close"]
        h["value"] = (h["value"] or 0.0) + lot_value
        sell = order_charges("SELL", lot.quantity, p["close"], cfg.costs).total
        tax = estimated_tax(lot_value - sell - lot.cost, is_long_term(lot.buy_date, today), cfg.tax)
        if tax is None:
            h["tax_unknown"] = True
        else:
            h["tax_if_sold"] += tax

    rows = []
    for cid, h in per_company.items():
        p = price_by.get(cid)
        if p is None:
            warnings.append(f"{cid}: no price in the data; excluded from totals")
        stale = (
            p is None or data_is_stale or (data_as_of is not None and p["price_date"] < data_as_of)
        )
        rows.append(
            {
                **h,
                "symbol": p["symbol"] if p else cid,
                "sector": sectors.get(cid, "Unclassified"),
                "avg_cost": h["invested"] / h["quantity"],
                "last_price": p["close"] if p else None,
                "price_date": p["price_date"] if p else None,
                "day_change": (p["close"] - p["prev_close"]) * h["quantity"]
                if p and p["prev_close"] is not None
                else None,
                "sell_charges": order_charges("SELL", h["quantity"], p["close"], cfg.costs).total
                if p
                else None,
                "stale": stale,
            }
        )
    holdings = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    if holdings.is_empty():
        return PortfolioView(holdings, _empty_totals(ledger), warnings, data_as_of)

    total_value = holdings["value"].fill_null(0).sum()
    holdings = holdings.with_columns(
        (pl.col("value") - pl.col("invested")).alias("unrealised"),
        ((pl.col("value") / pl.col("invested") - 1) * 100).alias("unrealised_pct"),
        (pl.col("value") / total_value * 100 if total_value else pl.lit(None)).alias("weight_pct"),
    ).sort("value", descending=True, nulls_last=True)

    stock_cap, sector_cap = cfg.risk.max_stock_weight * 100, cfg.risk.max_sector_weight * 100
    # compare at the precision shown (one decimal), so "15.0% (cap 15%)" never appears
    for r in holdings.filter(pl.col("weight_pct").round(1) > stock_cap).iter_rows(named=True):
        warnings.append(
            f"{r['symbol']} is {r['weight_pct']:.1f}% of the portfolio (cap {stock_cap:g}%)"
        )
    sector_w = holdings.group_by("sector").agg(pl.col("weight_pct").sum())
    for r in sector_w.filter(pl.col("weight_pct").round(1) > sector_cap).iter_rows(named=True):
        warnings.append(f"Sector {r['sector']} is {r['weight_pct']:.1f}% (cap {sector_cap:g}%)")

    invested_priced = holdings.filter(pl.col("value").is_not_null())["invested"].sum()
    realised_gain = sum(r.gain for r in ledger.realised)
    totals = {
        "invested": round(holdings["invested"].sum(), 2),
        "value": round(total_value, 2),
        "unrealised": round(total_value - invested_priced, 2),
        "realised": round(realised_gain, 2),
        "day_change": round(holdings["day_change"].fill_null(0).sum(), 2),
        "tax_if_sold": round(holdings["tax_if_sold"].sum(), 2),
    }
    return PortfolioView(holdings, totals, warnings, data_as_of)


def _empty_totals(ledger: Ledger) -> dict[str, float]:
    realised = round(sum(r.gain for r in ledger.realised), 2)
    return {"invested": 0.0, "value": 0.0, "unrealised": 0.0, "realised": realised,
            "day_change": 0.0, "tax_if_sold": 0.0}  # fmt: skip


# market data for the companies held ----------------------------------------------------------


LATEST = "latest_prices"


def build_latest_prices(lake: Lake, as_of: date) -> pl.DataFrame:
    """Gold table the app reads: each company's last close, the close before it, and its current
    symbol and ISIN. Small (one row per company), so pages load fast."""
    from stockapp.ingest.prices import combined_prices_sql

    df = duckdb.sql(
        f"""
        WITH r AS (SELECT company_id, symbol, isin, trade_date, close,
                          row_number() OVER (PARTITION BY company_id ORDER BY trade_date DESC) AS rn
                   FROM ({company_prices_sql(lake)}))
        SELECT a.company_id, a.symbol, a.isin, a.close, a.trade_date AS price_date,
               b.close AS prev_close,
               (SELECT max(trade_date) FROM ({combined_prices_sql(lake)})) AS data_as_of
        FROM r a LEFT JOIN r b ON b.company_id = a.company_id AND b.rn = 2
        WHERE a.rn = 1
        """
    ).pl()
    lake.write_partition("gold", LATEST, "built", as_of.isoformat(), df)
    return df


def load_latest_prices(lake: Lake) -> pl.DataFrame:
    df = lake.scan("gold", LATEST).collect()
    return df.filter(pl.col("built") == pl.col("built").max()).drop("built")


def latest_prices(lake: Lake, company_ids: list[str]) -> pl.DataFrame:
    return (
        load_latest_prices(lake)
        .filter(pl.col("company_id").is_in(company_ids))
        .select("company_id", "symbol", "close", "price_date", "prev_close")
    )


def data_as_of(lake: Lake) -> date | None:
    df = load_latest_prices(lake)
    return None if df.is_empty() else df["data_as_of"].max()


def sectors_for(lake: Lake) -> dict[str, str]:
    """company_id -> NSE industry, via the company's current symbol in the latest sector list."""
    if not lake.has_table("silver", "nse_sector_list"):
        return {}
    glob = lake.duckdb_glob("silver", "nse_sector_list")
    rows = duckdb.sql(
        f"""
        WITH s AS (SELECT * FROM read_parquet('{glob}', hive_partitioning = true)
                   WHERE snapshot_date = (SELECT max(snapshot_date)
                                          FROM read_parquet('{glob}', hive_partitioning = true)))
        SELECT m.company_id, s.sector FROM s
        JOIN ({company_map_sql(lake)}) m ON m.symbol = s.symbol AND m.valid_to = DATE '9999-12-31'
        """
    ).fetchall()
    return dict(rows)


def quantity_events(lake: Lake, company_ids: list[str]) -> list[QuantityEvent]:
    if not company_ids or not lake.has_table("silver", EVENTS):
        return []
    ids = ", ".join(f"'{c}'" for c in company_ids)
    df = duckdb.sql(
        f"""SELECT company_id, ex_date, kind, ratio_new, ratio_held, from_fv, to_fv
            FROM ({_latest(lake, EVENTS)})
            WHERE status = 'parsed' AND kind IN ('bonus', 'split', 'consolidation')
              AND company_id IN ({ids})"""
    ).pl()
    return [QuantityEvent(**r) for r in df.iter_rows(named=True)]


def resolve_symbol(lake: Lake, symbol: str) -> tuple[str, str | None] | None:
    """Current NSE symbol -> (company_id, isin). None if not a traded main-board equity."""
    hit = load_latest_prices(lake).filter(pl.col("symbol") == symbol.strip().upper())
    if hit.is_empty():
        return None
    row = hit.sort("price_date", descending=True).row(0, named=True)
    return row["company_id"], row["isin"]
