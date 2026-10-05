"""Run the money backtest (``stockapp.strategy``) on the model's stored out-of-sample picks for a
range of confidence cutoffs and two money scenarios, against the Nifty 50 and Nifty 500, and write
a plain-English report (local only, never committed)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

import duckdb
import polars as pl

from stockapp.adjust import adjusted_prices_sql
from stockapp.config import AppConfig
from stockapp.features.pipeline import load_weekly_samples
from stockapp.lake import Lake
from stockapp.models.run import stored_predictions
from stockapp.strategy import Params, benchmark, benchmark_metrics, simulate

CUTOFFS = (0.0, 0.10, 0.15, 0.20, 0.25, 0.30, 0.90)


@dataclass
class MoneyRun:
    table: pl.DataFrame
    report_path: Path


def _signals(lake: Lake, cfg: AppConfig) -> pl.DataFrame:
    preds = stored_predictions(lake, "A").select(
        "trade_date", "company_id", "symbol", "p", pl.col("expected_gain").fill_null(0.0)
    )
    market = (
        load_weekly_samples(lake)
        .group_by("trade_date")
        .agg(pl.col("mkt_sma_gap_200").first(), pl.col("vix_pct_250").first())
    )
    return preds.join(market, on="trade_date", how="left").with_columns(
        (
            (pl.col("mkt_sma_gap_200") < 0)
            & (pl.col("vix_pct_250") >= cfg.risk.stress_vix_percentile)
        )
        .fill_null(False)
        .alias("stress")
    )


def _prices(lake: Lake, company_ids: list[str]) -> pl.DataFrame:
    ids = ", ".join(f"'{c}'" for c in company_ids)
    raw = duckdb.sql(
        f"""SELECT company_id, trade_date, adj_open AS open, adj_high AS high, adj_low AS low,
                   adj_close AS close
            FROM ({adjusted_prices_sql(lake)}) WHERE company_id IN ({ids})"""
    ).pl()
    prev = pl.col("close").shift(1).over("company_id")
    return (
        raw.sort("company_id", "trade_date")
        .with_columns(
            pl.max_horizontal(
                pl.col("high") - pl.col("low"),
                (pl.col("high") - prev).abs(),
                (pl.col("low") - prev).abs(),
            ).alias("tr")
        )
        .with_columns(pl.col("tr").rolling_mean(14).over("company_id").alias("atr"))
        .select("company_id", "trade_date", "open", "close", "atr")
        .filter(pl.col("open").is_not_null() & pl.col("close").is_not_null())
    )


def _index(lake: Lake, name: str) -> pl.DataFrame:
    glob = lake.duckdb_glob("silver", "nse_index_close")
    return duckdb.sql(
        f"""SELECT trade_date, close FROM read_parquet('{glob}', hive_partitioning = true)
            WHERE lower(index_name) = '{name}' ORDER BY 1"""
    ).pl()


@dataclass
class Inputs:
    signals: pl.DataFrame
    prices: pl.DataFrame
    sessions: list[date]
    signal_days: set[date]
    nifty50: pl.DataFrame
    nifty500: pl.DataFrame
    base: Params


def base_params(cfg: AppConfig) -> Params:
    """The app's own trading rules as backtest parameters (cutoff and money set per run)."""
    s = cfg.signals
    return Params(
        cutoff=0.0,
        weekly=cfg.budget.weekly_inr,
        max_opps=s.max_opportunities,
        max_stock_weight=cfg.risk.max_stock_weight,
        min_position=cfg.budget.min_position_inr,
        gain=s.gain_threshold,
        stop_atr=cfg.risk.stop_atr_multiple,
        window=s.window_trading_days,
        slippage_bps=cfg.costs.slippage_bps,
        stcg_rate=cfg.tax.stcg_rate,
        drawdown_pause=cfg.risk.drawdown_pause,
        drawdown_lookback=cfg.risk.drawdown_lookback_days,
    )


