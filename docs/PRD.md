# **PRD v2.0: India Stock Analysis and Decision-Support Platform**
Oct 3, 2026 · @Shankar
## **1. Product overview**
**Vision:** a personal, self-updating research and decision-support app for Indian equities. It builds the deepest free dataset it can (prices, company financials, ownership, flows, macro, and event flags), scores stocks daily, tells you what to buy, sell or hold in your own portfolio with plain reasons, and measures its own accuracy so you can see whether to trust it.
**User:** one person (you), using it daily, investing a small fixed amount each week through Zerodha Kite, in Nifty 200 stocks. Personal amounts (budget, portfolio size, goals) live only in local settings, not in this repo.
**Product principles**
  - Data first: model quality is capped by data quantity and quality. Data management is the most important module and gets the most build effort.
  - Decision support only: no order placement, no broker login.
  - Transparent: every signal shows its reasons, data, and track record. No black box.
  - Honest: the app abstains when data or confidence is weak, and shows real accuracy, not backtest hope.
  - Cheap: run cost under about ₹800 a month on free tiers.
**Goals**
  - G1: complete, point-in-time-correct historical dataset for the full NSE/BSE universe as far back as free sources allow, refreshed automatically.
  - G2: a weekly, low-noise signal set: A (gain of 10% or more within 5 trading days), B (sell-out nudge), C (crash exit), each shown only at 90% or higher calibrated certainty, otherwise "no signal".
  - G3: daily scoring of Nifty 200 stocks with calibrated probabilities and plain-language reasons.
  - G4: a portfolio page, buy/sell/hold decisions, and a Config page tuned to your budget and risk.
  - G5: a performance review dashboard that tracks every call against its outcome and feeds recalibration.
  - G6: guardrails and evals that keep the system inside safe limits.
**Non-goals (v1):** order execution, F\&O or short ideas, intraday signals, mobile app, multiple users, giving advice to others (that needs SEBI registration).
**Success metrics**
|  |  |  |
| :- | :- | :- |
| Area | Metric | Target |
| Data | Daily data-quality score; universe coverage; freshness | At least 95; at least 98% of Nifty 200 priced daily; EOD data ready by 8 pm IST |
| Data | Historical backfill complete | Every source loaded back to its earliest reliable date, with gaps listed |
| Model | Precision of signals A and C against the 90% claim, walk-forward | At least 90% hit rate on 30+ past signals (95% lower bound at least 80%), else the signal is switched off |
| Model | Calibration error | Within 5 points per probability bucket |
| Product | Net return vs Nifty 50 total return over 12+ months of live or paper results | Beats it, with drawdown no worse |
| Reliability | Daily job success on trading days | At least 99% |

**Honest expectations**
  - Markets are close to efficient. A realistic edge is small. Plan for a modest, intermittent signal and for the baseline winning if the ML model does not beat it after costs.
  - A goal of +50% in a month is not realistic. It compounds to about 130× a year, against roughly 12–14% a year for the Nifty historically (approximate, from my knowledge). The app shows the probability of reaching any target and never takes more risk to close the gap. The realistic goal is beating a Nifty 50 index fund over 12+ months after charges.
  - With a small monthly budget, your own trades are too few to judge the model. It is evaluated on all of its signals, not only the ones you trade.
## **2. Architecture and technology stack**
Data moves one way: sources, ingestion, quality gates, a validated store, features, models, decisions, then the app. Raw files are always kept, so any day can be rebuilt.
\[image\]
pipeline · 8 stages, quality gate before storage
**Storage decision (changes my earlier "one Postgres" suggestion):** full-history data for the whole NSE/BSE universe is too big for a free Postgres tier (the full NSE and BSE universe is roughly 30–40 million price rows (see section 12), plus financials and event flags). So the data lake lives as Parquet files in free object storage and is queried with DuckDB, while a small Postgres holds app state only.
|  |  |  |
| :- | :- | :- |
| Layer | Choice | Why / cost |
| Language and tooling | Python 3.12, uv, ruff, pytest, pre-commit | Standard, Claude Code handles it well |
| Data lake | Parquet files in Cloudflare R2 (10 GB free) or Backblaze B2 (small free tier) | $0; partitioned by dataset and date |
| Analytics engine | DuckDB reading Parquet; Polars for transforms | $0; fast on a laptop or in a scheduled job |
| App database | Postgres on Neon free tier (holdings, lots, signals, decisions, outcomes, config, feedback, job runs, quality results) | $0; small, transactional, backed up |
| Ingestion | httpx, requests with retry, feedparser (RSS), BeautifulSoup/lxml for HTML, openpyxl for Excel, lxml for XBRL | $0 |
| Data quality | pandera schemas plus custom SQL checks; results stored in Postgres | $0 |
| Modeling | scikit-learn, LightGBM, statsmodels, arch, hmmlearn, MAPIE, SHAP | $0 |
| Orchestration | GitHub Actions scheduled workflows (private repo) | $0 within free minutes; a small VPS only if limits are hit |
| App | Streamlit Community Cloud with a login | $0 |
| Alerts | Telegram bot | $0 |
| Tracking | Git for code, dated Parquet snapshots for data, MLflow (local) for model runs | $0 |

