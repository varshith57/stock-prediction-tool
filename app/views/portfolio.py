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
from stockapp.portfolio.store import NewTransaction, holding_styles, set_holding_style
from stockapp.portfolio.valuation import load_latest_prices, resolve_symbol

MONEY = st.column_config.NumberColumn
STYLE_LABELS = {"investment": "Investment", "trade": "Trade"}


@st.cache_data(ttl=600)
def _symbols() -> list[str]:
    return sorted(load_latest_prices(lake())["symbol"].to_list())


def render() -> None:
    today = date.today()
    with connect() as conn:
        loaded = service.load(conn, lake(), cfg(), today)
    view, t = loaded.view, loaded.view.totals
    subtitle = (
        f"Valued at the market close of {view.data_as_of:%a %d %b %Y}"
        if view.data_as_of
        else "No prices yet"
    )
    ui.header("Portfolio", subtitle)

    unreal_pct = (t["unrealised"] / t["invested"]) if t["invested"] else None
    ui.kpis(
        [
            ("Worth now", inr(t["value"]), None, "grey"),
            ("You put in", inr(t["invested"]), "including fees", "grey"),
            (
                "Gain so far",
                inr(t["unrealised"]),
                f"{unreal_pct:+.2%}" if unreal_pct is not None else None,
                ui.tone_for(t["unrealised"]),
            ),
            ("Today", inr(t["day_change"]), None, ui.tone_for(t["day_change"])),
        ]
    )
    if view.warnings:
        st.warning("\n".join(f"- {w}" for w in view.warnings))
    if not view.holdings.is_empty() and view.holdings["stale"].any():
        st.error("Some prices are stale: no new advice on those until they trade again.")

    ui.section("Record a trade")
    with st.container(border=True):
        ui.muted("Bought or sold something in Kite? Add it here so This week stays accurate.")
        _add_trade()

    ui.section("More")
    with st.expander("Import everything from Kite"):
        _import_kite()
    with st.expander(f"Your holdings in detail ({view.holdings.height})"):
        _holdings(view, t)
        _styles(view)
    with st.expander("How you're doing vs the Nifty 50"):
        _chart()
    with st.expander(f"All trades ({len(loaded.transactions)})"):
        _transactions(loaded)
    with st.expander(f"Sold so far ({len(loaded.ledger.realised)})"):
        ui.muted(f"Profit from sales: {inr(t['realised'])}")
        _realised(loaded)


def _holdings(view, t) -> None:
    h = view.holdings
    if h.is_empty():
        ui.muted("No holdings yet.")
        return
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
    ui.muted(f"Estimated tax if you sold everything today: {inr(t['tax_if_sold'])}.")


def _styles(view) -> None:
    h = view.holdings
    if h.is_empty():
        return
    with connect() as conn:
        styles = holding_styles(conn)
    ui.muted(
        "Kind: "
        + " · ".join(
            f"{r['symbol']} {STYLE_LABELS[styles.get(r['company_id'], 'investment')].lower()}"
            for r in h.iter_rows(named=True)
        )
    )
    ids = dict(zip(h["symbol"].to_list(), h["company_id"].to_list(), strict=True))
    a, b, c = st.columns([2, 2, 1], vertical_alignment="bottom")
    pick = a.selectbox("Change a holding's kind", list(ids), index=None, placeholder="Stock")
    kind = b.segmented_control(
        "To", list(STYLE_LABELS), format_func=STYLE_LABELS.get, key="style_to"
    )
    if c.button("Change", disabled=pick is None or kind is None):
        with connect() as conn:
            set_holding_style(conn, ids[pick], kind)
        _refresh_plan()
        st.rerun()


def _refresh_plan() -> None:
    """Holdings changed: rebuild this week's buckets so they match the portfolio."""
    if not lake().has_table("gold", "latest_scores"):
        return
    from stockapp.pipeline import build_weekly_plan

    with st.spinner("Updating this week's advice..."), connect() as conn:
        build_weekly_plan(conn, lake(), cfg(), date.today())


@st.cache_data(ttl=300)
def _history() -> pl.DataFrame:
    with connect() as conn:
        return service.history(conn, lake(), cfg())


def _chart() -> None:
    hist = _history()
    if hist.height < 2:
        ui.muted("Shows up once you have a few days of history.")
        return
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
    symbol = a.selectbox("Stock", _symbols(), index=None, placeholder="Type a name, e.g. TCS")
    side = b.segmented_control("Bought or sold", ["BUY", "SELL"], default="BUY")
    trade_date = c.date_input("On", value=date.today(), max_value=date.today())
    d, e, k = st.columns(3)
    qty = d.number_input("Shares", min_value=1, step=1, value=1)
    price = e.number_input("Price per share (₹)", min_value=0.01, step=0.05, format="%.2f")
    style = k.segmented_control(
        "Kind",
        list(STYLE_LABELS),
        format_func=STYLE_LABELS.get,
        default="investment",
        help="Investment: yours for the long run, never sold by a rule; you get a review note "
        "if it falls past your review line. Trade: bought on one of the app's buy ideas; it "
        "follows the short-term stop-loss and target.",
    )
    est = order_charges(side or "BUY", int(qty), float(price), cfg().costs)
    with st.expander(f"Fees and note · fees estimated at {inr(est.total, 2)}"):
        f, g = st.columns(2)
        charges_text = f.text_input(
            "Actual fees (₹)", value="", placeholder="Leave blank to use the estimate"
        )
        note = g.text_input("Note", placeholder="Optional")
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
                if side == "BUY":
                    set_holding_style(conn, hit[0], style or "investment")
        except service.LedgerError as exc:
            st.error(f"Not saved: {exc}")
            return
        _history.clear()
        _refresh_plan()
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
        _refresh_plan()
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
        _refresh_plan()
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
