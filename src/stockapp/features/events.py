"""Results-date features (the "events" family), point in time.

A weekly decision is made on the signal date T after the close; the weekly job runs at 20:00 IST,
so anything published or announced by T 20:00 counts as known (``DECISION_HOUR``).

* ``days_since_results``: calendar days since the company's last quarterly results were
  published (silver ``nse_financial_results``, ``published_at``), capped at 200.
* ``results_ahead_days``: calendar days until the next results board meeting that had been
  *announced* by then (silver ``nse_board_meetings``, ``announced_at``), capped at 45; null when
  none is known. Using the announcement time keeps unannounced dates out (no peeking).
* ``results_this_week``: 1 when that meeting falls within the next 7 calendar days, else 0.
* ``last_results_reaction``: the stock's return on the first session that could react to its last
  results (the publication day if published before 15:30, else the next session), known only
  once that session has closed by T.

Callers map rows to ``company_id`` first: board meetings and the pre-2025 results feed by ISIN
(every ISIN a company has traded under), integrated filings (2025 on, no ISIN) by symbol as of the
publication date (``map_isin``, ``map_symbol``).
"""

from __future__ import annotations

from datetime import time, timedelta

import polars as pl

DECISION_HOUR = time(20, 0)
MARKET_CLOSE = time(15, 30)
FEATURES = {
    "days_since_results": "Calendar days since the last quarterly results were published (cap 200)",
    "results_ahead_days": "Calendar days to the next announced results meeting (cap 45, or null)",
    "results_this_week": "1 if an announced results meeting falls within the next 7 days",
    "last_results_reaction": "Return on the first session after the last results were published",
}  # fmt: skip


def map_isin(df: pl.DataFrame, isin_map: pl.DataFrame) -> pl.DataFrame:
    """Rows with ``isin`` -> + ``company_id`` (``isin_map``: isin, company_id)."""
    return df.with_columns(pl.col("isin").cast(pl.String)).join(isin_map, on="isin", how="inner")


def map_company(
    df: pl.DataFrame, isin_map: pl.DataFrame, symbol_map: pl.DataFrame, date_col: str
) -> pl.DataFrame:
    """ISIN first; rows whose ISIN the price data never saw (a pre-split ISIN, or a bond ISIN
    on a results filing) fall back to the symbol valid on ``date_col``."""
    df = df.with_row_index("_row")
    by_isin = map_isin(df, isin_map)
    rest = df.join(by_isin.select("_row"), on="_row", how="anti")
    by_symbol = map_symbol(rest, symbol_map, date_col)
    return pl.concat([by_isin, by_symbol], how="vertical_relaxed").drop("_row")


def map_symbol(df: pl.DataFrame, symbol_map: pl.DataFrame, date_col: str) -> pl.DataFrame:
    """Rows with ``symbol`` -> + ``company_id``, using the symbol valid on ``date_col``
    (``symbol_map``: company_id, symbol, valid_from, valid_to)."""
    d = pl.col(date_col).cast(pl.Date)
    return (
        df.join(symbol_map.select("company_id", "symbol", "valid_from", "valid_to"), on="symbol")
        .filter((d >= pl.col("valid_from")) & (d <= pl.col("valid_to")))
        .drop("valid_from", "valid_to")
    )


def event_features(
    samples: pl.DataFrame,
    meetings: pl.DataFrame,
    filings: pl.DataFrame,
    closes: pl.DataFrame,
) -> pl.DataFrame:
    """``samples``: company_id, trade_date. ``meetings``: company_id, meeting_date, announced_at,
    is_results. ``filings``: company_id, period_to, published_at. ``closes``: company_id,
    trade_date, adj_close (all sessions). Returns samples + the 4 features."""
    base = (
        samples.select("company_id", "trade_date")
        .unique()
        .with_columns(
            (
                pl.col("trade_date").cast(pl.Datetime("us")) + pl.duration(hours=DECISION_HOUR.hour)
            ).alias("_known_by")
        )
    )

    # last published results (one row per company and period: the first publication)
    filings = filings.cast({"period_to": pl.Date, "published_at": pl.Datetime("us")})
    meetings = meetings.cast(
        {"meeting_date": pl.Date, "announced_at": pl.Datetime("us"), "is_results": pl.Boolean}
    )
    pubs = (
        filings.filter(pl.col("published_at").is_not_null())
        .group_by("company_id", "period_to")
        .agg(pl.col("published_at").min().cast(pl.Datetime("us")))
        .sort("published_at")
    )
    last_pub = (
        base.sort("_known_by")
        .join_asof(
            pubs.select("company_id", "published_at"),
            left_on="_known_by",
            right_on="published_at",
            by="company_id",
            strategy="backward",
            check_sortedness=False,
        )
        .select(
            "company_id",
            "trade_date",
            ((pl.col("trade_date") - pl.col("published_at").dt.date()).dt.total_days())
            .clip(upper_bound=200)
            .cast(pl.Float64)
            .alias("days_since_results"),
        )
    )

    # reaction: the first session that could trade on the news, against the session before it
    c = closes.select("company_id", "trade_date", "adj_close").sort("company_id", "trade_date")
    c = c.with_columns(
        (pl.col("adj_close") / pl.col("adj_close").shift(1).over("company_id") - 1).alias("_ret")
    )
    react_from = pubs.with_columns(
        pl.when(pl.col("published_at").dt.time() <= MARKET_CLOSE)
        .then(pl.col("published_at").dt.date())
        .otherwise(pl.col("published_at").dt.date() + timedelta(days=1))
        .alias("_from")
    ).sort("_from")
    reactions = (
        react_from.join_asof(
            c.sort("trade_date"),
            left_on="_from",
            right_on="trade_date",
            by="company_id",
            strategy="forward",
            check_sortedness=False,
        )
        .filter(pl.col("trade_date").is_not_null())
        .select("company_id", pl.col("trade_date").alias("reaction_day"), "_ret")
        .sort("reaction_day")
    )
    last_reaction = (
        base.sort("trade_date")
        .join_asof(
            reactions,
            left_on="trade_date",
            right_on="reaction_day",
            by="company_id",
            strategy="backward",
            check_sortedness=False,
        )
        .select("company_id", "trade_date", pl.col("_ret").alias("last_results_reaction"))
    )

    # next announced results meeting
    ahead = (
        base.join(
            meetings.filter(pl.col("is_results")).select(
                "company_id", "meeting_date", pl.col("announced_at").cast(pl.Datetime("us"))
            ),
            on="company_id",
            how="inner",
        )
        .filter(
            (pl.col("announced_at") <= pl.col("_known_by"))
            & (pl.col("meeting_date") > pl.col("trade_date"))
        )
        .group_by("company_id", "trade_date")
        .agg(pl.col("meeting_date").min())
        .with_columns(
            (pl.col("meeting_date") - pl.col("trade_date"))
            .dt.total_days()
            .clip(upper_bound=45)
            .cast(pl.Float64)
            .alias("results_ahead_days")
        )
        .drop("meeting_date")
    )

    out = (
        samples.join(last_pub, on=["company_id", "trade_date"], how="left")
        .join(ahead, on=["company_id", "trade_date"], how="left")
        .join(last_reaction, on=["company_id", "trade_date"], how="left")
        .with_columns(
            (pl.col("results_ahead_days") <= 7)
            .fill_null(False)
            .cast(pl.Float64)
            .alias("results_this_week")
        )
    )
    return out
