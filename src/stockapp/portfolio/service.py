"""Portfolio operations used by the app: load the valued portfolio, check and add transactions,
import a Kite CSV. Validation replays the full ledger with the change, so an impossible state (a
sale larger than the holding on that date) is refused before anything is saved."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import psycopg

from stockapp.config import AppConfig
from stockapp.lake import Lake
from stockapp.portfolio import store
from stockapp.portfolio.costs import order_charges
from stockapp.portfolio.kite import ParsedHoldings
from stockapp.portfolio.ledger import Ledger, LedgerError, build_ledger
from stockapp.portfolio.valuation import (
    PortfolioView,
    data_as_of,
    latest_prices,
    quantity_events,
    resolve_symbol,
    sectors_for,
    value_portfolio,
)


@dataclass
class Loaded:
    view: PortfolioView
    ledger: Ledger
    transactions: list[dict]


def _estimator(cfg: AppConfig):
    return lambda side, qty, price: order_charges(side, qty, price, cfg.costs).total


def _ledger(lake: Lake, cfg: AppConfig, rows: list[dict]) -> Ledger:
    companies = sorted({r["company_id"] for r in rows})
    return build_ledger(
        store.to_ledger_txns(rows, _estimator(cfg)), quantity_events(lake, companies)
    )


def load(conn: psycopg.Connection, lake: Lake, cfg: AppConfig, today: date) -> Loaded:
    rows = store.list_transactions(conn)
    ledger = _ledger(lake, cfg, rows)
    held = sorted({lot.company_id for lot in ledger.lots})
    as_of = data_as_of(lake)
    view = value_portfolio(
        ledger, latest_prices(lake, held), sectors_for(lake), cfg, today, as_of,
        data_is_stale=as_of is None or (today - as_of).days > 4,
    )  # fmt: skip
    return Loaded(view, ledger, rows)


def check_new(
    conn: psycopg.Connection, lake: Lake, cfg: AppConfig, new: list[store.NewTransaction]
) -> None:
    """Raise LedgerError if adding ``new`` would make the ledger impossible."""
    rows = store.list_transactions(conn)
    now = datetime.now().astimezone()
    for i, t in enumerate(new):
        rows.append(
            {"txn_id": 10**12 + i, "company_id": t.company_id, "symbol": t.symbol, "isin": t.isin,
             "side": t.side, "quantity": t.quantity, "price": t.price, "trade_date": t.trade_date,
             "charges": t.charges, "source": t.source, "note": t.note, "created_at": now}
        )  # fmt: skip
    _ledger(lake, cfg, rows)


def delete(conn: psycopg.Connection, lake: Lake, cfg: AppConfig, txn_id: int) -> None:
    """Delete a transaction, unless the ledger without it is impossible (e.g. it's a buy that a
    later sale depends on)."""
    rows = [r for r in store.list_transactions(conn) if r["txn_id"] != txn_id]
    _ledger(lake, cfg, rows)
    store.delete_transaction(conn, txn_id)


def add(
    conn: psycopg.Connection, lake: Lake, cfg: AppConfig, new: list[store.NewTransaction]
) -> list[int]:
    check_new(conn, lake, cfg, new)
    return store.add_transactions(conn, new)


@dataclass(frozen=True)
class ImportPlan:
    ready: list[store.NewTransaction]
    unmatched: list[str]


def plan_kite_import(lake: Lake, parsed: ParsedHoldings) -> ImportPlan:
    ready, unmatched = [], []
    for r in parsed.rows.iter_rows(named=True):
        hit = resolve_symbol(lake, r["symbol"])
        if hit is None:
            unmatched.append(f"{r['symbol']}: not a traded NSE main-board equity in the data")
            continue
        company_id, isin = hit
        if r["isin"] and isin and r["isin"] != isin:
            unmatched.append(f"{r['symbol']}: ISIN {r['isin']} in file, {isin} in NSE data")
            continue
        ready.append(
            store.NewTransaction(
                company_id=company_id, symbol=r["symbol"], side="BUY", quantity=r["quantity"],
                price=r["avg_price"], trade_date=None, source="kite_csv", isin=isin, charges=None,
                note="Imported from Kite holdings; buy charges estimated, buy date unknown",
            )
        )  # fmt: skip
    return ImportPlan(ready, unmatched)


__all__ = [
    "ImportPlan", "LedgerError", "Loaded", "add", "check_new", "delete", "load", "plan_kite_import",
]  # fmt: skip
