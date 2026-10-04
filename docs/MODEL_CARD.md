# Model card: signals A and C (v1)

**Status (2026-10-04): both signals OFF.** Neither meets the 90% precision gate, so the app shows
no A or C signals; exits on holdings come from deterministic rules only.

## What the models do
- **A (opportunity):** probability that some close in the next 5 trading days is at least 10%
  above the next session's open (the price you could actually get).
- **C (crash):** probability that some close in the next 5 trading days is at least 10% below the
  signal-day close.
- **Expected gain:** median of the 5-day maximum gain (quantile model), used only to rank A.

## Data and method
- Weekly samples, Aug 2016 to Oct 2026: the last session of each week x that month's
  point-in-time top-500-by-turnover universe (equities only, delisted names included).
- 53 point-in-time features (returns, volatility, trend, liquidity, delivery, market regime,
  relative strength, breadth, cross-sectional ranks). Leakage gate: truncation invariance,
  random-walk no-edge, and real-data shuffled-label AUC 0.50.
- LightGBM per signal; walk-forward by quarter from 2018 (35 folds), expanding window,
  10-day purge/embargo, isotonic calibration on the 4 quarters before each test quarter.

## Out-of-sample results (walk-forward, 2018-2026)

| | A | C |
|---|---|---|
| Base rate | 6.2% | 4.7% |
| Model AUC (volatility baseline / linear) | 0.706 (0.637 / 0.702) | 0.681 (0.661 / 0.720) |
| Precision of the week's top 5 (volatility baseline) | 22.6% (15.3%) | 23.9% (17.7%) |
| Best precision with 30+ signals | 43% | 65% |
| Gate (90%, n >= 30, Wilson lower bound >= 80%) | OFF | OFF |

## Known limits
- **Overconfident tails:** above a stated 30% (A) or in the 10-30% band (C), observed rates are
  lower than stated. Isotonic calibration has few high-probability examples to learn from.
- **C in 2020:** worse than random (AUC 0.46). The COVID crash wasn't foreseeable from these
  features; crash risk is regime-driven.
- **C:** the linear baseline ranks better than LightGBM overall; an ensemble is a candidate.
- No fundamentals, ownership, flows or event flags yet (P1/P2); most of the edge is volatility
  and liquidity.

## Use
Decision support only, personal use, not investment advice. Re-evaluated on every retrain; a
signal goes LIVE only by passing the gate, never by lowering it.
