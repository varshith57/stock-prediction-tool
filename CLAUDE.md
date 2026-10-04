# Project: Nifty 500 weekly signal app (personal decision support)

Source of truth: docs/PLAN.md (the amendments table at its top overrides the rest) and docs/PRD.md.
Read only the sections named in the task.

## Product rules (never violate)
- Decision support only. No code places orders or logs in to a broker.
- No language model inside the app. Predictions come only from trained models.
- Signal A (+10% close within 5 trading days, measured from the next open) and signal C (-10% close
  within 5 trading days of the signal-date close) show only at 90%+ calibrated certainty AND a passing
  walk-forward gate. Otherwise show "No qualifying signal". Never lower the bar.
- Signal B and hard exits are rules, labelled as rules, never shown as a percentage.
- Every number in the UI carries a source and an as-of time.
- If data fails a quality gate, show NO SIGNAL with the reason. Never fill gaps with guesses.
- Point-in-time only: no feature may use data not known at the signal date.
- Holdings data is private: never log it, never put it in alerts (alerts are summaries only).
- At most 3 alerts a week; at most 5 opportunities shown.

## Stack
Python 3.12, uv, Polars, DuckDB, Parquet (local lake first, R2 later), Postgres (Docker locally,
Neon later) for app state, LightGBM, scikit-learn, Streamlit, pytest, ruff, GitHub Actions.

## Layout
- `src/stockapp/{ingest,quality,features,models,plan,portfolio,audit,alerts}`: library code
- `src/stockapp/config.py`: secrets from env (`Settings`) and product config from `config/defaults.yaml`
- `src/stockapp/cli.py`: `stockapp <command>` entry point
- `app/`: Streamlit pages; `tests/` with `tests/fixtures/`; `migrations/`; `docs/`

## Commands
- `uv sync`: install; `uv run pytest`: tests (live tests skip without env vars)
- `uv run ruff check . && uv run ruff format --check .`: lint
- `docker compose up -d db`: local Postgres; `uv run stockapp migrate`: schema + source registry
- `uv run stockapp ingest nse-udiff --start YYYY-MM-DD [--end ...]`: fetch, archive, validate, load
- `uv run stockapp calendar refresh` / `calendar show --start ... --end ...`
- `uv run stockapp --help`

## Data platform (M1)
- Lake (`LAKE_URI`, default `data/lake`): `bronze/<source>/<day>/<sha12>_<file>` raw and immutable;
  `silver/<dataset>/trade_date=<day>/data.parquet` validated, one file per day, replaced on rerun;
  `gold/` app tables. Silver rows carry `_source_file_id`, `_sha256`, `_ingested_at`,
  `_pipeline_version` for lineage.
- New daily source = subclass `ingest.base.DailyFileConnector` (url, header, parse, validate), add it
  to `ingest/sources.yaml`, record its known header fingerprint. The base class does archiving,
  manifest, quarantine, watermark, health and job runs.
- Migrations: add a new numbered file in `migrations/`; never edit an applied one (the runner refuses).

## Working rules
- One milestone at a time. Plan first, wait for approval.
- Tests with every feature. Run ruff and pytest before saying done.
- Connectors: test on the live source, save a small real sample in tests/fixtures/private, and test the parser
  on it. If a source can't be reached, say so; don't invent a format.
- Secrets from environment variables only (.env locally, never committed).
- The repo is **public**. Never commit market data, holdings, personal amounts (budget, portfolio
  size, goals: those go in the gitignored `config/local.yaml`), or anything under data/. Real source
  samples go in `tests/fixtures/private/` (gitignored); commit only small synthetic fixtures in the
  same format so CI can test parsers without redistributing exchange data.
- Thresholds, caps and costs live in `config/defaults.yaml`, not code.
- Small commits. No unrelated refactors. When unclear, ask.

## Definition of done
Tests and lint pass, the feature is visible in the app or CLI, and a note lists what was verified and
what was not.