**Environments:** local (development and heavy backfills), CI (tests on fixtures), production (scheduled jobs and the app). Heavy one-time backfills run on your computer, then write to the lake.
## **3. Data management: what we collect and how far back**
Data management is the foundation module. Build it first, test it hardest, and do not start modeling until its gates pass.
**Data principles**
1.  **Maximum history, honestly dated.** Load each dataset back to its earliest reliable date. Record that date and any gaps in the source registry (section 4) instead of assuming.
2.  **Full universe, not just Nifty 200.** Collect every NSE and BSE listed stock, including delisted, merged and suspended ones, so history has no survivorship bias. Scoring and the app focus on Nifty 200.
3.  **Point-in-time.** Every record carries when the event happened and when it became public. Models only see what was known at the time.
4.  **Raw is sacred.** Original files are stored untouched with a checksum; all cleaning is reproducible from raw.
5.  **Nothing silent.** Failed checks are quarantined and reported, never auto-fixed or ignored.
6.  **Lineage.** Every value in the app can be traced to a source file, fetch time, and pipeline version.
**Data domains and target history**
Expected depths below are targets from what I know of these sources, not verified facts. The first build task for each domain is to probe the source for its true earliest date and record it.
|  |  |  |  |
| :- | :- | :- | :- |
| Domain | Contents | Target history | Known limits |
| Daily prices and volume | Open, high, low, close, last, volume, value, trades, delivery quantity and %, for NSE and BSE | As far back as NSE and BSE archives go (late 1990s or earlier) | Archive file formats changed over time (NSE changed its daily file format in 2024); parsers must handle every era |
| Indices | Nifty 50, Nifty 200, Next 50, sector and broad indices: price and total return; India VIX; constituent history | From each index's start (1990s for the main ones); VIX from 2008 | Constituent history needs reconstitution notices |
| Corporate actions | Splits, bonuses, dividends, rights, mergers, demergers, buybacks, symbol and name changes, delistings | Full history in exchange announcements | Needed to adjust prices and holdings correctly |
| Company master | Identity, listing, classification, status (section below) | Full | Symbol changes need a mapping table keyed by ISIN |
| Financial statements | Quarterly and annual P\&L, balance sheet, cash flow; standalone and consolidated | Machine-readable XBRL from about April 2017 on BSE and a similar time on NSE, so roughly 36 quarters | Before 2017 results exist mainly as PDFs or HTML; backfill earlier years only if parsing proves reliable or a paid/export source is justified later |
| Ownership | Quarterly shareholding: promoter, FII, DII, MF, public; pledged shares; top holders | From the start of structured filings (confirm) | Quarterly only |
| Investor flows | Daily FII/DII net cash flows; FPI flows by sector; bulk and block deals; insider and SAST trades; MF holdings and NAV | As deep as each source offers (confirm per source) | Different start dates per source |
| Derivatives | Futures and options: open interest, volume, PCR, rollover, option-implied levels | From the start of F\&O data (index futures began in 2000) | Option chains are end-of-day only |
| Macro | Repo rate, liquidity, CPI, WPI, IIP, GDP, GST collections, forex reserves, USD/INR, G-sec yields, crude, gold, US yields and Fed rate, DXY, global indices | Decades (RBI, MOSPI, FRED) | Release lags must be modeled (a number is known only after its release date) |
| Event calendar and flags | Dates and structured flags only: results and board-meeting dates, rating changes, pledge changes, auditor changes, surveillance listings; RBI MPC, FOMC, budget and data-release calendar | Full where published online | No text analysis; flags only (section 5) |
| Surveillance and risk lists | ASM, GSM, circuit-band changes, trading halts | From when published (confirm) | Used as exclusion filters |

**Company master and financial parameters (collected per company)**
|  |  |
| :- | :- |
| Group | Fields |
| Identity | ISIN, NSE and BSE symbols with full symbol history, name history, CIN, face value, listing date, delisting date and reason, exchange status |
| Classification | Sector, industry and basic industry (NSE classification), index memberships over time, market-cap bucket over time, group and promoter entity |
| Income statement | Revenue, other income, expenses by head, EBITDA, depreciation, interest, PBT, tax, PAT, EPS, exceptional items |
| Balance sheet | Equity, reserves, borrowings (short and long), total assets, fixed assets, inventories, receivables, payables, cash and investments, contingent liabilities |
| Cash flow | Operating, investing, financing, capex, free cash flow, dividends paid |
| Derived ratios | Growth (YoY, QoQ, 3 and 5-year CAGR), margins, ROE, ROCE, debt/equity, interest cover, working-capital days, cash conversion, operating cash flow vs PAT, valuation (P/E, P/B, EV/EBITDA, dividend yield) against own history and sector |
| Segments and quality | Segment revenue and profit where filed, auditor and changes, audit qualifications, related-party transactions, credit ratings and rating changes, pledged shares, fund-raising and deviation reports |
| Disclosures | Board meeting dates, results dates, dividend and buyback announcements, concall dates and transcripts or summaries where available, management guidance extracted as structured fields |

Every financial value is stored with: reporting period, period type (quarter, half, year), basis (standalone or consolidated), filing date, filing version (original or restated), and source file.
## **4. Data management: sources, ingestion, backfill and refresh**
**Source registry.** A table in the database lists every source with its terms, access method, earliest available date, fallback and health status. Connectors are built from it, and it is updated by the first probe of each source. Free sources change formats and block bots without notice, so each domain has at least one fallback. Check each site's terms and robots rules before automating; use official files and APIs where they exist.
|  |  |  |  |
| :- | :- | :- | :- |
| Source | Provides | Access | Fallback or note |
| NSE archives (daily equity file with delivery data, F\&O file, indices, surveillance lists, corporate actions, FII/DII) | Prices, delivery, derivatives, index data, actions, flows | Official downloadable files; needs browser-like headers and cookies; format changed in 2024 | BSE files; yfinance (unofficial, \~15-minute delayed, check terms); jugaad-data and nsepython open-source wrappers |
| BSE (daily files, corporate announcements, XBRL results, shareholding) | BSE prices, filings, results, ownership | Official site downloads and public endpoints | NSE equivalents |
| NSE and BSE financial results and integrated filings (XBRL) | Quarterly and annual statements | XBRL files from the exchanges, parsed with lxml against the SEBI taxonomy | Parsed HTML result tables; a paid export later if gaps hurt |
| NSE and BSE shareholding pattern filings | Ownership, pledges | Structured filings and pages | Trendlyne or Screener exports (personal use, check terms) |
| AMFI | MF NAV, monthly portfolio holdings | Official files | Fund-house sites |
| NSDL FPI data | FPI flows, sector-wise | Official pages | NSE FII/DII daily |
| RBI (DBIE, press releases, MPC statements), MOSPI, PIB, Ministry of Finance, SEBI, CCIL | Macro series, policy text, circulars, G-sec yields | Official portals, files and RSS | FRED for global series |
| FRED (St. Louis Fed) | US rates, Fed funds, Brent, USD/INR, DXY proxies, global stress indices | Free API key | Stooq, yfinance |
| NSE and BSE announcements feed | Announcement categories and dates, used only for event flags | Official announcement pages and feeds | Exchange announcement archives |

**Ingestion framework (applies to every source)**
  - **Connector contract:** fetch → store raw file with SHA-256 and fetch time → parse → validate against schema → load to the silver store → log to a source\_files manifest and a job\_runs table.
  - **Idempotent and incremental:** a watermark per source records the last good date; reruns never duplicate rows; missing days are filled automatically.
  - **Resumable backfill:** history loads in date chunks that can be paused and resumed, and runs on your computer.
  - **Resilience:** retries with backoff, polite rate limits, a per-source circuit breaker, a daily canary request that detects blocked or changed sources, and a schema fingerprint that flags format drift before parsing.
  - **Calendars:** an exchange trading calendar (holidays, special sessions) so "missing day" and "no data because holiday" are never confused.
  - **Fixtures:** every connector saves small real sample files for tests, so parsers can be tested without hitting the source.
  - **Alerts:** a failed, late, or format-changed source sends a Telegram alert naming the source and what to check.
