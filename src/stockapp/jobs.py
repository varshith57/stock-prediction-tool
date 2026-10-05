"""Scheduled jobs (M9): daily, weekly and monthly, run by launchd on this Mac.

Each job runs its steps in order and stops at the first failure: the failure is recorded in
``job_runs`` and a Telegram alert names the step (summary only). Every step is recorded too, so
the Monthly Audit and Data Health can show what ran.

* daily (Mon-Fri 19:30): new NSE files -> (Mondays) reference snapshots -> quality build ->
  exit watch on holdings (hard rules alert immediately, one per stock per week).
* weekly (Fri 20:00): daily steps -> features -> final models and scores -> plan -> Telegram.
* monthly (first Saturday): re-validate the gate on fresh walk-forward results -> retrain ->
  "Monthly audit ready".
"""

from __future__ import annotations

import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import psycopg

from stockapp.alerts.notify import notify
from stockapp.alerts.telegram import AlertError, PrivacyViolation
from stockapp.config import AppConfig
from stockapp.ingest import registry as reg
from stockapp.lake import Lake

Step = tuple[str, Callable[[], str | None]]


@dataclass
class JobResult:
    name: str
    ok: bool
    lines: list[str] = field(default_factory=list)
    failed_step: str | None = None


def run_steps(
    conn: psycopg.Connection,
    cfg: AppConfig,
    name: str,
    steps: list[Step],
    today: date,
    alert: Callable[..., str] = notify,
) -> JobResult:
    result = JobResult(name, True)
    for step_name, fn in steps:
        job_id = reg.start_job(conn, f"job:{name}:{step_name}", None, today.isoformat())
        try:
            line = fn() or "ok"
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[:300]}"
            reg.finish_job(conn, job_id, "failed", message=message)
            result.ok, result.failed_step = False, step_name
            result.lines.append(f"{step_name}: FAILED {message}")
            traceback.print_exc(file=sys.stderr)
            text = (
                f"stockapp {name} job failed at '{step_name}' ({type(exc).__name__}). "
                "Data may be stale: no new actions until it's fixed."
            )
            try:
                alert(conn, cfg, "data_failure", text, dedupe_key=f"failure:{name}:{today}")
            except (AlertError, PrivacyViolation) as alert_exc:
                result.lines.append(f"(alert not sent: {alert_exc})")
            break
        reg.finish_job(conn, job_id, "success", message=line[:500])
        result.lines.append(line)
    return result


def daily_steps(conn: psycopg.Connection, lake: Lake, cfg: AppConfig, today: date) -> list[Step]:
    from stockapp import pipeline

    def watch() -> str:
        hits = pipeline.exit_watch(conn, lake, cfg, today)
        week = today.isocalendar()
        sent = 0
        for symbol, rule in hits:
            status = notify(
                conn,
                cfg,
                "exit_rule",
                f"Exit rule hit: {symbol} ({rule.replace('_', ' ')}). See This Week.",
                dedupe_key=f"exit:{symbol}:{rule}:{week.year}-W{week.week}",
            )
            sent += status == "sent"
        return f"exit watch: {len(hits)} rule hit(s), {sent} alert(s) sent"

    steps: list[Step] = [("ingest", lambda: pipeline.ingest_recent(conn, lake, today))]
    if today.weekday() == 0:
        steps.append(("reference", lambda: pipeline.refresh_reference(conn, lake, today)))

    def plan() -> str:
        # this week's advice with today's prices: rules, reviews, values and gains stay current
        if not lake.has_table("gold", "latest_scores"):
            return "plan refresh: no scores yet"
        p, _ = pipeline.build_weekly_plan(conn, lake, cfg, today)
        return f"plan refreshed: {p.action_count} action(s)"

    steps += [
        ("quality", lambda: pipeline.rebuild_quality(lake, cfg, today)),
        ("exit_watch", watch),
        ("plan refresh", plan),
    ]
    return steps


def weekly_steps(conn: psycopg.Connection, lake: Lake, cfg: AppConfig, today: date) -> list[Step]:
    from stockapp import pipeline
    from stockapp.cli import plan_summary
    from stockapp.features.pipeline import build_weekly_samples
    from stockapp.models.run import train_and_score

    def features() -> str:
        s = build_weekly_samples(lake, cfg, today)
        return f"features: {s.height} samples"

    def score() -> str:
        s = train_and_score(lake, cfg, today)
        return f"scores: {s.height} stocks for {s['trade_date'][0]}"

    def safety() -> str:
        from stockapp.models.safety import score_safety

        out = score_safety(lake) if cfg.safety.enabled else None
        return "safety net: not built yet" if out is None else f"safety net: {out.height} scored"

    def plan() -> str:
        p, plan_id = pipeline.build_weekly_plan(conn, lake, cfg, today)
        status = notify(
            conn, cfg, "weekly_plan", plan_summary(p), dedupe_key=f"plan:{p.signal_date}"
        )
        return f"plan {plan_id}: {p.action_count} action(s); telegram {status}"

    return [
        *daily_steps(conn, lake, cfg, today),
        ("features", features),
        ("score", score),
        ("safety scores", safety),
        ("plan", plan),
    ]


def monthly_steps(conn: psycopg.Connection, lake: Lake, cfg: AppConfig, today: date) -> list[Step]:
    from stockapp.models.run import run_backtests, train_and_score

    def revalidate() -> str:
        run = run_backtests(lake, cfg, today)
        return "gate: " + ", ".join(f"{s} {g.status}" for s, g in run.gates.items())

    def safety_net() -> str:
        from stockapp.models.safety import build_safety

        if not cfg.safety.enabled:
            return "safety net: off"
        return f"safety net: {build_safety(lake, cfg, today).status}"

    def money() -> str:
        from stockapp.models.money import evaluate_money_gate

        return f"money test: {evaluate_money_gate(lake, cfg, today).status}"

    def retrain() -> str:
        s = train_and_score(lake, cfg, today)
        return f"retrained: model {s['model_version'][0]}"

    def announce() -> str:
        status = notify(
            conn,
            cfg,
            "monthly_audit",
            "Monthly audit ready: open the app's Monthly Audit to review the last 4 weeks.",
            dedupe_key=f"audit:{today:%Y-%m}",
        )
        return f"audit notice: {status}"

    return [
        ("revalidate", revalidate),
        ("money test", money),
        ("safety net", safety_net),
        ("retrain", retrain),
        ("announce", announce),
    ]
