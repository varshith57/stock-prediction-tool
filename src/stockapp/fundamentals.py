"""Fundamentals backfill: download and parse the XBRL file of every quarterly result for stocks
that have been in the universe, from 2016 on.

* Targets: one filing per company and quarter, consolidated when the company files both, the
  first publication (not revisions). Sources: the older results feed (silver
  ``nse_financial_results``, matched by ISIN then symbol) and integrated Financials filings
  (silver ``nse_integrated_results``, matched by symbol). Filings without an XBRL link are skipped.
* Each file is kept gzipped in bronze (``bronze/nse_xbrl/<year>/``), parsed
  (``ingest.xbrl.parse_results_xbrl``), and its row written to silver ``nse_fundamentals`` in
  batches. Postgres ``xbrl_fetches`` records every URL tried, so a stop (lid closed, network
  drop, Ctrl-C) loses at most one batch and the next run resumes.
* Polite: one request per ``interval`` seconds to NSE's archive host. Network failures pause and
  retry instead of ending the run.
"""

from __future__ import annotations

import gzip
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import duckdb
import polars as pl
import psycopg

from stockapp.features.events import map_company, map_symbol
from stockapp.ingest.http import FetchError, NotAvailable, PoliteClient
from stockapp.ingest.xbrl import parse_results_xbrl
from stockapp.lake import Lake
from stockapp.master import company_map_sql, company_prices_sql

DATASET = "nse_fundamentals"
BATCH = 200


def targets(lake: Lake) -> pl.DataFrame:
    """company_id, period_to, consolidated, published_at, xbrl_url: one per company-quarter."""
    universe = lake.scan("silver", "universe_top500").select("company_id").unique().collect()
    isin_map = duckdb.sql(
        f"SELECT DISTINCT isin, company_id FROM ({company_prices_sql(lake)}) WHERE isin IS NOT NULL"
    ).pl()
    symbol_map = duckdb.sql(company_map_sql(lake)).pl()
    cols = ["company_id", "period_to", "consolidated", "published_at", "xbrl_url"]
    old = map_company(
        lake.scan("silver", "nse_financial_results")
        .select("symbol", "isin", "period_to", "consolidated", "published_at", "xbrl_url")
        .collect(),
        isin_map,
        symbol_map,
        "published_at",
    ).select(cols)
    frames = [old]
    if lake.has_table("silver", "nse_integrated_results"):
        new = map_symbol(
            lake.scan("silver", "nse_integrated_results")
            .filter(
                pl.col("filing_type").str.contains("(?i)financials")
                & ~pl.col("filing_sub").str.contains("(?i)revision")
            )
            .select("symbol", "period_to", "consolidated", "published_at", "xbrl_url")
            .collect(),
            symbol_map,
            "published_at",
        ).select(cols)
        frames.append(new)
    all_ = pl.concat(frames, how="vertical_relaxed").filter(
        pl.col("xbrl_url").str.ends_with(".xml") & pl.col("period_to").is_not_null()
    )
    all_ = all_.join(universe, on="company_id", how="semi").with_columns(
        (pl.col("consolidated").str.to_lowercase() == "consolidated").alias("is_consolidated")
    )
    # a filing that maps to two company ids (a rename the master kept as two companies) belongs
    # to the one that was trading when it was published
    trading = symbol_map.group_by("company_id").agg(
        pl.col("first_traded").min(), pl.col("last_traded").max()
    )
    all_ = (
        all_.join(trading, on="company_id", how="left")
        .with_columns(
            (
                (pl.col("published_at").dt.date() >= pl.col("first_traded"))
                & (
                    pl.col("published_at").dt.date()
                    <= pl.col("last_traded") + pl.duration(days=120)
                )
            )
            .fill_null(False)
            .alias("_trading")
        )
        .sort("_trading", descending=True)
        .unique(["xbrl_url"], keep="first", maintain_order=True)
        .drop("first_traded", "last_traded", "_trading")
    )
    # consolidated first, then the first publication
    return (
        all_.sort(
            ["company_id", "period_to", "is_consolidated", "published_at"],
            descending=[False, False, True, False],
        )
        .group_by(["company_id", "period_to"], maintain_order=True)
        .first()
        .select(*cols, "is_consolidated")
        .sort("published_at")
    )


