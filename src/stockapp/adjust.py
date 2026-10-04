"""Corporate-action adjustment: events, factors, series breaks, adjusted prices, continuity test.

Raw prices are never edited. Adjusted prices are computed on read (``adjusted_prices_sql``) from
raw prices x the product of factors for every event *after* each date (backward adjustment, so the
latest prices equal raw prices). Two factor sets:

* ``price_factor``: bonus, split, consolidation, rights. For comparing prices and volumes.
* ``dividend_factor``: (prior close - dividend) / prior close. Combined with the price factor it
  gives a total-return series.

Events that can't be adjusted (demerger, amalgamation, capital reduction, bonus debentures, odd
rights) are written as series breaks; features must not compute returns across them.

The continuity test checks every price-factor event: after adjustment, the move from the last
close before the ex-date to the first close on/after it must look like a normal day (|move| within
the 20% band plus two ticks). Events on the same day as a series break are skipped (the break
explains the move). Failures are reported, and the company's adjusted history before the event is
blocked until resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import duckdb
import polars as pl

from stockapp.actions import parse_subject, rights_factor
from stockapp.ingest.prices import MAIN_BOARD_SERIES
from stockapp.lake import Lake
from stockapp.master import company_map_sql, company_prices_sql

EVENTS, FACTORS, BREAKS = "corporate_action_events", "adjustment_factors", "series_breaks"
CONTINUITY_MAX_MOVE = 0.20  # the widest NSE price band for most stocks
CONTINUITY_TICK_SLACK = 0.10  # rupees: two ticks, so a stock closing at its +20% band still passes
PRICE_KINDS = ("bonus", "split", "consolidation", "rights")


def _face_value(raw: str | None) -> float | None:
    try:
        return float(raw) if raw not in (None, "", "-") else None
    except ValueError:
        return None


def build_events(lake: Lake) -> pl.DataFrame:
    """Parse every corporate action (deduplicated) and map it to a company at its ex-date."""
    actions = (
        lake.scan("silver", "nse_corporate_actions")
        .filter(pl.col("ex_date").is_not_null())
        .filter(pl.col("series").is_in([*MAIN_BOARD_SERIES, "", "-"]) | pl.col("series").is_null())
        .select("symbol", "series", "isin", "ex_date", "subject", "face_value")
        .unique(subset=["symbol", "ex_date", "subject"])
        .collect()
    )
    rows = []
    for a in actions.iter_rows(named=True):
        fv = _face_value(a["face_value"])
        for p in parse_subject(a["subject"], fv):
            if p.status == "no_price_effect":
                continue
            rows.append(
                {
                    "symbol": a["symbol"], "ex_date": a["ex_date"], "subject": a["subject"],
                    "face_value": fv, "kind": p.kind, "status": p.status,
                    "component": p.component, "ratio_new": p.ratio_new,
                    "ratio_held": p.ratio_held, "from_fv": p.from_fv, "to_fv": p.to_fv,
                    "rights_premium": p.rights_premium,
                    "dividend_per_share": p.dividend_per_share, "price_factor": p.price_factor,
                }
            )  # fmt: skip
    events = pl.DataFrame(rows, infer_schema_length=None)
    con = duckdb.connect()
    con.register("events", events)
    # NSE files historical actions under the company's *current* symbol (United Spirits' 2018 split
    # appears as UNITDSPR although it traded as MCDOWELL-N then). So: the symbol as of the ex-date
    # if that segment exists, else the company that holds the symbol now. Unmatched events are
    # returned with a null company_id, never dropped silently.
    return con.sql(
        f"""
        WITH m AS ({company_map_sql(lake)}),
        current_holder AS (
            SELECT symbol, company_id FROM m WHERE valid_to = DATE '9999-12-31'
        )
        SELECT coalesce(at_date.company_id, cur.company_id) AS company_id,
               CASE WHEN at_date.company_id IS NOT NULL THEN 'symbol_at_ex_date'
                    WHEN cur.company_id IS NOT NULL THEN 'current_symbol' END AS mapped_by,
               e.*
        FROM events e
        LEFT JOIN m at_date
          ON at_date.symbol = e.symbol AND e.ex_date BETWEEN at_date.valid_from AND at_date.valid_to
        LEFT JOIN current_holder cur ON cur.symbol = e.symbol
        """
    ).pl()


def _prior_closes(lake: Lake, keys: pl.DataFrame) -> pl.DataFrame:
    """Last close strictly before each (company_id, ex_date)."""
    con = duckdb.connect()
    con.register("k", keys.select("company_id", "ex_date").unique())
    return con.sql(
        f"""SELECT k.company_id, k.ex_date, p.close AS prior_close, p.trade_date AS prior_date
            FROM k ASOF LEFT JOIN ({company_prices_sql(lake)}) p
              ON p.company_id = k.company_id AND p.trade_date < k.ex_date"""
    ).pl()


@dataclass(frozen=True)
class AdjustmentBuild:
    events: pl.DataFrame
    factors: pl.DataFrame
    breaks: pl.DataFrame
    skipped: pl.DataFrame  # parsed events that couldn't produce a factor, with the reason
    unmapped: pl.DataFrame  # events whose symbol matches no traded main-board company


def build_adjustments(lake: Lake, as_of: date) -> AdjustmentBuild:
    all_events = build_events(lake)
    unmapped = all_events.filter(pl.col("company_id").is_null())
    events = all_events.filter(pl.col("company_id").is_not_null())
    prior = _prior_closes(lake, events)
    ev = events.join(prior, on=["company_id", "ex_date"], how="left")

    skipped: list[dict] = []
    factor_rows: list[dict] = []
    for e in ev.filter(pl.col("status") == "parsed").iter_rows(named=True):
        price_f, div_f, why = 1.0, 1.0, None
        if e["kind"] in ("bonus", "split", "consolidation"):
            price_f = e["price_factor"]
        elif e["kind"] == "rights":
            if e["prior_close"] is None or e["face_value"] is None:
                why = "rights without a prior close or face value"
            else:
                issue = e["face_value"] + (e["rights_premium"] or 0.0)
                price_f = rights_factor(e["prior_close"], e["ratio_new"], e["ratio_held"], issue)
        elif e["kind"] == "dividend":
            d, p = e["dividend_per_share"], e["prior_close"]
            if p is None:
                why = "dividend without a prior close"
            elif not 0 < d < p:
                why = f"dividend {d} not below prior close {p}"
            else:
                div_f = (p - d) / p
        if why:
            skipped.append({"company_id": e["company_id"], "ex_date": e["ex_date"],
                            "kind": e["kind"], "subject": e["subject"], "reason": why})  # fmt: skip
            continue
        factor_rows.append(
            {
                "company_id": e["company_id"],
                "ex_date": e["ex_date"],
                "kind": e["kind"],
                "price_factor": price_f,
                "dividend_factor": div_f,
            }
        )

    factors = (
        pl.DataFrame(factor_rows)
        .group_by("company_id", "ex_date")
        .agg(
            pl.col("price_factor").product(),
            pl.col("dividend_factor").product(),
            pl.col("kind").unique().sort().str.join(",").alias("kinds"),
        )
        .sort("company_id", "ex_date")
    )
    breaks = (
        ev.filter(pl.col("status") == "unadjustable")
        .select("company_id", pl.col("ex_date").alias("break_date"), "kind", "subject")
        .unique()
        .sort("company_id", "break_date")
    )
    key = as_of.isoformat()
    lake.write_partition("silver", EVENTS, "built", key, events)
    lake.write_partition("silver", FACTORS, "built", key, factors)
    lake.write_partition("silver", BREAKS, "built", key, breaks)
    skipped_df = pl.DataFrame(
        skipped, schema={"company_id": pl.String, "ex_date": pl.Date, "kind": pl.String,
                         "subject": pl.String, "reason": pl.String},
    )  # fmt: skip
    return AdjustmentBuild(events, factors, breaks, skipped_df, unmapped)


def _latest(lake: Lake, dataset: str) -> str:
    glob = lake.duckdb_glob("silver", dataset)
    return (
        f"SELECT * FROM read_parquet('{glob}', hive_partitioning = true) "
        f"WHERE built = (SELECT max(built) FROM read_parquet('{glob}', hive_partitioning = true))"
    )


def adjusted_prices_sql(lake: Lake) -> str:
    """Company prices with ``adj_factor`` (splits/bonuses/rights) and ``tr_factor`` (plus
    dividends), and adjusted OHLC, volume and total-return close."""
    return f"""
    WITH c AS (
        SELECT company_id, ex_date,
               exp(sum(ln(price_factor)) OVER w) AS cum_price,
               exp(sum(ln(price_factor * dividend_factor)) OVER w) AS cum_total
        FROM ({_latest(lake, FACTORS)})
        WINDOW w AS (PARTITION BY company_id ORDER BY ex_date DESC
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
    )
    SELECT p.*,
           coalesce(c.cum_price, 1.0) AS adj_factor,
           coalesce(c.cum_total, 1.0) AS tr_factor,
           p.open * coalesce(c.cum_price, 1.0) AS adj_open,
           p.high * coalesce(c.cum_price, 1.0) AS adj_high,
           p.low * coalesce(c.cum_price, 1.0) AS adj_low,
           p.close * coalesce(c.cum_price, 1.0) AS adj_close,
           p.volume / coalesce(c.cum_price, 1.0) AS adj_volume,
           p.close * coalesce(c.cum_total, 1.0) AS tr_close
    FROM ({company_prices_sql(lake)}) p
    ASOF LEFT JOIN c ON c.company_id = p.company_id AND p.trade_date < c.ex_date
    """


def continuity_check(lake: Lake, max_gap_days: int = 15) -> pl.DataFrame:
    """For every price-factor event: raw and adjusted move from the last close before the ex-date
    to the first close on/after it. ``passed`` = |adjusted move| <= CONTINUITY_MAX_MOVE."""
    return duckdb.sql(
        f"""
        WITH f AS (SELECT * FROM ({_latest(lake, FACTORS)}) WHERE abs(price_factor - 1) > 1e-9),
        p AS ({company_prices_sql(lake)}),
        before AS (SELECT f.*, p.close AS close_before, p.trade_date AS date_before
                   FROM f ASOF LEFT JOIN p ON p.company_id = f.company_id
                                           AND p.trade_date < f.ex_date),
        after AS (SELECT b.*, p.close AS close_after, p.trade_date AS date_after
                  FROM before b ASOF LEFT JOIN p ON p.company_id = b.company_id
                                                 AND p.trade_date >= b.ex_date)
        SELECT a.company_id, a.ex_date, kinds, price_factor, date_before, close_before, date_after,
               close_after,
               close_after / close_before - 1 AS raw_move,
               close_after / (close_before * price_factor) - 1 AS adjusted_move,
               abs(close_after / (close_before * price_factor) - 1)
                   <= {CONTINUITY_MAX_MOVE}
                      + {CONTINUITY_TICK_SLACK} / (close_before * price_factor) AS passed
        FROM after a
        WHERE close_before IS NOT NULL AND close_after IS NOT NULL
          AND date_after - date_before <= {max_gap_days}
          -- a series break on the same day (e.g. a demerger) explains the move by itself
          AND NOT EXISTS (SELECT 1 FROM ({_latest(lake, BREAKS)}) b
                          WHERE b.company_id = a.company_id AND b.break_date = a.ex_date)
        ORDER BY a.company_id, a.ex_date
        """
    ).pl()
