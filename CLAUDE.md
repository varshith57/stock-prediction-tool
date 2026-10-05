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
- `src/stockapp/config.py`: secrets from env (`Settings`) and product config. `get_app_config()`
  reads the latest version saved from the app's Settings screen (`settings_store`, Postgres
  `settings_versions`) and falls back to `config/defaults.yaml` + `config/local.yaml`.
- `src/stockapp/cli.py`: `stockapp <command>` entry point
- `app/`: Streamlit pages; `tests/` with `tests/fixtures/`; `migrations/`; `docs/`

## Commands
- `uv sync`: install; `uv run pytest`: tests (live tests skip without env vars)
- `uv run ruff check . && uv run ruff format --check .`: lint
- `docker compose up -d db`: local Postgres; `uv run stockapp migrate`: schema + source registry
- `uv run stockapp ingest nse-udiff --start YYYY-MM-DD [--end ...]`: fetch, archive, validate, load
- `uv run stockapp calendar refresh` / `calendar infer` / `calendar show --start ... --end ...`
- `uv run stockapp backfill [--start 2016-01-01]`: resumable history load (run in background)
- `uv run stockapp universe build`, `uv run stockapp coverage` (writes data/reports/, exits 1 if
  Gate G1 fails), `uv run stockapp probe-earliest <source> --floor ... --known-good ...`
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

## Data sources (M2)
- Prices: `nse_legacy_bhavcopy` (silver `nse_cm_bhavcopy_legacy`) before 2024-07-08,
  `nse_udiff_bhavcopy` (silver `nse_cm_bhavcopy`) from then; read them together only through
  `ingest.prices.combined_prices_sql` (one source per date). Jan to Jul 2024 is in both, for checks.
- Delivery `nse_mto_delivery` -> `nse_cm_delivery`; indices `nse_index_close` (price indices only;
  no TRI yet); corporate actions `nse_corporate_actions` by ex-date month (raw subject text; ratios
  parsed in M3). NSE's JSON API needs a cookie-keeping client and sometimes returns empty 200s.
- Universe: `universe.build_universe` -> silver `universe_top500` (AM3), point-in-time per month.
- Values are rupees (turnover too), not lakhs, except index `turnover_cr` (crores).
- Known NSE format variants are handled and tested (see module docstrings): MTO "rade Date" typo,
  re-published legacy files (no trailing comma, 2-digit year, nested zip path), UDiFF 2024-H1
  header (Rsvd01.., trailing comma), month-first dates in some index files. Add new variants the
  same way: inspect the raw file, add a fingerprint or rule, add a synthetic test.
- Known source gaps: no NSE index file for 2016-06-20 (404 at source). Corporate actions are not
  yet adjusted into prices (M3). Total-return indices not available yet.
- Universe and coverage: `uv run stockapp universe build` then `uv run stockapp coverage`.

## Quality layer (M3)
- `uv run stockapp quality build`: company master -> corporate-action events/factors/breaks ->
  universe -> price flags -> daily score -> M3 gate (exit 1 if it fails). About 35 s.
- Always read prices through `master.company_prices_sql` (company_id, equities only: ISIN INE/IN9,
  series EQ>BE>BZ) or `adjust.adjusted_prices_sql` (adds adj_* and tr_close); never raw symbols.
- NSE's corporate-action feed files history under the company's CURRENT symbol; events map by the
  symbol at the ex-date, else the current holder (`mapped_by`). Unmapped events are reported.
- Delivery: `master.company_delivery_sql`. BE/BZ have no MTO rows by design: 100% by rule
  (`delivery_source = 'rule_trade_for_trade'`).
- Unadjustable events (demerger, amalgamation, restructuring, capital reduction, bonus
  debentures, rights without price) are series breaks: features must not span them.
- BLOCK flags (silver `price_quality_flags`, date ranges) exclude a company from signals for the
  covered dates: unreconciled adjustments, possible missing actions, unparsed price actions, OHLC.

## App and portfolio (M4)
- `uv run streamlit run app/streamlit_app.py`; screens in `app/views/`. Login needs
  `APP_PASSWORD_HASH` (from `uv run stockapp set-password`); the app refuses to start without it.
  During development `APP_AUTH_DISABLED=true` in .env skips the login, honoured only while the
  server listens on 127.0.0.1 (`auth.login_skipped`). Remove it when development is over.
- The app reads small gold tables only (`latest_prices`, `quality_daily`), never full history.
  `quality build` refreshes them.
- Portfolio = `portfolio_transactions` in Postgres -> `portfolio.ledger` (FIFO lots, bonus/split
  quantity events) -> `portfolio.valuation`. Every add/delete replays the ledger first
  (`portfolio.service`), so impossible states are refused. Charges/tax: `portfolio.costs`.
- Holdings are private: never log rows or values, never put them in alerts or the lake.
- Not yet built: editing a transaction in place (delete + re-add).

## UI and settings
- Local only: `.streamlit/config.toml` binds 127.0.0.1, hides the Deploy toolbar and sets the
  theme. Don't deploy anywhere unless the user explicitly asks.
