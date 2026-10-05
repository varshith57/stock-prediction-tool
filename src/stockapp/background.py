"""Run a long task (the retrain) in the background from the app and report its progress.

One task at a time. The process writes to data/logs/retrain_<time>.log and ends with a
``RETRAIN DONE`` or ``RETRAIN FAILED`` line; a small state file records the pid and log path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from stockapp.config import REPO_ROOT

STATE = REPO_ROOT / "data" / "run" / "retrain.json"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def status() -> dict | None:
    if not STATE.exists():
        return None
    state = json.loads(STATE.read_text())
    log = Path(state["log"])
    text = log.read_text(errors="replace") if log.exists() else ""
    lines = [ln for ln in text.splitlines() if ln.strip() and "Warning" not in ln]
    done = any(ln.startswith("RETRAIN DONE") for ln in lines)
    failed = any(ln.startswith("RETRAIN FAILED") for ln in lines)
    running = _alive(state["pid"]) and not (done or failed)
    if not running and not (done or failed):
        failed = True  # the process ended without its final line
    return {**state, "running": running, "done": done, "failed": failed, "tail": lines[-6:]}


def start_retrain() -> dict:
    current = status()
    if current and current["running"]:
        return current
    STATE.parent.mkdir(parents=True, exist_ok=True)
    logs = REPO_ROOT / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"retrain_{datetime.now():%Y%m%d_%H%M%S}.log"
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    with log.open("w") as out:
        proc = subprocess.Popen(
            [
                "/usr/bin/caffeinate",
                "-i",
                uv,
                "run",
                "--directory",
                str(REPO_ROOT),
                "stockapp",
                "retrain",
            ],
            stdout=out,
            stderr=subprocess.STDOUT,
            cwd=REPO_ROOT,
            start_new_session=True,
        )
    state = {"pid": proc.pid, "log": str(log), "started_at": datetime.now().isoformat()}
    STATE.write_text(json.dumps(state))
    return {**state, "running": True, "done": False, "failed": False, "tail": []}
