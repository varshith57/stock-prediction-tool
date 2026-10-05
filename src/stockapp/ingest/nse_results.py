"""Results dates from NSE's JSON API, one month at a time (no row cap seen: a whole May 2024
results season came back in one call, matching weekly calls).

* ``NseBoardMeetings`` (``corporate-board-meetings``): board meetings by meeting month, with the
  purpose ("Financial Results", "Dividend", ...) and **when the meeting was announced**
  (``bm_timestamp``). Upcoming results are known only from that time on, which keeps features
  point-in-time. Meetings are announced ahead, so the coming month is fetched too.
* ``NseFinancialResults`` (``corporates-financial-results``, quarterly): results filings by
  filing month, with the **publication time** (``broadCastDate``), the period, consolidated or
  standalone, audited or not, and the XBRL link (the route to fundamentals later).

Payload keys vary a little over the years, so the format check covers the keys this code uses:
all of them present is the known format; anything missing quarantines the month.
"""

from __future__ import annotations

import calendar as _cal
import json
from datetime import date
from typing import ClassVar

import polars as pl

from stockapp.ingest.base import DailyFileConnector, Issue, header_fingerprint
from stockapp.ingest.nse_corp_actions import month_start

EMPTY = "<empty list>"
REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings"


def _month_bounds(day: date) -> tuple[date, date]:
    first = month_start(day)
    return first, first.replace(day=_cal.monthrange(first.year, first.month)[1])


def _ts(col: str) -> pl.Expr:
    """'05-Jul-2024 19:27:13' (IST) -> naive datetime; '-' or empty -> null."""
    c = pl.col(col).str.strip_chars()
    return (
        pl.when(c.is_in(["-", ""]) | c.is_null())
        .then(None)
        .otherwise(c)
        .str.to_datetime("%d-%b-%Y %H:%M:%S", strict=False)
    )


def _day(col: str) -> pl.Expr:
    c = pl.col(col).str.strip_chars()
    return (
        pl.when(c.is_in(["-", ""]) | c.is_null())
        .then(None)
        .otherwise(c)
        .str.to_date("%d-%b-%Y", strict=False)
    )


class _MonthlyJson(DailyFileConnector):
    partition_col = "month"
    required: ClassVar[tuple[str, ...]]
    api_path: ClassVar[str]
    request_headers: ClassVar[dict[str, str]] = {"Accept": "application/json", "Referer": REFERER}

    def partition_key(self, day: date) -> str:
        return f"{day:%Y-%m}"

    def url_for(self, day: date) -> str:
        first, last = _month_bounds(day)
        return (
            f"https://www.nseindia.com/api/{self.api_path}"
            f"&from_date={first:%d-%m-%Y}&to_date={last:%d-%m-%Y}"
        )

    def extract_header(self, content: bytes) -> str:
        rows = json.loads(content)
        if not isinstance(rows, list):
            raise ValueError(f"expected a JSON list, got {type(rows).__name__}")
        if not rows:
            return EMPTY
        keys = {k for r in rows for k in r}
        if set(self.required) <= keys:
            return ",".join(self.required)
        return ",".join(sorted(keys))  # unknown: quarantined

    def _rows(self, content: bytes) -> pl.DataFrame:
        rows = json.loads(content)
        return pl.DataFrame(
            [
                {k: (None if r.get(k) is None else str(r.get(k))) for k in self.required}
                for r in rows
            ],
            schema=dict.fromkeys(self.required, pl.String),
        )


