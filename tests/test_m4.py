"""M4: charges, tax, FIFO ledger with corporate actions, Kite import, valuation, service, auth.

Gate (plan M4): totals match a manual calculation within 1 rupee.
"""

from __future__ import annotations

from datetime import date

import polars as pl
import psycopg
import pytest

from stockapp.auth import hash_password, verify_password
from stockapp.config import load_app_config
from stockapp.lake import Lake
from stockapp.portfolio import service
from stockapp.portfolio.costs import estimated_tax, is_long_term, order_charges
from stockapp.portfolio.kite import ImportFormatError, parse_holdings_csv
from stockapp.portfolio.ledger import LedgerError, QuantityEvent, Txn, build_ledger
from stockapp.portfolio.store import NewTransaction
from stockapp.portfolio.valuation import value_portfolio

CFG = load_app_config(local_path=None)


# charges and tax ---------------------------------------------------------------------------------


def test_buy_charges_by_hand():
    c = order_charges("BUY", 10, 350.0, CFG.costs)
    # STT 3.50 + exchange 0.10745 + SEBI 0.0035 + GST 0.019971 + stamp 0.525 = 4.155921
    assert c.total == pytest.approx(4.16, abs=0.005)
    assert (c.dp, c.stamp) == (0.0, pytest.approx(0.525))


def test_sell_charges_by_hand_and_round_trip_matches_prd():
    s = order_charges("SELL", 10, 385.0, CFG.costs)
    # STT 3.85 + exchange 0.118195 + SEBI 0.00385 + GST 0.021968 + DP 15.34 = 19.334013
    assert s.total == pytest.approx(19.33, abs=0.005)
    assert s.stamp == 0.0
    round_trip = (
        order_charges("BUY", 10, 350.0, CFG.costs).total
        + order_charges("SELL", 10, 350.0, CFG.costs).total
    )
    assert round_trip == pytest.approx(23, abs=1)  # PRD: "about Rs 23 on Rs 3,500"


def test_bad_side():
    with pytest.raises(ValueError):
        order_charges("HOLD", 1, 1.0, CFG.costs)


@pytest.mark.parametrize(
    ("bought", "sold", "expected"),
    [
        (date(2024, 1, 10), date(2025, 1, 10), False),  # exactly 12 months: still short-term
        (date(2024, 1, 10), date(2025, 1, 11), True),
        (date(2024, 2, 29), date(2025, 3, 1), False),
        (date(2024, 2, 29), date(2025, 3, 2), True),
        (None, date(2025, 1, 1), None),
    ],
)
def test_long_term(bought, sold, expected):
    assert is_long_term(bought, sold) is expected


def test_tax():
    assert estimated_tax(1000, False, CFG.tax) == 200.0
    assert estimated_tax(1000, True, CFG.tax) == 125.0
    assert estimated_tax(-50, True, CFG.tax) == 0.0
    assert estimated_tax(1000, None, CFG.tax) is None


# ledger ------------------------------------------------------------------------------------------


def _t(i, side, qty, price, d, company="AAA", charges=0.0):
    return Txn(i, company, side, qty, price, d, d, charges)


def test_fifo_realised_gain():
    led = build_ledger(
        [
            _t(1, "BUY", 10, 100, date(2024, 1, 1), charges=1.0),
            _t(2, "BUY", 10, 120, date(2024, 2, 1), charges=1.0),
            _t(3, "SELL", 15, 150, date(2024, 3, 1), charges=3.0),
        ],
        [],
    )
    assert [(lot.txn_id, lot.quantity) for lot in led.lots] == [(2, 5)]
    assert led.lots[0].cost == pytest.approx(600.5)  # half of lot 2: (1200 + 1) / 2
    first, second = led.realised
    assert (first.quantity, first.cost) == (10, pytest.approx(1001.0))
    assert first.proceeds == pytest.approx((2250 - 3) * 10 / 15)
    assert (second.quantity, second.cost) == (5, pytest.approx(600.5))
    assert sum(r.gain for r in led.realised) == pytest.approx(2247 - 1601.5)


def test_bonus_and_split_adjust_quantity_not_cost():
    events = [
        QuantityEvent("AAA", date(2024, 2, 1), "bonus", ratio_new=1, ratio_held=1),
        QuantityEvent("AAA", date(2024, 3, 1), "split", from_fv=10, to_fv=2),
    ]
    led = build_ledger(
        [
            _t(1, "BUY", 10, 1000, date(2024, 1, 1)),
            _t(2, "BUY", 4, 400, date(2024, 3, 1)),  # bought on the split's ex-date: not adjusted
        ],
        events,
    )
    a, b = led.lots
    assert (a.quantity, a.cost) == (100, 10_000)  # 10 -> 20 (bonus) -> 100 (split)
    assert a.cost_per_share == 100
    assert len(a.adjustments) == 2
    assert (b.quantity, b.cost) == (4, 1600)


