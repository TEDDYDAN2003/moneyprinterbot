"""Wire TradeLearningLogger into bot/engine.py:
1. Import TradeLearningLogger and SRSnapshot.
2. Instantiate self.learning in LiveTradingEngine.__init__.
3. Call self.learning.log_open(...) in _execute_signal for each executed order.
4. Call self.learning.log_close(...) in _manage_open_positions for:
   - natural closes (broker SL/TP)
   - dollar exits (Dollar TP / Dollar SL)
   - daily circuit breakers
"""
import re
from pathlib import Path

FILE = Path("bot/engine.py")
content = FILE.read_text(encoding="utf-8")

# 1. Add import
OLD_IMPORT = "from bot.market_intel import MarketIntelLogger"
NEW_IMPORT = """from bot.market_intel import MarketIntelLogger
from bot.trade_learning_log import TradeLearningLogger, SRSnapshot"""

if OLD_IMPORT not in content:
    print("ERROR: old import not found")
    exit(1)
content = content.replace(OLD_IMPORT, NEW_IMPORT, 1)

# 2. Add self.learning to __init__
OLD_INIT = """        self.market_intel = MarketIntelLogger(
            cfg.logging.market_intel_enabled,
            cfg.logging.market_intel_path,
        )"""

NEW_INIT = """        self.market_intel = MarketIntelLogger(
            cfg.logging.market_intel_enabled,
            cfg.logging.market_intel_path,
        )
        self.learning = TradeLearningLogger(
            enabled=True,
            path="reports/trade_learning.jsonl",
        )"""

if OLD_INIT not in content:
    print("ERROR: old init not found")
    exit(1)
content = content.replace(OLD_INIT, NEW_INIT, 1)

# 3. Add helper method to LiveTradingEngine for getting current SR snapshot
HELPER_METHOD = '''
    def _current_sr_snapshot(self) -> SRSnapshot:
        """Extracts current SR context from strategy if available."""
        sr_dict = getattr(self.strategy, "_last_sr", {})
        return SRSnapshot(
            sr_low=sr_dict.get("sr_low"),
            sr_high=sr_dict.get("sr_high"),
            pos_ratio=sr_dict.get("pos_ratio", 0.5),
            zone_state=sr_dict.get("zone_state", "IN_BETWEEN"),
            consol_detected=sr_dict.get("consol_detected", False),
            consol_range_points=sr_dict.get("consol_range_points", 0.0),
        )
'''

# Insert helper before _execute_signal
OLD_EXEC_SIG_HEADER = "    def _execute_signal(self, sig: TradeSignal, tick: Tick) -> None:"
if OLD_EXEC_SIG_HEADER not in content:
    print("ERROR: _execute_signal header not found")
    exit(1)
content = content.replace(OLD_EXEC_SIG_HEADER, HELPER_METHOD + "\n" + OLD_EXEC_SIG_HEADER, 1)

# 4. In _execute_signal, log OPEN after open_market_order succeeds
OLD_ORDER_RECORD = """            if ticket is not None:
                executed_tickets.append(ticket)
                self.risk.record_trade_open(tick.time)"""

NEW_ORDER_RECORD = """            if ticket is not None:
                executed_tickets.append(ticket)
                self.risk.record_trade_open(tick.time)
                # Trade Learning: record open with full market context
                try:
                    candles = self.broker.get_m1_candles(self.spec.symbol, 2)
                    last_c = candles[-2] if len(candles) >= 2 else (candles[-1] if candles else None)
                    if last_c is not None:
                        self.learning.log_open(
                            ticket=ticket,
                            signal=sig,
                            lots=cur_lots,
                            candle=last_c,
                            structure_snap=getattr(self.strategy, "last_snapshot", None),
                            sr_snap=self._current_sr_snapshot(),
                            spread_points=tick.spread_points(self.spec.point),
                            pace_mode=pace,
                            cfg=self.cfg,
                        )
                except Exception as _learn_err:
                    logger.debug("TradeLearning open log error: %s", _learn_err)"""

if OLD_ORDER_RECORD not in content:
    print("ERROR: old order record block not found")
    exit(1)
content = content.replace(OLD_ORDER_RECORD, NEW_ORDER_RECORD, 1)

# 5. In _manage_open_positions, log natural broker close (SL/TP)
OLD_NATURAL_CLOSE = """                self._daily_realized_pnl_usd += old_pos.net_pnl_usd
                self.risk.record_trade_close(tick.time, old_pos.net_pnl_usd)"""

NEW_NATURAL_CLOSE = """                self._daily_realized_pnl_usd += old_pos.net_pnl_usd
                self.risk.record_trade_close(tick.time, old_pos.net_pnl_usd)
                # Trade Learning: record natural close (broker SL or TP hit)
                exit_price = tick.bid if old_pos.side == Side.BUY else tick.ask
                nat_reason = "BROKER_TP" if old_pos.net_pnl_usd > 0 else "BROKER_SL"
                self.learning.log_close(
                    ticket=old_ticket,
                    pnl_usd=old_pos.net_pnl_usd,
                    exit_price=exit_price,
                    exit_reason=nat_reason,
                    sr_snap=self._current_sr_snapshot(),
                    cfg=self.cfg,
                )"""

if OLD_NATURAL_CLOSE not in content:
    print("ERROR: old natural close not found")
    exit(1)
content = content.replace(OLD_NATURAL_CLOSE, NEW_NATURAL_CLOSE, 1)

# 6. In _manage_open_positions, log Dollar TP/SL ticket closes
OLD_TICKET_CLOSE = """                    self._daily_realized_pnl_usd += pos.net_pnl_usd
                    self.risk.record_trade_close(tick.time, pos.net_pnl_usd)
                    self._known_positions.pop(ticket, None)"""

NEW_TICKET_CLOSE = """                    self._daily_realized_pnl_usd += pos.net_pnl_usd
                    self.risk.record_trade_close(tick.time, pos.net_pnl_usd)
                    self._known_positions.pop(ticket, None)
                    # Trade Learning: record auto-closed trade
                    exit_price = tick.bid if pos.side == Side.BUY else tick.ask
                    self.learning.log_close(
                        ticket=ticket,
                        pnl_usd=pos.net_pnl_usd,
                        exit_price=exit_price,
                        exit_reason=reason,
                        sr_snap=self._current_sr_snapshot(),
                        cfg=self.cfg,
                    )"""

if OLD_TICKET_CLOSE not in content:
    print("ERROR: old ticket close not found")
    exit(1)
content = content.replace(OLD_TICKET_CLOSE, NEW_TICKET_CLOSE, 1)

FILE.write_text(content, encoding="utf-8")
print("Successfully wired TradeLearningLogger into bot/engine.py!")
