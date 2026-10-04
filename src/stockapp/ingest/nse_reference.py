"""NSE reference files, fetched as dated snapshots (partition = fetch date).

* ``symbolchange.csv``: every NSE symbol change since 1999 (company, old symbol, new symbol,
  date). No header row. Authoritative input for the company master: a symbol can be reused by a
  different company after a change (e.g. KPIT became BSOFT in 2019), so mappings are time-bound.
* ``EQUITY_L.csv``: currently listed equities with ISIN, listing date and face value. A snapshot of
  today only; history comes from the daily price files.
"""

from __future__ import annotations

import io
from datetime import date
from typing import ClassVar

import polars as pl

from stockapp.ingest.base import DailyFileConnector, Issue, header_fingerprint

BASE = "https://nsearchives.nseindia.com/content/equities"
SYMBOL_CHANGE_SHAPE = "symbolchange:4 columns, no header"
EQUITY_LIST_HEADER = (
    "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, "
    "FACE VALUE"
)


class NseSymbolChanges(DailyFileConnector):
    source_id = "nse_symbol_changes"
    dataset = "nse_symbol_changes"
    partition_col = "snapshot_date"
    min_rows: ClassVar[int] = 500  # 1,065 rows on 2026-10-04
    known_fingerprints: ClassVar[frozenset[str]] = frozenset(
        {header_fingerprint(SYMBOL_CHANGE_SHAPE)}
    )

    def url_for(self, day: date) -> str:
        return f"{BASE}/symbolchange.csv"

    def filename_for(self, day: date) -> str:
        return "symbolchange.csv"

    def extract_header(self, content: bytes) -> str:
        # No header row: fingerprint the shape (4 comma-separated fields, the last a date).
        first = content.decode("latin-1").splitlines()[0]
        parts = first.rsplit(",", 3)
        if len(parts) != 4 or len(parts[3].strip()) != 11:
            return f"unexpected first line: {first[:80]}"
        return SYMBOL_CHANGE_SHAPE

    def parse(self, content: bytes, day: date) -> pl.DataFrame:
        rows = []
        for line in content.decode("latin-1").splitlines():
            if not line.strip():
                continue
            # company names can contain commas: split from the right
            company, old, new, when = (p.strip() for p in line.rsplit(",", 3))
            rows.append((company, old, new, when))
        return pl.DataFrame(
            rows, schema=["company", "old_symbol", "new_symbol", "change_date"], orient="row"
        ).with_columns(
            pl.col("change_date").str.to_titlecase().str.to_date("%d-%b-%Y", strict=True),
            pl.lit(day).alias("snapshot_date"),
        )

    def validate(self, df: pl.DataFrame, day: date) -> list[Issue]:
        issues: list[Issue] = []
        if df.height < self.min_rows:
            issues.append(Issue("BLOCK", "too_few_rows", {"rows": df.height, "min": self.min_rows}))
        empty = df.filter((pl.col("old_symbol") == "") | (pl.col("new_symbol") == ""))
        if empty.height:
            issues.append(Issue("BLOCK", "empty_symbol", {"sample": empty.head(3).rows()}))
        same = df.filter(pl.col("old_symbol") == pl.col("new_symbol"))
        if same.height:
            issues.append(Issue("WARN", "unchanged_symbol", {"sample": same.head(3).rows()}))
        future = df.filter(pl.col("change_date") > day)
        if future.height:
            issues.append(Issue("WARN", "future_change_date", {"sample": future.head(3).rows()}))
        return issues