**Backfill order and report.** Reference data first (company master, symbol history, calendar), then corporate actions, prices and indices, financials, ownership, flows and derivatives, and macro. The backfill produces a coverage matrix (stock × dataset × year) showing loaded, partial, missing, and quarantined data. The model build cannot start until coverage for Nifty 200 passes the gates in section 6.
**Refresh schedule (IST)**
|  |  |  |
| :- | :- | :- |
| Dataset | Frequency | When |
| Daily prices, delivery, indices, VIX, F\&O, surveillance lists | Daily, trading days | After close, from about 6:30 pm; ready by 8 pm |
| FII/DII flows, bulk and block deals, insider trades | Daily | About 8 pm |
| Corporate actions and announcements | Hourly in market hours, plus a daily sweep | Continuous |
| Delayed intraday quotes for portfolio value | Hourly, 9:15 am to 3:30 pm, plus open and close snapshots | Market hours |
| Global cues, FRED series, USD/INR, crude | Daily | About 7:30 am |
| Quarterly results and financials | Daily during results season, weekly otherwise | After each filing |
| Shareholding patterns | Weekly in the weeks after quarter-end | Scheduled |
| MF holdings and NAV | Monthly and daily respectively | On release |
| Macro releases (CPI, IIP, GDP, GST) | On release date | Calendar-driven |
| Event calendar (RBI, FOMC, budget, data releases) | On publication | Event-driven |
| Universe and index reconstitution | Semi-annual, plus on announcement | Manual check |
| Full data-quality audit and restore test | Weekly | Sunday |

## **5. Weekly operating model, signals and event flags**
The app runs on a weekly rhythm with a four-week audit. It produces three kinds of signal, each held to a 90% certainty bar, and says "no signal" when nothing qualifies. News analysis is dropped from v1; a light, rule-based event layer replaces it.
**Weekly rhythm**
|  |  |
| :- | :- |
| When | What happens |
| Friday after close (about 8–9 pm IST) | Data refresh, quality gates, scoring; the Weekly Plan for the coming Monday to Friday is built and one Telegram summary is sent |
| Monday | You act on the plan in Kite and log it with one tap: done, partly, skipped |
| Tuesday to Friday, after close | Exit watch on your holdings only; an alert is sent only if a sell-out or crash-exit signal or a hard rule fires |
| Every 4 weeks | Monthly audit of the last four weeks, with the feedback loop (section 8.4) |

**Signals**
|  |  |  |  |
| :- | :- | :- | :- |
| Signal | Definition | Fires when | What you see |
| A. Opportunity (buy) | A stock closes at least 10% above the entry price on any close within the next 5 trading days (threshold and window are settings) | Calibrated probability is at least 90%, the validation gate below is passed, and it fits your budget, caps and liquidity filters | Ranked list, highest expected gain on top: stock, certainty, expected gain, suggested quantity within budget, guide entry price, sell-out price, one-line reason |
| B. Sell-out nudge (take profit) | For a stock you hold: the 10% target is reached, or it has gained and the probability of further gain this week has fallen below 50%, or it has dropped back from its peak by the trailing amount | Rule plus a calibrated probability check | "Sell now" or "Sell at or above ₹X", profit after charges, one-line reason |
| C. Crash exit | A held stock falls at least 10% below its last close on any close within the next 5 trading days (setting) | Calibrated probability is at least 90% and the validation gate is passed | "Sell before the drop", certainty, validated track record, one-line reason |
| Hard exit rules (not predictions) | Stop level hit; pledge jump; auditor resignation; ASM or GSM listing; trading halt; position far above its cap in a stress regime | Deterministic | "Exit rule hit: \<rule\>"; labelled as a rule, never as a percentage |

**How the 90% bar is enforced**
  - Models estimate the probability of each event and are calibrated against past outcomes, so a stated 90% should mean about 90% of the time historically.
  - The live cutoff is the lowest probability at which out-of-sample walk-forward signals were right at least 90% of the time, with at least 30 past signals and a 95% confidence interval whose lower bound is at least 80%.
  - If no cutoff passes, that signal type is switched off and the page says "The model cannot yet meet the 90% bar". The app never lowers the bar on its own.
  - Each signal shows its evidence: past signals at that level and how many were right.
  - Signals also require the data-quality score to be above the threshold and the stock to pass liquidity and surveillance filters. Default signal universe: Nifty 200.
**Honest feasibility.** These thresholds are very demanding. Treat the points below as approximate, from my knowledge; phases P2 and P6 measure the real frequencies.
  - A Nifty 200 stock typically moves about 3–4% in a week, so a +10% week is a rare event (roughly 1–3% of stock-weeks for large caps, more for smaller stocks). Such moves are mostly driven by catalysts that are hard to foresee, such as results surprises, deals and policy changes.
  - Crashes of 10% or more in a week are also mostly event-driven. Calling them at 90% certainty is harder still.
  - Expect most weeks to show "No qualifying signal", and some signal types may never pass validation on Nifty 200. That is the design working, not failing.
  - Confirming a 90% claim needs enough past signals. If a signal fires only a few times a year, validation takes years; full-universe history helps by adding more occurrences, but illiquid small caps are not tradeable at your size.
  - You can change the thresholds in Settings. Lowering them raises signal counts and noise, and the app shows the observed precision at whatever level you choose.
**Noise controls**
  - At most 5 opportunities shown per week; the rest are hidden.
  - No intraday signals. Alerts are limited to: the Weekly Plan is ready (one message a week), exit signals and hard rules on your holdings, and a failed-data warning.
  - A stock is not re-signalled in the same direction within the signal window.
  - When nothing qualifies, the plan shows a single line: the closest candidate and its probability (for example "Closest: ABC at 41%, below the 90% bar"), which can be turned off.
**Light event layer (replaces news)**
No news ingestion, no text analysis and no language models. Only structured data from exchange announcement categories and a fixed calendar:
  - Results and board-meeting dates ("results in 3 days" blocks new buys unless you allow them in Settings).
  - Announcement types read from structured fields: credit rating change, pledge change, auditor change, trading halt, ASM and GSM list changes, bulk and block deals.
  - Fixed calendar: RBI policy, FOMC, budget, and CPI, IIP and GST release dates.
  - Uses: model features (days to results, days to RBI decision), hard exit rules, and one line in the Weekly Plan only when an event touches a holding.
  - Deferred: news and text analysis. Revisit after the paper test if the monthly audit shows losses clustered around event shocks these flags missed.
