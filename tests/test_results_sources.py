"""Results-date connectors: synthetic payloads in NSE's format (real samples stay private)."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from stockapp.ingest.base import header_fingerprint
from stockapp.ingest.nse_results import NseBoardMeetings, NseFinancialResults

BM = [
    {
        "bm_symbol": "ABC", "bm_date": "10-Jul-2024", "bm_purpose": "Financial Results",
        "bm_desc": "To consider quarterly results", "sm_indusrty": "-",
        "bm_timestamp": "02-Jul-2024 19:27:13", "sm_name": "ABC Limited",
        "sm_isin": "INE000A01011", "diff": "00:00:03", "attachment": None,
    },
    {
        "bm_symbol": "XYZ", "bm_date": "15-Jul-2024", "bm_purpose": "Fund Raising",
        "bm_desc": "To consider fund raising", "bm_timestamp": "05-Jul-2024 10:00:00",
        "sm_isin": "INE000X01011",
    },
]  # fmt: skip
FR = [
    {
        "symbol": "ABC", "isin": "INE000A01011", "fromDate": "01-Apr-2024", "toDate": "30-Jun-2024",
        "broadCastDate": "10-Jul-2024 19:21:58", "filingDate": "10-Jul-2024 19:21",
        "consolidated": "Consolidated", "audited": "Un-Audited", "relatingTo": "First Quarter",
        "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/X.xml", "params": "x",
    }
]  # fmt: skip


def _conn(cls):
    return cls.__new__(cls)  # parsing needs no database or lake


def test_board_meetings_parse_with_announcement_time():
    c = _conn(NseBoardMeetings)
    raw = json.dumps(BM).encode()
    assert header_fingerprint(c.extract_header(raw)) in c.known_fingerprints
    df = c.parse(raw, date(2024, 7, 1))
    abc = df.row(0, named=True)
    assert abc["meeting_date"] == date(2024, 7, 10) and abc["is_results"]
    assert abc["announced_at"] == datetime(2024, 7, 2, 19, 27, 13)
    assert not df.row(1, named=True)["is_results"] and df["month"][0] == "2024-07"
    assert c.validate(df, date(2024, 7, 1)) == []


def test_results_filings_parse_with_publication_time():
    c = _conn(NseFinancialResults)
    raw = json.dumps(FR).encode()
    assert header_fingerprint(c.extract_header(raw)) in c.known_fingerprints
    r = c.parse(raw, date(2024, 7, 1)).row(0, named=True)
    assert r["published_at"] == datetime(2024, 7, 10, 19, 21, 58)
    assert (r["period_from"], r["period_to"]) == (date(2024, 4, 1), date(2024, 6, 30))
    assert r["xbrl_url"].endswith(".xml") and r["consolidated"] == "Consolidated"


@pytest.mark.parametrize("cls", [NseBoardMeetings, NseFinancialResults])
def test_missing_keys_or_empty_months(cls):
    c = _conn(cls)
    assert header_fingerprint(c.extract_header(b"[]")) in c.known_fingerprints
    assert c.parse(b"[]", date(2024, 7, 1)).height == 0
    broken = json.dumps([{"symbol": "A"}]).encode()
    assert header_fingerprint(c.extract_header(broken)) not in c.known_fingerprints  # quarantined
    with pytest.raises(ValueError):
        c.extract_header(b'{"error": "x"}')
    assert "/api/" in c.url_for(date(2024, 2, 15)) and "29-02-2024" in c.url_for(date(2024, 2, 1))


@pytest.mark.parametrize(
    ("cls", "name"),
    [
        (NseBoardMeetings, "board_meetings_sample.json"),
        (NseFinancialResults, "financial_results_sample.json"),
    ],
)
def test_real_samples_parse(cls, name):
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "private" / name
    if not path.exists():
        pytest.skip("private sample not present (not committed)")
    c, raw = _conn(cls), path.read_bytes()
    assert header_fingerprint(c.extract_header(raw)) in c.known_fingerprints
    df = c.parse(raw, date(2024, 5, 1))
    assert df.height == 40 and not [
        i for i in c.validate(df, date(2024, 5, 1)) if i.severity == "BLOCK"
    ]


def test_integrated_filings_parse_and_short_pages_are_quarantined():
    from stockapp.ingest.nse_results import NseIntegratedResults

    row = {
        "symbol": "ABC", "qe_Date": "30-JUN-2025", "broadcast_Date": "31-Jul-2025 22:59:52",
        "consolidated": "Consolidated", "audited": "Un-Audited", "xbrl": "x.xml",
        "ixbrl": "x.html", "seq_Id": "7", "cmName": "ABC Ltd",
        "type": "Integrated Filing- Financials", "type_Sub": "Original",
    }  # fmt: skip
    c = _conn(NseIntegratedResults)
    full = json.dumps({"data": [row], "totalCount": 1}).encode()
    assert header_fingerprint(c.extract_header(full)) in c.known_fingerprints
    r = c.parse(full, date(2025, 7, 1)).row(0, named=True)
    assert r["period_to"] == date(2025, 6, 30) and r["published_at"] == datetime(
        2025, 7, 31, 22, 59, 52
    )
    short = json.dumps({"data": [row], "totalCount": 20}).encode()
    assert header_fingerprint(c.extract_header(short)) not in c.known_fingerprints
    assert "size=10000" in c.url_for(date(2025, 7, 1))
