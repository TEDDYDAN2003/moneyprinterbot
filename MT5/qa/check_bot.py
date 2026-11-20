"""Compare the unchanged production Python lane to the archived v2 Pine oracle on identical OHLC.

Run from a full checkout with bot dependencies installed; the MQ5 shim is checked by check_port.py.
A chart's one-position fills are deliberately NOT used as broker executions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from bot.hull.config import Indicator
from bot.hull.signal import Hull
from bot.zones.engine import Bar


def main(path):
    tape = json.loads(Path(path).read_text())
    if tape.get("policy") == "momentum_retest_v3":
        raise SystemExit("The scanner remains v2. Generate its oracle from qa/reference.pine, not the v3 chart.")
    total = 0
    for case in tape["cases"]:
        opts = case["opts"]
        h = Hull(
            1,
            Indicator(
                hull_len=opts.get("hullLen", 16), swing_len=opts.get("swingLen", 3)
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
            # Independent, hand-specified acceptance observations (not generated from the ports).
            if case["name"].startswith("protected-"):
                d = -1 if "sell" in case["name"] else 1
                if i in (45, 92):
                    assert (
                        r.entry_side
                        == p["selected"]
                        == p["structureSide"]
                        == p["suggestion"]
                        == d
                    )
                    assert s.bias == d and s.event == ("choch" if i == 45 else "bos")
                if i == 46:
                    touch = case["name"].endswith("touch")
                    assert p["filledSide"] == (0 if touch else d) and p[
                        "openVetoCount"
                    ] == int(touch)
                if i == 92:
                    assert (s.origin.price, r.close) == (
                        (3682.84, 3677.159) if d == -1 else (3677.16, 3682.841)
                    )
                if i == 93:
                    assert p["filledSide"] == d
            total += 1
    print(
        f"PASS: Python / Pine raw and structure decisions, 16 exact fields on {total:,} closed bars; named regression and frozen-quote vetoes."
    )


if __name__ == "__main__":
    main(sys.argv[1])
