"""The data lake: Parquet files in three layers.

* ``bronze/<source_id>/<partition>/<sha256-prefix>_<filename>``: raw files exactly as fetched. Never
  modified. The checksum is in the name, so a changed re-publication is kept beside the original.
* ``silver/<dataset>/<partition_col>=<value>/data.parquet``: validated, typed tables.
* ``gold/<dataset>/...``: small precomputed tables the app reads.

Local filesystem for now (AM8). Polars and DuckDB read ``s3://`` URIs natively, so moving to
Cloudflare R2 means adding a remote writer here, not changing callers.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from stockapp.config import Settings, get_settings

LAYERS = ("bronze", "silver", "gold")


@dataclass(frozen=True)
class RawFile:
    path: Path
    sha256: str
    size_bytes: int


class Lake:
    def __init__(self, root: str | Path):
        root_str = str(root)
        if "://" in root_str:
            raise NotImplementedError("remote lakes (R2) are wired up in M9; use a local path")
        self.root = Path(root_str).expanduser().resolve()

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Lake:
        return cls((settings or get_settings()).lake_uri)

    # bronze ---------------------------------------------------------------------------------

    def write_raw(self, source_id: str, partition: str, filename: str, content: bytes) -> RawFile:
        sha = hashlib.sha256(content).hexdigest()
        path = self.root / "bronze" / source_id / partition / f"{sha[:12]}_{filename}"
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise RuntimeError(f"raw file on disk does not match its checksum: {path}")
        else:
            _atomic_write(path, content)
        return RawFile(path=path, sha256=sha, size_bytes=len(content))

    # silver / gold --------------------------------------------------------------------------

    def table_dir(self, layer: str, dataset: str) -> Path:
        if layer not in LAYERS:
            raise ValueError(f"unknown layer {layer!r}")
        return self.root / layer / dataset

    def write_partition(
        self, layer: str, dataset: str, partition_col: str, value: str, df: pl.DataFrame
    ) -> Path:
        """Write (or atomically replace) one partition. Rerunning a day never duplicates rows."""
        path = self.table_dir(layer, dataset) / f"{partition_col}={value}" / "data.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        os.close(fd)
        try:
            df.write_parquet(tmp, compression="zstd", statistics=True)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return path

    def scan(self, layer: str, dataset: str) -> pl.LazyFrame:
        glob = str(self.table_dir(layer, dataset) / "**" / "*.parquet")
        return pl.scan_parquet(glob, hive_partitioning=True)

    def duckdb_glob(self, layer: str, dataset: str) -> str:
        """Path pattern for DuckDB: ``read_parquet('<glob>', hive_partitioning = true)``."""
        return str(self.table_dir(layer, dataset) / "**" / "*.parquet")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
