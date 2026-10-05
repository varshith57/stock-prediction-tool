"""Compare the group of models with each member on the same walk-forward test (2018 on).

One walk-forward run trains the ensemble per quarterly fold; every member's own calibrated
predictions come out of the same run, so all scores are judged on identical, out-of-sample
weeks. Nothing live changes: no gold tables are written, only a local report. Switching the app
to the group is a separate, deliberate step (Settings, then retrain).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl
from sklearn.metrics import roc_auc_score

from stockapp.config import AppConfig
from stockapp.features.build import FEATURE_COLUMNS
from stockapp.features.pipeline import load_weekly_samples
from stockapp.lake import Lake
from stockapp.models.backtest import _top_k_precision, run_backtest
from stockapp.models.ensemble import MEMBERS
from stockapp.models.gate import evaluate_gate

SIGNALS = {"A": "label_a", "C": "label_c"}
FIRST_TEST = date(2018, 1, 1)
NAMES = {"p": "group (all 5)", **{f"m_{m}": m for m in MEMBERS}}


@dataclass
class Comparison:
    table: pl.DataFrame  # signal, model, auc, top5_precision, best_precision, signals, gate
    report_path: Path


def score_table(preds: pl.DataFrame, signal: str, cfg: AppConfig) -> pl.DataFrame:
    """One row per score column: ranking quality, top-5 precision and the 90% gate."""
    df = preds.filter(pl.col("label").is_not_null())
    y = df["label"].cast(pl.Int8).to_numpy()
    cap = cfg.signals.max_opportunities if signal == "A" else None
    rank_by = "expected_gain" if signal == "A" and "expected_gain" in df.columns else "p"
    rows = []
    for col, name in NAMES.items():
        if col not in df.columns:
            continue
        as_p = df.with_columns(pl.col(col).alias("p"))
        gate = evaluate_gate(
            as_p,
            signal,
            min_precision=cfg.signals.certainty_bar,
            min_signals=cfg.signals.gate.min_signals,
            min_lower_bound=cfg.signals.gate.min_wilson_lower_bound,
            max_per_week=cap,
            rank_by=rank_by,
        )
        rows.append(
            {
                "signal": signal,
                "model": name,
                "auc": round(float(roc_auc_score(y, as_p["p"].to_numpy())), 4),
                "top5_precision": round(_top_k_precision(as_p, "p"), 4),
                "best_precision": gate.best_precision if gate.status == "OFF" else gate.precision,
                "signals": gate.best_precision_signals if gate.status == "OFF" else gate.signals,
                "gate": gate.status,
            }
        )
    return pl.DataFrame(rows)


def compare_models(lake: Lake, cfg: AppConfig, today: date) -> Comparison:
    samples = load_weekly_samples(lake)
    tables = []
    for signal, label in SIGNALS.items():
        a = signal == "A"
        bt = run_backtest(samples, FEATURE_COLUMNS, label, FIRST_TEST, a, kind="ensemble")
        tables.append(score_table(bt.predictions, signal, cfg))
    table = pl.concat(tables)
    reports = lake.root.parent / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"compare_models_{today.isoformat()}.md"
    lines = [
        f"# Group of models vs each model ({today})\n",
        "Walk-forward, quarterly folds from 2018, every number out of sample. AUC: ranking "
        "quality (0.5 = coin flip). Top-5 precision: share of each week's 5 best-scored stocks "
        f"that hit. Best precision: the best accuracy any cutoff reached with "
        f"{cfg.signals.gate.min_signals}+ signals. Gate: {cfg.signals.certainty_bar:.0%} bar.\n",
        "| signal | model | auc | top5_precision | best_precision | signals | gate |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in table.iter_rows(named=True):
        best = "" if r["best_precision"] is None else f"{r['best_precision']:.1%}"
        lines.append(
            f"| {r['signal']} | {r['model']} | {r['auc']:.3f} | {r['top5_precision']:.1%} | "
            f"{best} | {r['signals']} | {r['gate']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Comparison(table, path)
