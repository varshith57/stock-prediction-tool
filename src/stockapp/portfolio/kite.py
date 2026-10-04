"""Import holdings from a Zerodha Kite / Console holdings CSV.

The exact export layout isn't assumed: columns are matched by common names (case and punctuation
ignored), e.g. ``Instrument``/``Symbol``, ``Qty.``/``Quantity``/``Quantity Available``,
``Avg. cost``/``Average Price``, optional ``ISIN``. If a field is missing or matched by more than
one column, the import is refused with the columns found, rather than guessed. Every parsed row
is shown for confirmation before anything is saved, and rows that can't be matched to an NSE
company are listed, not dropped silently.

Imported holdings have no buy date (the export doesn't carry one): their holding period, and so
short- vs long-term tax, stays "unknown" until you edit the date.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

import polars as pl

ALIASES = {
    "symbol": ("instrument", "symbol", "tradingsymbol", "trading symbol", "scrip"),
    "quantity": ("qty", "quantity", "quantity available", "qty available", "net qty"),
    "avg_price": ("avg cost", "average cost", "average price", "avg price", "buy average"),
    "isin": ("isin",),
}
REQUIRED = ("symbol", "quantity", "avg_price")
_SERIES_SUFFIX = re.compile(r"-(EQ|BE|BZ)$")


class ImportFormatError(ValueError):
    pass


def _norm(name: str) -> str:
    return re.sub(r"[^a-z ]", "", name.lower()).strip()


@dataclass(frozen=True)
class ParsedHoldings:
    rows: pl.DataFrame  # symbol, quantity, avg_price, isin
    column_map: dict[str, str]
    problems: list[str]


def parse_holdings_csv(content: bytes) -> ParsedHoldings:
    try:
        raw = pl.read_csv(io.BytesIO(content), infer_schema=False, truncate_ragged_lines=True)
    except Exception as exc:
        raise ImportFormatError(f"not a readable CSV: {exc}") from None
    normalized = {c: _norm(c) for c in raw.columns}
    column_map: dict[str, str] = {}
    for field_name, aliases in ALIASES.items():
        hits = [c for c, n in normalized.items() if n in aliases]
        if len(hits) > 1:
            raise ImportFormatError(
                f"more than one column could be '{field_name}': {hits}. Columns: {raw.columns}"
            )
        if hits:
            column_map[field_name] = hits[0]
    missing = [f for f in REQUIRED if f not in column_map]
    if missing:
        raise ImportFormatError(f"missing {missing}. Columns found: {raw.columns}")

    def num(col: str) -> pl.Expr:
        return pl.col(col).str.replace_all(r"[,\s₹]", "").cast(pl.Float64, strict=False)

    df = raw.select(
        pl.col(column_map["symbol"]).str.strip_chars().str.to_uppercase().alias("symbol_raw"),
        num(column_map["quantity"]).alias("quantity"),
        num(column_map["avg_price"]).alias("avg_price"),
        (pl.col(column_map["isin"]).str.strip_chars() if "isin" in column_map else pl.lit(None))
        .cast(pl.String)
        .alias("isin"),
    ).with_columns(pl.col("symbol_raw").str.replace(_SERIES_SUFFIX.pattern, "").alias("symbol"))

    problems: list[str] = []
    is_bad = (
        pl.col("symbol").is_null()
        | pl.col("quantity").is_null()
        | pl.col("avg_price").is_null()
        | (pl.col("quantity") <= 0)
        | (pl.col("quantity") != pl.col("quantity").floor())
        | (pl.col("avg_price") <= 0)
    ).fill_null(True)
    bad = df.filter(is_bad)
    for r in bad.iter_rows(named=True):
        problems.append(
            f"row skipped: {r['symbol_raw']!r} qty={r['quantity']} avg={r['avg_price']} "
            "(needs a symbol, a whole positive quantity and a positive average price)"
        )
    good = df.filter(~is_bad).with_columns(pl.col("quantity").cast(pl.Int64))
    dupes = good.group_by("symbol").len().filter(pl.col("len") > 1)
    if dupes.height:
        problems.append(
            f"symbols listed more than once (kept as separate lots): {dupes['symbol'].to_list()}"
        )
    return ParsedHoldings(
        good.select("symbol", "quantity", "avg_price", "isin"), column_map, problems
    )
