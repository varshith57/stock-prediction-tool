"""launchd schedule for the jobs on this Mac (M9). ``show`` prints exactly what ``install``
writes to ~/Library/LaunchAgents; nothing is installed without running ``install``.

Times are the Mac's local time (IST): daily refresh Mon-Sat 07:30 (fetches every trading day it
hasn't loaded yet, then refreshes this week's advice with the latest close), weekly plan Fri
20:00, monthly every Saturday 10:00 (the job itself only proceeds on the first Saturday). If the
Mac is asleep or the lid is closed at those times, launchd runs the missed job once when it wakes
(several missed runs collapse into one). It can't run while the Mac is shut down. ``caffeinate
-i`` keeps it awake while a job runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

from stockapp.config import REPO_ROOT

LABEL = "com.stockapp.{job}"
AGENTS = Path.home() / "Library" / "LaunchAgents"
TIMES = {
    "daily": [(wd, 7, 30) for wd in (1, 2, 3, 4, 5, 6)],  # launchd: 1 = Monday
    "weekly": [(5, 20, 0)],
    "monthly": [(6, 10, 0)],
}


def _interval(weekday: int, hour: int, minute: int) -> str:
    return (
        f"<dict><key>Weekday</key><integer>{weekday}</integer><key>Hour</key><integer>{hour}</integer>"
        f"<key>Minute</key><integer>{minute}</integer></dict>"
    )


def plist(job: str, uv: str | None = None) -> str:
    uv = uv or shutil.which("uv") or "/opt/homebrew/bin/uv"
    repo = escape(str(REPO_ROOT))
    logs = escape(str(REPO_ROOT / "data" / "logs"))
    args = [
        "/usr/bin/caffeinate",
        "-i",
        uv,
        "run",
        "--directory",
        str(REPO_ROOT),
        "stockapp",
        "job",
        job,
    ]
    arg_xml = "".join(f"<string>{escape(a)}</string>" for a in args)
    intervals = "".join(_interval(*t) for t in TIMES[job])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL.format(job=job)}</string>
  <key>ProgramArguments</key><array>{arg_xml}</array>
  <key>WorkingDirectory</key><string>{repo}</string>
  <key>StartCalendarInterval</key><array>{intervals}</array>
  <key>StandardOutPath</key><string>{logs}/launchd_{job}.log</string>
  <key>StandardErrorPath</key><string>{logs}/launchd_{job}.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
"""


def install() -> list[str]:
    AGENTS.mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / "data" / "logs").mkdir(parents=True, exist_ok=True)
    done, domain = [], f"gui/{os.getuid()}"
    for job in TIMES:
        path = AGENTS / f"{LABEL.format(job=job)}.plist"
        subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
        path.write_text(plist(job), encoding="utf-8")
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
        done.append(str(path))
    return done


def uninstall() -> list[str]:
    removed, domain = [], f"gui/{os.getuid()}"
    for job in TIMES:
        path = AGENTS / f"{LABEL.format(job=job)}.plist"
        if path.exists():
            subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
            path.unlink()
            removed.append(str(path))
    return removed
