"""Only explicitly selected profiles are eligible. Lot and configured USD base are per ticket."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


Rate = Literal["slow", "medium", "fast"]
COUNTS = {"slow": (10,), "medium": (4, 3), "fast": (7, 6)}
# Technical MT5 capabilities, NOT an approval to scan/trade any of these frames.
NATIVE_TF = {
    **{f"M{v}": v for v in (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30)},
    **{f"H{v}": v * 60 for v in (1, 2, 3, 4, 6, 8, 12)},
    "D1": 1440,
}
# M45 is a study interval, NEVER an MT5 native timeframe constant.
SIGNAL_TF = {"M15": 15, "M30": 30, "M45": 45}
TF_NAME = {v: k for k, v in (NATIVE_TF | SIGNAL_TF).items()}
LADDER = "profit_ladder_70_30_v1"
FIXED = "fixed_tp"
TARGET_POLICY = "base_plus_entry_spread_v1"  # all newly opened Hull tickets, never retrofit saved goals
DYNAMIC_HULL_LENGTH = 16  # shared base period for the active scanner
CLOSE_TIMING = "confirmed_cross_next_bar"
STOP_POLICY = "confirmed_cross_candle_wick_v1"
ENTRY_POLICY = "fc_dynamic_failed_mss_return_v3"
BATCH_ENTRY_POLICY = "first_eligible_while_lane_flat_v2"
# Removed in Hull only. Older full-file demo configs may still contain the keys.
RETIRED_LIMITS = ("drawdown_usd", "max_orders_minute")


class Indicator(Strict):
    hull_len: int = Field(DYNAMIC_HULL_LENGTH, ge=2, le=500)
    source: Literal[
        "dynamic", "open", "high", "low", "close", "hl2", "hlc3", "ohlc4", "custom"
    ] = "dynamic"
    neutral_mode: Literal["high", "wait"] = "high"
    custom_source: Literal["open", "high", "low", "close", "hl2", "hlc3", "ohlc4"] = (
        "hl2"
    )
    # None is Pine's blank “Chart” input. Overrides supply bias only, not an entry lane.
    structure_timeframe: str | None = None
    exit_opposite: Literal[True] = True
    exit_on_mss: Literal[True] = True
    # Legacy lookback stays parseable to preserve private configs and the state-key hash.
    stop: Literal["cross_candle"] = "cross_candle"
    pullback_bars: int = Field(7, ge=1, le=100)
    swing_len: int = Field(3, ge=1, le=50)
    atr_length: int = Field(14, ge=1, le=500)
    atr_buffer: float = Field(0.1, ge=0, le=100)
    extra_pips: float = Field(0.0, ge=0, le=100000)

    @property
    def signal_policy(self) -> str:
        return "fc_dynamic_source_internal_v1"

    @field_validator("structure_timeframe")
    @classmethod
    def valid_structure_tf(cls, v):
        if v in (None, "", "Chart"):
            return None
        if v not in NATIVE_TF:
            raise ValueError(
                "Expected a native MT5 structure timeframe, or null for Chart"
            )
        return v


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
    # Minimum free margin after funding, as a percentage of current equity.
    min_free_margin_pct: float = Field(0.0, ge=0, le=100)
    max_quote_age_s: float = Field(4.0, gt=0, le=15)
    max_entry_age_s: float = Field(8.0, gt=0, le=30)

    @model_validator(mode="before")
    @classmethod
    def omit_retired_limits(cls, data):
        # Drop only these two retired inputs; every other unexpected key is still
        # rejected. Keep the source mapping intact for the preflight notice.
        if isinstance(data, dict):
            return {
                key: value for key, value in data.items() if key not in RETIRED_LIMITS
            }
        return data


class HullConfig(Strict):
    entry_timing: Literal["confirmed_cross_next_bar"] = CLOSE_TIMING
    eligible_timeframes: tuple[str, ...] = ()  # no default trading authorization
    exit_policy: Literal["profit_ladder_70_30_v1", "fixed_tp"] = LADDER
    indicator: Indicator = Indicator()
    limits: Limits = Limits()
    magic: int = Field(26111825, gt=0, lt=2**31)
    enabled: tuple[str, ...] = ("XAUUSD",)
    symbols: dict[str, Profile] = Field(min_length=1)

    @field_validator("indicator")
    @classmethod
    def bot_length(cls, v: Indicator) -> Indicator:
        # Private YAML files are full configs: never silently run an old period.
        # Standalone indicator arithmetic can still be exercised at other lengths.
        if v.hull_len != DYNAMIC_HULL_LENGTH:
            raise ValueError(
                f"The active Hull bot requires hull.indicator.hull_len: {DYNAMIC_HULL_LENGTH} "
                f"(got {v.hull_len}). Update this field in your local config."
            )
        return v

    @field_validator("eligible_timeframes")
    @classmethod
    def valid_frames(cls, v):
        if len(set(v)) != len(v) or any(f not in SIGNAL_TF for f in v):
            raise ValueError(
                "Use a unique subset of the approved Hull lanes: M15, M30, M45"
            )
        return v

    def lanes(self, requested=None):
        approved = tuple(SIGNAL_TF[f] for f in self.eligible_timeframes)
        if not approved:
            raise ValueError(
                "No eligible Hull timeframes configured; scanner is disabled."
            )
        lanes = approved if requested is None else tuple(requested)
        if not lanes or len(set(lanes)) != len(lanes) or set(lanes) - set(approved):
            raise ValueError("Requested timeframe is not in hull.eligible_timeframes")
        return lanes

    @model_validator(mode="after")
    def valid(self):
        if (
            not self.enabled
            or len(self.enabled) != len(set(self.enabled))
            or set(self.enabled) - set(self.symbols)
        ):
            raise ValueError("Select unique, configured Hull symbol keys")
        st = NATIVE_TF.get(self.indicator.structure_timeframe)
        if st and any(SIGNAL_TF[f] != st for f in self.eligible_timeframes):
            raise ValueError(
                "Independent bot lanes require their own structure; set hull.indicator.structure_timeframe: null"
            )
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
                raise ValueError("--profile expects SYMBOL:LOT:BASE_USD:RATE")
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
