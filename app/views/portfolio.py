"""Portfolio: holdings valued at the latest NSE close, gain/loss after charges, sector weights and
cap warnings, value vs the Nifty 50 for the same cash flows, and adding or importing trades.

Holdings are private: they're read from and written to the app database only, and never logged.
"""

from __future__ import annotations

from datetime import date

import polars as pl
import streamlit as st
from views import ui
from views.common import cfg, inr, lake

from stockapp.db import connect
from stockapp.portfolio import service
from stockapp.portfolio.costs import order_charges
from stockapp.portfolio.kite import ImportFormatError, parse_holdings_csv
from stockapp.portfolio.store import NewTransaction
from stockapp.portfolio.valuation import load_latest_prices, resolve_symbol

MONEY = st.column_config.NumberColumn


@st.cache_data(ttl=600)
def _symbols() -> list[str]:
    return sorted(load_latest_prices(lake())["symbol"].to_list())


def render() -> None:
    today = date.today()
    with connect() as conn:
        loaded = service.load(conn, lake(), cfg(), today)
    view, t = loaded.view, loaded.view.totals
    subtitle = (
        f"Valued at the NSE close of {view.data_as_of:%a %d %b %Y} · end of day, no intraday quotes"
        if view.data_as_of
        else "No prices yet"
    )
    ui.header("Portfolio", subtitle)

    unreal_pct = (t["unrealised"] / t["invested"]) if t["invested"] else None
    ui.kpis(
        [
            ("Current value", inr(t["value"]), None, "grey"),
            ("Invested", inr(t["invested"]), "incl. buy charges", "grey"),
            (
                "Unrealised",
                inr(t["unrealised"]),
                f"{unreal_pct:+.2%}" if unreal_pct is not None else None,
                ui.tone_for(t["unrealised"]),
            ),
            ("Realised", inr(t["realised"]), None, "grey"),
            ("Today", inr(t["day_change"]), None, ui.tone_for(t["day_change"])),
        ]
    )
    if view.warnings:
        st.warning("**Check these:**\n" + "\n".join(f"- {w}" for w in view.warnings))

    h = view.holdings
    if h.is_empty():
        ui.hero(
            "No holdings yet",
            "Add a trade below, or import your holdings CSV from Kite.",
        )
    else:
        if h["stale"].any():
            st.error("Some prices are stale: no new actions on those until they trade again.")
        ui.section("Holdings")
        table = h.select(
            pl.col("symbol").alias("Stock"),
            pl.col("sector").alias("Sector"),
            pl.col("quantity").alias("Qty"),
            pl.col("avg_cost").alias("Avg cost"),
            pl.col("last_price").alias("Last"),
            pl.col("value").alias("Value"),
            pl.col("unrealised").alias("Gain / loss"),
            (pl.col("unrealised_pct") / 100).alias("Return"),
            (pl.col("weight_pct") / 100).alias("Weight"),
            pl.when(pl.col("stale"))
            .then(pl.lit("Stale price"))
            .otherwise(pl.lit("Hold"))
            .alias("Status"),
        )
        st.dataframe(
            table,
            hide_index=True,
            width="stretch",
            column_config={
                "Avg cost": MONEY(format="₹%.2f"),
                "Last": MONEY(format="₹%.2f"),
                "Value": MONEY(format="₹%.0f"),
                "Gain / loss": MONEY(format="₹%.0f"),
                "Return": MONEY(format="percent"),
                "Weight": st.column_config.ProgressColumn(
                    format="percent", min_value=0.0, max_value=1.0
                ),
            },
        )
        ui.muted(
            f"Estimated tax if everything were sold today: {inr(t['tax_if_sold'])} (indicative)."
        )
        _chart()

    ui.section("Trades")
    add, imp, txns, realised = st.tabs(
        [
            "Add a trade",
            "Import from Kite",
            f"Transactions ({len(loaded.transactions)})",
            f"Realised ({len(loaded.ledger.realised)})",
        ]
    )
    with add:
        _add_trade()
    with imp:
        _import_kite()
    with txns:
        _transactions(loaded)
    with realised:
        _realised(loaded)


@st.cache_data(ttl=300)
def _history() -> pl.DataFrame:
    with connect() as conn:
        return service.history(conn, lake(), cfg())


def _chart() -> None:
    hist = _history()
    if hist.height < 2:
        return
    ui.section("Value vs Nifty 50 (same cash flows)")
    chart = hist.select(
        pl.col("trade_date").alias("Date"),
        pl.col("value").alias("Your portfolio"),
        pl.col("benchmark_value").alias("Nifty 50"),
    )
    st.line_chart(
        chart, x="Date", y=["Your portfolio", "Nifty 50"], color=[ui.INK, ui.RED], height=260
    )
    ui.muted(
        "The Nifty 50 line invests the same rupees on the same days. It's the price index "
        "(dividends not included), so it slightly understates the benchmark."
    )


