"""This week (home): three buckets, Buy | Hold | Sell, that together hold the whole portfolio.

"Act now" items are the saved weekly plan: validated signals at the bar (90%+, gate LIVE) and
rule-based exits. The confidence slider only reveals what's next in line below that ("watch, not
a signal"), from the queue saved with the same plan; it never turns a watch item into an action.
Sells come first by urgency, holds riskiest first, buys by chance.
"""

from __future__ import annotations

from html import escape

import streamlit as st
from views import ui
from views.common import cfg, inr, lake

from stockapp.cli import IST
from stockapp.config import horizon
from stockapp.db import connect
from stockapp.models.run import precision_at_bar
from stockapp.plan.store import actions_for, latest_plan, log_action

ACT_BAR = 0.90
LOG = {"done": "Done", "skipped": "Skipped"}
RULE_NAMES = {
    "stop_loss": "Stop-loss hit",
    "trailing_stop": "Trailing stop hit",
    "time_stop": "Time's up",
    "target_reached": "Target reached",
}


@st.cache_data(ttl=3600, show_spinner=False)
def _track(signal: str, bar: float) -> dict | None:
    return precision_at_bar(lake(), cfg(), signal, bar)


def _card(symbol: str, tag: str, tone: str, lines: list[str]) -> None:
    body = "".join(f'<div class="sa-line">{escape(x)}</div>' for x in lines if x)
    st.markdown(
        f'<div class="sa-row"><span class="sa-sym">{escape(symbol)}</span>'
        f"{ui.pill(tag, tone)}</div>{body}",
        unsafe_allow_html=True,
    )


def _log(plan_id: int, key: str, logged: dict) -> None:
    current = logged.get(key)
    choice = st.segmented_control(
        "Log",
        list(LOG),
        format_func=LOG.get,
        default=current["action"] if current and current["action"] in LOG else None,
        key=f"log:{key}",
        label_visibility="collapsed",
    )
    if choice and (current is None or choice != current["action"]):
        with connect() as conn:
            log_action(conn, plan_id, key, choice)
        st.rerun()


def _mine(item: dict) -> str:
    parts = []
    if item.get("value") is not None:
        parts.append(inr(item["value"]))
    if item.get("return_pct") is not None:
        parts.append(f"{item['return_pct']:+.1%} since you bought")
    return " · ".join(parts)


def _watch(key: str, symbol: str, tag: str, lines: list[str]) -> None:
    with st.container(key=f"watch_{key}"):
        _card(symbol, tag, "grey", lines)


def _column_head(title: str, count: int, tone: str, hint: str) -> None:
    st.markdown(
        f'<div class="sa-colhead"><span>{escape(title)}</span>{ui.pill(str(count), tone)}</div>'
        f'<div class="sa-colhint">{escape(hint)}</div>',
        unsafe_allow_html=True,
    )


def _buy(plan: dict, plan_id: int, logged: dict, bar: float, gain: float, when: str) -> None:
    acts = plan["opportunities"]
    queue = [w for w in plan.get("watch_buys", []) if (w["probability"] or 0) >= bar]
    _column_head("Buy", len(acts), "green", f"New stocks likely to rise {gain:.0%} {when}.")
    for o in acts:
        with st.container(border=True):
            _card(
                o["symbol"],
                f"{o['probability']:.0%} sure",
                "green",
                [
                    f"Buy {o['quantity']} shares near {inr(o['guide_price'], 2)}",
                    f"Sell when it reaches {inr(o['sellout_price'], 2)} (+{gain:.0%})",
                ],
            )
            _log(plan_id, f"BUY:{o['company_id']}", logged)
    if not acts:
        with st.container(border=True):
            st.markdown('<div class="sa-sym">Nothing to buy</div>', unsafe_allow_html=True)
            ui.muted(
                f"No stock is {ACT_BAR:.0%} sure to rise {gain:.0%} {when}. Most weeks are "
                "like this: waiting is a decision too."
            )
    for n in plan["notes"]:
        if "paused" in n or "stress" in n:
            st.warning(n[0].upper() + n[1:])
    if queue:
        st.markdown(
            '<div class="sa-watchhead">Up next · watch, don\'t buy</div>',
            unsafe_allow_html=True,
        )
        for w in queue:
            _watch(
                f"b_{w['company_id']}",
                w["symbol"],
                f"{w['probability']:.0%} chance",
                [f"Near {inr(w['guide_price'], 2)}"],
            )


