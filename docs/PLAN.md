# Build plan

> **Amendments agreed on 4 Oct 2026 take precedence over the original plan below.**
>
> | # | Original | Amended |
> |---|---|---|
> | AM1 | Gate: >=90% precision, >=30 signals, Wilson 95% lower bound >=80% | Rule unchanged, but note its real strictness: at n=30 it needs 29/30; at exactly 90% precision it needs about 80 signals. Settings shows the exact counts. |
> | AM2 | Label A measured from the signal-date (Friday) close | Label A measured from the **next trading day's open** (the price you can actually get), over the closes of the next 5 trading days. Label C stays relative to the signal-date close. |
> | AM3 | Nifty 500 membership history from index notices | Universe per date = **top 500 by 6-month median traded value** from NSE's daily files (these include later-delisted names, so there's no survivorship bias). Official reconstitution history is a P1 item. |
> | AM4 | Prices from 2020 | Prices from **2016-01-01** (2017 is feature warm-up); walk-forward test from 2018, so 2018-19, 2020 and 2022 are all inside test periods. Delivery data's true start is probed and recorded; NSE's July 2024 format change needs two parsers. |
> | AM5 | "Stress regime" listed as a hard exit | Stress regime **blocks new buys** and flags positions above their cap. It is not a sell-everything rule. |
> | AM6 | Opportunities sorted by expected gain (no model produced it) | Add a LightGBM **quantile model** for max 5-day return in M6, used only for ranking. |
> | AM7 | Daily and weekly jobs on GitHub Actions | Same, but every job is runnable from a local scheduler (launchd), in case NSE blocks GitHub's datacenter IPs. |
> | AM8 | Cloud services from hour 0 | **Local-first**: Postgres in Docker and a local Parquet lake behind a storage abstraction; Neon, R2 and Streamlit Cloud are needed only from M9/M11. |
> | AM9 | Weekly budget | A weekly budget **accumulates**; a buy is suggested once the cash available covers the minimum position (about ₹3,000). |
> | AM10 | Layout `/src/ingest` etc. | Package layout `src/stockapp/<module>` (same modules), Streamlit pages in `/app`. |

---


Oct 4, 2026 · @Shankar

## 1. Reality check and definition of done

In 48 hours you can ship a working, honest weekly-signal app on Nifty 500 data from 2020; you cannot prove 90% accuracy in that time. Accuracy is earned by forward results, so the app ships in paper mode with every signal gated off until it passes validation.

| What 48 hours can deliver | What it cannot |
|----|----|
| Nifty 500 daily data from 2020, quality-gated, automated | A proven 90% hit rate: needs 30+ matured live signals, about 3 to 6 months |
| Your portfolio page with live gain/loss | Pre-2017 financials, news, F&O contract-level data |
| Signals A, B, C trained, calibrated, backtested walk-forward | A guarantee that any signal passes the 90% bar; most weeks will show "No qualifying signal" |
| Weekly plan, Telegram alerts, Monthly Audit with feedback loop | Real-money automation (decision support only) |

**Definition of done (end of hour 48):**

1.  Data lake loaded: Nifty 500 plus past constituents, 2020 to last close, with a coverage report and quality score above 95.

2.  Streamlit app live with four screens: This Week, Portfolio, Monthly Audit, Settings.

3.  Weekly job runs unattended on GitHub Actions and sends a Telegram summary.

4.  Backtest report per signal: precision at the 90% cutoff, count of signals, 95% lower bound; each signal marked LIVE or OFF by rule.

5.  Paper log started so the first forward results begin accumulating immediately.

## 2. Prioritised scope

Build P0 first and do not start P1 until every P0 gate passes. Everything else from PRD v2.0 is deferred.

