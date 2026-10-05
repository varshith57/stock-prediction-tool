"""Safety net: overlapping monthly warnings count once, the test needs separate, consistent
warnings, and a proven warning moves any holding to Sell (after hard rules)."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
from test_m8 import CALM, CFG, LIVE, NO_DD, cand, hold

from stockapp.models.safety import dedupe, judge
from stockapp.plan.engine import SignalGate, build_plan


def _preds(rows: list[tuple[str, date, float, bool]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=["company_id", "trade_date", "p", "label"], orient="row")


def test_overlapping_weeks_count_as_one_warning():
    d0 = date(2020, 1, 3)
    weeks = [d0 + timedelta(weeks=i) for i in range(6)]  # 0, 7, ..., 35 days
    p = _preds([("X", d, 0.9, True) for d in weeks] + [("Y", d0, 0.2, True)])
    kept = dedupe(p, 0.5, 28)
    assert kept["trade_date"].to_list() == [weeks[0], weeks[4]]  # 28 days apart; Y below cutoff


def _many(n_years: int, per_year: int, hit_share: float, p: float = 0.9) -> pl.DataFrame:
    rows = []
    for y in range(n_years):
        for i in range(per_year):  # a different stock each time: no deduplication
            rows.append((f"S{y}_{i}", date(2018 + y, 1, 5), p, i < hit_share * per_year))
    return _preds(rows)


def test_judge_needs_enough_consistent_separate_warnings():
    good = judge(_many(5, 10, 0.9), CFG)
    assert good.status == "LIVE" and good.cutoff == 0.3 and good.warnings == 50
    weak = judge(_many(5, 10, 0.7), CFG)
    assert weak.status == "OFF" and "needs 80%" in weak.reason
    few = judge(_many(1, 20, 1.0), CFG)
    assert few.status == "OFF"  # fewer than 30 separate warnings


def test_proven_warning_moves_any_holding_to_sell_after_rules():
    gate = SignalGate("LIVE", 0.6, "right 85% of the time on 120 separate warnings")
    holdings = [
        hold("INV", cost=100, last=100),  # an investment: rules never sell it, a warning can
        hold("RULE", cost=100, last=80, opened_by_signal_a=True),  # a trade past its stop
        hold("CALM", cost=100, last=100),
    ]
    cands = [cand("INV"), cand("RULE"), cand("CALM")]
    kw = dict(
        cfg=CFG, signal_date=date(2026, 10, 2), week_of=date(2026, 10, 5), quality_score=99.0,
        candidates=cands, holdings=holdings, holding_weights={}, gates=LIVE, regime=CALM,
        drawdown=NO_DD, budget_available=0.0,
    )  # fmt: skip
    p = build_plan(**kw, safety={"INV": 0.7, "RULE": 0.9, "CALM": 0.2}, safety_gate=gate)
    assert [e.symbol for e in p.exits] == ["RULE", "INV"]  # the hard rule first
    inv = p.exits[1]
    assert inv.safety and inv.probability == 0.7 and "Consider selling or trimming" in inv.reason
    assert [h.symbol for h in p.holds] == ["CALM"]
    off = build_plan(**kw, safety={"INV": 0.7}, safety_gate=SignalGate("OFF", None, "x"))
    assert [e.symbol for e in off.exits] == ["RULE"]  # unproven warnings change nothing