class NseBoardMeetings(_MonthlyJson):
    source_id = "nse_board_meetings"
    dataset = "nse_board_meetings"
    api_path = "corporate-board-meetings?index=equities"
    required = ("bm_symbol", "sm_isin", "bm_date", "bm_purpose", "bm_desc", "bm_timestamp")
    known_fingerprints: ClassVar[frozenset[str]] = frozenset(
        {header_fingerprint(",".join(required)), header_fingerprint(EMPTY)}
    )

    def filename_for(self, day: date) -> str:
        return f"board_meetings_{day:%Y-%m}.json"

    def parse(self, content: bytes, day: date) -> pl.DataFrame:
        month = self.partition_key(day)
        if not json.loads(content):
            return pl.DataFrame(schema=BM_SCHEMA).with_columns(pl.lit(month).alias("month"))
        return self._rows(content).select(
            pl.col("bm_symbol").str.strip_chars().alias("symbol"),
            pl.col("sm_isin").str.strip_chars().alias("isin"),
            _day("bm_date").alias("meeting_date"),
            pl.col("bm_purpose").str.strip_chars().alias("purpose"),
            pl.col("bm_desc").str.strip_chars().alias("description"),
            _ts("bm_timestamp").alias("announced_at"),
            pl.col("bm_purpose")
            .str.contains("(?i)result")  # "Results" (2016-17), "Financial Results" (later)
            .fill_null(False)
            .alias("is_results"),
            pl.lit(month).alias("month"),
        )

    def validate(self, df: pl.DataFrame, day: date) -> list[Issue]:
        if df.height == 0:
            return [Issue("WARN", "no_meetings_in_month", {"month": self.partition_key(day)})]
        issues = []
        nulls = {c: n for c in ("symbol", "meeting_date") if (n := df[c].null_count())}
        if nulls:
            issues.append(Issue("BLOCK", "required_nulls", nulls))
        if (n := df["announced_at"].null_count()) > df.height * 0.05:
            issues.append(Issue("WARN", "announcement_time_missing", {"rows": n}))
        return issues


class NseFinancialResults(_MonthlyJson):
    source_id = "nse_financial_results"
    dataset = "nse_financial_results"
    api_path = "corporates-financial-results?index=equities&period=Quarterly"
    required = (
        "symbol", "isin", "fromDate", "toDate", "broadCastDate", "filingDate", "consolidated",
        "audited", "relatingTo", "xbrl",
    )  # fmt: skip
    known_fingerprints: ClassVar[frozenset[str]] = frozenset(
        {header_fingerprint(",".join(required)), header_fingerprint(EMPTY)}
    )

    def filename_for(self, day: date) -> str:
        return f"financial_results_{day:%Y-%m}.json"

    def parse(self, content: bytes, day: date) -> pl.DataFrame:
        month = self.partition_key(day)
        if not json.loads(content):
            return pl.DataFrame(schema=FR_SCHEMA).with_columns(pl.lit(month).alias("month"))
        return self._rows(content).select(
            pl.col("symbol").str.strip_chars(),
            pl.col("isin").str.strip_chars(),
            _day("fromDate").alias("period_from"),
            _day("toDate").alias("period_to"),
            _ts("broadCastDate").alias("published_at"),
            pl.col("consolidated").str.strip_chars(),
            pl.col("audited").str.strip_chars(),
            pl.col("relatingTo").str.strip_chars().alias("relating_to"),
            pl.col("xbrl").str.strip_chars().alias("xbrl_url"),
            pl.lit(month).alias("month"),
        )

    def validate(self, df: pl.DataFrame, day: date) -> list[Issue]:
        if df.height == 0:
            return [Issue("WARN", "no_results_in_month", {"month": self.partition_key(day)})]
        nulls = {c: n for c in ("symbol", "published_at") if (n := df[c].null_count())}
        return [Issue("WARN", "required_nulls", nulls)] if nulls else []


