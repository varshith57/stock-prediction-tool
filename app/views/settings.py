"""Settings: edit everything here. Each save is validated, stored as a new version in the app
database (no files), and applied at once:

* plan settings (budget, caps, stops, regime, alerts) -> this week's plan is rebuilt;
* the certainty bar and gate rules -> signal status is re-checked on the stored out-of-sample
  predictions;
* thresholds, window and universe size -> models must be retrained (button, runs in the
  background).
"""

from __future__ import annotations

from datetime import date, datetime

import polars as pl
import streamlit as st
from pydantic import ValidationError
from views import ui
from views.common import lake

from stockapp import background, settings_store
from stockapp.config import AppConfig
from stockapp.db import connect
from stockapp.goal import feasibility
from stockapp.models.run import reevaluate_gate

LABELS = {
    "budget.weekly_inr": "Weekly budget",
    "budget.min_position_inr": "Smallest buy",
    "signals.gain_threshold": "Rise to look for",
    "signals.crash_threshold": "Drop to warn about",
    "signals.window_trading_days": "Within (market days)",
    "signals.certainty_bar": "Accuracy needed to act",
    "signals.max_opportunities": "Buy ideas a week",
    "signals.gate.min_signals": "Past calls needed as proof",
    "signals.gate.min_wilson_lower_bound": "Worst-case accuracy needed",
    "risk.max_stock_weight": "Most in one stock",
    "risk.max_sector_weight": "Most in one industry",
    "risk.stop_atr_multiple": "Stop-loss distance",
    "risk.trailing_stop_atr_multiple": "Lock in gains",
    "risk.drawdown_review": "Heads-up when down",
    "risk.drawdown_pause": "Stop new buys when down",
    "risk.stress_vix_percentile": "Nervous-market level",
    "universe.size": "Stocks to scan",
    "data.min_quality_score": "Data quality needed",
    "alerts.telegram_enabled": "Phone alerts",
    "alerts.max_per_week": "Alerts a week",
    "alerts.hide_closest_candidate": "Hide closest candidate",
    "goal.target_monthly_return": "Monthly return goal",
    "costs.brokerage": "Broker fee",
    "costs.stt_rate": "STT",
    "costs.stamp_duty_buy_rate": "Stamp duty",
    "costs.exchange_txn_rate": "Exchange fee",
    "costs.sebi_fee_rate": "SEBI fee",
    "costs.dp_charge_per_scrip_sell_inr": "Sell fee per stock",
    "costs.gst_rate": "GST",
    "costs.slippage_bps": "Price slip",
    "tax.stcg_rate": "Short-term tax",
    "tax.ltcg_rate": "Long-term tax",
    "tax.ltcg_exemption_inr": "Long-term exemption",
}
# Stored as fractions, shown as percentages.
PERCENT_KEYS = {
    "signals.gain_threshold",
    "signals.crash_threshold",
    "signals.certainty_bar",
    "signals.gate.min_wilson_lower_bound",
    "risk.max_stock_weight",
    "risk.max_sector_weight",
    "risk.drawdown_review",
    "risk.drawdown_pause",
    "risk.stress_vix_percentile",
    "goal.target_monthly_return",
    "costs.stt_rate",
    "costs.stamp_duty_buy_rate",
    "costs.exchange_txn_rate",
    "costs.sebi_fee_rate",
    "costs.gst_rate",
    "tax.stcg_rate",
    "tax.ltcg_rate",
}


def _pct(
    label: str,
    value: float,
    *,
    step: float = 0.5,
    min_value: float = 0.0,
    max_value: float = 100.0,
    help: str | None = None,
    key: str | None = None,
) -> float:
    """A percentage field: shown as 15.0 (%), stored as 0.15. Tiny rates (fine steps) show more
    decimals, and an untouched field returns the exact stored value (no rounding drift)."""
    decimals = 2 if step >= 0.01 else 5
    shown = round(value * 100, decimals)
    v = st.number_input(
        f"{label} (%)",
        value=shown,
        step=step,
        min_value=min_value,
        max_value=max_value,
        format=f"%.{decimals}f",
        help=help,
        key=key,
    )
    return value if v == shown else round(v / 100, 10)


def _moves(text: str) -> None:
    st.markdown(f'<div class="sa-moves">{text}</div>', unsafe_allow_html=True)


