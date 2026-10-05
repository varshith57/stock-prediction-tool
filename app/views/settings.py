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
from views.common import inr, lake

from stockapp import background, settings_store
from stockapp.config import AppConfig
from stockapp.db import connect
from stockapp.goal import feasibility
from stockapp.models.run import precision_at_bar, reevaluate_gate

LABELS = {
    "budget.weekly_inr": "Weekly budget",
    "budget.min_position_inr": "Minimum position",
    "signals.gain_threshold": "Gain threshold",
    "signals.crash_threshold": "Crash threshold",
    "signals.window_trading_days": "Signal window",
    "signals.certainty_bar": "Certainty bar",
    "signals.max_opportunities": "Max opportunities a week",
    "signals.gate.min_signals": "Minimum validated signals",
    "signals.gate.min_wilson_lower_bound": "Minimum lower bound",
    "risk.max_stock_weight": "Per-stock cap",
    "risk.max_sector_weight": "Per-sector cap",
    "risk.stop_atr_multiple": "Stop distance",
    "risk.trailing_stop_atr_multiple": "Trailing stop",
    "risk.drawdown_review": "Drawdown review level",
    "risk.drawdown_pause": "Drawdown pause level",
    "risk.stress_vix_percentile": "Stress regime VIX percentile",
    "universe.size": "Universe size",
    "data.min_quality_score": "Minimum data quality",
    "alerts.telegram_enabled": "Telegram alerts",
    "alerts.max_per_week": "Alerts a week",
    "alerts.hide_closest_candidate": "Hide closest candidate",
    "goal.target_monthly_return": "Goal",
    "costs.brokerage": "Brokerage",
    "costs.stt_rate": "STT",
    "costs.stamp_duty_buy_rate": "Stamp duty",
    "costs.exchange_txn_rate": "NSE charge",
    "costs.sebi_fee_rate": "SEBI fee",
    "costs.dp_charge_per_scrip_sell_inr": "DP charge",
    "costs.gst_rate": "GST",
    "costs.slippage_bps": "Slippage",
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
    """The few settings that change what you're shown each week."""
    ui.section("Your inputs")
    with st.container(border=True, key="sa_main"):
        a, b, d = st.columns(3)
        with a:
            v["budget"]["weekly_inr"] = st.number_input(
                "Weekly budget (₹)", value=float(c.budget.weekly_inr), min_value=1.0, step=500.0
            )
            _moves("Cash added each week for new buys. It builds up until a buy uses it.")
        with b:
            v["budget"]["min_position_inr"] = st.number_input(
                "Minimum position (₹)",
                value=float(c.budget.min_position_inr),
                min_value=1.0,
                step=500.0,
            )
            _moves("Smallest buy suggested. Higher means fewer, larger buys.")
        with d:
            v["signals"]["max_opportunities"] = st.number_input(
                "Buy ideas a week (max)",
                value=min(c.signals.max_opportunities, 5),
                min_value=0,
                max_value=5,
                step=1,
            )
            _moves("How many opportunities you see, best expected gain first. 5 at most.")
        a, b, d = st.columns(3)
        with a:
            v["risk"]["max_stock_weight"] = _pct(
                "Per-stock cap", c.risk.max_stock_weight, min_value=1.0
            )
            _moves("Most of the portfolio in one stock. Sets the size of each buy.")
        with b:
            v["risk"]["max_sector_weight"] = _pct(
                "Per-sector cap", c.risk.max_sector_weight, min_value=1.0
            )
            _moves("Most in one sector. Buys that would break it are skipped.")
        with d:
            current_goal = c.goal.target_monthly_return
            goal = _pct(
                "Monthly return goal",
                current_goal or 0.0,
                step=0.25,
                min_value=0.0,
                max_value=100.0,
            )
            v["goal"]["target_monthly_return"] = goal or None
            _moves("Compared with history below. Never changes recommendations. 0 = off.")
        note = st.text_input(
            "What changed and why (optional)",
            placeholder="e.g. raised budget",
            label_visibility="collapsed",
        )
        v["_note"] = note
        return st.form_submit_button("Save and apply", type="primary", icon=":material/check:")


def _set_once(c: AppConfig, v: dict) -> bool:
    """Rarely changed: shown toned down and folded away."""
    st.markdown(
        '<div class="sa-section sa-quiet-title">Set once · rarely changed</div>',
        unsafe_allow_html=True,
    )
    with st.container(key="sa_quiet"):
        _moves(
            "Defaults come from the PRD and the validated models. Most people never touch these."
        )
        with st.expander(
            f"Signals and validation · bar {c.signals.certainty_bar:.0%}, "
            f"+{c.signals.gain_threshold:.0%} / -{c.signals.crash_threshold:.0%} in "
            f"{c.signals.window_trading_days} days"
        ):
            a, b, d = st.columns(3)
            with a:
                v["signals"]["gain_threshold"] = _pct(
                    "Gain threshold (A)",
                    c.signals.gain_threshold,
                    min_value=1.0,
                    max_value=50.0,
                    help="Retrain needed after a change.",
                )
            with b:
                v["signals"]["crash_threshold"] = _pct(
                    "Crash threshold (C)",
                    c.signals.crash_threshold,
                    min_value=1.0,
                    max_value=50.0,
                    help="Retrain needed after a change.",
                )
            v["signals"]["window_trading_days"] = d.number_input(
                "Window (trading days)",
                value=c.signals.window_trading_days,
                min_value=1,
                max_value=20,
                step=1,
                help="Retrain needed after a change.",
            )
            a, b, d = st.columns(3)
            with a:
                v["signals"]["certainty_bar"] = _pct(
                    "Certainty bar",
                    max(c.signals.certainty_bar, 0.9),
                    min_value=90.0,
                    max_value=99.0,
                    help="Signals show only at or above this validated precision (never below "
                    "90%). Use the explorer below to see what a lower bar would have meant.",
                )
            with b:
                v["signals"]["gate"]["min_wilson_lower_bound"] = _pct(
                    "Minimum lower bound",
                    c.signals.gate.min_wilson_lower_bound,
                    min_value=40.0,
                    max_value=99.0,
                    help="95% confidence lower bound the gate also requires.",
                )
            v["signals"]["gate"]["min_signals"] = d.number_input(
                "Minimum validated signals", value=c.signals.gate.min_signals, min_value=10, step=5
            )
            _moves("Changing thresholds or the window needs a retrain (offered after saving).")

        trail_now = c.risk.trailing_stop_atr_multiple
        with st.expander(
            f"Risk rules · stop {c.risk.stop_atr_multiple:g}x ATR, review at "
            f"{c.risk.drawdown_review:.0%}, pause at {c.risk.drawdown_pause:.0%}"
        ):
            a, b, d = st.columns(3)
            v["risk"]["stop_atr_multiple"] = a.number_input(
                "Stop distance (x ATR below cost)",
                value=float(c.risk.stop_atr_multiple),
                min_value=0.5,
                max_value=10.0,
                step=0.5,
            )
            trailing_on = b.toggle("Trailing stop", value=trail_now is not None)
            trail = d.number_input(
                "Trailing distance (x ATR)",
                value=float(trail_now or 3.0),
                min_value=0.5,
                max_value=10.0,
                step=0.5,
            )
            v["risk"]["trailing_stop_atr_multiple"] = trail if trailing_on else None
            a, b, d = st.columns(3)
            with a:
                v["risk"]["drawdown_review"] = -_pct(
                    "Review when down", -c.risk.drawdown_review, min_value=1.0, max_value=60.0
                )
            with b:
                v["risk"]["drawdown_pause"] = -_pct(
                    "Pause new buys when down",
                    -c.risk.drawdown_pause,
                    min_value=1.0,
                    max_value=60.0,
                )
            with d:
                v["risk"]["stress_vix_percentile"] = _pct(
                    "Stress: VIX percentile",
                    c.risk.stress_vix_percentile,
                    min_value=50.0,
                    max_value=99.0,
                    help="Plus Nifty 500 below its 200-day average.",
                )

        with st.expander(
            f"Data and alerts · top {c.universe.size}, Telegram "
            f"{'on' if c.alerts.telegram_enabled else 'off'}"
        ):
            a, b, d = st.columns(3)
            v["universe"]["size"] = a.number_input(
                "Universe size (top N by turnover)",
                value=c.universe.size,
                min_value=50,
                max_value=1000,
                step=50,
                help="Retrain needed after a change.",
            )
            v["data"]["min_quality_score"] = b.number_input(
                "Minimum data quality (0-100)",
                value=float(c.data.min_quality_score),
                min_value=50.0,
                max_value=100.0,
                step=1.0,
                help="Below this: NO SIGNAL.",
            )
            v["alerts"]["max_per_week"] = d.number_input(
                "Alerts a week (max 3)",
                value=min(c.alerts.max_per_week, 3),
                min_value=0,
                max_value=3,
                step=1,
            )
            a, b = st.columns(2)
            v["alerts"]["telegram_enabled"] = a.toggle(
                "Telegram alerts", value=c.alerts.telegram_enabled
            )
            v["alerts"]["hide_closest_candidate"] = b.toggle(
                "Hide the closest-candidate line", value=c.alerts.hide_closest_candidate
            )

        with st.expander("Charges and tax · Zerodha delivery, indicative tax"):
            k = c.costs
            a, b, d, e = st.columns(4)
            with a:
                v["costs"]["stt_rate"] = _pct("STT each side", k.stt_rate, step=0.01)
            with b:
                v["costs"]["stamp_duty_buy_rate"] = _pct(
                    "Stamp duty (buy)", k.stamp_duty_buy_rate, step=0.001
                )
            with d:
                v["costs"]["exchange_txn_rate"] = _pct(
                    "NSE charge", k.exchange_txn_rate, step=0.0001
                )
            with e:
                v["costs"]["sebi_fee_rate"] = _pct("SEBI fee", k.sebi_fee_rate, step=0.0001)
            a, b, d, e = st.columns(4)
            with a:
                v["costs"]["gst_rate"] = _pct("GST", k.gst_rate, step=1.0)
            v["costs"]["dp_charge_per_scrip_sell_inr"] = b.number_input(
                "DP charge per sale (₹)",
                value=float(k.dp_charge_per_scrip_sell_inr),
                min_value=0.0,
                step=0.5,
            )
            v["costs"]["brokerage"] = d.number_input(
                "Brokerage (₹)", value=float(k.brokerage), min_value=0.0, step=1.0
            )
            v["costs"]["slippage_bps"] = e.number_input(
                "Slippage (bps)", value=float(k.slippage_bps), min_value=0.0, step=1.0
            )
            t = c.tax
            a, b, d = st.columns(3)
            with a:
                v["tax"]["stcg_rate"] = _pct("Short-term tax", t.stcg_rate, step=0.5)
            with b:
                v["tax"]["ltcg_rate"] = _pct("Long-term tax", t.ltcg_rate, step=0.5)
            v["tax"]["ltcg_exemption_inr"] = d.number_input(
                "Long-term exemption a year (₹)",
                value=float(t.ltcg_exemption_inr),
                min_value=0.0,
                step=5000.0,
            )
        return st.form_submit_button("Save", icon=":material/check:")


def _form(c: AppConfig) -> tuple[dict, str, bool]:
    """One form: the main inputs on top, the rarely changed ones folded away below. Either
    save button saves everything."""
    v = c.model_dump()
    with st.form("settings", border=False):
        main = _main_inputs(c, v)
        _goal(c)
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


def _explore(c: AppConfig) -> None:
    ui.section("Signal status and the bar")
    with st.container(border=True):
        gate_rows = _gate_rows()
        if gate_rows:
            pills = " ".join(
                ui.pill(
                    f"Signal {r['signal']} {r['status']}",
                    "green" if r["status"] == "LIVE" else "grey",
                )
                for r in gate_rows
            )
            st.markdown(pills, unsafe_allow_html=True)
            for r in gate_rows:
                ui.muted(f"{r['signal']}: {r['reason']}")
        bar = st.slider(
            "Explore a certainty bar",
            0.10,
            0.95,
            float(c.signals.certainty_bar),
            0.05,
            format="%.2f",
        )
        rows = []
        for s in ("A", "C"):
            r = precision_at_bar(lake(), c, s, bar)
            if r:
                rows.append(
                    {
                        "Signal": s,
                        "Signals at this bar": r["signals"],
                        "Correct": r["hits"],
                        "Precision": r["precision"],
                        "Lower bound": r["wilson_lb"],
                        "Weeks with a signal": r["weeks_with_signal"],
                    }
                )
        if rows:
            st.dataframe(
                pl.DataFrame(rows),
                hide_index=True,
                width="stretch",
                column_config={
                    k: st.column_config.NumberColumn(format="percent")
                    for k in ("Precision", "Lower bound")
                },
            )
        ui.muted(
            "From walk-forward predictions since 2018 (out of sample). Lowering the bar shows "
            "more signals and more wrong ones."
        )


def _goal(c: AppConfig) -> None:
    g = c.goal.target_monthly_return
    if g is None:
        return
    f = feasibility(lake(), g)
    tone = {"Realistic": "green", "Stretch": "amber", "Unrealistic": "red"}[f.band]
    ui.section("Goal feasibility")
    with st.container(border=True):
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
        "Change your inputs, then save: this week's plan updates at once. "
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

    _explore(current)

    ui.section("Change history")
    with st.container(border=True):
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

    ui.section("Schedule (on this Mac)")
    ui.muted(
        "Daily Mon-Thu 19:30 · weekly plan Fri 20:00 · monthly retrain on the first Saturday. "
        "Change with `uv run stockapp schedule`."
    )
    ui.muted(
        f"Charges and tax rates came from the PRD (checked 2 Oct 2026); confirm against a "
        f"contract note. A ₹3,500 round trip costs about {inr(23)} at these rates."
    )


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
