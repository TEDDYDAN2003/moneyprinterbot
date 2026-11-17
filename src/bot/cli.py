from __future__ import annotations

import argparse
import json
from pathlib import Path

from bot.config.loader import load_yaml
from bot.config.schema import validate_config


def main() -> int:
    parser = argparse.ArgumentParser(prog="xauusd-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-config")
    validate.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    if args.command == "validate-config":
        config = validate_config(load_yaml(Path(args.config)))
        print(json.dumps(config.model_dump(), indent=2, default=str))
        return 0
    raise RuntimeError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