## **6. Data quality, point-in-time correctness and governance**
No data reaches features, models or your portfolio page until it passes the gates below. A failure is quarantined with a reason, never silently repaired. Each gate is BLOCK (stops everything that depends on it) or WARN (flagged, continues).
**Quality gates**
|  |  |  |
| :- | :- | :- |
| Gate | Check | Severity |
| Schema | Required columns, types, units (rupees vs lakhs and crores), encoding, date formats, time zones | BLOCK |
| Freshness | Data date equals the last trading day per the exchange calendar; source file not older than its schedule | BLOCK |
| Completeness | At least 98% of Nifty 200 and 95% of the full universe priced each day; no gaps inside a stock's listed life; every portfolio holding priced | BLOCK for holdings and Nifty 200 |
| Keys and duplicates | One row per stock per date; ISIN to symbol mapping unique over time; symbol changes and mergers resolved | BLOCK |
| Range and sanity | High at least low, close between them, no negative volume or price, circuit limits respected; a one-day move over 20% must match a corporate action or a circuit-band rule | WARN, BLOCK if unexplained |
| Corporate actions | Adjusted series continuous across splits and bonuses; holdings quantity and cost adjusted to match | BLOCK |
| Cross-source | NSE close versus BSE or a second source within 0.5% for benchmarks, holdings and top-ranked names | WARN, BLOCK on large gaps |
| Financial integrity | Balance sheet balances within tolerance; standalone and consolidated never mixed; units consistent; restated filings versioned; period matches filing date | BLOCK for that company's fundamentals |
| Ownership integrity | Holding percentages sum to about 100%; pledge not above holding | WARN |
| Macro integrity | Release date recorded; values within historical bounds; units and frequency checked | WARN |
| Point-in-time | No row visible to a model before its public release time | BLOCK (CI test) |
| Distribution drift | Feature null rates and distributions within expected bounds | WARN, BLOCK on extreme drift |
| Backfill completeness | Coverage matrix matches expected trading days and filing counts | BLOCK before modeling |

**Daily Data Quality Score (0–100):** weighted by domain (prices and corporate actions highest, then financials, flows, macro, event flags). It appears next to every output. Below the set threshold (default 90) the app shows NO SIGNAL with the reason.
**Point-in-time and versioning**
  - Every table stores event time (what the number is about), known-at time (when it became public), ingested-at time, and a version. Financial restatements create new versions; models see the version that existed on the day.
  - Fundamentals join to prices by known-at date plus a safety lag of one trading day.
  - Raw prices and adjusted prices are both kept; adjusted series are rebuilt from raw plus corporate actions, never edited by hand.
  - Index membership, sector and market-cap bucket are stored as of each date.
  - Delisted, merged and suspended stocks stay in history.
**Lineage and cataloguing:** a data catalog table documents each dataset (owner module, source, grain, refresh, schema, quality rules, first and last date). Each row can be traced back to its raw file and the pipeline version that produced it.
**Data Health page:** per-source status and last success, freshness, coverage matrix, quality score trend, quarantined records with reasons, and format-drift warnings.
**Data KPIs**
|  |  |
| :- | :- |
| KPI | Target |
| Daily quality score | At least 95 on at least 95% of trading days |
| Freshness | EOD data ready by 8 pm IST on at least 98% of trading days |
| Nifty 200 price completeness | At least 99% of stock-days |
| Fundamentals coverage | At least 95% of Nifty 200 with the latest filed quarter within 2 days of filing |
| Unresolved quarantine items | Zero older than 7 days |
| Backfill | Every dataset loaded to its recorded earliest date, with gaps documented |

**Governance**
  - Licensing: record each source's terms in the registry; store personal-use data only; do not redistribute.
  - Retention: raw and silver data kept indefinitely; holdings data kept only in the private database.
  - Backups: daily database backup and weekly lake snapshot; a monthly restore drill that rebuilds the silver layer from raw.
  - Access: data lake credentials read-only for the app, write access only for jobs.
## **7. Features and models**
**Features.** Computed per stock per day from point-in-time data, ranked within sector and universe each date.
|  |  |  |
| :- | :- | :- |
| Family | Examples | Data used |
| Price and volume | 1, 5, 20, 60, 120-day returns; distance from 52-week high; moving-average gaps; RSI; realised volatility; gap frequency; volume spikes | Prices |
| Participation | Turnover, delivery % trend, liquidity | Prices, delivery |
| Financial behaviour | Revenue and profit growth (YoY, QoQ), margin trend, ROE, ROCE, leverage, interest cover, cash conversion, earnings surprise versus trend, valuation versus own history and sector | Financials |
| Investor behaviour | Change in FII, DII, MF and promoter holding; pledge change; bulk and block deals; market-level FII/DII flows | Ownership, flows |
| Derivatives | Open interest change, PCR, rollover, India VIX level and change | F\&O, VIX |
| Market regime | Nifty trend, breadth, VIX percentile, liquidity and rate stance, USD/INR, crude, US yields | Index, macro |
| Sensitivity profile | Rolling beta; downside beta; up and down capture; drawdown in past corrections; betas to USD/INR, crude, US yields, VIX; return on past FII-selling days; earnings-day abnormal return and drift | All above |
| Event flags | Days to results and to RBI, FOMC and budget dates; rating, pledge, auditor and surveillance flags; data-release surprise versus trend | Announcement categories, calendar |
| Cross-sectional | Rank and z-score of each feature within sector and universe | Derived |

Rules: all features lagged to avoid look-ahead; outliers winsorised; each feature has a definition hash and an owner; features are versioned and documented in the catalog; a feature with high missingness or unstable behaviour is dropped.
**Targets**
  - **Event A (opportunity):** the stock closes at least 10% above the entry price on any close within the next 5 trading days. Binary target.
  - **Event C (crash):** the stock closes at least 10% below its last close on any close within the next 5 trading days. Binary target.
  - **Expected gain:** predicted maximum 5-day gain from a quantile model, used to rank Event A signals.
  - **Context only:** 20-day excess return rank, kept as a trend feature, not a signal.
  - Thresholds and window are settings; models are retrained when they change.
**Model stack (each layer must earn its place)**
1.  **Baselines:** Nifty 50 total return, equal-weight Nifty 200, 12-1 momentum, low volatility, quality plus value rank, and for each event the base rate (the precision of picking a stock at random). If the model cannot beat these, nothing ships.
2.  **Two LightGBM classifiers** (event A and event C) trained on weekly samples across the full-universe history, with monotonic constraints where the economics is clear and weighting to handle rare events without leaking future data.
3.  **Quantile model** for expected gain, used only to rank signals.
4.  **Regime layer:** hidden Markov or rule-based regime (bull, range, stress); precision is reported per regime.
5.  **Calibration and validation:** isotonic calibration, then the precision-at-cutoff test with confidence bounds from section 5; conformal intervals where useful.
6.  **Ensemble and explanation:** average a few diverse models; SHAP drivers for every signal, turned into the one-line reason.
**Training data:** the full-universe history (including delisted names) trains the model; the app scores Nifty 200. Retrain monthly with an expanding window; run a full hyperparameter search quarterly; promote a challenger only if it wins out-of-sample. Daily data for hundreds of stocks over many years is enough for gradient boosting; deep sequence models are out of scope.
**No language models in v1.** Reason lines are templated from model drivers and database fields; no text is generated or analysed by a language model.
## **8. Application: four clean screens**
**Design rules (no clutter, no noise)**
  - Four screens only: **This Week**, **Portfolio**, **Monthly Audit**, **Settings**. A small data-health badge sits in the header and expands only when something is wrong.
  - The home screen answers one question: what should I do this week? Its top line says "2 actions this week" or "No action this week".
  - Nothing below the 90% bar is shown as a signal. No news, no tickers scrolling, no intraday charts, no widgets.
  - At most 5 opportunities; every exit signal; everything else collapses into "Hold (n)".
  - Plain words, one accent colour per action (green buy, red sell, grey hold). Details sit behind a "Why" expander, not on the page.
  - Every number carries its source and as-of time in small text.