def _sell(plan: dict, plan_id: int, logged: dict, watch: list[dict], drop: str) -> None:
    acts = plan["exits"]
    _column_head("Sell", len(acts), "red", "Stocks to sell, most urgent first.")
    for e in acts:
        with st.container(border=True):
            if e.get("rule"):
                tag, why = RULE_NAMES.get(e["rule"], "Rule"), e["reason"]
            else:
                tag, why = f"{e['probability']:.0%} drop risk", f"Likely to fall {drop}"
            _card(e["symbol"], tag, "red", [why, _mine(e)])
            _log(plan_id, f"SELL:{e['company_id']}", logged)
    if not acts:
        with st.container(border=True):
            st.markdown('<div class="sa-sym">Nothing to sell</div>', unsafe_allow_html=True)
            ui.muted("None of your stocks has hit a sell rule or a 90% drop warning.")
    if watch:
        st.markdown(
            '<div class="sa-watchhead">Up next · watch, don\'t sell yet</div>',
            unsafe_allow_html=True,
        )
        for h in watch:
            _watch(
                f"s_{h['company_id']}",
                h["symbol"],
                f"{h['probability']:.0%} drop risk",
                [_mine(h)],
            )


def _hold(holds: list[dict]) -> None:
    _column_head("Hold", len(holds), "grey", "Keep these. Riskiest first.")
    with st.container(border=True):
        if not holds:
            ui.muted("Nothing else you own. Record your trades on Portfolio to see them here.")
        for h in holds:
            risk = (
                f"{h['probability']:.0%} drop risk" if h.get("probability") is not None else "Hold"
            )
            st.markdown(
                f'<div class="sa-holdrow"><div><span class="sa-sym">{escape(h["symbol"])}</span>'
                f'<div class="sa-line">{escape(_mine(h))}</div></div>'
                f'<span class="sa-muted">{escape(risk)}</span></div>',
                unsafe_allow_html=True,
            )


def render() -> None:
    with connect() as conn:
        row = latest_plan(conn)
        logged = actions_for(conn, row["plan_id"]) if row else {}
    if row is None:
        ui.header("This week")
        ui.hero("No plan yet", "It's built every Friday evening after the market closes.")
        return
    plan = row["payload"]
    built = row["built_at"].astimezone(IST)
    ui.header(
        "This week",
        f"Based on the market close of {row['signal_date']:%a %d %b} · "
        f"updated {built:%a %d %b, %H:%M}",
    )
    if plan["status"] == "NO_SIGNAL":
        ui.hero(
            "No advice this week",
            f"The market data didn't pass its checks ({plan['status_reason']}). The app won't "
            "guess: hold everything until the data is fixed.",
        )
        return

    left, right = st.columns([2, 3], vertical_alignment="center")
    with left:
        pct = st.slider(
            "Confidence",
            min_value=5,
            max_value=90,
            value=90,
            step=5,
            format="%d%%",
            key="confidence",
            help="How sure the app must be before it lists a stock.",
        )
    bar = pct / 100
    with right:
        if pct >= 90:
            ui.muted(
                "Act only on what's 90% sure. Slide left to peek at what's next in line: "
                "those are to watch, not to act on."
            )
        else:
            a, c = _track("A", bar), _track("C", bar)
            parts = []
            if a and a["signals"]:
                parts.append(f"buy calls rose as hoped {a['precision']:.0%} of the time")
            if c and c["signals"]:
                parts.append(f"drop warnings came true {c['precision']:.0%} of the time")
            ui.muted(
                f"At {pct}% in past years (2018 on), "
                + (" and ".join(parts) if parts else "there were too few calls to tell")
                + ". Watch these; don't act on them."
            )

    holds = plan["holds"]
    sell_watch = [h for h in holds if (h.get("probability") or 0) >= bar]
    keep = [h for h in holds if h not in sell_watch]
    sig = cfg().signals
    gain, when = sig.gain_threshold, horizon(sig.window_trading_days)

    b, h, s = st.columns(3, gap="medium")
    with b:
        _buy(plan, row["plan_id"], logged, bar, gain, when)
    with h:
        _hold(keep)
    with s:
        _sell(plan, row["plan_id"], logged, sell_watch, f"{sig.crash_threshold:.0%}+ {when}")

    gates = plan.get("gates", {})
    if any(g["status"] != "LIVE" for g in gates.values()):
        st.markdown('<div style="height:1.5rem"></div>', unsafe_allow_html=True)
        ui.muted(
            "The buy and drop predictions haven't yet proven 90% accurate in testing, so for now "
            "only rule-based sells (like a stop-loss) can be acted on. Rules are shown without a "
            "percentage because they're rules, not predictions."
        )
