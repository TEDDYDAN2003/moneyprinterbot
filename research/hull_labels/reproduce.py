"""Small implementation check, not parameter selection or a broker-specific claim."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from bot.config.loader import load_yaml
from bot.hull.config import FRAMES, HullConfig
from bot.hull.replay import study
from bot.zones.feed import tick_minutes
from bot.zones.replay import file_hash

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, default=Path("data/public_xau_ticks"))
    ap.add_argument("--out", type=Path, default=Path("reports/hull_replay_public"))
    a = ap.parse_args()
    manifest = json.loads(Path("research/candle_retest/inputs.json").read_text())
    names = [f"XAUUSD_2026_w{w}.csv.gz" for w in (45, 46)]
    paths = [a.cache / n for n in names]
    hashes = {p.name: file_hash(p) for p in paths}
    if any(hashes[n] != manifest["sha256"][n] for n in names):
        raise ValueError("Bad quote hash")
    cfg = load_yaml("config.yaml")
    h = HullConfig.model_validate(cfg["hull"])
    ts = lambda s: int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp())
    res = study(
        tick_minutes(paths),
        cfg,
        h,
        h.symbols["XAUUSD"],
        FRAMES,
        ts("2026-11-12"),
        ts("2026-11-16"),
        0.001,
        100,
        "quote_usd",
        ("slow", "medium", "fast"),
        a.out,
    )
    (a.out / "inputs.json").write_text(
        json.dumps({"base": manifest["base"], "sha256": hashes}, indent=2) + "\n"
    )
    print(json.dumps(res["rates"], indent=2))