**8.1 This Week (home)**
Week of Mon 12 Oct                         Data OK · as of Fri 9 Oct, 8:40 pm  
  
2 actions this week  
  
SELL NOW (exit before drop)  
  XYZ   Crash risk 92%   Sell at market open     Loss avoided: est. ₹X  
        Why: weak trend, rising volatility, pledge up      \[Done\] \[Partly\] \[Skipped\]  
  
OPPORTUNITIES (best expected gain first)  
  1  ABC   Certainty 93%   Expected gain +12%   Buy 3 shares near ₹1,150   Sell-out at ₹1,265  
        Why: three quarters of rising profit, FII buying   \[Done\] \[Partly\] \[Skipped\]  
  
Hold (6)   ▸ expand  
Events this week: results for DEF on Wed.  
Closest candidate: GHI 41%, below the 90% bar.
If nothing qualifies, the screen says "No action this week. Hold all positions." and shows only the events and closest-candidate lines. Each row has a one-tap log (done, partly, skipped), which feeds the audit.
**8.2 Portfolio**
  - **Input per holding:** stock (autocomplete, mapped to ISIN), quantity (whole number), buy price per share or total invested, buy date, optional charges; multiple lots; CSV import from your Zerodha Kite Console holdings download; edit, delete, and mark a sale.
  - **Shows:** invested, current value, unrealised and realised gain or loss in ₹ and %, today's change, one table of holdings (quantity, average cost, last price, value, gain or loss, weight, status: Hold, Sell now or Sell at ₹X), and one line chart of value versus Nifty 50 for the same cash flows.
  - **Prices:** open and close snapshots, hourly delayed quotes in market hours, official close from the daily file; a "delayed" tag and a red "stale" tag when old; no new actions on stale prices.
  - **Correctness:** splits, bonuses and dividends adjust quantity and cost automatically. Estimated charges and tax appear on any sale suggestion. Holdings are private: stored only in the private database, never logged, never sent to a third-party service or into alerts.
  - **Cost awareness (Zerodha, checked 2 Oct 2026; confirm):** zero brokerage on delivery; STT 0.1% each side; stamp 0.015% on buy; NSE charge 0.00307%; ₹15.34 depository fee per stock sold. A buy then sell costs about ₹23 on ₹3,500. A sell-out nudge is shown only if profit after charges and tax is positive; a stop or crash exit is shown regardless of cost.
**8.3 Monthly Audit (every 4 weeks) and feedback loop**
A locked, exportable review of the last four weeks, with a date filter (last 4 weeks by default; 12 weeks, 6 months, year, custom). The top line is a verdict, for example "Opportunity signals: 3 issued, 2 correct (67%); claim is 90%. Not meeting the claim yet."
|  |  |
| :- | :- |
| Panel | What it shows |
| Signal scorecard | For each signal type: issued, correct, wrong, precision with confidence range versus the 90% claim; outcome of every signal with date, price, result |
| Missed events | \+10% weeks and 10% drops in your universe that the model did not flag, to track recall honestly |
| Your actions | Done, partly, skipped, and what each group earned or lost; slippage from guide price to your fill |
| Money | Four-week and since-start return versus Nifty 50 total return, realised and unrealised gain or loss, charges and tax paid, max drawdown |
| Calibration | Stated probability versus what actually happened, by band |
| Data and model health | Data-quality trend, failed jobs, drift, guardrail triggers, kill-switch events |
| Proposed changes | Settings or model changes with evidence (below) |

**Feedback loop**
1.  **Your tags:** after each action you tap done, partly or skipped, and optionally a reason (price moved, no cash, disagreed). Stored as data; never changes the model directly.
2.  **Settings proposals:** from outcomes of all signals (not only the ones you traded), the app may propose changes such as the probability cutoff, signal window, trailing-stop distance, universe or caps, with the replayed effect on past signals. You approve one change at a time; each becomes a new versioned setting.
3.  **Retraining:** monthly retrain on all weekly samples with realised outcomes; the new model replaces the old only if it wins out-of-sample (champion and challenger). If a signal type fails validation after retraining, it is switched off and the audit says so.
**Limits:** with a small monthly budget and a rare-signal design you will have very few trades, so the audit leans on all signals the model issues, not your trades. Proposals appear only after enough matured signals (at least 30) and a replay across sub-periods; there are no automatic changes, and a changelog records each one.
**8.4 Settings**
|  |  |  |
| :- | :- | :- |
| Group | Setting | Default |
| Budget | Weekly and monthly budget; contribution day | Set locally |
| Signals | Gain threshold; crash threshold; window; certainty bar; max opportunities shown | 10%; 10%; 5 trading days; 90%; 5 |
| Risk | Per-stock and per-sector caps; stop rule; trailing stop; drawdown pause | 15%; 30%; 2× ATR; set in Settings; -12% (also shown in rupees) |
| Universe | Signal universe; minimum liquidity; allow buying before results | Nifty 200; on; off |
| Costs and tax | Zerodha preset; tax profile | As above |
| Schedule | Plan day and time; audit date | Friday evening; first Saturday after every 4th plan |
| Alerts | Telegram on or off; hide closest-candidate line | On; off |
| Goal | Target return and date | Shown as a progress line with its probability |

