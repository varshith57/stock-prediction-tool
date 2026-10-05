"""One-line reasons from a model's top drivers (SHAP contributions), filled into fixed templates.

No generated text: each phrase is a template, and every number in it is the stock's own feature
value from the database, so a reason can always be checked against the data.
"""

from __future__ import annotations

import numpy as np

# feature -> (template using {v}, formatter)
PHRASES: dict[str, tuple[str, str]] = {
    "ret_5": ("{v} over 5 days", "pct"),
    "ret_20": ("{v} over 20 days", "pct"),
    "ret_60": ("{v} over 3 months", "pct"),
    "ret_250": ("{v} over a year", "pct"),
    "rel_ret_20": ("{v} vs the market over 20 days", "pct"),
    "vol_20": ("daily volatility {v}", "pct_abs"),
    "vol_ratio_5_60": ("volatility {v}x its usual level", "x"),
    "atr_14_pct": ("typical daily range {v}", "pct_abs"),
    "max_abs_move_20": ("a {v} single-day move this month", "pct_abs"),
    "dist_52w_high": ("{v} below its 52-week high", "pct_abs"),
    "drawdown_60": ("{v} below its 3-month high", "pct_abs"),
    "sma_gap_50": ("{v} vs its 50-day average", "pct"),
    "sma_gap_200": ("{v} vs its 200-day average", "pct"),
    "rsi_14": ("RSI {v}", "num"),
    "value_ratio_5_20": ("trading activity {v}x its monthly average", "x"),
    "value_ratio_1_60": ("today's turnover {v}x its 3-month average", "x"),
    "delivery_pct": ("delivery {v}%", "num"),
    "delivery_ratio_5_20": ("delivery {v}x its monthly average", "x"),
    "big_move_count_60": ("{v} days with 5%+ moves in 3 months", "int"),
    "gap_freq_20": ("gapped 2%+ on {v} of days this month", "pct_abs"),
    "beta_60": ("beta {v}", "num"),
    "days_since_results": ("results {v} days ago", "int"),
    "results_ahead_days": ("results due in {v} days", "int"),
    "last_results_reaction": ("{v} on its last results", "pct"),
}


def _fmt(v: float, kind: str) -> str:
    if kind == "pct_abs":  # a size, not a direction: "13%", "48% below"
        return f"{abs(v):.0%}" if abs(v) >= 0.01 else f"{abs(v):.1%}"
    if kind == "pct":
        return f"{v:+.0%}" if abs(v) >= 0.01 else f"{v:+.1%}"
    if kind == "x":
        return f"{v:.1f}"
    if kind == "int":
        return f"{v:.0f}"
    return f"{v:.0f}"


def top_drivers(contributions: np.ndarray, features: list[str], k: int = 3) -> list[str]:
    """Features pushing the probability up the most (positive SHAP), with a phrase available."""
    order = np.argsort(-contributions[: len(features)])
    out = [features[i] for i in order if contributions[i] > 0 and features[i] in PHRASES]
    return out[:k]


def reason_line(drivers: list[str], values: dict[str, float | None]) -> str:
    parts = []
    for f in drivers:
        v = values.get(f)
        if v is None:
            continue
        template, kind = PHRASES[f]
        parts.append(template.format(v=_fmt(float(v), kind)))
    return ", ".join(parts) if parts else "no single strong driver"
