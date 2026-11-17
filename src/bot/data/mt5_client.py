from __future__ import annotations

from datetime import datetime
from typing import Any


class MT5UnavailableError(RuntimeError):
    """Raised when an MT5 operation is requested without the optional package."""


class MT5Client:
    def __init__(self, terminal_path: str | None = None) -> None:
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise MT5UnavailableError(
                "MetaTrader5 is optional; install the live extra on the MT5 Windows host"
            ) from exc
        self._mt5 = mt5
        self._terminal_path = terminal_path

    def connect(self) -> None:
        initialized = (
            self._mt5.initialize(self._terminal_path)
            if self._terminal_path
            else self._mt5.initialize()
        )
        if not initialized:
            raise ConnectionError(f"MT5 initialize failed: {self._mt5.last_error()}")

    def bars(self, symbol: str, timeframe: int, start: datetime, end: datetime) -> Any:
        values = self._mt5.copy_rates_range(symbol, timeframe, start, end)
        if values is None:
            raise RuntimeError(f"MT5 bar download failed: {self._mt5.last_error()}")
        return values

    def close(self) -> None:
        self._mt5.shutdown()
