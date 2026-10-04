"""NSE capital-market bhavcopy, legacy format (published until 2024-07-05).

Same content as UDiFF minus instrument id and name. Header unchanged from 2016 to 2024 (checked on
samples from 2016, 2018, 2020, 2022 and 2024). ``TOTTRDVAL`` is turnover in rupees.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from typing import ClassVar

import polars as pl

from stockapp.ingest.base import DailyFileConnector, Issue, header_fingerprint
from stockapp.ingest.calendar import record_session
from stockapp.ingest.prices import validate_price_frame

LEGACY_HEADER_V1 = (
    "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,"
    "TOTALTRADES,ISIN,"
)
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

COLUMNS: dict[str, tuple[str, type[pl.DataType] | pl.DataType]] = {
    "TIMESTAMP": ("trade_date", pl.Date),
    "SYMBOL": ("symbol", pl.String),
    "SERIES": ("series", pl.String),
    "ISIN": ("isin", pl.String),
    "OPEN": ("open", pl.Float64),
    "HIGH": ("high", pl.Float64),
    "LOW": ("low", pl.Float64),
    "CLOSE": ("close", pl.Float64),
    "LAST": ("last", pl.Float64),
    "PREVCLOSE": ("prev_close", pl.Float64),
    "TOTTRDQTY": ("volume", pl.Int64),
    "TOTTRDVAL": ("value_inr", pl.Float64),
    "TOTALTRADES": ("trades", pl.Int64),
}


class NseLegacyBhavcopy(DailyFileConnector):
    source_id = "nse_legacy_bhavcopy"
    dataset = "nse_cm_bhavcopy_legacy"
    min_rows: ClassVar[int] = 1000  # about 1,600 rows in 2016, 2,800 in 2024
    known_fingerprints: ClassVar[frozenset[str]] = frozenset({header_fingerprint(LEGACY_HEADER_V1)})

    def _stem(self, day: date) -> str:
        return f"cm{day:%d}{_MONTHS[day.month - 1]}{day:%Y}bhav.csv"

    def url_for(self, day: date) -> str:
        mon = _MONTHS[day.month - 1]
        return (
            "https://nsearchives.nseindia.com/content/historical/EQUITIES/"
            f"{day:%Y}/{mon}/{self._stem(day)}.zip"
        )

    def filename_for(self, day: date) -> str:
        return f"{self._stem(day)}.zip"

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
                # "04-JAN-2016": title-case the month so %b parses it
                exprs.append(col.str.to_titlecase().str.to_date("%d-%b-%Y", strict=True).alias(dst))
            else:
                exprs.append(col.cast(dtype, strict=True).alias(dst))
        return raw.select(exprs)

    def validate(self, df: pl.DataFrame, day: date) -> list[Issue]:
        return validate_price_frame(df, day, self.min_rows)

    def on_loaded(self, day: date, df: pl.DataFrame) -> None:
        record_session(self.conn, day, evidence=self.source_id)


def _open_csv(content: bytes) -> io.BufferedReader:
    zf = zipfile.ZipFile(io.BytesIO(content))
    names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    if len(names) != 1:
        raise ValueError(f"expected one CSV in the zip, found {names}")
    return zf.open(names[0])  # type: ignore[return-value]
