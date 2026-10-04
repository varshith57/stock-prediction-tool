"""Walk-forward backtest for signals A and C: model vs baselines, all out of sample (M6).

Baselines (PRD: "if the model can't beat these, nothing ships"):
* ``base_rate``: no information (every stock equally likely). Its precision is the base rate.
* ``volatility``: rank of 20-session volatility. The obvious predictor of a +/-10% week.
* ``momentum``: rank of 20-session return (A: strongest first; C: weakest first).
* ``logistic``: a linear model on the same features, refit per fold.

Metrics, per signal, over every out-of-sample week:
* AUC and average precision (ranking quality);
* top-5 precision: share of hits among each week's 5 highest-scored stocks, which is what the app
  could show at most;
* calibration: stated probability vs observed rate, by band (model only).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import polars as pl
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from stockapp.models.walkforward import (
    FoldResult,
    make_folds,
    run_fold,
    run_quantile_fold,
)

SCORES = ("p", "volatility", "momentum", "logistic")


def _logistic_scores(samples: pl.DataFrame, fold, features: list[str], label: str) -> np.ndarray:
    s = samples.filter(pl.col(label).is_not_null())
    train = s.filter(pl.col("trade_date") < fold.calib_end)
    test = s.filter(
        (pl.col("trade_date") >= fold.test_start) & (pl.col("trade_date") < fold.test_end)
    )
    x = lambda d: d.select(pl.col(features).cast(pl.Float64).fill_null(0).fill_nan(0)).to_numpy()  # noqa: E731
    m = make_pipeline(StandardScaler(), LogisticRegression(max_iter=500, class_weight="balanced"))
    m.fit(x(train), train[label].cast(pl.Int8).to_numpy())
    return m.predict_proba(x(test))[:, 1]


@dataclass
class Backtest:
    label: str
    predictions: pl.DataFrame
    folds: list[FoldResult]


def run_backtest(samples: pl.DataFrame, features: list[str], label: str, first_test: date,
                 with_quantile: bool = False) -> Backtest:  # fmt: skip
    usable = samples.filter(~pl.col("blocked"))
    last = usable.filter(pl.col(label).is_not_null())["trade_date"].max()
    results, frames = [], []
    for fold in make_folds(first_test, last):
        r = run_fold(usable, fold, features, label)
        if r is None:
            continue
        test = usable.filter(
            pl.col(label).is_not_null()
            & (pl.col("trade_date") >= fold.test_start)
            & (pl.col("trade_date") < fold.test_end)
        )
        preds = r.predictions.with_columns(
            test["rank_vol_20"].alias("volatility"),
            (test["rank_ret_20"] if label == "label_a" else 1 - test["rank_ret_20"]).alias(
                "momentum"
            ),
            pl.Series("logistic", _logistic_scores(usable, fold, features, label)),
            pl.lit(fold.test_start).alias("quarter"),
        )
        if with_quantile:
            q = run_quantile_fold(usable, fold, features)
            if q is not None:
                preds = preds.join(q, on=["company_id", "trade_date"], how="left")
        results.append(r)
        frames.append(preds)
    return Backtest(label, pl.concat(frames, how="diagonal"), results)


def _top_k_precision(df: pl.DataFrame, score: str, k: int = 5) -> float:
    top = (
        df.filter(pl.col(score).is_not_null())
        .with_columns(pl.col(score).rank("ordinal", descending=True).over("trade_date").alias("_r"))
        .filter(pl.col("_r") <= k)
    )
    return float(top["label"].mean())


def summarize(bt: Backtest) -> pl.DataFrame:
    df = bt.predictions.filter(pl.col("label").is_not_null())
    y = df["label"].cast(pl.Int8).to_numpy()
    rows = [{"score": "base_rate", "auc": 0.5, "avg_precision": float(y.mean()),
             "top5_precision": float(y.mean())}]  # fmt: skip
    for s in SCORES:
        sc = df[s].fill_null(0).to_numpy()
        rows.append(
            {"score": "model" if s == "p" else s, "auc": roc_auc_score(y, sc),
             "avg_precision": average_precision_score(y, sc),
             "top5_precision": _top_k_precision(df, s)}
        )  # fmt: skip
    return pl.DataFrame(rows).with_columns(pl.col(pl.Float64).round(4))


def by_year(bt: Backtest) -> pl.DataFrame:
    df = bt.predictions.filter(pl.col("label").is_not_null()).with_columns(
        pl.col("trade_date").dt.year().alias("year")
    )
    out = []
    for (year,), g in df.partition_by("year", as_dict=True).items():
        y = g["label"].cast(pl.Int8).to_numpy()
        if y.min() == y.max():
            continue
        out.append(
            {"year": year, "samples": g.height, "base_rate": float(y.mean()),
             "model_auc": roc_auc_score(y, g["p"].to_numpy()),
             "vol_auc": roc_auc_score(y, g["volatility"].fill_null(0).to_numpy()),
             "model_top5": _top_k_precision(g, "p"), "vol_top5": _top_k_precision(g, "volatility")}
        )  # fmt: skip
    return pl.DataFrame(out).sort("year").with_columns(pl.col(pl.Float64).round(4))


def calibration_table(bt: Backtest) -> pl.DataFrame:
    df = bt.predictions.filter(pl.col("label").is_not_null())
    bands = [0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.0001]
    return (
        df.with_columns(pl.col("p").cut(bands[1:-1], left_closed=True).alias("band"))
        .group_by("band")
        .agg(
            pl.len().alias("n"),
            pl.col("p").mean().round(4).alias("stated"),
            pl.col("label").cast(pl.Float64).mean().round(4).alias("observed"),
        )
        .sort("band")
    )


def brier(bt: Backtest) -> float:
    df = bt.predictions.filter(pl.col("label").is_not_null())
    return float(brier_score_loss(df["label"].cast(pl.Int8).to_numpy(), df["p"].to_numpy()))
