"""Patch on_bar_close to handle 6-tuple from updated _evaluate_sr_zone
and add CONSOL_BREAK_UP / CONSOL_BREAK_DOWN handling."""
import re
from pathlib import Path

FILE = Path("bot/structure_momentum_strategy.py")
content = FILE.read_text(encoding="utf-8")

OLD = '        if self.cfg.use_sr_zones:\n            sr_low, sr_high, sr_pos, sr_zone = self._evaluate_sr_zone(candle)\n            if sr_zone == "AT_SUPPORT":\n                if candle.close > candle.open:\n                    sr_side = Side.BUY\n                    sr_tag = "SR_SUPPORT_BOUNCE"\n                else:\n                    self._record_skip("at_support_block_sell")\n            elif sr_zone == "AT_RESISTANCE":\n                if candle.close < candle.open:\n                    sr_side = Side.SELL\n                    sr_tag = "SR_RESISTANCE_REJECT"\n                else:\n                    self._record_skip("at_resistance_block_buy")\n            elif sr_zone == "BREAKOUT_UP":\n                sr_side = Side.BUY\n                sr_tag = "SR_BREAKOUT_UP"\n            elif sr_zone == "BREAKDOWN_DOWN":\n                sr_side = Side.SELL\n                sr_tag = "SR_BREAKDOWN_DOWN"\n            elif sr_zone == "IN_BETWEEN":\n                if snapshot.internal_trend > 0 and candle.close > candle.open:\n                    sr_side = Side.BUY\n                    sr_tag = "SR_FLOW_UP"\n                elif snapshot.internal_trend < 0 and candle.close < candle.open:\n                    sr_side = Side.SELL\n                    sr_tag = "SR_FLOW_DOWN"'

NEW = '        if self.cfg.use_sr_zones:\n            sr_low, sr_high, sr_pos, sr_zone, sr_consol, sr_consol_range = self._evaluate_sr_zone(candle)\n            # Store SR context so the engine can pass it to the learning logger\n            self._last_sr = {\n                "sr_low": sr_low, "sr_high": sr_high,\n                "pos_ratio": sr_pos, "zone_state": sr_zone,\n                "consol_detected": sr_consol, "consol_range_points": sr_consol_range,\n            }\n            if sr_zone in ("CONSOL_BREAK_UP", "BREAKOUT_UP"):\n                # High-confidence BUY — price broke above prior range/consolidation\n                sr_side = Side.BUY\n                sr_tag = sr_zone  # "CONSOL_BREAK_UP" or "BREAKOUT_UP"\n            elif sr_zone in ("CONSOL_BREAK_DOWN", "BREAKDOWN_DOWN"):\n                # High-confidence SELL — price broke below prior range/consolidation\n                sr_side = Side.SELL\n                sr_tag = sr_zone  # "CONSOL_BREAK_DOWN" or "BREAKDOWN_DOWN"\n            elif sr_zone == "AT_SUPPORT":\n                if candle.close > candle.open:\n                    sr_side = Side.BUY\n                    sr_tag = "SR_SUPPORT_BOUNCE"\n                else:\n                    self._record_skip("at_support_block_sell")\n            elif sr_zone == "AT_RESISTANCE":\n                if candle.close < candle.open:\n                    sr_side = Side.SELL\n                    sr_tag = "SR_RESISTANCE_REJECT"\n                else:\n                    self._record_skip("at_resistance_block_buy")\n            elif sr_zone == "IN_BETWEEN":\n                if snapshot.internal_trend > 0 and candle.close > candle.open:\n                    sr_side = Side.BUY\n                    sr_tag = "SR_FLOW_UP"\n                elif snapshot.internal_trend < 0 and candle.close < candle.open:\n                    sr_side = Side.SELL\n                    sr_tag = "SR_FLOW_DOWN"'

# Normalize to \n for matching (file may use \r\n or \n)
content_norm = content.replace('\r\n', '\n')
OLD_norm = OLD.replace('\r\n', '\n')
NEW_norm = NEW.replace('\r\n', '\n')

if OLD_norm not in content_norm:
    print("ERROR: could not find SR zone block in on_bar_close — check for drift")
    # Print what's there
    idx = content_norm.find('if self.cfg.use_sr_zones:')
    if idx >= 0:
        print("Found use_sr_zones block at:", idx)
        print(repr(content_norm[idx:idx+300]))
    exit(1)

new_content = content_norm.replace(OLD_norm, NEW_norm, 1)
FILE.write_text(new_content, encoding="utf-8")
print(f"Patched on_bar_close SR zone block ({len(OLD_norm)} -> {len(NEW_norm)} chars).")
