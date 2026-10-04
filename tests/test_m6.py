"""M6: walk-forward folds are purged, and the pipeline finds signal only when there is one."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from stockapp.models.backtest import calibration_table, run_backtest, summarize
from stockapp.models.walkforward import EMBARGO_DAYS, make_folds


def test_folds_are_quarterly_and_purged():
    folds = make_folds(date(2018, 2, 15), date(2019, 6, 30))
    assert [f.test_start for f in folds] == [
        date(2018, 1, 1), date(2018, 4, 1), date(2018, 7, 1), date(2018, 10, 1),
        date(2019, 1, 1), date(2019, 4, 1),
    ]  # fmt: skip
    for f in folds:
        # training ends an embargo before calibration starts; calibration an embargo before test
        assert f.train_end == f.calib_start - timedelta(days=EMBARGO_DAYS)
        assert f.calib_end == f.test_start - timedelta(days=EMBARGO_DAYS)
        assert f.calib_start < f.calib_end < f.test_start < f.test_end
        # a training sample's label window (<= 7 calendar days) can't reach calibration or test
        assert f.train_end + timedelta(days=7) < f.calib_start


def _synthetic(signal: float, seed: int, weeks: int = 260, stocks: int = 120) -> pl.DataFrame:
    """Weekly samples with two informative-looking features. ``signal`` sets how much feature
    ``x_signal`` raises the event probability; volatility rank raises it a little in all cases."""
    rng = np.random.default_rng(seed)
    start = date(2016, 1, 1)
    rows = []
    for w in range(weeks):
        day = start + timedelta(weeks=w)
        vol = rng.uniform(0, 1, stocks)
        x = rng.normal(0, 1, stocks)
        logit = -3 + 0.8 * vol + signal * x
        y = rng.uniform(0, 1, stocks) < 1 / (1 + np.exp(-logit))
        for i in range(stocks):
            rows.append(
                {"company_id": f"S{i}", "symbol": f"S{i}", "trade_date": day, "blocked": False,
                 "rank_vol_20": vol[i], "rank_ret_20": rng.uniform(), "x_signal": x[i],
                 "x_noise": rng.normal(), "label_a": bool(y[i]), "max_gain_5": 0.0}
            )  # fmt: skip
    return pl.DataFrame(rows)


FEATS = ["rank_vol_20", "rank_ret_20", "x_signal", "x_noise"]


@pytest.mark.slow
def test_no_signal_means_no_edge_over_volatility():
    s = _synthetic(signal=0.0, seed=1)
    bt = run_backtest(s, FEATS, "label_a", date(2019, 1, 1))
    t = {r["score"]: r for r in summarize(bt).iter_rows(named=True)}
    # the model can only rediscover volatility; it must not look much better than that baseline
    assert t["model"]["auc"] < t["volatility"]["auc"] + 0.02


@pytest.mark.slow
def test_real_signal_is_found_and_calibrated():
    s = _synthetic(signal=1.2, seed=2)
    bt = run_backtest(s, FEATS, "label_a", date(2019, 1, 1))
    t = {r["score"]: r for r in summarize(bt).iter_rows(named=True)}
    assert t["model"]["auc"] > t["volatility"]["auc"] + 0.1
    assert t["model"]["top5_precision"] > 2 * t["base_rate"]["top5_precision"]
    cal = calibration_table(bt).filter(pl.col("n") >= 200)
    assert ((cal["stated"] - cal["observed"]).abs() < 0.05).all()  # calibrated out of sample
