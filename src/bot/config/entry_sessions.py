"""Mandatory, versioned symbol entry hours shared by the Hull and Wickless runners.

The file beside this module is the single source of truth, including in a wheel.
Never consult cwd, a chart's analytics buckets, or a broker suffix heuristic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field

POLICY_FILE = Path(__file__).resolve().with_name("entry_sessions.yaml")
VERSION = "symbol-entry-sessions/v1"
OUTSIDE = "outside_symbol_session"
UNMAPPED = "entry_session_unmapped_symbol"
MISMATCH = "entry_session_profile_mismatch"
UNAVAILABLE = "entry_session_policy_unavailable"


class SessionConfigError(ValueError):
    """Cannot authorize new orders with this file."""


class EntrySessionVeto(ValueError):
    """A terminal new-entry decision, never a request to wait until the next opening."""

    def __init__(self, reason, stage, phase, report):
        super().__init__(reason)
        self.fields = {"stage": stage, "session_clock": phase, "entry_session": report}


class _Unique(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        seen = set()
        for key, _ in node.value:
            k = self.construct_object(key, deep=deep)
            if not isinstance(k, str) or k in seen:
                raise ValueError(f"Non-text or duplicate YAML key: {k!r}")
            seen.add(k)
        return super().construct_mapping(node, deep=deep)


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


HHMM = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class _Clock(_Spec):
    tz: str
    start: HHMM
    end: HHMM


class _Window(_Spec):
    all_of: list[Name] = Field(min_length=1)


class _Market(_Spec):
    windows: list[Name] = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)


class _File(_Spec):
    version: Literal["symbol-entry-sessions/v1"]
    clocks: dict[Name, _Clock] = Field(min_length=1)
    windows: dict[Name, _Window] = Field(min_length=1)
    markets: dict[str, _Market] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class Clock:
    zone: ZoneInfo
    first: int
    end: int

    def active(self, utc: datetime) -> bool:
        local = utc.astimezone(self.zone)
        return (
            local.weekday() < 5
            and self.first <= local.hour * 60 + local.minute < self.end
        )


@dataclass(frozen=True, slots=True)
class Window:
    name: str
    required: tuple[Clock, ...]

    def active(self, utc: datetime) -> bool:
        return all(c.active(utc) for c in self.required)


def _utc(sec):
    if isinstance(sec, bool) or not isinstance(sec, (int, float)):
        return None
    try:
        # Floor to the second, so a sub-microsecond instant before a closing or
        # opening boundary can never round UP into the next minute.
        stamp = datetime.fromtimestamp(math.floor(sec), UTC)
        return (
            stamp if 1 < stamp.year < 9999 else None
        )  # local conversion must not overflow
    except (OSError, OverflowError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class MarketGate:
    market: str | None
    windows: tuple[Window, ...] = ()
    issue: str = ""

    def veto(self, sec) -> str:
        """Membership in the union, not a separate admission for each match."""
        if self.issue:
            return self.issue
        utc = _utc(sec)
        if utc is None:
            return "invalid_entry_session_clock"
        return "" if any(w.active(utc) for w in self.windows) else OUTSIDE

    def require(self, stage, **instants):
        if not instants:
            raise ValueError("An entry check needs a timestamp")
        for phase, stamp in instants.items():
            if reason := self.veto(stamp):
                raise EntrySessionVeto(reason, stage, phase, self.report(stamp))

    def report(self, sec):
        utc = _utc(sec)
        return {
            "market": self.market,
            "windows": [w.name for w in self.windows],
            "active": [w.name for w in self.windows if utc and w.active(utc)],
            "at_utc": utc.isoformat() if utc else None,
            "status": self.veto(sec) or "in_symbol_session",
        }


@dataclass(frozen=True, slots=True)
class EntrySessions:
    path: Path
    sha256: str | None
    by_name: Mapping[str, MarketGate]
    markets: tuple[str, ...]
    error: str | None = None

    @classmethod
    def blocked(cls, error: SessionConfigError):
        # Hull may already own tickets. Do not abandon their protection, and do
        # not use a permissive backup schedule if its file cannot be loaded.
        return cls(POLICY_FILE, None, MappingProxyType({}), (), str(error))

    def bind(self, symbol: str, profile: str | None = None) -> MarketGate:
        if self.error:
            return MarketGate(None, issue=UNAVAILABLE)
        gate = self.by_name.get(symbol)
        if gate is None:
            return MarketGate(None, issue=UNMAPPED)
        if profile is not None and self.by_name.get(profile) is not gate:
            return MarketGate(gate.market, gate.windows, MISMATCH)
        return gate

    def identity(self):
        return {
            "version": VERSION,
            "file": str(self.path),
            "sha256": self.sha256,
            "error": self.error,
        }


def _minutes(value):
    return 60 * int(value[:2]) + int(value[3:])


def _compile(path, raw):
    data = _File.model_validate(yaml.load(raw.decode("utf-8-sig"), Loader=_Unique))
    clocks = {}
    for name, c in data.clocks.items():
        if c.start >= c.end:
            raise ValueError(
                f"{name}: expected a nonempty same-day interval, start < end"
            )
        clocks[name] = Clock(ZoneInfo(c.tz), _minutes(c.start), _minutes(c.end))
    windows = {}
    for name, w in data.windows.items():
        refs = w.all_of
        if len(set(refs)) != len(refs) or set(refs) - clocks.keys():
            raise ValueError(
                f"{name}: duplicate, conflicting or undefined clock reference"
            )
        windows[name] = Window(name, tuple(clocks[n] for n in refs))
    names, spellings = {}, set()
    for name, m in data.markets.items():
        if len(set(m.windows)) != len(m.windows) or set(m.windows) - windows.keys():
            raise ValueError(f"{name}: duplicate or undefined session window")
        gate = MarketGate(name, tuple(windows[n] for n in m.windows))
        for alias in (name, *m.aliases):
            if alias != alias.strip() or not re.fullmatch(
                r"[A-Za-z0-9_.+#-][A-Za-z0-9_.+# -]{0,63}", alias
            ):
                raise ValueError(
                    f"Use an exact market spelling, not a pattern: {alias!r}"
                )
            if alias.casefold() in spellings:
                raise ValueError(f"Duplicate or ambiguous market spelling: {alias!r}")
            names[alias] = gate
            spellings.add(alias.casefold())
    return EntrySessions(
        path,
        hashlib.sha256(raw).hexdigest(),
        MappingProxyType(names),
        tuple(data.markets),
    )


def load_entry_sessions(path: Path | None = None) -> EntrySessions:
    path = POLICY_FILE if path is None else Path(path)
    try:
        return _compile(path, path.read_bytes())
    except (OSError, ValueError, ZoneInfoNotFoundError, yaml.YAMLError) as exc:
        raise SessionConfigError(
            f"Invalid mandatory entry sessions at {path}: {exc}"
        ) from exc


def sessions_for_runner() -> EntrySessions:
    try:
        return load_entry_sessions()
    except SessionConfigError as exc:
        return EntrySessions.blocked(exc)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Inspect the shared entry schedule offline; never connects to MT5."
    )
    p.add_argument(
        "--at", help="ISO date/time with Z or an explicit UTC offset (default: now)"
    )
    p.add_argument(
        "--symbol",
        action="append",
        help="Exact market key or broker symbol (repeatable; default: all)",
    )
    args = p.parse_args(argv)
    try:
        sessions = load_entry_sessions()
        at = datetime.fromisoformat(args.at) if args.at else datetime.now(UTC)
        if at.utcoffset() is None:
            raise ValueError("--at requires a timezone/UTC offset")
        selected = {s: sessions.bind(s) for s in (args.symbol or sessions.markets)}
        print(
            json.dumps(
                {
                    "entry_sessions": sessions.identity(),
                    "symbols": {
                        s: g.report(at.timestamp()) for s, g in selected.items()
                    },
                },
                indent=2,
            )
        )
        return 2 if any(g.issue for g in selected.values()) else 0
    except ValueError as exc:
        p.exit(2, f"{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
