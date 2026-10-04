"""M5 gate: labels are right, features are point-in-time, and no leakage shows up.

* Hand-built series check labels A and C, including breaks, halts and the end of data.
* Truncation invariance: features at T are identical whether or not a split *after* T is known.
* Random walk: with no real signal, a model on the features has no edge (AUC about 0.5), shuffled
  labels too; and a deliberately leaky feature is caught.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from stockapp.features.build import (
    CROSS_SECTION,
    FEATURE_COLUMNS,
    add_cross_section,
    compute_features,
    compute_labels,
    market_features,
)
from stockapp.features.checks import holdout_auc, shuffled_label_auc


def _sessions(n: int, start: date = date(2018, 1, 1)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _panel(
    closes: list[float],
    opens: list[float] | None = None,
    company: str = "AAA",
    days: list[date] | None = None,
    segment: list[int] | None = None,
) -> pl.DataFrame:
    closes = [float(x) for x in closes]
    opens = [float(x) for x in (opens or closes)]
    n = len(closes)
    days = days or _sessions(n)
    return pl.DataFrame(
        {
            "company_id": [company] * n,
            "segment": segment or [0] * n,
            "trade_date": days,
            "series": ["EQ"] * n,
            "close": closes,
            "adj_open": opens,
            "adj_high": [max(o, c) * 1.01 for o, c in zip(opens, closes, strict=True)],
            "adj_low": [min(o, c) * 0.99 for o, c in zip(opens, closes, strict=True)],
            "adj_close": closes,
            "value_inr": [1e7] * n,
            "delivery_pct": [50.0] * n,
        }
    )


# labels ------------------------------------------------------------------------------------------


def _labels(panel: pl.DataFrame) -> list[tuple]:
    return compute_labels(panel).sort("trade_date").select("label_a", "label_c").rows()


def test_label_a_uses_next_open_not_todays_close():
    # T close 100; T+1 opens at 105 (gap up); best close in the window 113 -> +7.6% from the open
    closes = [100, 106, 110, 113, 108, 107, 100, 100, 100, 100, 100]
    opens = [100, 105, 106, 110, 113, 108, 107, 100, 100, 100, 100]
    labels = _labels(_panel(closes, opens))
    assert labels[0] == (False, False)  # +13% from the close, but only +7.6% from the open
    # a 10% move from the next open does count
    closes2 = [100, 100, 104, 111, 100, 100, 100, 100, 100, 100, 100]
    assert _labels(_panel(closes2))[0] == (True, False)


def test_label_c_from_signal_close():
    closes = [100, 99, 95, 89.9, 92, 93, 100, 100, 100, 100, 100]
    assert _labels(_panel(closes))[0] == (False, True)


def test_labels_null_at_end_of_data_across_breaks_and_halts():
    closes = [100.0] * 12
    labels = _labels(_panel(closes))
    assert labels[-5:] == [(None, None)] * 5  # not enough future sessions
    seg = [0] * 6 + [1] * 6  # a demerger between session 5 and 6
    broken = compute_labels(_panel(closes, segment=seg)).sort("trade_date")
    assert broken.filter(pl.col("segment") == 0)["label_a"].null_count() == 5
    days = _sessions(12)
    days = days[:3] + [d + timedelta(days=30) for d in days[3:]]  # a month-long halt
    assert _labels(_panel(closes, days=days))[0] == (None, None)


# point in time -----------------------------------------------------------------------------------


def _random_walk(n_days: int, n_companies: int, seed: int) -> tuple[pl.DataFrame, pl.DataFrame]:
    rng = np.random.default_rng(seed)
    days = _sessions(n_days)
    mkt = 10000 * np.exp(np.cumsum(rng.normal(0, 0.01, n_days)))
    frames = []
    for i in range(n_companies):
        beta = rng.uniform(0.5, 1.5)
        lr = beta * np.diff(np.log(mkt), prepend=np.log(mkt[0])) + rng.normal(0, 0.025, n_days)
        close = 100 * np.exp(np.cumsum(lr))
        opn = close * np.exp(rng.normal(0, 0.005, n_days))
        frames.append(
            _panel(list(close), list(opn), company=f"C{i:03d}", days=days).with_columns(
                pl.Series("value_inr", rng.lognormal(16, 1, n_days)),
                pl.Series("delivery_pct", rng.uniform(20, 80, n_days)),
            )
        )
    index = pl.DataFrame(
        {"trade_date": days, "nifty500": mkt, "vix": 15 + rng.normal(0, 1, n_days)}
    )
    return pl.concat(frames), market_features(index)


def test_features_at_t_ignore_a_split_after_t():
    panel, market = _random_walk(400, 1, seed=1)
    t_idx, split_idx = 300, 350
    days = panel["trade_date"].to_list()
    raw = panel["adj_close"].to_numpy()
    raw_after = np.where(np.arange(400) >= split_idx, raw / 5, raw)  # 1:5 split after T
    as_traded = panel.with_columns(pl.Series("close", raw_after))
    # full history: backward-adjusted, so everything before the split is scaled by 1/5
    scale = np.where(np.arange(400) < split_idx, 0.2, 1.0)
    full = as_traded.with_columns(
        *[(pl.col(c) * pl.Series(scale)).alias(c) for c in ("adj_open", "adj_high", "adj_low")],
        pl.Series("adj_close", raw_after * scale),
    )
    # truncated at T: the split isn't known yet, so adjusted = raw up to T
    trunc = as_traded.filter(pl.col("trade_date") <= days[t_idx])
    a = compute_features(full, market).filter(pl.col("trade_date") == days[t_idx])
    b = compute_features(trunc, market).filter(pl.col("trade_date") == days[t_idx])
    for col in [c for c in FEATURE_COLUMNS if c not in CROSS_SECTION]:
        x, y = a[col][0], b[col][0]
        assert (x is None and y is None) or x == pytest.approx(y, rel=1e-9, abs=1e-12), col


def test_features_never_look_past_t():
    panel, market = _random_walk(300, 2, seed=2)
    days = panel["trade_date"].unique().sort().to_list()
    t = days[200]
    full = compute_features(panel, market).filter(pl.col("trade_date") == t).sort("company_id")
    cut = (
        compute_features(panel.filter(pl.col("trade_date") <= t), market)
        .filter(pl.col("trade_date") == t)
        .sort("company_id")
    )
    for col in [c for c in FEATURE_COLUMNS if c not in CROSS_SECTION and c != "breadth_50"]:
        np.testing.assert_allclose(
            full[col].fill_null(np.nan).to_numpy(), cut[col].fill_null(np.nan).to_numpy(),
            rtol=1e-9, err_msg=col,
        )  # fmt: skip


# no edge on a random walk -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rw_samples() -> pl.DataFrame:
    panel, market = _random_walk(900, 60, seed=3)
    feats = compute_features(panel, market)
    labels = compute_labels(panel)
    weekly = feats.filter(pl.col("trade_date").dt.weekday() == 5)
    s = weekly.join(labels, on=["company_id", "segment", "trade_date"])
    return add_cross_section(s)


def _split(s: pl.DataFrame) -> date:
    days = s["trade_date"].unique().sort()
    return days[int(len(days) * 0.6)]


@pytest.mark.parametrize("label", ["label_a", "label_c"])
def test_no_edge_on_a_random_walk(rw_samples: pl.DataFrame, label: str):
    assert rw_samples[label].mean() > 0.02  # enough events to measure
    auc = holdout_auc(rw_samples, FEATURE_COLUMNS, label, _split(rw_samples))
    assert 0.42 < auc < 0.58, auc
    shuffled = shuffled_label_auc(rw_samples, FEATURE_COLUMNS, label, _split(rw_samples))
    assert 0.42 < shuffled < 0.58, shuffled


def test_a_leaky_feature_is_caught(rw_samples: pl.DataFrame):
    leaky = rw_samples.with_columns(pl.col("label_a").cast(pl.Float64).alias("leak"))
    leaky = leaky.with_columns(
        pl.col("leak") + pl.Series(np.random.default_rng(0).normal(0, 0.3, leaky.height))
    )
    auc = holdout_auc(leaky, [*FEATURE_COLUMNS, "leak"], "label_a", _split(leaky))
    assert auc > 0.8
