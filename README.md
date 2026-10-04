# stockapp

Personal weekly decision-support app for Nifty 500 stocks: a quality-gated data lake, calibrated
signal models held to a 90% bar (with "no signal" when it isn't met), a portfolio page, and an
honest monthly audit.

**This is a personal research tool, not investment advice.** It places no orders and never logs in
to a broker.

- What and why: [docs/PRD.md](docs/PRD.md); build order and agreed amendments: [docs/PLAN.md](docs/PLAN.md)
- Accounts and secrets: [docs/SETUP.md](docs/SETUP.md)

## Quick start

```bash
uv sync                      # Python 3.12 + dependencies
cp .env.example .env         # then fill in values (docs/SETUP.md)
docker compose up -d db      # local Postgres
uv run stockapp migrate      # create tables, sync the source registry
uv run stockapp calendar refresh
uv run stockapp ingest nse-udiff --start 2026-09-29 --end 2026-10-01
uv run stockapp quality build    # company master, adjustments, universe, quality, app tables
uv run stockapp set-password     # then put the printed APP_PASSWORD_HASH line in .env
uv run stockapp features build && uv run stockapp models backtest && uv run stockapp models train
uv run stockapp plan build       # this week's plan (add --notify for the Telegram summary)
uv run streamlit run app/streamlit_app.py
uv run pytest                # tests (live ones skip without secrets)
uv run stockapp config       # show product config
uv run stockapp telegram-test
```

## Status

| Milestone | State |
|---|---|
| M0 Setup | Done |
| M1 Data platform | Done: lake, app DB schema, source registry, connector framework, NSE calendar, NSE daily prices (current format) |
| M2 History backfill | Done (Gate G1 PASS, 2026-10-04): 2016-01 to 2026-10, 2,521 universe sessions, none below 98% priced, delivery 98-99.9%, index closes every session but one |
| M3 Quality gates | Done (gate PASS, 2026-10-04): company master (3,628 companies, renames tracked), corporate-action adjustment with 1,015/1,018 events reconciling and 16/16 known large-cap events, quality score >= 95 on 98.6% of sessions |
| M4 Portfolio | Done: Portfolio screen (holdings at the latest NSE close, gain/loss after Zerodha charges, tax estimate, sector caps), FIFO ledger with automatic bonus/split adjustment, Kite CSV import with preview, password login; totals match a hand calculation within ₹1 |
| M5 Features | Done (gate PASS): 53 point-in-time features in 10 families, labels A/C per AM2, 265k weekly samples 2016-2026; truncation, random-walk and shuffled-label (AUC 0.50) leakage tests pass |
| M6-M7 Models and gate | Done: walk-forward LightGBM A/C (35 quarterly folds, purged, calibrated) + expected-gain model; beats volatility and momentum baselines (A top-5 precision 22.6% vs 15.3%); **both signals OFF** at the 90% bar (best: A 43%, C 65%). See [docs/MODEL_CARD.md](docs/MODEL_CARD.md) |
| M8 Weekly plan | Done: plan engine (NO SIGNAL on low data quality; stop-loss/trailing/signal-B rules; C exits and A opportunities only when LIVE, capped and budget-sized; stress-regime and drawdown pauses), This Week screen with one-tap logging, Telegram summary (no amounts). Current plan: no actions, closest candidate shown |
| M10 Monthly Audit | Done: verdict, scorecard vs the 90% claim (issued = what the plans showed), missed events, your logged actions with reasons, money vs Nifty 50 with the same cash flows, live calibration, data/model health, evidence-gated proposals; lock and export. Reconciles with the log (gate) |
| M9 Automation | Built: daily/weekly/monthly jobs on this Mac via launchd (stop at first failure, Telegram failure alerts, 3/week cap with hard-rule exemption); `uv run stockapp schedule show` / `install` |
