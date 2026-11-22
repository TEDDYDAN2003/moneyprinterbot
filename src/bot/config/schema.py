"""Active desktop config. Older full-file YAML may contain unused research sections."""

from typing import Literal

from pydantic import BaseModel, Field

from bot.hull.config import HullConfig
from bot.hull.hours import Hours, effective_sessions


class RunConfig(BaseModel):
    mode: Literal["paper", "demo"] = "paper"
    market_close_timezone: str = "UTC"


class TerminalConfig(BaseModel):
    terminal_path: str = ""


class SafetyConfig(BaseModel):
    emergency_flatten_hotkey_file: str = Field("KILL", min_length=1)


class RootConfig(BaseModel):
    run: RunConfig = RunConfig()
    mt5: TerminalConfig = TerminalConfig()
    safety: SafetyConfig = SafetyConfig()
    sessions: dict
    hull: HullConfig  # missing is an error, never fall back to a different trader


def validate_config(data: dict) -> RootConfig:
    model = RootConfig.model_validate({**data, "sessions": effective_sessions(data)})
    Hours(
        model.model_dump()
    )  # resolve timezones and optional entry restrictions before MT5
    return model