def _main_inputs(c: AppConfig, v: dict) -> bool:
    """The few settings that change what you're told to do each week."""
    ui.section("Your inputs")
    with st.container(border=True, key="sa_main"):
        a, b, d = st.columns(3)
        with a:
            v["budget"]["weekly_inr"] = st.number_input(
                "Weekly budget (₹)", value=float(c.budget.weekly_inr), min_value=1.0, step=500.0
            )
            _moves("New money you add each week. Whatever isn't spent rolls over.")
        with b:
            v["budget"]["min_position_inr"] = st.number_input(
                "Smallest buy (₹)",
                value=float(c.budget.min_position_inr),
                min_value=1.0,
                step=500.0,
            )
            _moves("Buys smaller than this aren't worth it: fees would eat the profit.")
        with d:
            v["risk"]["max_stock_weight"] = _pct(
                "Most in one stock", c.risk.max_stock_weight, min_value=1.0
            )
            _moves(
                "The most of your money any one company can take, so one bad pick can't sink you."
            )
        a, b, d = st.columns(3)
        with a:
            v["signals"]["max_opportunities"] = st.number_input(
                "Buy ideas a week",
                value=min(c.signals.max_opportunities, 5),
                min_value=0,
                max_value=5,
                step=1,
            )
            _moves("The most stocks you'll be told to buy in one week (5 at most).")
        with b:
            st.markdown('<div style="height:1.9rem"></div>', unsafe_allow_html=True)
            v["alerts"]["telegram_enabled"] = st.toggle(
                "Phone alerts", value=c.alerts.telegram_enabled
            )
            _moves("A short Telegram message when there's something to do (3 a week at most).")
        note = st.text_input(
            "What changed and why (optional)",
            placeholder="Note for yourself, e.g. raised budget",
            label_visibility="collapsed",
        )
        v["_note"] = note
        return st.form_submit_button("Save and apply", type="primary", icon=":material/check:")


def _status_line() -> str:
    rows = {r["signal"]: r for r in _gate_rows()}
    names = {"A": "Buy predictions", "C": "Drop predictions"}
    parts = []
    for s, name in names.items():
        r = rows.get(s)
        if r is None:
            continue
        state = "on" if r["status"] == "LIVE" else "off, not yet 90% accurate in testing"
        parts.append(f"{name}: {state}")
    return " · ".join(parts)


