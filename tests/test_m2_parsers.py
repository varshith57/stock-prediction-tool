"""Parsers and source-level validation for the M2 sources (no network, no database)."""

import io
import json
import zipfile
from datetime import date

import polars as pl
import pytest
from conftest import FIXTURES, zip_csv
from synthetic import STOCKS, corp_actions_json, index_csv, legacy_zip, mto_bytes

from stockapp.ingest.base import header_fingerprint
from stockapp.ingest.nse_corp_actions import NseCorporateActions
from stockapp.ingest.nse_index import NseIndexClose
from stockapp.ingest.nse_legacy import NseLegacyBhavcopy
from stockapp.ingest.nse_mto import NseMtoDelivery

DAY = date(2016, 1, 4)
PRIVATE = FIXTURES / "private"


def bare(cls, **attrs):
    obj = cls.__new__(cls)  # parse/validate need no db, lake or http
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


def blocks(conn, df, day=DAY) -> set[str]:
    return {i.reason for i in conn.validate(df, day) if i.severity == "BLOCK"}


def warns(conn, df, day=DAY) -> set[str]:
    return {i.reason for i in conn.validate(df, day) if i.severity == "WARN"}


# legacy bhavcopy ------------------------------------------------------------------------------


def test_legacy_urls():
    c = bare(NseLegacyBhavcopy)
    assert c.url_for(DAY).endswith("/EQUITIES/2016/JAN/cm04JAN2016bhav.csv.zip")
    assert c.url_for(date(2024, 7, 5)).endswith("/2024/JUL/cm05JUL2024bhav.csv.zip")


def test_legacy_parse_and_validate():
    c = bare(NseLegacyBhavcopy, min_rows=3)
    content = legacy_zip(DAY)
    assert header_fingerprint(c.extract_header(content)) in c.known_fingerprints
    df = c.parse(content, DAY)
    assert df.height == len(STOCKS)
    assert df["trade_date"].unique().to_list() == [DAY]  # "04-JAN-2016" parsed
    assert df.schema["volume"] == pl.Int64 and df.schema["value_inr"] == pl.Float64
    assert df.filter(pl.col("symbol") == "ALPHAIND")["close"].item() == 103.2
    assert c.validate(df, DAY) == []
    assert blocks(c, df, date(2016, 1, 5)) == {"date_mismatch"}


@pytest.mark.skipif(not (PRIVATE / "cm04JAN2016bhav.csv.zip").exists(), reason="no private sample")
def test_legacy_real_file():
    c = bare(NseLegacyBhavcopy)
    content = (PRIVATE / "cm04JAN2016bhav.csv.zip").read_bytes()
    assert header_fingerprint(c.extract_header(content)) in c.known_fingerprints
    df = c.parse(content, DAY)
    assert df.height > 1500 and c.validate(df, DAY) == []


# MTO delivery ---------------------------------------------------------------------------------


def test_mto_parse_and_validate():
    c = bare(NseMtoDelivery, min_rows=3)
    content = mto_bytes(DAY)
    assert header_fingerprint(c.extract_header(content)) in c.known_fingerprints
    df = c.parse(content, DAY)
    assert df.columns == [
        "trade_date", "symbol", "series", "qty_traded", "deliverable_qty", "delivery_pct",
    ]  # fmt: skip
    assert df.height == len(STOCKS)
    row = df.filter(pl.col("symbol") == "ALPHAIND").row(0, named=True)
    assert (row["qty_traded"], row["deliverable_qty"], row["delivery_pct"]) == (
        250000,
        125000,
        50.0,
    )
    assert c.validate(df, DAY) == []


def test_mto_date_comes_from_the_file():
    c = bare(NseMtoDelivery, min_rows=3)
    df = c.parse(mto_bytes(DAY, header_day=date(2016, 1, 5)), DAY)
    assert blocks(c, df) == {"date_mismatch"}


def test_mto_malformed_record_raises():
    c = bare(NseMtoDelivery, min_rows=3)
    bad = mto_bytes(DAY).replace(b"20,1,ALPHAIND,EQ,", b"20,1,ALPHAIND,")
    with pytest.raises(ValueError, match="7 fields"):
        c.parse(bad, DAY)


