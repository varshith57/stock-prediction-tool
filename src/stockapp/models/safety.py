"""Safety net: monthly drop warnings for stocks you already own.

The question: will a stock close at least ``safety.drop`` below the signal-day close within
``safety.window`` market days (default -5% within 20 days, about a month)? It is used only to cut
risk in holdings, never to pick new stocks. Monthly horizons were where the models came closest
to proven (``models compare --gain 0.05 --window 20``).

Testing is stricter than for weekly signals, because weekly samples with month-long windows
overlap: a stock flagged four weeks running is one warning, not four. The test counts at most one
warning per stock per ``dedupe_days`` (``dedupe``), then a cutoff passes when deduplicated warnings
were right at least ``bar`` of the time, on ``min_signals`` or more, with a 95% lower bound of at
least ``min_lower_bound``, and right at least ``bar - 0.1`` in most years that had warnings.

Monthly (``build_safety``): labels in memory, walk-forward backtest (the group of 5 models by
default), the test, final models saved under data/models/safety_*.pkl, the newest week scored.
Weekly (``score_safety``): the saved model scores the newest week (seconds). Gold tables:
``safety_gate`` (verdict) and ``safety_scores`` (p_drop per stock per week).
"""

from __future__ import annotations

import json
import pickle
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from stockapp.config import AppConfig
from stockapp.features.build import FEATURE_COLUMNS, label_span_days
from stockapp.features.pipeline import FEATURE_VERSION, load_weekly_samples
from stockapp.lake import Lake
from stockapp.models.backtest import run_backtest
from stockapp.models.compare import horizon_samples
from stockapp.models.gate import wilson_lower_bound
from stockapp.models.walkforward import add_months, fit_calibrator, fit_model

FIRST_TEST = date(2018, 1, 1)
LABEL = "label_c"


@dataclass(frozen=True)
class SafetyGate:
    status: str  # LIVE or OFF
    cutoff: float | None
    warnings: int  # deduplicated
    hits: int
    precision: float | None
    lower_bound: float | None
    years_ok: int
    years: int
    reason: str
    drop: float
    window: int

    def as_row(self) -> dict:
        return asdict(self)


def dedupe(preds: pl.DataFrame, cutoff: float, days: int) -> pl.DataFrame:
    """Warnings at or above ``cutoff``, at most one per stock per ``days`` calendar days (the
    first of a run is kept). Overlapping monthly windows would otherwise count one fall many
    times."""
    rows = preds.filter(pl.col("p") >= cutoff).sort("company_id", "trade_date")
    keep, last = [], {}
    for r in rows.iter_rows(named=True):
        prev = last.get(r["company_id"])
        if prev is None or (r["trade_date"] - prev).days >= days:
            keep.append(r)
            last[r["company_id"]] = r["trade_date"]
    return pl.DataFrame(keep, schema=rows.schema) if keep else rows.head(0)


def judge(preds: pl.DataFrame, cfg: AppConfig) -> SafetyGate:
    """The lowest cutoff whose deduplicated warnings pass; else OFF with the closest result."""
    s = cfg.safety
    data = preds.filter(pl.col("label").is_not_null())
    cutoffs = [round(0.30 + 0.025 * i, 3) for i in range(27)]  # 30% .. 95%
    best: tuple | None = None
    for c in cutoffs:
        w = dedupe(data, c, s.dedupe_days)
        n = w.height
        if n < s.min_signals:
            break
        hits = int(w["label"].sum())
        precision, lb = hits / n, wilson_lower_bound(hits, n)
        per_year = (
            w.with_columns(pl.col("trade_date").dt.year().alias("y"))
            .group_by("y")
            .agg(pl.len().alias("n"), pl.col("label").cast(pl.Float64).mean().alias("prec"))
            .filter(pl.col("n") >= 5)
        )
        years, years_ok = per_year.height, int((per_year["prec"] >= s.bar - 0.1).sum())
        row = (c, n, hits, precision, lb, years_ok, years)
        if best is None or precision > best[3]:
            best = row
        if precision >= s.bar and lb >= s.min_lower_bound and years and years_ok / years >= 0.6:
            return SafetyGate(
                "LIVE", c, n, hits, precision, lb, years_ok, years,
                f"right {precision:.0%} of the time on {n} separate warnings (worst case "
                f"{lb:.0%}), in {years_ok} of {years} years",
                s.drop, s.window,
            )  # fmt: skip
    if best is None:
        return SafetyGate("OFF", None, 0, 0, None, None, 0, 0,
                          f"never {s.min_signals}+ separate warnings at any level", s.drop,
                          s.window)  # fmt: skip
    c, n, hits, precision, lb, years_ok, years = best
    return SafetyGate(
        "OFF", None, n, hits, precision, lb, years_ok, years,
        f"best: right {precision:.0%} of the time on {n} separate warnings at {c:.0%}+ "
        f"(worst case {lb:.0%}, {years_ok} of {years} years); needs {s.bar:.0%} with a worst "
        f"case of {s.min_lower_bound:.0%}",
        s.drop, s.window,
    )  # fmt: skip


