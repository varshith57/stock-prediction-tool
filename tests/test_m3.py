"""M3: corporate-action parsing, company master, adjustments, quality gates and score."""

from __future__ import annotations

import json
from datetime import date, timedelta

import duckdb
import polars as pl
import pytest
from conftest import build_master

from stockapp.actions import parse_subject, rights_factor
from stockapp.adjust import adjusted_prices_sql, build_adjustments, continuity_check
from stockapp.lake import Lake
from stockapp.master import build_symbol_map
from stockapp.quality.gates import build_price_flags
from stockapp.quality.score import build_quality_scores
from stockapp.universe import build_universe

# parser -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Bonus 1:2", [("bonus", "parsed", 2 / 3, None)]),
        ("Bonus 2:1/Dividend- Rs 1.60 Per Share",
         [("bonus", "parsed", 1 / 3, None), ("dividend", "parsed", None, 1.6)]),
        ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
         [("split", "parsed", 0.2, None)]),
        ("Fv Splt Frm Rs 10 To Re 1", [("split", "parsed", 0.1, None)]),
        ("Interim Div - Rs 6/- Per Share + Face Value Split (Sub-Division) - From Rs 10/- Per Share"
         " To Re 1/- Per Share",
         [("dividend", "parsed", None, 6.0), ("split", "parsed", 0.1, None)]),
        ("Capital Reduction Rs 10 To Rs 1 / Consolidation Rs 1 To Rs.10",
         [("capital_reduction", "unadjustable", None, None),
          ("consolidation", "parsed", 10.0, None)]),
        ("Rights 7:10 @ Prm Rs 102/-", [("rights", "parsed", None, None)]),
        ("Rights 2:7", [("rights", "unadjustable", None, None)]),  # no issue price: don't guess
        ("Rights:14 Compulosry Convertible Debentures For Every 15 Equity Shares",
         [("rights", "unadjustable", None, None)]),
        ("Scheme Of Arrangement- Bonus - 1 Debenture For 1 Equity Share Held",
         [("bonus_debenture", "unadjustable", None, None)]),
        ("Demerger", [("demerger", "unadjustable", None, None)]),
        ("Scheme Of Arrangement", [("restructuring", "unadjustable", None, None)]),
        ("Dividend - 50%", [("dividend", "parsed", None, 5.0)]),  # % of face value 10
        ("Dividend Rs - 2 Per Share", [("dividend", "parsed", None, 2.0)]),
        ("Interim Dividend", [("dividend", "unparsed", None, None)]),
        ("Annual General Meeting", [("other", "no_price_effect", None, None)]),
        ("Bonus Issue", [("bonus", "unparsed", None, None)]),
    ],
)  # fmt: skip
def test_parse_subject(subject: str, expected: list[tuple]):
    got = [
        (p.kind, p.status, p.price_factor, p.dividend_per_share)
        for p in parse_subject(subject, 10.0)
    ]
    assert len(got) == len(expected)
    for (k, s, f, d), (ek, es, ef, ed) in zip(got, expected, strict=True):
        assert (k, s) == (ek, es)
        assert f == pytest.approx(ef) if ef is not None else f is None
        assert d == pytest.approx(ed) if ed is not None else d is None


def test_rights_factor_terp_and_cap():
    # 1 new for every 4 held at 60 when the stock is at 100: TERP = (4*100 + 60) / 5 = 92
    assert rights_factor(100, 1, 4, 60) == pytest.approx(0.92)
    assert rights_factor(100, 1, 4, 150) == 1.0  # rights above market transfer no value


# company master ---------------------------------------------------------------------------------


def test_symbol_map_follows_renames_and_separates_reused_symbols():
    observed = pl.DataFrame(
        {
            "symbol": ["KPIT", "BSOFT", "ZOMATO", "ETERNAL", "PLAIN"],
            "first_seen": [date(2016, 1, 1), date(2019, 2, 26), date(2021, 7, 23),
                           date(2025, 4, 9), date(2016, 1, 1)],
            "last_seen": [date(2026, 1, 1), date(2026, 1, 1), date(2025, 4, 8),
                          date(2026, 1, 1), date(2026, 1, 1)],
        }
    )  # fmt: skip
    changes = pl.DataFrame(
        {
            "old_symbol": ["KPIT", "ZOMATO", "MFUNIT"],
            "new_symbol": ["BSOFT", "ETERNAL", "MFUNIT2"],  # MFUNIT never traded: ignored
            "change_date": [date(2019, 2, 26), date(2025, 4, 9), date(2020, 1, 1)],
        }
    )
    m = build_symbol_map(observed, changes)
    company = {(r["symbol"], r["valid_from"]): r["company_id"] for r in m.iter_rows(named=True)}
    assert company[("ZOMATO", date(1900, 1, 1))] == "ETERNAL"
    assert company[("ETERNAL", date(2025, 4, 9))] == "ETERNAL"
    assert company[("KPIT", date(1900, 1, 1))] == "BSOFT"  # old KPIT became Birlasoft
    assert company[("KPIT", date(2019, 2, 26))] == "KPIT"  # the symbol reused later is separate
    assert company[("PLAIN", date(1900, 1, 1))] == "PLAIN"
    assert "MFUNIT" not in m["symbol"].to_list()


