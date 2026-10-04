"""The small data-health line in the header: quiet when all is well, explicit when it isn't."""

from __future__ import annotations

import polars as pl
import streamlit as st
from views.common import cfg, lake


@st.cache_data(ttl=600)
def _status() -> dict:
    lk = lake()
    out: dict = {"as_of": None, "score": None}
    if lk.has_table("gold", "quality_daily"):
        q = lk.scan("gold", "quality_daily").collect()
        q = q.filter(pl.col("built") == pl.col("built").max()).sort("trade_date")
        if q.height:
            out["as_of"], out["score"] = q["trade_date"][-1], q["score"][-1]
    return out


def header_line() -> None:
    s = _status()
    if s["as_of"] is None:
        st.warning("No data quality results yet: run `uv run stockapp quality build`.")
        return
    minimum = cfg().data.min_quality_score
    if s["score"] < minimum:
        st.error(
            f"Data quality {s['score']:.0f}/100 on {s['as_of']:%a %d %b} is below {minimum:g}: "
            "NO SIGNAL until it recovers."
        )
    else:
        st.caption(
            f"Data OK · quality {s['score']:.0f}/100 · NSE close of {s['as_of']:%a %d %b %Y}"
        )
