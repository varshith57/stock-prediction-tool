"""Quarterly fundamentals from the XBRL files attached to NSE results filings.

Both the older results feed (``in-bse-fin`` taxonomy, to early 2025) and SEBI Integrated Filing
(Financials) (``in-capmkt``, 2025 on) use the same layout: the quarter's figures sit in a context
whose period runs over exactly that quarter and has no dimensions (segment/scenario). Banks and
other companies use different line items, so each field takes the first tag present from a
ranked list.

Older filings made with the exchange's tool often reference a quarter context named ``OneD``
without declaring it (``FourD`` is the year to date). For those, ``OneD`` is used only when the
filing's own end date (``DateOfEndOfReportingPeriod``, or ``DateOfEndOfFinancialYear`` in
year-end filings, whose ``ReportingQuarter`` reads "Yearly") is the quarter end, and, when both
are present, the ``OneD`` revenue is clearly smaller than the year's, so a full-year figure can't
pass as a quarter.

Values are rupees as filed (EPS in rupees per share). Nothing here guesses: a field that isn't
in the filing is null.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

FIELDS: dict[str, tuple[str, ...]] = {
    "revenue": ("RevenueFromOperations", "InterestEarned"),  # banks: interest earned
    "total_income": ("Income",),
    "expenses": ("Expenses", "ExpenditureExcludingProvisionsAndContingencies"),
    "finance_costs": ("FinanceCosts", "InterestExpended"),
    "pbt": (
        "ProfitBeforeTax",
        "ProfitLossFromOrdinaryActivitiesBeforeTax",
        "ProfitLossBeforeTax",
    ),
    "profit": (
        "ProfitLossForPeriod",
        "ProfitLossForThePeriod",
        "ProfitLoss",
        "ProfitLossForPeriodBeforeMinorityInterest",
    ),
    "eps": (
        "BasicEarningsLossPerShareFromContinuingOperations",
        "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
        "BasicEarningsPerShareAfterExtraordinaryItems",
        "BasicEarningsPerShareBeforeExtraordinaryItems",
        "BasicEarningsLossPerShare",
    ),
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _quarter_contexts(root: ET.Element, period_to: date) -> list[str]:
    """Plain contexts covering the quarter ending ``period_to`` (75-100 days), best first."""
    found = []
    for ctx in root:
        if _local(ctx.tag) != "context":
            continue
        if any(_local(e.tag) in ("segment", "scenario") for e in ctx.iter()):
            continue
        start = end = None
        for e in ctx.iter():
            name = _local(e.tag)
            if name == "startDate":
                start = date.fromisoformat((e.text or "").strip()[:10])
            elif name == "endDate":
                end = date.fromisoformat((e.text or "").strip()[:10])
        if start and end and end == period_to and 75 <= (end - start).days <= 100:
            found.append(ctx.get("id"))
    return found


def parse_results_xbrl(content: bytes, period_to: date) -> dict | None:
    """The quarter's main line items, or None when the file has no quarter context (e.g. not a
    results filing)."""
    root = ET.fromstring(content)
    contexts = _quarter_contexts(root, period_to)
    undeclared = False
    if not contexts:
        ends = {
            (el.text or "").strip()[:10]
            for el in root
            if el.get("contextRef") == "OneD"
            and _local(el.tag) in ("DateOfEndOfReportingPeriod", "DateOfEndOfFinancialYear")
        }
        if period_to.isoformat() not in ends:
            return None
        contexts, undeclared = ["OneD"], True
    facts: dict[str, float] = {}
    for el in root:
        ref = el.get("contextRef")
        if ref not in contexts:
            continue
        name = _local(el.tag)
        if name in facts:
            continue
        try:
            facts[name] = float((el.text or "").strip())
        except ValueError:
            continue
    if undeclared:
        year = next(
            (
                float(el.text)
                for el in root
                if el.get("contextRef") == "FourD"
                and _local(el.tag) in FIELDS["revenue"]
                and (el.text or "").strip()
            ),
            None,
        )
        rev = next((facts[t] for t in FIELDS["revenue"] if t in facts), None)
        if year and rev and rev >= 0.9 * year and period_to.month != 6:  # Jun: Q1 = year to date
            return None
    out: dict = {"context_id": contexts[0]}
    for field, tags in FIELDS.items():
        out[field] = next((facts[t] for t in tags if t in facts), None)
        out[f"{field}_tag"] = next((t for t in tags if t in facts), None)
    if all(out[f] is None for f in FIELDS):
        return None
    return out
