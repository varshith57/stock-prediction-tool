"""This Week (home): one question answered: what should I do this week?

Built from the latest saved weekly plan (``stockapp plan build``, Friday evening). Sells first,
then opportunities (green), then holds collapsed. Every action has a one-tap log that feeds the
Monthly Audit. Rules are labelled as rules; probabilities appear only for validated LIVE signals.
"""

from __future__ import annotations

import streamlit as st
from views.common import cfg

from stockapp.cli import IST
from stockapp.db import connect
from stockapp.plan.store import actions_for, latest_plan, log_action

ACTIONS = (("done", "Done"), ("partly", "Partly"), ("skipped", "Skipped"))


def _log_buttons(plan_id: int, key: str, logged: dict) -> None:
    current = logged.get(key)
    labels = {value: label for value, label in ACTIONS}
    choice = st.segmented_control(
        "Log what you did",
        options=[value for value, _ in ACTIONS],
        format_func=labels.get,
        default=current["action"] if current else None,
        key=f"log:{key}",
        label_visibility="collapsed",
    )
    if choice and (current is None or choice != current["action"]):
        with connect() as conn:
            log_action(conn, plan_id, key, choice)
        st.rerun()
    if current:
        st.caption(
            f"Logged {current['action']} on {current['acted_at'].astimezone(IST):%a %d %b %H:%M}"
        )


def render() -> None:
    with connect() as conn:
        row = latest_plan(conn)
        logged = actions_for(conn, row["plan_id"]) if row else {}
    if row is None:
        st.header("This Week")
        st.info("No plan yet. It's built on Friday evening (`uv run stockapp plan build`).")
        return
    plan = row["payload"]
    week_of = row["week_of"]
    st.header(f"Week of {week_of:%a %d %b}")
    st.caption(
        f"Plan built {row['built_at'].astimezone(IST):%a %d %b, %H:%M} IST from the NSE close of "
        f"{row['signal_date']:%a %d %b}"
    )

    if plan["status"] == "NO_SIGNAL":
        st.error(
            f"NO SIGNAL this week: {plan['status_reason']}. No actions are suggested until "
            "the data recovers."
        )
        return

    n = len(plan["exits"]) + len(plan["opportunities"])
    if n:
        st.subheader(f"{n} action{'s' if n != 1 else ''} this week")
    else:
        st.subheader("No action this week. Hold all positions.")

    if plan["exits"]:
        st.markdown("#### :red[Sell]")
        for item in plan["exits"]:
            with st.container(border=True):
                st.markdown(f"**:red[{item['symbol']}]** · {item['headline']}")
                if item["reason"]:
                    st.caption(f"Why: {item['reason']}")
                _log_buttons(row["plan_id"], f"SELL:{item['company_id']}", logged)

    if plan["opportunities"]:
        st.markdown("#### :green[Opportunities] (best expected gain first)")
        for i, item in enumerate(plan["opportunities"], 1):
            with st.container(border=True):
                st.markdown(f"{i}. **:green[{item['symbol']}]** · {item['headline']}")
                if item["stop_price"]:
                    st.caption(f"Stop {item['stop_price']:,.2f} · Why: {item['reason']}")
                _log_buttons(row["plan_id"], f"BUY:{item['company_id']}", logged)

    holds = plan["holds"]
    if holds:
        with st.expander(f"Hold ({len(holds)})"):
            for item in holds:
                st.markdown(
                    f"**{item['symbol']}**" + (f" · {item['reason']}" if item["reason"] else "")
                )

    for note in plan["notes"]:
        if "drawdown" not in note:  # shown once, below, with its context
            st.warning(note[0].upper() + note[1:])
    regime = plan["regime"]
    st.caption(f"Market regime: {regime['reason']}")
    dd = plan["drawdown"]
    if dd["from_peak"] is not None and (dd["review"] or dd["pause_buys"]):
        st.warning(
            f"Portfolio {dd['from_peak']:.1%} from its peak (time-weighted): "
            + ("new buys paused." if dd["pause_buys"] else "time to review.")
        )
    st.caption(
        "Events this week: not available yet (results dates and announcements arrive in P1)."
    )

    closest = plan.get("closest")
    if closest and not plan["opportunities"] and not cfg().alerts.hide_closest_candidate:
        status = closest["signal_status"]
        line = (
            f"Closest: **{closest['symbol']}** at {closest['probability']:.0%}, below the "
            f"{closest['bar']:.0%} bar"
        )
        if status != "LIVE":
            line += " (and signal A is OFF: it hasn't passed validation yet)"
        st.markdown(line)
        if closest["reason"]:
            st.caption(f"Drivers: {closest['reason']}")

    gates = plan["gates"]
    if gates:
        st.caption(
            " · ".join(f"Signal {s}: {g['status']}" for s, g in sorted(gates.items()))
            + " · details in Settings"
        )
