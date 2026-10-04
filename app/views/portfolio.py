"""Portfolio: holdings valued at the latest NSE close, gain/loss after charges, sector weights and
cap warnings, plus adding trades and importing a Kite holdings CSV.

Holdings are private: they're read from and written to the app database only, and never logged.
"""

from __future__ import annotations

from datetime import date

import polars as pl
import streamlit as st
from views.common import cfg, inr, lake, pct

from stockapp.db import connect
from stockapp.portfolio import service
from stockapp.portfolio.costs import order_charges
from stockapp.portfolio.kite import ImportFormatError, parse_holdings_csv
from stockapp.portfolio.store import NewTransaction
from stockapp.portfolio.valuation import load_latest_prices, resolve_symbol


@st.cache_data(ttl=600)
def _symbols() -> list[str]:
    return sorted(load_latest_prices(lake())["symbol"].to_list())


def render() -> None:
    st.header("Portfolio")
    today = date.today()
    with connect() as conn:
        loaded = service.load(conn, lake(), cfg(), today)
    view, t = loaded.view, loaded.view.totals

    c1, c2, c3 = st.columns(3)
    c1.metric("Invested", inr(t["invested"]))
    c2.metric("Current value", inr(t["value"]))
    unreal_pct = (t["unrealised"] / t["invested"] * 100) if t["invested"] else None
    c3.metric("Unrealised", inr(t["unrealised"]), pct(unreal_pct))
    c4, c5, _ = st.columns(3)
    c4.metric("Realised", inr(t["realised"]))
    c5.metric("Today", inr(t["day_change"]))
    if view.data_as_of:
        st.caption(
            f"Valued at the NSE official close of {view.data_as_of:%a %d %b %Y} (end of day; no "
            "intraday quotes). Gains include buy charges; estimated tax if all sold today: "
            f"{inr(t['tax_if_sold'])} (indicative)."
        )
    if view.warnings:
        st.warning("**Check these:**\n" + "\n".join(f"- {w}" for w in view.warnings))

    h = view.holdings
    if h.is_empty():
        st.info("No holdings yet. Add a trade or import your Kite holdings below.")
    else:
        if h["stale"].any():
            st.error("Some prices are stale (shown below): no new actions on those until fresh.")
        st.dataframe(
            h.select(
                pl.col("symbol").alias("Stock"),
                pl.col("sector").alias("Sector"),
                pl.col("quantity").alias("Qty"),
                pl.col("avg_cost").round(2).alias("Avg cost ₹"),
                pl.col("last_price").alias("Last ₹"),
                pl.col("value").round(0).alias("Value ₹"),
                pl.col("unrealised").round(0).alias("Gain/loss ₹"),
                pl.col("unrealised_pct").round(2).alias("Gain/loss %"),
                pl.col("weight_pct").round(1).alias("Weight %"),
                pl.col("day_change").round(0).alias("Today ₹"),
                pl.when(pl.col("stale")).then(pl.lit("Stale price")).otherwise(pl.lit("Hold"))
                .alias("Status"),
                pl.col("price_date").alias("Price date"),
            ),
            hide_index=True,
            use_container_width=True,
        )  # fmt: skip
        st.caption("Status becomes Sell now / Sell at ₹X once exit signals exist (M8).")

    _add_trade(loaded)
    _import_kite()
    _transactions(loaded)
    if loaded.ledger.realised:
        with st.expander(f"Realised gains ({len(loaded.ledger.realised)})"):
            st.dataframe(
                pl.DataFrame(
                    [
                        {"Company": r.company_id, "Qty": r.quantity, "Bought": r.buy_date,
                         "Sold": r.sell_date, "Cost ₹": round(r.cost, 2),
                         "Proceeds ₹": round(r.proceeds, 2), "Gain ₹": round(r.gain, 2)}
                        for r in loaded.ledger.realised
                    ]
                ),
                hide_index=True,
            )  # fmt: skip


