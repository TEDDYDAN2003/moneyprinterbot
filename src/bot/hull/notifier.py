"""Notification helper for blocked setups and reconciliation holds.

Dispatches non-blocking notifications:
1. Windows Desktop Toast with sound (winotify)
2. Windows System Exclamation Beep (winsound)
3. Dedicated alert append log (reports/hull_runtime/{account}/blocks.txt)
4. Optional Telegram alert if TG_TOKEN / TG_CHAT or config reporting.telegram is configured
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path

# Common non-error/success outcomes to ignore
IGNORED_OUTCOMES = {
    "retest_batch_placed",
    "orders_placed",
    "waiting_for_fill",
    "working",
    "shadow_retest_working",
    "lane_held_by_opposing_batch",
    "no_aligned_eligible_pair",
}

BLOCK_ALERTS_LOG = Path("reports/hull_runtime/BLOCKED_SETUPS.txt")


def _send_toast(title: str, message: str) -> None:
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass

    try:
        from winotify import Notification, audio
        toast = Notification(
            app_id="Hull Trading Bot",
            title=title,
            msg=message,
            duration="short",
        )
        toast.set_audio(audio.Reminder, loop=False)
        toast.show()
    except Exception:
        pass


_MT5_COMMON_INI = (
    r"C:\Users\hp\AppData\Roaming\MetaQuotes\Terminal"
    r"\D0E8209F77C8CF37AD8BF550E51FF075\config\common.ini"
)

def _get_mt5_push_id() -> str:
    """Read MetaQuotes PushID from MT5's common.ini (base64-encoded token)."""
    push_id = os.environ.get("MT5_METAQUOTES_ID", "").strip()
    if push_id:
        return push_id
    try:
        with open(_MT5_COMMON_INI, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("PushID="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _send_mt5_push(title: str, message: str) -> None:
    """Send push notification to MT5 mobile via MetaQuotes Notification Service."""
    push_id = _get_mt5_push_id()
    if not push_id:
        return
    try:
        import urllib.parse
        import urllib.request
        # MetaQuotes limits messages to 255 chars
        text = f"{title} | {message}"[:255]
        url = "https://www.mql5.com/api/notifications/send"
        data = urllib.parse.urlencode({"id": push_id, "msg": text}).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=8) as resp:
            _ = resp.read()
    except Exception:
        pass


def _send_telegram(title: str, message: str) -> None:
    token = os.environ.get("TG_TOKEN")
    chat_id = os.environ.get("TG_CHAT")
    if not token or not chat_id:
        return
    try:
        import urllib.parse
        import urllib.request
        text = f"🚨 *{title}*\n{message}"
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _log_block(ts_str: str, title: str, message: str) -> None:
    try:
        BLOCK_ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with BLOCK_ALERTS_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{ts_str}] {title} | {message}\n")
    except Exception:
        pass


def notify_blocked_setup(
    symbol: str,
    tf: int | str,
    trigger_tf: int | str | None,
    side: int | str,
    reason: str,
    extra: str = "",
) -> None:
    """Dispatches a notification on a separate thread so trading is never blocked."""
    if reason in IGNORED_OUTCOMES:
        return

    side_str = "BUY" if side in (1, "BUY") else "SELL" if side in (-1, "SELL") else str(side)
    tf_str = f"M{tf}" if str(tf).isdigit() else str(tf)
    trig_str = f"/M{trigger_tf}" if trigger_tf else ""
    title = f"⚠️ Setup Blocked: {symbol} {tf_str}{trig_str}"
    detail = f"Side: {side_str} | Reason: {reason}"
    if extra:
        detail += f" ({extra})"

    ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def worker():
        _log_block(ts_str, title, detail)
        _send_toast(title, detail)
        _send_mt5_push(title, detail)
        _send_telegram(title, detail)

    threading.Thread(target=worker, daemon=True).start()
