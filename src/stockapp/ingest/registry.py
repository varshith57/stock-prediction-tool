"""Source registry, job runs, raw-file manifest and watermarks (all in Postgres)."""

from __future__ import annotations

import json
import os
import subprocess
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
from typing import Any

import psycopg
import yaml

from stockapp.config import REPO_ROOT
from stockapp.lake import RawFile

SOURCES_YAML = Path(__file__).with_name("sources.yaml")
_DESCRIPTIVE = ("name", "provides", "access", "url_template", "terms_note", "fallback")


@lru_cache
def pipeline_version() -> str:
    """Package version plus git commit, e.g. ``0.1.0+4afdb4a`` (``.dirty`` if uncommitted edits)."""
    base = version("stockapp")
    sha = os.getenv("GITHUB_SHA", "")[:7]
    if not sha:
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            dirty = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            sha += ".dirty" if dirty else ""
        except (OSError, subprocess.CalledProcessError):
            sha = "nogit"
    return f"{base}+{sha}"


# registry -------------------------------------------------------------------------------------


def load_sources(path: Path = SOURCES_YAML) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        sources = yaml.safe_load(f)
    for sid, spec in sources.items():
        missing = [k for k in ("name", "provides", "access", "terms_note") if not spec.get(k)]
        if missing:
            raise ValueError(f"source {sid} is missing {missing}")
    return sources


def sync_registry(conn: psycopg.Connection, path: Path = SOURCES_YAML) -> int:
    """Upsert descriptive fields. Health and the earliest date (and its note, once a probe has
    written one) are owned by the database; the YAML note only seeds a newly added source."""
    sources = load_sources(path)
    for sid, spec in sources.items():
        cols = ["source_id", *_DESCRIPTIVE, "earliest_date_note"]
        vals = [sid, *(spec.get(k) for k in _DESCRIPTIVE), spec.get("earliest_date_note")]
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[1:] if c != "earliest_date_note")
        conn.execute(
            f"INSERT INTO source_registry ({', '.join(cols)}) "
            f"VALUES ({', '.join(['%s'] * len(cols))}) "
            f"ON CONFLICT (source_id) DO UPDATE SET {updates}, updated_at = now()",
            vals,
        )
    return len(sources)


def record_success(conn: psycopg.Connection, source_id: str) -> None:
    conn.execute(
        """UPDATE source_registry SET health = 'ok', consecutive_failures = 0,
               last_success_at = now(), updated_at = now() WHERE source_id = %s""",
        (source_id,),
    )


def record_failure(conn: psycopg.Connection, source_id: str, health: str, reason: str) -> None:
    conn.execute(
        """UPDATE source_registry SET health = %s, consecutive_failures = consecutive_failures + 1,
               last_failure_at = now(), last_failure_reason = %s, updated_at = now()
           WHERE source_id = %s""",
        (health, reason[:500], source_id),
    )


# job runs -------------------------------------------------------------------------------------


def start_job(
    conn: psycopg.Connection, job: str, source_id: str | None, partition_key: str | None
) -> int:
    row = conn.execute(
        """INSERT INTO job_runs (job, source_id, partition_key, status, pipeline_version)
           VALUES (%s, %s, %s, 'running', %s) RETURNING job_run_id""",
        (job, source_id, partition_key, pipeline_version()),
    ).fetchone()
    assert row is not None
    return int(row["job_run_id"])


def finish_job(
    conn: psycopg.Connection,
    job_run_id: int,
    status: str,
    *,
    rows_loaded: int | None = None,
    message: str | None = None,
) -> None:
    conn.execute(
        """UPDATE job_runs SET status = %s, finished_at = now(), rows_loaded = %s, message = %s
           WHERE job_run_id = %s""",
        (status, rows_loaded, message, job_run_id),
    )


# manifest, quarantine, watermarks -------------------------------------------------------------


def find_source_file(
    conn: psycopg.Connection, source_id: str, partition_key: str, sha256: str
) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT source_file_id, status FROM source_files
           WHERE source_id = %s AND partition_key = %s AND sha256 = %s""",
        (source_id, partition_key, sha256),
    ).fetchone()


def upsert_source_file(
    conn: psycopg.Connection,
    *,
    source_id: str,
    partition_key: str,
    url: str,
    raw: RawFile,
    lake_root: Path,
    schema_fingerprint: str | None,
    job_run_id: int,
) -> int:
    row = conn.execute(
        """INSERT INTO source_files (source_id, partition_key, url, lake_path, sha256, size_bytes,
                                    fetched_at, schema_fingerprint, status, job_run_id)
           VALUES (%s, %s, %s, %s, %s, %s, now(), %s, 'archived', %s)
           ON CONFLICT (source_id, partition_key, sha256) DO UPDATE
               SET fetched_at = now(), job_run_id = EXCLUDED.job_run_id
           RETURNING source_file_id""",
        (
            source_id,
            partition_key,
            url,
            str(raw.path.relative_to(lake_root)),
            raw.sha256,
            raw.size_bytes,
            schema_fingerprint,
            job_run_id,
        ),
    ).fetchone()
    assert row is not None
    return int(row["source_file_id"])


def set_source_file_status(conn: psycopg.Connection, source_file_id: int, status: str) -> None:
    conn.execute(
        "UPDATE source_files SET status = %s WHERE source_file_id = %s", (status, source_file_id)
    )


def quarantine(
    conn: psycopg.Connection,
    *,
    source_id: str,
    partition_key: str,
    severity: str,
    reason: str,
    detail: dict[str, Any] | None = None,
    source_file_id: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO quarantine
               (source_id, partition_key, source_file_id, severity, reason, detail)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (
            source_id,
            partition_key,
            source_file_id,
            severity,
            reason,
            json.dumps(detail, default=str) if detail else None,
        ),
    )


def advance_watermark(conn: psycopg.Connection, source_id: str, key: str) -> None:
    """Move the watermark forward only (ISO dates sort correctly as text)."""
    conn.execute(
        """INSERT INTO watermarks (source_id, last_good_key) VALUES (%s, %s)
           ON CONFLICT (source_id) DO UPDATE
               SET last_good_key = GREATEST(watermarks.last_good_key, EXCLUDED.last_good_key),
                   updated_at = now()""",
        (source_id, key),
    )


def get_watermark(conn: psycopg.Connection, source_id: str) -> str | None:
    row = conn.execute(
        "SELECT last_good_key FROM watermarks WHERE source_id = %s", (source_id,)
    ).fetchone()
    return None if row is None else str(row["last_good_key"])
