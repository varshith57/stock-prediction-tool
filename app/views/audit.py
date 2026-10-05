"""Track record: did the app's advice work? A plain verdict and four numbers for the chosen period
(last 4 weeks by default); the full monthly audit (scorecards, misses, calibration, health,
proposals, lock and export) is folded away below."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import streamlit as st
from views import ui
from views.common import cfg, inr, lake

from stockapp.audit.report import build_audit, lock_snapshot, money_for_period, to_markdown
from stockapp.config import horizon
from stockapp.db import connect

PERIODS = {"4 weeks": 28, "12 weeks": 84, "6 months": 182, "1 year": 365}


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:+.1%}"


def render() -> None:
    head, picker = st.columns([1, 1], vertical_alignment="bottom")
    with picker:
        choice = st.segmented_control(
            "Period", [*PERIODS, "Custom"], default="4 weeks", label_visibility="collapsed"
        )
    end = date.today()
    if choice == "Custom":
        start, end = st.date_input("From / to", value=(end - timedelta(days=28), end))
    else:
        start = end - timedelta(days=PERIODS.get(choice or "4 weeks", 28))
    with head:
        ui.header("Track record", f"Did the advice work? {start:%d %b %Y} to {end:%d %b %Y}")

    with connect() as conn:
        money = money_for_period(conn, lake(), cfg(), start, end)
        audit = build_audit(conn, lake(), cfg(), start, end, money)

    cards = list(audit.scorecards)
    issued = sum(c.issued for c in cards)
    if issued:
        ui.hero(audit.verdict, f"From {audit.plans} weekly plan(s) in this period.")
    else:
        ui.hero(
            "No buy or drop advice in this period",
            "The predictions weren't proven 90% accurate yet, so the app stayed quiet rather "
            "than guess. Rule-based sells still applied.",
        )
    matured = sum(c.matured for c in cards)
    correct = sum(c.correct for c in cards)
    ui.kpis(
        [
            ("Advice given", str(issued), f"{matured} with a known result", "grey"),
            (
                "Advice that worked",
                f"{correct / matured:.0%}" if matured else "—",
                f"{correct} of {matured}" if matured else "results take a week",
                "grey",
            ),
            (
                "Your return",
                _pct(money.get("twr")),
                "after fees",
                ui.tone_for(money.get("twr")),
            ),
            ("Nifty 50, same money", _pct(money.get("benchmark")), "for comparison", "grey"),
        ]
    )

    with st.expander("Full report"):
        _details(audit, money, start, end)


def _details(audit, money, start, end) -> None:
    for n in audit.notes:
        st.info(n)
    ui.section("Signal scorecard")
    st.dataframe(
        pl.DataFrame(
            [
                {
                    "Signal": c.signal,
                    "Issued": c.issued,
                    "Matured": c.matured,
                    "Correct": c.correct,
                    "Pending": c.pending,
                    "Precision": c.precision,
                    "Lower bound": c.wilson_lb,
                    "Claim": c.claim,
                }
                for c in audit.scorecards
            ]
        ),
        hide_index=True,
        width="stretch",
        column_config={
            c: st.column_config.NumberColumn(format="percent")
            for c in ("Precision", "Lower bound", "Claim")
        },
    )

    ui.section("Missed events")
    with st.container(border=True):
        for s, m in audit.missed.items():
            sig = cfg().signals
            when = horizon(sig.window_trading_days).replace("this week", "in a week")
            what = (
                f"+{sig.gain_threshold:.0%} moves {when}"
                if s == "A"
                else f"-{sig.crash_threshold:.0%} moves {when}"
            )
            st.markdown(
                f'<div class="sa-row" style="padding:.2rem 0"><span><b>Signal {s}</b> · '
                f"{m['events']} {what} in the universe</span><span class='sa-muted'>"
                f"{m['flagged']} flagged · {m['missed']} missed</span></div>",
                unsafe_allow_html=True,
            )
        ui.muted(
            f"With a {cfg().signals.certainty_bar:.0%} bar most events are expected to be "
            "missed; recall is shown honestly."
        )

    ui.section("Your actions")
    if audit.actions:
        ui.muted(" · ".join(f"{k}: {v}" for k, v in sorted(audit.action_counts.items())))
        st.dataframe(pl.DataFrame(audit.actions), hide_index=True, width="stretch")
    else:
        ui.muted("No actions logged in this period.")

    ui.section("Money")
    ui.muted(
        f"Realised gain {inr(money.get('realised'))} · charges paid "
        f"{inr(money.get('charges'), 2)}. "
        "The Nifty 50 comparison uses the price index (no dividends), so it slightly understates "
        "the benchmark."
    )

    ui.section("Calibration (live, matured weeks)")
    if audit.calibration:
        st.dataframe(pl.DataFrame(audit.calibration), hide_index=True, width="stretch")
    else:
        ui.muted("No matured live scores yet: an outcome is known once its window has passed.")

    ui.section("Data and model health")
    hlt = audit.health
    gates = hlt.get("gates", {})
    ui.kpis(
        [
            ("Data quality (min)", f"{hlt.get('quality_min') or 0:.0f}/100", None, "grey"),
            (
                "Unresolved failures",
                str(hlt.get("failures_unresolved", 0)),
                f"{hlt.get('failed_attempts_retried', 0)} retried OK",
                "grey",
            ),
            (
                "Open quarantine",
                f"{hlt.get('open_quarantine_block', 0)} block",
                f"{hlt.get('open_quarantine_warn', 0)} warning(s)",
                "grey",
            ),
            (
                "Signals",
                " · ".join(f"{s} {g}" for s, g in sorted(gates.items())) or "—",
                None,
                "grey",
            ),
        ]
    )

    ui.section("Proposed changes")
    with st.container(border=True):
        for p in audit.proposals:
            st.markdown(f"- {p}")
        ui.muted(
            "Proposals are never applied automatically: change Settings yourself if you agree."
        )

    a, b, _ = st.columns([1, 1, 3])
    if a.button("Lock this audit", icon=":material/lock:"):
        with connect() as conn:
            sid = lock_snapshot(conn, audit)
        st.success(f"Locked as snapshot {sid}.")
    b.download_button(
        "Export",
        to_markdown(audit),
        file_name=f"audit_{start}_{end}.md",
        mime="text/markdown",
        icon=":material/download:",
    )
