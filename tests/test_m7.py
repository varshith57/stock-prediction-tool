"""M7: the accuracy gate's arithmetic and rules."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from stockapp.models.gate import evaluate_gate, precision_curve, wilson_lower_bound


@pytest.mark.parametrize(
    ("hits", "n", "expected"),
    [(29, 30, 0.8333), (27, 30, 0.7438), (72, 80, 0.8149), (0, 0, 0.0)],
)
def test_wilson_matches_the_amendment_table(hits, n, expected):
    # AM1: at n=30 the rule needs 29/30; exactly 90% passes only from about 80 signals
    assert wilson_lower_bound(hits, n) == pytest.approx(expected, abs=1e-4)


def _preds(spec: list[tuple[float, int, int]], per_week: int = 10) -> pl.DataFrame:
    """spec: (p, n_rows, n_hits) blocks, spread over weeks ``per_week`` rows at a time."""
    rows, i = [], 0
    for p, n, hits in spec:
        for k in range(n):
            rows.append({"trade_date": date(2020, 1, 3) + timedelta(weeks=i // per_week),
                         "p": p, "label": k < hits, "gain": p})  # fmt: skip
            i += 1
    return pl.DataFrame(rows)


def test_live_at_the_lowest_qualifying_cutoff():
    preds = _preds([(0.5, 400, 200), (0.93, 40, 39), (0.97, 50, 49)])
    r = evaluate_gate(preds, "A")
    assert r.status == "LIVE" and r.cutoff == 0.93
    assert (r.signals, r.hits) == (90, 88)
    assert r.wilson_lb >= 0.8


def test_off_with_the_reason_and_closest_result():
    preds = _preds([(0.5, 400, 200), (0.9, 100, 85)])  # 85% at best: never lowered
    r = evaluate_gate(preds, "C")
    assert r.status == "OFF" and r.cutoff is None
    assert r.best_precision == pytest.approx(0.85)
    assert "85.0%" in r.reason and "needs 90%" in r.reason


def test_too_few_signals_is_off_even_at_100_percent():
    r = evaluate_gate(_preds([(0.5, 400, 100), (0.99, 20, 20)]), "A")
    assert r.status == "OFF"


def test_weekly_cap_counts_only_what_would_be_shown():
    # each week: 10 rows above the cutoff, only the 5 with the highest gain would be shown,
    # and those 5 are the misses
    rows = []
    for w in range(20):
        for k in range(10):
            rows.append({"trade_date": date(2020, 1, 3) + timedelta(weeks=w), "p": 0.95,
                         "label": k >= 5, "gain": float(k < 5)})  # fmt: skip
    preds = pl.DataFrame(rows)
    assert evaluate_gate(preds, "A").best_precision == pytest.approx(0.5)  # no cap: 100 of 200
    capped = evaluate_gate(preds, "A", max_per_week=5, rank_by="gain")
    assert capped.status == "OFF" and capped.best_precision == 0.0


def test_precision_curve_columns():
    curve = precision_curve(_preds([(0.5, 100, 50), (0.95, 50, 48)]))
    row = curve.filter(pl.col("cutoff") == 0.9).row(0, named=True)
    assert (row["signals"], row["hits"]) == (50, 48)
