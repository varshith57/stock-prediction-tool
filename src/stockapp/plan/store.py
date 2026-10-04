"""Persist weekly plans and logged actions (Postgres). A rebuilt plan for the same signal date
replaces the earlier one only if no action has been logged against it yet."""

from __future__ import annotations

import json

import psycopg

from stockapp.ingest.registry import pipeline_version
from stockapp.plan.engine import Plan


class PlanLocked(RuntimeError):
    pass


def save_plan(conn: psycopg.Connection, plan: Plan, feature_version: str | None) -> int:
    existing = conn.execute(
        "SELECT plan_id FROM weekly_plans WHERE signal_date = %s", (plan.signal_date,)
    ).fetchone()
    if existing:
        acted = conn.execute(
            "SELECT count(*) AS n FROM plan_actions WHERE plan_id = %s", (existing["plan_id"],)
        ).fetchone()
        if acted and acted["n"]:
            raise PlanLocked(
                f"the plan for {plan.signal_date} already has logged actions; not replaced"
            )
        conn.execute("DELETE FROM weekly_plans WHERE plan_id = %s", (existing["plan_id"],))
    row = conn.execute(
        """INSERT INTO weekly_plans (signal_date, week_of, status, action_count, payload,
                                     model_version, feature_version, pipeline_version)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING plan_id""",
        (
            plan.signal_date,
            plan.week_of,
            plan.status,
            plan.action_count,
            json.dumps(plan.to_json(), default=str),
            plan.model_version,
            feature_version,
            pipeline_version(),
        ),
    ).fetchone()
    assert row is not None
    return int(row["plan_id"])


def latest_plan(conn: psycopg.Connection) -> dict | None:
    return conn.execute("SELECT * FROM weekly_plans ORDER BY signal_date DESC LIMIT 1").fetchone()


def log_action(
    conn: psycopg.Connection, plan_id: int, item_key: str, action: str, reason: str | None = None
) -> None:
    conn.execute(
        """INSERT INTO plan_actions (plan_id, item_key, action, reason) VALUES (%s, %s, %s, %s)
           ON CONFLICT (plan_id, item_key) DO UPDATE
               SET action = EXCLUDED.action, reason = EXCLUDED.reason, acted_at = now()""",
        (plan_id, item_key, action, reason),
    )


def actions_for(conn: psycopg.Connection, plan_id: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT item_key, action, reason, acted_at FROM plan_actions WHERE plan_id = %s",
        (plan_id,),
    ).fetchall()
    return {r["item_key"]: r for r in rows}
