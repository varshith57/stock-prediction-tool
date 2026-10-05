"""This week (home): three buckets, Buy | Hold | Sell, that together hold the whole portfolio.

"Act now" items are the saved weekly plan: validated signals at the bar (90%+, gate LIVE) and
rule-based exits. The confidence slider only reveals what's next in line below that ("watch, not
a signal"), from the queue saved with the same plan; it never turns a watch item into an action.
Sells come first by urgency, holds riskiest first, buys by chance.
"""

from __future__ import annotations

from datetime import date
from html import escape

import streamlit as st
from views import ui
from views.common import cfg, inr, lake

from stockapp.cli import IST
from stockapp.config import horizon
from stockapp.db import connect
from stockapp.models.run import precision_at_bar
from stockapp.plan.store import actions_for, latest_plan, log_action, review_decisions

ACT_BAR = 0.90
LOG = {"done": "Done", "skipped": "Skipped"}
REVIEW_LOG = {"kept": "Keep", "sold": "Sold"}
KEEP_QUIET_DAYS = 28  # after "Keep", the flag stays quiet this long
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


def _todo(text: str) -> None:
    st.markdown(
        f'<div class="sa-todo"><b>What to do:</b> {escape(text)}</div>', unsafe_allow_html=True
    )


def _log(plan_id: int, key: str, logged: dict, options: dict | None = None) -> None:
    options = options or LOG
    current = logged.get(key)
    choice = st.segmented_control(
        "Log",
        list(options),
        format_func=options.get,
        default=current["action"] if current and current["action"] in options else None,
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
    core = plan.get("core_inr") or 0.0
    _column_head("Buy", len(acts) + (1 if core else 0), "green", "Index fund first, then ideas.")
    if core:
        share = cfg().budget.satellite_share
        with st.container(border=True):
            _card(
                "Index fund",
                "Core",
                "green",
                [
                    f"Put {inr(core)} into a Nifty 50 or Nifty 500 index fund or ETF this week",
                    f"That's {1 - share:.0%} of your weekly budget. Stock ideas get the other "
                    f"{share:.0%}, so a bad run of ideas can only touch a small slice.",
                ],
            )
            _todo(
                f"In Kite, open Coin and invest {inr(core)} in a Nifty 50 or Nifty 500 index fund "
                "(direct plan), or buy an index ETF for that amount. Then tap Done."
            )
            _log(plan_id, "CORE", logged)
    for o in acts:
        with st.container(border=True):
            _card(
                o["symbol"],
                f"{o['probability']:.0%} sure",
                "green",
                [
                    f"Buy {o['quantity']} shares near {inr(o['guide_price'], 2)}",
                    f"Expected gain after costs {o['expected_gain']:+.1%}",
                    f"Sell when it reaches {inr(o['sellout_price'], 2)} (+{gain:.0%})",
                ],
            )
            _todo(
                f"At Monday's open in Kite, place a limit order for {o['quantity']} shares at "
                f"about {inr(o['guide_price'] * 1.01, 2)}. Skip it if the price is already more "
                "than 2% higher. Then tap Done and record it on Portfolio as a Trade: the app "
                "will tell you when to sell."
            )
            _log(plan_id, f"BUY:{o['company_id']}", logged)
    if not acts:
        with st.container(border=True):
            st.markdown('<div class="sa-sym">No stock ideas</div>', unsafe_allow_html=True)
            a = plan.get("gates", {}).get("A", {})
            ui.muted(
                "Stock ideas haven't proven they beat an index fund after costs, so none are "
                "'act now'. Waiting is a decision too."
                if a and a.get("status") != "LIVE"
                else f"Nothing qualifies {when}. Waiting is a decision too."
            )
    for n in plan["notes"]:
        if "paused" in n:
            st.warning(
                n[0].upper() + n[1:] + ". What to do: nothing. Keep holding what you have and "
                "keep adding the index-fund amount; stock buying resumes on its own."
            )
        elif "stress" in n:
            st.warning(
                "The market is nervous, so new stock buys are paused. What to do: keep holding "
                "and keep adding the index-fund amount."
            )
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
                [f"Near {inr(w['guide_price'], 2)} · don't buy yet: it hasn't passed the bar"],
            )