@dataclass
class Progress:
    total: int
    done: int = 0
    parsed: int = 0
    started: float = 0.0

    def line(self) -> str:
        rate = self.done / max(time.monotonic() - self.started, 1e-9)
        left = (self.total - self.done) / rate / 3600 if rate else float("inf")
        return (
            f"{self.done}/{self.total} files ({self.parsed} parsed) · {rate * 60:.0f}/min · "
            f"about {left:.1f} h left"
        )


def _record(conn: psycopg.Connection, rows: list[tuple]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO xbrl_fetches (url, company_id, period_to, status, detail)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (url) DO UPDATE
               SET status = EXCLUDED.status, detail = EXCLUDED.detail, fetched_at = now()""",
            rows,
        )


def backfill(
    conn: psycopg.Connection,
    lake: Lake,
    *,
    interval: float = 1.0,
    limit: int | None = None,
    log: Callable[[str], None] = print,
) -> Progress:
    todo = targets(lake)
    seen = {r["url"] for r in conn.execute("SELECT url FROM xbrl_fetches").fetchall()}
    todo = todo.filter(~pl.col("xbrl_url").is_in(list(seen)))
    if limit:
        todo = todo.head(limit)
    prog = Progress(todo.height, started=time.monotonic())
    log(f"fundamentals: {todo.height} files to fetch ({len(seen)} already done)")
    bronze = lake.root / "bronze" / "nse_xbrl"
    rows: list[dict] = []
    fetches: list[tuple] = []
    http = PoliteClient(min_interval_s=interval)

    def flush() -> None:
        if rows:
            key = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            lake.write_partition("silver", DATASET, "batch", key, pl.DataFrame(rows))
            rows.clear()
        _record(conn, fetches)  # after the rows are safely written
        fetches.clear()

    try:
        for t in todo.iter_rows(named=True):
            url, cid, q = t["xbrl_url"], t["company_id"], t["period_to"]
            while True:
                try:
                    content = http.get(url).content
                    break
                except NotAvailable:
                    content = None
                    break
                except FetchError as exc:  # network down or breaker open: wait, then retry
                    log(f"network trouble ({type(exc).__name__}); retrying in 2 minutes")
                    time.sleep(120)
                    http.close()
                    http = PoliteClient(min_interval_s=interval)
            prog.done += 1
            if content is None:
                fetches.append((url, cid, q, "missing", "404"))
            else:
                path = bronze / str(q.year) / (url.rsplit("/", 1)[-1] + ".gz")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(gzip.compress(content))
                try:
                    parsed = parse_results_xbrl(content, q)
                except Exception as exc:  # malformed XML: keep the file, record why
                    parsed = None
                    fetches.append((url, cid, q, "error", f"{type(exc).__name__}: {exc}"[:200]))
                else:
                    if parsed is None:
                        fetches.append((url, cid, q, "no_quarter", None))
                if parsed is not None:
                    prog.parsed += 1
                    rows.append(
                        {
                            "company_id": cid,
                            "period_to": q,
                            "published_at": t["published_at"],
                            "consolidated": t["is_consolidated"],
                            "xbrl_url": url,
                            **parsed,
                            "_fetched_at": datetime.now(UTC),
                        }
                    )
                    fetches.append((url, cid, q, "parsed", None))
            if len(fetches) >= BATCH:
                flush()
                log(prog.line())
    finally:
        flush()
        http.close()
    log("fundamentals backfill finished: " + prog.line())
    return prog


def load_fundamentals(lake: Lake) -> pl.DataFrame:
    """One row per company and quarter (the latest fetch wins if a file was fetched twice)."""
    if not lake.has_table("silver", DATASET):
        return pl.DataFrame()
    df = lake.scan("silver", DATASET).collect()
    return df.sort("_fetched_at").unique(["company_id", "period_to"], keep="last")
