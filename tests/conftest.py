"""Shared fixtures.

Database tests run against a separate ``stockapp_test`` database on the same server as
``TEST_DATABASE_URL`` (default: local Docker). Locally they skip if Postgres isn't running; in CI
(``REQUIRE_DB=1``) an unreachable database is an error, so they can't be skipped silently.
"""

from __future__ import annotations

import io
import os
import zipfile
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from stockapp.db import migrate
from stockapp.ingest.registry import sync_registry
from stockapp.lake import Lake

FIXTURES = Path(__file__).parent / "fixtures"
ADMIN_URL = os.getenv("TEST_DATABASE_URL", "postgresql://stockapp:stockapp@localhost:5432/stockapp")
TEST_DB = "stockapp_test"


def _test_url() -> str:
    return ADMIN_URL.rsplit("/", 1)[0] + f"/{TEST_DB}"


@pytest.fixture(scope="session")
def _test_database() -> Iterator[str]:
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.getenv("REQUIRE_DB"):
            raise
        pytest.skip(
            f"Postgres not reachable ({exc.__class__.__name__}); run docker compose up -d db"
        )
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    with psycopg.connect(_test_url(), autocommit=True, row_factory=dict_row) as conn:
        migrate(conn)
    yield _test_url()


@pytest.fixture
def db(_test_database: str) -> Iterator[psycopg.Connection]:
    """A migrated, empty database with the source registry seeded."""
    with psycopg.connect(_test_database, autocommit=True, row_factory=dict_row) as conn:
        tables = [
            r["tablename"]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename <> 'schema_migrations'"
            )
        ]
        conn.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
        sync_registry(conn)
        yield conn


@pytest.fixture
def lake(tmp_path: Path) -> Lake:
    return Lake(tmp_path / "lake")


def zip_csv(csv_text: str, name: str = "data.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, csv_text)
    return buf.getvalue()


def write_symbol_changes(
    lake: Lake, changes: list[tuple[str, str, str]] | None = None, snapshot: str = "2026-10-04"
) -> None:
    """Write a symbol-change snapshot: (old_symbol, new_symbol, change_date ISO) tuples."""
    from datetime import date as _date

    import polars as pl

    rows = changes or [("ZZOLD", "ZZNEW", "2000-01-03")]  # irrelevant row: no renames by default
    df = pl.DataFrame(
        {
            "company": [f"{o} Ltd" for o, _, _ in rows],
            "old_symbol": [o for o, _, _ in rows],
            "new_symbol": [n for _, n, _ in rows],
            "change_date": [_date.fromisoformat(d) for _, _, d in rows],
        }
    )
    lake.write_partition("silver", "nse_symbol_changes", "snapshot_date", snapshot, df)


def build_master(lake: Lake, changes: list[tuple[str, str, str]] | None = None):
    from datetime import date as _date

    from stockapp.master import build_company_master

    write_symbol_changes(lake, changes)
    return build_company_master(lake, _date(2026, 10, 4))


@pytest.fixture
def udiff_csv() -> str:
    return (FIXTURES / "udiff_synthetic.csv").read_text()
