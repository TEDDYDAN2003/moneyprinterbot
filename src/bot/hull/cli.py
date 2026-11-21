from bot.config.loader import load_yaml

from .config import NATIVE_TF, HullConfig, selection


def register(sub):
    for name, helptext in [
        ("trade", "observe Hull labels; explicit demo arming only"),
        (
            "trade-all",
            "one Dynamic Hull scanner for selected symbols and explicitly approved timeframes",
        ),
        (
            "hull-preflight",
            "inspect demo terminal, symbol metadata and sizing; no orders",
        ),
        (
            "hull-replay",
            "archived v4 offline benchmark ONLY; not the Dynamic Hull bot",
        ),
    ]:
        p = sub.add_parser(
            name, aliases=["demo"] if name == "trade" else [], help=helptext
        )
        p.add_argument("--config", default="config.yaml")
        p.add_argument(
            "--symbols", nargs="+", help="configured keys; default hull.enabled"
        )
        p.add_argument("--lots", type=float, help="per position")
        p.add_argument(
            "--target-usd",
            type=float,
            help="USD base per new ticket; one entry spread in USD is added",
        )
        p.add_argument("--rate", choices=("slow", "medium", "fast"))
        p.add_argument(
            "--profile",
            action="append",
            help="repeat SYMBOL:LOT:BASE_USD:RATE instead of global overrides",
        )
        p.add_argument(
            "--timeframe",
            choices=tuple(NATIVE_TF),
            help="optional single approved lane; cannot override an empty allowlist",
        )
        if name in ("trade", "trade-all"):
            p.add_argument(
                "--arm-demo", "--arm-orders", dest="armed", action="store_true"
            )
            p.add_argument(
                "--poll-seconds",
                type=float,
                default=1.0,
                help="normal poll interval (0.25–10 s); poll faster around native closes, first quotes and active ladders",
            )
            p.add_argument("--max-iterations", type=int)
        if name == "hull-replay":
            p.add_argument("--ticks", nargs="+", required=True)
            p.add_argument("--tick-size", type=float, required=True)
            p.add_argument("--contract-size", type=float, required=True)
            p.add_argument(
                "--conversion", choices=("quote_usd", "base_usd"), required=True
            )
            p.add_argument("--start", required=True)
            p.add_argument("--end", required=True)
            p.add_argument("--out", default="reports/hull_replay")


def handle(args):
    cfg = load_yaml(args.config)
    if "hull" not in cfg:
        raise ValueError(
            "Missing hull section. No fallback to the retired M15/zone executor."
        )
    if args.command == "hull-replay":
        # Refuse to misrepresent a closed-label replay as the new live strategy.
        if (
            cfg["hull"].get("entry_timing") == "live_hull_cross"
            or "eligible_timeframes" in cfg["hull"]
        ):
            raise ValueError(
                "hull-replay is archived v4 only; not a Dynamic Hull backtest"
            )
        from .v4.config import HullConfig as V4, FRAMES
        from .v4.replay import run_replay

        h = V4.model_validate(cfg["hull"])
        p = selection(
            h, args.symbols, args.profile, args.lots, args.target_usd, args.rate
        )
        frames = (NATIVE_TF[args.timeframe],) if args.timeframe else FRAMES
        if set(frames) - set(FRAMES):
            raise ValueError("Not a v4 benchmark timeframe")
        run_replay(args, cfg, h, p, frames)
    else:
        from .terminal import run

        h = HullConfig.model_validate(cfg["hull"])
        frames = h.lanes((NATIVE_TF[args.timeframe],) if args.timeframe else None)
        p = selection(
            h, args.symbols, args.profile, args.lots, args.target_usd, args.rate
        )
        run(
            cfg,
            h,
            p,
            frames,
            armed=getattr(args, "armed", False),
            preflight=args.command == "hull-preflight",
            poll=getattr(args, "poll_seconds", 1.0),
            iterations=getattr(args, "max_iterations", None),
        )
    return 0
