"""Reproduce predeclared study + identified exploratory pocket follow-up."""

import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

import pandas as pd

from bot.config.loader import load_yaml
from bot.zones.candle_replay import benchmark
from bot.zones.feed import tick_minutes
from bot.zones.price_action import POLICIES
from bot.zones.replay import file_hash

PRIMARY = [
    ("replication", "2026-09-03", "2026-10-01"),
    ("early", "2026-10-01", "2026-10-29"),
    ("later", "2026-10-29", "2026-11-12"),
    ("forward", "2026-11-12", "2026-11-16"),
]
FRESH = ("untouched_aug", "2026-08-06", "2026-09-03")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="data/public_xau_ticks", type=Path)
    ap.add_argument("--out", default="reports/candle_replay_public", type=Path)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--download", action="store_true")
    ap.add_argument(
        "--phase", choices=("primary", "followup", "sensitivity", "all"), default="all"
    )
    args = ap.parse_args()
    inputs = json.loads(Path(__file__).with_name("inputs.json").read_text())
    args.cache.mkdir(exist_ok=True, parents=True)
    cfg = load_yaml(args.config)
    for phase in (
        ["primary", "followup", "sensitivity"] if args.phase == "all" else [args.phase]
    ):
        first_week = 36 if phase == "primary" else 32
        files = []
        for name, h in sorted(inputs["sha256"].items()):
            if int(name.split("_w")[1].split(".")[0]) < first_week:
                continue
            p = args.cache / name
            if not p.exists() and args.download:
                with urlopen(inputs["base"] + name, timeout=90) as r:
                    v = r.read()
                if hashlib.sha256(v).hexdigest() != h:
                    raise ValueError(f"Bad download {name}")
                p.write_bytes(v)
            if file_hash(p) != h:
                raise ValueError(f"Checksum mismatch {name}")
            files.append(p)
        bounds = PRIMARY if phase == "primary" else [FRESH, *PRIMARY]
        policies = POLICIES[:6] if phase == "primary" else POLICIES
        stop_fill = "barrier"
        if phase == "sensitivity":
            policies = (
                "baseline",
                "return_cash",
                "return_wick",
                "sweep_wick",
                "reaction_managed",
            )
            stop_fill = "first_quote"
        t = lambda a: int(pd.Timestamp(a, tz="UTC").timestamp())
        windows = [(w, t(a), t(b)) for w, a, b in bounds]
        dst = args.out / phase
        result = benchmark(
            tick_minutes(files),
            cfg,
            "XAUUSD",
            windows,
            policies=policies,
            stop_fill=stop_fill,
            out=dst,
            paired=phase != "sensitivity",
        )
        manifest = {
            "base": inputs["base"],
            "sha256": {p.name: inputs["sha256"][p.name] for p in files},
            "windows": bounds,
            "policies": policies,
            "config_sha256": hashlib.sha256(
                json.dumps(cfg, sort_keys=True).encode()
            ).hexdigest(),
            "zones": cfg["zones"],
            "stop_fill": stop_fill,
            "research_code": {
                p.name: file_hash(p)
                for p in sorted(Path("src/bot/zones").glob("*.py"))
                if p.name
                in (
                    "engine.py",
                    "candles.py",
                    "orders.py",
                    "feed.py",
                    "price_action.py",
                    "hours.py",
                    "candle_replay.py",
                )
            },
        }
        (dst / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        for window, rec in result.items():
            print(
                phase,
                window,
                [
                    (k, a["metrics"]["trades"], round(a["metrics"]["net_profit"], 2))
                    for k, a in rec["arms"].items()
                ],
                flush=True,
            )


if __name__ == "__main__":
    main()
