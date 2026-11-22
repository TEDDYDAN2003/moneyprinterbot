from bot.config.schema import RootConfig

from .config import SIGNAL_TF, selection


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
            choices=tuple(SIGNAL_TF),
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


def handle(args, cfg, model: RootConfig):
    from .terminal import run

    h = model.hull
    frames = h.lanes((SIGNAL_TF[args.timeframe],) if args.timeframe else None)
    p = selection(h, args.symbols, args.profile, args.lots, args.target_usd, args.rate)
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