| Priority | Item | Why |
|----|----|----|
| P0 | Prices with delivery volume, indices, VIX, corporate actions (adjusted series), Nifty 500 membership history | The only data the signals truly need |
| P0 | Quality gates: schema, duplicates, gaps, price jumps against corporate actions, daily quality score; NO SIGNAL on failure | Bad data is the main cause of false signals |
| P0 | Features v1: about 60 price, volume, delivery, volatility, relative-strength and market-regime features, computed point-in-time | Enough for a 5-day model; 300 features is not needed |
| P0 | Signal A (+10% in 5 days), C (-10% in 5 days), calibration, walk-forward precision test, OFF rule | The core product |
| P0 | Portfolio page: manual entry and Kite CSV import, live gain/loss | You use it daily |
| P0 | This Week screen, weekly plan, hard exit rules, Telegram summary | The clean output you asked for |
| P1 | Signal B sell-out nudge (target, trailing stop, time stop) | Rule-based, fast to add |
| P1 | Monthly Audit screen and decision log with outcome tracking | Starts the feedback loop |
| P1 | XBRL fundamentals from 2020 (a handful of ratios) and shareholding | Adds signal but costs hours |
| P1 | Light event flags (results dates, ex-dates, pledges) | Cheap, rule-based |
| P2 | Settings-driven config proposals, SHAP drill-down, FII/DII flows, F&O summary, BSE-only names, backfill before 2020 | After launch |

**Cut entirely for now:** news, language models, hourly refresh, paid feeds, pre-2017 financials, contract-level F&O.

## 3. Lean architecture and sizing

One repo, one language, no servers: files in, Parquet lake, DuckDB, LightGBM, small precomputed tables, Streamlit.

| Layer | Choice | Cost |
|----|----|----|
| Language and tools | Python 3.12, uv, ruff, pytest | Free |
| Lake | Parquet in Cloudflare R2 (or local folder first, synced to R2 on Day 2) | Free under 10 GB |
| Query | DuckDB and Polars | Free |
| Models | LightGBM, scikit-learn isotonic calibration | Free |
| App state | Neon Postgres (holdings, decision log, config) | Free tier |
| Scheduler | GitHub Actions: daily at 19:30 IST, weekly Friday 20:00 IST, monthly retrain | Free, about 300 to 400 of 2,000 minutes |
| App | Streamlit Community Cloud with login, reading small gold tables | Free |
| Alerts | Telegram bot, summaries only | Free |

**Sizing (Nifty 500, 2020 to today):**

| Resource | Need |
|----|----|
| Storage | About 1.5 to 2.5 GB curated, 1.5 GB raw (add 3 to 5 GB only if F&O raw files are kept) |
| RAM | 2 to 4 GB for backfill, 4 to 6 GB for training; an 8 GB laptop works |
| Backfill compute | About 1 to 2 hours for roughly 1,700 daily files, throttled |
| Daily job | 5 to 10 minutes; weekly plan 10 to 20 minutes |
| Monthly retrain | 1 to 3 hours on 4 cores, no GPU |

Run the heavy backfill and first training on your computer; Actions only runs the light daily and weekly jobs.

## 4. Two-day schedule with gates

About 12 working hours a day; each block is one Claude Code session. Do not pass a gate by skipping its test.