**Goal feasibility:** you can enter a target (for example +50% in a month). The app simulates from historical outcomes, shows the probability of reaching it (about zero for +50% a month), and labels it Realistic (up to about 2% a month), Stretch (2–5%) or Unrealistic (above 5%). Recommendations are bounded by the risk settings, never by the target.
**8.5 Alerts (Telegram):** the Weekly Plan is ready (one message a week), a sell-out, crash-exit or hard-rule alert on a holding, and a failed-data warning; at most 3 a week unless a hard rule fires; summaries only, never quantities or values.
**8.6 Requirements and acceptance criteria**
|  |  |  |
| :- | :- | :- |
| ID | Requirement | Acceptance criteria |
| D1 | Source registry and connectors | Each source has terms, earliest date, fallback, health; each connector passes fixture tests and a live probe |
| D2 | Historical backfill | Every dataset loaded to its recorded earliest date; coverage matrix generated; gaps documented |
| D3 | Daily and intraday refresh | Schedules in section 4 run; end-of-day data ready by 8 pm IST on at least 98% of trading days |
| D4 | Company master and corporate actions | ISIN-keyed, symbol history complete; adjusted prices rebuilt from raw and actions with a continuity test |
| D5 | Financial statements and ratios | Point-in-time, versioned, standalone and consolidated kept apart; integrity test passes |
| D6 | Ownership, flows, derivatives, macro | Loaded with release dates; coverage of Nifty 200 at least 95% |
| D7 | Light event layer | Results dates, announcement categories, calendar and flags loaded from structured sources; no text analysis |
| D8 | Quality gates and Data Health | All section 6 gates enforce BLOCK or WARN; quality score on every output |
| A1 | Portfolio input and valuation | Invalid entries rejected; totals match a manual calculation within ₹1; delayed and stale tags |
| A2 | Weekly scoring and plan | Plan built Friday by about 9 pm IST when gates pass; otherwise NO SIGNAL with the reason |
| A3 | Signals A, B, C with the 90% bar | Fires only above the validated cutoff; evidence shown; switches off when validation fails; never lowers the bar silently |
| A4 | Hard exit rules | Each rule fires on fixtures; labelled as a rule, not a percentage |
| A5 | This Week screen and logging | At most 5 opportunities; one-tap logging; empty-state message when nothing qualifies |
| A6 | Monthly Audit and feedback | Four-week verdict, missed events, calibration, proposals gated by evidence; exportable snapshot |
| A7 | Settings | All settings versioned; goal feasibility band shown |
| A8 | Alerts | Telegram messages for each trigger within the cap; summaries only |
| A9 | Security and privacy | Login required; private database; secrets only in environment stores |

## **9. Guardrails, transparency, evals and backtesting**
Guardrails are enforced in code. Any breach downgrades the output to NO SIGNAL and alerts you. Each fired guardrail appears on the page with its reason.
**Risk and loss guardrails**
|  |  |
| :- | :- |
| Guardrail | Default |
| Max weight per stock / per sector | 15% / 30% (set for a small portfolio; section 12) |
| Position sizing | Volatility-scaled; fractional Kelly capped at 0.25 |
| Suggested stop | 2× ATR or thesis-break event, shown with every signal |
| Drawdown alert | At -8%: review. At -12%: pause new buy signals and recommend reducing risk |
| Liquidity and surveillance | Exclude low-turnover, ASM or GSM-listed, circuit-locked, and stocks listed under 1 year |
| Regime brake | In stress regime, cap suggested equity exposure and require higher confidence |
| Goal guard | The target return never raises risk limits |

**Model guardrails**
  - Abstain when calibrated probability is under the threshold or the conformal interval crosses zero.
  - Kill switch: if rolling 3-month rank IC turns negative, rolling precision of signal A or C falls below 80%, calibration error exceeds 8 points, or input drift is flagged, freeze to baseline or NO SIGNAL until you re-approve.
  - Champion-challenger promotion only after out-of-sample wins and 3 months of paper forward-testing.
  - Monotonic constraints and a feature allowlist; no unreviewed feature goes to production.
**Anti-hallucination**
  - No language model is used anywhere in v1. Predictions come only from trained statistical models; reasons are templated from model drivers and database fields.
  - The UI can render only values present in the database, each with source and as-of time.
  - Every signal shows its validated track record. If no probability cutoff meets the 90% bar in validation, the app says so and shows no signals; it never lowers the bar silently. Signals A and C need 90% or higher calibrated certainty; signal B and hard exits are labelled as rules, not predictions; at most 3 alerts a week.
**Transparency:** the one-line reason on every signal (section 8.1), the Scorecard of every signal versus its outcome, the decision log, versioned models and configs, and a model card per release.
**Evals**
|  |  |  |
| :- | :- | :- |
| Eval | When | Pass condition |
| Walk-forward backtest with costs | Every model change | Beats baselines and Nifty net of charges; positive across most sub-periods |
| Leakage and shuffled-label tests | CI on every change | Shuffled labels give about zero edge; no feature uses future data |
| Calibration check | Weekly | Within 5 points per probability bucket |
| Live outcome tracking (IC, hit rate) | Daily | Rolling 3-month IC above 0, else freeze |
| Decision-rule tests | CI | Fixture portfolios give expected actions including cost and cap edge cases |
| Guardrail drills | CI and monthly | Simulated stale data, drawdown and bad prices trigger halt and alert |
| Explanation faithfulness | CI and daily sample | Every number in a reason matches the database |
| Data-quality gates | CI and daily | Gate tests fail on bad fixtures and pass on good ones |

**Backtesting rules**
  - Walk-forward only; purged and embargoed validation (5-trading-day labels need a 5-day embargo); no random splits.
  - Point-in-time data, delisted stocks included, index membership as of each date.
  - Costs: Zerodha charges plus 10–20 bps slippage; minimum liquidity filter; signal at close of day T, trade at the next open or VWAP; skip circuit-locked stocks.
  - Overfitting controls: log every experiment; deflated Sharpe or reality check; stable results across 2018–19, 2020, 2021–22 and 2023–26 and across sectors.
  - Promotion targets: mean rank IC above 0.03; net excess return over Nifty 50 total return positive in at least 70% of rolling 12-month windows; drawdown no worse than benchmark; turnover under a set cap. These are starting hypotheses; tighten them after the baseline results.
## **10. Automation, MLOps, security and compliance**
**Automation.** One scheduled workflow per cadence in section 4 (daily prices, flows, global cues, results season, weekly plan, monthly audit and retrain). Each workflow runs ingest, quality gates, features, scoring and dashboard refresh in order, and stops at the first BLOCK. Heavy history loads run locally.
**MLOps**
  - Git for code; dated Parquet snapshots for data; MLflow for runs; each signal stores its model version, config version and data hash so any past signal can be reproduced.
  - Idempotent jobs with retries, backoff, and a dead-letter log for failed sources.
  - Unit tests for connectors (on fixtures), features, decision rules and cost maths; a leakage test in CI that fails the build; evals gate every model release.
  - Monitoring: job success and runtime, source health, data-quality score, drift, rolling IC and calibration, guardrail triggers; failures alert within 5 minutes.
  - Daily database backup, weekly lake snapshot, monthly restore drill.
  - Non-functional targets: daily job succeeds on at least 99% of trading days and finishes in under 30 minutes after data arrival; pages load in under 3 seconds with cached data; run cost under about ₹800 a month.
