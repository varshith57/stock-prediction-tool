"""Point-in-time features and labels (M5).

Rules (PRD section 7; tests in tests/test_m5.py enforce them):

* Features at date T use only rows at or before T.
* Only *scale-free* functions of adjusted prices (returns, ratios, ranks). Backward adjustment
  scales history by factors of events *after* T, so an absolute adjusted price level would leak
  future splits. Raw (as-traded) price is allowed: it was known at T.
* Windows never span a series break (demerger etc.): rolling stats restart after a break.
* Labels (amendment AM2): A = some close in T+1..T+5 at least ``gain`` above the T+1 *open*
  (the price you could actually get); C = some close in T+1..T+5 at least ``crash`` below the T
  close. Null when the window crosses a break, runs past the data, or is stretched by a trading
  halt (more than ``MAX_LABEL_SPAN_DAYS`` calendar days).
"""

from __future__ import annotations

import math

import polars as pl

GROUP = ["company_id", "segment"]
WINDOW = 5  # trading days, from config.signals.window_trading_days
MAX_LABEL_SPAN_DAYS = 14  # for the 5-session window; longer windows scale it (label_span_days)


def label_span_days(window: int) -> int:
    """Most calendar days a label window may span before it counts as stretched by a halt. Also
    the embargo between walk-forward blocks: a label can't see further ahead than this."""
    return max(MAX_LABEL_SPAN_DAYS, -(-window * MAX_LABEL_SPAN_DAYS // WINDOW))


# name -> description (the catalogue; families in FAMILIES)
from stockapp.features.events import FEATURES as _EVENT_FEATURES  # noqa: E402

FEATURES: dict[str, str] = {}
FAMILIES: dict[str, list[str]] = {}


def _register(family: str, **items: str) -> None:
    FAMILIES.setdefault(family, []).extend(items)
    FEATURES.update(items)


_register(
    "returns",
    **{f"ret_{k}": f"Adjusted close-to-close return over {k} sessions"
       for k in (1, 3, 5, 10, 20, 60, 120, 250)},
)  # fmt: skip
_register(
    "volatility",
    vol_5="Std of daily log returns, 5 sessions",
    vol_20="Std of daily log returns, 20 sessions",
    vol_60="Std of daily log returns, 60 sessions",
    vol_ratio_5_60="vol_5 / vol_60",
    atr_14_pct="Average true range (14) / close",
    hl_vol_20="Parkinson high-low volatility, 20 sessions",
    max_abs_move_20="Largest absolute daily log return, 20 sessions",
    big_move_count_60="Days with a move beyond 5%, 60 sessions",
    gap_freq_20="Share of days opening more than 2% away from the prior close, 20 sessions",
)
_register(
    "trend",
    sma_gap_20="Close / 20-session average - 1",
    sma_gap_50="Close / 50-session average - 1",
    sma_gap_200="Close / 200-session average - 1",
    dist_52w_high="Close / 250-session high - 1",
    dist_52w_low="Close / 250-session low - 1",
    drawdown_60="Close / 60-session high - 1",
    rsi_14="Relative strength index, 14 sessions (0-100)",
    up_share_10="Share of up days, 10 sessions",
)
_register(
    "liquidity",
    log_value_20="Log median daily traded value (rupees), 20 sessions",
    value_ratio_5_20="Mean traded value 5 sessions / 20 sessions",
    value_ratio_1_60="Traded value today / 60-session mean",
    log_amihud_20="Log Amihud illiquidity (|return| per rupee traded), 20 sessions",
    log_price="Log raw (as-traded) close: affordability, known at T",
)
_register(
    "delivery",
    delivery_pct="Delivery % today (BE/BZ: 100 by trade-for-trade rule)",
    delivery_5="Mean delivery %, 5 sessions",
    delivery_20="Mean delivery %, 20 sessions",
    delivery_ratio_5_20="delivery_5 / delivery_20",
    is_t2t="Trades in a trade-for-trade series (BE/BZ) today",
)
_register(
    "market",
    mkt_ret_5="Nifty 500 return, 5 sessions",
    mkt_ret_20="Nifty 500 return, 20 sessions",
    mkt_ret_60="Nifty 500 return, 60 sessions",
    mkt_sma_gap_200="Nifty 500 close / 200-session average - 1",
    vix="India VIX close",
    vix_change_5="India VIX change, 5 sessions",
    vix_pct_250="India VIX percentile within the last 250 sessions",
)
_register(
    "relative",
    rel_ret_20="ret_20 minus Nifty 500 return, 20 sessions",
    rel_ret_60="ret_60 minus Nifty 500 return, 60 sessions",
    beta_60="Beta to Nifty 500, 60 sessions",
    idio_vol_60="Volatility not explained by the market, 60 sessions",
)
_register(
    "history",
    sessions_since_break="Sessions since listing or the last series break (capped at 500)",
)
_register(
    "breadth",
    breadth_50="Share of universe-history stocks above their 50-session average (same for all)",
)
_register(
    "cross_section",
    rank_ret_5="Percentile of ret_5 among this week's universe",
    rank_ret_20="Percentile of ret_20 among this week's universe",
    rank_vol_20="Percentile of vol_20 among this week's universe",
    rank_value_20="Percentile of log_value_20 among this week's universe",
    rank_delivery_20="Percentile of delivery_20 among this week's universe",
)
_register("events", **_EVENT_FEATURES)  # computed in features.events (needs results dates)
FEATURE_COLUMNS = list(FEATURES)
CROSS_SECTION = FAMILIES["cross_section"]
EVENTS = FAMILIES["events"]  # added by features.pipeline.add_event_features, not from prices
LABELS = ["label_a", "label_c", "max_gain_5"]


def market_features(index: pl.DataFrame) -> pl.DataFrame:
    """``index``: trade_date, nifty500, vix (closes). One row per session."""
    m = index.sort("trade_date")
    lr = (pl.col("nifty500") / pl.col("nifty500").shift(1)).log()
    return m.select(
        "trade_date",
        lr.alias("_mkt_lr"),
        (pl.col("nifty500") / pl.col("nifty500").shift(5) - 1).alias("mkt_ret_5"),
        (pl.col("nifty500") / pl.col("nifty500").shift(20) - 1).alias("mkt_ret_20"),
        (pl.col("nifty500") / pl.col("nifty500").shift(60) - 1).alias("mkt_ret_60"),
        (pl.col("nifty500") / pl.col("nifty500").rolling_mean(200) - 1).alias("mkt_sma_gap_200"),
        pl.col("vix"),
        (pl.col("vix") / pl.col("vix").shift(5) - 1).alias("vix_change_5"),
        (
            pl.col("vix").rolling_map(
                lambda s: (s < s[-1]).sum() / (len(s) - 1) if len(s) > 1 else None,
                window_size=250,
            )
        ).alias("vix_pct_250"),
    )


def compute_features(panel: pl.DataFrame, market: pl.DataFrame) -> pl.DataFrame:
    """``panel``: one row per company-session with company_id, segment, trade_date, series,
    close (raw), adj_open, adj_high, adj_low, adj_close, value_inr, delivery_pct.
    ``market``: output of ``market_features``. Returns panel keys + FEATURE_COLUMNS (except the
    cross-sectional ranks, which need the weekly universe: see ``add_cross_section``)."""
    p = panel.sort([*GROUP, "trade_date"])

    def g(e: pl.Expr) -> pl.Expr:
        return e.over(GROUP)

    c, o, h, lo = (pl.col(x) for x in ("adj_close", "adj_open", "adj_high", "adj_low"))
    prev_c = g(c.shift(1))
    p = p.with_columns(
        g((c / c.shift(1)).log()).alias("_lr"),
        pl.max_horizontal(h - lo, (h - prev_c).abs(), (lo - prev_c).abs()).alias("_tr"),
        (h / lo).log().pow(2).alias("_hl2"),
        ((o / prev_c - 1).abs() > 0.02).cast(pl.Float64).alias("_gap"),
    ).join(market, on="trade_date", how="left")
    lr, mlr = pl.col("_lr"), pl.col("_mkt_lr")
    p = p.with_columns(
        *[g(c / c.shift(k) - 1).alias(f"ret_{k}") for k in (1, 3, 5, 10, 20, 60, 120, 250)],
        *[g(lr.rolling_std(k)).alias(f"vol_{k}") for k in (5, 20, 60)],
        g(pl.col("_tr").rolling_mean(14) / c).alias("atr_14_pct"),
        g((pl.col("_hl2").rolling_mean(20) / (4 * math.log(2))).sqrt()).alias("hl_vol_20"),
        g(lr.abs().rolling_max(20)).alias("max_abs_move_20"),
        g((lr.abs() > math.log(1.05)).cast(pl.Float64).rolling_sum(60)).alias("big_move_count_60"),
        g(pl.col("_gap").rolling_mean(20)).alias("gap_freq_20"),
        *[g(c / c.rolling_mean(k) - 1).alias(f"sma_gap_{k}") for k in (20, 50, 200)],
        g(c / c.rolling_max(250) - 1).alias("dist_52w_high"),
        g(c / c.rolling_min(250) - 1).alias("dist_52w_low"),
        g(c / c.rolling_max(60) - 1).alias("drawdown_60"),
        g(100 - 100 / (1 + lr.clip(lower_bound=0).rolling_mean(14)
                       / (-lr.clip(upper_bound=0)).rolling_mean(14))).alias("rsi_14"),
        g((lr > 0).cast(pl.Float64).rolling_mean(10)).alias("up_share_10"),
        g(pl.col("value_inr").rolling_median(20).log()).alias("log_value_20"),
        g(pl.col("value_inr").rolling_mean(5) / pl.col("value_inr").rolling_mean(20))
        .alias("value_ratio_5_20"),
        g(pl.col("value_inr") / pl.col("value_inr").rolling_mean(60)).alias("value_ratio_1_60"),
        g((lr.abs() / pl.col("value_inr") * 1e9).rolling_mean(20).log1p()).alias("log_amihud_20"),
        pl.col("close").log().alias("log_price"),
        pl.col("delivery_pct"),
        g(pl.col("delivery_pct").rolling_mean(5)).alias("delivery_5"),
        g(pl.col("delivery_pct").rolling_mean(20)).alias("delivery_20"),
        pl.col("series").is_in(["BE", "BZ"]).cast(pl.Float64).alias("is_t2t"),
        g(pl.int_range(pl.len())).clip(upper_bound=500).cast(pl.Float64)
        .alias("sessions_since_break"),
        g(((lr * mlr).rolling_mean(60) - lr.rolling_mean(60) * mlr.rolling_mean(60))
          / mlr.rolling_var(60)).alias("beta_60"),
    )  # fmt: skip
    p = p.with_columns(
        (pl.col("vol_5") / pl.col("vol_60")).alias("vol_ratio_5_60"),
        (pl.col("delivery_5") / pl.col("delivery_20")).alias("delivery_ratio_5_20"),
        (pl.col("ret_20") - pl.col("mkt_ret_20")).alias("rel_ret_20"),
        (pl.col("ret_60") - pl.col("mkt_ret_60")).alias("rel_ret_60"),
        g(
            (lr.rolling_var(60) - pl.col("beta_60").pow(2) * mlr.rolling_var(60))
            .clip(lower_bound=0)
            .sqrt()
        ).alias("idio_vol_60"),
    )
    breadth = p.group_by("trade_date").agg((pl.col("sma_gap_50") > 0).mean().alias("breadth_50"))
    p = p.join(breadth, on="trade_date", how="left")
    keep = ["company_id", "segment", "trade_date", "series", *FEATURE_COLUMNS]
    return p.select([k for k in keep if k not in CROSS_SECTION and k not in EVENTS]).with_columns(
        pl.col(pl.Float64).replace([float("inf"), float("-inf")], None)
    )


def compute_labels(
    panel: pl.DataFrame, gain: float = 0.10, crash: float = 0.10, window: int = WINDOW
) -> pl.DataFrame:
    p = panel.sort([*GROUP, "trade_date"])
    c = pl.col("adj_close")
    future = [c.shift(-k).over(GROUP) for k in range(1, window + 1)]
    entry = pl.col("adj_open").shift(-1).over(GROUP)
    end_date = pl.col("trade_date").shift(-window).over(GROUP)
    ok = end_date.is_not_null() & ((end_date - pl.col("trade_date")).dt.total_days()
                                   <= label_span_days(window)) & entry.is_not_null()  # fmt: skip
    fut_max, fut_min = pl.max_horizontal(future), pl.min_horizontal(future)
    return p.select(
        "company_id", "segment", "trade_date",
        pl.when(ok).then(fut_max >= entry * (1 + gain)).alias("label_a"),
        pl.when(ok).then(fut_min <= c * (1 - crash)).alias("label_c"),
        pl.when(ok).then(fut_max / entry - 1).alias("max_gain_5"),
    )  # fmt: skip


def add_cross_section(samples: pl.DataFrame) -> pl.DataFrame:
    """Percentile ranks within each signal date's sample (the universe that week)."""
    pairs = {"rank_ret_5": "ret_5", "rank_ret_20": "ret_20", "rank_vol_20": "vol_20",
             "rank_value_20": "log_value_20", "rank_delivery_20": "delivery_20"}  # fmt: skip
    return samples.with_columns(
        (pl.col(src).rank("average") / pl.col(src).count()).over("trade_date").alias(dst)
        for dst, src in pairs.items()
    )
