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
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from hull_analytics import add_dimensions

MAGIC = 26111825
ENTRY_WINDOW = (
    60  # The shipped scanner caps entry age at 30s. Allow late sequential receipts.
)
TAG = re.compile(r"h(1|2|5)_(\d{9,11})_([1-9]\d{0,3})\Z")
LIVE_TAG = re.compile(r"i(1|2|5)_([1-9]\d{11,13})_([1-9]\d{0,3})\Z")
LIVE_KEY_MIN = 10**11  # disjoint from every accepted legacy seconds key
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


def tag(s):
    text = str(s or "")
    m = TAG.fullmatch(text) or LIVE_TAG.fullmatch(text)
    if not m:
        return None
    tf, stamp, leg = map(int, m.groups())
    maximum = 32_503_680_000 * (1000 if text.startswith("i") else 1)
    return (tf, stamp, leg) if 0 < stamp < maximum else None


def trigger_ms(key):
    return key if key >= LIVE_KEY_MIN else key * 1000


def signal_window(key):
    ms = trigger_ms(key)
    return ms // 1000, (ms + 999) // 1000 + ENTRY_WINDOW


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
        if old.get("format") != 3 or old.get("path") != str(path) or off > size:
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
                    b'"closed_label"',
                    b'"live_hull_cross"',
                    b'"label_decision"',
                    b'"exit_request"',
                )
            ):
                continue
            try:
                ev = json.loads(line)
                typ = ev["event"]
                sy = str(ev["symbol"])
                before = c.total_changes
                if typ == "demo_result":
                    q = ev["request"]
                    key = str(int(ev.get("deal") or 0))
                    parsed = tag(q.get("comment"))
                    if (
                        key != "0"
                        and parsed
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
                    tf, end = int(ev["tf"]), int(ev["close_s"])
                    side = "BUY" if ev["entry_side"] == 1 else "SELL"
                    stamp = end
                    if typ == "live_hull_cross":
                        stamp = int(ev["trigger_msc"])
                        quote = ev["trigger"]
                        if (
                            tf not in (1, 2, 5)
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
                        "INSERT OR REPLACE INTO finished VALUES(?,?,?,?)",
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
                changed |= c.total_changes != before
            except (
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                ArithmeticError,
                UnicodeDecodeError,
            ):
                bad += 1
        f.seek(max(0, off - 96))
        tail = hashlib.sha256(f.read(min(off, 96))).hexdigest()
    put(
        c,
        "log_cursor",
        {"format": 3, "path": str(path), "off": off, "tail": tail, "bad": bad},
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
    start = max(0, start)
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
                ref = tag(ent["comment"]) or tag(receipt_tags.get(ent["id"]))
                sy = ent["symbol"]
                if not ref:
                    continue
                stamp = ref[1]
                lower, upper = signal_window(stamp)
                if lower >= covered_from or upper > now or (sy, stamp) in windows:
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
    windows = set(c.execute("SELECT symbol,signal FROM windows"))
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
        sources = set()
        brackets = []
        for d in ins:
            meta = receipts.get(d["id"])
            k = tag(d["comment"])
            if k:
                refs.add(k)
                sources.add("Entry comment")
            if meta and meta[0] == sy and meta[2] == side:
                k = tag(meta[1])
                refs.add(k)
                sources.add("Bot receipt")
                brackets.append((meta[3], meta[4]))
        start = a["ms"]
        link = next(iter(refs)) if len(refs) == 1 else None
        model = ""
        gid = ""
        r = None
        if link:
            tf, stamp, _leg = link
            gid = f"{sy}|M{tf}|{stamp}|{side}"
            model = labels.get((sy, tf, stamp, side), "")
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
                timeframe=f"M{link[0]}" if link else "Unknown",
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
                r["entry_timing"] = "live_hull_cross" if is_live else "confirmed_close"
                r["signal_trigger_utc"] = iso(trigger_ms(stamp))
                if not is_live:
                    r["signal_close_utc"] = r["signal_trigger_utc"]
                    bar_end = stamp
                else:
                    bar_end, r["trigger_bid"], r["trigger_ask"] = live_triggers.get(
                        (sy, tf, stamp, side), (None, "", "")
                    )
                if bar_end is not None:
                    r["signal_bar_open_utc"] = iso((bar_end - tf * 60) * 1000)
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
            members[(sy, tf, stamp, side)].append(r)
    missing = defaultdict(set)
    for did, (sy, comment, side, _, _) in receipts.items():
        ref = tag(comment)
        if ref and did not in seen_deals:
            missing[(sy, ref[0], ref[1], side)].add(did)
    wait = 0
    for key, rs in members.items():
        sy, tf, stamp, side = key
        begin_window, end_window = signal_window(stamp)
        result = finished.get((sy, tf, stamp), "")
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
            or (floor > begin_window and (sy, stamp) not in windows)
            or any(
                a <= end_window and b >= begin_window for a, b in unknown.get(sy, [])
            )
        )
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
            timeframe=f"M{tf}",
            side=side,
            setup_id=first["setup_id"],
            ticket_count=str(len(rs)),
            setup_type=first["setup_type"],
            **{
                k: first[k]
                for k in (
                    "signal_close_utc",
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
