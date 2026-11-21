"""One broker per export; an explicit list of exact, enabled Market Watch symbols."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, strict=True
    )


class Rules(Strict):
    tolerance_ticks: int = Field(0, ge=0, le=100)
    min_body_ticks: int = Field(0, ge=0, le=10000)
    zone_pips: float = Field(3, ge=0, le=1000)
    retest_bars: int = Field(10, ge=1, le=100)
    stop_buffer_ticks: int = Field(0, ge=0, le=1000)


class Pair(Strict):
    symbol: str = Field(pattern=r"^[A-Za-z0-9_#.+-]{1,64}$")
    lots: float = Field(gt=0, le=1000)
    pip_size: float = Field(0, ge=0, le=10000)
    max_spread_pips: float = Field(gt=0, le=10000)
    max_risk_usd: float = Field(gt=0, le=1000000)

    def pip(self, tick: float) -> Decimal:
        if self.pip_size:
            return Decimal(str(self.pip_size))
        return (
            Decimal("0.1") if "XAU" in self.symbol.upper() else 10 * Decimal(str(tick))
        )

    def zone_ticks(self, rules: Rules, tick: float) -> int:
        return int(Decimal(str(rules.zone_pips)) * self.pip(tick) / Decimal(str(tick)))


class Config(Strict):
    version: Literal["wickless-m15/v1"]
    mode: Literal["paper", "demo"] = "paper"
    broker: str = Field(min_length=1, max_length=80)
    server: str = Field("", max_length=128)
    max_positions: int = Field(2, ge=1, le=32)
    rules: Rules = Rules()
    pairs: list[Pair] = Field(min_length=1, max_length=48)

    @field_validator("broker", "server")
    @classmethod
    def clean(cls, v):
        if v != v.strip() or any(ord(c) < 32 for c in v):
            raise ValueError("Broker and server must be plain, trimmed text")
        return v

    @model_validator(mode="after")
    def distinct(self):
        if len({p.symbol.casefold() for p in self.pairs}) != len(self.pairs):
            raise ValueError("Duplicate broker symbol")
        if self.mode == "demo" and not self.server:
            raise ValueError("Private demo config must pin the exact MT5 server")
        return self


def load(path: str | Path) -> Config:
    return Config.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))