def _add_trade(loaded: service.Loaded) -> None:
    with st.expander("Add a buy or sale"):
        symbols = _symbols()
        a, b, c = st.columns(3)
        symbol = a.selectbox(
            "Stock (NSE symbol)", symbols, index=None, placeholder="Type to search"
        )
        side = b.radio("Side", ["BUY", "SELL"], horizontal=True)
        trade_date = c.date_input("Trade date", value=date.today(), max_value=date.today())
        d, e, f = st.columns(3)
        qty = d.number_input("Quantity", min_value=1, step=1, value=1)
        price = e.number_input("Price per share ₹", min_value=0.01, step=0.05, format="%.2f")
        charges_text = f.text_input("Charges ₹ (blank = estimate)", value="")
        note = st.text_input("Note (optional)")
        est = order_charges(side, int(qty), float(price), cfg().costs)
        st.caption(
            f"Estimated charges {inr(est.total, 2)}: STT {inr(est.stt, 2)}, exchange "
            f"{inr(est.exchange, 2)}, SEBI {inr(est.sebi, 2)}, GST {inr(est.gst, 2)}, stamp "
            f"{inr(est.stamp, 2)}, DP {inr(est.dp, 2)}"
        )
        if st.button("Save trade", type="primary", disabled=symbol is None):
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
                company_id=hit[0], symbol=symbol, side=side, quantity=int(qty), price=float(price),
                trade_date=trade_date, isin=hit[1], charges=charges, note=note or None,
            )  # fmt: skip
            try:
                with connect() as conn:
                    service.add(conn, lake(), cfg(), [new])
            except service.LedgerError as exc:
                st.error(f"Not saved: {exc}")
                return
            st.success("Saved.")
            st.rerun()


def _import_kite() -> None:
    with st.expander("Import holdings from Kite (CSV)"):
        st.caption(
            "Export your holdings from Kite or Console as CSV. Nothing is saved until you confirm. "
            "Imported lots have no buy date (edit it later for tax), and buy charges are estimated."
        )
        up = st.file_uploader("Holdings CSV", type=["csv"])
        if up is None:
            return
        try:
            parsed = parse_holdings_csv(up.getvalue())
        except ImportFormatError as exc:
            st.error(f"Couldn't read this file: {exc}")
            return
        plan = service.plan_kite_import(lake(), parsed)
        st.caption(f"Columns used: {parsed.column_map}")
        for p in parsed.problems + plan.unmatched:
            st.warning(p)
        if not plan.ready:
            st.info("Nothing to import.")
            return
        st.dataframe(
            pl.DataFrame(
                [{"Stock": t.symbol, "Qty": t.quantity, "Avg cost ₹": t.price} for t in plan.ready]
            ),
            hide_index=True,
        )
        if st.button(f"Import {len(plan.ready)} holdings", type="primary"):
            try:
                with connect() as conn:
                    service.add(conn, lake(), cfg(), plan.ready)
            except service.LedgerError as exc:
                st.error(f"Not imported: {exc}")
                return
            st.success(f"Imported {len(plan.ready)} holdings.")
            st.rerun()


def _transactions(loaded: service.Loaded) -> None:
    rows = loaded.transactions
    with st.expander(f"Transactions ({len(rows)})"):
        if not rows:
            st.caption("None yet.")
            return
        st.dataframe(
            pl.DataFrame(
                [
                    {"ID": r["txn_id"], "Date": r["trade_date"], "Stock": r["symbol"],
                     "Side": r["side"], "Qty": r["quantity"], "Price ₹": r["price"],
                     "Charges ₹": r["charges"] if r["charges"] is not None else "estimated",
                     "Source": r["source"], "Note": r["note"]}
                    for r in rows
                ],
                infer_schema_length=None,
                strict=False,
            ),
            hide_index=True,
        )  # fmt: skip
        ids = [r["txn_id"] for r in rows]
        a, b = st.columns([2, 1])
        target = a.selectbox("Delete transaction ID", ids, index=None)
        confirm = b.checkbox("Yes, delete it")
        if st.button("Delete", disabled=target is None or not confirm):
            try:
                with connect() as conn:
                    service.delete(conn, lake(), cfg(), int(target))
            except service.LedgerError as exc:
                st.error(f"Can't delete: {exc}. Delete or edit the later sale first.")
                return
            st.success("Deleted.")
            st.rerun()
