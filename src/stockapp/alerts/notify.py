"""The one way the app sends Telegram alerts (PRD 8.5).

* Summaries only: ``send_message`` refuses any rupee amount.
* At most ``alerts.max_per_week`` per ISO week, except exit-rule alerts (a hard rule always gets
  through).
* ``dedupe_key`` stops repeats (e.g. one exit alert per stock per week).
* Every alert sent is logged in ``alerts_sent``; nothing is sent when Telegram is switched off.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import psycopg

from stockapp.alerts.telegram import send_message
from stockapp.config import AppConfig

EXEMPT_FROM_CAP = {"exit_rule"}


def _week_start(now: datetime) -> datetime:
    start = now - timedelta(days=now.weekday())
    return start.replace(hour=0, minute=0, second=0, microsecond=0)


def notify(
    conn: psycopg.Connection,
    cfg: AppConfig,
    kind: str,
    text: str,
    *,
    dedupe_key: str | None = None,
    now: datetime | None = None,
    sender: Callable[[str], None] = send_message,
) -> str:
    """Returns 'sent', 'disabled', 'duplicate' or 'capped'."""
    if not cfg.alerts.telegram_enabled:
        return "disabled"
    now = now or datetime.now().astimezone()
    if (
        dedupe_key
        and conn.execute(
            "SELECT 1 FROM alerts_sent WHERE dedupe_key = %s", (dedupe_key,)
        ).fetchone()
    ):
        return "duplicate"
    if kind not in EXEMPT_FROM_CAP:
        sent = conn.execute(
            """SELECT count(*) AS n FROM alerts_sent
               WHERE sent_at >= %s AND kind <> ALL(%s)""",
            (_week_start(now), list(EXEMPT_FROM_CAP)),
        ).fetchone()["n"]
        if sent >= cfg.alerts.max_per_week:
            return "capped"
    sender(text)
    conn.execute(
        "INSERT INTO alerts_sent (kind, dedupe_key, summary, sent_at) VALUES (%s, %s, %s, %s)",
        (kind, dedupe_key, text, now),
    )
    return "sent"
