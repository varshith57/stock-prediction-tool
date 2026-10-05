"""Home screen: the three buckets render from the saved plan, the confidence slider only reveals
watch items, and the whole portfolio appears exactly once across Hold and Sell."""

from __future__ import annotations

import html
from pathlib import Path

import psycopg
import pytest
from streamlit.testing.v1 import AppTest
from test_m8 import OFF, cand, hold, plan

from stockapp.config import get_settings
from stockapp.plan.store import save_plan

APP = Path(__file__).parent.parent / "app"


def _script() -> None:  # runs as its own script: only names imported here are available
    import os
    import sys

    sys.path.insert(0, os.environ["TEST_APP_DIR"])
    from views import this_week

    this_week.render()


@pytest.fixture
def home(db: psycopg.Connection, _test_database: str, tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", _test_database)
    monkeypatch.setenv("LAKE_URI", str(tmp_path / "lake"))
    monkeypatch.setenv("TEST_APP_DIR", str(APP))
    get_settings.cache_clear()
    p = plan(
        [cand("LOSER", p_c=0.01), cand("RISKY", p_c=0.40), cand("SAFE", p_c=0.02)]
        + [cand(f"N{i}", p_a=0.05 * i) for i in range(1, 5)],
        [
            hold("LOSER", cost=100, last=80, quantity=5, opened_by_signal_a=True),
            hold("RISKY"),
            hold("SAFE"),
        ],
        gates=OFF,
    )
    save_plan(db, p, "test")
    yield p
    get_settings.cache_clear()


def _text(at: AppTest) -> str:
    return html.unescape(" ".join(m.value for m in at.markdown))


def test_buckets_and_slider(home):
    at = AppTest.from_function(_script, default_timeout=30).run()
    assert not at.exception
    text = _text(at)
    assert "Nothing to buy" in text and "N4" not in text  # 90%: watch items stay hidden
    assert "Stop-loss hit" in text and "-20.0% since you bought" in text  # rule, no %
    assert text.index("RISKY") < text.index("SAFE")  # riskiest hold first
    assert "haven't yet proven 90% accurate" in text

    at.slider(key="confidence").set_value(15).run()
    assert not at.exception
    text = _text(at)
    assert "N4" in text and "N3" in text and "N2" not in text  # 20% and 15% shown, 10% not
    assert "watch, don't buy" in text
    assert "40% drop risk" in text and "watch, don't sell yet" in text  # RISKY moves to Sell
    for symbol in ("LOSER", "RISKY", "SAFE"):  # every holding shown exactly once
        assert text.count(f">{symbol}<") == 1
