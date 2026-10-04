"""Holdings ledger: transactions -> open FIFO lots and realised trades.

* Each BUY is a lot: quantity, total cost (price x quantity + buy charges), buy date.
* SELLs consume the oldest lots first (FIFO, as Indian capital-gains tax does).
* Bonus and split/consolidation events adjust lot quantities on their ex-date; total cost is
  unchanged, so cost per share follows. A bonus a:b gives floor(qty x a / b) new shares (the
  fractional entitlement is settled in cash by the company, not tracked here). Rights are not
  applied: shares arrive only if you subscribe, which you record as a BUY.
* An action on its ex-date applies to shares held *before* that day; a buy on the ex-date is
  already at the post-action price.
* Imported holdings without a buy date use their import date for event timing (the broker's
  average cost is already post-action), and their holding period is unknown.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date


class LedgerError(ValueError):
    pass


@dataclass(frozen=True)
class Txn:
    txn_id: int
    company_id: str
    side: str  # BUY or SELL
    quantity: int
    price: float
    trade_date: date | None
    effective_date: date  # trade_date, or the import date when the trade date is unknown
    charges: float


@dataclass(frozen=True)
class QuantityEvent:
    company_id: str
    ex_date: date
    kind: str  # bonus | split | consolidation
    ratio_new: float | None = None
    ratio_held: float | None = None
    from_fv: float | None = None
    to_fv: float | None = None

    def apply(self, qty: int) -> int:
        if self.kind == "bonus":
            assert self.ratio_new and self.ratio_held
            return qty + math.floor(qty * self.ratio_new / self.ratio_held + 1e-9)
        assert self.from_fv and self.to_fv
        return math.floor(qty * self.from_fv / self.to_fv + 1e-9)


@dataclass
class Lot:
    txn_id: int
    company_id: str
    quantity: int
    cost: float  # total, including buy charges
    buy_date: date | None
    adjustments: list[str] = field(default_factory=list)

    @property
    def cost_per_share(self) -> float:
        return self.cost / self.quantity


@dataclass(frozen=True)
class Realised:
    company_id: str
    sell_txn_id: int
    buy_txn_id: int
    quantity: int
    buy_date: date | None
    sell_date: date
    cost: float
    proceeds: float  # after sell charges (allocated by quantity)

    @property
    def gain(self) -> float:
        return self.proceeds - self.cost


@dataclass
class Ledger:
    lots: list[Lot]
    realised: list[Realised]


def build_ledger(txns: list[Txn], events: list[QuantityEvent]) -> Ledger:
    by_company: dict[str, list[tuple[date, int, object]]] = defaultdict(list)
    for e in events:
        by_company[e.company_id].append((e.ex_date, 0, e))  # 0: events before same-day trades
    for t in txns:
        by_company[t.company_id].append((t.effective_date, 1, t))

    lots: list[Lot] = []
    realised: list[Realised] = []
    for company, items in by_company.items():
        open_lots: deque[Lot] = deque()
        for _, _, item in sorted(items, key=lambda x: (x[0], x[1], getattr(x[2], "txn_id", 0))):
            if isinstance(item, QuantityEvent):
                for lot in open_lots:
                    before = lot.quantity
                    lot.quantity = item.apply(lot.quantity)
                    lot.adjustments.append(
                        f"{item.kind} {item.ex_date}: {before} -> {lot.quantity}"
                    )
                continue
            t: Txn = item  # type: ignore[assignment]
            if t.side == "BUY":
                open_lots.append(
                    Lot(
                        t.txn_id,
                        company,
                        t.quantity,
                        t.quantity * t.price + t.charges,
                        t.trade_date,
                    )
                )
                continue
            held = sum(lot.quantity for lot in open_lots)
            if t.quantity > held:
                raise LedgerError(
                    f"{company}: selling {t.quantity} on {t.effective_date} but only {held} held"
                )
            remaining = t.quantity
            while remaining:
                lot = open_lots[0]
                take = min(remaining, lot.quantity)
                cost = lot.cost * take / lot.quantity
                share = take / t.quantity
                realised.append(
                    Realised(
                        company_id=company, sell_txn_id=t.txn_id, buy_txn_id=lot.txn_id,
                        quantity=take, buy_date=lot.buy_date, sell_date=t.effective_date,
                        cost=cost, proceeds=(t.quantity * t.price - t.charges) * share,
                    )
                )  # fmt: skip
                lot.quantity -= take
                lot.cost -= cost
                remaining -= take
                if lot.quantity == 0:
                    open_lots.popleft()
        lots.extend(open_lots)
    return Ledger(lots=lots, realised=realised)
