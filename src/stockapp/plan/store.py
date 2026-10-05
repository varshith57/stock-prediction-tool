"""Persist weekly plans and logged actions (Postgres). Rebuilding a week's plan (e.g. after a
settings change) replaces it in place and archives the previous version."""

from __future__ import annotations

import json

import psycopg

from stockapp.ingest.registry import pipeline_version
from stockapp.plan.engine import Plan


def save_plan(conn: psycopg.Connection, plan: Plan, feature_version: str | None,
              settings_version: int | None = None) -> int:  # fmt: skip
    """Insert the plan for its signal date, or replace that week's plan in place: logged actions
    stay attached (same plan_id) and the replaced payload is archived in weekly_plan_revisions."""
    payload = json.dumps(plan.to_json(), default=str)
    existing = conn.execute(
        "SELECT plan_id, payload FROM weekly_plans WHERE signal_date = %s", (plan.signal_date,)
    ).fetchone()
    if existing:
        with conn.transaction():
            conn.execute(
                "INSERT INTO weekly_plan_revisions (plan_id, payload) VALUES (%s, %s)",
                (existing["plan_id"], json.dumps(existing["payload"])),
            )
            conn.execute(
                """UPDATE weekly_plans SET week_of = %s, status = %s, action_count = %s,
                       payload = %s, model_version = %s, feature_version = %s,
                       pipeline_version = %s, settings_version = %s, built_at = now()
                   WHERE plan_id = %s""",
                (
                    plan.week_of,
                    plan.status,
                    plan.action_count,
                    payload,
                    plan.model_version,
                    feature_version,
                    pipeline_version(),
                    settings_version,
                    existing["plan_id"],
                ),
            )
        return int(existing["plan_id"])
    row = conn.execute(
        """INSERT INTO weekly_plans (signal_date, week_of, status, action_count, payload,
                                     model_version, feature_version, pipeline_version,
                                     settings_version)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING plan_id""",
        (
            plan.signal_date,
            plan.week_of,
            plan.status,
            plan.action_count,
            payload,
            plan.model_version,
            feature_version,
            pipeline_version(),
            settings_version,
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


def review_decisions(conn: psycopg.Connection) -> dict[str, dict]:
    """company_id -> the latest decision on a "Review" flag (``kept`` / ``sold``) across weeks,
    so a holding you chose to keep isn't flagged as new every week."""
    rows = conn.execute(
        """SELECT DISTINCT ON (item_key) item_key, action, acted_at FROM plan_actions
           WHERE item_key LIKE 'REVIEW:%%' ORDER BY item_key, acted_at DESC"""
    ).fetchall()
    return {r["item_key"].removeprefix("REVIEW:"): r for r in rows}
