"""Reconcile stored ledgers without a terminal or raw quotes."""

import csv
import json
import math
from collections import Counter
from datetime import datetime
from hashlib import sha256
from pathlib import Path


def verify(root):
    count = 0
    for phase in ("primary", "followup", "sensitivity"):
        folder = root / phase
        manifest = json.loads((folder / "manifest.json").read_text())
        for f, h in manifest["research_code"].items():
            assert sha256((Path("src/bot/zones") / f).read_bytes()).hexdigest() == h, f
        for name, _, _ in manifest["windows"]:
            summary = json.loads((folder / name / "summary.json").read_text())
            archive = {}
            for policy in manifest["policies"]:
                arm = summary["arms"][policy]
                m = arm["metrics"]
                p = folder / name / (policy + ".csv")
                with p.open() as f:
                    rows = list(csv.DictReader(f))
                archive[policy] = rows
                net = eq = peak = dd = gp = gl = 0.0
                wins = 0
                for r in rows:
                    signal, entry, end = [
                        datetime.fromisoformat(r[k])
                        for k in ("signal_utc", "entry_utc", "exit_utc")
                    ]
                    assert signal <= entry <= end
                    assert (
                        0 < float(r["risk_usd"]) <= 5.000001
                        and float(r["lots"]) == 0.09
                    )
                    if policy != "baseline":
                        assert (entry - signal).total_seconds() < 180
                    sign = 1 if r["side"] == "BUY" else -1
                    risk = sign * (float(r["fill"]) - float(r["sl"])) * 9
                    val = float(r["net_usd"])
                    assert math.isclose(risk, float(r["risk_usd"]), abs_tol=1e-7)
                    assert (
                        abs(val - sign * (float(r["exit_fill"]) - float(r["fill"])) * 9)
                        < 1e-7
                    )
                    if manifest["stop_fill"] == "barrier" and r["exit"] == "stop":
                        assert abs(float(r["sl"]) - float(r["exit_fill"])) < 1e-9
                    gp += max(val, 0)
                    gl += max(-val, 0)
                    net += val
                    eq += val
                    peak = max(peak, eq)
                    dd = max(dd, peak - eq)
                    wins += val > 0
                assert (
                    m["trades"] == len(rows)
                    and m["wins"] == wins
                    and abs(m["net_profit"] - net) < 1e-7
                    and abs(m["max_drawdown"] - dd) < 1e-7
                )
                assert Counter(x["exit"] for x in rows) == Counter(arm["exits"])
                if gl:
                    assert abs(m["profit_factor"] - gp / gl) < 1e-8
                else:
                    assert m["profit_factor"] is None
                if not rows:
                    assert m["win_rate"] is None and m["expectancy_usd"] is None
                count += 1
            if phase in ("primary", "followup"):
                with (folder / name / "unbounded_same_entries.csv").open() as f:
                    paired = list(csv.DictReader(f))

                def keys(rows):
                    return sorted(
                        (r["signal_utc"], r["entry_utc"], r["side"], r["fill"], r["tp"])
                        for r in rows
                    )

                assert keys(paired) == keys(archive["baseline"])
                assert (
                    sum(float(r["risk_usd"]) > 5.000001 for r in paired)
                    == summary["unbounded_same_entries_diagnostic"]["over_budget"]
                )
            if phase == "primary" and name in ("early", "later"):
                old = (
                    Path("research/zone_pilot/results")
                    / (name + "-zones")
                    / "trades.csv"
                )
                with old.open() as f:
                    base = list(csv.DictReader(f))
                assert len(base) == len(archive["baseline"])
                for a, b in zip(base, archive["baseline"], strict=True):
                    assert (a["entry_utc"], a["exit_utc"]) == (
                        b["entry_utc"],
                        b["exit_utc"],
                    )
                    assert abs(float(a["net_usd"]) - float(b["net_usd"])) < 1e-8
    return count


if __name__ == "__main__":
    print(
        "Verified",
        verify(Path(__file__).with_name("evidence")),
        "funded-policy window ledgers; paired entry cohorts and original Oct/Nov controls reconcile.",
    )
