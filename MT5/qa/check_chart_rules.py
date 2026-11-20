"""Independent assertions on the actual current Pine output. Synthetic OHLC; not broker fills."""

import json, sys, math
from collections import Counter
from pathlib import Path


def check(path):
    tape = json.loads(Path(path).read_text())
    assert tape["policy"] == "bos_half_range_close_v4"
    counts = Counter()
    for case in tape["cases"]:
        prev = None
        for i, (bar, row) in enumerate(
            zip(tape["fixtures"][case["fixture"]], case["rows"], strict=True)
        ):
            r = dict(zip(tape["keys"], row, strict=True))
            t, o, h, l, c = bar
            d = r["selected"]
            bias = r["bias"]
            mid = r["midRange"]
            guard = r["origin"]
            tip = r["extreme"]
            ready = (
                r["eventKind"] == 3
                and r["guardEvent"] == 0
                and tip is not None
                and guard is not None
                and r["broken"] is not None
                and bias * (tip - r["broken"]) > 0
                and bias * (tip - guard) > 0
            )
            assert (mid is not None) == ready
            if mid is not None:
                assert math.isclose(mid, (guard + tip) / 2, abs_tol=5.1e-10, rel_tol=0)
            bz = ready and bias == 1
            sz = ready and bias == -1
            assert (r["buyLo"], r["buyHi"]) == ((guard, mid) if bz else (None, None))
            assert (r["sellLo"], r["sellHi"]) == ((mid, guard) if sz else (None, None))
            tr = (
                1
                if bz and d == 1 and guard < c <= mid
                else -1
                if sz and d == -1 and mid <= c < guard
                else 0
            )
            assert (
                r["structureSide"] == tr
                and r["suggestion"] in (0, tr)
                and r["pendingSide"] == r["suggestion"]
            )
            if r["suggestion"]:
                counts["trend" + str(d)] += 1
            if abs(r["guardEvent"]) == 2:
                assert not tr and not r["pendingSide"] and not r["riskQualified"]
                counts["choch_context_only"] += 1
            if r["filledSide"]:
                assert (
                    prev
                    and prev["pendingSide"] == prev["suggestion"] == r["filledSide"]
                    and t == prev["t"] + 60
                )
                px = r["simFillPx"]
                assert (
                    prev["buyLo"] < px <= prev["buyHi"]
                    if r["filledSide"] == 1
                    else prev["sellLo"] <= px < prev["sellHi"]
                )
            if case["name"].startswith("no-reversal-") and i in (410, 418, 419, 420):
                assert (
                    not r["structureSide"]
                    and not r["pendingSide"]
                    and not r["filledSide"]
                )
                if i == 418:
                    assert abs(r["guardEvent"]) == 2 and d == 0
            if case["name"] in ("protected-buy", "protected-sell"):
                if i == 45:
                    assert (
                        abs(r["guardEvent"]) == 2
                        and d == bias
                        and not r["suggestion"]
                        and not r["pendingSide"]
                    )
                if i == 92:
                    assert r["suggestion"] == d == bias and d != 0
            prev = r | {"t": t}
            counts["bars"] += 1
    assert min(counts[x] for x in ("trend1", "trend-1", "choch_context_only")) > 0
    print("PASS v4 semantic invariants", dict(counts))


if __name__ == "__main__":
    check(sys.argv[1])
