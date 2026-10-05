"""Money backtest: fills at the next open, exits by the app's trade rules, every charge and tax,
cash never invented, and returns measured the way an investor would."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from stockapp.config import load_app_config
from stockapp.strategy import Params, benchmark, max_drawdown, simulate, xirr

COSTS = load_app_config(local_path=None).costs


def _sessions(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _prices(cid: str, sessions: list[date], step: float, start: float = 100.0) -> pl.DataFrame:
    closes = [start * (1 + step) ** i for i in range(len(sessions))]
    return pl.DataFrame(
        {
            "company_id": cid,
            "trade_date": sessions,
            "open": closes,  # opens at yesterday-ish levels: simple, monotone
            "close": closes,
            "atr": [start * 0.02] * len(sessions),
        }
    )


def _signals(cid: str, sessions: list[date], p: float = 0.5) -> pl.DataFrame:
    fridays = [d for d in sessions if d.weekday() == 4]
    return pl.DataFrame(
        {
            "trade_date": fridays,
            "company_id": cid,
            "symbol": cid,
            "p": p,
            "expected_gain": 0.1,
            "stress": False,
        }
    )


P = Params(cutoff=0.2, weekly=0.0, initial=100_000.0, min_position=3000.0, drawdown_pause=None)


def test_xirr_and_drawdown():
    r = xirr([(date(2020, 1, 1), 100.0)], date(2021, 1, 1), 110.0)
    assert r == pytest.approx(0.10, abs=0.002)
    assert max_drawdown([1.0, 1.2, 0.9, 1.3]) == pytest.approx(0.9 / 1.2 - 1)
    assert xirr([], date(2021, 1, 1), 1.0) is None


def test_rising_stock_hits_its_target_and_pays_costs():
    s = _sessions(date(2021, 1, 4), 30)
    r = simulate(_signals("UP", s), _prices("UP", s, 0.03), s, P, COSTS)  # +12.6% by day 5
    assert r.trades and r.trades[0].reason == "target"
    t = r.trades[0]
    assert t.entry_date == s[5]  # first Friday is s[4]; filled at the next open
    assert t.pnl > 0 and r.fees > 0
    assert r.metrics["final_value"] > 100_000 and r.metrics["win_rate"] == 1.0


def test_falling_stock_is_stopped_out_with_a_loss():
    s = _sessions(date(2021, 1, 4), 30)
    r = simulate(_signals("DOWN", s), _prices("DOWN", s, -0.02), s, P, COSTS)
    assert r.trades[0].reason == "stop-loss" and r.trades[0].pnl < 0
    assert r.metrics["final_value"] < 100_000


def test_cutoff_above_every_pick_never_trades_and_money_sits_idle():
    s = _sessions(date(2021, 1, 4), 30)
    r = simulate(_signals("UP", s, p=0.1), _prices("UP", s, 0.02), s, P, COSTS)
    assert r.trades == [] and r.fees == 0
    assert r.metrics["final_value"] == pytest.approx(100_000)


def test_flat_stock_leaves_by_time_and_short_term_tax_is_charged_at_year_end():
    s = _sessions(date(2021, 2, 1), 60)  # crosses 1 April
    up = simulate(_signals("UP", s), _prices("UP", s, 0.01), s, P, COSTS)
    assert up.taxes > 0  # gains realised in FY 2020-21, taxed at the start of April
    flat = simulate(_signals("FLAT", s), _prices("FLAT", s, 0.0), s, P, COSTS)
    assert {t.reason for t in flat.trades} == {"time"}
    assert flat.taxes == 0 and flat.metrics["final_value"] < 100_000  # only costs lost


def test_weekly_money_and_benchmark_on_the_same_days():
    s = _sessions(date(2021, 1, 4), 30)
    weekly = Params(cutoff=0.99, weekly=1000.0, drawdown_pause=None)
    r = simulate(_signals("UP", s), _prices("UP", s, 0.02), s, weekly, COSTS)
    fridays = {d for d in s if d.weekday() == 4}
    assert r.metrics["contributed"] == 1000.0 * len(fridays)
    idx = pl.DataFrame({"trade_date": s, "close": [100.0] * 15 + [200.0] * 15})
    b = benchmark(idx, s, fridays, Params(cutoff=0, weekly=0.0, initial=1000.0))
    assert b["value"][-1] == pytest.approx(2000.0) and b["twr"][-1] == pytest.approx(2.0)


def test_spare_cash_can_ride_the_index_between_trades():
    s = _sessions(date(2021, 1, 4), 30)
    idx = pl.DataFrame({"trade_date": s, "close": [100.0 * 1.01**i for i in range(30)]})
    never = Params(cutoff=0.99, weekly=0.0, initial=1000.0, drawdown_pause=None)
    idle = simulate(_signals("UP", s), _prices("UP", s, 0.0), s, never, COSTS)
    assert idle.metrics["final_value"] == pytest.approx(1000.0)
    from dataclasses import replace

    parked = simulate(
        _signals("UP", s), _prices("UP", s, 0.0), s, replace(never, idle_in_index=True), COSTS, idx
    )
    assert parked.metrics["final_value"] == pytest.approx(1000.0 * 1.01**29)
