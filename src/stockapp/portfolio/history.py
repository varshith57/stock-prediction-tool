"""Daily portfolio value, a time-weighted index (for drawdown) and a Nifty 50 benchmark that gets
exactly the same cash flows (for the "vs Nifty 50" chart). Uses as-traded closes; quantities
follow the ledger, including bonus/split adjustments on their ex-dates."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

import polars as pl

from stockapp.portfolio.ledger import QuantityEvent, Txn


def value_history(
    txns: list[Txn],
    events: list[QuantityEvent],
    prices: pl.DataFrame,  # company_id, trade_date, close (as traded)
    benchmark: pl.DataFrame,  # trade_date, close (Nifty 50)
) -> pl.DataFrame:
    """One row per session from the first trade: value, net_flow (cash in on that day: buys
    minus sale proceeds), twr_index (time-weighted, starts at 1), benchmark_value."""
    if not txns:
        return pl.DataFrame(
            schema={"trade_date": pl.Date, "value": pl.Float64, "net_flow": pl.Float64,
                    "twr_index": pl.Float64, "benchmark_value": pl.Float64}
        )  # fmt: skip
    start = min(t.effective_date for t in txns)
    sessions = benchmark.filter(pl.col("trade_date") >= start).sort("trade_date")
    closes: dict[str, dict[date, float]] = defaultdict(dict)
    for cid, d, c in prices.select("company_id", "trade_date", "close").iter_rows():
        closes[cid][d] = c
    by_day_txn: dict[date, list[Txn]] = defaultdict(list)
    for t in txns:
        by_day_txn[t.effective_date].append(t)
    by_day_evt: dict[date, list[QuantityEvent]] = defaultdict(list)
    for e in events:
        by_day_evt[e.ex_date].append(e)

    qty: dict[str, int] = defaultdict(int)
    last_close: dict[str, float] = {}
    pending_events = sorted(by_day_evt)
    pending_txn_days = sorted(by_day_txn)
    rows = []
    index, prev_value, units = 1.0, 0.0, 0.0
    ei = ti = 0
    for d, bench in sessions.select("trade_date", "close").iter_rows():
        while ei < len(pending_events) and pending_events[ei] <= d:  # actions before trades
            for e in by_day_evt[pending_events[ei]]:
                if qty.get(e.company_id):
                    qty[e.company_id] = e.apply(qty[e.company_id])
            ei += 1
        flow = 0.0
        while ti < len(pending_txn_days) and pending_txn_days[ti] <= d:
            for t in by_day_txn[pending_txn_days[ti]]:
                sign = 1 if t.side == "BUY" else -1
                qty[t.company_id] += sign * t.quantity
                flow += sign * t.quantity * t.price + t.charges * (1 if t.side == "BUY" else -1)
            ti += 1
        value = 0.0
        for cid, q in qty.items():
            if q:
                last_close[cid] = closes[cid].get(d, last_close.get(cid, 0.0))
                value += q * last_close[cid]
        if prev_value > 0:
            index *= (value - flow) / prev_value
        units += flow / bench
        rows.append((d, value, flow, index, units * bench))
        prev_value = value
    return pl.DataFrame(
        rows,
        schema=["trade_date", "value", "net_flow", "twr_index", "benchmark_value"],
        orient="row",
    )
