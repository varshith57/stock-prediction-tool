"""Editable settings: versions, diffs, impact, restore; gate re-check; goal bands; background."""

from __future__ import annotations

import json
import os
from datetime import date

import polars as pl
import psycopg
import pytest

from stockapp import background, settings_store
from stockapp.config import file_config
from stockapp.goal import band, feasibility
from stockapp.lake import Lake
from stockapp.models.run import precision_at_bar, reevaluate_gate


def _with(cfg, **changes):
    """Copy with dotted-path changes, e.g. budget__weekly_inr=2500."""
    d = cfg.model_dump()
    for path, value in changes.items():
        node = d
        *parents, leaf = path.split("__")
        for p in parents:
            node = node[p]
        node[leaf] = value
    return type(cfg).model_validate(d)


def test_seed_save_diff_impact_and_restore(db: psycopg.Connection):
    v1 = settings_store.ensure_seeded(db)
    assert settings_store.ensure_seeded(db) == v1  # seeded once
    base = settings_store.latest_config(db)
    assert base == file_config()

    new = _with(base, budget__weekly_inr=2500.0, signals__certainty_bar=0.85)
    v2, changes = settings_store.save(db, new, "more budget")
    assert changes == {
        "budget.weekly_inr": [base.budget.weekly_inr, 2500.0],
        "signals.certainty_bar": [0.9, 0.85],
    }
    assert settings_store.impact(changes) == {"plan", "gate"}
    assert settings_store.latest_config(db).budget.weekly_inr == 2500.0
    assert settings_store.save(db, new) == (None, {})  # nothing changed: no new version

    _, retrain = settings_store.save(db, _with(new, signals__gain_threshold=0.08))
    assert settings_store.impact(retrain) == {"plan", "retrain"}

    v4, back = settings_store.restore(db, v1)
    assert v4 > v2 and back["budget.weekly_inr"] == [2500.0, base.budget.weekly_inr]
    assert settings_store.latest_config(db) == base
    hist = settings_store.history(db)
    assert [h["version_id"] for h in hist][:2] == [v4, v4 - 1]
    with pytest.raises(ValueError):
        settings_store.restore(db, 999)


def test_gate_recheck_on_stored_predictions(lake: Lake):
    rows = [
        {
            "company_id": f"S{i}",
            "trade_date": date(2020, 1, 3),
            "label": i < 48,
            "p": 0.95,
            "expected_gain": 0.1,
        }
        for i in range(50)
    ]
    rows += [
        {
            "company_id": f"T{i}",
            "trade_date": date(2020, 1, 10),
            "label": i < 10,
            "p": 0.3,
            "expected_gain": 0.1,
        }
        for i in range(100)
    ]
    preds = pl.DataFrame(rows)
    lake.write_partition("gold", "oos_predictions", "signal", "C", preds.drop("expected_gain"))
    lake.write_partition("gold", "oos_predictions", "signal", "A", preds)  # A has an extra column
    cfg = file_config()
    gates = reevaluate_gate(lake, cfg, date(2026, 10, 5))
    assert gates["C"].status == "LIVE" and gates["C"].cutoff == 0.95  # 48/50: lower bound 86%
    strict = _with(cfg, signals__certainty_bar=0.97)  # 96% < 97%
    assert reevaluate_gate(lake, strict, date(2026, 10, 5))["C"].status == "OFF"
    at_bar = precision_at_bar(lake, cfg, "C", 0.9)
    assert (at_bar["signals"], at_bar["hits"]) == (50, 48)
    stored = lake.scan("gold", "signal_gate").collect()
    assert stored.height >= 1


def test_goal_bands_and_probability(lake: Lake):
    assert [band(x) for x in (0.01, 0.02, 0.03, 0.06)] == [
        "Realistic",
        "Realistic",
        "Stretch",
        "Unrealistic",
    ]
    days = pl.date_range(date(2020, 1, 1), date(2020, 4, 30), eager=True)[:80]
    for i, d in enumerate(days):  # one partition per day, like the real index files
        idx = pl.DataFrame(
            {"index_name": ["Nifty 500"], "trade_date": [d], "close": [100 * 1.01**i]}
        )
        lake.write_partition("silver", "nse_index_close", "trade_date", str(d), idx)
    f = feasibility(lake, 0.5)
    assert f.band == "Unrealistic" and f.probability == 0.0
    assert feasibility(lake, 0.1).probability == 1.0
    assert f.yearly_equivalent == pytest.approx(1.5**12 - 1)


def test_background_status(tmp_path, monkeypatch: pytest.MonkeyPatch):
    state, log = tmp_path / "retrain.json", tmp_path / "r.log"
    monkeypatch.setattr(background, "STATE", state)
    assert background.status() is None
    log.write_text("[1/5] universe and quality...\nRETRAIN DONE\n")
    state.write_text(
        json.dumps({"pid": os.getpid(), "log": str(log), "started_at": "2026-10-05T10:00:00"})
    )
    s = background.status()
    assert s["done"] and not s["running"] and not s["failed"]
    log.write_text("[1/5] universe and quality...\n")
    s = background.status()
    assert s["running"]  # this test's own pid is alive and no final line yet
    state.write_text(
        json.dumps({"pid": 999999, "log": str(log), "started_at": "2026-10-05T10:00:00"})
    )
    assert background.status()["failed"]  # process gone without a final line


def test_horizon_wording_follows_the_window():
    from stockapp.config import horizon

    assert [horizon(d) for d in (5, 10, 20, 30)] == [
        "this week",
        "within 2 weeks",
        "within a month",
        "within 30 market days",
    ]
