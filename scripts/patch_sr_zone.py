"""Patch the _evaluate_sr_zone method in structure_momentum_strategy.py
to add consolidation breakout detection."""
import re
from pathlib import Path

FILE = Path("bot/structure_momentum_strategy.py")
content = FILE.read_text(encoding="utf-8")

NEW_METHOD = '''    def _evaluate_sr_zone(self, candle: Candle) -> tuple[Optional[float], Optional[float], float, str, bool, float]:
        """
        Calculates Support/Resistance zone from recent M1 candles and detects
        consolidation breakouts (like tight range -> sharp impulse move).

        Returns: (sr_low, sr_high, pos_ratio, zone_state, consol_detected, consol_range)

        zone_state values:
          "AT_SUPPORT"        -- price near bottom of range; prefer BUY
          "AT_RESISTANCE"     -- price near top of range; prefer SELL
          "BREAKOUT_UP"       -- close above prior range high; aggressive BUY
          "BREAKDOWN_DOWN"    -- close below prior range low; aggressive SELL
          "CONSOL_BREAK_UP"   -- broke above a tight consolidation zone (high-confidence BUY)
          "CONSOL_BREAK_DOWN" -- broke below a tight consolidation zone (high-confidence SELL)
          "IN_BETWEEN"        -- mid-range; follow momentum trend
        """
        candles = self.m1.candles
        lookback = max(5, self.cfg.sr_lookback_bars)
        if len(candles) < 5:
            return None, None, 0.5, "IN_BETWEEN", False, 0.0

        window = candles[-lookback:]
        # Exclude the just-closed candle so we evaluate the PRIOR range
        eval_window = window[:-1] if len(window) > 1 else window

        sr_high = max(c.high for c in eval_window)
        sr_low = min(c.low for c in eval_window)
        cur = candle.close
        rng = sr_high - sr_low

        # -- Consolidation detection -----------------------------------------
        # Look at the most recent consol_bars candles in eval_window to see if
        # price was coiling in a tighter-than-average range before this candle.
        consol_detected = False
        consol_range = 0.0
        consol_bars = min(10, max(5, len(eval_window) // 3))
        recent_slice = eval_window[-consol_bars:] if len(eval_window) >= consol_bars else eval_window
        if len(recent_slice) >= 3:
            consol_high = max(c.high for c in recent_slice)
            consol_low = min(c.low for c in recent_slice)
            consol_range = consol_high - consol_low
            # Tight consolidation: recent range < 40% of full lookback range AND
            # the current candle closes OUTSIDE that tight range
            tight_threshold = max(0.001, rng * 0.40) if rng > 0 else 0.001
            if consol_range > 0 and consol_range < tight_threshold:
                consol_detected = True
                # Did we just break OUT of that consolidation on this candle?
                if candle.close > consol_high and candle.close > candle.open:
                    return sr_low, sr_high, 1.0, "CONSOL_BREAK_UP", True, consol_range
                if candle.close < consol_low and candle.close < candle.open:
                    return sr_low, sr_high, 0.0, "CONSOL_BREAK_DOWN", True, consol_range

        # -- Standard breakout check -----------------------------------------
        if cur > sr_high:
            return sr_low, sr_high, 1.0, "BREAKOUT_UP", consol_detected, consol_range
        if cur < sr_low:
            return sr_low, sr_high, 0.0, "BREAKDOWN_DOWN", consol_detected, consol_range

        if rng <= 0:
            return sr_low, sr_high, 0.5, "IN_BETWEEN", consol_detected, consol_range

        pos = (cur - sr_low) / rng
        threshold = max(0.10, min(0.45, self.cfg.sr_zone_pct))
        if pos <= threshold:
            return sr_low, sr_high, pos, "AT_SUPPORT", consol_detected, consol_range
        if pos >= (1.0 - threshold):
            return sr_low, sr_high, pos, "AT_RESISTANCE", consol_detected, consol_range
        return sr_low, sr_high, pos, "IN_BETWEEN", consol_detected, consol_range
'''

# Match the old method using a pattern
pattern = re.compile(
    r'    def _evaluate_sr_zone\(self, candle: Candle\).*?'
    r'return sr_low, sr_high, pos, "IN_BETWEEN"\r?\n',
    re.DOTALL,
)
match = pattern.search(content)
if not match:
    print("ERROR: could not find _evaluate_sr_zone method in file")
    exit(1)

new_content = content[:match.start()] + NEW_METHOD + "\n" + content[match.end():]
FILE.write_text(new_content, encoding="utf-8")
print(f"Patched _evaluate_sr_zone (chars {match.start()}-{match.end()} replaced).")
