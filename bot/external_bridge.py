from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from bot.models import ExternalBridgeConfig, ExternalPriceSignal, Side, Tick, TradeSignal

logger = logging.getLogger(__name__)


class ExternalSignalBridge:
    """
    Watches an external signal file (e.g., updated by an MT5 MQL5 indicator or
    TradingView alert webhook) containing explicit Entry / SL / TP prices.
    As live ticks arrive, checks if market price reaches the configured entry
    level and emits a standardized TradeSignal for the Risk & Execution engine.
    """

    def __init__(self, cfg: ExternalBridgeConfig, symbol: str, point: float) -> None:
        self.cfg = cfg
        self.symbol = symbol
        self.point = point
        self.signal_file = Path(cfg.signal_file)
        self._lock = threading.Lock()
        self._signals: dict[str, ExternalPriceSignal] = {}
        self._consumed_ids: set[str] = set()
        self._last_file_mtime: float = 0.0
        self._http_server: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None

    def start_webhook_if_enabled(self) -> None:
        if not self.cfg.webhook_enabled:
            return
        bridge = self

        class _WebhookHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                logger.debug("Webhook: " + format, *args)

            def do_POST(self) -> None:  # noqa: N802
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    raw_body = self.rfile.read(length).decode("utf-8")
                    payload = json.loads(raw_body)
                    if bridge.cfg.webhook_secret:
                        secret = payload.get("secret", "")
                        if secret != bridge.cfg.webhook_secret:
                            self.send_response(403)
                            self.end_headers()
                            self.wfile.write(b'{"error":"forbidden"}')
                            return

                    sig = bridge._parse_signal_dict(payload)
                    if sig is None:
                        self.send_response(400)
                        self.end_headers()
                        self.wfile.write(b'{"error":"invalid_signal_payload"}')
                        return

                    bridge.add_signal(sig)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"status":"accepted"}')
                except Exception as exc:
                    logger.exception("Webhook error: %s", exc)
                    self.send_response(500)
                    self.end_headers()

        self._http_server = ThreadingHTTPServer(
            (self.cfg.webhook_host, self.cfg.webhook_port),
            _WebhookHandler,
        )
        self._http_thread = threading.Thread(
            target=self._http_server.serve_forever,
            name="TradingViewWebhookServer",
            daemon=True,
        )
        self._http_thread.start()
        logger.info(
            "External TradingView webhook server listening on http://%s:%s",
            self.cfg.webhook_host,
            self.cfg.webhook_port,
        )

    def stop(self) -> None:
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
            self._http_server = None

    def _parse_signal_dict(self, item: dict) -> Optional[ExternalPriceSignal]:
        try:
            symbol = str(item.get("symbol", self.symbol)).strip()
            side_raw = str(item["side"]).strip().upper()
            if side_raw in ("LONG", "BUY"):
                side = Side.BUY
            elif side_raw in ("SHORT", "SELL"):
                side = Side.SELL
            else:
                return None

            sl_price = float(item["sl_price"])
            tp_price = float(item["tp_price"])
            entry_raw = item.get("entry_price")
            entry_price = float(entry_raw) if entry_raw is not None else None
            tolerance = float(item.get("tolerance_points", 15.0))
            signal_id = str(item.get("id") or uuid.uuid4().hex[:10])
            tag = str(item.get("tag", "ExternalBridge"))

            expires_at: Optional[datetime] = None
            if item.get("expires_at"):
                expires_at = datetime.fromisoformat(str(item["expires_at"]).replace("Z", "+00:00"))
            else:
                expires_at = datetime.now(timezone.utc) + timedelta(
                    minutes=self.cfg.default_expiry_minutes
                )

            return ExternalPriceSignal(
                signal_id=signal_id,
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                sl_price=sl_price,
                tp_price=tp_price,
                tolerance_points=tolerance,
                expires_at=expires_at,
                tag=tag,
            )
        except (KeyError, TypeError, ValueError):
            return None

    def add_signal(self, signal: ExternalPriceSignal) -> None:
        with self._lock:
            if signal.signal_id in self._consumed_ids:
                return
            self._signals[signal.signal_id] = signal
            logger.info(
                "Loaded external signal %s: %s %s entry=%s sl=%s tp=%s",
                signal.signal_id,
                signal.side.value,
                signal.symbol,
                signal.entry_price,
                signal.sl_price,
                signal.tp_price,
            )

    def poll_signal_file(self) -> None:
        if not self.signal_file.exists():
            return
        try:
            stat = self.signal_file.stat()
            if stat.st_mtime <= self._last_file_mtime:
                return
            self._last_file_mtime = stat.st_mtime
            raw = json.loads(self.signal_file.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else raw.get("signals", [raw])
            for item in items:
                if isinstance(item, dict):
                    sig = self._parse_signal_dict(item)
                    if sig is not None:
                        self.add_signal(sig)
        except Exception as exc:
            logger.warning("Failed reading external signal file %s: %s", self.signal_file, exc)

    def evaluate_on_tick(self, tick: Tick) -> list[TradeSignal]:
        self.poll_signal_file()
        now = tick.time if tick.time.tzinfo else tick.time.replace(tzinfo=timezone.utc)
        triggered: list[TradeSignal] = []

        with self._lock:
            to_remove: list[str] = []
            for sig_id, sig in self._signals.items():
                if sig.consumed:
                    to_remove.append(sig_id)
                    continue
                if sig.expires_at is not None:
                    exp = sig.expires_at if sig.expires_at.tzinfo else sig.expires_at.replace(tzinfo=timezone.utc)
                    if now > exp:
                        to_remove.append(sig_id)
                        continue
                if sig.symbol.upper() != self.symbol.upper():
                    continue

                market_price = tick.ask if sig.side == Side.BUY else tick.bid
                tol_price = sig.tolerance_points * self.point

                if sig.entry_price is None:
                    hit = True
                elif sig.side == Side.BUY:
                    # Wait for price to reach or pull back into configured entry price +/- tolerance
                    hit = market_price <= sig.entry_price + tol_price and market_price > sig.sl_price
                else:
                    hit = market_price >= sig.entry_price - tol_price and market_price < sig.sl_price

                if hit:
                    risk = (
                        (market_price - sig.sl_price)
                        if sig.side == Side.BUY
                        else (sig.sl_price - market_price)
                    )
                    reward = (
                        (sig.tp_price - market_price)
                        if sig.side == Side.BUY
                        else (market_price - sig.tp_price)
                    )
                    if risk > 0 and reward > 0:
                        triggered.append(
                            TradeSignal(
                                signal_id=sig.signal_id,
                                side=sig.side,
                                entry_price=market_price,
                                sl_price=sig.sl_price,
                                tp_price=sig.tp_price,
                                rr=reward / risk,
                                tag=sig.tag,
                                source="external_bridge",
                                created_time=now,
                                is_immediate=(sig.entry_price is None),
                            )
                        )
                    sig.consumed = True
                    self._consumed_ids.add(sig_id)
                    to_remove.append(sig_id)

            for sig_id in to_remove:
                self._signals.pop(sig_id, None)

        return triggered
