"""M8 gate: fixture portfolios give the expected actions; history, reasons and storage behave."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import polars as pl
import psycopg
import pytest

from stockapp.alerts.telegram import check_summary_only
from stockapp.cli import plan_summary
from stockapp.config import load_app_config
from stockapp.plan.engine import Candidate, SignalGate, build_plan
from stockapp.plan.reasons import reason_line, top_drivers
from stockapp.plan.rules import (
    Drawdown,
    HoldingState,
    Regime,
    drawdown_state,
    exit_rules,
    market_regime,
)
from stockapp.plan.store import actions_for, latest_plan, log_action, save_plan
from stockapp.portfolio.history import value_history
from stockapp.portfolio.ledger import QuantityEvent, Txn

CFG = load_app_config(local_path=None)
LIVE = {"A": SignalGate("LIVE", 0.9, "ok"), "C": SignalGate("LIVE", 0.9, "ok")}
OFF = {"A": SignalGate("OFF", None, "best 43%"), "C": SignalGate("OFF", None, "best 65%")}
CALM = Regime(False, "normal")
NO_DD = Drawdown(-0.01, False, False)


def cand(cid: str, p_a: float = 0.5, gain: float = 0.12, close: float = 500.0, **kw) -> Candidate:
    base = dict(
        company_id=cid,
        symbol=cid,
        p_a=p_a,
        p_c=0.05,
        expected_gain=gain,
        last_close=close,
        atr_14=10.0,
        blocked=False,
        trade_for_trade=False,
        sessions_listed=600,
        reason_a="momentum",
        reason_c="",
    )
    base.update(kw)
    return Candidate(**base)


def hold(cid: str, cost: float = 100.0, last: float = 100.0, **kw) -> HoldingState:
    base = dict(
        company_id=cid,
        symbol=cid,
        cost_per_share=cost,
        last_close=last,
        atr_14=5.0,
        peak_close_since_buy=last,
        sessions_held=20,
    )
    base.update(kw)
    return HoldingState(**base)


def plan(
    candidates=(),
    holdings=(),
    gates=LIVE,
    regime=CALM,
    dd=NO_DD,
    quality=99.0,
    budget=10_000.0,
    weights=None,
    value=0.0,
):
    return build_plan(
        cfg=CFG,
        signal_date=date(2026, 10, 2),
        week_of=date(2026, 10, 5),
        quality_score=quality,
        candidates=list(candidates),
        holdings=list(holdings),
        holding_weights=weights or {},
        gates=gates,
        regime=regime,
        drawdown=dd,
        budget_available=budget,
        portfolio_value=value,
    )


def test_low_quality_means_no_signal_and_no_actions():
    p = plan([cand("AAA", p_a=0.99)], [hold("BBB", last=50)], quality=80.0)
    assert p.status == "NO_SIGNAL" and "below 90" in p.status_reason
    assert p.action_count == 0 and not p.holds


def test_stop_loss_is_a_rule_without_a_percentage():
    p = plan(holdings=[hold("BBB", cost=100, last=89)])  # stop = 100 - 2 x 5 = 90
    [item] = p.exits
    assert item.rule == "stop_loss" and item.probability is None
    assert "exit rule hit (stop loss)" in item.headline and "%" not in item.headline


def test_signal_c_live_sells_with_its_probability():
    p = plan([cand("BBB", p_c=0.93)], [hold("BBB")])
    assert p.exits[0].headline.startswith("Sell before the drop: crash risk 93%")


def test_off_signals_never_produce_actions_but_show_the_closest():
    p = plan([cand("AAA", p_a=0.95), cand("BBB", p_a=0.4)], [hold("CCC")], gates=OFF)
    assert p.opportunities == [] and p.exits == []
    assert p.closest["symbol"] == "AAA" and p.closest["signal_status"] == "OFF"
    assert [h.symbol for h in p.holds] == ["CCC"]


def test_opportunity_filters_ranking_cap_and_budget():
    cands = [
        cand("LOWP", p_a=0.5),  # below the 0.9 cutoff
        cand("BLOCK", p_a=0.95, blocked=True),
        cand("T2T", p_a=0.95, trade_for_trade=True),
        cand("NEW", p_a=0.95, sessions_listed=100),
        cand("HELD", p_a=0.95),
        *[cand(f"G{i}", p_a=0.95, gain=0.10 + i / 100, close=100.0) for i in range(7)],
    ]
    p = plan(cands, [hold("HELD")], budget=1_000_000)
    assert [o.symbol for o in p.opportunities] == ["G6", "G5", "G4", "G3", "G2"]  # top 5 by gain
    first = p.opportunities[0]
    assert first.sellout_price == pytest.approx(110.0) and first.stop_price == pytest.approx(80.0)


def test_budget_floor_and_minimum_position():
    p = plan(
        [cand("PRICEY", p_a=0.95, close=2_500.0), cand("OK", p_a=0.95, gain=0.11, close=300.0)],
        budget=4_000,
    )
    # empty portfolio: the cap is the minimum position, 3,000. PRICEY: 1 share = 2,500 is below
    # the minimum; OK: 10 x 300 = 3,000
    assert [(o.symbol, o.quantity) for o in p.opportunities] == [("OK", 10)]
    assert any("PRICEY" in n and "below the minimum" in n for n in p.notes)


def test_stress_regime_and_drawdown_pause_block_buys_not_holdings():
    stress = Regime(True, "stress: ...")
    p = plan([cand("AAA", p_a=0.95)], [hold("BIG")], regime=stress, weights={"BIG": 0.4})
    assert p.opportunities == [] and any("stress regime" in n for n in p.notes)
    assert "above the 15% cap in a stress regime" in p.holds[0].reason
    p2 = plan([cand("AAA", p_a=0.95)], dd=Drawdown(-0.13, True, True))
    assert p2.opportunities == [] and any("paused" in n for n in p2.notes)


def test_signal_b_target_needs_profit_after_costs_and_time_stop():
    base = dict(opened_by_signal_a=True, cost=100.0)
    rules = exit_rules(hold("X", last=111, net_profit_if_sold=50.0, sessions_held=2, **base), CFG)
    assert [r.rule for r in rules] == ["target_reached"]
    rules = exit_rules(hold("X", last=111, net_profit_if_sold=-1.0, sessions_held=2, **base), CFG)
    assert rules == []  # target reached but not profitable after charges: no nudge
    rules = exit_rules(hold("X", last=104, net_profit_if_sold=10.0, sessions_held=5, **base), CFG)
    assert [r.rule for r in rules] == ["time_stop"]


def test_trailing_stop_only_when_configured():
    h = hold("X", cost=50, last=120, peak_close_since_buy=150)
    assert exit_rules(h, CFG) == []
    cfg = CFG.model_copy(
        update={"risk": CFG.risk.model_copy(update={"trailing_stop_atr_multiple": 3.0})}
    )
    assert [r.rule for r in exit_rules(h, cfg)] == ["trailing_stop"]  # 150 - 15 = 135 >= 120


def test_regime_and_drawdown():
    assert market_regime(-0.05, 0.9, CFG).stress
    assert not market_regime(-0.05, 0.5, CFG).stress
    assert not market_regime(0.02, 0.95, CFG).stress
    assert not market_regime(None, 0.95, CFG).stress
    d = drawdown_state([1.0, 1.2, 1.04], CFG)  # -13.3% from the peak
    assert d.pause_buys and d.review
    assert drawdown_state([], CFG).from_peak is None


def test_value_history_twr_ignores_contributions_and_follows_bonus():
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(4)]
    prices = pl.DataFrame(
        {"company_id": ["X"] * 4, "trade_date": days, "close": [100.0, 110.0, 55.0, 55.0]}
    )  # bonus 1:1 on day 3
    bench = pl.DataFrame({"trade_date": days, "close": [1000.0, 1000.0, 1000.0, 1100.0]})
    txns = [
        Txn(1, "X", "BUY", 10, 100.0, days[0], days[0], 0.0),
        Txn(2, "X", "BUY", 10, 55.0, days[3], days[3], 0.0),
    ]
    events = [QuantityEvent("X", days[2], "bonus", ratio_new=1, ratio_held=1)]
    h = value_history(txns, events, prices, bench)
    assert h["value"].to_list() == [1000.0, 1100.0, 1100.0, 1650.0]
    assert h["twr_index"].to_list() == pytest.approx([1.0, 1.1, 1.1, 1.1])  # new money isn't gain
    assert h["benchmark_value"][-1] == pytest.approx(1000 * 1.1 + 550)


def test_reasons_are_templated_from_feature_values():
    feats = ["ret_20", "vol_20", "unknown_feature"]
    drivers = top_drivers(np.array([0.3, 0.1, 0.9, 0.0]), feats)
    assert drivers == ["ret_20", "vol_20"]  # no phrase for unknown_feature: skipped
    assert reason_line(drivers, {"ret_20": 0.18, "vol_20": 0.031}) == (
        "+18% over 20 days, daily volatility 3%"
    )
    assert reason_line(
        ["drawdown_60", "atr_14_pct"], {"drawdown_60": -0.48, "atr_14_pct": 0.13}
    ) == ("48% below its 3-month high, typical daily range 13%")
    assert reason_line([], {}) == "no single strong driver"


def test_telegram_summary_never_carries_amounts():
    p = plan([cand("AAA", p_a=0.95)], [hold("BBB", cost=100, last=80)], budget=50_000)
    text = plan_summary(p)
    check_summary_only(text)  # raises on any rupee amount
    assert "Sell: BBB" in text and "Buy: AAA" in text


def test_rebuilt_plan_keeps_actions_and_archives_the_old_version(db: psycopg.Connection):
    p = plan(gates=OFF)
    pid = save_plan(db, p, "fv")
    assert latest_plan(db)["plan_id"] == pid
    log_action(db, pid, "SELL:X", "done")
    log_action(db, pid, "SELL:X", "partly", "no cash")
    assert actions_for(db, pid)["SELL:X"]["action"] == "partly"
    # settings changed -> the week's plan is rebuilt in place
    pid2 = save_plan(db, plan(gates=OFF, budget=99.0), "fv", settings_version=7)
    assert pid2 == pid
    assert actions_for(db, pid)["SELL:X"]["reason"] == "no cash"
    row = latest_plan(db)
    assert row["settings_version"] == 7 and row["payload"]["budget_available"] == 99.0
    archived = db.execute(
        "SELECT payload FROM weekly_plan_revisions WHERE plan_id = %s", (pid,)
    ).fetchall()
    assert len(archived) == 1 and archived[0]["payload"]["budget_available"] == 10_000.0
    assert isinstance(row["built_at"], datetime)


def test_each_position_is_capped_at_the_stock_weight():
    # portfolio 30,000 + budget 4,000: cap 15% = 5,100, but only 4,000 cash -> 13 x 300
    p = plan(
        [cand("A1", p_a=0.95, close=300.0), cand("A2", p_a=0.95, gain=0.11, close=300.0)],
        budget=4_000,
        value=30_000,
    )
    assert [(o.symbol, o.quantity) for o in p.opportunities] == [("A1", 13)]
    p = plan([cand("A1", p_a=0.95, close=300.0)], budget=50_000, value=30_000)
    assert p.opportunities[0].quantity == 40  # 15% of 80,000 = 12,000 -> 40 shares, not 166


def test_atr_14_in_rupees():
    from stockapp.plan.inputs import atr_14

    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(20)]
    h = pl.DataFrame(
        {
            "trade_date": days,
            "adj_close": [100.0] * 20,
            "adj_high": [102.0] * 20,
            "adj_low": [99.0] * 20,
        }
    )
    assert atr_14(h) == pytest.approx(3.0)
    assert atr_14(h.head(10)) is None


def test_next_monday():
    from stockapp.plan.inputs import next_monday

    assert next_monday(date(2026, 10, 2)) == date(2026, 10, 5)  # Friday -> Monday
    assert next_monday(date(2026, 10, 1)) == date(2026, 10, 5)  # Thursday (Friday holiday)
    assert next_monday(date(2026, 10, 5)) == date(2026, 10, 12)


def test_buckets_cover_the_portfolio_sells_by_urgency_holds_riskiest_first():
    cands = [
        cand("SAFE", p_c=0.02),
        cand("RISKY", p_c=0.6),
        cand("CRASH", p_c=0.95),
        cand("LOSER", p_c=0.01),
        cand("TARGET", p_c=0.01),
    ]
    holdings = [
        hold("SAFE", quantity=10),
        hold("RISKY"),
        hold("CRASH"),
        hold("LOSER", cost=100, last=80),  # stop loss, the deepest loss
        hold("TARGET", cost=100, last=125, opened_by_signal_a=True, net_profit_if_sold=500.0),
    ]
    p = plan(cands, holdings)
    assert [i.symbol for i in p.exits] == ["LOSER", "TARGET", "CRASH"]  # losses, rules, model
    assert [i.symbol for i in p.holds] == ["RISKY", "SAFE"]  # riskiest first
    assert {i.symbol for i in p.exits + p.holds} == {h.symbol for h in holdings}
    safe = p.holds[1]
    assert safe.probability == 0.02 and safe.value == 1000.0 and safe.return_pct == 0.0
    assert p.exits[0].return_pct == pytest.approx(-0.2)


def test_watch_queue_ranks_unchosen_candidates_even_when_off():
    cands = [cand(f"S{i}", p_a=i / 100) for i in range(30)] + [cand("HELD", p_a=0.99)]
    p = plan(cands, [hold("HELD")], gates=OFF)
    assert p.opportunities == []
    assert [i.symbol for i in p.watch_buys][:3] == ["S29", "S28", "S27"]  # held never listed
    assert len(p.watch_buys) == 15 and p.watch_buys[0].probability == 0.29
    live = plan([cand("TOP", p_a=0.95), cand("NEXT", p_a=0.5)])
    assert [o.symbol for o in live.opportunities] == ["TOP"]
    assert [w.symbol for w in live.watch_buys] == ["NEXT"]  # chosen ones aren't repeated
