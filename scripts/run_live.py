from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_config
from bot.engine import LiveTradingEngine
from bot.mt5_adapter import MT5Adapter
from bot.telegram_bot import TelegramController


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the XAUUSDm Wickless Structure MT5 + Telegram Bot")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML configuration")
    parser.add_argument("--env", default=".env", help="Path to .env secrets file")
    parser.add_argument("--dry-run", action="store_true", help="Read live MT5 market but simulate order sends")
    parser.add_argument("--auto-start", action="store_true", help="Start in RUNNING state instead of waiting for /run")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    cfg = load_config(args.config, args.env)
    broker = MT5Adapter(cfg.mt5)
    engine = LiveTradingEngine(cfg=cfg, broker=broker, dry_run=args.dry_run)
    if args.auto_start:
        engine.set_running()

    tg = TelegramController(engine)
    engine.set_notifier(tg.enqueue_message)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, sig_name):
            try:
                loop.add_signal_handler(getattr(signal, sig_name), engine.stop)
            except NotImplementedError:
                # Windows event loop fallback
                pass

    try:
        loop.run_until_complete(tg.run_async())
    except KeyboardInterrupt:
        engine.stop()
    finally:
        loop.close()


if __name__ == "__main__":
    main()
