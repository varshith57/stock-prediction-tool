"""Monthly Audit: what the app showed vs what happened, over a chosen period (last 4 weeks by
default). The verdict comes first; details follow. Lock a review to freeze it, or export it."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import streamlit as st
from views.common import cfg, inr, lake

from stockapp.audit.report import build_audit, lock_snapshot, money_for_period, to_markdown
from stockapp.db import connect

PERIODS = {"Last 4 weeks": 28, "12 weeks": 84, "6 months": 182, "1 year": 365}


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:+.1%}"


def render() -> None:
    st.header("Monthly Audit")
    choice = st.segmented_control("Period", [*PERIODS, "Custom"], default="Last 4 weeks")
    end = date.today()
    if choice == "Custom":
        start, end = st.date_input("From / to", value=(end - timedelta(days=28), end))
    else:
        start = end - timedelta(days=PERIODS.get(choice or "Last 4 weeks", 28))

    with connect() as conn:
        money = money_for_period(conn, lake(), cfg(), start, end)
        audit = build_audit(conn, lake(), cfg(), start, end, money)

    st.subheader(audit.verdict)
    st.caption(f"{start:%d %b %Y} to {end:%d %b %Y} · {audit.plans} weekly plan(s)")
    for n in audit.notes:
        st.info(n)

    st.markdown("#### Signal scorecard (vs the 90% claim)")
    st.dataframe(
        pl.DataFrame(
            [
                {
                    "Signal": c.signal,
                    "Issued": c.issued,
                    "Matured": c.matured,
                    "Correct": c.correct,
                    "Pending": c.pending,
                    "Precision": f"{c.precision:.0%}" if c.precision is not None else "—",
                    "Lower bound": f"{c.wilson_lb:.0%}" if c.wilson_lb is not None else "—",
                    "Claim": f"{c.claim:.0%}",
                }
                for c in audit.scorecards
            ]
        ),
        hide_index=True,
    )
    st.markdown("#### Missed events")
    for s, m in audit.missed.items():
        what = "+10% weeks" if s == "A" else "-10% weeks"
        st.write(
            f"Signal {s}: {m['events']} {what} in the universe; {m['flagged']} flagged, "
            f"{m['missed']} missed."
        )
    st.caption("Recall is reported honestly: with a 90% bar most events are expected to be missed.")

    st.markdown("#### Your actions")
    if audit.actions:
        st.write(", ".join(f"{k}: {v}" for k, v in sorted(audit.action_counts.items())))
        st.dataframe(pl.DataFrame(audit.actions), hide_index=True)
    else:
        st.caption("No actions logged in this period.")

    st.markdown("#### Money")
    c1, c2, c3 = st.columns(3)
    c1.metric("Portfolio (time-weighted)", _pct(money.get("twr")))
    c2.metric("Nifty 50, same cash flows", _pct(money.get("benchmark")))
    c3.metric("Worst drawdown", _pct(money.get("max_drawdown")))
    st.caption(
        f"Realised gain {inr(money.get('realised'))} · charges paid "
        f"{inr(money.get('charges'), 2)}. "
        "Nifty 50 is the price index (total-return data isn't available yet), so the "
        "benchmark is slightly understated."
    )

    st.markdown("#### Calibration (live, matured weeks only)")
    if audit.calibration:
        st.dataframe(pl.DataFrame(audit.calibration), hide_index=True)
    else:
        st.caption(
            "No matured live scores in this period yet (a week's outcome is known after 5 "
            "trading days)."
        )

    st.markdown("#### Data and model health")
    st.write(" · ".join(f"{k.replace('_', ' ')}: {v}" for k, v in audit.health.items()))

    st.markdown("#### Proposed changes")
    for p in audit.proposals:
        st.write(f"- {p}")
    st.caption("Proposals are never applied automatically.")

    a, b = st.columns(2)
    if a.button("Lock this audit"):
        with connect() as conn:
            sid = lock_snapshot(conn, audit)
        st.success(f"Locked as snapshot {sid}.")
    b.download_button(
        "Export (Markdown)",
        to_markdown(audit),
        file_name=f"audit_{start}_{end}.md",
        mime="text/markdown",
    )
