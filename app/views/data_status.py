"""Data status: a quiet line in the sidebar when all is well, a red banner when it isn't."""

from __future__ import annotations

import polars as pl
import streamlit as st
from views import ui
from views.common import cfg, lake


@st.cache_data(ttl=300)
def _status() -> dict:
    lk = lake()
    out: dict = {"as_of": None, "score": None}
    if lk.has_table("gold", "quality_daily"):
        q = lk.scan("gold", "quality_daily").collect()
        q = q.filter(pl.col("built") == pl.col("built").max()).sort("trade_date")
        if q.height:
            out["as_of"], out["score"] = q["trade_date"][-1], q["score"][-1]
    return out


def sidebar_status() -> None:
    s = _status()
    if s["as_of"] is None:
        st.markdown(ui.pill("No data yet", "amber"), unsafe_allow_html=True)
        return
    ok = s["score"] >= cfg().data.min_quality_score
    st.markdown(
        ui.pill(f"Data {'OK' if ok else 'issue'} · {s['score']:.0f}/100", "green" if ok else "red")
        + f'<div class="sa-foot" style="margin-top:.4rem">NSE close of {s["as_of"]:%a %d %b %Y}'
        "</div>",
        unsafe_allow_html=True,
    )


def quality_alert() -> None:
    s = _status()
    if s["as_of"] is None:
        st.warning("No data yet: run `uv run stockapp quality build`.")
    elif s["score"] < cfg().data.min_quality_score:
        st.error(
            f"Data quality {s['score']:.0f}/100 on {s['as_of']:%a %d %b} is below "
            f"{cfg().data.min_quality_score:g}: NO SIGNAL until it recovers."
        )
