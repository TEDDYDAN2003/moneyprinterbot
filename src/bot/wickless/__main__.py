"""python -m bot.wickless --help. There is no live-account trading mode."""

import argparse
import json
import sys

from .config import load


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m bot.wickless",
        description="Wickless M15 · independent demo strategy",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "replay", "preflight", "observe", "trade", "reconcile"):
        c = sub.add_parser(name)
        c.add_argument(
            "--config", required=True, help="Wickless Desk JSON, not Hull YAML"
        )
        if name in ("preflight", "observe", "trade", "reconcile"):
            c.add_argument(
                "--terminal", default="", help="optional local MT5 executable path"
            )
        if name == "trade":
            c.add_argument("--arm-demo", action="store_true", required=True)
        if name == "reconcile":
            c.add_argument("--reviewed-flat", action="store_true", required=True)
        if name == "replay":
            c.add_argument("--symbol", required=True)
            c.add_argument("--bars", required=True)
            c.add_argument("--tick-size", type=float, required=True)
            c.add_argument("--out", help="optional trade ledger CSV")
    a = p.parse_args(argv)
    try:
        cfg = load(a.config)
        if a.cmd == "validate":
            print(cfg.model_dump_json(indent=2))
        elif a.cmd == "replay":
            from .replay import replay, rows

            pair = next((v for v in cfg.pairs if v.symbol == a.symbol), None)
            if pair is None:
                raise ValueError("Unselected symbol")
            print(
                json.dumps(
                    replay(
                        rows(a.bars, a.tick_size), pair, cfg.rules, a.tick_size, a.out
                    ),
                    indent=2,
                )
            )
        else:
            from .terminal import run

            run(
                cfg,
                a.cmd,
                arm=getattr(a, "arm_demo", False),
                terminal_path=a.terminal,
                review_flat=getattr(a, "reviewed_flat", False),
            )
        return 0
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, ValueError, OSError, ArithmeticError, KeyError) as exc:
        print(f"Wickless stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
