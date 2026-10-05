"""M9: jobs stop at the first failure and alert; alerts respect the cap, dedupe and privacy."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest

from stockapp.alerts.notify import notify
from stockapp.alerts.telegram import PrivacyViolation, check_summary_only
from stockapp.config import load_app_config
from stockapp.jobs import run_steps
from stockapp.schedule import TIMES, plist

CFG = load_app_config(local_path=None)
MON = datetime(2026, 10, 5, 9, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))


class Outbox:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def __call__(self, text: str) -> None:
        check_summary_only(text)  # same privacy rule as the real sender
        self.sent.append(text)


def test_cap_dedupe_and_exit_rule_exemption(db: psycopg.Connection):
    box = Outbox()
    for i in range(3):
        assert notify(db, CFG, "weekly_plan", f"plan {i}", now=MON, sender=box) == "sent"
    assert notify(db, CFG, "data_failure", "x", now=MON, sender=box) == "capped"  # 3 a week
    # a hard rule always gets through, but only once per key
    assert (
        notify(
            db, CFG, "exit_rule", "Exit rule hit: X", dedupe_key="exit:X:W41", now=MON, sender=box
        )
        == "sent"
    )
    assert (
        notify(db, CFG, "exit_rule", "again", dedupe_key="exit:X:W41", now=MON, sender=box)
        == "duplicate"
    )
    # a new week resets the cap
    assert notify(db, CFG, "weekly_plan", "next", now=MON + timedelta(days=7), sender=box) == "sent"
    assert len(box.sent) == 5
    assert db.execute("SELECT count(*) AS n FROM alerts_sent").fetchone()["n"] == 5


def test_disabled_sends_nothing(db: psycopg.Connection):
    off = CFG.model_copy(
        update={"alerts": CFG.alerts.model_copy(update={"telegram_enabled": False})}
    )
    box = Outbox()
    assert notify(db, off, "weekly_plan", "x", now=MON, sender=box) == "disabled" and not box.sent


def test_amounts_are_refused(db: psycopg.Connection):
    with pytest.raises(PrivacyViolation):
        notify(db, CFG, "weekly_plan", "Bought for ₹3,900", now=MON, sender=Outbox())


def test_job_stops_at_first_failure_records_and_alerts(db: psycopg.Connection):
    ran: list[str] = []
    alerts: list[tuple] = []

    def ok(name: str):
        return lambda: ran.append(name) or f"{name} fine"

    def boom() -> str:
        raise RuntimeError("source unreachable")

    def fake_alert(conn, cfg, kind, text, dedupe_key=None):
        alerts.append((kind, text, dedupe_key))
        return "sent"

    steps = [("one", ok("one")), ("two", boom), ("three", ok("three"))]
    r = run_steps(db, CFG, "daily", steps, date(2026, 10, 5), alert=fake_alert)
    assert not r.ok and r.failed_step == "two" and ran == ["one"]
    assert alerts == [
        (
            "data_failure",
            "stockapp daily job failed at 'two' (RuntimeError). Data may be stale: "
            "no new actions until it's fixed.",
            "failure:daily:2026-10-05",
        )
    ]
    rows = db.execute("SELECT job, status, message FROM job_runs ORDER BY job_run_id").fetchall()
    assert [(x["job"], x["status"]) for x in rows] == [
        ("job:daily:one", "success"),
        ("job:daily:two", "failed"),
    ]
    assert "source unreachable" in rows[1]["message"]


def test_plists_have_the_agreed_schedule():
    # user's choice 2026-10-05: daily refresh every morning Mon-Sat at 07:30
    assert TIMES["daily"] == [(wd, 7, 30) for wd in (1, 2, 3, 4, 5, 6)]
    assert TIMES["weekly"] == [(5, 20, 0)]
    xml = plist("weekly", uv="/opt/homebrew/bin/uv")
    assert "<string>com.stockapp.weekly</string>" in xml
    assert "<string>/usr/bin/caffeinate</string><string>-i</string>" in xml
    assert "<key>Weekday</key><integer>5</integer><key>Hour</key><integer>20</integer>" in xml
