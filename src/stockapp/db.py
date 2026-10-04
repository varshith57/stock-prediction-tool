"""App database (Postgres): connections and migrations.

Migrations are plain numbered SQL files in ``migrations/``, applied in order, each in its own
transaction, and recorded in ``schema_migrations``. Applied files are never edited; changes go in a
new file.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from stockapp.config import REPO_ROOT, Settings, get_settings

MIGRATIONS_DIR = REPO_ROOT / "migrations"


@contextmanager
def connect(
    settings: Settings | None = None, url: str | None = None, *, autocommit: bool = True
) -> Iterator[psycopg.Connection]:
    """Yield a connection.

    Autocommit by default: pipeline bookkeeping (job runs, manifest, quarantine) must survive a
    later failure in the same run. Use ``conn.transaction()`` where several writes must be atomic.
    """
    url = url or (settings or get_settings()).database_url.get_secret_value()
    with psycopg.connect(url, row_factory=dict_row, autocommit=autocommit) as conn:
        yield conn


def migrate(conn: psycopg.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations. Returns the names applied. Refuses if an applied file changed."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               name text PRIMARY KEY,
               sha256 char(64) NOT NULL,
               applied_at timestamptz NOT NULL DEFAULT now())"""
    )
    conn.commit()
    applied = {
        r["name"]: r["sha256"] for r in conn.execute("SELECT name, sha256 FROM schema_migrations")
    }
    done: list[str] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(sql.encode()).hexdigest()
        if path.name in applied:
            if applied[path.name] != digest:
                raise RuntimeError(
                    f"migration {path.name} was edited after it was applied; add a new file instead"
                )
            continue
        with conn.transaction():
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (name, sha256) VALUES (%s, %s)", (path.name, digest)
            )
        done.append(path.name)
    return done