def _set_once(c: AppConfig, v: dict) -> bool:
    """Rarely changed: shown toned down and folded away, each with a plain one-liner."""
    st.markdown(
        '<div class="sa-section sa-quiet-title">Set once · rarely changed</div>',
        unsafe_allow_html=True,
    )
    with st.container(key="sa_quiet"):
        _moves("Sensible defaults are already filled in. Hover the ? next to any box for more.")
        with st.expander("How the predictions work"):
            status = _status_line()
            if status:
                _moves(f"Right now: {status}.")
            a, b, d = st.columns(3)
            with a:
                v["signals"]["gain_threshold"] = _pct(
                    "Rise to look for",
                    c.signals.gain_threshold,
                    min_value=1.0,
                    max_value=50.0,
                    help="A buy means the stock should rise at least this much in the time "
                    "below. Changing it means retraining (about an hour).",
                )
            with b:
                v["signals"]["crash_threshold"] = _pct(
                    "Drop to warn about",
                    c.signals.crash_threshold,
                    min_value=1.0,
                    max_value=50.0,
                    help="A sell warning means the stock may fall at least this much. "
                    "Changing it means retraining.",
                )
            v["signals"]["window_trading_days"] = d.number_input(
                "Within (market days)",
                value=c.signals.window_trading_days,
                min_value=1,
                max_value=20,
                step=1,
                help="How many market days the rise or drop has to happen in. 5 is one week. "
                "Changing it means retraining.",
            )
            a, b, d = st.columns(3)
            with a:
                v["signals"]["certainty_bar"] = _pct(
                    "Accuracy needed to act",
                    max(c.signals.certainty_bar, 0.9),
                    min_value=90.0,
                    max_value=99.0,
                    help="How often similar past calls must have been right before the app "
                    "tells you to act. Never below 90%.",
                )
            with b:
                v["signals"]["gate"]["min_wilson_lower_bound"] = _pct(
                    "Worst-case accuracy needed",
                    c.signals.gate.min_wilson_lower_bound,
                    min_value=40.0,
                    max_value=99.0,
                    help="Even allowing for luck in the test, accuracy must be at least this.",
                )
            v["signals"]["gate"]["min_signals"] = d.number_input(
                "Past calls needed as proof",
                value=c.signals.gate.min_signals,
                min_value=10,
                step=5,
                help="How many past calls the test needs before its accuracy counts.",
            )

        trail_now = c.risk.trailing_stop_atr_multiple
        with st.expander("Safety rules"):
            _moves("Automatic rules that protect your money. They're rules, not predictions.")
            a, b, d = st.columns(3)
            v["risk"]["stop_atr_multiple"] = a.number_input(
                "Stop-loss distance",
                value=float(c.risk.stop_atr_multiple),
                min_value=0.5,
                max_value=10.0,
                step=0.5,
                help="Sell when a stock falls this many 'normal days' below what you paid. A "
                "normal day is its typical daily swing; 2 means about two days' worth.",
            )
            trailing_on = b.toggle(
                "Lock in gains",
                value=trail_now is not None,
                help="Also sell when a stock falls back this far from its best price since "
                "you bought (a trailing stop).",
            )
            trail = d.number_input(
                "Lock-in distance",
                value=float(trail_now or 3.0),
                min_value=0.5,
                max_value=10.0,
                step=0.5,
                help="How far (in normal days) a stock may fall from its best before you sell.",
            )
            v["risk"]["trailing_stop_atr_multiple"] = trail if trailing_on else None
            a, b, d = st.columns(3)
            with a:
                v["risk"]["drawdown_review"] = -_pct(
                    "Heads-up when down",
                    -c.risk.drawdown_review,
                    min_value=1.0,
                    max_value=60.0,
                    help="A warning when your whole portfolio is this far below its best value.",
                )
            with b:
                v["risk"]["drawdown_pause"] = -_pct(
                    "Stop new buys when down",
                    -c.risk.drawdown_pause,
                    min_value=1.0,
                    max_value=60.0,
                    help="No new buys while your portfolio is this far below its best value.",
                )
            with d:
                v["risk"]["max_sector_weight"] = _pct(
                    "Most in one industry",
                    c.risk.max_sector_weight,
                    min_value=1.0,
                    help="The most of your money one industry (like banks) can hold.",
                )
            v["risk"]["stress_vix_percentile"] = _pct(
                "Nervous-market level",
                c.risk.stress_vix_percentile,
                min_value=50.0,
                max_value=99.0,
                help="New buys pause when India's fear index (VIX) is above this share of its "
                "past year and the market is falling.",
            )

        with st.expander("Data and alerts"):
            a, b, d = st.columns(3)
            v["universe"]["size"] = a.number_input(
                "Stocks to scan",
                value=c.universe.size,
                min_value=50,
                max_value=1000,
                step=50,
                help="How many of India's most traded stocks the app looks at. Changing it "
                "means retraining.",
            )
            v["data"]["min_quality_score"] = b.number_input(
                "Data quality needed (out of 100)",
                value=float(c.data.min_quality_score),
                min_value=50.0,
                max_value=100.0,
                step=1.0,
                help="If the day's market data scores below this, the app gives no advice "
                "rather than guess.",
            )
            v["alerts"]["max_per_week"] = d.number_input(
                "Alerts a week (max 3)",
                value=min(c.alerts.max_per_week, 3),
                min_value=0,
                max_value=3,
                step=1,
                help="Sell-rule alerts always get through.",
            )

        with st.expander("Fees and tax"):
            _moves(
                "Used to work out real profit after costs. Zerodha delivery rates are filled in."
            )
            k = c.costs
            a, b, d, e = st.columns(4)
            with a:
                v["costs"]["stt_rate"] = _pct(
                    "STT", k.stt_rate, step=0.01, help="Government tax on every buy and sell."
                )
            with b:
                v["costs"]["stamp_duty_buy_rate"] = _pct(
                    "Stamp duty",
                    k.stamp_duty_buy_rate,
                    step=0.001,
                    help="State tax charged when you buy.",
                )
            with d:
                v["costs"]["exchange_txn_rate"] = _pct(
                    "Exchange fee", k.exchange_txn_rate, step=0.0001, help="NSE's fee per trade."
                )
            with e:
                v["costs"]["sebi_fee_rate"] = _pct(
                    "SEBI fee", k.sebi_fee_rate, step=0.0001, help="The market regulator's fee."
                )
            a, b, d, e = st.columns(4)
            with a:
                v["costs"]["gst_rate"] = _pct(
                    "GST", k.gst_rate, step=1.0, help="GST charged on the fees above."
                )
            v["costs"]["dp_charge_per_scrip_sell_inr"] = b.number_input(
                "Sell fee per stock (₹)",
                value=float(k.dp_charge_per_scrip_sell_inr),
                min_value=0.0,
                step=0.5,
                help="Fixed depository fee for each stock you sell on a day.",
            )
            v["costs"]["brokerage"] = d.number_input(
                "Broker fee (₹)",
                value=float(k.brokerage),
                min_value=0.0,
                step=1.0,
                help="Per order. Zerodha charges nothing for delivery trades.",
            )
            v["costs"]["slippage_bps"] = e.number_input(
                "Price slip (bps)",
                value=float(k.slippage_bps),
                min_value=0.0,
                step=1.0,
                help="Allowance for paying a bit more or selling a bit lower than the quoted "
                "price. 100 bps = 1%.",
            )
            t = c.tax
            a, b, d = st.columns(3)
            with a:
                v["tax"]["stcg_rate"] = _pct(
                    "Tax: held under a year",
                    t.stcg_rate,
                    step=0.5,
                    help="Tax on profit from shares sold within a year of buying.",
                )
            with b:
                v["tax"]["ltcg_rate"] = _pct(
                    "Tax: held over a year",
                    t.ltcg_rate,
                    step=0.5,
                    help="Tax on profit from shares held longer than a year.",
                )
            v["tax"]["ltcg_exemption_inr"] = d.number_input(
                "Tax-free profit a year (₹)",
                value=float(t.ltcg_exemption_inr),
                min_value=0.0,
                step=5000.0,
                help="Long-term profit up to this each year is tax-free.",
            )

        with st.expander("Monthly goal (optional)"):
            current_goal = c.goal.target_monthly_return
            goal = _pct(
                "Monthly return goal",
                current_goal or 0.0,
                step=0.25,
                min_value=0.0,
                max_value=100.0,
                help="Only for comparison with history. It never changes the advice. 0 = off.",
            )
            v["goal"]["target_monthly_return"] = goal or None
            _goal(c)
        return st.form_submit_button("Save", icon=":material/check:")