def load_inputs(lake: Lake, cfg: AppConfig) -> Inputs:
    signals = _signals(lake, cfg)
    s = cfg.signals
    # only the stocks any tested cutoff could buy (a few per week), not the whole universe
    wanted = (
        signals.with_columns(
            pl.col("expected_gain").rank("ordinal", descending=True).over("trade_date").alias("r")
        )
        .filter((pl.col("r") <= s.max_opportunities * 4) | (pl.col("p") >= min(CUTOFFS[1:])))
        .get_column("company_id")
        .unique()
        .to_list()
    )
    prices = _prices(lake, wanted)
    nifty50, nifty500 = _index(lake, "nifty 50"), _index(lake, "nifty 500")
    start, end = signals["trade_date"].min(), prices["trade_date"].max()
    sessions = [d for d in nifty50["trade_date"].to_list() if start <= d <= end]
    base = base_params(cfg)
    days = set(signals["trade_date"].unique().to_list())
    return Inputs(signals, prices, sessions, days, nifty50, nifty500, base)


def run_money_backtest(lake: Lake, cfg: AppConfig, today: date) -> MoneyRun:
    i = load_inputs(lake, cfg)
    signals, prices, sessions, signal_days = i.signals, i.prices, i.sessions, i.signal_days
    nifty50, nifty500, base = i.nifty50, i.nifty500, i.base
    scenarios = {
        f"₹{cfg.budget.weekly_inr:,.0f} a week (your budget)": base,
        "₹1,00,000 at the start": replace(base, weekly=0.0, initial=100_000.0),
    }
    rows = []
    for scenario, p0 in scenarios.items():
        for name, idx in (("Nifty 50", nifty50), ("Nifty 500", nifty500)):
            b = benchmark(idx, sessions, signal_days, p0)
            flows = [(sessions[0], p0.initial)] if p0.initial else []
            flows += [(d, p0.weekly) for d in sessions if d in signal_days and p0.weekly]
            rows.append({"scenario": scenario, "strategy": f"{name} index (same money)",
                         **benchmark_metrics(b, flows)})  # fmt: skip
        for cut in CUTOFFS:
            r = simulate(signals, prices, sessions, replace(p0, cutoff=cut), cfg.costs)
            label = "top 5 every week" if cut == 0 else f"buy at {cut:.0%}+ chance"
            m = dict(r.metrics)
            m["exits"] = " · ".join(f"{k} {v}" for k, v in sorted(m["exits"].items()))
            rows.append({"scenario": scenario, "strategy": label, **m})
        for cut in (0.0, 0.15, 0.20):
            p = replace(p0, cutoff=cut, idle_in_index=True)
            r = simulate(signals, prices, sessions, p, cfg.costs, nifty500)
            label = "top 5 every week" if cut == 0 else f"buy at {cut:.0%}+ chance"
            m = dict(r.metrics)
            m["exits"] = " · ".join(f"{k} {v}" for k, v in sorted(m["exits"].items()))
            rows.append(
                {"scenario": scenario, "strategy": f"{label}, spare cash in Nifty 500", **m}
            )
    table = pl.DataFrame(rows, infer_schema_length=None, strict=False)
    path = _report(lake, cfg, today, table, sessions[0], sessions[-1])
    return MoneyRun(table, path)


def _fmt(v, kind: str) -> str:
    if v is None:
        return "—"
    if kind == "pct":
        return f"{v:+.1%}"
    if kind == "inr":
        return f"₹{v:,.0f}"
    if kind == "share":
        return f"{v:.0%}"
    return str(v)


