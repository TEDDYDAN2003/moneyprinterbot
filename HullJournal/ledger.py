"""Read-only broker history -> closed positions and completed Hull setups. Python 3.11+."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path

from hull_analytics import add_dimensions

MAGIC = 26111825
ENTRY_WINDOW = (
    60  # The shipped scanner caps entry age at 30s. Allow late sequential receipts.
)
# Recognized ticket formats only; parsing a tag never authorizes a timeframe.
NATIVE_MINUTES = {
    1,
    2,
    3,
    4,
    5,
    6,
    10,
    12,
    15,
    20,
    30,
    45,  # synthetic, ticket tag still identifies a distinct setup
    60,
    120,
    180,
    240,
    360,
    480,
    720,
    1440,
}
TAG = re.compile(r"h([1-9]\d{0,3})_(\d{9,11})_([1-9]\d{0,3})\Z")
MINUTE_TAG = re.compile(r"m(5|15|30|45)_(\d{9,11})_([1-9]\d{0,3})\Z")
# Journal is standalone; accepting historical tags never authorizes trading.
PAIRS = {5: 3, 10: 5, 15: 10}
PAIR_TAG = re.compile(r"[pr](5|10|15)x(3|5|10)_(\d{9,11})_([1-9]\d{0,3})\Z")
PAIR_STOP = "confirmed_pair_trigger_candle_wick_v1"
PAIR_ENTRY = "closed_pair_structure_cross_v1"
RETEST_FIELDS = (
    "retest_limit",
    "cross_candle_close",
    "retest_segment",
    "retest_expiry_utc",
    "submit_to_fill_ms",
)
PAIR_SHARED = ("parent_structure_close_utc", "confirmed_pair_bias")
LIVE_TAG = re.compile(r"i([1-9]\d{0,3})_([1-9]\d{11,13})_([1-9]\d{0,3})\Z")
LIVE_KEY_MIN = 10**11  # disjoint from every accepted legacy seconds key
# These are optional evidence columns, not new ownership/settlement rules. Historical
# confirmed h-tags alone do NOT prove which stop/quote policy produced a trade.
CROSS_POLICY = "confirmed_cross_candle_wick_v1"
M1_POLICY = "confirmed_m1_cross_candle_wick_v1"
CROSS_SHARED = (
    "initial_stop_policy",
    "cross_candle_wick",
    "frozen_cross_sl",
    "mss_skip_exception",
)
SEND_FIELDS = ("send_quote_utc", "send_bid", "send_ask")
LOG_FORMAT = 7  # incremental index: replay available demo.jsonl once, do not reset broker history
CASH = ("gross_profit", "commission", "swap", "fee")
FIELDS = [
    "schema_version",
    "record_type",
    "account_key",
    "currency",
    "symbol",
    "timeframe",
    "side",
    "position_id",
    "setup_id",
    "leg",
    "ticket_count",
    "setup_type",
    "signal_close_utc",
    "open_time_utc",
    "close_time_utc",
    "duration_s",
    "lots",
    "open_price",
    "close_price",
    "gross_profit",
    "commission",
    "swap",
    "fee",
    "net_profit",
    "outcome",
    "exit_reason",
    "opening_order",
    "entry_deals",
    "exit_deals",
    "initial_sl",
    "initial_tp",
    "attribution",
    "entry_reason",
    "open_session",
    "close_session",
    "bot_exit_request",
    "bot_exit_request_utc",
    "entry_timing",
    "signal_trigger_utc",
    "signal_bar_open_utc",
    "signal_bar_end_utc",
    "trigger_bid",
    "trigger_ask",
    *CROSS_SHARED,
    *SEND_FIELDS,
    "trigger_timeframe",
    *PAIR_SHARED,
    *RETEST_FIELDS,
]
NUMERIC = {
    "schema_version",
    "leg",
    "ticket_count",
    "duration_s",
    "lots",
    "open_price",
    "close_price",
    "gross_profit",
    "commission",
    "swap",
    "fee",
    "net_profit",
    "initial_sl",
    "initial_tp",
    "cross_candle_wick",
    "frozen_cross_sl",
    "send_bid",
    "send_ask",
    "retest_limit",
    "cross_candle_close",
    "submit_to_fill_ms",
}
REASONS = {
    0: "Desktop",
    1: "Mobile",
    2: "Web",
    3: "Expert",
    4: "Stop loss",
    5: "Take profit",
    6: "Stop out",
    7: "Rollover",
    8: "Variation margin",
    9: "Split",
    10: "Corporate action",
}
D = Decimal


class JournalError(RuntimeError):
    pass


def iso(ms):
    return (
        datetime.fromtimestamp(int(ms) / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def dec(x):
    v = D(str(x))
    if not v.is_finite():
        raise ValueError("Non-finite broker number; sync not committed.")
    return v


def plain(x):
    s = format(dec(x), "f")
    return (s.rstrip("0").rstrip(".") if "." in s else s) if dec(x) else "0"


def dt(s):
    return datetime.fromtimestamp(s, UTC)


def enc(v):
    return json.dumps(
        v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def db_open(path):
    c = sqlite3.connect(path)
    c.executescript("""PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS tracked(pid TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS windows(symbol TEXT,signal INTEGER,PRIMARY KEY(symbol,signal));
      CREATE TABLE IF NOT EXISTS deals(id TEXT PRIMARY KEY,pid TEXT NOT NULL,body TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS by_pid ON deals(pid);
      CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,symbol TEXT, tag TEXT,side TEXT,sl TEXT,tp TEXT);
      CREATE TABLE IF NOT EXISTS cross_receipts(id TEXT PRIMARY KEY, wick TEXT NOT NULL, sl TEXT NOT NULL,
          waiver TEXT NOT NULL, send_ms INTEGER NOT NULL, bid TEXT NOT NULL, ask TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS retest_windows(symbol TEXT,tf INTEGER,signal INTEGER,PRIMARY KEY(symbol,tf,signal));
      CREATE TABLE IF NOT EXISTS retest_deals(id TEXT PRIMARY KEY,evidence TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS retest_settled(symbol TEXT,tf INTEGER,signal INTEGER,side TEXT,deals TEXT NOT NULL,PRIMARY KEY(symbol,tf,signal,side));
      CREATE TABLE IF NOT EXISTS pair_context(id TEXT PRIMARY KEY,parent_close INTEGER NOT NULL,bias INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS pair_finished(symbol TEXT,tf INTEGER,signal INTEGER,outcome TEXT,PRIMARY KEY(symbol,tf,signal));
      CREATE TABLE IF NOT EXISTS labels(symbol TEXT,tf INTEGER,signal INTEGER,side TEXT,model TEXT,
          PRIMARY KEY(symbol,tf,signal,side));
      CREATE TABLE IF NOT EXISTS live_triggers(symbol TEXT,tf INTEGER,signal INTEGER,side TEXT,bar_end INTEGER,bid TEXT,ask TEXT,
          PRIMARY KEY(symbol,tf,signal,side));
      CREATE TABLE IF NOT EXISTS exit_notes(symbol TEXT,ticket TEXT,at_ms INTEGER,why TEXT,
          PRIMARY KEY(symbol,ticket,at_ms,why));
      CREATE TABLE IF NOT EXISTS finished(symbol TEXT,tf INTEGER,signal INTEGER,outcome TEXT,
          PRIMARY KEY(symbol,tf,signal));""")
    return c


def get(c, k, default=None):
    row = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(row[0]) if row else default


def put(c, k, value):
    c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, enc(value)))


def identity(mt, magic):
    t = mt.terminal_info()
    a = mt.account_info()
    if t is None or not t.connected or a is None:
        raise JournalError("Terminal disconnected. Last good exports are retained.")
    if (
        a.trade_mode != mt.ACCOUNT_TRADE_MODE_DEMO
        or a.margin_mode != mt.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
    ):
        raise JournalError(
            "Select the Hull hedging DEMO account in MT5. No records imported."
        )
    key = hashlib.sha256(repr((int(a.login), str(a.server))).encode()).hexdigest()[:16]
    digits = int(a.currency_digits)
    if not a.login or not a.server or not a.currency or not 0 <= digits <= 8:
        raise JournalError("Incomplete account identity, currency or precision.")
    return {"key": key, "currency": str(a.currency), "digits": digits, "magic": magic}


def norm(x):
    """Keep broker integers as text where spreadsheet/JS conversions could lose precision."""
    ms = int(x.time_msc or x.time * 1000)
    if not x.ticket or ms <= 0:
        raise JournalError("Deal has no stable identifier/time.")
    return dict(
        id=str(int(x.ticket)),
        pid=str(int(x.position_id)),
        order=str(int(x.order)),
        ms=ms,
        typ=int(x.type),
        ent=int(x.entry),
        magic=int(x.magic),
        symbol=str(x.symbol),
        reason=int(x.reason),
        comment=str(x.comment or ""),
        **{
            k: plain(getattr(x, k))
            for k in ("price", "volume", "profit", "commission", "swap", "fee")
        },
    )


def tf_name(minutes):
    if minutes == 1440:
        return "D1"
    return f"H{minutes // 60}" if minutes >= 60 else f"M{minutes}"


def tag(s):
    text = str(s or "")
    if text.startswith(("p", "r")):
        m = PAIR_TAG.fullmatch(text)
        if not m:
            return None
        tf, child, stamp, leg = map(int, m.groups())
        if PAIRS[tf] != child:
            return None
    else:
        m = (
            TAG.fullmatch(text)
            or MINUTE_TAG.fullmatch(text)
            or LIVE_TAG.fullmatch(text)
        )
        if not m:
            return None
        tf, stamp, leg = map(int, m.groups())
    maximum = 32_503_680_000 * (1000 if text.startswith("i") else 1)
    return (tf, stamp, leg) if tf in NATIVE_MINUTES and 0 < stamp < maximum else None


def trigger_ms(key):
    return key if key >= LIVE_KEY_MIN else key * 1000


def signal_window(key):
    ms = trigger_ms(key)
    return ms // 1000, (ms + 999) // 1000 + ENTRY_WINDOW


def pair_clock_valid(ev, close_s, quote_ms):
    # Millisecond counters in receipts are rounded to three decimals. Compare
    # relative to bar close, not to journal ingestion time or a guessed fill.
    entry, tick, submitted = (
        dec(ev[k]) for k in ("max_entry_age_s", "max_quote_age_s", "close_to_submit_ms")
    )
    elapsed = quote_ms - close_s * 1000
    return (
        0 < entry <= 30
        and 0 < tick <= 15
        and 0 <= elapsed <= submitted <= entry * 1000
        and submitted - elapsed <= tick * 1000
    )


def index_cross_receipt(c, ev, vals, ref):
    """Enrich a *matched* original request, never a label/preview or a historical h-tag."""
    if ev.get("initial_stop_policy") not in (CROSS_POLICY, M1_POLICY, PAIR_STOP):
        return
    tf, close_s, leg = ref
    did, _sy, tag_text, side, requested_sl, _tp = vals
    minute = ev["initial_stop_policy"] == M1_POLICY
    paired = ev["initial_stop_policy"] == PAIR_STOP
    candle_tf = PAIRS.get(tf, 0) if paired else 1 if minute else tf
    sign = 1 if side == "BUY" else -1
    ms = int(ev["send_tick_time_msc"])
    wick, sl, bid, ask = (
        dec(ev[k])
        for k in ("cross_candle_wick", "frozen_cross_sl", "send_bid", "send_ask")
    )
    waiver = ev.get("mss_skip_exception") or ""
    if not (
        ev.get("retcode") in (10009, 10010)
        and tag_text.startswith("p" if paired else "m" if minute else "h")
        and tf in (PAIRS if paired else (5, 15, 30, 45))
        and (
            not paired
            or (
                pair_clock_valid(ev, close_s, ms)
                and 0 < dec(ev["stop_tick_size"]) <= (wick - sl) * sign
                and sl % dec(ev["stop_tick_size"]) == 0
                and ev.get("entry_policy") == PAIR_ENTRY
                and ev.get("trigger_tf") == candle_tf
                and ev.get("parent_tf") == tf
                and ev.get("trigger_source")
                and ev.get("trigger_structure_close_s") == close_s
                and ev.get("parent_structure_bias")
                == ev.get("trigger_structure_bias")
                == sign
                and isinstance(ev.get("parent_structure_close_s"), int)
                and 0 <= close_s - ev["parent_structure_close_s"] < tf * 60
            )
        )
        and (
            not minute
            or (
                ev.get("trigger_tf") == 1
                and ev.get("parent_tf") == tf
                and ev.get("m1_source")
                and isinstance(ev.get("parent_structure_close_s"), int)
                and 0 <= close_s - ev["parent_structure_close_s"] < tf * 60
            )
        )
        and ev.get("tf", tf) == tf
        and ev.get("leg") == leg
        and ev.get("entry_timing") == "confirmed_close"
        and ev.get("entry_side") == sign
        and ev.get("signal") == close_s
        and ev.get("trigger_msc") == close_s * 1000
        and ev.get("cross_candle_open_s") == close_s - candle_tf * 60
        and close_s * 1000 <= ms < (close_s + candle_tf * 60) * 1000
        and 0 < bid <= ask
        and min(wick, sl) > 0
        and (wick - sl) * sign > 0
        and sl == dec(requested_sl)
        and waiver in ("", "failed_counter_mss_return")
        and c.execute("SELECT * FROM receipts WHERE id=?", (did,)).fetchone() == vals
    ):
        raise ValueError("Inconsistent confirmed-cross receipt metadata")
    evidence = (did, plain(wick), plain(sl), waiver, ms, plain(bid), plain(ask))
    previous = c.execute("SELECT * FROM cross_receipts WHERE id=?", (did,)).fetchone()
    if paired:
        ctx = (did, ev["parent_structure_close_s"], sign)
        original = c.execute("SELECT * FROM pair_context WHERE id=?", (did,)).fetchone()
        if original is None:
            c.execute("INSERT INTO pair_context VALUES(?,?,?)", ctx)
        elif original != ctx:
            c.execute("UPDATE cross_receipts SET send_ms=0 WHERE id=?", (did,))
            raise ValueError("Conflicting paired structure evidence")
    if previous is None:
        c.execute("INSERT INTO cross_receipts VALUES(?,?,?,?,?,?,?)", evidence)
    elif previous != evidence and previous[4] != 0:
        # Contradictory valid receipts for one deal: do not pick the first or last.
        # A zero clock is a durable ambiguity marker, not an executable quote.
        c.execute("UPDATE cross_receipts SET send_ms=0 WHERE id=?", (did,))
        raise ValueError("Conflicting cross-candle receipts for the same deal")


def index_retest(c, ev, magic):
    """Store only confirmed-fill evidence. make_views independently joins broker deal fields."""
    sy = ev["symbol"]
    tf = ev["tf"]
    stamp = ev["signal"]
    child = ev["trigger_tf"]
    if PAIRS.get(tf) != child or not isinstance(stamp, int):
        raise ValueError("invalid retest pair")
    if ev["event"] == "retest_batch_settled":
        ids = ev["fill_deals"]
        n = ev["planned"]
        states = ev["outcomes"]
        sign = ev["side"]
        if not (
            isinstance(ids, list)
            and all(type(v) is int and v > 0 for v in ids)
            and len(set(ids)) == len(ids)
            and sign in (-1, 1)
            and isinstance(n, int)
            and 1 <= n <= 10
            and len(states) <= n
            and set(states)
            <= {
                "filled",
                "flat",
                "partial_flatten",
                "rejected",
                "expired",
                "cancelled",
                "not_sent",
            }
            and bool(ids)
            == any(x in ("filled", "flat", "partial_flatten") for x in states)
        ):
            raise ValueError("invalid settled batch")
        key = (sy, tf, stamp, "BUY" if sign == 1 else "SELL")
        value = enc(sorted(str(v) for v in ids))
        old = c.execute(
            "SELECT deals FROM retest_settled WHERE symbol=? AND tf=? AND signal=? AND side=?",
            key,
        ).fetchone()
        if old and old[0] != value:
            value = "null"  # ambiguous: no complete setup
        c.execute(
            "INSERT OR REPLACE INTO retest_settled VALUES(?,?,?,?,?)", (*key, value)
        )
        return
    q = ev["request"]
    a = ev["audit"]
    bar = ev["trigger_bar"]
    leg = ev["leg"]
    sgn = a["entry_side"]
    tag_text = q["comment"]
    tick, atr, fraction, cap = (
        dec(ev[k]) for k in ("tick", "atr", "fraction", "atr_cap")
    )
    price, sl, tp, qty = (dec(q[k]) for k in ("price", "sl", "tp", "volume"))
    fill, vol, bid, ask = (
        dec(ev[k]) for k in ("price", "fill_lots", "send_bid", "send_ask")
    )
    o, h, l, close = (dec(bar[k]) for k in ("o", "h", "l", "c"))
    wick = l if sgn == 1 else h
    body = sgn * (close - o) > 0
    span = abs(close - o) if body else close - l if sgn == 1 else h - close
    depth = sgn * (close - price)
    upper = min(span, cap * atr)
    wanted = max(tick, min(upper, fraction * span))
    submit, send, filled, exp = (
        ev[k] for k in ("submit_ms", "send_ms", "fill_ms", "expiry")
    )
    entry_age = dec(ev["max_entry_age_s"])
    quote_age = dec(ev["max_quote_age_s"])
    did = ev["deal"]
    parent_end = a["parent_structure_close_s"]
    if not (
        all(
            type(v) is int and v > 0
            for v in (ev["order"], ev["position_id"], did, submit, send, filled, exp)
        )
        and ev["entry_route"] == "direct_retest_limit"
        and ev["pricing_policy"] == "confirmed_pair_direct_retest_v1"
        and sgn in (-1, 1)
        and tag_text.startswith("r")
        and tag(tag_text) == (tf, stamp, leg)
        and q["symbol"] == sy
        and q["magic"] == magic
        and q["action"] == 5
        and q["type"] == (2 if sgn == 1 else 3)
        and q["type_time"] == 2
        and q["type_filling"] == 2
        and q["expiration"] == exp
        and type(leg) is int
        and 1 <= leg <= 10
        and a["entry_policy"] == PAIR_ENTRY
        and a["initial_stop_policy"] == PAIR_STOP
        and a["entry_reason"] == "ready"
        and a["parent_tf"] == tf
        and a["trigger_tf"] == child
        and a["trigger_structure_close_s"] == stamp
        and a["trigger_structure_bias"] == a["parent_structure_bias"] == sgn
        and 0 <= stamp - parent_end < tf * 60
        and bar["t"] == a["cross_candle_open_s"] == stamp - child * 60
        and 0 < l <= min(o, close) <= max(o, close) <= h
        and tick > 0
        and atr > 0
        and 0 < fraction <= 1
        and 0 < cap <= 1
        and tick <= depth <= upper
        and price
        == tick
        * max(
            (min(close - sgn * tick, close - sgn * upper) / tick).to_integral_value(
                rounding=ROUND_CEILING
            ),
            min(
                (max(close - sgn * tick, close - sgn * upper) / tick).to_integral_value(
                    rounding=ROUND_FLOOR
                ),
                ((close - sgn * wanted) / tick).to_integral_value(
                    rounding=ROUND_HALF_UP
                ),
            ),
        )
        and ev["segment"]
        == ("body" if body else "lower_wick" if sgn == 1 else "upper_wick")
        and sl == dec(a["frozen_cross_sl"])
        and wick == dec(a["cross_candle_wick"])
        and (wick - sl) * sgn >= tick
        and min(sl, price, tp, fill) > 0
        and all(v % tick == 0 for v in (sl, price, tp))
        and sgn * (tp - price) >= tick
        and 0 < vol <= qty
        and 0 < bid <= ask
        and sgn * ((ask if sgn == 1 else bid) - price) >= tick
        and sgn * (fill - price) <= 0
        and a.get("mss_skip_exception") in ("", None, "failed_counter_mss_return")
        and 0 < entry_age <= 30
        and 0 < quote_age <= 15
        and stamp * 1000 <= send <= submit <= stamp * 1000 + entry_age * 1000
        and submit - send <= quote_age * 1000
        and submit <= filled < exp * 1000
        and exp <= stamp + child * 60
    ):
        raise ValueError("invalid pending fill receipt")
    proof = {
        "meta": [sy, tag_text, "BUY" if sgn == 1 else "SELL", plain(sl), plain(tp)],
        "broker": [
            str(ev["order"]),
            str(ev["position_id"]),
            filled,
            plain(fill),
            plain(vol),
            magic,
        ],
        "fields": dict(
            zip(
                (*CROSS_SHARED, *SEND_FIELDS, *PAIR_SHARED),
                (
                    PAIR_STOP,
                    plain(wick),
                    plain(sl),
                    a.get("mss_skip_exception") or "",
                    iso(send),
                    plain(bid),
                    plain(ask),
                    iso(parent_end * 1000),
                    "bullish" if sgn == 1 else "bearish",
                ),
                strict=True,
            )
        ),
    }
    proof["fields"].update(
        zip(
            RETEST_FIELDS,
            (
                plain(price),
                plain(close),
                ev["segment"],
                iso(exp * 1000),
                str(filled - submit),
            ),
            strict=True,
        )
    )
    value = enc(proof)
    key = str(did)
    old = c.execute("SELECT evidence FROM retest_deals WHERE id=?", (key,)).fetchone()
    if old and old[0] != value:
        value = "null"
    c.execute("INSERT OR REPLACE INTO retest_deals VALUES(?,?)", (key, value))


def matching_retest(evidence, deal):
    if not evidence or deal["typ"] not in (0, 1) or deal["ent"] != 0:
        return False
    return (
        evidence["broker"]
        == [
            deal["order"],
            deal["pid"],
            deal["ms"],
            plain(deal["price"]),
            plain(deal["volume"]),
            deal["magic"],
        ]
        and evidence["meta"][0] == deal["symbol"]
        and evidence["meta"][2] == ("BUY", "SELL")[deal["typ"]]
    )


def import_log(c, path, magic):
    """Optional exact joins from the account-scoped ARMED log. Never use a preview as a fill."""
    if path is None or not path.is_file():
        return False, False
    old = get(c, "log_cursor", {})
    size = path.stat().st_size
    off = int(old.get("off", 0))
    bad = int(old.get("bad", 0))
    changed = False
    with path.open("rb") as f:
        if (
            old.get("format") != LOG_FORMAT
            or old.get("path") != str(path)
            or off > size
        ):
            off = bad = 0
        if off:
            f.seek(max(off - 96, 0))
            chk = hashlib.sha256(f.read(min(off, 96))).hexdigest()
            if chk != old.get("tail"):
                off = bad = 0
        f.seek(off)
        while f.tell() < size:
            line = f.readline()
            end = f.tell()
            if not line.endswith(b"\n"):
                break  # scanner may be appending
            off = end
            if not any(
                w in line
                for w in (
                    b'"demo_result"',
                    b'"retest_fill_receipt"',
                    b'"retest_batch_settled"',
                    b'"closed_label"',
                    b'"live_hull_cross"',
                    b'"label_decision"',
                    b'"exit_request"',
                )
            ):
                continue
            before = c.total_changes
            try:
                ev = json.loads(line)
                if ev.get("scanner_magic", magic) != magic:
                    continue  # same account's audit can contain several strategy identities
                typ = ev["event"]
                sy = str(ev["symbol"])
                if typ in ("retest_fill_receipt", "retest_batch_settled"):
                    index_retest(c, ev, magic)
                elif typ == "demo_result":
                    q = ev["request"]
                    key = str(int(ev.get("deal") or 0))
                    parsed = tag(q.get("comment"))
                    if (
                        int(key) > 0
                        and ("retcode" not in ev or ev["retcode"] in (10009, 10010))
                        and parsed
                        and not q["comment"].startswith("r")
                        and int(q["magic"]) == magic
                        and q["symbol"] == sy
                        and int(q["type"]) in (0, 1)
                    ):
                        vals = (
                            key,
                            sy,
                            q["comment"],
                            ("BUY", "SELL")[int(q["type"])],
                            plain(q["sl"]),
                            plain(q["tp"]),
                        )
                        c.execute(
                            "INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?,?)", vals
                        )
                        index_cross_receipt(c, ev, vals, parsed)
                elif (
                    typ == "exit_request"
                    and ev.get("retcode") in (10009, 10010)
                    and ev.get("reason")
                ):
                    moment = datetime.fromisoformat(ev["at"])
                    ticket = str(int(ev["ticket"]))
                    if moment.tzinfo is not None and int(ticket) > 0:
                        c.execute(
                            "INSERT OR IGNORE INTO exit_notes VALUES(?,?,?,?)",
                            (
                                sy,
                                ticket,
                                round(moment.timestamp() * 1000),
                                str(ev["reason"]),
                            ),
                        )
                elif typ in ("closed_label", "live_hull_cross") and ev.get(
                    "entry_side"
                ) in (1, -1):
                    if ev.get("entry_policy") == PAIR_ENTRY:
                        continue  # paired setup evidence is joined by receipt/deal, not proximity to a label
                    tf, end = int(ev["tf"]), int(ev["close_s"])
                    side = "BUY" if ev["entry_side"] == 1 else "SELL"
                    stamp = end
                    if typ == "live_hull_cross":
                        stamp = int(ev["trigger_msc"])
                        quote = ev["trigger"]
                        if (
                            tf not in NATIVE_MINUTES
                            or not (end - 60 * tf) * 1000 <= stamp < end * 1000
                            or stamp != quote["ms"]
                            or not 0 < dec(quote["bid"]) <= dec(quote["ask"])
                        ):
                            raise ValueError("Inconsistent live label")
                        c.execute(
                            "INSERT OR IGNORE INTO live_triggers VALUES(?,?,?,?,?,?,?)",
                            (
                                sy,
                                tf,
                                stamp,
                                side,
                                end,
                                plain(quote["bid"]),
                                plain(quote["ask"]),
                            ),
                        )
                    c.execute(
                        "INSERT OR IGNORE INTO labels VALUES(?,?,?,?,?)",
                        (sy, tf, stamp, side, str(ev.get("entry_setup") or "")),
                    )
                elif (
                    typ == "label_decision"
                    and ev.get("tf")
                    and (
                        ev.get("outcome") == "demo_batch_sent"
                        or str(ev.get("outcome", "")).startswith(
                            ("protected_partial_no_topup:", "manual_hold:")
                        )
                        or "manual_hold" in str(ev.get("outcome", ""))
                    )
                ):
                    c.execute(
                        "INSERT OR REPLACE INTO pair_finished VALUES(?,?,?,?)"
                        if ev.get("entry_policy") == PAIR_ENTRY
                        else "INSERT OR REPLACE INTO finished VALUES(?,?,?,?)",
                        (
                            sy,
                            int(ev["tf"]),
                            int(
                                ev["trigger_msc"]
                                if ev.get("entry_timing") == "live_hull_cross"
                                else ev["close_s"]
                            ),
                            str(ev["outcome"]),
                        ),
                    )
            except (
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                ArithmeticError,
                UnicodeDecodeError,
            ):
                bad += 1
            changed |= c.total_changes != before
        f.seek(max(0, off - 96))
        tail = hashlib.sha256(f.read(min(off, 96))).hexdigest()
    put(
        c,
        "log_cursor",
        {"format": LOG_FORMAT, "path": str(path), "off": off, "tail": tail, "bad": bad},
    )
    return changed, True


def sync(c, mt, now, *, days=30, magic=MAGIC, repo=None, reconcile=False):
    """One all-or-nothing broker read. A failed API call never moves the cursor."""
    now = int(now)
    who = identity(mt, magic)
    old = get(c, "identity")
    last = get(c, "until")
    floor = get(c, "floor")
    if old and old != who:
        raise JournalError(
            "Account, currency or magic changed. Select the original account or use a separate --out folder."
        )
    if last is not None and now < last:
        raise JournalError("Clock precedes the saved cursor. Check the host clock.")
    deep = (
        last is None
        or reconcile
        or now - get(c, "deep_at", 0) >= 3600
        or days > get(c, "days", days)
    )
    start = (
        min(last - 300 if last is not None else now, now - days * 86400)
        if deep
        else last - 300
    )
    start = max(floor or 0, start)
    held = set()
    dirty = old is None or (floor is not None and start < floor)
    path = (
        Path(repo).resolve() / "reports" / "hull_runtime" / who["key"] / "demo.jsonl"
        if repo
        else None
    )
    with c:
        hints_changed, has_log = import_log(c, path, magic)
        dirty |= hints_changed
        tracked = {x[0] for x in c.execute("SELECT pid FROM tracked")}
        receipt_tags = dict(c.execute("SELECT id,tag FROM receipts"))
        receipt_ids = receipt_tags.keys()
        retest = {
            k: json.loads(v)
            for k, v in c.execute("SELECT id,evidence FROM retest_deals")
        }
        retest_windows = set(c.execute("SELECT symbol,tf,signal FROM retest_windows"))
        windows = set(c.execute("SELECT symbol,signal FROM windows"))
        covered_from = min(start, floor if floor is not None else start)
        events = defaultdict(dict)
        a = start
        while a <= now:
            b = min(now, a + 7 * 86400)
            ds = mt.history_deals_get(dt(a), dt(b + 1))
            if ds is None:
                raise JournalError(
                    "Broker range history unavailable; no cursor advanced."
                )
            for x in ds:
                if int(x.position_id or 0) > 0:
                    d = norm(x)
                    events[d["pid"]][d["id"]] = d
            a = b + 1
        ps = mt.positions_get()
        if ps is None:
            raise JournalError(
                "Open-position snapshot unavailable; no closes inferred."
            )
        live = {str(int(getattr(p, "identifier", 0) or p.ticket)): p.symbol for p in ps}
        live_ours = {
            str(int(getattr(p, "identifier", 0) or p.ticket))
            for p in ps
            if p.magic == magic
        }
        old_live = set(get(c, "active", []))
        candidates = (
            set(events) | live_ours | (old_live - set(live)) | set(get(c, "retry", []))
        )
        if reconcile:
            candidates |= tracked
        to_retry = set(get(c, "retry", []))
        disappeared = old_live - set(live)
        retry = []
        queue = sorted(candidates, reverse=True)
        visited = set()
        while queue:
            pid = queue.pop()
            if pid in visited:
                continue
            visited.add(pid)
            subset = events.get(pid, {})
            ins = [d for d in subset.values() if d["typ"] in (0, 1) and d["ent"] == 0]
            if (
                pid not in tracked
                and pid not in live_ours
                and ins
                and not any(d["magic"] == magic or d["id"] in receipt_ids for d in ins)
            ):
                continue
            saved = (
                dict(c.execute("SELECT id,body FROM deals WHERE pid=?", (pid,)))
                if pid in tracked
                else {}
            )
            need = (
                not saved
                or pid in disappeared
                or pid in to_retry
                or reconcile
                or any(saved.get(k) != enc(v) for k, v in subset.items())
            )
            if not need:
                continue
            whole = mt.history_deals_get(position=int(pid))
            if whole is None:
                raise JournalError(
                    "Position history unavailable; retrying without committing partial data."
                )
            ds = dict(subset)
            for x in whole:
                d = norm(x)
                if d["pid"] != pid:
                    raise JournalError(
                        "Inconsistent position identifier in broker response."
                    )
                ds[d["id"]] = d
            entries = [d for d in ds.values() if d["typ"] in (0, 1) and d["ent"] == 0]
            owned = any(d["magic"] == magic or d["id"] in receipt_ids for d in entries)
            # Incomplete linkage: retain as unresolved; an unrelated exit is not a guessed Hull trade.
            if not entries:
                retry.append(pid)
            if not (
                owned
                or pid in tracked
                or pid in live_ours
                or (not entries and any(d["magic"] == magic for d in ds.values()))
            ):
                continue
            # A long-lived ticket can close inside the backfill but open before it. Fetch its
            # original signal window to find siblings (including legs already flat before the window).
            for ent in entries:
                evidence = retest.get(ent["id"])
                verified = matching_retest(evidence, ent)
                text = (
                    ent["comment"]
                    if tag(ent["comment"])
                    else evidence["meta"][1]
                    if verified
                    else receipt_tags.get(ent["id"])
                )
                ref = tag(text)
                sy = ent["symbol"]
                if not ref:
                    continue
                tf, stamp, _ = ref
                resting = text.startswith("r")
                lower, upper = signal_window(stamp)
                if resting:
                    upper = stamp + PAIRS[tf] * 60
                known_window = (
                    (sy, tf, stamp) in retest_windows
                    if resting
                    else (sy, stamp) in windows
                )
                if lower >= covered_from or upper > now or known_window:
                    continue
                related = mt.history_deals_get(dt(lower - 1), dt(upper + 1))
                if related is None:
                    raise JournalError(
                        "Original setup entry window unavailable; poll not committed."
                    )
                for x in related:
                    if (
                        x.symbol != sy
                        or x.entry != 0
                        or x.type not in (0, 1)
                        or not x.position_id
                    ):
                        continue
                    d = norm(x)
                    other = tag(d["comment"]) or tag(receipt_tags.get(d["id"]))
                    if (d["magic"] == magic or d["id"] in receipt_ids) and (
                        other is None or other[1] == stamp
                    ):
                        events[d["pid"]][d["id"]] = d
                        if d["pid"] not in visited:
                            queue.append(d["pid"])
                if resting:
                    retest_windows.add((sy, tf, stamp))
                    c.execute(
                        "INSERT OR IGNORE INTO retest_windows VALUES(?,?,?)",
                        (sy, tf, stamp),
                    )
                else:
                    windows.add((sy, stamp))
                    c.execute("INSERT OR IGNORE INTO windows VALUES(?,?)", (sy, stamp))
                dirty = True
            if (
                entries
                and pid not in live
                and sum((D(d["volume"]) for d in entries), D(0))
                != sum(
                    (
                        D(d["volume"])
                        for d in ds.values()
                        if d["typ"] in (0, 1) and d["ent"] in (1, 3)
                    ),
                    D(0),
                )
            ):
                retry.append(pid)
            if pid not in tracked:
                tracked.add(pid)
                c.execute("INSERT INTO tracked VALUES(?)", (pid,))
                dirty = True
            for k, d in ds.items():
                body = enc(d)
                if saved.get(k) != body:
                    c.execute(
                        "INSERT OR REPLACE INTO deals VALUES(?,?,?)", (k, pid, body)
                    )
                    dirty = True
        held = set(live) & tracked
        if identity(mt, magic) != who:
            raise JournalError(
                "Account changed during history read. Entire poll discarded."
            )
        dirty |= held != old_live or set(retry) != set(get(c, "retry", []))
        active_meta = {
            str(int(getattr(p, "identifier", 0) or p.ticket)): [
                str(p.symbol),
                int(getattr(p, "time", 0) or 0),
            ]
            for p in ps
            if str(int(getattr(p, "identifier", 0) or p.ticket)) in held
        }
        # A disappearing live position with no deals yet must keep blocking its possible
        # siblings until the broker publishes its missing history.
        old_meta = get(c, "active_meta", {})
        for pid in retry:
            if pid not in active_meta and pid in old_meta:
                active_meta[pid] = old_meta[pid]
        dirty |= old_meta != active_meta
        for k, v in {
            "active_meta": active_meta,
            "identity": who,
            "until": now,
            "floor": min(start, floor if floor is not None else start),
            "days": days,
            "active": sorted(held),
            "retry": sorted(retry),
            "has_log": has_log,
        }.items():
            put(c, k, v)
        if deep:
            put(c, "deep_at", now)
    return dirty


def cash_out(row, comps, digits):
    net = sum(comps, D(0)).quantize(D(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    row.update(zip(CASH, map(plain, comps)))
    row["net_profit"] = format(abs(net) if net == 0 else net, "f")
    row["outcome"] = "Win" if net > 0 else "Loss" if net < 0 else "Breakeven"


def price(n):
    return plain(n.quantize(D("0.0000000001"), rounding=ROUND_HALF_UP))


def make_views(c):
    who = get(c, "identity")
    now = get(c, "until")
    active = set(get(c, "active", []))
    floor = get(c, "floor")
    receipts = {r[0]: r[1:] for r in c.execute("SELECT * FROM receipts")}
    retest = {
        k: json.loads(v) for k, v in c.execute("SELECT id,evidence FROM retest_deals")
    }
    settled = {
        (sy, tf, ts, side): json.loads(v)
        for sy, tf, ts, side, v in c.execute("SELECT * FROM retest_settled")
    }
    cross = {
        did: dict(
            zip(
                (*CROSS_SHARED, *SEND_FIELDS),
                (
                    PAIR_STOP
                    if receipts[did][1].startswith("p")
                    else M1_POLICY
                    if receipts[did][1].startswith("m")
                    else CROSS_POLICY,
                    wick,
                    sl,
                    waiver,
                    iso(ms),
                    bid,
                    ask,
                ),
                strict=True,
            )
        )
        for did, wick, sl, waiver, ms, bid, ask in c.execute(
            "SELECT * FROM cross_receipts WHERE send_ms>0"
        )
        if did in receipts
    }
    contexts = {
        did: (iso(end * 1000), "bullish" if sign == 1 else "bearish")
        for did, end, sign in c.execute("SELECT * FROM pair_context")
    }
    for did, proof in cross.items():
        proof.update(zip(PAIR_SHARED, contexts.get(did, ("", "")), strict=True))
    notes = defaultdict(list)
    for sy, number, ms, why in c.execute(
        "SELECT symbol,ticket,at_ms,why FROM exit_notes"
    ):
        notes[(sy, number)].append((ms, why))
    seen_deals = set()
    labels = {
        (s, t, stamp, side): model
        for s, t, stamp, side, model in c.execute("SELECT * FROM labels")
    }
    live_triggers = {
        (s, t, stamp, side): (end, bid, ask)
        for s, t, stamp, side, end, bid, ask in c.execute("SELECT * FROM live_triggers")
    }
    finished = {
        (s, t, stamp): o for s, t, stamp, o in c.execute("SELECT * FROM finished")
    }
    pair_finished = {
        (s, t, stamp): o for s, t, stamp, o in c.execute("SELECT * FROM pair_finished")
    }
    windows = set(c.execute("SELECT symbol,signal FROM windows"))
    retest_windows = set(c.execute("SELECT symbol,tf,signal FROM retest_windows"))
    live_meta = get(c, "active_meta", {})
    raw = defaultdict(list)
    for pid, body in c.execute("SELECT pid,body FROM deals"):
        raw[pid].append(json.loads(body))
    tickets = []
    setups = []
    members = defaultdict(list)
    unknown = defaultdict(list)
    reviews = 0
    unlinked = 0
    opens = 0
    due = None
    base = {k: "" for k in FIELDS}
    base.update(schema_version="1", account_key=who["key"], currency=who["currency"])
    for (pid,) in c.execute("SELECT pid FROM tracked"):
        ds = sorted(raw[pid], key=lambda x: (x["ms"], int(x["id"])))
        seen_deals.update(d["id"] for d in ds)
        ins = [d for d in ds if d["typ"] in (0, 1) and d["ent"] == 0]
        outs = [d for d in ds if d["typ"] in (0, 1) and d["ent"] in (1, 3)]
        if not ins:
            reviews += 1
            if ds:
                unknown[ds[0]["symbol"]].append((0, ds[-1]["ms"] // 1000))
            elif pid in live_meta:
                sy, opened = live_meta[pid]
                unknown[sy].append((opened - 2, opened + 2) if opened else (0, now))
            continue
        a = ins[0]
        sy = a["symbol"]
        side = ("BUY", "SELL")[a["typ"]]
        refs = set()
        tag_kinds = set()
        sources = set()
        brackets, proofs = [], []
        for d in ins:
            meta = receipts.get(d["id"])
            evidence = retest.get(d["id"])
            if matching_retest(evidence, d) and not meta:
                meta = evidence["meta"]
                proofs.append(evidence["fields"])
            k = tag(d["comment"])
            if k:
                refs.add(k)
                tag_kinds.add(d["comment"][0])
                sources.add("Entry comment")
            if meta and meta[0] == sy and meta[2] == side:
                k = tag(meta[1])
                refs.add(k)
                tag_kinds.add(meta[1][0])
                sources.add("Bot receipt")
                brackets.append((meta[3], meta[4]))
                if k and d["id"] in cross:
                    proofs.append(cross[d["id"]])
        start = a["ms"]
        link = next(iter(refs)) if len(refs) == 1 and len(tag_kinds) == 1 else None
        is_minute = tag_kinds == {"m"}
        pending = tag_kinds == {"r"}
        paired = tag_kinds == {"p"} or pending
        model = ""
        gid = ""
        r = None
        if link:
            tf, stamp, _leg = link
            lane = f"{tf_name(tf)}>{tf_name(PAIRS[tf])}" if paired else tf_name(tf)
            if pending:
                lane = "R:" + lane
            gid = f"{sy}|{lane}|{stamp}|{side}"
            model = (
                (
                    ("direct_retest_limit" if pending else "paired_structure_cross")
                    if len(proofs) == len(ins)
                    else ""
                )
                if paired
                else labels.get((sy, tf, stamp, side), "")
            )
        else:
            unknown[sy].append((start // 1000 - 2, start // 1000 + 2))
        vol = sum((D(d["volume"]) for d in ins), D(0))
        exited = sum((D(d["volume"]) for d in outs), D(0))
        invalid = (
            not sy
            or any(
                d["typ"] in (13, 14)
                or (
                    d["typ"] in (0, 1)
                    and (
                        d["ent"] not in (0, 1, 3)
                        or d["symbol"] != sy
                        or D(d["volume"]) <= 0
                        or D(d["price"]) <= 0
                    )
                )
                for d in ds
            )
            or any(
                d["typ"] != a["typ"]
                or not (d["magic"] == who["magic"] or d["id"] in receipts)
                for d in ins
            )
            or any(d["typ"] == a["typ"] or d["ms"] < start for d in outs)
            or exited > vol
        )
        if invalid:
            reviews += 1
        elif pid in active:
            opens += 1
        elif vol <= 0 or not outs or vol != exited:
            reviews += 1
        else:
            end = outs[-1]["ms"]
            r = base.copy()
            last = outs[-1]
            why = (
                "Close by"
                if last["ent"] == 3
                else "Hull exit"
                if last["comment"] == "hull_exit" and last["reason"] == 3
                else REASONS.get(last["reason"], "Reason " + str(last["reason"]))
            )
            r.update(
                record_type="ticket",
                symbol=sy,
                timeframe=tf_name(link[0]) if link else "Unknown",
                side=side,
                position_id=pid,
                setup_id=gid,
                leg=str(link[2]) if link else "",
                ticket_count="1",
                setup_type=model,
                open_time_utc=iso(start),
                close_time_utc=iso(end),
                duration_s=plain(D(end - start) / 1000),
                lots=plain(vol),
                open_price=price(
                    sum(D(d["price"]) * D(d["volume"]) for d in ins) / vol
                ),
                close_price=price(
                    sum(D(d["price"]) * D(d["volume"]) for d in outs) / vol
                ),
                exit_reason=why,
                opening_order=a["order"],
                entry_deals=";".join(d["id"] for d in ins),
                exit_deals=";".join(d["id"] for d in outs),
                initial_sl=brackets[0][0] if brackets else "",
                initial_tp=brackets[0][1] if brackets else "",
                attribution=" + ".join(sorted(sources)) if link else "Unattributed",
            )
            if link:
                is_live = stamp >= LIVE_KEY_MIN
                r["entry_timing"] = (
                    "live_hull_cross"
                    if is_live
                    else "confirmed_pair_retest_limit"
                    if pending
                    else "confirmed_pair_close"
                    if paired
                    else "confirmed_m1_close"
                    if is_minute
                    else "confirmed_close"
                )
                r["signal_trigger_utc"] = iso(trigger_ms(stamp))
                if not is_live:
                    # All component fills must have exact, non-conflicting receipts.
                    # Never infer these fields from a start time or the nearest setup.
                    if len(proofs) == len(ins):
                        for k in (
                            *CROSS_SHARED,
                            *SEND_FIELDS,
                            *PAIR_SHARED,
                            *(RETEST_FIELDS if pending else ()),
                        ):
                            vs = {v[k] for v in proofs}
                            if len(vs) == 1:
                                r[k] = vs.pop()
                    r["signal_close_utc"] = r["signal_trigger_utc"]
                    bar_end = stamp
                else:
                    bar_end, r["trigger_bid"], r["trigger_ask"] = live_triggers.get(
                        (sy, tf, stamp, side), (None, "", "")
                    )
                if paired:
                    r["trigger_timeframe"] = tf_name(PAIRS[tf])
                if bar_end is not None:
                    r["signal_bar_open_utc"] = iso(
                        (
                            bar_end
                            - (PAIRS[tf] if paired else 1 if is_minute else tf) * 60
                        )
                        * 1000
                    )
                    r["signal_bar_end_utc"] = iso(bar_end * 1000)
            # The audit contains the position ticket but no exit deal ID. Show the
            # request separately, never overwrite the final broker deal reason.
            if why == "Hull exit":
                hits = [
                    (ms, code)
                    for ms, code in notes.get((sy, pid), [])
                    if max(start, end - 5000) <= ms <= end + 15000
                ]
                near = [
                    d
                    for d in outs
                    if d["comment"] == "hull_exit" and abs(d["ms"] - end) <= 20000
                ]
                if len(hits) == 1 and len(near) == 1:
                    ms, code = hits[0]
                    r["bot_exit_request"] = code
                    r["bot_exit_request_utc"] = iso(ms)
            cash_out(
                r,
                [
                    sum(D(d[k]) for d in ds)
                    for k in ("profit", "commission", "swap", "fee")
                ],
                who["digits"],
            )
            tickets.append(r)
            if not link:
                unlinked += 1
        if link:
            members[(sy, tf, stamp, side, "r" if pending else paired)].append(r)
    missing = defaultdict(set)
    for did, (sy, comment, side, _, _) in receipts.items():
        ref = tag(comment)
        if ref and did not in seen_deals:
            missing[(sy, ref[0], ref[1], side, comment.startswith("p"))].add(did)
    wait = 0
    for key, rs in members.items():
        sy, tf, stamp, side, paired = key
        begin_window, end_window = signal_window(stamp)
        if paired == "r":
            end_window = max(end_window, stamp + PAIRS[tf] * 60)
        result = (pair_finished if paired else finished).get((sy, tf, stamp), "")
        # A receipt without a broker deal, an unresolved sibling or any open leg prevents completion.
        final_receipt = (
            result == "demo_batch_sent"
            or result.startswith("protected_partial_no_topup:")
        ) and all(
            r is not None
            and any(did in receipts for did in r["entry_deals"].split(";"))
            for r in rs
        )
        blocked = (
            None in rs
            or key in missing
            or (
                floor > begin_window
                and (
                    (sy, tf, stamp) not in retest_windows
                    if paired == "r"
                    else (sy, stamp) not in windows
                )
            )
            or any(
                a <= end_window and b >= begin_window for a, b in unknown.get(sy, [])
            )
        )
        if paired == "r":
            ids = sorted(
                did for x in rs if x is not None for did in x["entry_deals"].split(";")
            )
            final_receipt = settled.get((sy, tf, stamp, side)) == ids and all(
                x and x["setup_type"] == "direct_retest_limit" for x in rs
            )
            blocked = blocked or not final_receipt
        if blocked or (not final_receipt and now < end_window):
            wait += 1
            if not blocked and now < end_window:
                due = end_window if due is None else min(due, end_window)
            continue
        r = base.copy()
        first = min(rs, key=lambda x: x["open_time_utc"])
        last = max(rs, key=lambda x: x["close_time_utc"])
        vol = sum(D(x["lots"]) for x in rs)
        dur = datetime.fromisoformat(last["close_time_utc"]) - datetime.fromisoformat(
            first["open_time_utc"]
        )
        r.update(
            record_type="setup",
            symbol=sy,
            timeframe=tf_name(tf),
            side=side,
            setup_id=first["setup_id"],
            ticket_count=str(len(rs)),
            setup_type=first["setup_type"]
            if not paired
            or all(
                x["setup_type"]
                == (
                    "direct_retest_limit" if paired == "r" else "paired_structure_cross"
                )
                for x in rs
            )
            else "",
            **{
                k: first[k]
                for k in (
                    "signal_close_utc",
                    "trigger_timeframe",
                    "entry_timing",
                    "signal_trigger_utc",
                    "signal_bar_open_utc",
                    "signal_bar_end_utc",
                    "trigger_bid",
                    "trigger_ask",
                )
            },
            open_time_utc=first["open_time_utc"],
            close_time_utc=last["close_time_utc"],
            duration_s=plain(dur.total_seconds()),
            lots=plain(vol),
            open_price=price(sum(D(x["open_price"]) * D(x["lots"]) for x in rs) / vol),
            close_price=price(
                sum(D(x["close_price"]) * D(x["lots"]) for x in rs) / vol
            ),
            exit_reason=rs[0]["exit_reason"]
            if len({x["exit_reason"] for x in rs}) == 1
            else "Mixed exits",
            attribution="Exact signal group",
        )
        # A setup may have many executable quotes. Leave those ticket-only; show
        # stop evidence only if ALL of its settled tickets agree on the same stop.
        shared = (
            *CROSS_SHARED,
            *PAIR_SHARED,
            *(RETEST_FIELDS[:-1] if paired == "r" else ()),
        )
        frozen = {tuple(x[k] for k in shared) for x in rs}
        if len(frozen) == 1:
            r.update(zip(shared, frozen.pop(), strict=True))
        noted = {x["bot_exit_request"] for x in rs}
        r["bot_exit_request"] = (
            next(iter(noted)) if len(noted) == 1 else "Varies by ticket"
        )
        r["bot_exit_request_utc"] = (
            rs[0]["bot_exit_request_utc"] if len(rs) == 1 else ""
        )
        cash_out(r, [sum(D(x[k]) for x in rs) for k in CASH], who["digits"])
        setups.append(r)
    for arr in (tickets, setups):
        for row in arr:
            add_dimensions(row)
        arr.sort(
            key=lambda x: (
                x["close_time_utc"],
                x["symbol"],
                x["position_id"] or x["setup_id"],
            )
        )
    return (
        {"tickets": tickets, "setups": setups},
        {
            "open_tickets": opens,
            "review": reviews,
            "unattributed": unlinked,
            "pending_setups": wait,
            "unmatched_receipts": sum(map(len, missing.values())),
            "history_retries": len(get(c, "retry", [])),
        },
        due,
    )


def cell(k, s):
    s = str(s)
    return (
        "'" + s
        if k not in NUMERIC
        and (s.lstrip()[:1] in ("=", "+", "-", "@") or s.startswith(("\t", "\r", "\n")))
        else s
    )


def csv_bytes(rows):
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(FIELDS)
    for row in rows:
        w.writerow([cell(k, row[k]) for k in FIELDS])
    return buf.getvalue().encode("utf-8-sig")


def atomic(path, data):
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            prefix="." + path.name + ".",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as f:
            tmp = Path(f.name)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
