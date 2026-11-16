from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from bot.broker_interface import BrokerAdapter
from bot.models import AccountInfo, Candle, MT5Config, Position, Side, SymbolSpec, Tick

logger = logging.getLogger(__name__)


class MT5Adapter(BrokerAdapter):
    """
    Thread-safe MetaTrader 5 Desktop Python API adapter for Windows.
    Serializes all MT5 C-API calls via a re-entrant lock and handles
    broker-specific filling modes (IOC / FOK / RETURN) automatically.
    """

    def __init__(self, cfg: MT5Config) -> None:
        self.cfg = cfg
        self._lock = threading.RLock()
        self._mt5 = None
        self._connected = False

    def _import_mt5(self):
        if self._mt5 is None:
            try:
                import MetaTrader5 as mt5  # type: ignore

                self._mt5 = mt5
            except ImportError as exc:
                raise RuntimeError(
                    "MetaTrader5 Python package is not installed. Run 'pip install MetaTrader5' on Windows."
                ) from exc
        return self._mt5

    def connect(self) -> bool:
        with self._lock:
            mt5 = self._import_mt5()
            kwargs: dict = {}
            if self.cfg.terminal_path:
                kwargs["path"] = self.cfg.terminal_path
            if self.cfg.login:
                kwargs["login"] = int(self.cfg.login)
            if self.cfg.password:
                kwargs["password"] = self.cfg.password
            if self.cfg.server:
                kwargs["server"] = self.cfg.server

            ok = mt5.initialize(**kwargs) if kwargs else mt5.initialize()
            if not ok:
                logger.error("MT5 initialize() failed: %s", mt5.last_error())
                self._connected = False
                return False

            if not mt5.symbol_select(self.cfg.symbol, True):
                logger.error("MT5 failed to select symbol %s: %s", self.cfg.symbol, mt5.last_error())
                self._connected = False
                return False

            self._connected = True
            logger.info("Connected to MT5 terminal for symbol %s.", self.cfg.symbol)
            return True

    def ensure_connected(self) -> None:
        if not self._connected and self.cfg.auto_reconnect:
            self.connect()

    def disconnect(self) -> None:
        with self._lock:
            if self._mt5 is not None and self._connected:
                self._mt5.shutdown()
            self._connected = False

    def _resolve_filling_mode(self, symbol_info) -> int:
        mt5 = self._import_mt5()
        filling_mask = getattr(symbol_info, "filling_mode", 0)
        # SYMBOL_FILLING_IOC = 2, SYMBOL_FILLING_FOK = 1
        if filling_mask & 2:
            return mt5.ORDER_FILLING_IOC
        if filling_mask & 1:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        with self._lock:
            self.ensure_connected()
            mt5 = self._import_mt5()
            info = mt5.symbol_info(symbol)
            if info is None:
                raise RuntimeError(f"Symbol '{symbol}' not found in MT5 Market Watch.")
            if not info.visible:
                mt5.symbol_select(symbol, True)
                info = mt5.symbol_info(symbol)

            return SymbolSpec(
                symbol=symbol,
                point=float(info.point),
                digits=int(info.digits),
                trade_tick_size=float(info.trade_tick_size),
                trade_tick_value=float(info.trade_tick_value),
                trade_contract_size=float(info.trade_contract_size),
                volume_min=float(info.volume_min),
                volume_max=float(info.volume_max),
                volume_step=float(info.volume_step),
                stops_level=int(getattr(info, "trade_stops_level", 0)),
                filling_mode=self._resolve_filling_mode(info),
            )

    def get_account_info(self) -> AccountInfo:
        with self._lock:
            self.ensure_connected()
            mt5 = self._import_mt5()
            acc = mt5.account_info()
            if acc is None:
                raise RuntimeError(f"MT5 account_info() failed: {mt5.last_error()}")
            return AccountInfo(
                balance=float(acc.balance),
                equity=float(acc.equity),
                margin=float(acc.margin),
                margin_free=float(acc.margin_free),
                margin_level=float(getattr(acc, "margin_level", 0.0)),
                currency=str(getattr(acc, "currency", "USD")),
            )

    def get_latest_tick(self, symbol: str) -> Optional[Tick]:
        with self._lock:
            self.ensure_connected()
            mt5 = self._import_mt5()
            t = mt5.symbol_info_tick(symbol)
            if t is None:
                return None
            return Tick(
                time=datetime.fromtimestamp(t.time, tz=timezone.utc),
                bid=float(t.bid),
                ask=float(t.ask),
                last=float(getattr(t, "last", 0.0)),
                volume=float(getattr(t, "volume_real", getattr(t, "volume", 0.0))),
                time_msc=int(getattr(t, "time_msc", t.time * 1000)),
            )

    def get_ticks_from(self, symbol: str, from_time: datetime, count: int) -> list[Tick]:
        with self._lock:
            self.ensure_connected()
            mt5 = self._import_mt5()
            raw = mt5.copy_ticks_from(symbol, from_time, count, mt5.COPY_TICKS_ALL)
            if raw is None or len(raw) == 0:
                return []
            ticks: list[Tick] = []
            for row in raw:
                ticks.append(
                    Tick(
                        time=datetime.fromtimestamp(int(row["time"]), tz=timezone.utc),
                        bid=float(row["bid"]),
                        ask=float(row["ask"]),
                        last=float(row["last"]),
                        volume=float(row["volume"]),
                        time_msc=int(row["time_msc"]),
                    )
                )
            return ticks

    def get_m1_candles(self, symbol: str, count: int) -> list[Candle]:
        with self._lock:
            self.ensure_connected()
            mt5 = self._import_mt5()
            candles: list[Candle] = []
            remaining = max(0, int(count))
            start_pos = 0
            chunk_size = 10000
            while remaining > 0:
                request_count = min(chunk_size, remaining)
                rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, start_pos, request_count)
                if rates is None or len(rates) == 0:
                    break
                for r in rates:
                    candles.append(
                        Candle(
                            time=datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
                            open=float(r["open"]),
                            high=float(r["high"]),
                            low=float(r["low"]),
                            close=float(r["close"]),
                            tick_volume=int(r["tick_volume"]),
                            spread=int(r["spread"]),
                        )
                    )
                received = len(rates)
                start_pos += received
                remaining -= received
                if received < request_count:
                    break
            candles.sort(key=lambda candle: candle.time)
            deduped: dict[datetime, Candle] = {candle.time: candle for candle in candles}
            return list(deduped.values())

    def get_open_positions(self, symbol: str, magic: Optional[int] = None) -> list[Position]:
        with self._lock:
            self.ensure_connected()
            mt5 = self._import_mt5()
            positions = mt5.positions_get(symbol=symbol)
            if positions is None:
                return []
            out: list[Position] = []
            for p in positions:
                if magic is not None and int(p.magic) != magic:
                    continue
                side = Side.BUY if int(p.type) == mt5.POSITION_TYPE_BUY else Side.SELL
                out.append(
                    Position(
                        ticket=int(p.ticket),
                        symbol=str(p.symbol),
                        side=side,
                        volume=float(p.volume),
                        open_price=float(p.price_open),
                        sl=float(p.sl),
                        tp=float(p.tp),
                        profit_usd=float(p.profit),
                        swap_usd=float(getattr(p, "swap", 0.0)),
                        commission_usd=0.0,
                        magic=int(p.magic),
                        open_time=datetime.fromtimestamp(int(p.time), tz=timezone.utc),
                        comment=str(getattr(p, "comment", "")),
                    )
                )
            return out

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
        with self._lock:
            self.ensure_connected()
            mt5 = self._import_mt5()
            spec = self.get_symbol_spec(symbol)
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                logger.error("No live tick available to execute %s %s", side.value, symbol)
                return None

            price = float(tick.ask if side == Side.BUY else tick.bid)
            order_type = mt5.ORDER_TYPE_BUY if side == Side.BUY else mt5.ORDER_TYPE_SELL
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": float(volume),
                "type": order_type,
                "price": price,
                "sl": round(sl, spec.digits) if sl > 0 else 0.0,
                "tp": round(tp, spec.digits) if tp > 0 else 0.0,
                "deviation": int(deviation),
                "magic": int(magic),
                "comment": comment[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": spec.filling_mode,
            }
            result = mt5.order_send(request)
            if result is None:
                logger.error("order_send returned None: %s", mt5.last_error())
                return None
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                logger.error(
                    "order_send rejected (retcode=%s, comment=%s) req=%s",
                    result.retcode,
                    result.comment,
                    request,
                )
                return None
            return int(result.order)

    def modify_position_sl_tp(self, ticket: int, symbol: str, sl: float, tp: float) -> bool:
        with self._lock:
            self.ensure_connected()
            mt5 = self._import_mt5()
            spec = self.get_symbol_spec(symbol)
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": int(ticket),
                "symbol": symbol,
                "sl": round(sl, spec.digits) if sl > 0 else 0.0,
                "tp": round(tp, spec.digits) if tp > 0 else 0.0,
            }
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                logger.warning(
                    "SLTP modify failed for ticket %s: %s",
                    ticket,
                    None if result is None else f"{result.retcode} {result.comment}",
                )
                return False
            return True

    def close_position(self, pos: Position, deviation: int, comment: str) -> bool:
        with self._lock:
            self.ensure_connected()
            mt5 = self._import_mt5()
            spec = self.get_symbol_spec(pos.symbol)
            tick = mt5.symbol_info_tick(pos.symbol)
            if tick is None:
                return False

            close_type = mt5.ORDER_TYPE_SELL if pos.side == Side.BUY else mt5.ORDER_TYPE_BUY
            close_price = float(tick.bid if pos.side == Side.BUY else tick.ask)
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": float(pos.volume),
                "type": close_type,
                "position": int(pos.ticket),
                "price": close_price,
                "deviation": int(deviation),
                "magic": int(pos.magic),
                "comment": comment[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": spec.filling_mode,
            }
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                logger.error(
                    "Close position failed for ticket %s: %s",
                    pos.ticket,
                    None if result is None else f"{result.retcode} {result.comment}",
                )
                return False
            return True
