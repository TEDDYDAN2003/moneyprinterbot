"""Stable journal dimensions. A session is an analytical FX window, not an exchange calendar."""

from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Local wall-clock hours, Mon-Fri, start inclusive/end exclusive. Order fixes overlap bucket names.
SESSIONS = (
    ("Sydney", "Australia/Sydney", 8, 17),
    ("Tokyo", "Asia/Tokyo", 9, 18),
    ("London", "Europe/London", 8, 17),
    ("New York", "America/New_York", 8, 17),
)
REASON_LABELS = {
    "direct_retest_limit": "Dynamic Hull · direct pullback limit",
    "paired_structure_cross": "Dynamic Hull · aligned parent and trigger cross",
    "m1_parent_structure_cross": "Dynamic Hull · M1 close / parent structure",
    "trend_hull_cross": "Dynamic Hull · trend-aligned cross",
    "bull_bos_discount_buy": "BUY · BOS discount (closed cross)",
    "bear_bos_premium_sell": "SELL · BOS premium (closed cross)",
    "bull_choch_buy": "BUY · bullish CHoCH close",
    "bear_choch_sell": "SELL · bearish CHoCH close",
    "bull_protected_pullback_buy": "BUY · pullback above protected HL",
    "bear_protected_pullback_sell": "SELL · pullback below protected LH",
    "bull_discount_buy": "BUY · bullish discount",
    "bear_premium_sell": "SELL · bearish premium",
    "buy_below_bear_bos": "BUY · below bearish BOS",
    "sell_above_bull_bos": "SELL · above bullish BOS",
}
REASON_NOTES = {
    "direct_retest_limit": "A broker opening deal matched the closed pair and its previously placed candle-relative limit. A pending acknowledgement is not a fill.",
    "paired_structure_cross": "The trigger Hull crossed on its own completed candle, in the direction of both its own and its parent’s confirmed structure. Other charts do not vote.",
    "m1_parent_structure_cross": "A completed M1 Hull cross agreed with the owning parent’s closed structure. No parent Hull cross or continuation BOS was required.",
    "trend_hull_cross": "The selected Hull curve crossed in the direction of confirmed internal structure. No price-zone or continuation-BOS requirement.",
    "bull_bos_discount_buy": "A fresh Hull BUY closed above the BOS-protected higher low and at or below the range midpoint. CHoCH and its retest are not entries.",
    "bear_bos_premium_sell": "A fresh Hull SELL closed at or above the BOS range midpoint and below the protected lower high. CHoCH and its retest are not entries.",
    "bull_choch_buy": "A fresh Hull BUY closed strictly above the protected bearish LH on the bullish CHoCH candle.",
    "bear_choch_sell": "A fresh Hull SELL closed strictly below the protected bullish HL on the bearish CHoCH candle.",
    "bull_protected_pullback_buy": "After a completed pullback, the fresh BUY triggered above the protected higher low. An internal swing alone does not move that boundary.",
    "bear_protected_pullback_sell": "After a completed pullback, the fresh SELL triggered below the protected lower high. An internal swing alone does not move that boundary.",
    "bull_discount_buy": "The confirmed bullish swing range allowed a BUY between the higher low and the midpoint.",
    "bear_premium_sell": "The confirmed bearish swing range allowed a SELL between the midpoint and the lower high.",
    "buy_below_bear_bos": "After a bearish continuation break and a confirmed new low, the BUY closed below the broken low inside the countertrend band.",
    "sell_above_bull_bos": "After a bullish continuation break and a confirmed new high, the SELL closed above the broken high inside the countertrend band.",
}


def entry_reason(model):
    if not model:
        return "Not recorded"
    return REASON_LABELS.get(model, "Recorded: " + model.replace("_", " "))


@lru_cache(maxsize=100_000)
def session_minute(utc_minute):
    ts = datetime.fromtimestamp(utc_minute * 60, UTC)
    names = []
    try:
        for name, zone, first, last in SESSIONS:
            local = ts.astimezone(ZoneInfo(zone))
            if local.weekday() < 5 and first <= local.hour < last:
                names.append(name)
    except ZoneInfoNotFoundError:
        return "Timezone data unavailable"
    return " + ".join(names) if names else "Off-session"


def add_dimensions(row):
    row["entry_reason"] = entry_reason(row["setup_type"])
    for key in ("open", "close"):
        when = datetime.fromisoformat(row[key + "_time_utc"])
        row[key + "_session"] = session_minute(int(when.timestamp()) // 60)
