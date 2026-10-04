"""Parser and source-level validation for the NSE UDiFF bhavcopy (no network, no database)."""

from datetime import date
from pathlib import Path

import polars as pl
import pytest
from conftest import FIXTURES, zip_csv

from stockapp.ingest.base import header_fingerprint
from stockapp.ingest.nse_udiff import NseUdiffBhavcopy

DAY = date(2026, 10, 1)
PRIVATE_REAL = FIXTURES / "private" / "BhavCopy_NSE_CM_0_0_0_20261001_F_0000.csv.zip"


class Small(NseUdiffBhavcopy):
    min_rows = 3


@pytest.fixture
def conn() -> Small:
    return Small.__new__(Small)  # parse/validate don't touch db, lake or http


def test_url_and_filename():
    c = NseUdiffBhavcopy.__new__(NseUdiffBhavcopy)
    assert c.url_for(DAY).endswith("/content/cm/BhavCopy_NSE_CM_0_0_0_20261001_F_0000.csv.zip")
    assert c.filename_for(DAY) == "BhavCopy_NSE_CM_0_0_0_20261001_F_0000.csv.zip"


def test_header_fingerprint_is_known(conn: Small, udiff_csv: str):
    fp = header_fingerprint(conn.extract_header(zip_csv(udiff_csv)))
    assert fp in Small.known_fingerprints


def test_changed_header_is_not_known(conn: Small, udiff_csv: str):
    changed = udiff_csv.replace("TtlTrfVal", "TtlTrfValLakhs", 1)
    assert header_fingerprint(conn.extract_header(zip_csv(changed))) not in Small.known_fingerprints


def test_fingerprint_ignores_bom_and_spacing():
    assert header_fingerprint("﻿a, b ,c\r\n") == header_fingerprint("a,b,c")


def test_parse_types_and_values(conn: Small, udiff_csv: str):
    df = conn.parse(zip_csv(udiff_csv), DAY)
    assert df.height == 5
    assert df.schema["trade_date"] == pl.Date
    assert df.schema["volume"] == pl.Int64
    assert df.schema["close"] == pl.Float64
    row = df.filter(pl.col("symbol") == "ALPHAIND").row(0, named=True)
    assert row["isin"] == "INE000A01001"
    assert row["series"] == "EQ"
    assert (row["open"], row["high"], row["low"], row["close"]) == (100.0, 104.5, 99.1, 103.2)
    assert row["value_inr"] == 25_800_000.0
    assert conn.validate(df, DAY) == []


def test_unparseable_number_raises(conn: Small, udiff_csv: str):
    bad = udiff_csv.replace("103.20,103.00", "abc,103.00", 1)
    with pytest.raises(pl.exceptions.InvalidOperationError):
        conn.parse(zip_csv(bad), DAY)


def _reasons(conn: Small, df: pl.DataFrame, day: date = DAY) -> set[str]:
    return {i.reason for i in conn.validate(df, day) if i.severity == "BLOCK"}


def test_validate_blocks_wrong_date(conn: Small, udiff_csv: str):
    df = conn.parse(zip_csv(udiff_csv), DAY)
    assert _reasons(conn, df, date(2026, 9, 30)) == {"date_mismatch"}


def test_validate_blocks_duplicates_nulls_and_segment(conn: Small, udiff_csv: str):
    df = conn.parse(zip_csv(udiff_csv), DAY)
    dup = pl.concat([df, df.head(1)])
    assert "duplicate_keys" in _reasons(conn, dup)
    nulls = df.with_columns(
        pl.when(pl.col("symbol") == "BETAFIN").then(None).otherwise(pl.col("close")).alias("close")
    )
    assert "required_nulls" in _reasons(conn, nulls)
    seg = df.with_columns(pl.lit("FO").alias("segment"))
    assert "unexpected_segment" in _reasons(conn, seg)


def test_validate_blocks_truncated_file(udiff_csv: str):
    c = NseUdiffBhavcopy.__new__(NseUdiffBhavcopy)  # real min_rows = 1000
    df = c.parse(zip_csv(udiff_csv), DAY)
    assert "too_few_rows" in _reasons(c, df)  # type: ignore[arg-type]


@pytest.mark.skipif(not PRIVATE_REAL.exists(), reason="real NSE sample not present (gitignored)")
def test_real_file_parses_and_validates():
    c = NseUdiffBhavcopy.__new__(NseUdiffBhavcopy)
    content = Path(PRIVATE_REAL).read_bytes()
    assert header_fingerprint(c.extract_header(content)) in c.known_fingerprints
    df = c.parse(content, DAY)
    assert df.height > 3000
    assert c.validate(df, DAY) == []
    assert df.filter(pl.col("series") == "EQ").height > 2000
