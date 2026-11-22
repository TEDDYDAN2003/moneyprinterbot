"""Shared terminal guards and OS lock. No strategy, terminal initialization or order sender."""

from __future__ import annotations

import json
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class DemoSafetyError(RuntimeError):
    """The requested demo run failed a fail-closed safety check."""


@contextmanager
def _account_order_lock(path: Path, timeout_seconds: float = 15.0):
    """Serialize orders with an OS lock, which is safe across Windows TF workers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    acquired = False
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    try:
        if os.name == "nt":
            import msvcrt

            while time.monotonic() < deadline:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl

            while time.monotonic() < deadline:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    time.sleep(0.05)
        if not acquired:
            raise DemoSafetyError(f"Timed out waiting for account order lock: {path}")
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _side_is_allowed(mt5: Any, symbol_info: Any, side: str) -> bool:
    mode = int(symbol_info.trade_mode)
    if mode == int(mt5.SYMBOL_TRADE_MODE_DISABLED) or mode == int(
        mt5.SYMBOL_TRADE_MODE_CLOSEONLY
    ):
        return False
    if side == "BUY" and mode == int(mt5.SYMBOL_TRADE_MODE_SHORTONLY):
        return False
    return not (side == "SELL" and mode == int(mt5.SYMBOL_TRADE_MODE_LONGONLY))


def _filling_mode(mt5: Any, symbol_info: Any) -> int:
    mode = int(getattr(symbol_info, "filling_mode", 0))
    if mode & int(getattr(mt5, "SYMBOL_FILLING_FOK", 1)):
        return int(mt5.ORDER_FILLING_FOK)
    if mode & int(getattr(mt5, "SYMBOL_FILLING_IOC", 2)):
        return int(mt5.ORDER_FILLING_IOC)
    return int(mt5.ORDER_FILLING_RETURN)


def account_check(mt, mode, armed, identity=None):
    a, terminal = mt.account_info(), mt.terminal_info()
    if a is None or terminal is None or not terminal.connected:
        raise DemoSafetyError("Account / terminal disconnected; no orders")
    if identity is not None and identity != (int(a.login), str(a.server)):
        raise DemoSafetyError("Account changed; restart and reconcile")
    if armed and (
        mode != "demo"
        or a.trade_mode != mt.ACCOUNT_TRADE_MODE_DEMO
        or a.currency != "USD"
        or a.margin_mode != mt.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
        or not a.trade_allowed
        or not a.trade_expert
        or not terminal.trade_allowed
    ):
        raise DemoSafetyError(
            "Arming requires explicit demo mode, a USD hedging DEMO account and trading permission"
        )
    return a


def fresh(tick, now, max_age):
    if tick is None:
        return False
    ts = float(getattr(tick, "time_msc", 0)) / 1000
    return (
        all(math.isfinite(v) for v in (ts, tick.bid, tick.ask))
        and 0 < tick.bid <= tick.ask
        and 0 <= now - ts <= max_age
    )


def snapshot(mt):
    ps, os = mt.positions_get(), mt.orders_get()
    if ps is None or os is None:
        raise DemoSafetyError("Broker exposure unavailable; fail closed")
    return ps, os


def save(path, state):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, allow_nan=False), encoding="utf-8")
    tmp.replace(path)
