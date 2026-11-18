from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bot.hull.config import HullConfig
from bot.zones.config import ZoneConfig


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    mode: Literal["backtest", "paper", "auto", "demo", "live"] = "paper"
    symbol: str = "XAUUSD"
    seed: int = 42


class DataConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    history_years: int = Field(default=3, ge=1)
    base_timeframe: str = "M1"
    timeframes: list[str] = ["M1", "M2", "M5"]


class RootConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    run: RunConfig = RunConfig()
    data: DataConfig = DataConfig()
    zones: ZoneConfig | None = None  # archived experiments
    hull: HullConfig | None = None


def validate_config(data: dict) -> RootConfig:
    """Validate the stable Phase 1 configuration contract."""
    return RootConfig.model_validate(data)
