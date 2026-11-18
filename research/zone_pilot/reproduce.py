"""Reproduce the spread-only gold experiment; no MT5 connection or secret needed.

Call from repository root after `pip install -e .`. Without --download, only
verified local files in --cache are used. Do not pool the split risk accounts.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
from urllib.request import urlopen

REV = "3ef0f9a5940898a3f690140481500730a3fe178a"
ROOT = f"https://raw.githubusercontent.com/Touhid-HZ/xauusd-tick-data-2026/{REV}/data/tick/"
HASHES = [
    "c833e8dafc7d4e7712d1c614b57352157f4332b7177b67d3320fb324b925357d",
    "91ff4516f9464256bd1dd992a5bc28a6a8c0f5ccfd17c9b6d9140695cc491ebf",
    "143ae8f84fdae26d443190c3c46f06709d7b0da487aef155107aa996cc9fec37",
    "9cc41eac154de7c6f88c4d9b4ab2af84bc05b272da2e5844ec19a74788e00af5",
    "af7f48fb7d63c3b6ad87bb3b8289177a6091e85df46e483368edc4541781edf8",
    "bfabc0e7d4aa86ac4eceb96cdd8a63ae5f705915877db616b9224615f6711aa8",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, default=Path("data/public_xau_ticks"))
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--out", type=Path, default=Path("reports/zone_replay_public"))
    args = ap.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    files = []
    for w, h in enumerate(HASHES, 40):
        path = args.cache / f"XAUUSD_2026_w{w}.csv.gz"
        if args.download and not path.exists():
            with urlopen(ROOT + path.name, timeout=90) as r:
                raw = r.read()
            if hashlib.sha256(raw).hexdigest() != h:
                raise ValueError(f"Bad download {path.name}")
            path.write_bytes(raw)
        with path.open("rb") as f:
            if hashlib.file_digest(f, "sha256").hexdigest() != h:
                raise ValueError(f"Checksum mismatch: {path}")
        files.append(str(path))
    for split, a, b in [
        ("early", "2026-10-01", "2026-10-29"),
        ("later", "2026-10-29", "2026-11-12"),
    ]:
        for name, opts in [
            ("legacy", ["--strategy", "legacy"]),
            ("zones", []),
            ("hold", ["--exit-mode", "next_zone"]),
            ("structural", ["--require-structural-stop"]),
        ]:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "bot.cli",
                    "zones-replay",
                    "--symbol",
                    "XAUUSD",
                    "--start",
                    a,
                    "--end",
                    b,
                    "--ticks",
                    *files,
                    "--out",
                    str(args.out / f"{split}-{name}"),
                    *opts,
                ],
                check=True,
            )


if __name__ == "__main__":
    main()
