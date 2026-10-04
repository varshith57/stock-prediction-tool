"""NSE daily index closes (``ind_close_all``): every NSE index including Nifty 50, Nifty 500 and
India VIX. Price indices only; total-return indices are not in this file.

Quirk: some files list an index twice (seen for Nifty 50 on 2016-01-04). Identical duplicates are
dropped. Differing duplicates block the file for Nifty 50/500; for other indices both rows are kept
and flagged (WARN), since nothing downstream uses them yet.
"""

from __future__ import annotations

import io
from datetime import date
from typing import ClassVar

import polars as pl

from stockapp.ingest.base import DailyFileConnector, Issue, header_fingerprint

INDEX_HEADER_V1 = (
    "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,"
    "Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield"
)
COLUMNS = {
    "Index Name": "index_name",
    "Open Index Value": "open",
    "High Index Value": "high",
    "Low Index Value": "low",
    "Closing Index Value": "close",
    "Points Change": "points_change",
    "Change(%)": "change_pct",
    "Volume": "volume",
    "Turnover (Rs. Cr.)": "turnover_cr",
    "P/E": "pe",
    "P/B": "pb",
    "Div Yield": "div_yield",
}
MUST_HAVE = ("nifty 50", "nifty 500")  # BLOCK if missing
SHOULD_HAVE = ("india vix",)  # WARN if missing


class NseIndexClose(DailyFileConnector):
    source_id = "nse_index_close"
    dataset = "nse_index_close"
    min_rows: ClassVar[int] = 30  # about 60 indices in 2016, 200+ in 2026
    known_fingerprints: ClassVar[frozenset[str]] = frozenset({header_fingerprint(INDEX_HEADER_V1)})

    def url_for(self, day: date) -> str:
        return f"https://nsearchives.nseindia.com/content/indices/ind_close_all_{day:%d%m%Y}.csv"

    def filename_for(self, day: date) -> str:
        return f"ind_close_all_{day:%d%m%Y}.csv"

    def extract_header(self, content: bytes) -> str:
        return content.decode("utf-8-sig").splitlines()[0]

    def parse(self, content: bytes, day: date) -> pl.DataFrame:
        raw = pl.read_csv(io.BytesIO(content), infer_schema=False)
        numeric = [
            pl.col(src)
            .str.strip_chars()
            .replace("-", None)
            .cast(pl.Float64, strict=True)
            .alias(dst)
            for src, dst in COLUMNS.items()
            if dst != "index_name"
        ]
        return raw.select(
            pl.col("Index Name").str.strip_chars().alias("index_name"),
            pl.col("Index Date")
            .str.strip_chars()
            .str.to_date("%d-%m-%Y", strict=True)
            .alias("trade_date"),
            *numeric,
        ).unique(maintain_order=True)

    def validate(self, df: pl.DataFrame, day: date) -> list[Issue]:
        issues: list[Issue] = []
        if df.height < self.min_rows:
            issues.append(Issue("BLOCK", "too_few_rows", {"rows": df.height, "min": self.min_rows}))
        dates = df["trade_date"].unique().to_list()
        if dates != [day]:
            issues.append(Issue("BLOCK", "date_mismatch", {"requested": day, "found": dates[:5]}))
        dup_names = df.group_by("index_name").len().filter(pl.col("len") > 1)["index_name"]
        core_dups = [n for n in dup_names.to_list() if n.lower() in MUST_HAVE]
        if core_dups:
            issues.append(Issue("BLOCK", "conflicting_core_duplicates", {"indices": core_dups}))
        elif dup_names.len():
            issues.append(
                Issue("WARN", "conflicting_duplicates", {"indices": dup_names.to_list()[:10]})
            )
        names = set(df["index_name"].str.to_lowercase().to_list())
        missing = [n for n in MUST_HAVE if n not in names]
        if missing:
            issues.append(Issue("BLOCK", "missing_core_index", {"missing": missing}))
        soft = [n for n in SHOULD_HAVE if n not in names]
        if soft:
            issues.append(Issue("WARN", "missing_index", {"missing": soft}))
        if df["close"].null_count():
            nulls = df.filter(pl.col("close").is_null())["index_name"].to_list()
            issues.append(Issue("WARN", "index_without_close", {"indices": nulls[:10]}))
        return issues
