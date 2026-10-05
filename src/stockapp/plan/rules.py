"""Deterministic rules (PRD sections 5 and 9). These are rules, never predictions: the app labels
them "Exit rule hit: <rule>" and never shows a percentage next to them.

* Stop loss: last close at or below cost per share minus ``stop_atr_multiple`` x ATR(14).
* Trailing stop (off unless configured): last close at or below the highest close since buying
  minus ``trailing_stop_atr_multiple`` x ATR(14).
* Signal B, for short-term positions opened from an A signal: target reached (+gain from cost,
  shown only if the sale is profitable after charges and tax), or the window has run out.
* Regime: stress when the Nifty 500 is below its 200-day average and India VIX is in the top
  part of its yearly range. Stress blocks new buys and flags positions above their cap (AM5); it
  is not a sell-everything rule.
* Drawdown: from the portfolio's peak value, review at ``drawdown_review`` and pause new buys at
  ``drawdown_pause``.
"""

from __future__ import annotations

from dataclasses import dataclass

from stockapp.config import AppConfig


@dataclass(frozen=True)
class RuleHit:
    rule: str  # stop_loss | trailing_stop | target_reached | time_stop
    message: str
    level: float | None = None  # the price level involved, for "Sell at or above Rs X"


@dataclass(frozen=True)
class HoldingState:
    company_id: str
    symbol: str
    cost_per_share: float
    last_close: float
    atr_14: float | None  # in rupees, from adjusted prices
    peak_close_since_buy: float | None
    sessions_held: int | None  # None if the buy date is unknown
    opened_by_signal_a: bool = False
    net_profit_if_sold: float | None = None  # after charges and indicative tax
    quantity: float | None = None


def exit_rules(h: HoldingState, cfg: AppConfig) -> list[RuleHit]:
    hits: list[RuleHit] = []
    if h.atr_14:
        stop = h.cost_per_share - cfg.risk.stop_atr_multiple * h.atr_14
        if h.last_close <= stop:
            hits.append(
                RuleHit(
                    "stop_loss",
                    f"closed at {h.last_close:,.2f}, at or below the stop "
                    f"{stop:,.2f} ({cfg.risk.stop_atr_multiple:g} x ATR below cost)",
                    stop,
                )
            )
        k = cfg.risk.trailing_stop_atr_multiple
        if k and h.peak_close_since_buy:
            trail = h.peak_close_since_buy - k * h.atr_14
            if h.last_close <= trail:
                hits.append(
                    RuleHit(
                        "trailing_stop",
                        f"fell to {h.last_close:,.2f} from a peak of "
                        f"{h.peak_close_since_buy:,.2f} (trailing stop {trail:,.2f})",
                        trail,
                    )
                )
    if h.opened_by_signal_a:
        target = h.cost_per_share * (1 + cfg.signals.gain_threshold)
        profitable = h.net_profit_if_sold is not None and h.net_profit_if_sold > 0
        if h.last_close >= target and profitable:
            hits.append(
                RuleHit(
                    "target_reached",
                    f"reached the +{cfg.signals.gain_threshold:.0%} target {target:,.2f}",
                    target,
                )
            )
        elif h.sessions_held is not None and h.sessions_held >= cfg.signals.window_trading_days:
            hits.append(
                RuleHit(
                    "time_stop",
                    f"the {cfg.signals.window_trading_days}-day signal "
                    "window is over without reaching the target",
                )
            )
    return hits


@dataclass(frozen=True)
class Regime:
    stress: bool
    reason: str


def market_regime(
    mkt_sma_gap_200: float | None, vix_pct_250: float | None, cfg: AppConfig
) -> Regime:
    if mkt_sma_gap_200 is None or vix_pct_250 is None:
        return Regime(False, "regime unknown (missing market data): treated as normal")
    below = mkt_sma_gap_200 < 0
    fear = vix_pct_250 >= cfg.risk.stress_vix_percentile
    market = f"Nifty 500 {mkt_sma_gap_200:+.1%} vs its 200-day average"
    vix = f"India VIX at the {vix_pct_250:.0%} percentile of the last year"
    if below and fear:
        return Regime(True, f"stress: {market} and {vix}")
    return Regime(False, f"normal: {market}, {vix}")


@dataclass(frozen=True)
class Drawdown:
    from_peak: float | None
    review: bool
    pause_buys: bool


def drawdown_state(values: list[float], cfg: AppConfig) -> Drawdown:
    """``values``: a time-weighted portfolio index (oldest first; see
    ``portfolio.history``). Raw value would hide losses behind new contributions."""
    if not values:
        return Drawdown(None, False, False)
    peak = max(values)
    dd = values[-1] / peak - 1 if peak > 0 else 0.0
    return Drawdown(dd, dd <= cfg.risk.drawdown_review, dd <= cfg.risk.drawdown_pause)
