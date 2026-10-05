"""Fundamentals from results XBRL: the quarter's own context, the older undeclared ``OneD``
convention (only with a matching period end), banks' line items, and nothing guessed."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from stockapp.ingest.xbrl import parse_results_xbrl

HEAD = (
    '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
    'xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/in-capmkt" '
    'xmlns:xbrldi="http://xbrl.org/2006/xbrldi">'
)


def _ctx(cid: str, start: str, end: str, segment: bool = False) -> str:
    seg = "<xbrli:segment><xbrldi:explicitMember>x</xbrldi:explicitMember></xbrli:segment>"
    return (
        f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier>1</xbrli:identifier>'
        f"{seg if segment else ''}</xbrli:entity><xbrli:period><xbrli:startDate>{start}"
        f"</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>"
    )


def _fact(tag: str, ctx: str, value: str) -> str:
    return f'<in-capmkt:{tag} contextRef="{ctx}">{value}</in-capmkt:{tag}>'


def test_quarter_context_and_ranked_tags():
    xml = (
        HEAD
        + _ctx("Q", "2026-04-01", "2026-06-30")
        + _ctx("YTD", "2025-04-01", "2026-06-30")
        + _ctx("SEG", "2026-04-01", "2026-06-30", segment=True)
        + _fact("RevenueFromOperations", "SEG", "1")  # a segment: ignored
        + _fact("RevenueFromOperations", "YTD", "999")  # year to date: ignored
        + _fact("RevenueFromOperations", "Q", "100")
        + _fact("ProfitLossForPeriod", "Q", "12.5")
        + _fact("BasicEarningsLossPerShareFromContinuingOperations", "Q", "3.2")
        + "</xbrli:xbrl>"
    ).encode()
    r = parse_results_xbrl(xml, date(2026, 6, 30))
    assert (r["revenue"], r["profit"], r["eps"]) == (100.0, 12.5, 3.2)
    assert r["revenue_tag"] == "RevenueFromOperations" and r["pbt"] is None  # not guessed
    assert parse_results_xbrl(xml, date(2026, 3, 31)) is None  # wrong quarter


def test_banks_use_their_own_line_items():
    xml = (
        HEAD
        + _ctx("Q", "2026-04-01", "2026-06-30")
        + _fact("InterestEarned", "Q", "900")
        + _fact("ProfitLossForThePeriod", "Q", "200")
        + _fact("BasicEarningsPerShareAfterExtraordinaryItems", "Q", "12.5")
        + "</xbrli:xbrl>"
    ).encode()
    r = parse_results_xbrl(xml, date(2026, 6, 30))
    assert (r["revenue"], r["profit"], r["eps"]) == (900.0, 200.0, 12.5)
    assert r["revenue_tag"] == "InterestEarned"


def test_undeclared_oned_needs_a_matching_period_end():
    body = (
        _fact("DateOfEndOfReportingPeriod", "OneD", "2018-12-31")
        + _fact("RevenueFromOperations", "OneD", "50")
        + _fact("RevenueFromOperations", "FourD", "150")
        + "</xbrli:xbrl>"
    )
    xml = (HEAD + body).encode()
    assert parse_results_xbrl(xml, date(2018, 12, 31))["revenue"] == 50.0
    assert parse_results_xbrl(xml, date(2019, 3, 31)) is None


@pytest.mark.parametrize(
    ("name", "quarter", "revenue", "eps"),
    [
        ("fin_INFY", date(2026, 6, 30), 399_570_000_000.0, 17.87),
        ("fin_HDFCBANK", date(2026, 6, 30), 905_753_300_000.0, 12.5),
        ("old_HDFCBANK_2019", date(2018, 12, 31), 258_902_600_000.0, 20.6),
        ("int_INFY", date(2026, 6, 30), None, None),  # a governance filing: no figures
    ],
)
def test_real_samples(name, quarter, revenue, eps):
    path = Path(__file__).parent / "fixtures" / "private" / f"xbrl_{name}.xml"
    if not path.exists():
        pytest.skip("private sample not present (not committed)")
    r = parse_results_xbrl(path.read_bytes(), quarter)
    if revenue is None:
        assert r is None
    else:
        assert (r["revenue"], r["eps"]) == (revenue, eps)


def test_year_end_filings_use_the_financial_year_end_and_reject_annual_figures():
    def year_end(one: str) -> bytes:
        return (
            HEAD
            + _fact("ReportingQuarter", "OneD", "Yearly")
            + _fact("DateOfEndOfFinancialYear", "OneD", "2018-03-31")
            + _fact("RevenueFromOperations", "OneD", one)
            + _fact("RevenueFromOperations", "FourD", "1443")
            + _fact("ProfitLossForPeriodBeforeMinorityInterest", "OneD", "65")
            + "</xbrli:xbrl>"
        ).encode()

    r = parse_results_xbrl(year_end("423"), date(2018, 3, 31))
    assert (r["revenue"], r["profit"]) == (423.0, 65.0)
    assert parse_results_xbrl(year_end("1443"), date(2018, 3, 31)) is None  # a full year
