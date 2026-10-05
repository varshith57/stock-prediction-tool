"""Results-date features are point in time: only what was announced or published by the signal
date's 20:00 counts, and adding later data never changes an earlier week's features."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl
import pytest

from stockapp.features.events import event_features, map_company, map_isin, map_symbol

T = date(2024, 7, 5)  # a Friday
ISIN = pl.DataFrame({"isin": ["INE1", "INE1-OLD"], "company_id": ["ABC", "ABC"]})


def _sessions(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _closes() -> pl.DataFrame:
    days = _sessions(date(2024, 4, 1), date(2024, 7, 31))
    px = [100.0 + i for i in range(len(days))]
    px[days.index(date(2024, 4, 22))] = px[days.index(date(2024, 4, 19))] * 1.10  # +10% reaction
    return pl.DataFrame({"company_id": "ABC", "trade_date": days, "adj_close": px})


def _meetings(rows) -> pl.DataFrame:
    return pl.DataFrame(
        rows, schema=["isin", "meeting_date", "announced_at", "is_results"], orient="row"
    )


def _filings(rows) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=["isin", "period_to", "published_at"], orient="row")


MEET = [
    ("INE1", date(2024, 7, 10), datetime(2024, 7, 2, 19, 0), True),  # known: 5 days ahead
    ("INE1", date(2024, 7, 8), datetime(2024, 7, 5, 21, 0), True),  # announced after 20:00
    ("INE1", date(2024, 7, 9), datetime(2024, 7, 1, 9, 0), False),  # not a results meeting
]
FILE = [
    ("INE1-OLD", date(2024, 3, 31), datetime(2024, 4, 19, 18, 0)),  # after close: reacts 22 Apr
    ("INE1", date(2024, 3, 31), datetime(2024, 4, 20, 10, 0)),  # same period, later: ignored
]


def _run(meet, files) -> dict:
    s = pl.DataFrame({"company_id": ["ABC"], "trade_date": [T]})
    m, f = map_isin(_meetings(meet), ISIN), map_isin(_filings(files), ISIN)
    return event_features(s, m, f, _closes()).row(0, named=True)


def test_results_features_use_only_what_was_known():
    f = _run(MEET, FILE)
    assert f["results_ahead_days"] == 5 and f["results_this_week"] == 1.0
    assert f["days_since_results"] == (T - date(2024, 4, 19)).days
    assert f["last_results_reaction"] == pytest.approx(0.10)  # old ISIN still maps


def test_later_data_never_changes_the_past():
    later = [
        ("INE1", date(2024, 7, 9), datetime(2024, 7, 6, 9, 0), True),  # announced next day
    ]
    later_file = [("INE1", date(2024, 6, 30), datetime(2024, 7, 5, 21, 30))]  # after 20:00
    assert _run(MEET + later, FILE + later_file) == _run(MEET, FILE)
    # published at 19:00 on T: known by 20:00, but its reaction session (Mon) hasn't happened
    f = _run(MEET, [*FILE, ("INE1", date(2024, 6, 30), datetime(2024, 7, 5, 19, 0))])
    assert f["days_since_results"] == 0 and f["last_results_reaction"] == pytest.approx(0.10)


def test_no_known_meeting_means_null_not_zero():
    f = _run([], [])
    assert f["results_ahead_days"] is None and f["results_this_week"] == 0.0
    assert f["days_since_results"] is None and f["last_results_reaction"] is None


def test_symbol_mapping_uses_the_symbol_valid_on_the_date():
    symbols = pl.DataFrame(
        {
            "company_id": ["NEW", "NEW"],
            "symbol": ["OLDSYM", "NEWSYM"],
            "valid_from": [date(1900, 1, 1), date(2025, 3, 1)],
            "valid_to": [date(2025, 2, 28), date(9999, 12, 31)],
        }
    )
    rows = pl.DataFrame(
        {
            "symbol": ["OLDSYM", "NEWSYM", "OLDSYM"],
            "published_at": [datetime(2025, 1, 5), datetime(2025, 5, 1), datetime(2025, 5, 1)],
        }
    )
    out = map_symbol(rows, symbols, "published_at")
    assert out.height == 2 and set(out["company_id"]) == {"NEW"}  # stale symbol dropped


def test_unknown_isin_falls_back_to_the_symbol():
    isins = pl.DataFrame({"isin": ["INE-NEW"], "company_id": ["SHIL"]})
    symbols = pl.DataFrame(
        {"company_id": ["SHIL"], "symbol": ["SHILSYM"], "valid_from": [date(1900, 1, 1)],
         "valid_to": [date(9999, 12, 31)]}
    )  # fmt: skip
    rows = pl.DataFrame(
        {
            "symbol": ["SHILSYM", "SHILSYM", "OTHER"],
            "isin": ["INE-NEW", "INE-PRESPLIT", "INE-X"],
            "published_at": [datetime(2023, 1, 1)] * 3,
        }
    )
    out = map_company(rows, isins, symbols, "published_at")
    assert out.height == 2 and set(out["company_id"]) == {"SHIL"}  # one by ISIN, one by symbol
