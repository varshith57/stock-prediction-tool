from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from stockapp.config import DEFAULT_CONFIG_PATH, Settings, load_app_config


def test_local_overrides_merge_over_defaults(tmp_path: Path):
    local = tmp_path / "local.yaml"
    local.write_text("budget:\n  weekly_inr: 750\n")
    cfg = load_app_config(local_path=local)
    assert cfg.budget.weekly_inr == 750
    assert cfg.budget.min_position_inr == 3000  # untouched sibling key survives the merge


def test_local_override_typo_is_rejected(tmp_path: Path):
    local = tmp_path / "local.yaml"
    local.write_text("budget:\n  weekly_in: 750\n")
    with pytest.raises(ValidationError):
        load_app_config(local_path=local)


def test_defaults_load_and_match_plan():
    cfg = load_app_config(local_path=None)
    assert cfg.signals.certainty_bar == 0.90
    assert cfg.signals.max_opportunities == 5
    assert cfg.signals.gate.min_signals == 30
    assert cfg.risk.max_stock_weight == 0.15
    assert cfg.risk.drawdown_pause == -0.12
    assert cfg.data.history_start == "2016-01-01"
    assert cfg.alerts.max_per_week == 3


def test_unknown_key_is_rejected(tmp_path: Path):
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    raw["signals"]["certainty_barr"] = 0.5  # typo must not be silently ignored
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError):
        load_app_config(p, local_path=None)


def test_out_of_range_value_is_rejected(tmp_path: Path):
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    raw["signals"]["certainty_bar"] = 1.5
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError):
        load_app_config(p, local_path=None)


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    s = Settings(_env_file=None)
    assert s.telegram_configured
    assert "123:abc" not in repr(s)  # secrets are masked


def test_settings_without_telegram(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert not Settings(_env_file=None).telegram_configured