# end to end on a synthetic lake ------------------------------------------------------------------


def _sessions(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _sessions(date(2024, 1, 1), 60)
SPLIT_DAY, RENAME_DAY, HALF_DAY, BONUS_DAY, BAD_DAY = (
    DAYS[20],
    DAYS[30],
    DAYS[25],
    DAYS[40],
    DAYS[45],
)


def _price(sym: str, i: int, d: date) -> float:
    base = {"ALPHA": 500.0, "BETA": 200.0, "GAMMA": 100.0, "DELTA": 50.0, "EPS": 80.0}[sym]
    p = base * (1 + 0.001 * (i % 5))
    if sym == "ALPHA" and d >= SPLIT_DAY:
        p /= 5  # split 10 -> 2, filed under the *current* symbol ALPHANEW
    if sym == "BETA" and d >= HALF_DAY:
        p /= 2  # unexplained halving: a missing action
    return p  # GAMMA has a bonus 1:1 that never shows in the price


def _write_lake(lake: Lake) -> None:
    for i, d in enumerate(DAYS):
        rows = []
        for sym in ("ALPHA", "BETA", "GAMMA", "DELTA", "EPS"):
            symbol = "ALPHANEW" if sym == "ALPHA" and d >= RENAME_DAY else sym
            c = _price(sym, i, d)
            hi, lo = c * 1.01, c * 0.99
            if sym == "DELTA" and d == BAD_DAY:
                hi, lo = lo, hi  # high below low
            rows.append((symbol, c, hi, lo))
        n = len(rows)
        df = pl.DataFrame(
            {
                "trade_date": [d] * n,
                "symbol": [r[0] for r in rows],
                "series": ["EQ"] * n,
                "isin": [f"INE{k:03d}A01010" for k in range(n)],
                "open": [r[1] for r in rows],
                "high": [r[2] for r in rows],
                "low": [r[3] for r in rows],
                "close": [r[1] for r in rows],
                "last": [r[1] for r in rows],
                "prev_close": [r[1] for r in rows],
                "volume": [1000] * n,
                "value_inr": [1000 * r[1] for r in rows],
                "trades": [10] * n,
            }
        )
        lake.write_partition("silver", "nse_cm_bhavcopy_legacy", "trade_date", d.isoformat(), df)
        mto = df.select(
            "trade_date", "symbol", "series", pl.col("volume").alias("qty_traded"),
            (pl.col("volume") // 2).alias("deliverable_qty"), pl.lit(50.0).alias("delivery_pct"),
        )  # fmt: skip
        lake.write_partition("silver", "nse_cm_delivery", "trade_date", d.isoformat(), mto)
        idx = pl.DataFrame(
            {"index_name": ["Nifty 50", "Nifty 500", "India VIX"], "trade_date": [d] * 3,
             "close": [22000.0, 21000.0, 14.0]}
        )  # fmt: skip
        lake.write_partition("silver", "nse_index_close", "trade_date", d.isoformat(), idx)

    actions = [
        ("ALPHANEW", SPLIT_DAY,
         "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"),
        ("GAMMA", BONUS_DAY, "Bonus 1:1"),
        ("EPS", DAYS[50], "Face Value Split Somehow"),  # price-affecting but unparseable
        ("EPS", DAYS[10], "Interim Dividend"),  # no amount: unparsed dividend (WARN)
    ]  # fmt: skip
    for month in sorted(
        {a[1].strftime("%Y-%m") for a in actions} | {d.strftime("%Y-%m") for d in DAYS}
    ):
        rows = [a for a in actions if a[1].strftime("%Y-%m") == month]
        df = pl.DataFrame(
            {
                "symbol": [a[0] for a in rows], "series": ["EQ"] * len(rows),
                "isin": ["INE000A01010"] * len(rows), "company": ["X"] * len(rows),
                "face_value": ["10"] * len(rows), "subject": [a[2] for a in rows],
                "ex_date": [a[1] for a in rows],
            },
            schema={"symbol": pl.String, "series": pl.String, "isin": pl.String,
                    "company": pl.String, "face_value": pl.String, "subject": pl.String,
                    "ex_date": pl.Date},
        )  # fmt: skip
        lake.write_partition("silver", "nse_corporate_actions", "month", month, df)


@pytest.fixture
def m3_lake(lake: Lake) -> Lake:
    _write_lake(lake)
    build_master(lake, [("ALPHA", "ALPHANEW", RENAME_DAY.isoformat())])
    build_adjustments(lake, date(2026, 10, 4))
    return lake


def test_split_filed_under_current_symbol_is_applied_and_reconciles(m3_lake: Lake):
    c = continuity_check(m3_lake)
    alpha = c.filter(pl.col("company_id") == "ALPHANEW").row(0, named=True)
    assert alpha["price_factor"] == pytest.approx(0.2) and alpha["passed"]
    adj = duckdb.sql(
        f"SELECT trade_date, adj_close FROM ({adjusted_prices_sql(m3_lake)}) "
        "WHERE company_id = 'ALPHANEW' ORDER BY 1"
    ).pl()
    moves = (adj["adj_close"] / adj["adj_close"].shift(1) - 1).drop_nulls().abs()
    assert moves.max() < 0.01  # one continuous series across the split and the rename
    gamma = c.filter(pl.col("company_id") == "GAMMA").row(0, named=True)
    assert not gamma["passed"]  # a bonus the price never reflected


def test_quality_flags(m3_lake: Lake):
    build_universe(m3_lake, size=10, window=5)
    flags = build_price_flags(m3_lake, date(2026, 10, 4))
    by = {(r["company_id"], r["check"]): r for r in flags.iter_rows(named=True)}

    beta = by[("BETA", "possible_missing_action")]
    assert beta["severity"] == "BLOCK" and beta["to_date"] == HALF_DAY - timedelta(days=1)
    assert json.loads(beta["detail"])["raw_ratio"] == pytest.approx(0.5, rel=0.01)
    gamma = by[("GAMMA", "unreconciled_adjustment")]
    assert (gamma["from_date"], gamma["to_date"]) == (DAYS[0], BONUS_DAY - timedelta(days=1))
    assert by[("DELTA", "ohlc_integrity")]["from_date"] == BAD_DAY
    assert by[("EPS", "unparsed_action")]["severity"] == "BLOCK"
    assert by[("EPS", "unparsed_dividend")]["severity"] == "WARN"
    assert ("ALPHANEW", "large_move") not in by  # the split day is explained, not a big move

    scores = build_quality_scores(m3_lake, date(2026, 10, 4))
    assert scores["score"].min() >= 0 and scores["score"].max() <= 100
    # on BAD_DAY, DELTA (bad OHLC) and EPS (unparsed split ahead) are blocked: 2 of 5 members;
    # BETA's and GAMMA's blocks end before it. 60% unblocked is below the 90% floor: gates = 0
    bad = scores.filter(pl.col("trade_date") == BAD_DAY).row(0, named=True)
    assert bad["blocked"] == 2 and bad["c_gates"] == 0.0


def test_m3_gate_requires_known_events_present_and_reconciled(
    m3_lake: Lake, monkeypatch: pytest.MonkeyPatch
):
    from stockapp.quality import gate

    scores = pl.DataFrame({"trade_date": DAYS, "score": [99.0] * len(DAYS)})
    monkeypatch.setattr(gate, "MIN_CONTINUITY", 0.5)
    monkeypatch.setattr(gate, "KNOWN_EVENTS", (("ALPHANEW", SPLIT_DAY, "split"),))
    assert gate.evaluate_m3_gate(m3_lake, scores).passed

    monkeypatch.setattr(gate, "KNOWN_EVENTS", (("GAMMA", BONUS_DAY, "bonus"),))  # doesn't reconcile
    result = gate.evaluate_m3_gate(m3_lake, scores)
    assert not result.passed and any("FAIL GAMMA" in line for line in result.lines)

    monkeypatch.setattr(gate, "KNOWN_EVENTS", (("NOPE", SPLIT_DAY, "split"),))
    assert any("MISSING NOPE" in line for line in gate.evaluate_m3_gate(m3_lake, scores).lines)

    low = scores.with_columns(pl.lit(90.0).alias("score"))
    monkeypatch.setattr(gate, "KNOWN_EVENTS", ())
    assert not gate.evaluate_m3_gate(m3_lake, low).passed
