"""Monthly Audit (M10, PRD 8.3): a review of a period against what the app actually showed.

Sources, all recorded at the time:
* issued signals = items in the saved weekly plans (Postgres ``weekly_plans``), not backtests;
* your actions = ``plan_actions``;
* outcomes = labels from the weekly samples once the 5-day window has passed;
* live calibration = every weekly score (gold ``latest_scores`` history) vs its outcome;
* money = the portfolio's time-weighted return vs a Nifty 50 portfolio with the same cash flows.

The model is judged on all of its signals, not just the ones traded (PRD). Proposals appear only
with at least ``min_signals`` matured signals and are never applied automatically.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

import duckdb
import polars as pl
import psycopg

from stockapp.config import AppConfig
from stockapp.lake import Lake
from stockapp.models.gate import wilson_lower_bound

SIGNAL_LABEL = {"A": "label_a", "C": "label_c"}


@dataclass
class Scorecard:
    signal: str
    issued: int
    matured: int
    correct: int
    pending: int
    precision: float | None
    wilson_lb: float | None
    claim: float


@dataclass
class AuditResult:
    start: date
    end: date
    verdict: str
    scorecards: list[Scorecard]
    missed: dict[str, dict[str, int]]
    actions: list[dict]
    action_counts: dict[str, int]
    plans: int
    money: dict[str, float | None]
    calibration: list[dict]
    health: dict[str, object]
    proposals: list[str]
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        d["start"], d["end"] = str(self.start), str(self.end)
        return d


def issued_signals(conn: psycopg.Connection, start: date, end: date) -> pl.DataFrame:
    """Every A opportunity and C exit shown in a plan whose signal date is in [start, end]."""
    rows = conn.execute(
        """SELECT plan_id, signal_date, payload FROM weekly_plans
           WHERE signal_date BETWEEN %s AND %s ORDER BY signal_date""",
        (start, end),
    ).fetchall()
    out = []
    for r in rows:
        p = r["payload"]
        for item in p.get("opportunities", []):
            out.append(
                {
                    "plan_id": r["plan_id"],
                    "trade_date": r["signal_date"],
                    "signal": "A",
                    "company_id": item["company_id"],
                    "symbol": item["symbol"],
                    "probability": item["probability"],
                }
            )
        for item in p.get("exits", []):
            if item.get("probability") is not None:  # C predictions; rules carry no probability
                out.append(
                    {
                        "plan_id": r["plan_id"],
                        "trade_date": r["signal_date"],
                        "signal": "C",
                        "company_id": item["company_id"],
                        "symbol": item["symbol"],
                        "probability": item["probability"],
                    }
                )
    schema = {
        "plan_id": pl.Int64,
        "trade_date": pl.Date,
        "signal": pl.String,
        "company_id": pl.String,
        "symbol": pl.String,
        "probability": pl.Float64,
    }
    return pl.DataFrame(out, schema=schema)


def _labels(lake: Lake, start: date, end: date) -> pl.DataFrame:
    if not lake.has_table("gold", "weekly_samples"):
        return pl.DataFrame(
            schema={
                "company_id": pl.String,
                "trade_date": pl.Date,
                "label_a": pl.Boolean,
                "label_c": pl.Boolean,
            }
        )
    return (
        lake.scan("gold", "weekly_samples")
        .filter((pl.col("trade_date") >= start) & (pl.col("trade_date") <= end))
        .select("company_id", "trade_date", "label_a", "label_c")
        .collect()
        .unique(subset=["company_id", "trade_date"], keep="last")  # one row even after a rebuild
    )


def scorecards(issued: pl.DataFrame, labels: pl.DataFrame, claim: float) -> list[Scorecard]:
    cards = []
    for signal, label in SIGNAL_LABEL.items():
        s = issued.filter(pl.col("signal") == signal).join(
            labels.select("company_id", "trade_date", pl.col(label).alias("outcome")),
            on=["company_id", "trade_date"],
            how="left",
        )
        matured = s.filter(pl.col("outcome").is_not_null())
        correct = int(matured["outcome"].sum()) if matured.height else 0
        n = matured.height
        cards.append(
            Scorecard(
                signal,
                s.height,
                n,
                correct,
                s.height - n,
                correct / n if n else None,
                wilson_lower_bound(correct, n) if n else None,
                claim,
            )
        )
    return cards


def missed_events(issued: pl.DataFrame, labels: pl.DataFrame) -> dict[str, dict[str, int]]:
    """+10% weeks (A) and -10% weeks (C) in the universe, and how many the app flagged (recall)."""
    out = {}
    for signal, label in SIGNAL_LABEL.items():
        events = labels.filter(pl.col(label).fill_null(False))
        flagged = events.join(
            issued.filter(pl.col("signal") == signal), on=["company_id", "trade_date"], how="semi"
        ).height
        out[signal] = {
            "events": events.height,
            "flagged": flagged,
            "missed": events.height - flagged,
        }
    return out


def live_calibration(lake: Lake, labels: pl.DataFrame) -> list[dict]:
    if not lake.has_table("gold", "latest_scores"):
        return []
    scores = (
        lake.scan("gold", "latest_scores")
        .select("company_id", "trade_date", "p_a", "p_c")
        .collect()
    )
    joined = scores.join(labels, on=["company_id", "trade_date"])
    rows = []
    for signal, p, label in (("A", "p_a", "label_a"), ("C", "p_c", "label_c")):
        m = joined.filter(pl.col(label).is_not_null())
        if m.is_empty():
            continue
        t = (
            m.with_columns(pl.col(p).cut([0.05, 0.1, 0.2, 0.3, 0.5]).alias("band"))
            .group_by("band")
            .agg(
                pl.len().alias("n"),
                pl.col(p).mean().alias("stated"),
                pl.col(label).cast(pl.Float64).mean().alias("observed"),
            )
            .sort("band")
        )
        rows += [{"signal": signal, **r, "band": str(r["band"])} for r in t.iter_rows(named=True)]
    return rows


def plan_actions(conn: psycopg.Connection, start: date, end: date) -> list[dict]:
    return conn.execute(
        """SELECT p.signal_date, a.item_key, a.action, a.reason, a.acted_at
           FROM plan_actions a JOIN weekly_plans p USING (plan_id)
           WHERE p.signal_date BETWEEN %s AND %s ORDER BY p.signal_date, a.item_key""",
        (start, end),
    ).fetchall()


def health(conn: psycopg.Connection, lake: Lake, start: date, end: date) -> dict[str, object]:
    out: dict[str, object] = {}
    if lake.has_table("gold", "quality_daily"):
        q = lake.scan("gold", "quality_daily").collect()
        q = q.filter(
            (pl.col("built") == pl.col("built").max())
            & (pl.col("trade_date") >= start)
            & (pl.col("trade_date") <= end)
        )
        out["quality_min"] = q["score"].min() if q.height else None
        out["quality_mean"] = round(q["score"].mean(), 2) if q.height else None
    jobs = conn.execute(
        """WITH period AS (SELECT * FROM job_runs WHERE started_at::date BETWEEN %s AND %s),
           latest AS (SELECT DISTINCT ON (source_id, partition_key) status FROM period
                      ORDER BY source_id, partition_key, started_at DESC, job_run_id DESC)
           SELECT (SELECT count(*) FROM latest WHERE status = 'failed') AS unresolved,
                  (SELECT count(*) FROM period WHERE status = 'failed') AS failed_attempts,
                  (SELECT count(*) FROM period) AS total""",
        (start, end),
    ).fetchone()
    out["failures_unresolved"] = jobs["unresolved"]
    out["failed_attempts_retried"] = jobs["failed_attempts"] - jobs["unresolved"]
    out["job_runs"] = jobs["total"]
    q = conn.execute(
        """SELECT count(*) FILTER (WHERE severity = 'BLOCK') AS block,
                  count(*) FILTER (WHERE severity = 'WARN') AS warn
           FROM quarantine WHERE resolved_at IS NULL"""
    ).fetchone()
    out["open_quarantine_block"], out["open_quarantine_warn"] = q["block"], q["warn"]
    if lake.has_table("gold", "signal_gate"):
        g = lake.scan("gold", "signal_gate").collect()
        g = g.filter(pl.col("built") == pl.col("built").max())
        out["gates"] = {r["signal"]: r["status"] for r in g.iter_rows(named=True)}
    return out


def proposals(cards: list[Scorecard], min_signals: int) -> list[str]:
    out = []
    for c in cards:
        if c.matured < min_signals:
            out.append(
                f"Signal {c.signal}: no proposal yet ({c.matured} of {min_signals} matured "
                "signals needed)"
            )
        elif c.precision is not None and c.precision < c.claim:
            out.append(
                f"Signal {c.signal}: precision {c.precision:.0%} is below the {c.claim:.0%} "
                "claim: propose switching it OFF until a retrain passes the gate"
            )
    return out


def money_for_period(
    conn: psycopg.Connection, lake: Lake, cfg: AppConfig, start: date, end: date
) -> dict[str, float | None]:
    """Time-weighted return of the portfolio in the period vs a Nifty 50 portfolio fed the same
    cash flows, realised gain, estimated charges paid, and the worst drawdown in the period."""
    from stockapp.adjust import adjusted_prices_sql
    from stockapp.portfolio.costs import order_charges
    from stockapp.portfolio.history import value_history
    from stockapp.portfolio.ledger import build_ledger
    from stockapp.portfolio.store import list_transactions, to_ledger_txns
    from stockapp.portfolio.valuation import quantity_events

    rows = list_transactions(conn)
    if not rows:
        return {
            "twr": None,
            "benchmark": None,
            "realised": 0.0,
            "charges": 0.0,
            "max_drawdown": None,
        }
    est = lambda side, q, p: order_charges(side, q, p, cfg.costs).total  # noqa: E731
    txns = to_ledger_txns(rows, est)
    companies = sorted({t.company_id for t in txns})
    ids = ", ".join(f"'{c}'" for c in companies)
    prices = duckdb.sql(
        f"SELECT company_id, trade_date, close FROM ({adjusted_prices_sql(lake)}) "
        f"WHERE company_id IN ({ids})"
    ).pl()
    idx = lake.duckdb_glob("silver", "nse_index_close")
    nifty = duckdb.sql(
        f"""SELECT trade_date, close FROM read_parquet('{idx}', hive_partitioning = true)
            WHERE lower(index_name) = 'nifty 50' ORDER BY 1"""
    ).pl()
    events = quantity_events(lake, companies)
    h = value_history(txns, events, prices, nifty)
    period = h.filter((pl.col("trade_date") >= start) & (pl.col("trade_date") <= end))
    before = h.filter(pl.col("trade_date") < start).tail(1)
    if period.is_empty():
        twr = bench = dd = None
    else:
        base = before["twr_index"][0] if before.height else 1.0
        twr = period["twr_index"][-1] / base - 1
        flows = period["net_flow"].sum()
        b0 = before["benchmark_value"][0] if before.height else 0.0
        b_end = period["benchmark_value"][-1]
        bench = (b_end - flows) / b0 - 1 if b0 else (b_end / flows - 1 if flows else None)
        running = period["twr_index"].cum_max()
        dd = float((period["twr_index"] / running - 1).min())
    realised = sum(
        r.gain for r in build_ledger(txns, events).realised if start <= r.sell_date <= end
    )
    charges = sum(t.charges for t in txns if start <= t.effective_date <= end)
    return {
        "twr": twr,
        "benchmark": bench,
        "realised": round(realised, 2),
        "charges": round(charges, 2),
        "max_drawdown": dd,
    }


def build_audit(
    conn: psycopg.Connection,
    lake: Lake,
    cfg: AppConfig,
    start: date,
    end: date,
    money: dict[str, float | None] | None = None,
) -> AuditResult:
    issued = issued_signals(conn, start, end)
    labels = _labels(lake, start, end)
    cards = scorecards(issued, labels, cfg.signals.certainty_bar)
    acts = plan_actions(conn, start, end)
    counts: dict[str, int] = {}
    for a in acts:
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    plans = conn.execute(
        "SELECT count(*) AS n FROM weekly_plans WHERE signal_date BETWEEN %s AND %s", (start, end)
    ).fetchone()["n"]
    parts = []
    for c in cards:
        if c.issued == 0:
            parts.append(f"Signal {c.signal}: none issued")
        else:
            prec = f"{c.precision:.0%}" if c.precision is not None else "pending"
            parts.append(
                f"Signal {c.signal}: {c.issued} issued, {c.correct} correct ({prec}); "
                f"claim {c.claim:.0%}"
            )
    notes = []
    if plans == 0:
        notes.append("No weekly plans in this period yet: the live record builds up week by week.")
    return AuditResult(
        start,
        end,
        ". ".join(parts) + ".",
        cards,
        missed_events(issued, labels),
        acts,
        counts,
        plans,
        money or {},
        live_calibration(lake, labels),
        health(conn, lake, start, end),
        proposals(cards, cfg.signals.gate.min_signals),
        notes,
    )


def lock_snapshot(conn: psycopg.Connection, audit: AuditResult) -> int:
    import json

    from stockapp.ingest.registry import pipeline_version

    row = conn.execute(
        """INSERT INTO audit_snapshots (period_start, period_end, payload, pipeline_version)
           VALUES (%s, %s, %s, %s) RETURNING snapshot_id""",
        (audit.start, audit.end, json.dumps(audit.to_json(), default=str), pipeline_version()),
    ).fetchone()
    return int(row["snapshot_id"])


def to_markdown(audit: AuditResult) -> str:
    lines = [
        f"# Monthly Audit {audit.start} to {audit.end}",
        "",
        f"**Verdict:** {audit.verdict}",
        "",
    ]
    lines += [
        "## Signal scorecard",
        "",
        "| signal | issued | matured | correct | pending | precision | lower bound | claim |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in audit.scorecards:
        prec = f"{c.precision:.1%}" if c.precision is not None else "-"
        lb = f"{c.wilson_lb:.1%}" if c.wilson_lb is not None else "-"
        lines.append(
            f"| {c.signal} | {c.issued} | {c.matured} | {c.correct} | {c.pending} | {prec} | "
            f"{lb} | {c.claim:.0%} |"
        )
    lines += ["", "## Missed events", ""]
    for s, m in audit.missed.items():
        lines.append(
            f"- Signal {s}: {m['events']} events in the universe, {m['flagged']} flagged, "
            f"{m['missed']} missed"
        )
    lines += [
        "",
        "## Your actions",
        "",
        f"Plans: {audit.plans}. Logged: {audit.action_counts or 'none'}",
    ]
    lines += ["", "## Money", ""] + [f"- {k}: {v}" for k, v in audit.money.items()]
    lines += ["", "## Data and model health", ""] + [f"- {k}: {v}" for k, v in audit.health.items()]
    lines += ["", "## Proposals", ""] + [f"- {p}" for p in audit.proposals]
    lines += ["", *[f"_{n}_" for n in audit.notes]]
    return "\n".join(lines) + "\n"
