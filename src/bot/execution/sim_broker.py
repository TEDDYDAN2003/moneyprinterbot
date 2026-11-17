from __future__ import annotations

from dataclasses import dataclass

from bot.execution.broker_base import OrderRequest, Position


@dataclass(frozen=True)
class ClosedTrade:
    position_id: int
    pnl: float
    closed_at: object


class SimBroker:
    def __init__(self, commission_per_unit: float = 0.0) -> None:
        if commission_per_unit < 0:
            raise ValueError("commission_per_unit cannot be negative")
        self._commission = commission_per_unit
        self._next_id = 1
        self._positions: dict[int, Position] = {}
        self.closed: list[ClosedTrade] = []

    @property
    def positions(self) -> tuple[Position, ...]:
        return tuple(self._positions.values())

    def open(self, request: OrderRequest, timestamp: object) -> Position:
        if request.side not in {"BUY", "SELL"}:
            raise ValueError(f"Unsupported side: {request.side}")
        if min(request.quantity, request.entry, request.stop_loss, request.take_profit) <= 0:
            raise ValueError("Order values must be positive")
        position = Position(self._next_id, request, timestamp)
        self._positions[position.id] = position
        self._next_id += 1
        return position

    def close(self, position_id: int, price: float, timestamp: object) -> float:
        position = self._positions.pop(position_id)
        direction = 1 if position.request.side == "BUY" else -1
        pnl = (price - position.request.entry) * direction * position.request.quantity
        pnl -= self._commission * position.request.quantity
        self.closed.append(ClosedTrade(position_id, pnl, timestamp))
        return pnl