def _form(c: AppConfig) -> tuple[dict, str, bool]:
    """One form: the main inputs on top, the rarely changed ones folded away below. Either
    save button saves everything."""
    v = c.model_dump()
    with st.form("settings", border=False):
        main = _main_inputs(c, v)
        rest = _set_once(c, v)
    return v, v.pop("_note", ""), main or rest


def _extra_checks(c: AppConfig) -> list[str]:
    problems = []
    if c.risk.drawdown_pause > c.risk.drawdown_review:
        problems.append("The pause level must be a deeper drawdown than the review level.")
    if c.risk.max_sector_weight < c.risk.max_stock_weight:
        problems.append("The per-sector cap can't be smaller than the per-stock cap.")
    if c.signals.gate.min_wilson_lower_bound > c.signals.certainty_bar:
        problems.append("The minimum lower bound can't be above the certainty bar.")
    return problems


def _apply(changes: dict, new: AppConfig) -> list[str]:
    done = []
    effects = settings_store.impact(changes)
    today = date.today()
    if "gate" in effects:
        gates = reevaluate_gate(lake(), new, today)
        done.append(
            "Signal status re-checked: " + ", ".join(f"{s} {g.status}" for s, g in gates.items())
        )
    if "plan" in effects and lake().has_table("gold", "latest_scores"):
        from stockapp.pipeline import build_weekly_plan

        with connect() as conn:
            p, _ = build_weekly_plan(conn, lake(), new, today)
        done.append(f"This week's plan rebuilt: {p.action_count} action(s)")
    if "retrain" in effects:
        done.append("Thresholds, window or universe changed: retrain below to update the models")
    return done


def _retrain_panel(pending: bool) -> None:
    status = background.status()
    running = status is not None and status["running"]
    if not (pending or running or (status and status.get("failed"))):
        return
    ui.section("Retrain")
    with st.container(border=True):
        if running:
            started = datetime.fromisoformat(status["started_at"])
            st.markdown(
                ui.pill("Running", "blue") + f'&nbsp; <span class="sa-muted">started '
                f"{started:%H:%M}</span>",
                unsafe_allow_html=True,
            )
            for line in status["tail"]:
                ui.muted(line)
            st.button("Refresh progress", icon=":material/refresh:")
        elif status and status.get("failed") and not pending:
            st.markdown(ui.pill("Last retrain failed", "red"), unsafe_allow_html=True)
            for line in status["tail"]:
                ui.muted(line)
        if pending and not running:
            ui.muted(
                "Your threshold, window or universe change isn't in the models yet. A "
                "retrain rebuilds labels, re-runs the walk-forward test and gate, and "
                "retrains (about 10 minutes, in the background)."
            )
            if st.button("Retrain now", type="primary", icon=":material/model_training:"):
                background.start_retrain()
                st.rerun()