**Security and privacy**
  - Login required for the app; private repository; private database; read-only credentials for the app; secrets only in .env locally and in GitHub or Streamlit secrets online, never in code or prompts.
  - Holdings and portfolio values are never logged, never sent to a language model, and never put in Telegram alerts (summaries only).
  - Respect each source's terms and robots rules; use official files and APIs first; rate-limit requests.
**Compliance**
  - Personal use only; the app is a research tool, not investment advice, and says so on every output.
  - Sharing signals with others as advice may require SEBI registration (research analyst or investment adviser).
  - No order placement in v1. If you ever automate orders through Kite, SEBI's retail algo framework applies (static IP, India-hosted servers, exchange algo identifier); reviewed separately before any such work.
  - Tax estimates are indicative; check current rates and use a tax professional for filing.
## **11. Cost, roadmap, risks and open questions**
**Monthly run cost (target under ₹800)**
|  |  |  |
| :- | :- | :- |
| Item | Cost | Needed? |
| Data sources (exchange files, filings, RBI, FRED) | ₹0 | Yes |
| Storage (R2 or B2 free tier) and DuckDB | ₹0 | Yes |
| App database (Neon free) | ₹0 | Yes |
| Scheduling (GitHub Actions free minutes) | ₹0 | Yes |
| App (Streamlit Community Cloud) and alerts (Telegram) | ₹0 | Yes |
| VPS if free scheduling limits are hit | \~₹400 | Only if needed |
| Kite Connect plan for live ticks | ₹500 | Not needed in v1 |
| Paid fundamentals feed for pre-2017 data | Deferred | Only if free history proves too thin |

**Data-first roadmap (about 6 months to start of forward testing, one person part-time)**
|  |  |  |  |
| :- | :- | :- | :- |
| Phase | Weeks | Deliverables | Exit gate |
| P0 Foundations | 1 | Repo, CLAUDE.md, CI, Telegram bot, job framework | CI green; test alert received |
| P1 Data platform core | 2–4 | Lake and DuckDB layout, app database and schema, source registry, connector framework, trading calendar, company master, raw archive, quality-gate framework | Framework runs end to end on one source with fixtures |
| P2 Market data backfill | 4–8 | Prices, delivery, indices, VIX, F\&O, corporate actions, adjusted series, surveillance lists for the full universe, backfilled on your computer; coverage matrix; Data Health v1 | **Gate G1:** coverage and quality gates pass for Nifty 200; earliest dates recorded |
| P3 Portfolio and Config pages | 6–10 (parallel) | Portfolio input, Kite CSV import, valuation and gain/loss, delayed hourly prices, Config page with Zerodha cost preset and feasibility band, login | Totals match manual calculation within ₹1 |
| P4 Company and investor data | 8–12 | XBRL financials and ratios, ownership, FII/DII flows, deals, MF data, macro and policy series, all point-in-time | **Gate G2:** point-in-time test passes; fundamentals coverage at least 95% of Nifty 200 |
| P5 Light event flags | 11–13 | Calendar and exchange announcement-category flags (results, board meetings, ex-dates, pledges, auditor changes, regulatory actions); no text analysis, no language models | **Gate G3:** flags match exchange records on 20 sampled stocks |
| P6 Features, baselines, backtester | 13–17 | Feature store and catalog, sensitivity features, baselines, backtester with Zerodha costs, leakage tests | **Gate G4:** leakage and shuffled-label tests pass; baselines reproduce plausibly |
| P7 Models | 17–21 | LightGBM signal A/B/C models, regime, risk layer, calibration, walk-forward precision-at-cutoff test, SHAP, evals and model card | **Gate G5:** beats best baseline net of costs; each signal goes live only if it meets the 90% precision bar in walk-forward tests, else it stays off |
| P8 Decisions and review | 20–25 | Weekly plan engine, reasons, the four screens (This Week, Portfolio, Monthly Audit, Settings), decision log, outcome tracking, feedback loop | Fixtures give expected actions; dashboard reconciles with Portfolio page |
| P9 Automation and monitoring | 24–26 | All schedules, drift and quality alerts, backups, weekly report | A full week runs unattended; forced failure alerts you |
| P10 Paper forward test | 27–39 | Live weekly signals logged, no real money; feedback tags on; config proposals enabled once enough signals mature | **Gate G6:** 3 months of IC, calibration and net return within backtest tolerance |
| P11 Small live use | After G6 | Manual trades with small capital; signals as one input | Review monthly; scale only if 6+ months of results hold |

**Key risks**
|  |  |
| :- | :- |
| Risk | Mitigation |
| Free sources change format, block automated access, or disappear | Source registry, canary checks, schema-drift alerts, fallbacks, official files first, optional paid feed later |
| Short or thin history for fundamentals and event flags | Record true start dates; validate on available periods; weight event flags low until proven |
| Weak or no predictive edge | Baselines, honest gates, stop-at-baseline rule |
| Leakage or overfitting | Point-in-time storage, purged validation, leakage tests in CI, forward test |
| Data errors reaching signals | Quality gates, quarantine, NO SIGNAL on failure |
| Free-tier limits (storage, scheduling minutes) | Partitioned Parquet, local backfills, VPS fallback |
| Over-trusting signals; unrealistic goal | Goal feasibility band, abstention, caps, Scorecard shows real accuracy |
| The 90% bar is rarely met, so most weeks show no signal | Accepted by design: abstain, never lower the bar; switch a signal off if validation fails; hard exit rules still protect holdings |

**Open questions**
  - Decided: full NSE and BSE history for every stock, including delisted ones.
  - Decided: heavy backfills run on your computer.
  - Decided: consider a paid source for pre-2017 financials only after the 2017-onward data proves its worth (review at gate G2).
  - Decided: news is dropped; a light rule-based event layer replaces it (section 5). To confirm: crash threshold defaults to a 10% drop within 5 trading days.
  - Holdings are a small retail portfolio; sizing and cost defaults adjusted in section 12.
