"""Pipeline steps shared by the CLI commands and the scheduled jobs (M9). Each step returns a
short, amount-free summary line; any exception fails the step (and the job stops there)."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import psycopg

from stockapp.config import AppConfig
from stockapp.lake import Lake

LOOKBACK_DAYS = 10  # re-check the last ~2 weeks so late or failed files are picked up


def ingest_recent(conn: psycopg.Connection, lake: Lake, today: date) -> str:
    from stockapp.ingest.backfill import backfill
    from stockapp.ingest.calendar import infer_holidays
    from stockapp.ingest.http import PoliteClient

    with PoliteClient(min_interval_s=1.0) as archives, PoliteClient(min_interval_s=2.0) as api:
        stats = backfill(
            conn,
            lake,
            archives,
            api,
            today - timedelta(days=LOOKBACK_DAYS),
            today,
            today=today,
            log=lambda _: None,
        )
    infer_holidays(
        conn,
        date(today.year - 1, 1, 1),
        today,
        price_sources=("nse_legacy_bhavcopy", "nse_udiff_bhavcopy"),
    )
    new = sum(n for (src, status), n in stats.counts.items() if status == "success")
    failed = [f"{f.source_id} {f.partition_key}" for f in stats.failures]
    if failed:
        raise RuntimeError(f"ingest failures: {', '.join(failed[:5])}")
    return f"ingest: {new} new file(s)"


def refresh_reference(conn: psycopg.Connection, lake: Lake, today: date) -> str:
    """Weekly: holiday list and the symbol-change, equity and sector snapshots."""
    from stockapp.ingest.calendar import refresh_holidays
    from stockapp.ingest.http import PoliteClient
    from stockapp.ingest.nse_reference import NseEquityList, NseSectorList, NseSymbolChanges

    with PoliteClient(min_interval_s=1.5) as http:
        n = refresh_holidays(conn, lake, http, today=today)
        statuses = [
            c(conn, lake, http).run(today).status
            for c in (NseSymbolChanges, NseEquityList, NseSectorList)
        ]
    if "failed" in statuses:
        raise RuntimeError(f"reference snapshot failed: {statuses}")
    return f"reference: {n} holidays, snapshots {statuses}"


def rebuild_quality(lake: Lake, cfg: AppConfig, today: date) -> str:
    from stockapp.adjust import build_adjustments
    from stockapp.master import build_company_master
    from stockapp.portfolio.valuation import build_latest_prices
    from stockapp.quality.gates import build_price_flags
    from stockapp.quality.score import build_quality_scores
    from stockapp.universe import build_universe

    build_company_master(lake, today)
    build_adjustments(lake, today)
    build_universe(lake, size=cfg.universe.size)
    build_price_flags(lake, today)
    scores = build_quality_scores(lake, today)
    build_latest_prices(lake, today)
    latest = scores.sort("trade_date").tail(1).row(0, named=True)
    if latest["score"] < cfg.data.min_quality_score:
        raise RuntimeError(
            f"data quality {latest['score']:.0f}/100 on {latest['trade_date']} is below "
            f"{cfg.data.min_quality_score:g}: NO SIGNAL until it recovers"
        )
    return f"quality: {latest['score']:.0f}/100 on {latest['trade_date']}"


def build_weekly_plan(conn: psycopg.Connection, lake: Lake, cfg: AppConfig, today: date):
    from stockapp.features.pipeline import FEATURE_VERSION
    from stockapp.plan import inputs
    from stockapp.plan.engine import build_plan
    from stockapp.plan.store import save_plan
    from stockapp.portfolio import service

    scores = lake.scan("gold", "latest_scores").collect()
    signal_date = scores["trade_date"].max()
    cands, market, model_version = inputs.candidates(lake, signal_date)
    safety_scores, safety_gate = inputs.safety(lake, signal_date)
    states, weights, dd, budget = inputs.holdings(conn, lake, cfg, today)
    value = service.load(conn, lake, cfg, today).view.totals["value"]
    plan = build_plan(
        cfg=cfg,
        signal_date=signal_date,
        week_of=inputs.next_monday(signal_date),
        quality_score=inputs.quality_for(lake, signal_date),
        candidates=cands,
        holdings=states,
        holding_weights=weights,
        gates=inputs.gates(lake, cfg),
        regime=inputs.regime(market, cfg),
        drawdown=dd,
        budget_available=budget,
        portfolio_value=value,
        model_version=model_version,
        safety=safety_scores,
        safety_gate=safety_gate,
    )
    from stockapp.settings_store import latest

    row = latest(conn)
    plan_id = save_plan(conn, plan, FEATURE_VERSION, row["version_id"] if row else None)
    return plan, plan_id


def exit_watch(
    conn: psycopg.Connection, lake: Lake, cfg: AppConfig, today: date
) -> list[tuple[str, str]]:
    """Hard exit rules on current holdings with today's close (no model needed)."""
    from stockapp.plan import inputs
    from stockapp.plan.rules import exit_rules

    states, _, _, _ = inputs.holdings(conn, lake, cfg, today)
    return [(h.symbol, r.rule) for h in states for r in exit_rules(h, cfg)]


def latest_session(lake: Lake) -> date | None:
    if not lake.has_table("gold", "latest_prices"):
        return None
    df = lake.scan("gold", "latest_prices").collect()
    return df.filter(pl.col("built") == pl.col("built").max())["data_as_of"].max()
