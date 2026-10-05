"""Money backtest: would following the app have made money, after every cost and tax, compared
with putting the same rupees into an index fund?

Accuracy (how often a pick hits +10%) is not money. This simulates the app's own trading rules
day by day on the model's *out-of-sample* picks (gold ``oos_predictions``: every pick was made by
a model that never saw that week), so nothing is known in advance:

* Each signal week, the weekly budget is added. Picks at or above a confidence ``cutoff``, not
  already held, ranked by expected gain, at most ``max_opps``; none in a stress regime or while
  the portfolio is past the drawdown pause (as the app does).
* Each buy is sized like the app: the lower of the cash left and the per-stock cap (share of the
  portfolio, at least the minimum position), whole shares, skipped below the minimum position.
* Orders are decided at a close and filled at the next session's open, with slippage against
  you. Every order pays Zerodha delivery charges (STT, stamp, exchange, SEBI, GST, DP).
* Exits (the trade rules): close at or above the +gain target, close at or below the stop
  (``stop_atr`` x ATR(14) at entry below the entry price), or the window is over. Sold at the next
  open. A stock that stops trading is sold at its last close.
* Tax: net short-term gains of each financial year (April-March) at ``stcg_rate``, paid at the
  year end; a year's net loss is carried forward against later gains.
* Benchmark: the same rupees on the same days into the Nifty 50 / Nifty 500 (price indices: no
  dividends, so they understate an index fund by about 1-1.5% a year; no fund fees or tax).

Prices are split/bonus-adjusted, so returns are right; share counts can differ from what was
traded at the time, which only matters for the flat DP charge.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date

import polars as pl

from stockapp.config import CostsConfig
from stockapp.portfolio.costs import order_charges


@dataclass(frozen=True)
class Params:
    cutoff: float  # minimum calibrated chance to buy
    weekly: float  # added every signal week
    initial: float = 0.0
    max_opps: int = 5
    max_stock_weight: float = 0.15
    min_position: float = 3000.0
    gain: float = 0.10
    stop_atr: float = 2.0
    window: int = 5
    slippage_bps: float = 15.0
    stcg_rate: float = 0.20
    drawdown_pause: float | None = -0.12  # None: never pause
    drawdown_lookback: int = 126  # market days for the drawdown peak (as the app)
    use_regime: bool = True
    idle_in_index: bool = False  # spare cash earns the index (passed to simulate) between trades


@dataclass
class Position:
    company_id: str
    symbol: str
    qty: int
    entry_date: date
    entry_price: float
    cost: float  # rupees paid incl. charges
    atr: float | None
    sessions: int = 0


@dataclass
class Trade:
    company_id: str
    symbol: str
    entry_date: date
    exit_date: date
    qty: int
    cost: float
    proceeds: float
    reason: str

    @property
    def pnl(self) -> float:
        return self.proceeds - self.cost

    @property
    def ret(self) -> float:
        return self.pnl / self.cost if self.cost else 0.0


@dataclass
class Result:
    params: Params
    daily: pl.DataFrame  # trade_date, value, contributed, cash, twr
    trades: list[Trade]
    flows: list[tuple[date, float]]  # money put in (positive), for XIRR
    fees: float
    taxes: float
    metrics: dict = field(default_factory=dict)
    open_positions: list[Position] = field(default_factory=list)


def financial_year(d: date) -> int:
    return d.year if d.month >= 4 else d.year - 1


def xirr(flows: list[tuple[date, float]], final_date: date, final_value: float) -> float | None:
    """Annual money-weighted return: the rate at which the money put in grows to the end value."""
    if not flows or final_value <= 0:
        return None
    cash = [(d, -a) for d, a in flows] + [(final_date, final_value)]
    t0 = cash[0][0]

    def npv(r: float) -> float:
        return sum(a / (1 + r) ** ((d - t0).days / 365.25) for d, a in cash)

    lo, hi = -0.99, 10.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def max_drawdown(index: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for v in index:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1)
    return worst


def simulate(
    signals: pl.DataFrame,
    prices: pl.DataFrame,
    sessions: list[date],
    params: Params,
    costs: CostsConfig,
    index: pl.DataFrame | None = None,
) -> Result:
    """``signals``: trade_date, company_id, symbol, p, expected_gain, stress (bool).
    ``prices``: company_id, trade_date, open, close, atr (adjusted, rupees).
    ``index`` (trade_date, close): with ``params.idle_in_index``, uninvested cash grows with it
    (an index fund moved in and out at no cost or tax: optimistic, and labelled so)."""
    idx_close = (
        dict(zip(index["trade_date"].to_list(), index["close"].to_list(), strict=True))
        if index is not None and params.idle_in_index
        else {}
    )
    prev_idx: float | None = None
    px: dict[str, dict[date, tuple[float, float, float | None]]] = defaultdict(dict)
    last_seen: dict[str, tuple[date, float]] = {}
    for r in prices.sort("trade_date").iter_rows(named=True):
        px[r["company_id"]][r["trade_date"]] = (r["open"], r["close"], r["atr"])
        last_seen[r["company_id"]] = (r["trade_date"], r["close"])
    picks: dict[date, list[dict]] = defaultdict(list)
    for r in (
        signals.filter(pl.col("p") >= params.cutoff)
        .sort(["trade_date", "expected_gain"], descending=[False, True])
        .iter_rows(named=True)
    ):
        picks[r["trade_date"]].append(r)
    signal_days = set(signals["trade_date"].unique().to_list())
    stress_days = set(signals.filter(pl.col("stress"))["trade_date"].unique().to_list())

    slip = params.slippage_bps / 10_000
    cash, contributed, fees, taxes = params.initial, params.initial, 0.0, 0.0
    flows = [(sessions[0], params.initial)] if params.initial else []
    held: dict[str, Position] = {}
    to_sell: dict[str, str] = {}  # company_id -> reason
    to_buy: list[tuple[dict, int]] = []
    trades: list[Trade] = []
    realised: dict[int, float] = defaultdict(float)
    carry, fy = 0.0, financial_year(sessions[0])
    twr, prev_value = 1.0, None
    recent: deque[float] = deque(maxlen=params.drawdown_lookback)
    last_close: dict[str, float] = {}
    rows = []

    for d in sessions:
        if idx_close:  # spare cash parked in the index since yesterday's close
            c = idx_close.get(d)
            if c is not None:
                if prev_idx and cash > 0:
                    cash *= c / prev_idx
                prev_idx = c
        # financial year end: pay tax on the year's net short-term gains
        if financial_year(d) != fy:
            net = realised[fy]
            if net < 0:
                carry += -net
            else:
                taxable = max(0.0, net - carry)
                carry = max(0.0, carry - net)
                tax = taxable * params.stcg_rate
                cash -= tax
                taxes += tax
            fy = financial_year(d)

        # morning: fill yesterday's orders at the open, sells first
        for cid in list(to_sell):
            pos = held[cid]
            bar = px[cid].get(d)
            gone = last_seen[cid][0] < d
            if bar is None and not gone:
                continue  # no trade today (halt): try tomorrow
            price = (bar[0] * (1 - slip)) if bar else last_seen[cid][1]
            ch = order_charges("SELL", pos.qty, price, costs).total
            proceeds = pos.qty * price - ch
            cash += proceeds
            fees += ch
            t = Trade(cid, pos.symbol, pos.entry_date, d, pos.qty, pos.cost, proceeds, to_sell[cid])
            trades.append(t)
            realised[financial_year(d)] += t.pnl
            del held[cid], to_sell[cid]
        for pick, qty in to_buy:
            cid = pick["company_id"]
            bar = px[cid].get(d)
            if bar is None or cid in held:
                continue
            price = bar[0] * (1 + slip)
            while qty > 0 and qty * price + order_charges("BUY", qty, price, costs).total > cash:
                qty -= 1
            if qty <= 0 or qty * price < params.min_position * 0.9:
                continue
            ch = order_charges("BUY", qty, price, costs).total
            cash -= qty * price + ch
            fees += ch
            held[cid] = Position(cid, pick["symbol"], qty, d, price, qty * price + ch, bar[2])
        to_buy = []

        # close: new money, mark to market, check exits
        flow = 0.0
        if d in signal_days:
            cash += params.weekly
            contributed += params.weekly
            flow = params.weekly
            if params.weekly:
                flows.append((d, params.weekly))
        for cid in held:
            bar = px[cid].get(d)
            if bar is not None:
                last_close[cid] = bar[1]
        value = cash + sum(
            p.qty * last_close.get(p.company_id, p.entry_price) for p in held.values()
        )
        if prev_value and prev_value > 0:
            twr *= (value - flow) / prev_value
        prev_value = value
        recent.append(twr)

        for cid, pos in held.items():
            if cid in to_sell:
                continue
            bar = px[cid].get(d)
            if bar is None:
                if last_seen[cid][0] < d:
                    to_sell[cid] = "stopped trading"
                continue
            pos.sessions += 1
            close = bar[1]
            if close >= pos.entry_price * (1 + params.gain):
                to_sell[cid] = "target"
            elif pos.atr and close <= pos.entry_price - params.stop_atr * pos.atr:
                to_sell[cid] = "stop-loss"
            elif pos.sessions >= params.window:
                to_sell[cid] = "time"

        # signal day: decide next morning's buys
        paused = (
            params.drawdown_pause is not None and twr / max(recent) - 1 <= params.drawdown_pause
        )
        if d in signal_days and not paused and not (params.use_regime and d in stress_days):
            left = cash
            cap = max(params.max_stock_weight * value, params.min_position)
            chosen = 0
            for pick in picks.get(d, []):
                if chosen >= params.max_opps:
                    break
                cid = pick["company_id"]
                bar = px[cid].get(d)
                if cid in held or bar is None or bar[1] <= 0:
                    continue
                qty = math.floor(min(left, cap) / bar[1])
                if qty * bar[1] < params.min_position:
                    continue
                to_buy.append((pick, qty))
                left -= qty * bar[1]
                chosen += 1
        rows.append((d, value, contributed, cash, twr))

    daily = pl.DataFrame(
        rows, schema=["trade_date", "value", "contributed", "cash", "twr"], orient="row"
    )
    res = Result(params, daily, trades, flows, fees, taxes, open_positions=list(held.values()))
    res.metrics = metrics(res)
    return res


def benchmark(
    index: pl.DataFrame, sessions: list[date], signal_days: set[date], params: Params
) -> pl.DataFrame:
    """Same money, same days, into an index (close to close). ``index``: trade_date, close."""
    closes = dict(zip(index["trade_date"].to_list(), index["close"].to_list(), strict=True))
    units, contributed, twr, prev, last = 0.0, 0.0, 1.0, None, None
    rows = []
    for i, d in enumerate(sessions):
        c = closes.get(d, last)
        if c is None:
            continue
        last = c
        flow = (params.initial if i == 0 else 0.0) + (params.weekly if d in signal_days else 0.0)
        units += flow / c
        contributed += flow
        value = units * c
        if prev:
            twr *= (value - flow) / prev
        prev = value
        rows.append((d, value, contributed, twr))
    return pl.DataFrame(rows, schema=["trade_date", "value", "contributed", "twr"], orient="row")


def metrics(res: Result) -> dict:
    d = res.daily
    end, value = d["trade_date"][-1], float(d["value"][-1])
    years = max((end - d["trade_date"][0]).days / 365.25, 1e-9)
    invested = (d["value"] - d["cash"]).clip(lower_bound=0) / d["value"].clip(lower_bound=1e-9)
    wins = [t for t in res.trades if t.pnl > 0]
    by_reason: dict[str, int] = defaultdict(int)
    for t in res.trades:
        by_reason[t.reason] += 1
    return {
        "final_value": value,
        "contributed": float(d["contributed"][-1]),
        "xirr": xirr(res.flows, end, value),
        "twr_annual": float(d["twr"][-1]) ** (1 / years) - 1,
        "max_drawdown": max_drawdown(d["twr"].to_list()),
        "trades": len(res.trades),
        "win_rate": len(wins) / len(res.trades) if res.trades else None,
        "avg_trade": sum(t.ret for t in res.trades) / len(res.trades) if res.trades else None,
        "fees": res.fees,
        "taxes": res.taxes,
        "time_invested": float(invested.mean()),
        "exits": dict(by_reason),
    }


def benchmark_metrics(b: pl.DataFrame, flows: list[tuple[date, float]]) -> dict:
    end, value = b["trade_date"][-1], float(b["value"][-1])
    years = max((end - b["trade_date"][0]).days / 365.25, 1e-9)
    return {
        "final_value": value,
        "contributed": float(b["contributed"][-1]),
        "xirr": xirr(flows, end, value),
        "twr_annual": float(b["twr"][-1]) ** (1 / years) - 1,
        "max_drawdown": max_drawdown(b["twr"].to_list()),
    }
