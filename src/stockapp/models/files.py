"""Finding the newest saved model. File names are ``<prefix>_<feature version>-<YYYYMMDD>.pkl``;
the feature version is a hash, so sorting names would pick an arbitrary version. Prefer models
built on the current feature version, then the latest training date."""

from __future__ import annotations

from pathlib import Path


def newest_model(models_dir: Path, prefix: str, feature_version: str) -> Path | None:
    files = list(models_dir.glob(f"{prefix}_*.pkl"))
    if not files:
        return None

    def key(p: Path) -> tuple[bool, str, float]:
        version, _, day = p.stem.removeprefix(f"{prefix}_").rpartition("-")
        return (version == feature_version, day, p.stat().st_mtime)

    return max(files, key=key)