def _sell(plan: dict, plan_id: int, logged: dict, watch: list[dict], drop: str) -> None:
    acts = plan["exits"]
    _column_head(
        "Sell", len(acts), "red", "Most urgent first. Investments are never sold by a rule."
    )
    for e in acts:
        with st.container(border=True):
            qty = e.get("quantity")
            shares = f"{qty} shares" if qty else "your shares"
            if e.get("rule"):
                tag, why = RULE_NAMES.get(e["rule"], "Rule"), e["reason"]
                todo = {
                    "target_reached": f"Sell all {shares} at Monday's open in Kite to take the "
                    "profit.",
                    "time_stop": f"Sell all {shares} at Monday's open in Kite: the trade had its "
                    "window and the money can go to the next idea.",
                }.get(
                    e["rule"],
                    f"Sell all {shares} at Monday's open in Kite (a market order) so the loss "
                    "can't grow.",
                )
            else:
                if e.get("safety"):
                    tag, why = "Drop warning", f"{e['headline']}. {e['reason']}."
                else:
                    tag, why = f"{e['probability']:.0%} drop risk", f"Likely to fall {drop}"
                half = f"half ({qty // 2} shares)" if qty and qty > 1 else "some"
                todo = (
                    f"Cut risk: sell {half} in Kite, or all {shares} if you'd rather be safe. "
                    "Keep the rest if you still believe in the company."
                )
            _card(e["symbol"], tag, "red", [why, _mine(e)])
            _todo(todo + " Then tap Done and record the sale on Portfolio.")
            _log(plan_id, f"SELL:{e['company_id']}", logged)
    if not acts:
        with st.container(border=True):
            st.markdown('<div class="sa-sym">Nothing to sell</div>', unsafe_allow_html=True)
            ui.muted("None of your stocks has hit a sell rule or a proven drop warning.")
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


def _hold(holds: list[dict], plan_id: int, logged: dict, decisions: dict) -> None:
    _column_head("Hold", len(holds), "grey", "Nothing to do for these unless one is flagged.")
    today = date.today()
    with st.container(border=True):
        if not holds:
            ui.muted("Nothing else you own. Record your trades on Portfolio to see them here.")
        for h in holds:
            cid = h["company_id"]
            last = decisions.get(cid)
            kept_recently = (
                h.get("review")
                and last is not None
                and last["action"] == "kept"
                and (today - last["acted_at"].date()).days < KEEP_QUIET_DAYS
            )
            if h.get("review") and not kept_recently:
                right = ui.pill("Review", "amber")
            elif kept_recently:
                right = '<span class="sa-muted">Kept</span>'
            elif h.get("probability") is not None:
                right = f'<span class="sa-muted">{h["probability"]:.0%} drop risk</span>'
            else:
                right = '<span class="sa-muted">Hold</span>'
            st.markdown(
                f'<div class="sa-holdrow"><div><span class="sa-sym">{escape(h["symbol"])}</span>'
                f'<div class="sa-line">{escape(_mine(h))}</div></div>{right}</div>',
                unsafe_allow_html=True,
            )
            if kept_recently:
                ui.muted(
                    f"You chose to keep it on {last['acted_at']:%d %b}. Nothing to do; it'll ask "
                    f"again after {KEEP_QUIET_DAYS} days if it's still down."
                )
            elif h.get("review"):
                price = inr(h.get("guide_price"), 2) if h.get("guide_price") else "today's price"
                ui.muted(h["reason"] + ".")
                _todo(
                    f"1) Check why it fell: recent results or news (Kite's stock page has both). "
                    f"2) Ask yourself: would I buy it today at {price}? "
                    "3) Yes: tap Keep and hold on. No: sell in Kite, tap Sold and record the "
                    "sale on Portfolio."
                )
                _log(plan_id, f"REVIEW:{cid}", logged, REVIEW_LOG)


def _price_date(plan: dict, row: dict):
    """The close the values on this page use (the plan is refreshed every morning)."""
    from views.data_status import _status

    as_of = _status().get("as_of")
    return as_of if as_of and as_of >= row["signal_date"] else row["signal_date"]


def render() -> None:
    with connect() as conn:
        row = latest_plan(conn)
        logged = actions_for(conn, row["plan_id"]) if row else {}
        decisions = review_decisions(conn)
    if row is None:
        ui.header("This week")
        ui.hero("No plan yet", "It's built every Friday evening after the market closes.")
        return
    plan = row["payload"]
    built = row["built_at"].astimezone(IST)
    ui.header(
        "This week",
        f"Prices: NSE official close of {_price_date(plan, row):%a %d %b} · "
        f"ideas from the week ending {row['signal_date']:%a %d %b} · "
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
                "Only proven ideas are 'act now'. Slide left to see stocks by their chance of "
                f"rising {cfg().signals.gain_threshold:.0%} "
                f"{horizon(cfg().signals.window_trading_days)}: those are to watch, not to act on."
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
        _hold(keep, row["plan_id"], logged, decisions)
    with s:
        _sell(plan, row["plan_id"], logged, sell_watch, f"{sig.crash_threshold:.0%}+ {when}")

    gates = plan.get("gates", {})
    notes = []
    a, c = gates.get("A"), gates.get("C")
    if a and a["status"] != "LIVE":
        notes.append(f"Stock ideas: {a['reason']}.")
    elif a and a["reason"].startswith("proven"):
        notes.append(
            f"Stock ideas passed the money test: {a['reason'].removeprefix('proven: ')}. The "
            "margin is small, so watch the paper-trading record on Track record for a few "
            "months before putting real money behind them."
        )
    if c and c["status"] != "LIVE":
        notes.append(
            "Drop warnings haven't passed their tests yet, so sells come only from rules on "
            "trades (stop-loss, target, time), shown without a percentage because they're rules."
        )
    if notes:
        st.markdown('<div style="height:1.5rem"></div>', unsafe_allow_html=True)
        for n in notes:
            ui.muted(n[0].upper() + n[1:])
