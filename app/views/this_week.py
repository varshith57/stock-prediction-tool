"""This week (home): one question answered: what should I do this week?

Built from the latest saved weekly plan. Sells first (red), then opportunities (green), then
holdings to keep. Each action has a one-tap log that feeds the Monthly Audit. Rules are labelled
as rules; probabilities appear only for validated LIVE signals.
"""

from __future__ import annotations

from html import escape

import streamlit as st
from views import ui
from views.common import cfg

from stockapp.cli import IST
from stockapp.db import connect
from stockapp.plan.store import actions_for, latest_plan, log_action

ACTIONS = (("done", "Done"), ("partly", "Partly"), ("skipped", "Skipped"))
REASONS = ["", "price moved", "no cash", "disagreed", "other"]


def _log_control(plan_id: int, key: str, logged: dict) -> None:
    current = logged.get(key)
    labels = dict(ACTIONS)
    left, right = st.columns([3, 2])
    with left:
        choice = st.segmented_control(
            "Log what you did",
            options=[v for v, _ in ACTIONS],
            format_func=labels.get,
            default=current["action"] if current else None,
            key=f"log:{key}",
            label_visibility="collapsed",
        )
    if choice and (current is None or choice != current["action"]):
        with connect() as conn:
            log_action(conn, plan_id, key, choice)
        st.rerun()
    if current and current["action"] != "done":
        with right:
            why = st.selectbox(
                "Why?",
                REASONS,
                key=f"why:{key}",
                label_visibility="collapsed",
                index=REASONS.index(current["reason"]) if current["reason"] in REASONS else 0,
                format_func=lambda r: r or "Why? (optional)",
            )
        if why and why != current["reason"]:
            with connect() as conn:
                log_action(conn, plan_id, key, current["action"], why)
            st.rerun()
    if current:
        when = current["acted_at"].astimezone(IST)
        ui.muted(f"Logged {current['action']} · {when:%a %d %b, %H:%M}")


def _item(plan_id: int, item: dict, tone: str, tag: str, logged: dict, key: str) -> None:
    with st.container(border=True):
        st.markdown(
            f'<div class="sa-row"><div><span class="sa-sym">{escape(item["symbol"])}</span>'
            f"&nbsp;&nbsp;{ui.pill(tag, tone)}</div></div>"
            f'<div style="margin:.35rem 0 .2rem">{escape(item["headline"])}</div>',
            unsafe_allow_html=True,
        )
        if item.get("reason"):
            ui.muted(f"Why: {item['reason']}")
        _log_control(plan_id, key, logged)


def render() -> None:
    with connect() as conn:
        row = latest_plan(conn)
        logged = actions_for(conn, row["plan_id"]) if row else {}
    if row is None:
        ui.header("This week", "No plan yet")
        ui.hero(
            "No plan yet",
            "The plan is built on Friday evening, or run `uv run stockapp job weekly`.",
        )
        return
    plan = row["payload"]
    built = row["built_at"].astimezone(IST)
    ui.header(
        f"Week of {row['week_of']:%a %d %b}",
        f"Built {built:%a %d %b, %H:%M} from the NSE close of {row['signal_date']:%a %d %b}",
    )

    if plan["status"] == "NO_SIGNAL":
        ui.hero(
            "No signal this week",
            f"{plan['status_reason']}. No actions until the data recovers.",
            ui.pill("Data issue", "red"),
        )
        return

    n = len(plan["exits"]) + len(plan["opportunities"])
    regime = plan["regime"]
    gates = plan.get("gates", {})
    pills = [
        ui.pill(f"Data {plan['quality_score']:.0f}/100", "green"),
        ui.pill(
            "Stress regime" if regime["stress"] else "Market normal",
            "red" if regime["stress"] else "grey",
        ),
    ]
    for s, g in sorted(gates.items()):
        pills.append(
            ui.pill(f"Signal {s} {g['status']}", "green" if g["status"] == "LIVE" else "grey")
        )
    if n:
        ui.hero(
            f"{n} action{'s' if n != 1 else ''} this week",
            "Act in Kite on Monday, then log what you did here.",
            " ".join(pills),
        )
    else:
        ui.hero("No action this week", "Hold all positions.", " ".join(pills))

    dd = plan["drawdown"]
    if dd["from_peak"] is not None and (dd["review"] or dd["pause_buys"]):
        st.warning(
            f"Portfolio {dd['from_peak']:.1%} from its peak (time-weighted): "
            + ("new buys are paused." if dd["pause_buys"] else "time to review.")
        )
    for note in plan["notes"]:
        if "drawdown" not in note:
            st.warning(note[0].upper() + note[1:])

    if plan["exits"]:
        ui.section("Sell")
        for item in plan["exits"]:
            tag = "Exit rule" if item.get("rule") else "Crash risk"
            _item(row["plan_id"], item, "red", tag, logged, f"SELL:{item['company_id']}")
    if plan["opportunities"]:
        ui.section("Opportunities · best expected gain first")
        for item in plan["opportunities"]:
            _item(row["plan_id"], item, "green", "Buy", logged, f"BUY:{item['company_id']}")

    if plan["holds"]:
        ui.section(f"Keep holding ({len(plan['holds'])})")
        with st.container(border=True):
            for item in plan["holds"]:
                st.markdown(
                    f'<div class="sa-row" style="padding:.25rem 0"><span class="sa-sym">'
                    f'{escape(item["symbol"])}</span><span class="sa-muted">'
                    f"{escape(item['reason'] or 'Hold')}</span></div>",
                    unsafe_allow_html=True,
                )

    closest = plan.get("closest")
    if closest and not plan["opportunities"] and not cfg().alerts.hide_closest_candidate:
        ui.section("Closest candidate")
        with st.container(border=True):
            certainty = ui.pill(f"{closest['probability']:.0%} certainty", "grey")
            note = f"Needs {closest['bar']:.0%}"
            if closest["signal_status"] != "LIVE":
                note += " · signal A is OFF (not yet validated)"
            st.markdown(
                f'<div class="sa-row"><div><span class="sa-sym">{escape(closest["symbol"])}'
                f'</span>&nbsp;&nbsp;{certainty}</div><div class="sa-muted">{escape(note)}</div>'
                "</div>",
                unsafe_allow_html=True,
            )
            if closest["reason"]:
                ui.muted(f"Drivers: {closest['reason']}")

    ui.section("Context")
    ui.muted(f"Market regime: {regime['reason']}.")
    ui.muted("Events this week: not available yet (results dates and announcements come later).")
