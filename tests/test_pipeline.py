"""End-to-end connector contract against a real Postgres and a temp lake (HTTP is mocked)."""

from datetime import date
from pathlib import Path

import httpx
import polars as pl
import psycopg
import pytest
import respx
from conftest import zip_csv

from stockapp.db import migrate
from stockapp.ingest.http import PoliteClient
from stockapp.ingest.nse_udiff import NseUdiffBhavcopy
from stockapp.ingest.registry import get_watermark
from stockapp.lake import Lake

DAY = date(2026, 10, 1)


class Small(NseUdiffBhavcopy):
    min_rows = 3


@pytest.fixture
def connector(db: psycopg.Connection, lake: Lake) -> Small:
    http = PoliteClient(min_interval_s=0, backoff_s=0, retries=1, sleep=lambda _: None)
    return Small(db, lake, http)


def _one(db: psycopg.Connection, sql: str, *args: object) -> dict:
    row = db.execute(sql, args).fetchone()
    assert row is not None
    return row


@respx.mock
def test_successful_run_loads_and_logs_everything(connector: Small, db, lake: Lake, udiff_csv):
    respx.get(connector.url_for(DAY)).mock(
        return_value=httpx.Response(200, content=zip_csv(udiff_csv))
    )
    r = connector.run(DAY)
    assert (r.status, r.rows) == ("success", 5)

    silver = lake.scan("silver", "nse_cm_bhavcopy").collect()
    assert silver.height == 5
    f = _one(db, "SELECT * FROM source_files")
    assert f["status"] == "loaded"
    assert (lake.root / f["lake_path"]).exists()
    # lineage: every silver row points at its raw file
    assert silver["_source_file_id"].unique().to_list() == [f["source_file_id"]]
    assert silver["_sha256"].unique().to_list() == [f["sha256"]]

    job = _one(db, "SELECT * FROM job_runs")
    assert (job["status"], job["rows_loaded"], job["partition_key"]) == ("success", 5, "2026-10-01")
    assert job["pipeline_version"].startswith("0.1.0+")
    assert get_watermark(db, "nse_udiff_bhavcopy") == "2026-10-01"
    assert (
        _one(db, "SELECT health FROM source_registry WHERE source_id = %s", "nse_udiff_bhavcopy")[
            "health"
        ]
        == "ok"
    )
    assert (
        _one(db, "SELECT count(*) AS n FROM trading_sessions WHERE session_date = %s", DAY)["n"]
        == 1
    )


@respx.mock
def test_rerun_skips_and_force_reloads_without_duplicates(connector: Small, lake: Lake, udiff_csv):
    respx.get(connector.url_for(DAY)).mock(
        return_value=httpx.Response(200, content=zip_csv(udiff_csv))
    )
    assert connector.run(DAY).status == "success"
    assert connector.run(DAY).status == "skipped"
    assert connector.run(DAY, force=True).status == "success"
    assert lake.scan("silver", "nse_cm_bhavcopy").collect().height == 5


@respx.mock
def test_republished_file_replaces_the_day(connector: Small, db, lake: Lake, udiff_csv):
    route = respx.get(connector.url_for(DAY))
    route.mock(return_value=httpx.Response(200, content=zip_csv(udiff_csv)))
    connector.run(DAY)
    fewer = "\n".join(udiff_csv.strip().splitlines()[:4]) + "\n"  # header + 3 rows
    route.mock(return_value=httpx.Response(200, content=zip_csv(fewer)))
    assert connector.run(DAY).status == "success"
    assert lake.scan("silver", "nse_cm_bhavcopy").collect().height == 3
    assert _one(db, "SELECT count(*) AS n FROM source_files")["n"] == 2  # both raw versions kept


@respx.mock
def test_missing_file_is_not_available_not_failure(connector: Small, db):
    respx.get(connector.url_for(DAY)).mock(return_value=httpx.Response(404))
    assert connector.run(DAY).status == "not_available"
    assert _one(db, "SELECT status FROM job_runs")["status"] == "not_available"
    assert (
        _one(db, "SELECT health FROM source_registry WHERE source_id = %s", "nse_udiff_bhavcopy")[
            "health"
        ]
        == "unknown"
    )


