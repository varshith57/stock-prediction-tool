"""The 90% accuracy gate (M7, PRD section 5; amendment AM1 explains its strictness).

For a signal, on out-of-sample walk-forward predictions only: find the *lowest* probability
cutoff at which the signals that would have been shown were right at least ``min_precision`` of
the time, with at least ``min_signals`` of them and a 95% Wilson lower bound of at least
``min_lower_bound``. That cutoff makes the signal LIVE. If no cutoff qualifies the signal is OFF,
with the reason and the closest result; the bar is never lowered to make it pass.

"Shown" follows the product: for signal A at most ``max_per_week`` per week, highest expected
gain first; signal C applies to holdings, but it's evaluated on every stock (PRD: judge the model
on all its signals, not only the ones traded).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import polars as pl

Z95 = 1.959963984540054


def wilson_lower_bound(hits: int, n: int, z: float = Z95) -> float:
    if n == 0:
        return 0.0
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - margin


@dataclass(frozen=True)
class GateResult:
    signal: str
    status: str  # LIVE or OFF
    cutoff: float | None
    signals: int
    hits: int
    precision: float | None
    wilson_lb: float | None
    reason: str
    best_precision: float | None  # closest result when OFF
    best_precision_signals: int

    def as_row(self) -> dict:
        return asdict(self)


def shown(
    preds: pl.DataFrame, cutoff: float, max_per_week: int | None, rank_by: str
) -> pl.DataFrame:
    s = preds.filter(pl.col("p") >= cutoff)
    if max_per_week is None:
        return s
    return s.with_columns(
        pl.col(rank_by).rank("ordinal", descending=True).over("trade_date").alias("_r")
    ).filter(pl.col("_r") <= max_per_week)


def evaluate_gate(
    preds: pl.DataFrame,
    signal: str,
    *,
    min_precision: float = 0.90,
    min_signals: int = 30,
    min_lower_bound: float = 0.80,
    max_per_week: int | None = None,
    rank_by: str = "p",
) -> GateResult:
    """``preds``: out-of-sample rows with trade_date, label (bool), p (calibrated) and
    ``rank_by``. Cutoffs are tried from low to high over the distinct stated probabilities."""
    data = preds.filter(pl.col("label").is_not_null())
    cutoffs = sorted({round(float(x), 6) for x in data["p"].to_list() if x is not None})
    best = (None, 0)  # (precision, n) with the most signals among the best-precision cutoffs
    for c in cutoffs:
        s = shown(data, c, max_per_week, rank_by)
        n = s.height
        if n == 0:
            break
        hits = int(s["label"].sum())
        precision = hits / n
        lb = wilson_lower_bound(hits, n)
        if n >= min_signals and (best[0] is None or precision > best[0]):
            best = (precision, n)
        if precision >= min_precision and n >= min_signals and lb >= min_lower_bound:
            return GateResult(signal, "LIVE", c, n, hits, precision, lb,
                              f"precision {precision:.1%} on {n} signals, lower bound {lb:.1%}",
                              precision, n)  # fmt: skip
    if best[0] is None:
        reason = f"never {min_signals}+ signals at any cutoff"
    else:
        reason = (
            f"best precision with {min_signals}+ signals was {best[0]:.1%} ({best[1]} signals); "
            f"needs {min_precision:.0%} with a lower bound of {min_lower_bound:.0%}"
        )
    return GateResult(signal, "OFF", None, 0, 0, None, None, reason, best[0], best[1])


def precision_curve(preds: pl.DataFrame, max_per_week: int | None = None, rank_by: str = "p",
                    cutoffs: tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
                    ) -> pl.DataFrame:  # fmt: skip
    """What the signal would have looked like at each cutoff: for the report and Settings."""
    data = preds.filter(pl.col("label").is_not_null())
    rows = []
    for c in cutoffs:
        s = shown(data, c, max_per_week, rank_by)
        n, hits = s.height, int(s["label"].sum()) if s.height else 0
        rows.append(
            {"cutoff": c, "signals": n, "hits": hits,
             "precision": round(hits / n, 4) if n else None,
             "wilson_lb": round(wilson_lower_bound(hits, n), 4) if n else None,
             "weeks_with_signal": s["trade_date"].n_unique() if n else 0}
        )  # fmt: skip
    return pl.DataFrame(rows)
