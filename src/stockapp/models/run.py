"""Orchestration for M6-M7: backtest both signals, apply the gate, train the final models and
score the latest week.

Outputs:
* gold ``oos_predictions`` (partition ``signal``): every walk-forward prediction, for the gate,
  the audit and later recalibration;
* gold ``signal_gate``: LIVE/OFF per signal with the evidence, read by the app;
* gold ``latest_scores``: the newest week's probabilities for every universe member;
* ``data/models/``: the fitted models with their metadata;
* ``data/reports/backtest_<date>.md``: the human-readable report (local only).
"""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from stockapp.config import AppConfig
from stockapp.features.build import FEATURE_COLUMNS
from stockapp.features.pipeline import FEATURE_VERSION, load_weekly_samples
from stockapp.ingest.registry import pipeline_version
from stockapp.lake import Lake
from stockapp.models.backtest import (
    Backtest,
    brier,
    by_year,
    calibration_table,
    run_backtest,
    summarize,
)
from stockapp.models.gate import GateResult, evaluate_gate, precision_curve
from stockapp.models.walkforward import (
    EMBARGO_DAYS,
    PARAMS,
    add_months,
    fit_calibrator,
    fit_classifier,
)

SIGNALS = {"A": "label_a", "C": "label_c"}
FIRST_TEST = date(2018, 1, 1)


def _md(df: pl.DataFrame) -> str:
    cols = df.columns
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += [
        "| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in df.iter_rows()
    ]
    return "\n".join(lines) + "\n"


@dataclass
class BacktestRun:
    backtests: dict[str, Backtest]
    gates: dict[str, GateResult]
    report_path: Path


def run_backtests(lake: Lake, cfg: AppConfig, today: date) -> BacktestRun:
    samples = load_weekly_samples(lake)
    gate_cfg = cfg.signals.gate
    backtests, gates, sections = {}, {}, []
    for signal, label in SIGNALS.items():
        bt = run_backtest(samples, FEATURE_COLUMNS, label, FIRST_TEST, with_quantile=signal == "A")
        backtests[signal] = bt
        cap = cfg.signals.max_opportunities if signal == "A" else None
        rank_by = "expected_gain" if signal == "A" else "p"
        gate = evaluate_gate(
            bt.predictions, signal, min_precision=cfg.signals.certainty_bar,
            min_signals=gate_cfg.min_signals, min_lower_bound=gate_cfg.min_wilson_lower_bound,
            max_per_week=cap, rank_by=rank_by,
        )  # fmt: skip
        gates[signal] = gate
        lake.write_partition(
            "gold", "oos_predictions", "signal", signal,
            bt.predictions.with_columns(pl.lit(FEATURE_VERSION).alias("feature_version")),
        )  # fmt: skip
        sections.append(
            f"## Signal {signal} ({label})\n\n"
            f"Out-of-sample predictions: {bt.predictions.height} across {len(bt.folds)} quarterly "
            f"folds from {FIRST_TEST}. Brier score {brier(bt):.4f}.\n\n"
            f"### Model vs baselines\n\n{_md(summarize(bt))}\n"
            f"### By year\n\n{_md(by_year(bt))}\n"
            f"### Calibration (stated vs observed)\n\n{_md(calibration_table(bt))}\n"
            f"### Precision at each cutoff (as shown: {cap or 'no'} per week cap)\n\n"
            f"{_md(precision_curve(bt.predictions, cap, rank_by))}\n"
            f"### Gate: **{gate.status}**\n\n{gate.reason}\n"
        )
    built = datetime.now(UTC)
    lake.write_partition(
        "gold", "signal_gate", "built", today.isoformat(),
        pl.DataFrame([g.as_row() for g in gates.values()]).with_columns(
            pl.lit(built).alias("evaluated_at"), pl.lit(FEATURE_VERSION).alias("feature_version"),
            pl.lit(pipeline_version()).alias("pipeline_version"),
        ),
    )  # fmt: skip
    reports = lake.root.parent / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"backtest_{today.isoformat()}.md"
    header = (
        f"# Walk-forward backtest ({today})\n\nFeature version {FEATURE_VERSION}, pipeline "
        f"{pipeline_version()}. Quarterly folds, expanding window, isotonic calibration on the 4 "
        f"quarters before each test quarter, {EMBARGO_DAYS}-day embargo between blocks. Every "
        "number is out of sample.\n\n"
        + "\n".join(f"- Signal {s}: **{g.status}**: {g.reason}" for s, g in gates.items())
        + "\n\n"
    )
    path.write_text(header + "\n".join(sections), encoding="utf-8")
    return BacktestRun(backtests, gates, path)


