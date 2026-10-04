"""NSE security-wise delivery position (MTO file), 2016 to today in one stable format.

Layout: 3 preamble lines (title, a control record, a line with the trade date), a header line, then
``20,<sr>,<symbol>,<series>,<qty traded>,<deliverable qty>,<delivery %>`` records.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime
from typing import ClassVar

import polars as pl

from stockapp.ingest.base import DailyFileConnector, Issue, header_fingerprint

MTO_HEADER_V1 = (
    "Record Type,Sr No,Name of Security,Quantity Traded,"
    "Deliverable Quantity(gross across client level),"
    "% of Deliverable Quantity to Traded Quantity"
)
_TRADE_DATE = re.compile(r"Trade Date <(\d{2}-[A-Za-z]{3}-\d{4})>")
_HEADER_LINE = 3  # zero-based index of the header line


class NseMtoDelivery(DailyFileConnector):
    source_id = "nse_mto_delivery"
    dataset = "nse_cm_delivery"
    min_rows: ClassVar[int] = 1000  # about 1,550 rows in 2016, 3,400 in 2026
    known_fingerprints: ClassVar[frozenset[str]] = frozenset({header_fingerprint(MTO_HEADER_V1)})

    def url_for(self, day: date) -> str:
        return f"https://nsearchives.nseindia.com/archives/equities/mto/MTO_{day:%d%m%Y}.DAT"

    def filename_for(self, day: date) -> str:
        return f"MTO_{day:%d%m%Y}.DAT"

    def extract_header(self, content: bytes) -> str:
        return _lines(content)[_HEADER_LINE]

    def parse(self, content: bytes, day: date) -> pl.DataFrame:
        lines = _lines(content)
        m = _TRADE_DATE.search(lines[2])
        if not m:
            raise ValueError(f"no trade date in preamble: {lines[2][:80]!r}")
        file_day = datetime.strptime(m.group(1).title(), "%d-%b-%Y").date()
        records = [ln.split(",") for ln in lines[_HEADER_LINE + 1 :] if ln.startswith("20,")]
        bad = [r for r in records if len(r) != 7]
        if bad:
            raise ValueError(f"{len(bad)} records without 7 fields, e.g. {bad[0]}")
        df = pl.DataFrame(
            records,
            schema=["record_type", "sr_no", "symbol", "series", "qty", "deliv", "pct"],
            orient="row",
        )
        return df.select(
            pl.lit(file_day).alias("trade_date"),
            pl.col("symbol").str.strip_chars(),
            pl.col("series").str.strip_chars(),
            pl.col("qty").str.strip_chars().cast(pl.Int64, strict=True).alias("qty_traded"),
            pl.col("deliv").str.strip_chars().cast(pl.Int64, strict=True).alias("deliverable_qty"),
            pl.col("pct").str.strip_chars().cast(pl.Float64, strict=True).alias("delivery_pct"),
        )

    def validate(self, df: pl.DataFrame, day: date) -> list[Issue]:
        issues: list[Issue] = []
        if df.height < self.min_rows:
            issues.append(Issue("BLOCK", "too_few_rows", {"rows": df.height, "min": self.min_rows}))
        if df.height and df["trade_date"][0] != day:
            issues.append(Issue("BLOCK", "date_mismatch", {"found": df["trade_date"][0]}))
        dups = df.group_by("symbol", "series").len().filter(pl.col("len") > 1)
        if dups.height:
            issues.append(Issue("BLOCK", "duplicate_keys", {"sample": dups.head(5).rows()}))
        over = df.filter(pl.col("deliverable_qty") > pl.col("qty_traded"))
        if over.height:
            issues.append(
                Issue(
                    "WARN",
                    "delivery_above_traded",
                    {"count": over.height, "sample": over.head(5).rows()},
                )
            )
        return issues


def _lines(content: bytes) -> list[str]:
    text = io.TextIOWrapper(io.BytesIO(content), encoding="utf-8-sig", errors="strict").read()
    lines = text.splitlines()
    if len(lines) <= _HEADER_LINE:
        raise ValueError("file too short to contain a header")
    return lines