class NseIntegratedResults(_MonthlyJson):
    """Quarterly results filed as SEBI "Integrated Filing (Financials)", which replaced the
    results feed above from early 2025 (the old feed drops to a handful of rows a month).

    The API pages 20 rows by default and reports ``totalCount``; ``size`` asks for everything in
    one call, and a short page fails the format check rather than silently losing filings. No ISIN
    here: companies are matched by symbol as of the publication date. The feed mixes results
    ("Integrated Filing- Financials") with governance filings, and originals with revisions:
    results-date features use original Financials only."""

    source_id = "nse_integrated_results"
    dataset = "nse_integrated_results"
    api_path = "integrated-filing-results?index=equities&period=Quarterly&size=10000"
    required = (
        "symbol", "qe_Date", "broadcast_Date", "consolidated", "audited", "xbrl", "ixbrl",
        "seq_Id", "type", "type_Sub",
    )  # fmt: skip
    known_fingerprints: ClassVar[frozenset[str]] = frozenset(
        {header_fingerprint(",".join(required)), header_fingerprint(EMPTY)}
    )
    request_headers: ClassVar[dict[str, str]] = {
        "Accept": "application/json",
        "Referer": "https://www.nseindia.com/companies-listing/corporate-integrated-filing",
    }

    def filename_for(self, day: date) -> str:
        return f"integrated_results_{day:%Y-%m}.json"

    def _payload(self, content: bytes) -> tuple[list[dict], int]:
        d = json.loads(content)
        if not isinstance(d, dict) or "data" not in d:
            raise ValueError("expected {data, totalCount}")
        return d["data"], int(d.get("totalCount", len(d["data"])))

    def extract_header(self, content: bytes) -> str:
        rows, total = self._payload(content)
        if len(rows) != total:
            return f"short page: {len(rows)} of {total}"  # unknown: quarantined
        return super().extract_header(json.dumps(rows).encode())

    def parse(self, content: bytes, day: date) -> pl.DataFrame:
        month = self.partition_key(day)
        rows, _ = self._payload(content)
        if not rows:
            return pl.DataFrame(schema=IR_SCHEMA).with_columns(pl.lit(month).alias("month"))
        return self._rows(json.dumps(rows).encode()).select(
            pl.col("symbol").str.strip_chars(),
            pl.col("qe_Date").str.strip_chars().str.to_titlecase().str.to_date("%d-%b-%Y",
                                                                               strict=False)
            .alias("period_to"),
            _ts("broadcast_Date").alias("published_at"),
            pl.col("consolidated").str.strip_chars(),
            pl.col("audited").str.strip_chars(),
            pl.col("xbrl").str.strip_chars().alias("xbrl_url"),
            pl.col("ixbrl").str.strip_chars().alias("ixbrl_url"),
            pl.col("seq_Id").alias("seq_id"),
            # "Integrated Filing- Financials" (results) or "...- Governance"; Original/Revision
            pl.col("type").str.strip_chars().alias("filing_type"),
            pl.col("type_Sub").str.strip_chars().alias("filing_sub"),
            pl.lit(month).alias("month"),
        )  # fmt: skip

    def validate(self, df: pl.DataFrame, day: date) -> list[Issue]:
        if df.height == 0:
            return [Issue("WARN", "no_results_in_month", {"month": self.partition_key(day)})]
        nulls = {c: n for c in ("symbol", "published_at") if (n := df[c].null_count())}
        return [Issue("WARN", "required_nulls", nulls)] if nulls else []


IR_SCHEMA = {
    "symbol": pl.String,
    "period_to": pl.Date,
    "published_at": pl.Datetime,
    "consolidated": pl.String,
    "audited": pl.String,
    "xbrl_url": pl.String,
    "ixbrl_url": pl.String,
    "seq_id": pl.String,
    "filing_type": pl.String,
    "filing_sub": pl.String,
}
BM_SCHEMA = {
    "symbol": pl.String,
    "isin": pl.String,
    "meeting_date": pl.Date,
    "purpose": pl.String,
    "description": pl.String,
    "announced_at": pl.Datetime,
    "is_results": pl.Boolean,
}
FR_SCHEMA = {
    "symbol": pl.String,
    "isin": pl.String,
    "period_from": pl.Date,
    "period_to": pl.Date,
    "published_at": pl.Datetime,
    "consolidated": pl.String,
    "audited": pl.String,
    "relating_to": pl.String,
    "xbrl_url": pl.String,
}