def _report(lake, cfg, today, table: pl.DataFrame, start: date, end: date) -> Path:
    reports = lake.root.parent / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"money_backtest_{today.isoformat()}.md"
    s = cfg.signals
    lines = [
        f"# Money backtest ({today})\n",
        f"Following the app's buy ideas from {start:%d %b %Y} to {end:%d %b %Y}, on the model's "
        f"out-of-sample picks (question: +{s.gain_threshold:.0%} within "
        f"{s.window_trading_days} market days). Rules as in the app: up to "
        f"{s.max_opportunities} buys a week by expected gain, per-stock cap "
        f"{cfg.risk.max_stock_weight:.0%}, minimum position ₹{cfg.budget.min_position_inr:,.0f}, "
        f"exit at +{s.gain_threshold:.0%}, the {cfg.risk.stop_atr_multiple:g} x ATR stop-loss or "
        f"after {s.window_trading_days} days; next-open fills, {cfg.costs.slippage_bps:g} bps "
        f"slippage, Zerodha charges, {cfg.tax.stcg_rate:.0%} short-term tax; new buys pause "
        f"while {-cfg.risk.drawdown_pause:.0%} below the best value of the last "
        f"{cfg.risk.drawdown_lookback_days} market days. 'Spare cash in Nifty 500' rows "
        "keep uninvested money in the index between trades, moved at no cost or tax "
        "(optimistic). Index rows: same "
        "rupees on the same days into the price index (no dividends: understates a real index "
        "fund by ~1-1.5% a year).\n",
    ]
    for scenario in table["scenario"].unique(maintain_order=True):
        t = table.filter(pl.col("scenario") == scenario)
        lines += [
            f"\n## {scenario}\n",
            "| strategy | end value | put in | yearly return (XIRR) | worst fall | trades | "
            "won | avg trade | fees | tax | time invested |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in t.iter_rows(named=True):
            lines.append(
                f"| {r['strategy']} | {_fmt(r['final_value'], 'inr')} | "
                f"{_fmt(r['contributed'], 'inr')} | {_fmt(r['xirr'], 'pct')} | "
                f"{_fmt(r['max_drawdown'], 'pct')} | {_fmt(r.get('trades'), '')} | "
                f"{_fmt(r.get('win_rate'), 'share')} | {_fmt(r.get('avg_trade'), 'pct')} | "
                f"{_fmt(r.get('fees'), 'inr')} | {_fmt(r.get('taxes'), 'inr')} | "
                f"{_fmt(r.get('time_invested'), 'share')} |"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- the money gate: do buy ideas beat the index after costs? ------------------------------------

GATE_CUTOFFS = (0.10, 0.15, 0.20, 0.25, 0.30)
GATE_START = 100_000.0  # a lump sum isolates the strategy from the timing of contributions


@dataclass(frozen=True)
class MoneyGate:
    status: str  # LIVE or OFF
    cutoff: float | None
    reason: str
    xirr: float | None
    xirr_stress: float | None
    index_xirr: float | None
    max_drawdown: float | None
    index_drawdown: float | None
    years_beaten: int
    years: int
    tested_from: date
    tested_to: date

    def as_row(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def yearly_returns(daily: pl.DataFrame) -> dict[int, float]:
    """Calendar-year returns from a time-weighted index (``twr``)."""
    y = (
        daily.with_columns(pl.col("trade_date").dt.year().alias("y"))
        .group_by("y")
        .agg(pl.col("twr").last())
        .sort("y")
    )
    out, prev = {}, 1.0
    for year, twr in y.iter_rows():
        out[year] = twr / prev - 1
        prev = twr
    return out


def judge_cutoffs(results: dict[float, dict], index: dict, cfg: AppConfig) -> MoneyGate:
    """``results``: cutoff -> {xirr, xirr_stress, max_drawdown, years: {year: return}};
    ``index``: {xirr, max_drawdown, years, tested_from, tested_to}. Pure: unit-tested."""
    m = cfg.signals.money
    beats = {c: (r["xirr"] or -1) > (index["xirr"] or 0) for c, r in results.items()}
    cuts = sorted(results)
    rows = []
    for i, c in enumerate(cuts):
        r = results[c]
        years = [y for y in r["years"] if y in index["years"]]
        won = sum(r["years"][y] > index["years"][y] for y in years)
        neighbours = [cuts[j] for j in (i - 1, i + 1) if 0 <= j < len(cuts)]
        ok = (
            beats[c]
            and (r["xirr_stress"] or -1) > (index["xirr"] or 0)
            and r["max_drawdown"] >= index["max_drawdown"] - m.max_extra_drawdown
            and years
            and won / len(years) >= m.min_share_of_years
            and any(beats[n] for n in neighbours)
        )
        rows.append((c, r, won, len(years), bool(ok)))
    passing = [x for x in rows if x[4]]
    pick = max(passing or rows, key=lambda x: x[1]["xirr_stress"] or -1)
    c, r, won, n, ok = pick

    def pct(v):
        return "n/a" if v is None else f"{v:+.1%}"

    detail = (
        f"buy at {c:.0%}+ made {pct(r['xirr'])} a year vs the Nifty 500's "
        f"{pct(index['xirr'])} ({pct(r['xirr_stress'])} with "
        f"{m.stress_slippage_bps:g} bps slippage), beat it in {won} of {n} years, worst fall "
        f"{r['max_drawdown']:.0%} vs {index['max_drawdown']:.0%}"
    )
    common = dict(
        xirr=r["xirr"], xirr_stress=r["xirr_stress"], index_xirr=index["xirr"],
        max_drawdown=r["max_drawdown"], index_drawdown=index["max_drawdown"],
        years_beaten=won, years=n, tested_from=index["tested_from"],
        tested_to=index["tested_to"],
    )  # fmt: skip
    if ok:
        return MoneyGate("LIVE", c, "proven: " + detail, **common)
    return MoneyGate("OFF", None, "not proven to beat an index fund after costs. Best: " + detail,
                     **common)  # fmt: skip


def evaluate_money_gate(lake: Lake, cfg: AppConfig, today: date) -> MoneyGate:
    """Run the money backtest per cutoff (lump sum, spare cash in the index, as the satellite of
    a core-satellite portfolio would be), judge it, and store the verdict in gold ``money_gate``.
    """
    i = load_inputs(lake, cfg)
    p0 = replace(i.base, weekly=0.0, initial=GATE_START, idle_in_index=True)
    b = benchmark(i.nifty500, i.sessions, i.signal_days, p0)
    bm = benchmark_metrics(b, [(i.sessions[0], GATE_START)])
    index = {
        "xirr": bm["xirr"], "max_drawdown": bm["max_drawdown"], "years": yearly_returns(b),
        "tested_from": i.sessions[0], "tested_to": i.sessions[-1],
    }  # fmt: skip
    stress = max(cfg.signals.money.stress_slippage_bps, cfg.costs.slippage_bps)
    results = {}
    for c in GATE_CUTOFFS:
        r = simulate(i.signals, i.prices, i.sessions, replace(p0, cutoff=c), cfg.costs, i.nifty500)
        rs = simulate(
            i.signals, i.prices, i.sessions, replace(p0, cutoff=c, slippage_bps=stress),
            cfg.costs, i.nifty500,
        )  # fmt: skip
        results[c] = {
            "xirr": r.metrics["xirr"], "xirr_stress": rs.metrics["xirr"],
            "max_drawdown": r.metrics["max_drawdown"], "years": yearly_returns(r.daily),
        }  # fmt: skip
    gate = judge_cutoffs(results, index, cfg)
    lake.write_partition(
        "gold", "money_gate", "built", today.isoformat(),
        pl.DataFrame([gate.as_row()]).with_columns(pl.lit("A").alias("signal")),
    )  # fmt: skip
    return gate


def latest_money_gate(lake: Lake) -> dict | None:
    if not lake.has_table("gold", "money_gate"):
        return None
    g = lake.scan("gold", "money_gate").collect()
    g = g.filter(pl.col("built") == pl.col("built").max())
    return g.row(0, named=True) if g.height else None
