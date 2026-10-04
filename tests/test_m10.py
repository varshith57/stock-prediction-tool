"""M10 gate: the audit reconciles with the plan and action log."""

from __future__ import annotations

from datetime import date

import polars as pl
import psycopg
import pytest

from stockapp.audit.report import build_audit, lock_snapshot, to_markdown
from stockapp.config import load_app_config
from stockapp.lake import Lake
from stockapp.plan.engine import Plan, PlanItem
from stockapp.plan.rules import Drawdown, Regime
from stockapp.plan.store import log_action, save_plan

CFG = load_app_config(local_path=None)
W1, W2, W3 = date(2026, 9, 4), date(2026, 9, 11), date(2026, 9, 18)


def _plan(day: date, opps: list[tuple[str, float]], exits: list[tuple[str, float | None]]) -> Plan:
    p = Plan(day, day, "OK", "", 99.0, Regime(False, "normal"), Drawdown(None, False, False), 0.0)
    p.opportunities = [PlanItem("BUY", c, c, "buy", "", probability=pa) for c, pa in opps]
    p.exits = [
        PlanItem("SELL", c, c, "sell", "", probability=pc, rule=None if pc else "stop_loss")
        for c, pc in exits
    ]
    return p


@pytest.fixture
def audit_lake(lake: Lake) -> Lake:
    labels = pl.DataFrame(
        {
            "company_id": ["AAA", "BBB", "CCC", "DDD", "AAA", "EEE", "AAA"],
            "trade_date": [W1, W1, W1, W1, W2, W2, W3],
            "label_a": [True, False, False, True, False, True, None],  # W3 not matured yet
            "label_c": [False, False, True, False, False, False, None],
            "feature_version": ["fv"] * 7,
        }
    )
    lake.write_partition("gold", "weekly_samples", "year", "2026", labels)
    scores = pl.DataFrame(
        {
            "company_id": ["AAA", "BBB", "CCC", "DDD"],
            "trade_date": [W1] * 4,
            "p_a": [0.92, 0.6, 0.1, 0.3],
            "p_c": [0.02, 0.05, 0.91, 0.05],
        }
    )
    lake.write_partition("gold", "latest_scores", "trade_date", str(W1), scores)
    return lake


def test_audit_reconciles_with_the_log(db: psycopg.Connection, audit_lake: Lake):
    p1 = save_plan(
        db, _plan(W1, [("AAA", 0.92), ("BBB", 0.91)], [("CCC", 0.91), ("ZZZ", None)]), "fv"
    )
    p2 = save_plan(db, _plan(W2, [("AAA", 0.93)], []), "fv")
    save_plan(db, _plan(W3, [("AAA", 0.95)], []), "fv")
    log_action(db, p1, "BUY:AAA", "done")
    log_action(db, p1, "BUY:BBB", "skipped", "no cash")
    log_action(db, p1, "SELL:CCC", "partly")
    log_action(db, p2, "BUY:AAA", "done")

    a = build_audit(db, audit_lake, CFG, date(2026, 9, 1), date(2026, 9, 30))
    assert a.plans == 3
    cards = {c.signal: c for c in a.scorecards}
    # A: AAA W1 hit, BBB W1 miss, AAA W2 miss, AAA W3 pending
    assert (cards["A"].issued, cards["A"].matured, cards["A"].correct, cards["A"].pending) == (
        4,
        3,
        1,
        1,
    )
    assert cards["A"].precision == pytest.approx(1 / 3)
    # C: CCC W1 hit; the stop-loss exit (no probability) is a rule, not a C signal
    assert (cards["C"].issued, cards["C"].correct) == (1, 1)
    # missed events: A events were AAA W1 (flagged), DDD W1, EEE W2 (missed)
    assert a.missed["A"] == {"events": 3, "flagged": 1, "missed": 2}
    assert a.missed["C"] == {"events": 1, "flagged": 1, "missed": 0}
    # actions: exactly the logged rows
    assert a.action_counts == {"done": 2, "skipped": 1, "partly": 1}
    assert len(a.actions) == 4 and any(r["reason"] == "no cash" for r in a.actions)
    assert "Signal A: 4 issued, 1 correct (33%); claim 90%" in a.verdict
    assert any("no proposal yet (3 of 30" in p for p in a.proposals)
    # live calibration from the scores history
    assert {r["signal"] for r in a.calibration} == {"A", "C"}

    sid = lock_snapshot(db, a)
    stored = db.execute(
        "SELECT payload FROM audit_snapshots WHERE snapshot_id = %s", (sid,)
    ).fetchone()
    assert stored["payload"]["plans"] == 3
    assert "## Signal scorecard" in to_markdown(a)


def test_empty_period_is_honest(db: psycopg.Connection, lake: Lake):
    a = build_audit(db, lake, CFG, date(2026, 1, 1), date(2026, 1, 31))
    assert a.plans == 0 and a.verdict == "Signal A: none issued. Signal C: none issued."
    assert any("No weekly plans" in n for n in a.notes)
