"""Weekly plan engine (M8): turns scores, the gate, holdings, rules and data quality into the plan
shown on This Week. Pure: every input is passed in, so each rule is unit-tested.

Order of precedence:
1. Data quality below the minimum -> NO SIGNAL: no actions at all, just the reason.
2. Exits for holdings: hard rules (stop loss, trailing stop), signal B for signal positions, and
   signal C when it is LIVE at its validated cutoff. Rules are labelled as rules, never as a %.
3. Opportunities (signal A), only when LIVE: above the cutoff, not blocked by a quality flag, not
   trade-for-trade, listed a year or more, no stress regime or drawdown pause; at most
   ``max_opportunities``, highest expected gain first. Each is sized to the lower of the cash left
   and the per-stock cap (``max_stock_weight`` of portfolio + budget, but at least
   ``min_position_inr`` so a new portfolio can start), whole shares, and skipped with a note if
   that is below ``min_position_inr``.
4. Everything else held. When no opportunity qualifies, the closest candidate and its probability
   are shown with the signal's real status (hideable in Settings).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date

from stockapp.config import AppConfig
from stockapp.plan.rules import Drawdown, HoldingState, Regime, RuleHit, exit_rules

MIN_LISTED_SESSIONS = 250


@dataclass(frozen=True)
class Candidate:
    company_id: str
    symbol: str
    p_a: float | None
    p_c: float | None
    expected_gain: float | None
    last_close: float
    atr_14: float | None
    blocked: bool
    trade_for_trade: bool
    sessions_listed: int
    reason_a: str = ""
    reason_c: str = ""


@dataclass(frozen=True)
class SignalGate:
    status: str  # LIVE or OFF
    cutoff: float | None
    reason: str


@dataclass
class PlanItem:
    kind: str  # BUY | SELL | HOLD
    company_id: str
    symbol: str
    headline: str
    reason: str
    probability: float | None = None
    expected_gain: float | None = None
    quantity: int | None = None
    guide_price: float | None = None
    sellout_price: float | None = None
    stop_price: float | None = None
    rule: str | None = None

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.company_id}"


@dataclass
class Plan:
    signal_date: date
    week_of: date
    status: str  # OK or NO_SIGNAL
    status_reason: str
    quality_score: float | None
    regime: Regime
    drawdown: Drawdown
    budget_available: float
    exits: list[PlanItem] = field(default_factory=list)
    opportunities: list[PlanItem] = field(default_factory=list)
    holds: list[PlanItem] = field(default_factory=list)
    closest: dict | None = None
    notes: list[str] = field(default_factory=list)
    gates: dict[str, SignalGate] = field(default_factory=dict)
    model_version: str | None = None

    @property
    def action_count(self) -> int:
        return len(self.exits) + len(self.opportunities)

    def to_json(self) -> dict:
        d = asdict(self)
        d["signal_date"], d["week_of"] = str(self.signal_date), str(self.week_of)
        return d


def build_plan(
    *,
    cfg: AppConfig,
    signal_date: date,
    week_of: date,
    quality_score: float | None,
    candidates: list[Candidate],
    holdings: list[HoldingState],
    holding_weights: dict[str, float],  # company_id -> weight 0..1
    gates: dict[str, SignalGate],
    regime: Regime,
    drawdown: Drawdown,
    budget_available: float,
    portfolio_value: float = 0.0,
    model_version: str | None = None,
) -> Plan:
    plan = Plan(
        signal_date,
        week_of,
        "OK",
        "",
        quality_score,
        regime,
        drawdown,
        budget_available,
        gates=gates,
        model_version=model_version,
    )
    minimum = cfg.data.min_quality_score
    if quality_score is None or quality_score < minimum:
        plan.status = "NO_SIGNAL"
        plan.status_reason = (
            f"data quality {quality_score:.0f}/100 is below {minimum:g}"
            if quality_score is not None
            else "no data quality result for this week"
        )
        return plan

    by_id = {c.company_id: c for c in candidates}
    gate_a, gate_c = gates.get("A"), gates.get("C")
    a_live = gate_a is not None and gate_a.status == "LIVE" and gate_a.cutoff is not None
    c_live = gate_c is not None and gate_c.status == "LIVE" and gate_c.cutoff is not None

    # exits and holds -------------------------------------------------------------------------
    for h in holdings:
        hits: list[RuleHit] = exit_rules(h, cfg)
        cand = by_id.get(h.company_id)
        if hits:
            r = hits[0]
            level = f"Sell at or above {r.level:,.2f}" if r.rule == "target_reached" else "Sell"
            plan.exits.append(
                PlanItem(
                    "SELL",
                    h.company_id,
                    h.symbol,
                    f"{level}: exit rule hit ({r.rule.replace('_', ' ')})",
                    "; ".join(x.message for x in hits),
                    rule=r.rule,
                )
            )
        elif c_live and cand and cand.p_c is not None and cand.p_c >= gate_c.cutoff:  # type: ignore[union-attr]
            plan.exits.append(
                PlanItem(
                    "SELL",
                    h.company_id,
                    h.symbol,
                    f"Sell before the drop: crash risk {cand.p_c:.0%}",
                    cand.reason_c,
                    probability=cand.p_c,
                )
            )
        else:
            note = ""
            if regime.stress and holding_weights.get(h.company_id, 0) > cfg.risk.max_stock_weight:
                note = (
                    f"above the {cfg.risk.max_stock_weight:.0%} cap in a stress regime: "
                    "consider trimming"
                )
            plan.holds.append(PlanItem("HOLD", h.company_id, h.symbol, "Hold", note))

    # opportunities ---------------------------------------------------------------------------
    blockers = []
    if regime.stress:
        blockers.append("stress regime: no new buys")
    if drawdown.pause_buys:
        blockers.append(f"portfolio drawdown {drawdown.from_peak:.1%}: new buys paused")
    held = {h.company_id for h in holdings}
    eligible = [
        c
        for c in candidates
        if c.p_a is not None
        and not c.blocked
        and not c.trade_for_trade
        and c.sessions_listed >= MIN_LISTED_SESSIONS
        and c.company_id not in held
    ]
    if a_live and not blockers:
        chosen = sorted(
            (c for c in eligible if c.p_a >= gate_a.cutoff),  # type: ignore[operator,union-attr]
            key=lambda c: c.expected_gain or 0.0,
            reverse=True,
        )
        cash = budget_available
        # per-stock cap on the portfolio after this week's money goes in; never below the minimum
        # position, or a new or small portfolio could never buy anything
        cap_rupees = max(
            cfg.risk.max_stock_weight * (portfolio_value + budget_available),
            cfg.budget.min_position_inr,
        )
        for c in chosen:
            if len(plan.opportunities) >= cfg.signals.max_opportunities:
                break
            spend = min(cash, cap_rupees)
            qty = math.floor(spend / c.last_close) if c.last_close > 0 else 0
            if qty * c.last_close < cfg.budget.min_position_inr:
                plan.notes.append(
                    f"{c.symbol} qualified but a position within the budget and the "
                    f"{cfg.risk.max_stock_weight:.0%} cap would be below the minimum"
                )
                continue
            sellout = c.last_close * (1 + cfg.signals.gain_threshold)
            stop = c.last_close - cfg.risk.stop_atr_multiple * c.atr_14 if c.atr_14 else None
            plan.opportunities.append(
                PlanItem(
                    "BUY",
                    c.company_id,
                    c.symbol,
                    f"Certainty {c.p_a:.0%} · expected gain {c.expected_gain or 0:+.0%} · "
                    f"buy {qty} near {c.last_close:,.2f} · sell-out at {sellout:,.2f}",
                    c.reason_a,
                    probability=c.p_a,
                    expected_gain=c.expected_gain,
                    quantity=qty,
                    guide_price=c.last_close,
                    sellout_price=sellout,
                    stop_price=stop,
                )
            )
            cash -= qty * c.last_close
    plan.notes.extend(blockers)

    if not plan.opportunities and eligible:
        best = max(eligible, key=lambda c: c.p_a or 0.0)
        plan.closest = {
            "symbol": best.symbol,
            "probability": best.p_a,
            "bar": cfg.signals.certainty_bar,
            "signal_status": gate_a.status if gate_a else "OFF",
            "signal_reason": gate_a.reason if gate_a else "not validated yet",
            "reason": best.reason_a,
        }
    return plan