| Block | Hours | Milestone | Gate to continue |
|----|----|----|----|
| D1-1 | 0 to 1 | M0 Setup: repo, CLAUDE.md, accounts, CI | CI green, test Telegram message received |
| D1-2 | 1 to 3 | M1 Data platform: lake layout, source registry, connector base class, trading calendar | One source runs end to end on a saved fixture |
| D1-3 | 3 to 6 | M2 Backfill: NSE daily files with delivery, indices, VIX, corporate actions, Nifty 500 membership, from 2020 | Coverage at least 98% of Nifty 500 per day; earliest dates recorded |
| D1-4 | 6 to 8 | M3 Quality gates and adjusted series, quality score | Known splits and bonuses reconcile; score at least 95 |
| D1-5 | 8 to 10 | M4 Portfolio page: input, Kite CSV import, valuation, gain/loss, login | Totals match manual calculation within 1 rupee |
| D1-6 | 10 to 12 | M5 Features v1 (about 60) and labels for A and C, point-in-time | Leakage and shuffled-label tests pass |
| D2-1 | 12 to 15 | M6 Models: baseline rule, LightGBM A and C, isotonic calibration, walk-forward by quarter with 5-day embargo | Backtest report generated for both signals |
| D2-2 | 15 to 17 | M7 Accuracy gate: precision at the 90% cutoff, count, 95% bound, LIVE or OFF per signal | Gate runs; OFF is an accepted outcome |
| D2-3 | 17 to 19 | M8 Weekly plan engine, signal B and hard exit rules, This Week screen | Fixture portfolios give expected actions |
| D2-4 | 19 to 21 | M9 Automation: daily and weekly Actions, Telegram summary, failure alerts | A forced failure sends an alert |
| D2-5 | 21 to 23 | M10 Monthly Audit, decision log, outcome tracking, feedback tags | Audit screen reconciles with the log |
| D2-6 | 23 to 24 | M11 Deploy and dry run: Streamlit Cloud, paper log started, README | One full weekly run unattended end to end |

If a block overruns by more than 1 hour, drop the lowest P1 item from Section 2; never shorten M3 or M7.

## 5. Claude Code build guide

Start each milestone in a fresh Claude Code session, paste its prompt, approve the plan it proposes, then let it build. Save this plan as docs/PLAN.md and PRD v2.0 as docs/PRD.md.

**Before hour 0 (30 minutes):**

1.  Install Python 3.12, uv, git and Claude Code; create a private GitHub repo.

2.  Create a Cloudflare R2 bucket, a Neon database and a Streamlit Community Cloud account (all free).

3.  Telegram: message @BotFather, run /newbot, copy the token; send your bot a message and open the getUpdates URL to read your chat ID.

4.  Put TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, R2 keys and DATABASE_URL in a local .env and in GitHub secrets. Never commit .env.

**CLAUDE.md for the repo root:**

\# Project: Nifty 500 weekly signal app (personal decision support)
Source of truth: docs/PLAN.md and docs/PRD.md. Read only the sections named in the task.

\## Product rules (never violate)
- Decision support only. No code places orders or logs in to a broker.
- No language model inside the app. Predictions come only from trained models.
- Signals A (+10% close within 5 trading days) and C (-10% within 5 days) show only at 90%+ calibrated certainty AND a passing walk-forward gate. Otherwise show "No qualifying signal". Never lower the bar.
- Every number in the UI carries a source and an as-of time.
- If data fails a quality gate, show NO SIGNAL with the reason. Never fill gaps with guesses.
- Point-in-time only: no feature may use data not known at the signal date.
- Holdings data is private: never log it, never put it in alerts.
- At most 3 alerts a week; at most 5 opportunities shown.

\## Stack
Python 3.12, uv, Polars, DuckDB, Parquet (R2), Postgres (Neon) for app state, LightGBM, scikit-learn, Streamlit, pytest, ruff, GitHub Actions.

\## Layout
/src/ingest /src/quality /src/features /src/models /src/plan /src/portfolio /src/audit /src/alerts /app /tests /migrations /docs

\## Working rules
- One milestone per session. Plan first, wait for approval.
- Tests with every feature. Run ruff and pytest before saying done.
- Connectors: test on the live source, save a small real sample in tests/fixtures, test the parser on it. If a source cannot be reached, say so; do not invent a format.
- Secrets from environment variables only.
- Thresholds, caps and costs live in config, not code.
- Small commits. No unrelated refactors. When unclear, ask.

\## Definition of done
Tests and lint pass, feature visible in app or CLI, and a note lists what was verified and what was not.

**Milestone prompts (paste one per session):**

1.  **M0:** "Set up the repo per CLAUDE.md: uv project, ruff, pytest, pre-commit, GitHub Actions CI, a config module, a Telegram send function, and a test that sends me a message. Show the plan first."