def test_bonus_fraction_is_floored_and_consolidation():
    led = build_ledger(
        [_t(1, "BUY", 10, 100, date(2024, 1, 1))],
        [QuantityEvent("AAA", date(2024, 2, 1), "bonus", ratio_new=1, ratio_held=3)],
    )
    assert led.lots[0].quantity == 13  # 10 + floor(10/3)
    led = build_ledger(
        [_t(1, "BUY", 25, 10, date(2024, 1, 1))],
        [QuantityEvent("AAA", date(2024, 2, 1), "consolidation", from_fv=1, to_fv=10)],
    )
    assert led.lots[0].quantity == 2


def test_overselling_is_refused():
    with pytest.raises(LedgerError, match="only 5 held"):
        build_ledger(
            [_t(1, "BUY", 5, 100, date(2024, 1, 1)), _t(2, "SELL", 6, 100, date(2024, 1, 2))], []
        )


# Kite import -------------------------------------------------------------------------------------


def test_kite_web_style_csv():
    csv = (
        b'Instrument,Qty.,Avg. cost,LTP,Cur. val,P&L,Net chg.,Day chg.\n'
        b'RELIANCE,10,"1,234.50",1300,13000,655,5.3,0.4\n'
        b'GAMMA-BE,5,40,41,205,5,2.5,0\n'
        b'BADQTY,2.5,100,1,1,1,1,1\n'
        b',3,10,1,1,1,1,1\n'
    )  # fmt: skip
    p = parse_holdings_csv(csv)
    assert p.column_map == {"symbol": "Instrument", "quantity": "Qty.", "avg_price": "Avg. cost"}
    assert p.rows.select("symbol", "quantity", "avg_price").rows() == [
        ("RELIANCE", 10, 1234.5),
        ("GAMMA", 5, 40.0),
    ]
    assert len(p.problems) == 2  # fractional quantity, missing symbol: reported, not imported


def test_console_style_csv_with_isin():
    csv = b"Symbol,ISIN,Sector,Quantity Available,Average Price\nINFY,INE009A01021,IT,3,1500\n"
    p = parse_holdings_csv(csv)
    assert p.rows.row(0, named=True) == {
        "symbol": "INFY", "quantity": 3, "avg_price": 1500.0, "isin": "INE009A01021",
    }  # fmt: skip


def test_kite_unknown_or_ambiguous_layout_is_refused():
    with pytest.raises(ImportFormatError, match="missing"):
        parse_holdings_csv(b"Name,Units\nX,1\n")
    with pytest.raises(ImportFormatError, match="more than one"):
        parse_holdings_csv(b"Symbol,Qty,Quantity,Avg cost\nX,1,1,1\n")


# valuation: the M4 gate ------------------------------------------------------------------------


def test_totals_match_manual_calculation_within_one_rupee():
    buy_a = order_charges("BUY", 10, 350.0, CFG.costs).total  # 4.16
    buy_b = order_charges("BUY", 4, 1000.0, CFG.costs).total
    sell_a = order_charges("SELL", 4, 400.0, CFG.costs).total
    led = build_ledger(
        [
            _t(1, "BUY", 10, 350, date(2024, 1, 1), "AAA", buy_a),
            _t(2, "BUY", 4, 1000, date(2025, 6, 1), "BBB", buy_b),
            _t(3, "SELL", 4, 400, date(2025, 7, 1), "AAA", sell_a),
        ],
        [],
    )
    prices = pl.DataFrame(
        {"company_id": ["AAA", "BBB"], "symbol": ["AAA", "BBB"], "close": [380.0, 950.0],
         "price_date": [date(2026, 10, 1)] * 2, "prev_close": [370.0, 960.0]}
    )  # fmt: skip
    v = value_portfolio(
        led, prices, {"AAA": "IT", "BBB": "IT"}, CFG, date(2026, 10, 4), date(2026, 10, 1)
    )
    # by hand: AAA 6 left at cost (3500 + 4.16) * 6/10 = 2102.50; BBB 4000 + buy_b
    invested = (3500 + buy_a) * 0.6 + 4000 + buy_b
    value = 6 * 380 + 4 * 950
    realised = (4 * 400 - sell_a) - (3500 + buy_a) * 0.4
    assert v.totals["invested"] == pytest.approx(invested, abs=1)
    assert v.totals["value"] == pytest.approx(value, abs=1)
    assert v.totals["unrealised"] == pytest.approx(value - invested, abs=1)
    assert v.totals["realised"] == pytest.approx(realised, abs=1)
    assert v.totals["day_change"] == pytest.approx(6 * 10 + 4 * -10, abs=1)
    # both stocks are one sector (100% > 30% cap) and each is above 15%
    assert any("Sector IT" in w for w in v.warnings)
    assert sum("of the portfolio" in w for w in v.warnings) == 2


