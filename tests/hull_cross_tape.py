"""Feed the unmodified production scanner synthetic native candles and a fake MT5.
No credentials, terminal, network or real orders. Files are confined to the test directory.
"""

import sys
from dataclasses import replace
from types import SimpleNamespace as NS

from hull_mt import MT, ROOT, T, scanner_clock
from test_hull_failed_mss import bars

from bot.config.loader import load_yaml
from bot.hull.bar import Bar
from bot.hull.config import HullConfig, selection
from bot.hull.terminal import run


def as_rate(b):
    return {"time": b.t, "open": b.o, "high": b.h, "low": b.l, "close": b.c}


def run_tape(
    tmp_path, monkeypatch, tf, side, case="confirmed", *, armed=True, history=()
):
    path = bars(side, tf)
    original = path[-1]
    quiet = original.c - 2 * side
    # The widest excursion is not in the last M15 constituent. This also makes
    # the signal wick different from any previous-pullback or next-bar anchor.
    outer = quiet - 0.5 * side
    last = Bar(
        original.t,
        original.o,
        max(original.h, outer),
        min(original.l, outer),
        original.c,
    )
    successor = original.c + 0.04 * side
    times = [T - 2.0, T - 0.5, T + 0.125, T + 0.4, T + 0.7]
    if case == "late_tick":
        times[-2:] = [T + 8.125, T + 8.4]
    phase = [0]
    wall = lambda: times[phase[0]]

    def endbar():
        c = (
            quiet
            if phase[0] == 0 or (case == "vanishes" and phase[0] >= 2)
            else original.c
        )
        return replace(last, c=c)

    def chart_rows(_name, period, pos, count):
        assert period == (30 if tf == 30 else 15)
        later = Bar(T, successor, successor, successor, successor)
        tape = path[:-1] + [endbar(), later]
        if tf == 45:
            base = []
            for b in tape:
                base.extend(
                    [
                        Bar(b.t, b.o, b.h, b.l, b.o),
                        Bar(b.t + 900, b.o, b.o, b.o, b.o),
                        Bar(
                            b.t + 1800,
                            b.o,
                            max(b.o, b.c, quiet if b.t == last.t else b.o),
                            min(b.o, b.c, quiet if b.t == last.t else b.o),
                            b.c,
                        ),
                    ]
                )
            tape = base
        tape = [b for b in tape if b.t <= wall()]
        return [as_rate(b) for b in (tape[:-1] if pos else tape)[-count:]]

    def days(_name, period, start, end):
        assert period == mt.TIMEFRAME_D1
        day = int(start.timestamp()) // 86400 * 86400
        return [
            {"time": v}
            for v in range(day, int(end.timestamp()) + 1, 86400)
            if v >= start.timestamp()
        ]

    mt = MT()
    mt.deals.extend(history)
    mt.copy_rates_from_pos = chart_rows
    mt.copy_rates_range = days
    # A trade tick is 0.025, not the configured pip 0.01 or the broker point .001.
    mt.i.trade_tick_size = 0.025

    def tick(_name):
        before = phase[0] < 2
        mark = endbar().c if before else successor
        # MT reports shift-1 before the newest terminal tick. Do not price an
        # order off this pre-close quote; wait only within the original deadline.
        ms = T * 1000 - 1 if phase[0] == 2 else int(wall() * 1000)
        mt.q = NS(time_msc=ms, bid=mark, ask=mark + 0.05)
        return mt.q

    mt.symbol_info_tick = tick
    mt.initialize = lambda: True
    mt.shutdown = lambda: None
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt)
    scanner_clock(monkeypatch, wall, lambda _: phase.__setitem__(0, phase[0] + 1))
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["run"]["mode"] = "demo"
    cfg["hull"]["eligible_timeframes"] = [f"M{tf}"]
    cfg["hull"]["indicator"]["swing_len"] = 1
    h = HullConfig.model_validate(cfg["hull"])
    stamps = []
    prechecks = []
    send = mt.order_send
    check = mt.order_check

    def capture(req):
        stamps.append(mt.q.time_msc)
        return send(req)

    mt.order_send = capture

    def capture_check(req):
        prechecks.append(dict(req))
        return check(req)

    mt.order_check = capture_check
    run(cfg, h, selection(h, rate="medium"), armed=armed, iterations=len(times))
    return mt, path, last, outer, successor, times, stamps, prechecks