- Shared look in `app/views/ui.py` (header, hero, kpis, pill, section); use it for new screens.
- Product direction (user, 2026-10-05): clear signals, no noise. Home = three buckets Buy | Hold |
  Sell that together hold the whole portfolio; sells most urgent first, holds riskiest first.
  "Act now" = saved plan only (validated 90%+ signals, rule exits). The confidence slider only
  reveals the saved watch queue (`watch_buys`, holds' `p_c`) labelled "watch, don't act". Plain
  English everywhere (no finance jargon); non-essential detail goes in expanders. Portfolio
  changes rebuild the plan (`portfolio._refresh_plan`). Monthly audit is shown as "Track record".
- Settings are edited in the app and versioned (`settings_store`: save/diff/impact/restore).
  On save, `impact()` decides: plan keys rebuild this week's plan (`save_plan` replaces the week's
  plan and archives the old one in `weekly_plan_revisions`, keeping logged actions); gate keys
  re-check LIVE/OFF on stored out-of-sample predictions (`models.run.reevaluate_gate`); label
  keys (thresholds, window, universe size) need `stockapp retrain`, started from the screen in
  the background (`background.py`, state in `data/run/retrain.json`).

## Features (M5)
- `uv run stockapp features build` (about 16 s, 2.5 GB RAM) -> gold `weekly_samples` (last session
  of each week x that month's universe), versioned by a hash of the catalogue in
  `features/build.py`. `uv run stockapp features check` = leakage gate on real data.
- Features must be scale-free functions of adjusted prices (no absolute adjusted levels: those
  leak future splits). Windows group by (company_id, segment) so nothing spans a series break.
  Any new feature needs a catalogue entry and must pass tests/test_m5.py (truncation invariance).
- Labels: A from the T+1 open, C from the T close, 5-session window, null across breaks/halts.

## Models and gate (M6-M7)
- `uv run stockapp models backtest` (~7 min): walk-forward A/C + baselines -> gold
  `oos_predictions`, `signal_gate`; report in data/reports/. `uv run stockapp models train`:
  final models in data/models/ and gold `latest_scores` for the newest week.
- `signals.model`: `lightgbm` (default) or `ensemble` (`models/ensemble.py`: LightGBM, XGBoost,
  CatBoost, bootstrapped extra trees, logistic; each isotonic-calibrated, averaged, calibrated
  again). `uv run stockapp models compare` judges the group and every member on the same
  walk-forward (report only, no gold writes). Switch in Settings, then retrain.
- The gate (`models/gate.py`) uses out-of-sample predictions only, counts only what would be shown
  (A: 5/week by expected gain), and never lowers the bar. Both signals are OFF as of 2026-10-04;
  see docs/MODEL_CARD.md for numbers and known limits (overconfident tails).

## Weekly plan (M8)
- `uv run stockapp plan build [--notify]` -> Postgres `weekly_plans` (+ `plan_actions` from the
  This Week log). `plan/engine.py` is pure; `plan/inputs.py` gathers data; `plan/rules.py` holds
  the deterministic rules (stop 2xATR below cost, optional trailing stop, signal B, stress regime,
  time-weighted drawdown). Rules are labelled as rules, never with a percentage.
- Position size = min(cash left, max(15% of portfolio+budget, minimum position)), whole shares.
- Holdings are a "trade" (from the app's buy ideas: stop-loss, trailing, target, time stop apply)
  or an "investment" (default; Postgres `holding_styles`): investments are never sold by a rule,
  only flagged "Review" past `risk.investment_review_loss` (user's choice, 2026-10-05). A LIVE
  signal C sell applies to any holding.
- Telegram text comes from `cli.plan_summary`: tickers and counts only (checked by
  `check_summary_only`).

## Monthly Audit (M10)
- `audit/report.py`: issued signals come from saved plans (what was shown), outcomes from
  weekly-sample labels, live calibration from the `latest_scores` history. Health counts
  unresolved failures (latest attempt failed) and open quarantine by severity.
- Proposals need min_signals matured signals and are never applied automatically.

## Automation (M9: runs on this Mac via launchd, user's choice)
- `uv run stockapp job daily|weekly|monthly [--date D]`; steps in `jobs.py`, shared logic in
  `pipeline.py`. A job stops at the first failing step, records every step in `job_runs`, and
  alerts once via `alerts.notify` (cap 3/week except exit rules; dedupe keys; amounts refused).
- `uv run stockapp schedule show|install|uninstall`: plists in ~/Library/LaunchAgents
  (com.stockapp.*). Daily Mon-Thu 19:30, weekly Fri 20:00, monthly Sat 10:00 (first Saturday only).
  Never install or change the schedule without the user's explicit OK.
- Logs: data/logs/launchd_<job>.log. Docker (Postgres) must be running for jobs.

## Working rules
- One milestone at a time. Plan first, wait for approval.
- Tests with every feature. Run ruff and pytest before saying done.
- Connectors: test on the live source, save a small real sample in tests/fixtures/private, and test the parser
  on it. If a source can't be reached, say so; don't invent a format.
- Secrets from environment variables only (.env locally, never committed).
- The repo is **public**. Never commit market data, holdings, personal amounts (budget, portfolio
  size, goals: those live in the app database via Settings), or anything under data/. Real source
  samples go in `tests/fixtures/private/` (gitignored); commit only small synthetic fixtures in the
  same format so CI can test parsers without redistributing exchange data.
- Thresholds, caps and costs live in `config/defaults.yaml`, not code.
- Small commits. No unrelated refactors. When unclear, ask.

## Definition of done
Tests and lint pass, the feature is visible in the app or CLI, and a note lists what was verified and
what was not.
