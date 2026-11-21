"""v4 benchmark inputs; not accepted by the trading commands."""

from typing import Literal
from pydantic import Field, field_validator
from ..config import Strict, Limits, Profile, LADDER

FRAMES = (1, 2, 5)
CLOSE_ONLY = "confirmed_close_only"
RETIRED_LIVE_TIMING = "live_continuation_v1"


class Indicator(Strict):
    hull_len: int = Field(16, ge=2, le=500)
    source: Literal[
        "either", "open", "high", "low", "close", "hl2", "hlc3", "ohlc4"
    ] = "either"
    signal: Literal["labels", "ema_plan"] = "labels"
    exit_opposite: bool = True
    ema_length: int = Field(200, ge=1, le=2000)
    stop: Literal["local_pullback", "confirmed_pivot"] = "local_pullback"
    pullback_bars: int = Field(7, ge=1, le=100)
    swing_len: int = Field(3, ge=1, le=100)
    atr_length: int = Field(14, ge=1, le=500)
    atr_buffer: float = Field(0.1, ge=0, le=100)
    extra_pips: float = Field(0.0, ge=0, le=100000)

    @property
    def signal_policy(self) -> str:
        return "fc_hull_either_v2" if self.source == "either" else "fc_hull_single_v1"


class HullConfig(Strict):
    entry_timing: Literal["confirmed_close_only"] = CLOSE_ONLY
    exit_policy: Literal["profit_ladder_70_30_v1", "fixed_tp"] = LADDER
    indicator: Indicator = Indicator()
    limits: Limits = Limits()
    magic: int = Field(26111825, gt=0, lt=2**31)
    enabled: tuple[str, ...] = ("XAUUSD",)
    symbols: dict[str, Profile] = Field(min_length=1)

    @field_validator("entry_timing", mode="before")
    @classmethod
    def migrate_retired_live_clock(cls, value):
        # Old full-file private configs must not reactivate pre-close execution.
        # Preflight/startup report the migration. No private YAML or ticket state is edited.
        return CLOSE_ONLY if value == RETIRED_LIVE_TIMING else value