def test_stale_and_missing_prices_are_flagged():
    led = build_ledger(
        [
            _t(1, "BUY", 1, 10, date(2024, 1, 1), "OLD"),
            _t(2, "BUY", 1, 10, date(2024, 1, 1), "GONE"),
        ],
        [],
    )
    prices = pl.DataFrame(
        {"company_id": ["OLD"], "symbol": ["OLD"], "close": [12.0],
         "price_date": [date(2026, 9, 1)], "prev_close": [None]},
        schema_overrides={"prev_close": pl.Float64},
    )  # fmt: skip
    v = value_portfolio(led, prices, {}, CFG, date(2026, 10, 4), date(2026, 10, 1))
    by = {r["company_id"]: r for r in v.holdings.iter_rows(named=True)}
    assert by["OLD"]["stale"] and by["GONE"]["stale"]
    assert by["OLD"]["sector"] == "Unclassified"
    assert any("GONE: no price" in w for w in v.warnings)
    assert v.totals["value"] == 12.0


# service against Postgres and a lake with a gold latest-prices table ------------------------------


@pytest.fixture
def plake(lake: Lake) -> Lake:
    df = pl.DataFrame(
        {"company_id": ["RELIANCE", "INFY"], "symbol": ["RELIANCE", "INFY"],
         "isin": ["INE002A01018", "INE009A01021"], "close": [1300.0, 1500.0],
         "price_date": [date(2026, 10, 1)] * 2, "prev_close": [1290.0, 1490.0],
         "data_as_of": [date(2026, 10, 1)] * 2}
    )  # fmt: skip
    lake.write_partition("gold", "latest_prices", "built", "2026-10-04", df)
    return lake


def test_service_add_sell_and_refusals(db: psycopg.Connection, plake: Lake):
    buy = NewTransaction("RELIANCE", "RELIANCE", "BUY", 10, 1200.0, date(2026, 1, 5))
    service.add(db, plake, CFG, [buy])
    with pytest.raises(LedgerError):
        service.add(db, plake, CFG, [NewTransaction("RELIANCE", "RELIANCE", "SELL", 11, 1300.0,
                                                    date(2026, 2, 1))])  # fmt: skip
    [sell_id] = service.add(
        db,
        plake,
        CFG,
        [NewTransaction("RELIANCE", "RELIANCE", "SELL", 4, 1300.0, date(2026, 2, 1))],
    )
    loaded = service.load(db, plake, CFG, date(2026, 10, 4))
    assert loaded.view.totals["value"] == 6 * 1300
    buy_id = next(r["txn_id"] for r in loaded.transactions if r["side"] == "BUY")
    with pytest.raises(LedgerError):  # the sale depends on this buy
        service.delete(db, plake, CFG, buy_id)
    service.delete(db, plake, CFG, sell_id)
    service.delete(db, plake, CFG, buy_id)
    assert service.load(db, plake, CFG, date(2026, 10, 4)).transactions == []


def test_kite_import_plan(plake: Lake):
    csv = (b"Symbol,ISIN,Quantity,Average Price\nRELIANCE,INE002A01018,10,1250\n"
           b"INFY,INE000X00000,2,1400\nNOPE,,1,1\n")  # fmt: skip
    plan = service.plan_kite_import(plake, parse_holdings_csv(csv))
    assert [(t.symbol, t.quantity, t.trade_date, t.charges) for t in plan.ready] == [
        ("RELIANCE", 10, None, None)  # buy date unknown, charges estimated
    ]
    assert any("INFY: ISIN" in u for u in plan.unmatched)
    assert any(u.startswith("NOPE") for u in plan.unmatched)


# auth --------------------------------------------------------------------------------------------


def test_password_hash_roundtrip():
    stored = hash_password("correct horse battery")
    assert stored.startswith("scrypt$")
    assert verify_password("correct horse battery", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("x", "garbage")
    with pytest.raises(ValueError):
        hash_password("short")
