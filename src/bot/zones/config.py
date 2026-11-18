"""Validated, explicit whitelist; monetary settings are USD *per leg*."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class Instrument(Strict):
    enabled: bool = False
    broker_symbol: str = Field(pattern=r"^[a-zA-Z0-9_.#-]+$")
    lots: float = Field(gt=0)
    tp_usd: float = Field(gt=0)
    sl_usd: float = Field(gt=0)
    legs: int = Field(default=1, ge=1, le=10)
    contract_size: float = Field(gt=0)
    # USD quote (gold/EURUSD), or USD base (e.g. USDJPY), USD deposit only.
    conversion: Literal["quote_usd", "base_usd"] = "quote_usd"
    point: float = Field(gt=0)  # MT5 point; spread column is a count of points
    tick_size: float = Field(gt=0)
    max_spread: float = Field(gt=0)  # price units, NOT points
    assumed_spread: float = Field(gt=0)  # ONLY for a bid OHLC file without spread
    slippage: float = Field(ge=0)  # adverse price units per side
    commission: float = Field(ge=0)  # USD per lot, round trip
    round_step: float = Field(default=0, ge=0)  # optional opposite-zone objective


class Rules(Strict):
    pivot_bars: int = Field(default=2, ge=1, le=8)
    width_atr: float = Field(default=0.15, gt=0, le=1)
    momentum_min: float = Field(default=0.25, ge=0, le=1)
    recency_bars: int = Field(default=60, ge=5, le=300)  # in zone's own timeframe
    retest_minutes: int = Field(default=20, ge=1, le=240)
    max_chase_atr: float = Field(default=0.65, gt=0)
    reset_gap_minutes: int = Field(default=30, ge=2, le=1440)
    require_structural_stop: bool = False
    exit_mode: Literal["fixed_cash", "next_zone"] = "fixed_cash"


class Limits(Strict):
    max_legs: int = Field(default=3, ge=1)
    max_risk_usd: float = Field(default=15.0, gt=0)
    max_total_lots: float = Field(default=0.27, gt=0)
    daily_loss_usd: float = Field(default=30.0, gt=0)
    drawdown_usd: float = Field(default=75.0, gt=0)
    magic: int = Field(default=26011808, gt=0, le=2**31 - 1)
    entry_age_seconds: float = Field(default=8.0, gt=0, le=59)


class ZoneConfig(Strict):
    enabled: bool = True
    rules: Rules = Rules()
    limits: Limits = Limits()
    symbols: dict[str, Instrument] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_and_enabled(self):
        names = [s.broker_symbol.casefold() for s in self.symbols.values()]
        if len(names) != len(set(names)):
            raise ValueError("Broker symbols must be unique")
        if self.enabled and not any(s.enabled for s in self.symbols.values()):
            raise ValueError("Enable at least one instrument")
        return self
