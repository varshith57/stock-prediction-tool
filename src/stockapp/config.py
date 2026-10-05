"""Configuration.

Two kinds of configuration, kept apart on purpose:

* ``Settings``: secrets and environment wiring, read from environment variables (and ``.env``
  locally). Never committed, never logged.
* ``AppConfig``: product thresholds, caps and costs, read from ``config/defaults.yaml``. Versioned
  with the code so every signal can record which config produced it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "defaults.yaml"
LOCAL_CONFIG_PATH = REPO_ROOT / "config" / "local.yaml"  # gitignored personal overrides


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "ci", "prod"] = "local"
    telegram_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    database_url: SecretStr = SecretStr("postgresql://stockapp:stockapp@localhost:5432/stockapp")
    lake_uri: str = str(REPO_ROOT / "data" / "lake")  # local path now, s3://bucket on R2 later
    r2_account_id: str | None = None
    r2_access_key_id: SecretStr | None = None
    r2_secret_access_key: SecretStr | None = None
    app_password_hash: SecretStr | None = None  # set with: uv run stockapp set-password

    @property
    def telegram_configured(self) -> bool:
        return self.telegram_token is not None and bool(self.telegram_chat_id)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BudgetConfig(_Strict):
    weekly_inr: float = Field(gt=0)
    min_position_inr: float = Field(gt=0)


class GateConfig(_Strict):
    min_signals: int = Field(ge=1)
    min_wilson_lower_bound: float = Field(gt=0, lt=1)
    live_recheck_last_n: int = Field(ge=1)
    live_min_precision: float = Field(gt=0, lt=1)


class SignalsConfig(_Strict):
    gain_threshold: float = Field(gt=0, lt=1)
    crash_threshold: float = Field(gt=0, lt=1)
    window_trading_days: int = Field(ge=1)
    certainty_bar: float = Field(gt=0, lt=1)
    max_opportunities: int = Field(ge=0)
    gate: GateConfig


class RiskConfig(_Strict):
    max_stock_weight: float = Field(gt=0, le=1)
    max_sector_weight: float = Field(gt=0, le=1)
    stop_atr_multiple: float = Field(gt=0)
    trailing_stop_atr_multiple: float | None = Field(default=None, gt=0)
    drawdown_review: float = Field(lt=0)
    drawdown_pause: float = Field(lt=0)
    stress_vix_percentile: float = Field(gt=0, lt=1)


class UniverseConfig(_Strict):
    size: int = Field(ge=1)
    rank_by: Literal["median_traded_value_6m"]
    min_listing_days: int = Field(ge=0)
    allow_buy_before_results: bool


class DataConfig(_Strict):
    history_start: str
    min_quality_score: float = Field(ge=0, le=100)


class CostsConfig(_Strict):
    brokerage: float = Field(ge=0)
    stt_rate: float = Field(ge=0)
    stamp_duty_buy_rate: float = Field(ge=0)
    exchange_txn_rate: float = Field(ge=0)
    sebi_fee_rate: float = Field(ge=0)
    dp_charge_per_scrip_sell_inr: float = Field(ge=0)
    gst_rate: float = Field(ge=0)
    slippage_bps: float = Field(ge=0)


class TaxConfig(_Strict):
    stcg_rate: float = Field(ge=0)
    ltcg_rate: float = Field(ge=0)
    ltcg_exemption_inr: float = Field(ge=0)


class AlertsConfig(_Strict):
    telegram_enabled: bool
    max_per_week: int = Field(ge=0)
    hide_closest_candidate: bool


class ScheduleConfig(_Strict):
    daily_run: str
    weekly_plan: str


class GoalConfig(_Strict):
    target_monthly_return: float | None = Field(default=None, gt=-1, lt=10)


class AppConfig(_Strict):
    version: int
    budget: BudgetConfig
    signals: SignalsConfig
    risk: RiskConfig
    universe: UniverseConfig
    data: DataConfig
    costs: CostsConfig
    tax: TaxConfig
    alerts: AlertsConfig
    schedule: ScheduleConfig
    goal: GoalConfig = GoalConfig()


def load_app_config(
    path: Path = DEFAULT_CONFIG_PATH, local_path: Path | None = LOCAL_CONFIG_PATH
) -> AppConfig:
    """Load defaults, then layer the gitignored local overrides (personal amounts) on top."""
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if local_path is not None and local_path.exists():
        with local_path.open(encoding="utf-8") as f:
            raw = _deep_merge(raw, yaml.safe_load(f) or {})
    return AppConfig.model_validate(raw)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@lru_cache
def get_settings() -> Settings:
    return Settings()


def file_config() -> AppConfig:
    """Code defaults (config/defaults.yaml) plus the legacy local overrides file. Used only to seed
    the first settings version; afterwards the app's Settings page (Postgres) is the source."""
    return load_app_config()


def get_app_config() -> AppConfig:
    """Effective settings: the latest version saved in the app, else the file defaults. Read on
    every call (cheap), so a change in Settings applies everywhere at once."""
    try:
        from stockapp.settings_store import latest_config

        return latest_config() or file_config()
    except Exception:
        return file_config()
