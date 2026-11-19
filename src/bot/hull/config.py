"""Only explicitly selected profiles are eligible. Lot & target amounts are per ticket."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


Rate = Literal["slow", "medium", "fast"]
COUNTS = {"slow": (10,), "medium": (4, 3), "fast": (7, 6)}
FRAMES = (1, 2, 5)
LADDER = "profit_ladder_70_30_v1"
FIXED = "fixed_tp"


class Indicator(Strict):
    hull_len: int = Field(24, ge=2, le=500)
    source: Literal["open", "high", "low", "close", "hl2", "hlc3", "ohlc4"] = "hl2"
    signal: Literal["labels", "ema_plan"] = "labels"
    exit_opposite: bool = True
    ema_length: int = Field(200, ge=1, le=2000)
    stop: Literal["local_pullback", "confirmed_pivot"] = "local_pullback"
    pullback_bars: int = Field(7, ge=1, le=100)
    swing_len: int = Field(3, ge=1, le=100)
    atr_length: int = Field(14, ge=1, le=500)
    atr_buffer: float = Field(0.1, ge=0, le=100)
    extra_pips: float = Field(0.0, ge=0, le=100000)


class Profile(Strict):
    broker_symbol: str | None = Field(default=None, min_length=1, max_length=64)
    aliases: tuple[str, ...] = ()
    lots: float = Field(gt=0)
    target_usd: float = Field(default=10.0, gt=0)
    rate: Rate = "slow"
    session: Literal["configured", "continuous"] = "configured"
    pip_size: float = Field(gt=0)
    max_spread: float | None = Field(default=None, gt=0)  # price units, not points


class Limits(Strict):
    # None = no discretionary cap. Explicit older/local values remain binding.
    max_positions: int | None = Field(None, ge=1, le=100)
    max_total_lots: float | None = Field(None, gt=0)
    max_open_risk_usd: float | None = Field(None, gt=0)
    daily_loss_usd: float = Field(0.0, ge=0)
    drawdown_usd: float = Field(75.0, gt=0)
    # Minimum free margin after funding, as a percentage of current equity.
    min_free_margin_pct: float = Field(0.0, ge=0, le=100)
    max_orders_minute: int = Field(40, ge=1, le=200)
    max_quote_age_s: float = Field(4.0, gt=0, le=15)
    max_entry_age_s: float = Field(8.0, gt=0, le=30)


class HullConfig(Strict):
    exit_policy: Literal["profit_ladder_70_30_v1", "fixed_tp"] = LADDER
    indicator: Indicator = Indicator()
    limits: Limits = Limits()
    magic: int = Field(26111825, gt=0, lt=2**31)
    enabled: tuple[str, ...] = ("XAUUSD",)
    symbols: dict[str, Profile] = Field(min_length=1)

    @model_validator(mode="after")
    def valid(self):
        if (
            not self.enabled
            or len(self.enabled) != len(set(self.enabled))
            or set(self.enabled) - set(self.symbols)
        ):
            raise ValueError("Select unique, configured Hull symbol keys")
        return self


def selection(
    cfg: HullConfig, keys=None, profiles=None, lots=None, target=None, rate=None
):
    if profiles and (
        keys is not None or any(v is not None for v in (lots, target, rate))
    ):
        raise ValueError("Use --profile alone or --symbols with global overrides")
    result = {}
    entries = []
    if profiles:
        for raw in profiles:
            fields = raw.split(":")
            if len(fields) != 4:
                raise ValueError("--profile expects SYMBOL:LOT:TARGET_USD:RATE")
            k, l, t, r = fields
            entries.append((k, {"lots": float(l), "target_usd": float(t), "rate": r}))
    else:
        over = {
            k: v
            for k, v in [("lots", lots), ("target_usd", target), ("rate", rate)]
            if v is not None
        }
        entries = [(k, over) for k in (keys or cfg.enabled)]
    for k, over in entries:
        if k not in cfg.symbols or k in result:
            raise ValueError(
                f"Unknown or repeated symbol key {k!r}; explicitly configure hull.symbols first"
            )
        result[k] = Profile.model_validate(cfg.symbols[k].model_dump() | over)
    return result
