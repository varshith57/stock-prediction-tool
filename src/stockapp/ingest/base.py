"""Connector contract (PRD section 4), the same for every daily file source:

fetch -> archive raw with SHA-256 -> check the format fingerprint -> parse -> validate -> write the
silver partition -> log (manifest, job run, watermark, source health).

Nothing is silently fixed. A changed format, a parse error or a BLOCK validation issue quarantines
the file with a reason and fails the job; the raw file is kept so the day can be rebuilt later.
Runs are idempotent: the same file for the same day is skipped once loaded, and a re-published file
replaces the day's silver partition (never appends to it).
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import ClassVar, Literal

import polars as pl
import psycopg

from stockapp.ingest import registry as reg
from stockapp.ingest.http import Blocked, CircuitOpen, FetchError, NotAvailable, PoliteClient
from stockapp.lake import Lake

Severity = Literal["BLOCK", "WARN"]
Status = Literal["success", "skipped", "not_available", "failed"]


@dataclass(frozen=True)
class Issue:
    severity: Severity
    reason: str
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RunResult:
    source_id: str
    partition_key: str
    status: Status
    rows: int = 0
    message: str = ""
    issues: tuple[Issue, ...] = ()


class FormatChanged(ValueError):
    """The file's header doesn't match any format this parser knows."""


def header_fingerprint(header_line: str) -> str:
    normalized = ",".join(col.strip() for col in header_line.strip().lstrip("﻿").split(","))
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


class DailyFileConnector(ABC):
    """A source that publishes one file per trading day."""

    source_id: ClassVar[str]
    dataset: ClassVar[str]  # silver dataset written by this connector
    partition_col: ClassVar[str] = "trade_date"
    known_fingerprints: ClassVar[frozenset[str]]

    def __init__(self, conn: psycopg.Connection, lake: Lake, http: PoliteClient):
        self.conn = conn
        self.lake = lake
        self.http = http

    # per-source pieces ----------------------------------------------------------------------

    @abstractmethod
    def url_for(self, day: date) -> str: ...

    @abstractmethod
    def filename_for(self, day: date) -> str: ...

    @abstractmethod
    def extract_header(self, content: bytes) -> str:
        """Return the first line of the data file (unzipping if needed)."""

    @abstractmethod
    def parse(self, content: bytes, day: date) -> pl.DataFrame: ...

    def validate(self, df: pl.DataFrame, day: date) -> list[Issue]:
        """Source-level checks. Section 6 quality gates (M3) run separately on silver data."""
        return []

    def on_loaded(self, day: date, df: pl.DataFrame) -> None:  # noqa: B027 (optional hook)
        """Hook after a successful load (e.g. record the trading session)."""

    # the contract ---------------------------------------------------------------------------

    def run(self, day: date, *, force: bool = False) -> RunResult:
        key = day.isoformat()
        job_id = reg.start_job(self.conn, f"ingest:{self.source_id}", self.source_id, key)
        url = self.url_for(day)

        try:
            content = self.http.get(url).content
        except NotAvailable as exc:
            reg.finish_job(self.conn, job_id, "not_available", message=str(exc))
            return RunResult(self.source_id, key, "not_available", message=str(exc))
        except (Blocked, CircuitOpen, FetchError) as exc:
            health = "blocked" if isinstance(exc, Blocked) else "degraded"
            reg.record_failure(self.conn, self.source_id, health, str(exc))
            reg.finish_job(self.conn, job_id, "failed", message=str(exc))
            return RunResult(self.source_id, key, "failed", message=str(exc))

        raw = self.lake.write_raw(self.source_id, key, self.filename_for(day), content)
        existing = reg.find_source_file(self.conn, self.source_id, key, raw.sha256)
        if existing and existing["status"] == "loaded" and not force:
            reg.finish_job(self.conn, job_id, "skipped", message="same file already loaded")
            return RunResult(self.source_id, key, "skipped", message="same file already loaded")

        header_error: str | None = None
        try:
            fingerprint: str | None = header_fingerprint(self.extract_header(content))
        except Exception as exc:  # unreadable file (e.g. an HTML error page instead of a zip)
            fingerprint, header_error = None, repr(exc)[:500]
        file_id = reg.upsert_source_file(
            self.conn,
            source_id=self.source_id,
            partition_key=key,
            url=url,
            raw=raw,
            lake_root=self.lake.root,
            schema_fingerprint=fingerprint,
            job_run_id=job_id,
        )

        if header_error is not None or fingerprint not in self.known_fingerprints:
            detail = {"fingerprint": fingerprint, "error": header_error}
            return self._fail(job_id, file_id, key, "format_changed", detail, "format_changed")

        try:
            df = self.parse(content, day)
        except Exception as exc:
            return self._fail(job_id, file_id, key, "parse_error", {"error": repr(exc)[:500]})

        issues = self.validate(df, day)
        for issue in issues:
            reg.quarantine(
                self.conn,
                source_id=self.source_id,
                partition_key=key,
                severity=issue.severity,
                reason=issue.reason,
                detail=issue.detail,
                source_file_id=file_id,
            )
        blocking = [i for i in issues if i.severity == "BLOCK"]
        if blocking:
            reg.set_source_file_status(self.conn, file_id, "quarantined")
            message = "; ".join(i.reason for i in blocking)
            reg.finish_job(self.conn, job_id, "failed", message=message)
            return RunResult(self.source_id, key, "failed", message=message, issues=tuple(issues))

        df = df.with_columns(
            pl.lit(file_id, dtype=pl.Int64).alias("_source_file_id"),
            pl.lit(raw.sha256).alias("_sha256"),
            pl.lit(datetime.now(UTC)).alias("_ingested_at"),
            pl.lit(reg.pipeline_version()).alias("_pipeline_version"),
        )
        self.lake.write_partition("silver", self.dataset, self.partition_col, key, df)
        reg.set_source_file_status(self.conn, file_id, "loaded")
        reg.advance_watermark(self.conn, self.source_id, key)
        reg.record_success(self.conn, self.source_id)
        self.on_loaded(day, df)
        reg.finish_job(self.conn, job_id, "success", rows_loaded=df.height)
        return RunResult(self.source_id, key, "success", rows=df.height, issues=tuple(issues))

    def _fail(
        self,
        job_id: int,
        file_id: int,
        key: str,
        reason: str,
        detail: dict[str, object],
        health: str = "degraded",
    ) -> RunResult:
        reg.quarantine(
            self.conn,
            source_id=self.source_id,
            partition_key=key,
            severity="BLOCK",
            reason=reason,
            detail=detail,
            source_file_id=file_id,
        )
        reg.set_source_file_status(self.conn, file_id, "quarantined")
        reg.record_failure(self.conn, self.source_id, health, reason)
        reg.finish_job(self.conn, job_id, "failed", message=reason)
        return RunResult(self.source_id, key, "failed", message=reason)
