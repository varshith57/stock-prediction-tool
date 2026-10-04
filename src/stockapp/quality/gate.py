"""M3 gate (plan: "known splits and bonuses reconcile; quality score at least 95").

* Every corporate-action price event: at least ``MIN_CONTINUITY`` reconcile (continuity test).
* ``KNOWN_EVENTS``: well-known large-cap splits and bonuses. Each must be present in the data *and*
  reconcile; a missing one fails the gate (it would mean an action was dropped).
* Quality score: at least 95 on at least 95% of sessions (PRD data KPI), and on the latest session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import polars as pl

from stockapp.adjust import continuity_check
from stockapp.lake import Lake

MIN_CONTINUITY = 0.98
MIN_SCORE, MIN_SCORE_SHARE = 95.0, 0.95

# (company_id, ex_date, what): checked against NSE prices, not taken on trust from this list
KNOWN_EVENTS = (
    ("RELIANCE", date(2017, 9, 7), "bonus 1:1"),
    ("RELIANCE", date(2024, 10, 28), "bonus 1:1"),
    ("HDFCBANK", date(2019, 9, 19), "split 2 -> 1"),
    ("HDFCBANK", date(2025, 8, 26), "bonus 1:1"),
    ("INFY", date(2018, 9, 4), "bonus 1:1"),
    ("TCS", date(2018, 5, 31), "bonus 1:1"),
    ("WIPRO", date(2019, 3, 6), "bonus 1:3"),
    ("WIPRO", date(2024, 12, 3), "bonus 1:1"),
    ("ITC", date(2016, 7, 1), "bonus 1:2"),
    ("BAJFINANCE", date(2016, 9, 8), "bonus 1:1 + split 2 -> 1"),
    ("BAJFINANCE", date(2025, 6, 16), "bonus 4:1 + split 2 -> 1"),
    ("NESTLEIND", date(2024, 1, 5), "split 10 -> 1"),
    ("JSWSTEEL", date(2017, 1, 4), "split 10 -> 1"),
    ("UNITDSPR", date(2018, 6, 15), "split 10 -> 2 (traded as MCDOWELL-N then)"),
    ("TATASTEEL", date(2022, 7, 28), "split 10 -> 1"),
    ("IRCTC", date(2021, 10, 28), "split 10 -> 2"),
)


@dataclass(frozen=True)
class GateResult:
    passed: bool
    lines: list[str]


def evaluate_m3_gate(lake: Lake, scores: pl.DataFrame) -> GateResult:
    lines: list[str] = []
    c = continuity_check(lake)
    rate = c["passed"].mean() if c.height else 0.0
    ok_cont = rate >= MIN_CONTINUITY
    lines.append(
        f"continuity: {c['passed'].sum()}/{c.height} = {rate:.2%} (need >= {MIN_CONTINUITY:.0%})"
        f" {'OK' if ok_cont else 'FAIL'}"
    )

    ok_known = True
    for company, ex, what in KNOWN_EVENTS:
        row = c.filter((pl.col("company_id") == company) & (pl.col("ex_date") == ex))
        if row.is_empty():
            ok_known = False
            lines.append(f"  MISSING {company} {ex} {what}")
        elif not row["passed"][0]:
            ok_known = False
            lines.append(
                f"  FAIL {company} {ex} {what}: adjusted move {row['adjusted_move'][0]:.2%}"
            )
    lines.append(f"known events: {len(KNOWN_EVENTS)} checked {'OK' if ok_known else 'FAIL'}")

    share = (scores["score"] >= MIN_SCORE).mean() if scores.height else 0.0
    latest = scores.sort("trade_date")["score"][-1] if scores.height else 0.0
    ok_score = share >= MIN_SCORE_SHARE and latest >= MIN_SCORE
    lines.append(
        f"quality score: >= {MIN_SCORE:g} on {share:.1%} of {scores.height} sessions "
        f"(need {MIN_SCORE_SHARE:.0%}), latest {latest:.2f} {'OK' if ok_score else 'FAIL'}"
    )
    passed = ok_cont and ok_known and ok_score
    lines.append(f"M3 gate: {'PASS' if passed else 'FAIL'}")
    return GateResult(passed, lines)
