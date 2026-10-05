"""Group of models: each member learns, the average is a probability, folds carry every member's
own score, and the comparison table judges all of them on the same predictions."""

from __future__ import annotations

import pickle
from datetime import date, timedelta

import numpy as np
import polars as pl
from sklearn.metrics import roc_auc_score

from stockapp.config import load_app_config
from stockapp.models.compare import score_table
from stockapp.models.ensemble import MEMBERS, fit_ensemble
from stockapp.models.walkforward import make_folds, run_fold

FEATURES = ["f1", "f2", "f3"]


def _samples(n_weeks: int = 160, per_week: int = 60, seed: int = 0) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    start = date(2016, 1, 1)
    for w in range(n_weeks):
        d = start + timedelta(weeks=w)
        f = rng.normal(size=(per_week, 3))
        f[rng.random((per_week, 3)) < 0.05] = np.nan  # some missing values, like real features
        logit = -2.5 + 1.5 * np.nan_to_num(f[:, 0]) - 0.8 * np.nan_to_num(f[:, 1])
        y = rng.random(per_week) < 1 / (1 + np.exp(-logit))
        for i in range(per_week):
            rows.append(
                {
                    "company_id": f"C{i}",
                    "symbol": f"S{i}",
                    "trade_date": d,
                    "label_a": bool(y[i]),
                    "f1": f[i, 0],
                    "f2": f[i, 1],
                    "f3": f[i, 2],
                }
            )
    return pl.DataFrame(rows)


def test_ensemble_learns_and_round_trips():
    s = _samples()
    train = s.filter(pl.col("trade_date") < date(2018, 1, 1))
    calib = s.filter(pl.col("trade_date").is_between(date(2018, 1, 1), date(2018, 6, 30)))
    test = s.filter(pl.col("trade_date") > date(2018, 7, 10))
    ens = fit_ensemble(train, calib, FEATURES, "label_a")
    x = test.select(pl.col(FEATURES).cast(pl.Float32)).to_numpy()
    y = test["label_a"].cast(pl.Int8).to_numpy()
    members = ens.member_probabilities(x)
    assert set(members) == set(MEMBERS)
    for name, p in members.items():
        assert ((p >= 0) & (p <= 1)).all(), name
        assert roc_auc_score(y, p) > 0.7, name  # every member finds the planted pattern
    p = ens.predict_proba(x)
    assert p.shape == (len(y), 2) and np.allclose(p.sum(axis=1), 1)
    assert roc_auc_score(y, p[:, 1]) > 0.75
    assert ens.booster_ is not None  # the "why" lines still come from LightGBM
    again = pickle.loads(pickle.dumps(ens))
    assert np.allclose(again.predict_proba(x), p)


def test_fold_carries_member_scores_and_compare_table():
    s = _samples()
    fold = make_folds(date(2018, 7, 1), date(2018, 9, 30))[0]
    r = run_fold(s, fold, FEATURES, "label_a", kind="ensemble")
    assert r is not None
    cols = r.predictions.columns
    assert "p" in cols and all(f"m_{m}" in cols for m in MEMBERS)
    single = run_fold(s, fold, FEATURES, "label_a")
    assert single is not None and "m_lightgbm" not in single.predictions.columns
    table = score_table(r.predictions, "C", load_app_config(local_path=None))
    assert table.height == len(MEMBERS) + 1 and table["model"][0] == "group (all 5)"
    assert set(table["gate"]) <= {"LIVE", "OFF"}


def test_longer_windows_get_a_longer_label_span_and_embargo():
    from stockapp.features.build import compute_labels, label_span_days

    assert label_span_days(5) == 14  # today's one-week question is unchanged
    assert label_span_days(20) == 56
    f = make_folds(date(2018, 1, 1), date(2018, 3, 31), embargo_days=56)[0]
    assert (f.test_start - f.calib_end).days == 56 and (f.calib_start - f.train_end).days == 56

    days = pl.date_range(date(2020, 1, 1), date(2020, 3, 31), eager=True)
    days = [d for d in days if d.weekday() < 5]
    price = [100 * 1.004**i for i in range(len(days))]  # +0.4% a day
    panel = pl.DataFrame(
        {
            "company_id": "X",
            "segment": 0,
            "trade_date": days,
            "adj_open": price,
            "adj_close": price,
        }
    )
    week = compute_labels(panel, 0.05, 0.05, 5)
    month = compute_labels(panel, 0.05, 0.05, 20)
    assert not week["label_a"][0]  # +5% needs about 12 sessions: not within a week
    assert month["label_a"][0]  # but within a month
    assert month["label_a"].null_count() == 20  # the last 20 days have no full window yet
