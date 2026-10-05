"""Walk-forward training and out-of-sample predictions (M6).

For each test quarter Q (from ``first_test``):

* train on samples whose label window ended before Q's calibration block (expanding window);
* calibrate (isotonic) on the ``calib_quarters`` just before Q;
* predict Q.

Between every block there is an embargo of ``EMBARGO_DAYS`` calendar days: a sample at T has a
label that depends on prices up to ~T+7 calendar days (5 sessions plus a weekend), so a training
row within that distance of the next block could see prices from inside it (purged CV).

Every prediction is out of sample. These predictions, not training fits, feed the accuracy gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import lightgbm as lgb
import numpy as np
import polars as pl
from sklearn.isotonic import IsotonicRegression

EMBARGO_DAYS = 10
PARAMS = {
    "n_estimators": 300,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 200,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "verbose": -1,
    "n_jobs": 4,
}


def quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    return date(d.year + m // 12, m % 12 + 1, 1)


@dataclass(frozen=True)
class Fold:
    test_start: date
    test_end: date  # exclusive
    calib_start: date
    train_end: date  # exclusive (embargo applied)
    calib_end: date  # exclusive (embargo applied)


def make_folds(first_test: date, last_date: date, calib_quarters: int = 4,
               embargo_days: int = EMBARGO_DAYS) -> list[Fold]:  # fmt: skip
    folds, q = [], quarter_start(first_test)
    while q <= last_date:
        calib_start = add_months(q, -3 * calib_quarters)
        folds.append(
            Fold(
                test_start=q, test_end=add_months(q, 3), calib_start=calib_start,
                train_end=calib_start - timedelta(days=embargo_days),
                calib_end=q - timedelta(days=embargo_days),
            )
        )  # fmt: skip
        q = add_months(q, 3)
    return folds


def _matrix(df: pl.DataFrame, features: list[str]) -> np.ndarray:
    return df.select(pl.col(features).cast(pl.Float32)).to_numpy()


@dataclass
class FoldResult:
    fold: Fold
    predictions: pl.DataFrame  # company_id, trade_date, label, p_raw, p
    n_train: int
    n_calib: int
    importances: dict[str, float] = field(default_factory=dict)


def fit_classifier(train: pl.DataFrame, features: list[str], label: str) -> lgb.LGBMClassifier:
    y = train[label].cast(pl.Int8).to_numpy()
    pos = max(int(y.sum()), 1)
    model = lgb.LGBMClassifier(**PARAMS, scale_pos_weight=(len(y) - pos) / pos)
    model.fit(_matrix(train, features), y)
    return model


def fit_calibrator(model: lgb.LGBMClassifier, calib: pl.DataFrame, features: list[str],
                   label: str) -> IsotonicRegression:  # fmt: skip
    raw = model.predict_proba(_matrix(calib, features))[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(raw, calib[label].cast(pl.Int8).to_numpy())
    return iso


def fit_model(train: pl.DataFrame, calib: pl.DataFrame, features: list[str], label: str,
              kind: str = "lightgbm"):  # fmt: skip
    """The signal model: LightGBM alone, or the group of models (``models.ensemble``)."""
    if kind == "ensemble":
        from stockapp.models.ensemble import fit_ensemble

        return fit_ensemble(train, calib, features, label)
    return fit_classifier(train, features, label)


def run_fold(
    samples: pl.DataFrame, fold: Fold, features: list[str], label: str, kind: str = "lightgbm"
) -> FoldResult | None:
    s = samples.filter(pl.col(label).is_not_null())
    train = s.filter(pl.col("trade_date") < fold.train_end)
    calib = s.filter(
        (pl.col("trade_date") >= fold.calib_start) & (pl.col("trade_date") < fold.calib_end)
    )
    test = s.filter(
        (pl.col("trade_date") >= fold.test_start) & (pl.col("trade_date") < fold.test_end)
    )
    if min(train.height, calib.height, test.height) == 0 or train[label].sum() < 50:
        return None
    model = fit_model(train, calib, features, label, kind)
    iso = fit_calibrator(model, calib, features, label)
    x_test = _matrix(test, features)
    raw = model.predict_proba(x_test)[:, 1]
    preds = test.select(
        "company_id", "symbol", "trade_date", pl.col(label).alias("label")
    ).with_columns(pl.Series("p_raw", raw), pl.Series("p", iso.predict(raw)))
    if kind == "ensemble":  # each member's own calibrated probability, for the comparison report
        preds = preds.with_columns(
            pl.Series(f"m_{name}", p) for name, p in model.member_probabilities(x_test).items()
        )
    imp = dict(zip(features, model.booster_.feature_importance("gain").tolist(), strict=True))
    return FoldResult(fold, preds, train.height, calib.height, imp)


def run_quantile_fold(samples: pl.DataFrame, fold: Fold, features: list[str],
                      alpha: float = 0.5) -> pl.DataFrame | None:  # fmt: skip
    """Expected max gain over the window (amendment AM6), used only to rank signal A."""
    s = samples.filter(pl.col("max_gain_5").is_not_null())
    train = s.filter(pl.col("trade_date") < fold.calib_end)
    test = s.filter(
        (pl.col("trade_date") >= fold.test_start) & (pl.col("trade_date") < fold.test_end)
    )
    if train.height == 0 or test.height == 0:
        return None
    model = lgb.LGBMRegressor(**PARAMS, objective="quantile", alpha=alpha)
    model.fit(_matrix(train, features), train["max_gain_5"].to_numpy())
    return test.select("company_id", "trade_date").with_columns(
        pl.Series("expected_gain", model.predict(_matrix(test, features)))
    )