@respx.mock
def test_format_change_quarantines_and_flags_source(connector: Small, db, lake: Lake, udiff_csv):
    changed = udiff_csv.replace("TtlTrfVal", "TtlTrfValLakhs", 1)
    respx.get(connector.url_for(DAY)).mock(
        return_value=httpx.Response(200, content=zip_csv(changed))
    )
    r = connector.run(DAY)
    assert (r.status, r.message) == ("failed", "format_changed")
    q = _one(db, "SELECT severity, reason FROM quarantine")
    assert (q["severity"], q["reason"]) == ("BLOCK", "format_changed")
    assert _one(db, "SELECT status FROM source_files")["status"] == "quarantined"
    assert (
        _one(db, "SELECT health FROM source_registry WHERE source_id = %s", "nse_udiff_bhavcopy")[
            "health"
        ]
        == "format_changed"
    )
    assert not (lake.root / "silver").exists()  # nothing reached silver
    assert list((lake.root / "bronze").rglob("*.zip"))  # but the raw file is kept


@respx.mock
def test_html_error_page_instead_of_zip_is_quarantined(connector: Small, db):
    respx.get(connector.url_for(DAY)).mock(
        return_value=httpx.Response(200, content=b"<html>busy</html>")
    )
    assert connector.run(DAY).message == "format_changed"
    assert _one(db, "SELECT reason FROM quarantine")["reason"] == "format_changed"


@respx.mock
def test_block_validation_issue_keeps_data_out_of_silver(
    connector: Small, db, lake: Lake, udiff_csv
):
    wrong_day = udiff_csv.replace("2026-10-01,2026-10-01", "2026-09-30,2026-09-30")
    respx.get(connector.url_for(DAY)).mock(
        return_value=httpx.Response(200, content=zip_csv(wrong_day))
    )
    r = connector.run(DAY)
    assert r.status == "failed" and "date_mismatch" in r.message
    assert _one(db, "SELECT reason FROM quarantine")["reason"] == "date_mismatch"
    assert not (lake.root / "silver").exists()
    assert get_watermark(db, "nse_udiff_bhavcopy") is None


@respx.mock
def test_blocked_source_is_recorded(connector: Small, db):
    respx.get(connector.url_for(DAY)).mock(return_value=httpx.Response(403))
    assert connector.run(DAY).status == "failed"
    reg = _one(
        db,
        "SELECT health, consecutive_failures FROM source_registry WHERE source_id = %s",
        "nse_udiff_bhavcopy",
    )
    assert (reg["health"], reg["consecutive_failures"]) == ("blocked", 1)


def test_migrations_are_idempotent_and_edits_are_refused(db: psycopg.Connection, tmp_path: Path):
    assert migrate(db) == []
    edited = tmp_path / "migrations"
    edited.mkdir()
    (edited / "001_data_platform.sql").write_text("-- edited\n")
    with pytest.raises(RuntimeError, match="edited after it was applied"):
        migrate(db, edited)


def test_silver_lineage_columns_are_typed(connector: Small, lake: Lake, udiff_csv):
    with respx.mock:
        respx.get(connector.url_for(DAY)).mock(
            return_value=httpx.Response(200, content=zip_csv(udiff_csv))
        )
        connector.run(DAY)
    schema = lake.scan("silver", "nse_cm_bhavcopy").collect_schema()
    assert schema["_source_file_id"] == pl.Int64
    assert schema["_ingested_at"].time_zone == "UTC"


@respx.mock
def test_clean_reload_resolves_earlier_blocks(connector: Small, db, udiff_csv):
    route = respx.get(connector.url_for(DAY))
    route.mock(
        return_value=httpx.Response(200, content=zip_csv(udiff_csv.replace("TtlTrfVal", "X", 1)))
    )
    connector.run(DAY)
    route.mock(return_value=httpx.Response(200, content=zip_csv(udiff_csv)))
    assert connector.run(DAY).status == "success"
    q = _one(db, "SELECT resolved_at, resolution FROM quarantine")
    assert q["resolved_at"] is not None and "reloaded successfully" in q["resolution"]
