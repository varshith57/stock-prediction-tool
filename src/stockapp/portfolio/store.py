"""Postgres access for portfolio transactions. Holdings data never leaves this database: no
logging of rows or values here, and nothing in this module writes to the lake or to alerts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import psycopg

from stockapp.portfolio.ledger import Txn


@dataclass(frozen=True)
class NewTransaction:
    company_id: str
    symbol: str
    side: str
    quantity: int
    price: float
    trade_date: date | None
    source: str = "manual"
    isin: str | None = None
    charges: float | None = None  # None: estimate from the cost preset
    note: str | None = None


def add_transactions(conn: psycopg.Connection, txns: list[NewTransaction]) -> list[int]:
    ids: list[int] = []
    with conn.transaction():
        for t in txns:
            row = conn.execute(
                """INSERT INTO portfolio_transactions
                       (company_id, symbol, isin, side, quantity, price, trade_date, charges,
                        source, note)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING txn_id""",
                (t.company_id, t.symbol, t.isin, t.side, t.quantity, t.price, t.trade_date,
                 t.charges, t.source, t.note),
            ).fetchone()  # fmt: skip
            assert row is not None
            ids.append(int(row["txn_id"]))
    return ids


def update_transaction(conn: psycopg.Connection, txn_id: int, **fields: object) -> None:
    allowed = {"quantity", "price", "trade_date", "charges", "note", "side"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"can't update {sorted(bad)}")
    if not fields:
        return
    sets = ", ".join(f"{k} = %s" for k in fields)
    conn.execute(
        f"UPDATE portfolio_transactions SET {sets}, updated_at = now() WHERE txn_id = %s",
        (*fields.values(), txn_id),
    )


def delete_transaction(conn: psycopg.Connection, txn_id: int) -> None:
    conn.execute("DELETE FROM portfolio_transactions WHERE txn_id = %s", (txn_id,))


def list_transactions(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(
        """SELECT txn_id, company_id, symbol, isin, side, quantity, price::float8 AS price,
                  trade_date, charges::float8 AS charges, source, note, created_at
           FROM portfolio_transactions ORDER BY coalesce(trade_date, created_at::date), txn_id"""
    ).fetchall()


def to_ledger_txns(rows: list[dict], estimate_charges) -> list[Txn]:
    """``estimate_charges(side, quantity, price) -> float`` fills charges left empty."""
    out = []
    for r in rows:
        charges = r["charges"]
        if charges is None:
            charges = estimate_charges(r["side"], r["quantity"], r["price"])
        out.append(
            Txn(
                txn_id=r["txn_id"], company_id=r["company_id"], side=r["side"],
                quantity=r["quantity"], price=r["price"], trade_date=r["trade_date"],
                effective_date=r["trade_date"] or r["created_at"].date(), charges=charges,
            )
        )  # fmt: skip
    return out


STYLES = ("investment", "trade")


def holding_styles(conn: psycopg.Connection) -> dict[str, str]:
    """company_id -> 'trade' | 'investment'. Holdings without a row are investments."""
    return {r["company_id"]: r["style"] for r in conn.execute("SELECT * FROM holding_styles")}


def set_holding_style(conn: psycopg.Connection, company_id: str, style: str) -> None:
    if style not in STYLES:
        raise ValueError(f"style must be one of {STYLES}")
    conn.execute(
        """INSERT INTO holding_styles (company_id, style) VALUES (%s, %s)
           ON CONFLICT (company_id) DO UPDATE SET style = excluded.style, updated_at = now()""",
        (company_id, style),
    )
