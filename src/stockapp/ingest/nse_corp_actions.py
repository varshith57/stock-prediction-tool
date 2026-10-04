"""NSE corporate actions (equities), fetched one ex-date month at a time from NSE's JSON API.

Stored as published: the ``subject`` text ("Bonus 1:2", "Face Value Split (Sub-Division) - From Rs
5/- Per Share To Re 1/- Per Share", "Dividend - Rs 2 Per Share") is parsed into ratios in M3.

Point-in-time note: ``caBroadcastDate`` is empty in every payload seen so far, so the API does not
say when an action became public. Ex-dates are fixed in advance of the event, so adjustments keyed
on ex-date are safe; "known-at" for features is the later of record date and ingestion time.
"""

from __future__ import annotations

import calendar as _cal
import json
from datetime import date
from typing import ClassVar

import polars as pl

from stockapp.ingest.base import DailyFileConnector, Issue, header_fingerprint

KEYS_V1 = (
    "bcEndDate,bcStartDate,caBroadcastDate,comp,exDate,faceVal,ind,isin,ndEndDate,ndStartDate,"
    "recDate,series,subject,symbol"
)
EMPTY = "<empty list>"
_DATE_COLS = {
    "exDate": "ex_date",
    "recDate": "record_date",
    "bcStartDate": "book_closure_start",
    "bcEndDate": "book_closure_end",
    "ndStartDate": "no_delivery_start",
    "ndEndDate": "no_delivery_end",
}


def month_start(day: date) -> date:
    return day.replace(day=1)


class NseCorporateActions(DailyFileConnector):
    source_id = "nse_corporate_actions"
    dataset = "nse_corporate_actions"
    partition_col = "month"
    known_fingerprints: ClassVar[frozenset[str]] = frozenset(
        {header_fingerprint(KEYS_V1), header_fingerprint(EMPTY)}
    )
    request_headers: ClassVar[dict[str, str]] = {
        "Accept": "application/json",
        "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions",
    }

    def partition_key(self, day: date) -> str:
        return f"{day:%Y-%m}"

    def url_for(self, day: date) -> str:
        first = month_start(day)
        last = first.replace(day=_cal.monthrange(first.year, first.month)[1])
        return (
            "https://www.nseindia.com/api/corporates-corporateActions?index=equities"
            f"&from_date={first:%d-%m-%Y}&to_date={last:%d-%m-%Y}"
        )

    def filename_for(self, day: date) -> str:
        return f"corporate_actions_{day:%Y-%m}.json"

    def extract_header(self, content: bytes) -> str:
        rows = json.loads(content)
        if not isinstance(rows, list):
            raise ValueError(f"expected a JSON list, got {type(rows).__name__}")
        if not rows:
            return EMPTY
        keys = sorted({k for r in rows for k in r})
        return ",".join(keys)

    def parse(self, content: bytes, day: date) -> pl.DataFrame:
        rows = json.loads(content)
        month = self.partition_key(day)
        if not rows:
            return pl.DataFrame(schema=_SCHEMA).with_columns(pl.lit(month).alias("month"))
        raw = pl.DataFrame(rows, schema={k: pl.String for k in KEYS_V1.split(",")})

        def as_date(src: str) -> pl.Expr:
            col = pl.col(src).str.strip_chars()
            return (
                pl.when(col.is_in(["-", ""]))
                .then(None)
                .otherwise(col)
                .str.to_date("%d-%b-%Y", strict=True)
            )

        return raw.select(
            pl.col("symbol").str.strip_chars(),
            pl.col("series").str.strip_chars(),
            pl.col("isin").str.strip_chars(),
            pl.col("comp").str.strip_chars().alias("company"),
            pl.col("faceVal").str.strip_chars().alias("face_value"),
            pl.col("subject").str.strip_chars(),
            *[as_date(src).alias(dst) for src, dst in _DATE_COLS.items()],
            pl.col("caBroadcastDate").alias("broadcast_at"),
            pl.lit(month).alias("month"),
        )

    def validate(self, df: pl.DataFrame, day: date) -> list[Issue]:
        issues: list[Issue] = []
        if df.height == 0:
            issues.append(Issue("WARN", "no_actions_in_month", {"month": self.partition_key(day)}))
            return issues
        nulls = {c: n for c in ("symbol", "subject", "ex_date") if (n := df[c].null_count())}
        if nulls:
            issues.append(Issue("BLOCK", "required_nulls", nulls))
        first = month_start(day)
        outside = df.filter(
            (pl.col("ex_date").dt.year() != first.year)
            | (pl.col("ex_date").dt.month() != first.month)
        )
        if outside.height:
            issues.append(
                Issue(
                    "WARN",
                    "ex_date_outside_month",
                    {"count": outside.height, "sample": outside.head(3).rows()},
                )
            )
        return issues


_SCHEMA = {
    "symbol": pl.String,
    "series": pl.String,
    "isin": pl.String,
    "company": pl.String,
    "face_value": pl.String,
    "subject": pl.String,
    **{dst: pl.Date for dst in _DATE_COLS.values()},
    "broadcast_at": pl.String,
}