def _retrain_pending(history: list[dict]) -> bool:
    status = background.status()
    last_done = (
        datetime.fromisoformat(status["started_at"]) if status and status.get("done") else None
    )
    for h in history:
        keys = list((h["changed"] or {}).keys())
        if any(k.startswith(settings_store.RETRAIN_KEYS) for k in keys):
            created = h["created_at"].astimezone().replace(tzinfo=None)
            if last_done is None or created > last_done:
                return True
    return False


def _goal(c: AppConfig) -> None:
    g = c.goal.target_monthly_return
    if g is None:
        return
    f = feasibility(lake(), g)
    tone = {"Realistic": "green", "Stretch": "amber", "Unrealistic": "red"}[f.band]
    with st.container():
        prob = f"{f.probability:.0%}" if f.probability is not None else "unknown"
        st.markdown(
            f'<div class="sa-row"><div><span class="sa-sym">{g:+.2%} a month</span>&nbsp;&nbsp;'
            f"{ui.pill(f.band, tone)}</div><div class='sa-muted'>about {f.yearly_equivalent:+.0%} "
            "a year compounded</div></div>",
            unsafe_allow_html=True,
        )
        ui.muted(
            f"The Nifty 500 returned at least this in {prob} of months over "
            f"{f.months} months of history. Bands: up to 2% realistic, 2-5% stretch, "
            "above 5% unrealistic."
        )


def render() -> None:
    with connect() as conn:
        version = settings_store.ensure_seeded(conn)
        current = settings_store.latest_config(conn)
        history = settings_store.history(conn)
    assert current is not None
    when = history[0]["created_at"].astimezone() if history else None
    ui.header(
        "Settings",
        "Change your inputs and save: the advice updates straight away. "
        f"Version {version}" + (f" · saved {when:%d %b, %H:%M}" if when else ""),
    )

    if "settings_result" in st.session_state:
        ok, lines = st.session_state.pop("settings_result")
        (st.success if ok else st.info)("\n".join(f"- {x}" for x in lines))

    _retrain_panel(_retrain_pending(history))

    values, note, saved = _form(current)
    if saved:
        try:
            new = AppConfig.model_validate(values)
        except ValidationError as exc:
            st.error(
                "Not saved: "
                + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            )
            return
        problems = _extra_checks(new)
        if problems:
            st.error("Not saved: " + " ".join(problems))
            return
        with connect() as conn:
            vid, changes = settings_store.save(conn, new, note or None)
        if not vid:
            st.session_state["settings_result"] = (False, ["No changes to save."])
            st.rerun()
        lines = [f"Saved as version {vid}:"] + [
            f"{LABELS.get(k, k)}: {_show(old, k)} → {_show(new_v, k)}"
            for k, (old, new_v) in changes.items()
        ]
        with st.spinner("Applying..."):
            lines += _apply(changes, new)
        st.session_state["settings_result"] = (True, lines)
        st.rerun()

    st.markdown('<div style="height:1.5rem"></div>', unsafe_allow_html=True)
    with st.expander("Change history"):
        for h in history[:10]:
            keys = ", ".join(LABELS.get(k, k) for k in (h["changed"] or {})) or "initial values"
            st.markdown(
                f'<div class="sa-row" style="padding:.2rem 0"><span><b>v{h["version_id"]}</b> · '
                f"{keys}</span><span class='sa-muted'>{h['created_at'].astimezone():%d %b %H:%M}"
                f"{' · ' + h['note'] if h['note'] else ''}</span></div>",
                unsafe_allow_html=True,
            )
        a, b = st.columns([2, 1], vertical_alignment="bottom")
        target = a.selectbox(
            "Restore a version",
            [h["version_id"] for h in history],
            index=None,
            placeholder="Version",
        )
        if b.button("Restore", disabled=target is None or target == version):
            with connect() as conn:
                vid, changes = settings_store.restore(conn, int(target))
            if vid:
                restored = settings_store.latest_config()
                lines = [f"Restored version {target} as version {vid}", *_apply(changes, restored)]
                st.session_state["settings_result"] = (True, lines)
            st.rerun()

    ui.muted("The app updates itself on this Mac: daily Mon-Thu 19:30, weekly advice Friday 20:00.")


def _gate_rows() -> list[dict]:
    lk = lake()
    if not lk.has_table("gold", "signal_gate"):
        return []
    g = lk.scan("gold", "signal_gate").collect()
    return g.filter(pl.col("built") == pl.col("built").max()).sort("signal").to_dicts()


def _show(v: object, key: str = "") -> str:
    if v is None:
        return "off"
    if isinstance(v, bool):
        return "on" if v else "off"
    if key in PERCENT_KEYS and isinstance(v, int | float):
        return f"{v * 100:.6g}%"
    return f"{v:g}" if isinstance(v, float) else str(v)
