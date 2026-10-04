import hashlib
from pathlib import Path

import polars as pl
import pytest

from stockapp.lake import Lake


def test_write_raw_names_file_by_checksum_and_is_idempotent(lake: Lake):
    content = b"hello"
    sha = hashlib.sha256(content).hexdigest()
    a = lake.write_raw("src", "2026-10-01", "f.csv", content)
    b = lake.write_raw("src", "2026-10-01", "f.csv", content)
    assert a == b
    assert a.sha256 == sha and a.size_bytes == 5
    assert a.path.name == f"{sha[:12]}_f.csv"
    assert a.path.read_bytes() == content


def test_republished_file_is_kept_beside_the_original(lake: Lake):
    a = lake.write_raw("src", "2026-10-01", "f.csv", b"v1")
    b = lake.write_raw("src", "2026-10-01", "f.csv", b"v2")
    assert a.path != b.path
    assert a.path.read_bytes() == b"v1"


def test_tampered_raw_file_is_detected(lake: Lake):
    a = lake.write_raw("src", "2026-10-01", "f.csv", b"v1")
    a.path.write_bytes(b"edited")
    with pytest.raises(RuntimeError, match="checksum"):
        lake.write_raw("src", "2026-10-01", "f.csv", b"v1")


def test_write_partition_replaces_never_appends(lake: Lake):
    lake.write_partition("silver", "t", "trade_date", "2026-10-01", pl.DataFrame({"x": [1, 2]}))
    lake.write_partition("silver", "t", "trade_date", "2026-10-01", pl.DataFrame({"x": [3]}))
    lake.write_partition("silver", "t", "trade_date", "2026-10-02", pl.DataFrame({"x": [4]}))
    df = lake.scan("silver", "t").collect().sort("x")
    assert df["x"].to_list() == [3, 4]
    assert set(df["trade_date"].cast(pl.String).to_list()) == {"2026-10-01", "2026-10-02"}
    assert not list(Path(lake.root).rglob("*.tmp"))


def test_unknown_layer_rejected(lake: Lake):
    with pytest.raises(ValueError):
        lake.table_dir("platinum", "t")


def test_remote_lake_not_yet_supported():
    with pytest.raises(NotImplementedError):
        Lake("s3://bucket/lake")
