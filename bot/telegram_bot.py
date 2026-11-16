from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from bot.engine import LiveTradingEngine

logger = logging.getLogger(__name__)


class TelegramController:
    """
    Async python-telegram-bot v21+ controller using ApplicationBuilder.
    Strictly authorized to TELEGRAM_ADMIN_CHAT_ID.
    """

    def __init__(self, engine: LiveTradingEngine) -> None:
        self.engine = engine
        self.cfg = engine.cfg
        self._app = None
        self._notify_queue: asyncio.Queue[str] = asyncio.Queue()
        self._notifier_task: Optional[asyncio.Task] = None

    def enqueue_message(self, text: str) -> None:
        try:
            self._notify_queue.put_nowait(text)
        except Exception:
            pass

    def _is_authorized(self, chat_id: int) -> bool:
        admin_id = self.cfg.telegram.admin_chat_id
        if admin_id is None:
            return False
        return int(chat_id) == int(admin_id)

    async def _guard(self, update) -> bool:
        if update.effective_chat is None:
            return False
        chat_id = update.effective_chat.id
        if not self._is_authorized(chat_id):
            if update.message:
                await update.message.reply_text(
                    f"Unauthorized chat ID ({chat_id}). Set TELEGRAM_ADMIN_CHAT_ID={chat_id} in .env if this is you."
                )
            return False
        return True

    async def _cmd_start(self, update, context) -> None:
        if not await self._guard(update):
            return
        await update.message.reply_text(
            "<b>XAUUSDm Wickless Structure Bot</b>\n\n"
            "<b>Controls:</b>\n"
            "/run - Start reading market &amp; executing valid setups\n"
            "/pause - Pause new entries (keeps SL/TP &amp; $ exits active)\n"
            "/status - Account, spread, M1 structure &amp; active trades\n"
            "/closeall - Emergency close all bot positions &amp; pause\n\n"
            "<b>Live Risk &amp; Dollar Exit Config:</b>\n"
            "/set_tp_usd &lt;amt&gt; - Close trade at +$ profit (e.g. 15)\n"
            "/set_sl_usd &lt;amt&gt; - Close trade at -$ loss (0 = off)\n"
            "/set_basket_tp_usd &lt;amt&gt; - Close whole basket at +$ profit\n"
            "/set_basket_sl_usd &lt;amt&gt; - Close whole basket at -$ loss\n"
            "/risk &lt;pct&gt; - Set risk % per trade\n"
            "/lot &lt;vol&gt; - Switch to fixed lot size\n"
            "/mode &lt;wickless_structure|external_bridge|hybrid&gt;\n"
            "/params - Show current strategy &amp; dollar settings",
            parse_mode="HTML",
        )

    async def _cmd_run(self, update, context) -> None:
        if not await self._guard(update):
            return
        msg = self.engine.set_running()
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_pause(self, update, context) -> None:
        if not await self._guard(update):
            return
        msg = self.engine.set_paused()
        await update.message.reply_text(msg, parse_mode="HTML")

    async def _cmd_status(self, update, context) -> None:
        if not await self._guard(update):
            return
        status_html = self.engine.format_status_html()
        await update.message.reply_text(status_html, parse_mode="HTML")

    async def _cmd_params(self, update, context) -> None:
        if not await self._guard(update):
            return
        await update.message.reply_text(self.engine.format_params_html(), parse_mode="HTML")

    async def _cmd_closeall(self, update, context) -> None:
        if not await self._guard(update):
            return
        closed = self.engine.emergency_close_all("TELEGRAM_CLOSEALL")
        await update.message.reply_text(
            f"Emergency close completed. Closed <b>{closed}</b> position(s) and paused bot.",
            parse_mode="HTML",
        )

    async def _cmd_set_tp_usd(self, update, context) -> None:
        if not await self._guard(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /set_tp_usd 15 (0 disables)")
            return
        val = max(0.0, float(context.args[0]))
        self.cfg.dollar_exits.trade_tp_usd = val
        self.cfg.dollar_exits.enabled = True
        await update.message.reply_text(f"Per-trade Dollar TP set to <b>${val:.2f}</b>", parse_mode="HTML")

    async def _cmd_set_sl_usd(self, update, context) -> None:
        if not await self._guard(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /set_sl_usd 10 (0 disables)")
            return
        val = max(0.0, float(context.args[0]))
        self.cfg.dollar_exits.trade_sl_usd = val
        self.cfg.dollar_exits.enabled = True
        await update.message.reply_text(f"Per-trade Dollar SL set to <b>${val:.2f}</b>", parse_mode="HTML")

    async def _cmd_set_basket_tp_usd(self, update, context) -> None:
        if not await self._guard(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /set_basket_tp_usd 15 (0 disables)")
            return
        val = max(0.0, float(context.args[0]))
        self.cfg.dollar_exits.basket_tp_usd = val
        self.cfg.dollar_exits.enabled = True
        await update.message.reply_text(f"Basket Dollar TP set to <b>${val:.2f}</b>", parse_mode="HTML")

    async def _cmd_set_basket_sl_usd(self, update, context) -> None:
        if not await self._guard(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /set_basket_sl_usd 30 (0 disables)")
            return
        val = max(0.0, float(context.args[0]))
        self.cfg.dollar_exits.basket_sl_usd = val
        self.cfg.dollar_exits.enabled = True
        await update.message.reply_text(f"Basket Dollar SL set to <b>${val:.2f}</b>", parse_mode="HTML")

    async def _cmd_risk(self, update, context) -> None:
        if not await self._guard(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /risk 1.0 (percentage of equity per setup)")
            return
        val = max(0.05, min(10.0, float(context.args[0])))
        self.cfg.risk.sizing_mode = "risk_percent"
        self.cfg.risk.risk_per_trade_pct = val
        await update.message.reply_text(f"Sizing set to <b>{val:.2f}% equity risk</b> per setup.", parse_mode="HTML")

    async def _cmd_lot(self, update, context) -> None:
        if not await self._guard(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /lot 0.10 (fixed lot size)")
            return
        val = max(0.01, float(context.args[0]))
        self.cfg.risk.sizing_mode = "fixed_lot"
        self.cfg.risk.fixed_lot = val
        await update.message.reply_text(f"Sizing switched to <b>fixed {val:.2f} lots</b>.", parse_mode="HTML")

    async def _cmd_mode(self, update, context) -> None:
        if not await self._guard(update):
            return
        if not context.args or context.args[0] not in ("wickless_structure", "external_bridge", "hybrid"):
            await update.message.reply_text("Usage: /mode wickless_structure | external_bridge | hybrid")
            return
        self.cfg.strategy.mode = context.args[0]
        await update.message.reply_text(f"Strategy mode updated to <b>{context.args[0]}</b>.", parse_mode="HTML")

    async def _cmd_pace(self, update, context) -> None:
        if not await self._guard(update):
            return
        if not context.args or context.args[0].lower() not in ("fast", "medium", "slow"):
            await update.message.reply_text("Usage: /pace fast (6 trades) | medium (3 trades) | slow (1 trade)")
            return
        mode = context.args[0].lower()
        self.cfg.strategy.pace_mode = mode
        batch = 6 if mode == "fast" else 3 if mode == "medium" else 1
        await update.message.reply_text(f"Pace mode updated to <b>{mode.upper()}</b> ({batch} trades per entry).", parse_mode="HTML")

    async def _notifier_loop(self) -> None:
        while True:
            msg = await self._notify_queue.get()
            admin_id = self.cfg.telegram.admin_chat_id
            if self._app is not None and admin_id is not None:
                try:
                    await self._app.bot.send_message(chat_id=int(admin_id), text=msg, parse_mode="HTML")
                except Exception as exc:
                    logger.warning("Failed sending Telegram notification: %s", exc)

    async def run_async(self) -> None:
        if not self.cfg.telegram.enabled or not self.cfg.telegram.bot_token:
            logger.warning("Telegram disabled or TELEGRAM_BOT_TOKEN missing; running headless engine loop.")
            await self.engine.run_loop()
            return

        from telegram.ext import ApplicationBuilder, CommandHandler  # type: ignore

        self._app = ApplicationBuilder().token(self.cfg.telegram.bot_token).build()
        self._app.add_handler(CommandHandler(["start", "help"], self._cmd_start))
        self._app.add_handler(CommandHandler("run", self._cmd_run))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("params", self._cmd_params))
        self._app.add_handler(CommandHandler("closeall", self._cmd_closeall))
        self._app.add_handler(CommandHandler("set_tp_usd", self._cmd_set_tp_usd))
        self._app.add_handler(CommandHandler("set_sl_usd", self._cmd_set_sl_usd))
        self._app.add_handler(CommandHandler("set_basket_tp_usd", self._cmd_set_basket_tp_usd))
        self._app.add_handler(CommandHandler("set_basket_sl_usd", self._cmd_set_basket_sl_usd))
        self._app.add_handler(CommandHandler("risk", self._cmd_risk))
        self._app.add_handler(CommandHandler("lot", self._cmd_lot))
        self._app.add_handler(CommandHandler("mode", self._cmd_mode))
        self._app.add_handler(CommandHandler("pace", self._cmd_pace))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)

        self._notifier_task = asyncio.create_task(self._notifier_loop())
        self.enqueue_message(self.engine.format_startup_html())

        try:
            await self.engine.run_loop()
        finally:
            if self._notifier_task:
                self._notifier_task.cancel()
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
