"""Settings: read-only view of the active configuration for now (editable, versioned settings
arrive with the decision engine). Shows the cost preset used for every charge on the Portfolio
page, so the numbers can be checked."""

from __future__ import annotations

import polars as pl
import streamlit as st
from views.common import cfg, inr, lake


@st.cache_data(ttl=600)
def _gate() -> pl.DataFrame:
    lk = lake()
    if not lk.has_table("gold", "signal_gate"):
        return pl.DataFrame()
    g = lk.scan("gold", "signal_gate").collect()
    return g.filter(pl.col("built") == pl.col("built").max())


def render() -> None:
    c = cfg()
    st.header("Settings")
    st.caption("Read-only for now. Personal values come from config/local.yaml.")

    st.subheader("Budget and risk")
    st.write(
        f"Weekly budget {inr(c.budget.weekly_inr)} (accumulates) · minimum position "
        f"{inr(c.budget.min_position_inr)} · per-stock cap {c.risk.max_stock_weight:.0%} · "
        f"per-sector cap {c.risk.max_sector_weight:.0%} · drawdown review "
        f"{c.risk.drawdown_review:.0%}, pause {c.risk.drawdown_pause:.0%}"
    )
    st.subheader("Signals")
    st.write(
        f"Gain threshold {c.signals.gain_threshold:.0%} · crash threshold "
        f"{c.signals.crash_threshold:.0%} · window {c.signals.window_trading_days} trading days · "
        f"certainty bar {c.signals.certainty_bar:.0%} · at most {c.signals.max_opportunities} "
        "opportunities a week"
    )
    st.subheader("Signal status (validated walk-forward, out of sample)")
    gate = _gate()
    if gate.is_empty():
        st.info("No validation run yet: `uv run stockapp models backtest`.")
    for r in gate.iter_rows(named=True):
        name = {"A": "A. Opportunity (+10%)", "C": "C. Crash exit (-10%)"}.get(
            r["signal"], r["signal"]
        )
        if r["status"] == "LIVE":
            st.success(f"**{name}: LIVE** at {r['cutoff']:.0%}+. {r['reason']}")
        else:
            st.warning(f"**{name}: OFF.** The model cannot yet meet the 90% bar: {r['reason']}")
    if not gate.is_empty():
        st.caption(
            f"Evaluated {gate['evaluated_at'][0]:%d %b %Y} on every walk-forward prediction since "
            "2018. The bar is never lowered automatically."
        )
    st.subheader("Zerodha charges (delivery)")
    k = c.costs
    st.write(
        f"Brokerage {inr(k.brokerage)} · STT {k.stt_rate:.3%} each side · stamp duty "
        f"{k.stamp_duty_buy_rate:.3%} on buys · NSE charge {k.exchange_txn_rate:.5%} · SEBI fee "
        f"{k.sebi_fee_rate:.4%} · GST {k.gst_rate:.0%} on brokerage + exchange + SEBI · DP charge "
        f"{inr(k.dp_charge_per_scrip_sell_inr, 2)} per stock sold"
    )
    st.caption("Rates from the PRD, checked 2 Oct 2026. Confirm against a recent contract note.")
    st.subheader("Tax (indicative)")
    t = c.tax
    st.write(
        f"Short-term {t.stcg_rate:.1%} · long-term {t.ltcg_rate:.1%} above "
        f"{inr(t.ltcg_exemption_inr)} a year. Not tax advice."
    )
