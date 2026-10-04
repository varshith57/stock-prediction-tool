"""Zerodha equity-delivery charges and indicative capital-gains tax (rates from config).

Charges per order (NSE, delivery):
* brokerage: 0 for delivery
* STT: ``stt_rate`` on both buy and sell value
* exchange transaction charge: ``exchange_txn_rate`` of value
* SEBI fee: ``sebi_fee_rate`` of value
* GST: ``gst_rate`` on brokerage + exchange charge + SEBI fee
* stamp duty: ``stamp_duty_buy_rate`` on buy value only
* DP charge: a flat ``dp_charge_per_scrip_sell_inr`` per stock sold per day (GST included)

Tax (indicative only, never advice): listed equity held over 12 months is long-term
(``ltcg_rate`` above the yearly ``ltcg_exemption_inr``), otherwise short-term (``stcg_rate``).
Rates change; Settings shows the date they were last checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from stockapp.config import CostsConfig, TaxConfig


@dataclass(frozen=True)
class Charges:
    value: float
    stt: float
    exchange: float
    sebi: float
    gst: float
    stamp: float
    dp: float
    brokerage: float = 0.0

    @property
    def total(self) -> float:
        return round(
            self.brokerage + self.stt + self.exchange + self.sebi + self.gst + self.stamp + self.dp,
            2,
        )


def order_charges(side: str, quantity: int, price: float, costs: CostsConfig) -> Charges:
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, got {side!r}")
    value = quantity * price
    brokerage = costs.brokerage
    exchange = value * costs.exchange_txn_rate
    sebi = value * costs.sebi_fee_rate
    return Charges(
        value=value,
        brokerage=brokerage,
        stt=value * costs.stt_rate,
        exchange=exchange,
        sebi=sebi,
        gst=(brokerage + exchange + sebi) * costs.gst_rate,
        stamp=value * costs.stamp_duty_buy_rate if side == "BUY" else 0.0,
        dp=costs.dp_charge_per_scrip_sell_inr if side == "SELL" else 0.0,
    )


def is_long_term(buy_date: date | None, sell_date: date) -> bool | None:
    """More than 12 months held. None when the buy date is unknown."""
    if buy_date is None:
        return None
    anniversary = (
        buy_date.replace(year=buy_date.year + 1)
        if not (buy_date.month == 2 and buy_date.day == 29)
        else date(buy_date.year + 1, 3, 1)
    )
    return sell_date > anniversary


def estimated_tax(gain: float, long_term: bool | None, tax: TaxConfig) -> float | None:
    """Tax on one gain in isolation. Losses -> 0. LTCG exemption is yearly and shared across all
    sales, so per-position figures ignore it (conservative); the year view applies it once."""
    if long_term is None:
        return None
    if gain <= 0:
        return 0.0
    return round(gain * (tax.ltcg_rate if long_term else tax.stcg_rate), 2)
