"""Compare the archived v4 closed-cross lane to the current v4 Pine source on identical OHLC.

Run from a full checkout with bot dependencies installed; the MQ5 shim is checked by check_port.py.
A chart's one-position fills are deliberately NOT used as broker executions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from bot.hull.v4.config import Indicator
from bot.hull.v4.signal import Hull
from bot.zones.engine import Bar


def main(path):
    tape = json.loads(Path(path).read_text())
    if tape.get("policy") != "bos_half_range_close_v4":
        raise SystemExit("Use the current v4 chart oracle.")
    gate_count = 0
    total = 0
    for case in tape["cases"]:
        opts = case["opts"]
        h = Hull(
            1,
            Indicator(
                hull_len=opts.get("hullLen", 16),
                swing_len=opts.get("swingLen", 3),
                atr_length=opts.get("atrLength", 14),
                pullback_bars=opts.get("pullbackBars", 7),
                stop="confirmed_pivot"
                if opts.get("stopMode", "").startswith("Confirmed")
                else "local_pullback",
                signal="ema_plan" if opts.get("useEma") else "labels",
                ema_length=opts.get("emaLength", 200),
            ),
            0.01,
        )
        source = opts.get("sourceMode", "Either source")
        prev = 0
        for i, (row, values) in enumerate(
            zip(tape["fixtures"][case["fixture"]], case["rows"], strict=True)
        ):
            b = Bar(*row)
            r = h.update(b)
            s = r.structure
            p = dict(zip(tape["keys"], values, strict=True))
            if source == "Either source":
                direction = (
                    r.label
                )  # production Either decision, not recomputed from the chart
            elif source == "Directional":
                buy, sell = r.cross_low == 1, r.cross_high == -1
                direction = int(buy) - int(sell)
            else:
                direction = r.cross_low if source == "Low only" else r.cross_high
            if opts.get("alternateOnly") and prev == direction:
                direction = 0
            if direction:
                prev = direction
            bb, sb = s.band(1), s.band(-1)
            kind = {"none": 0, "seed": 1, "choch": 2, "bos": 3}[s.event]
            expected = {
                "selected": direction,
                "midRange": s.midpoint,
                "structureSide": direction
                if direction and not s.veto(direction, b.c)
                else 0,
                "eventKind": kind,
                "bias": s.bias,
                "origin": s.origin.price if s.origin else None,
                "broken": s.broken.price if s.broken else None,
                "extreme": s.extreme.price if s.extreme else None,
                "reaction": s.pullback.price if s.pullback else None,
                "runTip": s.impulse_tip.price if s.impulse_tip else None,
                "legOuter": h.structure.outer.price if h.structure.outer else None,
                "guardSeen": int(s.pullback_seen_s is not None),
                "guardEvent": kind * s.bias if s.break_now else 0,
                "buyLo": bb.low if bb else None,
                "buyHi": bb.high if bb else None,
                "sellLo": sb.low if sb else None,
                "sellHi": sb.high if sb else None,
            }
            # Exact comparisons: structure levels are copied OHLC wicks, not fitted floats.
            for key, value in expected.items():
                assert p[key] == value, (case["name"], i, key, value, p[key])
            # Bot has no chart-only alternation, side visibility or simulated slot. Compare
            # its actual stop/EMA/structure gate before occupancy under matching settings.
            if source == "Either source" and not opts.get("alternateOnly"):
                gate = r.entry_side
                if not opts.get("allowShort", True) and gate == -1:
                    gate = 0
                assert gate == p["riskQualified"], (
                    case["name"],
                    i,
                    gate,
                    p["riskQualified"],
                    r.entry_reason,
                )
                gate_count += 1
            if s.event == "choch":
                assert not r.entry_side and not s.band(1) and not s.band(-1)
            total += 1
    print(
        f"PASS: Python / Pine raw and structure decisions, 17 exact fields on {total:,} closed bars; {gate_count:,} comparable stop/EMA/entry decisions."
    )


if __name__ == "__main__":
    main(sys.argv[1])
