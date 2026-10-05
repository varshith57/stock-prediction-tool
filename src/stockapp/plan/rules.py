"""Deterministic rules (PRD sections 5 and 9). These are rules, never predictions: the app labels
them "Exit rule hit: <rule>" and never shows a percentage next to them.

Exit rules apply to *trades* only (holdings opened from the app's buy ideas). *Investments* (the
user's own long-term buys) are never auto-sold: past ``investment_review_loss`` below cost they
get a review note instead (``review_note``).

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
    opened_by_signal_a: bool = False  # a "trade": the short-term exit rules apply
    net_profit_if_sold: float | None = None  # after charges and indicative tax
    quantity: float | None = None


def _pct_from(a: float, b: float) -> str:
    return f"{a / b - 1:+.1%}"


def exit_rules(h: HoldingState, cfg: AppConfig) -> list[RuleHit]:
    """Exit rules for trades. Investments get none: see ``review_note``."""
    hits: list[RuleHit] = []
    if not h.opened_by_signal_a:
        return hits
    if h.atr_14:
        stop = h.cost_per_share - cfg.risk.stop_atr_multiple * h.atr_14
        if h.last_close <= stop:
            hits.append(
                RuleHit(
                    "stop_loss",
                    f"Down {1 - h.last_close / h.cost_per_share:.1%} since you bought. This "
                    f"trade's stop-loss was {stop:,.2f} ({_pct_from(stop, h.cost_per_share)}, "
                    f"about {cfg.risk.stop_atr_multiple:g} normal days' moves below your price): "
                    "trades are cut early so a small loss can't become a big one",
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
                        f"Fell to {h.last_close:,.2f} from its best of "
                        f"{h.peak_close_since_buy:,.2f} since you bought, past the lock-in "
                        f"line {trail:,.2f}: sell to keep the gain",
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
                    f"Reached the +{cfg.signals.gain_threshold:.0%} target {target:,.2f}, "
                    "with a profit after fees and tax: take it",
                    target,
                )
            )
        elif h.sessions_held is not None and h.sessions_held >= cfg.signals.window_trading_days:
            hits.append(
                RuleHit(
                    "time_stop",
                    f"The {cfg.signals.window_trading_days}-day window this trade was bought "
                    "for is over without reaching the target: free the money for the next idea",
                )
            )
    return hits


def review_note(h: HoldingState, cfg: AppConfig) -> str | None:
    """Investments only: a nudge (never a sell) once the loss passes the user's review line."""
    if h.opened_by_signal_a or not h.cost_per_share:
        return None
    change = h.last_close / h.cost_per_share - 1
    line = cfg.risk.investment_review_loss
    if change > -line:
        return None
    return (
        f"Down {-change:.1%} since you bought, past your {line:.0%} review line. Not a sell: "
        "ask whether you'd buy it today at this price"
    )


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
    ``portfolio.history``). Raw value would hide losses behind new contributions.

    The peak is the best value in the last ``drawdown_lookback_days`` market days: measured from
    the all-time peak, a portfolio that went to cash could never climb back, so a pause on new
    buys would never lift."""
    if not values:
        return Drawdown(None, False, False)
    peak = max(values[-cfg.risk.drawdown_lookback_days :])
    dd = values[-1] / peak - 1 if peak > 0 else 0.0
    return Drawdown(dd, dd <= cfg.risk.drawdown_review, dd <= cfg.risk.drawdown_pause)
