"""NSE capital-market bhavcopy, UDiFF format (published since July 2024).

One zip per trading day containing one CSV with every NSE CM instrument (all series: EQ, BE, SM,
GB, ...). Silver keeps every row; universe and series filters happen downstream.
Prices are in rupees per share; ``TtlTrfVal`` is turnover in rupees (not lakhs).
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from typing import ClassVar

import polars as pl

from stockapp.ingest.base import DailyFileConnector, Issue, header_fingerprint
from stockapp.ingest.calendar import record_session

# Header of the format seen on 2026-10-01. A different header fails the run as format_changed.
UDIFF_HEADER_V1 = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
    "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,"
    "PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,"
    "TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)

# source column -> (silver column, type)
COLUMNS: dict[str, tuple[str, type[pl.DataType] | pl.DataType]] = {
    "TradDt": ("trade_date", pl.Date),
    "TckrSymb": ("symbol", pl.String),
    "SctySrs": ("series", pl.String),
    "ISIN": ("isin", pl.String),
    "FinInstrmId": ("instrument_id", pl.Int64),
    "FinInstrmTp": ("instrument_type", pl.String),
    "FinInstrmNm": ("name", pl.String),
    "OpnPric": ("open", pl.Float64),
    "HghPric": ("high", pl.Float64),
    "LwPric": ("low", pl.Float64),
    "ClsPric": ("close", pl.Float64),
    "LastPric": ("last", pl.Float64),
    "PrvsClsgPric": ("prev_close", pl.Float64),
    "SttlmPric": ("settlement_price", pl.Float64),
    "TtlTradgVol": ("volume", pl.Int64),
    "TtlTrfVal": ("value_inr", pl.Float64),
    "TtlNbOfTxsExctd": ("trades", pl.Int64),
    "Sgmt": ("segment", pl.String),
    "Src": ("exchange", pl.String),
}
REQUIRED = ("trade_date", "symbol", "series", "isin", "open", "high", "low", "close", "volume")


class NseUdiffBhavcopy(DailyFileConnector):
    source_id = "nse_udiff_bhavcopy"
    dataset = "nse_cm_bhavcopy"
    min_rows: ClassVar[int] = 1000  # a real day has about 3,700 rows; far fewer = truncated file
    known_fingerprints: ClassVar[frozenset[str]] = frozenset({header_fingerprint(UDIFF_HEADER_V1)})

    def url_for(self, day: date) -> str:
        return (
            "https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
        )

    def filename_for(self, day: date) -> str:
        return f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"

    def extract_header(self, content: bytes) -> str:
        with _open_csv(content) as f:
            return f.readline().decode("utf-8-sig")

    def parse(self, content: bytes, day: date) -> pl.DataFrame:
        with _open_csv(content) as f:
            raw = pl.read_csv(f.read(), infer_schema=False)
        exprs = []
        for src, (dst, dtype) in COLUMNS.items():
            col = pl.col(src).str.strip_chars()
            if dtype == pl.Date:
                exprs.append(col.str.to_date("%Y-%m-%d", strict=True).alias(dst))
            else:
                exprs.append(col.cast(dtype, strict=True).alias(dst))
        return raw.select(exprs)

    def validate(self, df: pl.DataFrame, day: date) -> list[Issue]:
        issues: list[Issue] = []
        if df.height < self.min_rows:
            issues.append(Issue("BLOCK", "too_few_rows", {"rows": df.height, "min": self.min_rows}))
        dates = df["trade_date"].unique().to_list()
        if dates != [day]:
            issues.append(Issue("BLOCK", "date_mismatch", {"requested": day, "found": dates[:5]}))
        for col, expected in (("segment", "CM"), ("exchange", "NSE")):
            found = df[col].unique().to_list()
            if found != [expected]:
                issues.append(Issue("BLOCK", f"unexpected_{col}", {"found": found[:5]}))
        nulls = {c: n for c in REQUIRED if (n := df[c].null_count())}
        if nulls:
            issues.append(Issue("BLOCK", "required_nulls", nulls))
        dups = df.group_by("symbol", "series").len().filter(pl.col("len") > 1)
        if dups.height:
            issues.append(
                Issue(
                    "BLOCK", "duplicate_keys", {"count": dups.height, "sample": dups.head(5).rows()}
                )
            )
        return issues

    def on_loaded(self, day: date, df: pl.DataFrame) -> None:
        record_session(self.conn, day, evidence=self.source_id)


def _open_csv(content: bytes) -> io.BufferedReader:
    zf = zipfile.ZipFile(io.BytesIO(content))
    names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    if len(names) != 1:
        raise ValueError(f"expected one CSV in the zip, found {names}")
    return zf.open(names[0])  # type: ignore[return-value]