def _models_dir(lake: Lake) -> Path:
    d = lake.root.parent / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_safety(lake: Lake, cfg: AppConfig, today: date, on_fold=None) -> SafetyGate:
    s = cfg.safety
    samples = horizon_samples(lake, s.drop, s.drop, s.window)
    embargo = label_span_days(s.window)
    bt = run_backtest(samples, FEATURE_COLUMNS, LABEL, FIRST_TEST, False, kind=s.model,
                      embargo_days=embargo, on_fold=on_fold)  # fmt: skip
    gate = judge(bt.predictions, cfg)
    lake.write_partition(
        "gold", "safety_gate", "built", today.isoformat(),
        pl.DataFrame([gate.as_row()]).with_columns(
            pl.lit(datetime.now(UTC)).alias("evaluated_at"),
            pl.lit(FEATURE_VERSION).alias("feature_version"),
        ),
    )  # fmt: skip

    usable = samples.filter(~pl.col("blocked") & pl.col(LABEL).is_not_null())
    last = usable["trade_date"].max()
    calib_start = add_months(date(last.year, 3 * ((last.month - 1) // 3) + 1, 1), -9)
    train = usable.filter(pl.col("trade_date") < calib_start - timedelta(days=embargo))
    calib = usable.filter(pl.col("trade_date") >= calib_start)
    model = fit_model(train, calib, FEATURE_COLUMNS, LABEL, s.model)
    iso = fit_calibrator(model, calib, FEATURE_COLUMNS, LABEL)
    version = f"{FEATURE_VERSION}-{today:%Y%m%d}"
    meta = {"version": version, "drop": s.drop, "window": s.window, "model": s.model,
            "train_end": str(train["trade_date"].max()), "calib": [str(calib_start), str(last)],
            "gate": gate.as_row()}  # fmt: skip
    path = _models_dir(lake) / f"safety_{version}.pkl"
    with path.open("wb") as f:
        pickle.dump({"model": model, "calibrator": iso, "features": FEATURE_COLUMNS, "meta": meta},
                    f)  # fmt: skip
    path.with_suffix(".json").write_text(json.dumps(meta, indent=2, default=str))
    score_safety(lake)
    return gate


def score_safety(lake: Lake) -> pl.DataFrame | None:
    """Score the newest week with the saved safety model (weekly, seconds)."""
    from stockapp.models.files import newest_model

    path = newest_model(_models_dir(lake), "safety", FEATURE_VERSION)
    if path is None:
        return None
    with path.open("rb") as f:
        m = pickle.load(f)
    samples = load_weekly_samples(lake)
    day = samples["trade_date"].max()
    latest = samples.filter(pl.col("trade_date") == day)
    x = latest.select(pl.col(m["features"]).cast(pl.Float32)).to_numpy()
    p = m["calibrator"].predict(m["model"].predict_proba(x)[:, 1])
    out = latest.select("company_id", "symbol", "trade_date", "blocked").with_columns(
        pl.Series("p_drop", p), pl.lit(m["meta"]["version"]).alias("model_version")
    )
    lake.write_partition("gold", "safety_scores", "trade_date", day.isoformat(), out)
    return out


def latest_safety(lake: Lake, signal_date: date) -> tuple[dict[str, float], dict | None]:
    """(company_id -> p_drop for ``signal_date``, latest gate row). Empty when not built yet or
    when the scores are for another week (never use stale warnings)."""
    gate = None
    if lake.has_table("gold", "safety_gate"):
        g = lake.scan("gold", "safety_gate").collect()
        g = g.filter(pl.col("built") == pl.col("built").max())
        gate = g.row(0, named=True) if g.height else None
    scores: dict[str, float] = {}
    if lake.has_table("gold", "safety_scores"):
        sc = lake.scan("gold", "safety_scores").collect()
        sc = sc.filter((pl.col("trade_date") == signal_date) & ~pl.col("blocked"))
        scores = dict(zip(sc["company_id"].to_list(), sc["p_drop"].to_list(), strict=True))
    return scores, gate
