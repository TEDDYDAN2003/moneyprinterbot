"""Dynamic Hull desktop CLI. Observation is the default; no retired execution fallback."""

import argparse
import json
from time import time

from bot.config.entry_sessions import load_entry_sessions
from bot.config.loader import load_yaml
from bot.config.schema import validate_config
from bot.hull.cli import handle, register
from bot.hull.target_settings import effective_config


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="dynamic-hull", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    register(sub)
    sub.add_parser(
        "validate-config", help="validate Hull settings and entry sessions offline"
    ).add_argument("--config", default="config.yaml")
    export = sub.add_parser(
        "export-ticks", help="read-only quote export for a configured Hull symbol"
    )
    export.add_argument("--config", default="config.yaml")
    export.add_argument("--symbol", default="XAUUSD")
    export.add_argument("--start", required=True)
    export.add_argument("--end", required=True, help="exclusive UTC")
    export.add_argument("--out", required=True)
    a = parser.parse_args(argv)
    raw = effective_config(a.config, load_yaml(a.config))
    model = validate_config(raw)
    if a.command == "validate-config":
        sessions = load_entry_sessions()
        selected = {
            k: sessions.bind(model.hull.symbols[k].broker_symbol or k, k)
            for k in model.hull.enabled
        }
        when = time()
        result = model.model_dump() | {
            "entry_sessions": sessions.identity()
            | {"enabled": {k: g.report(when) for k, g in selected.items()}}
        }
        print(json.dumps(result, indent=2, default=str))
        return 2 if any(g.issue for g in selected.values()) else 0
    # Keep the original Hull mapping for preflight's retired-limits warning.
    cfg = model.model_dump() | {"hull": raw["hull"]}
    if a.command == "export-ticks":
        from bot.hull.terminal import export_quotes

        export_quotes(a, cfg)
        return 0
    return handle(a, cfg, model)


if __name__ == "__main__":
    raise SystemExit(main())
