"""Build the weekly training/scoring set from the lake.

Sample = (company, signal date T) where T is the last session of its week and the company is in
that month's universe (point-in-time membership). Each sample carries the feature vector at T,
labels A and C (null for the latest weeks, which are still unresolved and are what gets scored),
and ``blocked``: a BLOCK quality flag covers T or the label window, so the sample is excluded
from training and from signals.

Written to gold ``weekly_samples`` (one partition per year) with ``feature_version`` = a hash of
the feature catalogue, so a changed definition is never mixed with old rows.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime

import duckdb
import polars as pl

from stockapp.adjust import BREAKS, _latest, adjusted_prices_sql
from stockapp.config import AppConfig
from stockapp.features.build import (
    FEATURE_COLUMNS,
    FEATURES,
    LABELS,
    add_cross_section,
    compute_features,
    compute_labels,
    label_span_days,
    market_features,
)
from stockapp.ingest.registry import pipeline_version
from stockapp.lake import Lake
from stockapp.master import company_delivery_sql
from stockapp.quality.gates import flags_sql
from stockapp.universe import DATASET as UNIVERSE

DATASET = "weekly_samples"
FEATURE_VERSION = hashlib.sha256(json.dumps(FEATURES, sort_keys=True).encode()).hexdigest()[:12]


def load_panel(lake: Lake) -> pl.DataFrame:
    uni = lake.duckdb_glob("silver", UNIVERSE)
    return duckdb.sql(
        f"""
        WITH ever AS (SELECT DISTINCT company_id
                      FROM read_parquet('{uni}', hive_partitioning = true)),
        a AS (SELECT * FROM ({adjusted_prices_sql(lake)}) WHERE company_id IN (SELECT * FROM ever)),
        d AS (SELECT company_id, trade_date, delivery_pct FROM ({company_delivery_sql(lake)})
              WHERE company_id IN (SELECT * FROM ever)),
        b AS ({_latest(lake, BREAKS)})
        SELECT a.company_id, a.symbol, a.series, a.trade_date, a.close, a.adj_open, a.adj_high,
               a.adj_low, a.adj_close, a.value_inr, d.delivery_pct,
               (SELECT count(*) FROM b WHERE b.company_id = a.company_id
                  AND b.break_date <= a.trade_date) AS segment
        FROM a LEFT JOIN d USING (company_id, trade_date)
        """
    ).pl()


def load_market(lake: Lake) -> pl.DataFrame:
    idx = lake.duckdb_glob("silver", "nse_index_close")
    raw = duckdb.sql(
        f"""SELECT trade_date,
                   max(CASE WHEN lower(index_name) = 'nifty 500' THEN close END) AS nifty500,
                   max(CASE WHEN lower(index_name) = 'india vix' THEN close END) AS vix
            FROM read_parquet('{idx}', hive_partitioning = true) GROUP BY 1"""
    ).pl()
    return market_features(raw)


def weekly_dates(panel: pl.DataFrame) -> pl.DataFrame:
    sessions = panel.select("trade_date").unique().sort("trade_date")
    return (
        sessions.with_columns(pl.col("trade_date").dt.truncate("1w").alias("_week"))
        .group_by("_week")
        .agg(pl.col("trade_date").max())
        .select("trade_date")
        .sort("trade_date")
    )


def build_weekly_samples(lake: Lake, cfg: AppConfig, as_of: date) -> pl.DataFrame:
    panel = load_panel(lake)
    feats = compute_features(panel, load_market(lake))
    labels = compute_labels(
        panel, cfg.signals.gain_threshold, cfg.signals.crash_threshold,
        cfg.signals.window_trading_days,
    )  # fmt: skip
    weeks = weekly_dates(panel)
    uni = lake.scan("silver", UNIVERSE).select("month", "company_id", "symbol").collect()

    samples = (
        feats.join(weeks, on="trade_date", how="semi")
        .with_columns(pl.col("trade_date").dt.strftime("%Y-%m").alias("month"))
        .join(uni.select("month", "company_id"), on=["month", "company_id"], how="semi")
        .join(labels, on=["company_id", "segment", "trade_date"], how="left")
        .join(
            panel.select("company_id", "trade_date", "symbol"),
            on=["company_id", "trade_date"],
            how="left",
        )
    )
    samples = add_cross_section(samples)

    blocked = blocked_samples(lake, samples, label_span_days(cfg.signals.window_trading_days))
    samples = samples.join(blocked, on=["company_id", "trade_date"], how="left").with_columns(
        pl.col("blocked").fill_null(False),
        pl.lit(FEATURE_VERSION).alias("feature_version"),
        pl.lit(datetime.now(UTC)).alias("_built_at"),
        pl.lit(pipeline_version()).alias("_pipeline_version"),
    )
    cols = ["company_id", "symbol", "trade_date", "month", "segment", *FEATURE_COLUMNS, *LABELS,
            "blocked", "feature_version", "_built_at", "_pipeline_version"]  # fmt: skip
    samples = samples.select(cols).sort("trade_date", "company_id")
    for (year,), part in (
        samples.with_columns(pl.col("trade_date").dt.year().alias("_y"))
        .partition_by("_y", as_dict=True)
        .items()
    ):
        lake.write_partition("gold", DATASET, "year", str(year), part.drop("_y"))
    return samples


def blocked_samples(lake: Lake, samples: pl.DataFrame, days: int) -> pl.DataFrame:
    """(company_id, trade_date, blocked=True) for samples with a BLOCK quality flag overlapping
    [T, T + days]: the label window, so the label may rest on bad prices."""
    con = duckdb.connect()
    con.register("s", samples.select("company_id", "trade_date"))
    return (
        con.sql(
            f"""SELECT DISTINCT s.company_id, s.trade_date FROM s JOIN ({flags_sql(lake)}) f
            ON f.company_id = s.company_id AND f.severity = 'BLOCK'
           AND f.from_date <= s.trade_date + INTERVAL {int(days)} DAY
           AND f.to_date >= s.trade_date"""
        )
        .pl()
        .with_columns(pl.lit(True).alias("blocked"))
    )


def load_weekly_samples(lake: Lake) -> pl.DataFrame:
    df = lake.scan("gold", DATASET).collect()
    return df.filter(pl.col("feature_version") == FEATURE_VERSION).drop("year")
