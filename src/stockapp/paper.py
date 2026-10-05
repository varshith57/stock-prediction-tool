"""Paper trading: the app's *live* weekly picks traded with pretend money, so the live record can
be compared with the backtest before any real money follows them.

Every week the scores the app actually produced (gold ``latest_scores``, one partition per signal
week) are replayed through the same simulation as the money backtest (``strategy.simulate``):
picks at or above ``paper.cutoff``, the app's sizing and exit rules, next-open fills, charges,
tax, spare cash in the Nifty 500 (as the satellite of a core-satellite portfolio). Nothing is
stored: it is recomputed from the saved scores and prices, so it can't be edited after the fact.
The expectation it's judged against is the same strategy in the backtest (out-of-sample, 2018 on).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

import polars as pl

from stockapp.config import AppConfig
from stockapp.features.pipeline import load_weekly_samples
from stockapp.lake import Lake
from stockapp.models import money
from stockapp.strategy import Result, benchmark, benchmark_metrics, simulate


@dataclass
class Paper:
    started: date | None
    weeks: int
    result: Result | None
    index: dict | None  # Nifty 500, same money, same days
    expected: dict | None  # the same strategy in the backtest
    cutoff: float


def live_signals(lake: Lake, cfg: AppConfig) -> pl.DataFrame:
    if not lake.has_table("gold", "latest_scores"):
        return pl.DataFrame()
    scores = lake.scan("gold", "latest_scores").collect()
    market = (
        load_weekly_samples(lake)
        .group_by("trade_date")
        .agg(pl.col("mkt_sma_gap_200").first(), pl.col("vix_pct_250").first())
    )
    return (
        scores.filter(~pl.col("blocked") & pl.col("p_a").is_not_null())
        .select(
            "trade_date",
            "company_id",
            "symbol",
            pl.col("p_a").alias("p"),
            pl.col("expected_gain").fill_null(0.0),
        )
        .join(market, on="trade_date", how="left")
        .with_columns(
            (
                (pl.col("mkt_sma_gap_200") < 0)
                & (pl.col("vix_pct_250") >= cfg.risk.stress_vix_percentile)
            )
            .fill_null(False)
            .alias("stress")
        )
    )


def paper_cutoff(lake: Lake, cfg: AppConfig) -> float:
    """What the app would act on: the money test's level when it passes, else ``paper.cutoff``."""
    if cfg.signals.qualify == "money":
        g = money.latest_money_gate(lake)
        if g and g["status"] == "LIVE" and g["cutoff"] is not None:
            return float(g["cutoff"])
    return cfg.paper.cutoff


def run_paper(lake: Lake, cfg: AppConfig, with_expectation: bool = True) -> Paper:
    cut = paper_cutoff(lake, cfg)
    signals = live_signals(lake, cfg)
    if signals.is_empty():
        return Paper(None, 0, None, None, None, cut)
    started = signals["trade_date"].min()
    ids = signals.filter(pl.col("p") >= cut)["company_id"].unique().to_list() or ["-"]
    prices = money._prices(lake, ids)
    n500, n50 = money._index(lake, "nifty 500"), money._index(lake, "nifty 50")
    last = max(n50["trade_date"].max(), prices["trade_date"].max() if prices.height else started)
    sessions = [d for d in n50["trade_date"].to_list() if started <= d <= last]
    days = set(signals["trade_date"].unique().to_list())
    p = replace(  # the same rules as the money backtest
        money.base_params(cfg), cutoff=cut, weekly=0.0, initial=cfg.paper.start_inr,
        idle_in_index=True,
    )  # fmt: skip
    result = simulate(signals, prices, sessions, p, cfg.costs, n500)
    b = benchmark(n500, sessions, days, p)
    index = benchmark_metrics(b, [(sessions[0], p.initial)])
    expected = _expectation(lake, cfg, cut) if with_expectation else None
    return Paper(started, len(days), result, index, expected, cut)


def _expectation(lake: Lake, cfg: AppConfig, cut: float) -> dict | None:
    """The same strategy on the backtest's out-of-sample picks: what paper trading should look
    like if the model works live as it did in testing."""
    if not lake.has_table("gold", "oos_predictions"):
        return None
    i = money.load_inputs(lake, cfg)
    p = replace(i.base, cutoff=cut, weekly=0.0, initial=cfg.paper.start_inr, idle_in_index=True)
    r = simulate(i.signals, i.prices, i.sessions, p, cfg.costs, i.nifty500)
    weeks = len(i.signal_days)
    m = r.metrics
    return {
        "avg_trade": m["avg_trade"],
        "win_rate": m["win_rate"],
        "trades_per_week": m["trades"] / weeks if weeks else None,
        "xirr": m["xirr"],
    }