**Assumptions:** broker is Zerodha Kite; Telegram is the alert channel (you will install it); free-source terms permit personal automated use (verify per source before building each connector); all data-source depths in section 3 are targets until probed. The separate Claude Code Build Guide has milestone prompts and Telegram setup; it follows this data-first phase order.
## **12. Sizing, cost and infrastructure for the chosen configuration**
**Configuration:** full NSE and BSE history for every stock, heavy backfills on your computer, a small retail portfolio with a fixed monthly top-up, free-tier cloud wherever possible. **All numbers below are my estimates from typical file sizes and source behaviour, not measurements.** Phases P1 and P2 measure the real figures and this section is updated at gate G1.
**Universe size:** about 2,000+ main-board NSE stocks and roughly 5,000+ BSE-listed ones, plus delisted, merged and suspended names; about 8,000–10,000 instruments over the full history, many dual-listed.
**Data size estimate**
|  |  |  |  |  |
| :- | :- | :- | :- | :- |
| Dataset | Rows (approx.) | Curated Parquet | Raw archive | Notes |
| Daily prices with delivery, NSE and BSE, full history | 30–40 million | 1.2–2.5 GB | 3–5 GB | Adjusted series computed in DuckDB, not stored twice |
| Indices and VIX | \~1.5 million | 0.05 GB | 0.1 GB |  |
| F\&O summary per underlying per day (OI, PCR, rollover) | \~1.5 million | 0.15 GB | 3–6 GB if raw daily files are kept | Recommended; contract-level data for the last 2 years is optional (+1.5 GB) and full contract-level history (3–6 GB) is not recommended |
| Corporate actions and company master | \~0.2 million | 0.02 GB | 0.1 GB |  |
| Financial statements (parsed XBRL) | 150,000–200,000 filings; \~25 million facts | 0.5–1 GB | 3–5 GB | From about 2017 on; paid pre-2017 data would add to this |
| Shareholding patterns | \~160,000 filings; \~10 million rows | 0.3 GB | 2–3 GB |  |
| Announcements (metadata and links, not PDFs) | \~6 million | 0.4 GB | 1–2 GB | PDFs stored only for selected filings |
| Flows, deals, insider trades, MF aggregates, surveillance lists | a few million | 0.2 GB | 0.3 GB | Scheme-level MF holdings skipped in v1 |
| Macro and policy text | small | 0.2 GB | 0.2 GB |  |
| Features: liquid names (about 1,500) × about 300 features, daily | \~7 million rows | 3–4 GB (about 0.7 GB for Nifty 200 only) | n/a | Rebuilt from silver data |
| Models, backtests, eval reports | n/a | 1–2 GB | n/a |  |
| **Total** |  | **about 8–12 GB** | **about 13–22 GB** |  |

  - **Local disk needed:** raw plus curated is about 25–35 GB, plus 20–40 GB of temporary space during backfill and a backup copy. Plan for at least 100 GB free; an external SSD is a sensible safeguard.
  - **Growth after backfill:** about 1–2 GB a year.
  - **Cloud copy:** keep only curated data needed by daily jobs and the app in the cloud (Nifty 200 features, silver tables, gold tables), about 7–8 GB, which fits Cloudflare R2's free 10 GB. Raw archives stay on your computer with a backup.
**Backfill time (polite request rates, resumable, run overnight)**
|  |  |
| :- | :- |
| Dataset | Estimate |
| Daily price files, NSE and BSE (\~8,000 days each) | \~9 hours |
| F\&O daily files | \~4 hours |
| Financial result filings (\~150,000–200,000) | \~125–170 hours |
| Shareholding filings (\~160,000) | \~100–130 hours |
| Announcements metadata | \~30 hours |
| **Total** | **roughly 250–350 machine-hours, i.e. 3–5 weeks of overnight runs** |

Exchange sites may rate-limit harder than assumed, which would stretch this. The backfill is chunked, so it pauses and resumes safely, and the filings backfill is ordered newest first so recent data is usable early.
**Architecture requirements**
|  |  |
| :- | :- |
| Component | Requirement |
| Your computer (backfill, heavy training, development) | 16 GB RAM or more, 8 cores helpful, 100 GB or more free SSD, stable broadband; DuckDB and Polars stream larger-than-memory data; monthly LightGBM retrain on about 7 million rows × 300 features should take tens of minutes; quarterly hyperparameter search a few hours |
| Object storage | Cloudflare R2 free 10 GB (curated data only); free egress; Backblaze B2 is an alternative |
| App database | Neon Postgres free tier; under 100 MB for years (holdings, signals, decisions, outcomes, config, quality results, job runs) |
| Scheduled jobs | GitHub Actions; estimated 600 of the 2,000 free monthly minutes (daily pipeline \~440, weekly plan and monthly audit \~160); a small VPS (2 vCPU, 4 GB) is the fallback if minutes run short |
| App hosting | Streamlit Community Cloud; limited memory (about 1 GB), so the app reads small precomputed gold tables, never the raw lake |
| Alerts | Telegram bot |
| Write ownership | Each dataset has one writer (backfill from your computer, then daily jobs) to avoid conflicts; datasets are partitioned by date with manifests |
| Sync | Your computer uploads curated data to R2 with checksums; daily jobs write directly to R2; your computer pulls increments |

**Monthly cost for this configuration**
|  |  |  |
| :- | :- | :- |
| Item | Lean (recommended) | Comfort |
| Cloud storage (R2 or B2) | ₹0 (stay under 10 GB) | ₹25–100 |
| App database, app hosting, alerts | ₹0 | ₹0 |
| Scheduled jobs | ₹0 (GitHub free minutes) | \~₹450–600 (small VPS) |
| **Total** | **about ₹0** | **about ₹500–700** |

One-time costs: Optional external SSD ₹3,000–5,000; paid pre-2017 financials deferred (a retail premium plan was roughly ₹4,000–5,000 a year when I last checked; institutional vendors cost much more).
|  |  |  |
| :- | :- | :- |
| Tier | Sources | Cost |
| 1\. Official and regulatory | NSE and BSE announcements, RBI, SEBI, PIB, Ministry of Finance, FOMC, other central banks, OPEC and EIA releases | Free |
| 2\. Market and business media | Economic Times, Business Standard, Mint, Hindu BusinessLine, Moneycontrol RSS | Free |
| 3\. Geopolitical and global | Free central-bank and statistical-agency feeds | Free |
| 4\. Premium wires and terminals | Reuters, Bloomberg and similar | Not planned; paid and costly relative to a small retail portfolio |

**Defaults adjusted for a small portfolio with a monthly top-up**
  - On a small portfolio a 5% stock cap gives positions so small that the ₹15.34 sale fee alone is about 1% of each one. Defaults change to: stock cap 15%, sector cap 30%, 6–10 holdings, minimum position about ₹3,000.
  - A round trip on a ₹3,500 position costs about ₹23 (0.65%); the sell hurdle is expected edge above about 1% of the position after charges and tax.
  - The monthly top-up goes to one new position or adds to an existing one, once a month.
  - The -12% drawdown pause is shown in rupees for the actual portfolio.
  - Whole shares only, so the engine filters stocks by affordability.
**Economic reality, stated plainly.** Running cost of even ₹150–400 a month is ₹1,800–4,800 a year, a large share of what a realistic 1–3% yearly edge earns on a small portfolio. For the app to pay for itself in returns, running cost must stay near zero, which is why the lean variant keeps all cloud services on free tiers and uses no language models. Treat the build's main value as learning, a reusable data platform, and disciplined decision-making, with returns as a possible bonus rather than the case for the spend.
