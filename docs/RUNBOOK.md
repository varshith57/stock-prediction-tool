# Runbook

Everything runs on this Mac. Commands are run from the project folder.

## What runs when (launchd, installed 2026-10-04)

| Job | When (IST) | What | Telegram |
|---|---|---|---|
| daily | Mon-Thu 19:30 | new NSE files, quality build, exit watch on holdings | only if a hard exit rule fires or a step fails |
| weekly | Fri 20:00 | daily steps + features + scores + weekly plan | the plan summary |
| monthly | first Saturday 10:00 | re-validate the 90% gate, retrain | "Monthly audit ready" |

At most 3 messages a week, except exit-rule alerts. Messages never contain amounts.

Needs: the Mac on and logged in, **Docker running**, network. Asleep at the scheduled time? The
job runs once when the Mac wakes. Plugged in with the lid open is safest on Fridays.

## Everyday use

```bash
uv run streamlit run app/streamlit_app.py
```

The four screens are This week (Buy | Hold | Sell), Portfolio, Track record and Settings. Log Done
or Skipped on each action. Slide "Confidence" below 90% to see what's next in line (watch only). Add trades on Portfolio, or import your Kite holdings CSV there.

The app runs only on this Mac (http://localhost:8501). Change budget, certainty bar, caps, costs
and the goal on Settings: saving applies at once (the week's plan is rebuilt and signal status
re-checked). Changing thresholds, the window or universe size needs a retrain, which you can start
from the same screen (about an hour; it keeps the Mac awake). Every save is a version you can
restore.

## When something goes wrong

| Symptom | Check | Fix |
|---|---|---|
| Telegram: "job failed at '<step>'" | `tail -50 data/logs/launchd_<job>.log` | Fix the cause, then rerun: `uv run stockapp job <job>` |
| "couldn't reach its database" | Is Docker running? | Start Docker Desktop, then `docker compose up -d db` and rerun the job |
| `ingest failures` (network or NSE down) | Same log | Rerun later. Days already loaded are skipped; the last 10 days are re-checked automatically |
| NSE file format changed (`format_changed` in quarantine) | `uv run stockapp coverage` section 3 | Inspect the raw file under data/lake/bronze/<source>/<day>/, add the variant to that connector (see CLAUDE.md), then `uv run stockapp ingest <source> --start <day> --force` |
| App says NO SIGNAL (data quality below 90) | Header line and Monthly Audit, Data and model health | Usually a missing day: rerun `uv run stockapp job daily` |
| A stock shows "Stale price" | Was it suspended or delisted? | No new actions on it until it trades again |
| No plan this week | `launchctl list \| grep stockapp`, weekly log | Build it by hand: `uv run stockapp job weekly` |
| Forgot the app password | none | `uv run stockapp set-password`, replace `APP_PASSWORD_HASH` in .env |

## Maintenance

```bash
uv run stockapp schedule show
```

```bash
uv run stockapp schedule uninstall
```

```bash
uv run stockapp schedule install
```

```bash
uv run stockapp coverage
```

```bash
uv run stockapp features check
```

```bash
uv run pytest
```

After pulling code changes, rerun `uv sync`. If the code changed features or models, also run
`uv run stockapp job weekly`.

## Backups

The lake (data/lake) is rebuilt from raw files plus code, and raw files are never modified. Back up
`data/lake/bronze` and the Postgres database (your transactions, plans and logged actions):

```bash
docker compose exec -T db pg_dump -U stockapp stockapp > ~/stockapp_backup_$(date +%F).sql
```

Restore into a fresh database with `psql`. Then `uv run stockapp migrate` and
`uv run stockapp quality build` rebuild everything else.
