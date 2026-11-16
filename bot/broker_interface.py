from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from bot.models import AccountInfo, Candle, Position, Side, SymbolSpec, Tick


class BrokerAdapter(ABC):
    """Abstract interface implemented by MT5Adapter (live Windows) and SimBroker (tests)."""

    @abstractmethod
    def connect(self) -> bool:
        ...

    @abstractmethod
    def disconnect(self) -> None:
        ...

    @abstractmethod
    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        ...

    @abstractmethod
    def get_account_info(self) -> AccountInfo:
        ...

    @abstractmethod
    def get_latest_tick(self, symbol: str) -> Optional[Tick]:
        ...

    @abstractmethod
    def get_ticks_from(self, symbol: str, from_time: datetime, count: int) -> list[Tick]:
        ...

    @abstractmethod
    def get_m1_candles(self, symbol: str, count: int) -> list[Candle]:
        ...

    @abstractmethod
    def get_open_positions(self, symbol: str, magic: Optional[int] = None) -> list[Position]:
        ...

    @abstractmethod
    def open_market_order(
        self,
        symbol: str,
        side: Side,
        volume: float,
        sl: float,
        tp: float,
        magic: int,
        deviation: int,
        comment: str,
    ) -> Optional[int]:
        ...

    @abstractmethod
    def modify_position_sl_tp(
        self,
        ticket: int,
        symbol: str,
        sl: float,
        tp: float,
    ) -> bool:
        ...

    @abstractmethod
    def close_position(self, pos: Position, deviation: int, comment: str) -> bool:
        ...
