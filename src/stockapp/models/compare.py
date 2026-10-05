"""Compare the group of models with each member on the same walk-forward test (2018 on).

One walk-forward run trains the ensemble per quarterly fold; every member's own calibrated
predictions come out of the same run, so all scores are judged on identical, out-of-sample
weeks. Nothing live changes: no gold tables are written, only a local report. Switching the app
to the group is a separate, deliberate step (Settings, then retrain).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import polars as pl
from sklearn.metrics import roc_auc_score

from stockapp.config import AppConfig
from stockapp.features.build import FEATURE_COLUMNS, WINDOW, compute_labels, label_span_days
from stockapp.features.pipeline import blocked_samples, load_panel, load_weekly_samples
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


def horizon_samples(lake: Lake, gain: float, crash: float, window: int) -> pl.DataFrame:
    """The saved weekly samples with labels rebuilt for another question (e.g. +5% within 20
    sessions), in memory only. Same features and universe; ``blocked`` covers the longer window
    so no label rests on prices with a BLOCK quality flag."""
    base = load_weekly_samples(lake).drop("label_a", "label_c", "max_gain_5", "blocked")
    labels = compute_labels(load_panel(lake), gain, crash, window)
    s = base.join(labels, on=["company_id", "segment", "trade_date"], how="left")
    blocked = blocked_samples(lake, s, label_span_days(window))
    return s.join(blocked, on=["company_id", "trade_date"], how="left").with_columns(
        pl.col("blocked").fill_null(False)
    )


def compare_models(
    lake: Lake,
    cfg: AppConfig,
    today: date,
    gain: float | None = None,
    crash: float | None = None,
    window: int | None = None,
) -> Comparison:
    """Without arguments: the app's own question (signals as configured). With ``gain``/
    ``crash``/``window``: the same test on another question, labels rebuilt in memory."""
    custom = any(v is not None for v in (gain, crash, window))
    gain = cfg.signals.gain_threshold if gain is None else gain
    crash = gain if custom and crash is None else (crash or cfg.signals.crash_threshold)
    window = cfg.signals.window_trading_days if window is None else window
    samples = horizon_samples(lake, gain, crash, window) if custom else load_weekly_samples(lake)
    embargo = label_span_days(window)
    tables = []
    for signal, label in SIGNALS.items():
        a = signal == "A"
        bt = run_backtest(
            samples, FEATURE_COLUMNS, label, FIRST_TEST, a, kind="ensemble", embargo_days=embargo,
            on_fold=lambda i, n, f, s=signal: print(
                f"[{datetime.now():%H:%M}] signal {s}: quarter {i}/{n} ({f.test_start})", flush=True
            ),
        )  # fmt: skip
        base_rate = float(bt.predictions["label"].cast(pl.Float64).mean())
        tables.append(
            score_table(bt.predictions, signal, cfg).with_columns(
                pl.lit(round(base_rate, 4)).alias("base_rate")
            )
        )
    table = pl.concat(tables)
    reports = lake.root.parent / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    tag = f"_{gain:.0%}_{window}d".replace("%", "pct") if custom else ""
    path = reports / f"compare_models_{today.isoformat()}{tag}.md"
    question = (
        f"Buy (A): +{gain:.0%} within {window} market days. Drop (C): -{crash:.0%} within "
        f"{window} market days. Base rates: "
        + ", ".join(
            f"{r['signal']} {r['base_rate']:.1%}"
            for r in table.unique("signal").iter_rows(named=True)
        )
        + "."
    )
    overlap = (
        f" Weekly samples with a {window}-day window overlap, so neighbouring signals on the "
        "same stock are not independent: treat the signal counts as optimistic."
        if window > WINDOW
        else ""
    )
    lines = [
        f"# Group of models vs each model ({today})\n",
        question + overlap + "\n",
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
