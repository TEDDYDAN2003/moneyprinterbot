from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OrderRequest:
    side: str
    quantity: float
    entry: float
    stop_loss: float
    take_profit: float


@dataclass(frozen=True)
class Position:
    id: int
    request: OrderRequest
    opened_at: object


class Broker(Protocol):
    def open(self, request: OrderRequest, timestamp: object) -> Position: ...

    def close(self, position_id: int, price: float, timestamp: object) -> float: ...
