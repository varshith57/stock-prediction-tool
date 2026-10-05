"""Gather the plan engine's inputs from the lake (scores, features, gate, quality, market) and the
app database (holdings). Kept apart from the engine so the engine stays pure and testable."""

from __future__ import annotations

import pickle
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import polars as pl
import psycopg

from stockapp.adjust import adjusted_prices_sql
from stockapp.config import AppConfig
from stockapp.features.build import FEATURE_COLUMNS
from stockapp.features.pipeline import load_weekly_samples
from stockapp.lake import Lake
from stockapp.plan.engine import Candidate, SignalGate
from stockapp.plan.reasons import reason_line, top_drivers
from stockapp.plan.rules import Drawdown, HoldingState, Regime, drawdown_state, market_regime
from stockapp.portfolio import service
from stockapp.portfolio.costs import estimated_tax, is_long_term, order_charges
from stockapp.portfolio.history import value_history
from stockapp.portfolio.store import holding_styles, list_transactions, to_ledger_txns
from stockapp.portfolio.valuation import quantity_events


def atr_14(h: pl.DataFrame) -> float | None:
    """Average true range over the last 14 sessions, in rupees, from adjusted prices (which equal
    as-traded prices at the latest date). None with fewer than 15 sessions."""
    if h.height < 15:
        return None
    prev = pl.col("adj_close").shift(1)
    tr = h.sort("trade_date").select(
        pl.max_horizontal(
            pl.col("adj_high") - pl.col("adj_low"),
            (pl.col("adj_high") - prev).abs(),
            (pl.col("adj_low") - prev).abs(),
        ).alias("tr")
    )["tr"]
    return float(tr.tail(14).mean())


def next_monday(d: date) -> date:
    return d + timedelta(days=(7 - d.weekday()) % 7 or 7)


def _latest_model(models_dir: Path, signal: str) -> dict | None:
    from stockapp.features.pipeline import FEATURE_VERSION
    from stockapp.models.files import newest_model

    path = newest_model(models_dir, f"signal_{signal}", FEATURE_VERSION)
    if path is None:
        return None
    with path.open("rb") as f:
        return pickle.load(f)


def gates(lake: Lake, cfg: AppConfig | None = None) -> dict[str, SignalGate]:
    """LIVE/OFF per signal. With ``signals.qualify = money`` (the default), buy ideas (A) qualify
    by the money test (``models.money``: beat the index after costs) instead of 90% accuracy."""
    out: dict[str, SignalGate] = {}
    if lake.has_table("gold", "signal_gate"):
        g = lake.scan("gold", "signal_gate").collect()
        g = g.filter(pl.col("built") == pl.col("built").max())
        out = {
            r["signal"]: SignalGate(r["status"], r["cutoff"], r["reason"])
            for r in g.iter_rows(named=True)
        }
    if cfg is not None and cfg.signals.qualify == "money":
        from stockapp.models.money import latest_money_gate

        m = latest_money_gate(lake)
        out["A"] = (
            SignalGate(m["status"], m["cutoff"], m["reason"])
            if m
            else SignalGate("OFF", None, "not tested for money yet: run stockapp models money")
        )
    return out


def candidates(lake: Lake, signal_date: date) -> tuple[list[Candidate], dict, str | None]:
    """Universe members on ``signal_date`` with scores, reasons and filters. Also returns the
    market row (for the regime) and the model version."""
    samples = load_weekly_samples(lake).filter(pl.col("trade_date") == signal_date)
    scores = (
        lake.scan("gold", "latest_scores").collect().filter(pl.col("trade_date") == signal_date)
    )
    prices = lake.scan("gold", "latest_prices").collect()
    prices = prices.filter(pl.col("built") == pl.col("built").max()).select("company_id", "close")
    df = samples.join(
        scores.select("company_id", "p_a", "p_c", "expected_gain", "model_version"),
        on="company_id",
        how="left",
    ).join(prices, on="company_id", how="left")
    x = df.select(pl.col(FEATURE_COLUMNS).cast(pl.Float32)).to_numpy()
    reasons: dict[str, list[str]] = {"A": [""] * df.height, "C": [""] * df.height}
    models_dir = lake.root.parent / "models"
    for signal in ("A", "C"):
        m = _latest_model(models_dir, signal)
        if m is None:
            continue
        contrib = m["model"].booster_.predict(x, pred_contrib=True)
        for i, row in enumerate(df.iter_rows(named=True)):
            drivers = top_drivers(np.asarray(contrib[i]), FEATURE_COLUMNS)
            reasons[signal][i] = reason_line(drivers, row)
    out = []
    for i, r in enumerate(df.iter_rows(named=True)):
        close = r["close"]
        if close is None:
            continue
        out.append(
            Candidate(
                company_id=r["company_id"],
                symbol=r["symbol"],
                p_a=r["p_a"],
                p_c=r["p_c"],
                expected_gain=r["expected_gain"],
                last_close=close,
                atr_14=(r["atr_14_pct"] * close) if r["atr_14_pct"] is not None else None,
                blocked=bool(r["blocked"]),
                trade_for_trade=bool(r["is_t2t"]),
                sessions_listed=int(r["sessions_since_break"] or 0),
                reason_a=reasons["A"][i],
                reason_c=reasons["C"][i],
            )
        )
    market = df.select("mkt_sma_gap_200", "vix_pct_250").head(1).to_dicts()
    model_version = df["model_version"].drop_nulls().head(1).to_list()
    return out, (market[0] if market else {}), (model_version[0] if model_version else None)