2.  **M1:** "Read PRD sections 2 and 4. Build the lake layout (bronze, silver, gold in Parquet, local path or R2 via config), a source registry, a connector base class that saves raw files with SHA-256 before parsing, and an NSE trading calendar. Prove it on one source with a fixture."

3.  **M2:** "Backfill from 2020-01-01: NSE daily equity files with delivery data, index files, India VIX, corporate actions, and Nifty 500 membership as of each date including stocks that later left. Run on my computer, resumable and rate-limited with browser-like headers. Produce a coverage matrix and record each source's earliest date. Do not invent formats; save fixtures."

4.  **M3:** "Read PRD section 6. Build quality gates (schema, duplicates, missing days, price jumps not explained by corporate actions, volume anomalies) with BLOCK and WARN levels, a quarantine table, split and bonus adjusted series computed in DuckDB, and a daily quality score. Below 90 means NO SIGNAL."

5.  **M4:** "Build the Portfolio Streamlit page with login: manual holdings entry and Zerodha Kite holdings CSV import, current value from the latest close, gain/loss, sector weights, and a cost preset for Zerodha charges including the 15.34 rupee DP fee per scrip on sells. Store in Postgres."

6.  **M5:** "Build about 60 point-in-time features (returns over 1, 3, 5, 10, 20 days, volatility, ATR, volume and delivery ratios, distance from 20/50/200-day averages and 52-week high, relative strength vs Nifty 500, breadth, VIX level and change, regime flag). Build labels: A = close at or above +10% versus the signal-date close within the next 5 trading days; C = close at or below -10% within 5 days. Add leakage and shuffled-label tests."

7.  **M6:** "Train LightGBM classifiers for A and C with walk-forward splits by quarter and a 5-trading-day embargo, class weights for rarity, isotonic calibration on a later fold, and baselines (momentum rank, naive rate). Save a model card and a backtest report."

8.  **M7:** "Build the accuracy gate: for each signal find the lowest probability cutoff whose out-of-sample precision is at least 90% with at least 30 signals and a 95% Wilson lower bound of at least 80%. If none exists mark the signal OFF and say why. Store the gate result and show it in Settings. Never lower the bar."

9.  **M8:** "Build the weekly plan engine: Friday evening scoring, Monday action list. This Week screen: up to 5 opportunities sorted by expected gain with a one-line reason and a sell-out nudge (target +10%, trailing stop, 5-day time stop), sell-now alerts for holdings from signal C or hard exit rules, hold list, and the closest candidate with its probability. Reasons are templated from features and SHAP, no free text."

10. **M9:** "Create GitHub Actions: daily data and quality run at 19:30 IST, weekly plan Friday 20:00 IST, monthly retrain. Each stops at the first BLOCK, writes gold tables to R2, and sends a Telegram summary with no holdings values. A failure sends an alert."

11. **M10:** "Build the Monthly Audit: last 4 weeks of signals versus outcomes, precision against the 90% claim, missed crashes and false alarms, and a feedback loop with three levels: tag a decision, propose threshold changes, and retrain-with-approval. Proposals are never auto-applied."

12. **M11:** "Deploy to Streamlit Cloud, write the README with run and recovery steps, start the paper log, and do one full weekly run end to end. List anything unverified."

## 6. Signal logic and the accuracy gate

Each signal is a calibrated probability plus a validation record; the app shows a signal only when both clear the bar.

| Signal | Definition | Output |
|----|----|----|
| A: Opportunity | Close at or above +10% versus signal-date close within the next 5 trading days | Calibrated probability, expected gain, sell-out nudge (target, trailing stop, 5-day time stop) |
| B: Sell-out nudge | Rules on open positions: target hit, trailing stop from the peak, time stop at day 5 | Rule label, not a prediction |
| C: Crash exit | Close at or below -10% versus signal-date close within 5 trading days, for stocks you hold | Calibrated probability and a sell-now line |
| Hard exits | Stop loss at 2x ATR, quality-gate failure, surveillance listing, stress regime | Rule label |