def train_and_score(lake: Lake, cfg: AppConfig, today: date) -> pl.DataFrame:
    """Final models on all labelled history (the last 4 quarters calibrate), then score the
    newest week. Signals stay subject to the gate: an OFF signal is scored but never shown."""
    import lightgbm as lgb

    samples = load_weekly_samples(lake)
    usable = samples.filter(~pl.col("blocked"))
    latest_day = samples["trade_date"].max()
    models_dir = lake.root.parent / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    version = f"{FEATURE_VERSION}-{today:%Y%m%d}"
    latest = samples.filter(pl.col("trade_date") == latest_day)
    x_latest = latest.select(pl.col(FEATURE_COLUMNS).cast(pl.Float32)).to_numpy()
    out = latest.select("company_id", "symbol", "trade_date", "blocked")

    for signal, label in SIGNALS.items():
        lab = usable.filter(pl.col(label).is_not_null())
        last = lab["trade_date"].max()
        calib_start = add_months(date(last.year, 3 * ((last.month - 1) // 3) + 1, 1), -9)
        train = lab.filter(pl.col("trade_date") < calib_start - timedelta(days=EMBARGO_DAYS))
        calib = lab.filter(pl.col("trade_date") >= calib_start)
        model = fit_classifier(train, FEATURE_COLUMNS, label)
        iso = fit_calibrator(model, calib, FEATURE_COLUMNS, label)
        out = out.with_columns(
            pl.Series(f"p_{signal.lower()}", iso.predict(model.predict_proba(x_latest)[:, 1]))
        )
        meta = {
            "signal": signal,
            "label": label,
            "version": version,
            "feature_version": FEATURE_VERSION,
            "train_end": str(train["trade_date"].max()),
            "calib": [str(calib_start), str(last)],
            "params": PARAMS,
            "trained_at": datetime.now(UTC).isoformat(),
            "pipeline_version": pipeline_version(),
        }
        with (models_dir / f"signal_{signal}_{version}.pkl").open("wb") as f:
            pickle.dump(
                {"model": model, "calibrator": iso, "features": FEATURE_COLUMNS, "meta": meta}, f
            )
        (models_dir / f"signal_{signal}_{version}.json").write_text(json.dumps(meta, indent=2))

    gain = usable.filter(pl.col("max_gain_5").is_not_null())
    q = lgb.LGBMRegressor(**PARAMS, objective="quantile", alpha=0.5)
    q.fit(
        gain.select(pl.col(FEATURE_COLUMNS).cast(pl.Float32)).to_numpy(),
        gain["max_gain_5"].to_numpy(),
    )
    out = out.with_columns(
        pl.Series("expected_gain", q.predict(x_latest)), pl.lit(version).alias("model_version")
    )
    lake.write_partition("gold", "latest_scores", "trade_date", latest_day.isoformat(), out)
    return out


def stored_predictions(lake: Lake, signal: str) -> pl.DataFrame:
    # Read only this signal's partition: A and C store different columns (C has no gain model).
    part = lake.table_dir("gold", "oos_predictions") / f"signal={signal}"
    files = sorted(part.glob("*.parquet")) if part.is_dir() else []
    if not files:
        return pl.DataFrame()
    return pl.read_parquet(files, hive_partitioning=False).drop("signal", strict=False)


def reevaluate_gate(lake: Lake, cfg: AppConfig, today: date) -> dict[str, GateResult]:
    """Re-check LIVE/OFF with the current settings on the stored out-of-sample predictions (no
    retraining): used when the certainty bar or gate rules change in Settings."""
    gate_cfg, gates = cfg.signals.gate, {}
    for signal in SIGNALS:
        preds = stored_predictions(lake, signal)
        if preds.is_empty():
            continue
        cap = cfg.signals.max_opportunities if signal == "A" else None
        gates[signal] = evaluate_gate(
            preds,
            signal,
            min_precision=cfg.signals.certainty_bar,
            min_signals=gate_cfg.min_signals,
            min_lower_bound=gate_cfg.min_wilson_lower_bound,
            max_per_week=cap,
            rank_by="expected_gain" if signal == "A" else "p",
        )
    if gates:
        lake.write_partition(
            "gold",
            "signal_gate",
            "built",
            today.isoformat(),
            pl.DataFrame([g.as_row() for g in gates.values()]).with_columns(
                pl.lit(datetime.now(UTC)).alias("evaluated_at"),
                pl.lit(FEATURE_VERSION).alias("feature_version"),
                pl.lit(pipeline_version()).alias("pipeline_version"),
            ),
        )
    return gates


def precision_at_bar(lake: Lake, cfg: AppConfig, signal: str, bar: float) -> dict | None:
    """What the validated history says about signals at ``bar`` (shown live in Settings)."""
    preds = stored_predictions(lake, signal)
    if preds.is_empty():
        return None
    cap = cfg.signals.max_opportunities if signal == "A" else None
    curve = precision_curve(preds, cap, "expected_gain" if signal == "A" else "p", cutoffs=(bar,))
    return curve.row(0, named=True)
