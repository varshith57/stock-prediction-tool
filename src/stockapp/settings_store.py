"""Settings edited in the app (Postgres ``settings_versions``).

The first read seeds version 1 from the file defaults (and the old config/local.yaml), so after
that the app's Settings page is the only place values come from. Every save validates the whole
configuration, stores it as a new version with the list of changed fields, and returns what the
change affects:

* ``plan``: budget, caps, stops, regime, alerts -> rebuild this week's plan now;
* ``gate``: certainty bar and gate rules -> re-check LIVE/OFF from stored out-of-sample
  predictions now (no retraining);
* ``retrain``: thresholds, window, universe size -> labels and models change: features rebuild,
  backtest and retrain (minutes, run in the background).
"""

from __future__ import annotations

import json
from typing import Any

import psycopg

from stockapp.config import AppConfig, file_config

RETRAIN_KEYS = (
    "signals.gain_threshold",
    "signals.crash_threshold",
    "signals.window_trading_days",
    "universe.size",
)
GATE_KEYS = ("signals.certainty_bar", "signals.gate.", "signals.max_opportunities")


def _flatten(d: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def diff(old: AppConfig, new: AppConfig) -> dict[str, list]:
    a, b = _flatten(old.model_dump()), _flatten(new.model_dump())
    return {k: [a.get(k), b.get(k)] for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)}


def impact(changed: dict[str, list]) -> set[str]:
    out: set[str] = set()
    for key in changed:
        if key.startswith(RETRAIN_KEYS):
            out.add("retrain")
        elif key.startswith(GATE_KEYS):
            out.add("gate")
        out.add("plan")
    return out


def _connect():
    from stockapp.db import connect

    return connect()


def latest(conn: psycopg.Connection) -> dict | None:
    return conn.execute(
        "SELECT version_id, values, created_at FROM settings_versions "
        "ORDER BY version_id DESC LIMIT 1"
    ).fetchone()


def ensure_seeded(conn: psycopg.Connection) -> int:
    row = latest(conn)
    if row:
        return int(row["version_id"])
    cfg = file_config()
    r = conn.execute(
        """INSERT INTO settings_versions (values, note) VALUES (%s, %s) RETURNING version_id""",
        (json.dumps(cfg.model_dump()), "seeded from the configuration files"),
    ).fetchone()
    return int(r["version_id"])


def latest_config(conn: psycopg.Connection | None = None) -> AppConfig | None:
    if conn is None:
        with _connect() as c:
            return latest_config(c)
    row = latest(conn)
    if row is None:
        ensure_seeded(conn)
        row = latest(conn)
    return AppConfig.model_validate(row["values"]) if row else None


def save(
    conn: psycopg.Connection, new: AppConfig, note: str | None = None
) -> tuple[int | None, dict[str, list]]:
    """Store ``new`` as a version if anything changed. Returns (version_id, changes)."""
    current = latest_config(conn) or file_config()
    changes = diff(current, new)
    if not changes:
        return None, {}
    r = conn.execute(
        "INSERT INTO settings_versions (values, changed, note) VALUES (%s, %s, %s) "
        "RETURNING version_id",
        (json.dumps(new.model_dump()), json.dumps(changes, default=str), note),
    ).fetchone()
    return int(r["version_id"]), changes


def history(conn: psycopg.Connection, limit: int = 20) -> list[dict]:
    return conn.execute(
        "SELECT version_id, changed, note, created_at FROM settings_versions "
        "ORDER BY version_id DESC LIMIT %s",
        (limit,),
    ).fetchall()


def restore(conn: psycopg.Connection, version_id: int) -> tuple[int | None, dict[str, list]]:
    row = conn.execute(
        "SELECT values FROM settings_versions WHERE version_id = %s", (version_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no settings version {version_id}")
    return save(conn, AppConfig.model_validate(row["values"]), f"restored version {version_id}")