**Pipeline:** point-in-time features, LightGBM per signal, isotonic calibration on a later fold, walk-forward validation by quarter from 2021 on with a 5-day embargo, then the gate.

**Gate rule (per signal):** find the lowest cutoff where out-of-sample precision is at least 90%, with at least 30 signals and a 95% Wilson lower bound of at least 80%. Pass means LIVE; fail means OFF with the reason shown. Weekly, the live precision is re-checked; below 80% over the last 20 signals switches the signal OFF.

**Expect:**

- A 10% move in 5 days is rare for Nifty 500 names, roughly 1 to 3% of stock-weeks, so signal A is hard to push to 90% and will often be OFF.

- The 2020 start gives only one crash regime, so signal C may fail validation until more data or live results accumulate.

- Use the first 3 to 6 months in paper mode; the monthly audit then decides whether any signal earns LIVE.

**Abstain rules:** no signal if the quality score is below 90, the stock fails the liquidity or surveillance filter, the regime is stress and the signal is A, or the calibrated probability is under the cutoff. Defaults for a small portfolio: 15% per stock, 30% per sector, pause new buys at -12% drawdown, 3 alerts a week.

## 7. Screens and weekly rhythm

Four screens, no tickers, no charts you did not ask for.

| Screen | Shows |
|----|----|
| This Week | Sell now (holdings), up to 5 opportunities sorted by expected gain with probability, one-line reason and sell-out nudge, hold list, closest candidate if none qualify, data quality score and as-of time |
| Portfolio | Holdings, value, gain/loss after Zerodha charges, sector weights, cap warnings, add or import |
| Monthly Audit | Last 4 weeks: signals versus outcomes, precision against 90%, missed crashes, false alarms, decision log, feedback tags and threshold proposals |
| Settings | Weekly budget, caps, thresholds, signal status (LIVE or OFF with gate result), alert limits |

| When | What happens |
|----|----|
| Daily 19:30 IST | Ingest, quality gates, update exit watch on holdings; alert only if a hard exit or signal C fires |
| Friday 20:00 IST | Score all names, build the plan, Telegram summary |
| Monday before open | You review This Week and act manually in Kite |
| Monthly | 4-week audit, calibration check, retrain with your approval |

Telegram sends summaries only (action counts and tickers), never holdings values.

## 8. Risks, fallbacks and the path to proven accuracy

| Risk | Fallback |
|----|----|
| NSE blocks or changes a file format during backfill | Use browser-like headers and throttling; fall back to the jugaad-data or nsepython wrappers, then BSE files; record gaps in the coverage matrix |
| Backfill runs past 6 hours | Cut to Nifty 200 first, finish Nifty 500 after launch |
| Corporate-action adjustment errors | Block signals on any stock with an unreconciled price jump |
| A 5-day, 10% label is too rare to train well | Train on the liquid top 300, widen the history to 2015 for prices only, or relax the label to 7% as a research view, never as a shown signal |
| Both signals fail the gate | Ship as designed: the app shows hard exits and the closest candidate, and the paper log keeps collecting evidence |
| Free-tier limits (R2, Actions minutes, Streamlit RAM) | Keep gold tables small; run retraining locally; use a small VPS only if needed |
| Over-trusting the output | Settings shows each signal's validated record; caps and drawdown pause stay on |

**After launch:**

1.  Weeks 1 to 12: paper mode, log every signal, run the first Monthly Audits.

2.  Month 3: first review of live precision against backtest; tune thresholds only through the approved feedback loop.

3.  Month 3 to 6: add XBRL fundamentals, shareholding and event flags (P1), then re-run the gate.

4.  Go live with small capital only after a signal holds at least 90% on 30 or more matured live signals with a 95% lower bound of at least 80%.

This plan is decision support, not investment advice, and I am not a SEBI-registered adviser.