def test_mto_delivery_above_traded_is_a_warning():
    c = bare(NseMtoDelivery, min_rows=3)
    df = c.parse(mto_bytes(DAY), DAY).with_columns(pl.col("qty_traded") // 10)
    assert warns(c, df) == {"delivery_above_traded"} and not blocks(c, df)


@pytest.mark.skipif(not (PRIVATE / "MTO_04012016.DAT").exists(), reason="no private sample")
def test_mto_real_file():
    c = bare(NseMtoDelivery)
    content = (PRIVATE / "MTO_04012016.DAT").read_bytes()
    assert header_fingerprint(c.extract_header(content)) in c.known_fingerprints
    df = c.parse(content, DAY)
    assert df.height > 1500 and not blocks(c, df)


# index closes ---------------------------------------------------------------------------------


def test_index_parse_dashes_become_nulls():
    c = bare(NseIndexClose, min_rows=3)
    content = index_csv(DAY)
    assert header_fingerprint(c.extract_header(content)) in c.known_fingerprints
    df = c.parse(content, DAY)
    vix = df.filter(pl.col("index_name") == "India VIX").row(0, named=True)
    assert vix["close"] == 16.84 and vix["volume"] is None and vix["pe"] is None
    assert c.validate(df, DAY) == []


def test_index_identical_duplicates_are_dropped():
    c = bare(NseIndexClose, min_rows=3)
    lines = index_csv(DAY).decode().splitlines()
    content = "\n".join([*lines, lines[1]]).encode()  # the Nifty 50 row, repeated exactly
    df = c.parse(content, DAY)
    assert df.filter(pl.col("index_name") == "Nifty 50").height == 1
    assert c.validate(df, DAY) == []


def test_index_conflicting_core_duplicate_blocks_other_warns():
    c = bare(NseIndexClose, min_rows=3)
    df = c.parse(index_csv(DAY), DAY)
    core = pl.concat([df, df.filter(pl.col("index_name") == "Nifty 50").with_columns(close=1.0)])
    assert "conflicting_core_duplicates" in blocks(c, core)
    other = pl.concat([df, df.filter(pl.col("index_name") == "Nifty Bank").with_columns(close=1.0)])
    assert not blocks(c, other) and "conflicting_duplicates" in warns(c, other)


def test_index_missing_core_blocks_missing_vix_warns():
    c = bare(NseIndexClose, min_rows=2)
    assert "missing_core_index" in blocks(
        c, c.parse(index_csv(DAY, ("Nifty 50", "India VIX")), DAY)
    )
    df = c.parse(index_csv(DAY, ("Nifty 50", "Nifty 500")), DAY)
    assert not blocks(c, df) and "missing_index" in warns(c, df)


@pytest.mark.skipif(
    not (PRIVATE / "ind_close_all_04012016.csv").exists(), reason="no private sample"
)
def test_index_real_file_has_a_duplicate_row_that_is_dropped():
    c = bare(NseIndexClose)
    df = c.parse((PRIVATE / "ind_close_all_04012016.csv").read_bytes(), DAY)
    assert df.filter(pl.col("index_name") == "Nifty 50").height == 1
    assert not blocks(c, df)


# corporate actions ----------------------------------------------------------------------------

MONTH = date(2016, 1, 1)


def test_corp_actions_month_partition_and_url():
    c = bare(NseCorporateActions)
    assert c.partition_key(date(2016, 1, 17)) == "2016-01"
    assert "from_date=01-02-2024&to_date=29-02-2024" in c.url_for(date(2024, 2, 10))  # leap year


def test_corp_actions_parse():
    c = bare(NseCorporateActions)
    content = corp_actions_json(MONTH)
    assert header_fingerprint(c.extract_header(content)) in c.known_fingerprints
    df = c.parse(content, MONTH)
    assert df.height == 3
    row = df.row(0, named=True)
    assert row["ex_date"] == date(2016, 1, 5) and row["book_closure_start"] is None
    assert row["subject"] == "Bonus 1:2"  # leading space stripped
    assert c.validate(df, MONTH) == []


def test_corp_actions_empty_month_is_a_warning_not_a_failure():
    c = bare(NseCorporateActions)
    assert header_fingerprint(c.extract_header(b"[]")) in c.known_fingerprints
    df = c.parse(b"[]", MONTH)
    assert df.height == 0
    assert warns(c, df, MONTH) == {"no_actions_in_month"} and not blocks(c, df, MONTH)


def test_corp_actions_new_field_is_a_format_change():
    c = bare(NseCorporateActions)
    rows = [{**r, "newField": 1} for r in json.loads(corp_actions_json(MONTH))]
    content = json.dumps(rows).encode()
    assert header_fingerprint(c.extract_header(content)) not in c.known_fingerprints


@pytest.mark.skipif(
    not (PRIVATE / "corporate_actions_2016-01.json").exists(), reason="no private sample"
)
def test_corp_actions_real_file():
    c = bare(NseCorporateActions)
    content = (PRIVATE / "corporate_actions_2016-01.json").read_bytes()
    assert header_fingerprint(c.extract_header(content)) in c.known_fingerprints
    df = c.parse(content, MONTH)
    assert df.height > 10 and not blocks(c, df, MONTH)


def test_legacy_republished_variant_without_trailing_comma_and_short_year():
    c = bare(NseLegacyBhavcopy, min_rows=3)
    day = date(2020, 7, 13)
    with zipfile.ZipFile(io.BytesIO(legacy_zip(day))) as zf:
        text = zf.read(zf.namelist()[0]).decode()
    lines = [ln.rstrip(",") for ln in text.splitlines()]
    lines = [lines[0]] + [ln.replace("13-JUL-2020", "13-Jul-20") for ln in lines[1:]]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cm13JUL2020bhav.csv/cm13JUL2020bhav.csv", "\n".join(lines) + "\n")
    content = buf.getvalue()
    assert header_fingerprint(c.extract_header(content)) in c.known_fingerprints
    df = c.parse(content, day)
    assert df["trade_date"].unique().to_list() == [day]
    assert c.validate(df, day) == []


def test_legacy_garbage_date_is_blocked_not_guessed():
    c = bare(NseLegacyBhavcopy, min_rows=3)
    with zipfile.ZipFile(io.BytesIO(legacy_zip(DAY))) as zf:
        text = zf.read(zf.namelist()[0]).decode().replace("04-JAN-2016", "2016/01/04", 1)
    df = c.parse(zip_csv(text), DAY)
    assert "required_nulls" in blocks(c, df)


def test_mto_missing_first_letter_of_trade_date_line():
    c = bare(NseMtoDelivery, min_rows=3)
    content = mto_bytes(DAY).replace(b"Trade Date <", b"rade Date <", 1)
    assert c.parse(content, DAY)["trade_date"][0] == DAY


def test_index_month_first_date_accepted_only_when_it_is_the_requested_day():
    c = bare(NseIndexClose, min_rows=3)
    day = date(2023, 4, 6)
    content = index_csv(day).replace(b"06-04-2023", b"04-06-2023")
    df = c.parse(content, day)
    assert df["trade_date"].unique().to_list() == [day] and c.validate(df, day) == []
    # the same file fetched for a different day is still a mismatch
    other = date(2023, 4, 7)
    assert "date_mismatch" in blocks(c, c.parse(content, other), other)


def test_udiff_early_2024_header_variant():
    from synthetic import udiff_zip

    from stockapp.ingest.nse_udiff import NseUdiffBhavcopy

    c = bare(NseUdiffBhavcopy, min_rows=3)
    day = date(2024, 3, 1)
    with zipfile.ZipFile(io.BytesIO(udiff_zip(day))) as zf:
        text = zf.read(zf.namelist()[0]).decode()
    lines = text.splitlines()
    lines[0] = lines[0].replace("Rsvd1,Rsvd2,Rsvd3,Rsvd4", "Rsvd01,Rsvd02,Rsvd03,Rsvd04") + ","
    content = zip_csv("\n".join(lines) + "\n")
    assert header_fingerprint(c.extract_header(content)) in c.known_fingerprints
    df = c.parse(content, day)
    assert df.height == 4 and c.validate(df, day) == []