def _add_trade() -> None:
    a, b, c = st.columns([2, 1, 1])
    symbol = a.selectbox("Stock (NSE symbol)", _symbols(), index=None, placeholder="Type to search")
    side = b.segmented_control("Side", ["BUY", "SELL"], default="BUY")
    trade_date = c.date_input("Trade date", value=date.today(), max_value=date.today())
    d, e, f = st.columns(3)
    qty = d.number_input("Quantity", min_value=1, step=1, value=1)
    price = e.number_input("Price per share (₹)", min_value=0.01, step=0.05, format="%.2f")
    charges_text = f.text_input("Charges (₹)", value="", placeholder="Blank = estimate")
    note = st.text_input("Note", placeholder="Optional")
    est = order_charges(side or "BUY", int(qty), float(price), cfg().costs)
    ui.muted(
        f"Estimated charges {inr(est.total, 2)}: STT {inr(est.stt, 2)} · exchange "
        f"{inr(est.exchange, 2)} · SEBI {inr(est.sebi, 2)} · GST {inr(est.gst, 2)} · stamp "
        f"{inr(est.stamp, 2)} · DP {inr(est.dp, 2)}"
    )
    if st.button("Save trade", type="primary", disabled=symbol is None or side is None):
        try:
            charges = float(charges_text) if charges_text.strip() else None
        except ValueError:
            st.error("Charges must be a number, or blank to estimate.")
            return
        hit = resolve_symbol(lake(), symbol)
        if hit is None:
            st.error(f"{symbol} is not a traded NSE main-board equity in the data.")
            return
        new = NewTransaction(
            company_id=hit[0],
            symbol=symbol,
            side=side,
            quantity=int(qty),
            price=float(price),
            trade_date=trade_date,
            isin=hit[1],
            charges=charges,
            note=note or None,
        )
        try:
            with connect() as conn:
                service.add(conn, lake(), cfg(), [new])
        except service.LedgerError as exc:
            st.error(f"Not saved: {exc}")
            return
        _history.clear()
        st.success("Saved.")
        st.rerun()


def _import_kite() -> None:
    ui.muted(
        "Export your holdings from Kite or Console as CSV. Nothing is saved until you confirm. "
        "Imported lots have no buy date (edit later for tax), and buy charges are estimated."
    )
    up = st.file_uploader("Holdings CSV", type=["csv"], label_visibility="collapsed")
    if up is None:
        return
    try:
        parsed = parse_holdings_csv(up.getvalue())
    except ImportFormatError as exc:
        st.error(f"Couldn't read this file: {exc}")
        return
    plan = service.plan_kite_import(lake(), parsed)
    ui.muted(f"Columns used: {parsed.column_map}")
    for p in parsed.problems + plan.unmatched:
        st.warning(p)
    if not plan.ready:
        st.info("Nothing to import.")
        return
    st.dataframe(
        pl.DataFrame(
            [{"Stock": t.symbol, "Qty": t.quantity, "Avg cost": t.price} for t in plan.ready]
        ),
        hide_index=True,
        column_config={"Avg cost": MONEY(format="₹%.2f")},
    )
    if st.button(f"Import {len(plan.ready)} holdings", type="primary"):
        try:
            with connect() as conn:
                service.add(conn, lake(), cfg(), plan.ready)
        except service.LedgerError as exc:
            st.error(f"Not imported: {exc}")
            return
        _history.clear()
        st.success(f"Imported {len(plan.ready)} holdings.")
        st.rerun()


def _transactions(loaded: service.Loaded) -> None:
    rows = loaded.transactions
    if not rows:
        ui.muted("No transactions yet.")
        return
    st.dataframe(
        pl.DataFrame(
            [
                {
                    "ID": r["txn_id"],
                    "Date": r["trade_date"],
                    "Stock": r["symbol"],
                    "Side": r["side"],
                    "Qty": r["quantity"],
                    "Price": r["price"],
                    "Charges": "estimated" if r["charges"] is None else f"₹{r['charges']:,.2f}",
                    "Source": r["source"],
                    "Note": r["note"],
                }
                for r in rows
            ],
            infer_schema_length=None,
            strict=False,
        ),
        hide_index=True,
        width="stretch",
        column_config={"Price": MONEY(format="₹%.2f")},
    )
    a, b, c = st.columns([2, 1, 1])
    target = a.selectbox(
        "Delete a transaction",
        [r["txn_id"] for r in rows],
        index=None,
        placeholder="Transaction ID",
    )
    confirm = b.checkbox("Yes, delete it")
    if c.button("Delete", disabled=target is None or not confirm):
        try:
            with connect() as conn:
                service.delete(conn, lake(), cfg(), int(target))
        except service.LedgerError as exc:
            st.error(f"Can't delete: {exc}. Delete or edit the later sale first.")
            return
        _history.clear()
        st.success("Deleted.")
        st.rerun()


def _realised(loaded: service.Loaded) -> None:
    if not loaded.ledger.realised:
        ui.muted("No sales yet.")
        return
    st.dataframe(
        pl.DataFrame(
            [
                {
                    "Company": r.company_id,
                    "Qty": r.quantity,
                    "Bought": r.buy_date,
                    "Sold": r.sell_date,
                    "Cost": r.cost,
                    "Proceeds": r.proceeds,
                    "Gain": r.gain,
                }
                for r in loaded.ledger.realised
            ]
        ),
        hide_index=True,
        width="stretch",
        column_config={c: MONEY(format="₹%.2f") for c in ("Cost", "Proceeds", "Gain")},
    )
