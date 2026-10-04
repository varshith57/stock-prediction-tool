"""Leakage checks used by CI and by the model gate.

* ``holdout_auc``: fit a plain logistic regression on samples before ``split_date`` and score
  after it. On data with no real signal (a random walk) any AUC well above 0.5 means leakage.
* ``shuffled_label_auc``: same, but labels are shuffled across the whole sample first, which
  destroys any real relationship. It must come out about 0.5; if not, something other than the
  label's content (row order, dates, leakage) is being learned. Shuffling only *within* each date
  is not a null test: it keeps each week's event count, and market features legitimately predict
  which weeks are eventful (volatility clusters).
"""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


def _xy(df: pl.DataFrame, features: list[str], label: str) -> tuple[np.ndarray, np.ndarray]:
    x = df.select(pl.col(features).cast(pl.Float64).fill_null(0.0).fill_nan(0.0)).to_numpy()
    return x, df[label].cast(pl.Int8).to_numpy()


def holdout_auc(samples: pl.DataFrame, features: list[str], label: str, split_date: date) -> float:
    data = samples.filter(pl.col(label).is_not_null())
    train = data.filter(pl.col("trade_date") < split_date)
    test = data.filter(pl.col("trade_date") >= split_date)
    xtr, ytr = _xy(train, features, label)
    xte, yte = _xy(test, features, label)
    if len(set(ytr)) < 2 or len(set(yte)) < 2:
        raise ValueError("need both classes in train and test")
    model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))
    model.fit(xtr, ytr)
    return float(roc_auc_score(yte, model.predict_proba(xte)[:, 1]))


def shuffled_label_auc(
    samples: pl.DataFrame, features: list[str], label: str, split_date: date, seed: int = 7
) -> float:
    shuffled = samples.filter(pl.col(label).is_not_null()).with_columns(
        pl.col(label).shuffle(seed=seed)
    )
    return holdout_auc(shuffled, features, label, split_date)