class NseEquityList(DailyFileConnector):
    source_id = "nse_equity_list"
    dataset = "nse_equity_list"
    partition_col = "snapshot_date"
    min_rows: ClassVar[int] = 1500  # about 2,600 rows on 2026-10-04
    known_fingerprints: ClassVar[frozenset[str]] = frozenset(
        {header_fingerprint(EQUITY_LIST_HEADER)}
    )

    def url_for(self, day: date) -> str:
        return f"{BASE}/EQUITY_L.csv"

    def filename_for(self, day: date) -> str:
        return "EQUITY_L.csv"

    def extract_header(self, content: bytes) -> str:
        return content.decode("latin-1").splitlines()[0]

    def parse(self, content: bytes, day: date) -> pl.DataFrame:
        raw = pl.read_csv(io.BytesIO(content), infer_schema=False, encoding="latin1")
        raw = raw.rename({c: c.strip() for c in raw.columns})
        s = pl.col
        return raw.select(
            s("SYMBOL").str.strip_chars().alias("symbol"),
            s("NAME OF COMPANY").str.strip_chars().alias("company"),
            s("SERIES").str.strip_chars().alias("series"),
            s("DATE OF LISTING").str.strip_chars().str.to_titlecase()
            .str.to_date("%d-%b-%Y", strict=True).alias("listing_date"),
            s("ISIN NUMBER").str.strip_chars().alias("isin"),
            s("FACE VALUE").str.strip_chars().cast(pl.Float64, strict=True).alias("face_value"),
            s("MARKET LOT").str.strip_chars().cast(pl.Int64, strict=True).alias("market_lot"),
            pl.lit(day).alias("snapshot_date"),
        )  # fmt: skip

    def validate(self, df: pl.DataFrame, day: date) -> list[Issue]:
        issues: list[Issue] = []
        if df.height < self.min_rows:
            issues.append(Issue("BLOCK", "too_few_rows", {"rows": df.height, "min": self.min_rows}))
        dups = df.group_by("symbol").len().filter(pl.col("len") > 1)
        if dups.height:
            issues.append(Issue("BLOCK", "duplicate_symbols", {"sample": dups.head(5).rows()}))
        nulls = {c: n for c in ("symbol", "isin") if (n := df[c].null_count())}
        if nulls:
            issues.append(Issue("BLOCK", "required_nulls", nulls))
        return issues


SECTOR_LIST_HEADER = "Company Name,Industry,Symbol,Series,ISIN Code"


class NseSectorList(DailyFileConnector):
    """Nifty Total Market constituents (about 750 stocks: Nifty 500 plus microcaps) with NSE's
    industry classification (22 macro sectors). Current snapshot only; used for sector weights and
    caps. Stocks outside the list are 'Unclassified'."""

    source_id = "nse_sector_list"
    dataset = "nse_sector_list"
    partition_col = "snapshot_date"
    min_rows: ClassVar[int] = 600
    known_fingerprints: ClassVar[frozenset[str]] = frozenset(
        {header_fingerprint(SECTOR_LIST_HEADER)}
    )

    def url_for(self, day: date) -> str:
        return "https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv"

    def filename_for(self, day: date) -> str:
        return "ind_niftytotalmarket_list.csv"

    def extract_header(self, content: bytes) -> str:
        return content.decode("latin-1").splitlines()[0]

    def parse(self, content: bytes, day: date) -> pl.DataFrame:
        raw = pl.read_csv(io.BytesIO(content), infer_schema=False, encoding="latin1")
        return raw.select(
            pl.col("Symbol").str.strip_chars().alias("symbol"),
            pl.col("Company Name").str.strip_chars().alias("company"),
            pl.col("Industry").str.strip_chars().alias("sector"),
            pl.col("Series").str.strip_chars().alias("series"),
            pl.col("ISIN Code").str.strip_chars().alias("isin"),
            pl.lit(day).alias("snapshot_date"),
        )

    def validate(self, df: pl.DataFrame, day: date) -> list[Issue]:
        issues: list[Issue] = []
        if df.height < self.min_rows:
            issues.append(Issue("BLOCK", "too_few_rows", {"rows": df.height, "min": self.min_rows}))
        nulls = {c: n for c in ("symbol", "sector", "isin") if (n := df[c].null_count())}
        if nulls:
            issues.append(Issue("BLOCK", "required_nulls", nulls))
        if df.group_by("symbol").len().filter(pl.col("len") > 1).height:
            issues.append(Issue("BLOCK", "duplicate_symbols", {}))
        return issues