def holdings(
    conn: psycopg.Connection, lake: Lake, cfg: AppConfig, today: date
) -> tuple[list[HoldingState], dict[str, float], Drawdown, float]:
    """Holding states for the rules, weights, drawdown from the time-weighted index, and the
    accumulated budget (weekly budget x weeks since the last buy, at least one week)."""
    loaded = service.load(conn, lake, cfg, today)
    lots = loaded.ledger.lots
    rows = list_transactions(conn)
    styles = holding_styles(conn)
    weekly = cfg.budget.weekly_inr
    buys = [r for r in rows if r["side"] == "BUY"]
    last_buy = max((r["trade_date"] or r["created_at"].date() for r in buys), default=None)
    weeks = max(1, (today - last_buy).days // 7) if last_buy else 1
    budget = weekly * weeks
    if not lots:
        return [], {}, Drawdown(None, False, False), budget

    company_ids = sorted({lot.company_id for lot in lots})
    ids = ", ".join(f"'{c}'" for c in company_ids)
    hist = duckdb.sql(
        f"""SELECT company_id, trade_date, close, adj_close, adj_high, adj_low
            FROM ({adjusted_prices_sql(lake)}) WHERE company_id IN ({ids}) ORDER BY 1, 2"""
    ).pl()
    weights = {
        r["company_id"]: (r["weight_pct"] or 0) / 100
        for r in loaded.view.holdings.iter_rows(named=True)
    }
    states = []
    for cid in company_ids:
        h = hist.filter(pl.col("company_id") == cid)
        if h.is_empty():
            continue
        atr = atr_14(h)
        mine = [lot for lot in lots if lot.company_id == cid]
        qty = sum(lot.quantity for lot in mine)
        cost = sum(lot.cost for lot in mine)
        first_buy = min((lot.buy_date for lot in mine if lot.buy_date), default=None)
        since = h.filter(pl.col("trade_date") >= first_buy) if first_buy else h.tail(0)
        last = float(h["close"][-1])
        sell = order_charges("SELL", qty, last, cfg.costs).total
        gain = qty * last - sell - cost
        tax = estimated_tax(gain, is_long_term(first_buy, today), cfg.tax) or 0.0
        symbol = next(
            (
                r["symbol"]
                for r in loaded.view.holdings.iter_rows(named=True)
                if r["company_id"] == cid
            ),
            cid,
        )
        states.append(
            HoldingState(
                company_id=cid,
                symbol=symbol,
                cost_per_share=cost / qty,
                last_close=last,
                atr_14=atr,
                peak_close_since_buy=float(since["adj_close"].max()) if since.height else None,
                sessions_held=since.height if first_buy else None,
                net_profit_if_sold=gain - tax,
                quantity=qty,
                opened_by_signal_a=styles.get(cid) == "trade",
            )
        )

    idx = lake.duckdb_glob("silver", "nse_index_close")
    nifty = duckdb.sql(
        f"""SELECT trade_date, close FROM read_parquet('{idx}', hive_partitioning = true)
            WHERE lower(index_name) = 'nifty 50' ORDER BY 1"""
    ).pl()
    history = value_history(
        to_ledger_txns(rows, lambda s, q, p: order_charges(s, q, p, cfg.costs).total),
        quantity_events(lake, company_ids),
        hist.select("company_id", "trade_date", "close"),
        nifty,
    )
    return states, weights, drawdown_state(history["twr_index"].to_list(), cfg), budget


def regime(market: dict, cfg: AppConfig) -> Regime:
    return market_regime(market.get("mkt_sma_gap_200"), market.get("vix_pct_250"), cfg)


def quality_for(lake: Lake, signal_date: date) -> float | None:
    if not lake.has_table("gold", "quality_daily"):
        return None
    q = lake.scan("gold", "quality_daily").collect()
    q = q.filter((pl.col("built") == pl.col("built").max()) & (pl.col("trade_date") == signal_date))
    return None if q.is_empty() else float(q["score"][0])


def safety(lake: Lake, signal_date: date) -> tuple[dict[str, float], SignalGate | None]:
    """This week's monthly drop warnings for holdings and their test verdict."""
    from stockapp.models.safety import latest_safety

    scores, g = latest_safety(lake, signal_date)
    gate = SignalGate(g["status"], g["cutoff"], g["reason"]) if g else None
    return scores, gate
