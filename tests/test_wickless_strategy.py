"""Executable boundaries: touched does not imply respected, and failed watches never come back."""

import csv
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from wickless_qa.fixture import fixture

from bot.wickless.config import Config, Rules, load
from bot.wickless.engine import TF, Bar, Engine, Zone, ticks, verdict
from bot.wickless.replay import replay, rows

ROOT = Path(__file__).resolve().parents[1]
B = 1704067200
Z = Zone(1, B, 0, 100, 103, 95)


def mirror_bar(b):
    return Bar(b.t, 220 - b.o, 220 - b.l, 220 - b.h, 220 - b.c)


def mirror_zone(z):
    return Zone(-z.side, z.born, z.index, 220 - z.hi, 220 - z.lo, 220 - z.sl)


@pytest.mark.parametrize(
    "ohlc,age,trend,why",
    [
        ((100, 103, 99, 102), 1, 1, "respected"),  # inside, wick-through, no SL touch
        ((101, 107, 103 - 3, 106), 10, 1, "respected"),  # close away, last possible bar
        ((99, 101, 97, 100), 2, 1, "respected"),  # exact base is valid
        ((103, 105, 103, 105), 1, 1, "respected"),  # exact near-edge touch
        (
            (97, 101, 96, 99),
            1,
            1,
            "disrespected",
        ),  # bullish body and touch cannot rescue close below
        ((97, 102, 96, 98), 1, 1, "disrespected"),
        (
            (101, 106, 95, 105),
            1,
            1,
            "stop_touched",
        ),  # SL exactly touched wins over rejection
        ((100, 106, 99, 105), 1, -1, "trend_changed"),
        ((100, 106, 99, 105), 1, 0, "trend_changed"),
        ((106, 108, 104, 107), 9, 1, ""),  # favorable close but no overlap
        ((106, 108, 104, 107), 10, 1, "expired"),
        ((104, 105, 99, 104), 2, 1, ""),  # a doji is not a rejection
        ((104, 106, 99, 101), 2, 1, ""),  # wrong candle colour, valid zone -> wait
        ((100, 103, 99, 102), 11, 1, "expired"),
        ((100, 103, 99, 102), 0, 1, ""),
    ],
)
@pytest.mark.parametrize("flip", [False, True])
def test_zone_decisions(ohlc, age, trend, why, flip):
    b = Bar(B + TF * age, *ohlc)
    z = Z
    if flip:
        b, z, trend = mirror_bar(b), mirror_zone(z), -trend
    assert verdict(z, b, age, trend, 10) == why


@pytest.mark.parametrize(
    "ohlc,raw",
    [
        ((100, 109, 100, 106), 1),
        ((100, 100, 91, 94), -1),
        ((100, 104, 95, 100), 0),
        ((100, 106, 98, 105), 0),
        ((100, 102, 94, 95), 0),
    ],
)
def test_raw_in_neutral_is_never_a_setup(ohlc, raw):
    x = Engine(Rules(), 3).update(Bar(B, *ohlc))
    assert (x.raw, x.trend, x.armed, x.signal) == (raw, 0, None, None)


def test_wick_tolerance_and_min_body():
    assert (
        Engine(Rules(tolerance_ticks=1, min_body_ticks=3), 3)
        .update(Bar(B, 100, 104, 99, 103))
        .raw
        == 1
    )
    assert (
        Engine(Rules(tolerance_ticks=1, min_body_ticks=4), 3)
        .update(Bar(B, 100, 104, 99, 103))
        .raw
        == 0
    )


def test_newest_wins_and_cancel_never_resurrects():
    e = Engine(Rules(), 3)
    e.structure.bias = 1
    e.structure.i = 3
    e.last = B + 3 * TF
    older = Zone(1, B, 0, 100, 103, 95)
    new = Zone(1, B + TF, 1, 99, 105, 94)
    broken = Zone(1, B + 2 * TF, 2, 105, 109, 88)
    e.pending = [older, new, broken]
    d = e.update(Bar(B + 4 * TF, 100, 107, 98, 102))
    assert d.signal.zone == new and d.ended == [
        (broken.born, "disrespected"),
        (new.born, "respected"),
        (older.born, "same_close_newer_setup"),
    ]
    assert not e.pending
    assert e.update(Bar(B + 5 * TF, 103, 108, 99, 107)).signal is None


def test_gap_cancels_duplicate_fails():
    e = Engine(Rules(), 3)
    e.structure.bias = 1
    e.last = B
    e.pending = [Z]
    b = Bar(B + 2 * TF, 101, 107, 98, 106)
    o = e.update(b)
    assert o.signal is None and o.ended == [(B, "data_gap")]
    with pytest.raises(ValueError):
        e.update(b)


def test_long_and_short_are_exact_mirrors_no_lookahead():
    bs = fixture()
    a = Engine(Rules(), 30)
    m = Engine(Rules(), 30)
    snap = []
    entry_sides = set()
    events = set()
    for i, (t, o, h, l, c) in enumerate(bs):
        x = a.update(Bar(t, o, h, l, c))
        y = m.update(Bar(t, 20000 - o, 20000 - l, 20000 - h, 20000 - c))
        assert (x.raw, x.trend, x.event) == (-y.raw, -y.trend, y.event)
        assert bool(x.signal) == bool(y.signal) and bool(x.armed) == bool(y.armed)
        if x.armed:
            assert x.armed.lo == 20000 - y.armed.hi and x.armed.sl == 20000 - y.armed.sl
        if x.signal:
            s = x.signal
            entry_sides.add(s.zone.side)
            assert s.zone.side == x.trend and s.entry == c and s.close_s == t + TF
            assert (
                t - s.zone.born >= TF
                and s.zone.sl == 20000 - y.signal.zone.sl
                and s.tp == 20000 - y.signal.tp
            )
            assert abs(s.entry - s.zone.sl) == abs(s.tp - s.entry) > 0
        events.add(x.event)
        snap.append(x)
    assert events == {"", "mss", "bos"} and entry_sides == {-1, 1}
    e = Engine(Rules(), 30)
    assert [e.update(Bar(*r)) for r in bs[:412]] == snap[:412]


def test_broker_schema_and_pip_conventions():
    cfg = load(ROOT / "wickless.example.json")
    p = cfg.pairs[0]
    assert p.zone_ticks(cfg.rules, 0.00001) == 30
    assert p.model_copy(update={"symbol": "XAUUSD.r", "pip_size": 0}).pip(
        0.001
    ) == Decimal(".1")
    assert p.model_copy(update={"pip_size": 0}).pip(0.00001) == Decimal(".00010")
    for bad in (
        {"mode": "live"},
        {"mode": "demo", "server": ""},
        {"pairs": []},
        {"pairs": [p.model_dump()] * 2},
        {"rr": 3},
        {"rules": {"use_structure": False}},
        {"pairs": [p.model_dump() | {"lots": float("nan")}]},
        {"pairs": [p.model_dump() | {"symbol": "$(bad)"}]},
    ):
        with pytest.raises(ValidationError):
            Config.model_validate(cfg.model_dump() | bad)
    for px in (1.234567, float("nan"), -1):
        with pytest.raises(ValueError):
            ticks(px, 0.00001)


def test_published_chart_smoke_and_every_ledger_level(tmp_path):
    folder = ROOT / "research/wickless_m15"
    cfg = load(ROOT / "wickless.example.json")
    f = tmp_path / "out.csv"
    s = replay(
        rows(folder / "EURUSDm-M15.csv", 0.00001), cfg.pairs[0], cfg.rules, 0.00001, f
    )
    assert s == json.loads((folder / "smoke.json").read_text())
    assert f.read_text() == (folder / "close-ledger.csv").read_text()
    for r in csv.DictReader(f.read_text().splitlines()):
        sl, e, tp = [Decimal(r[k]) for k in ("SL", "entry", "TP")]
        assert (
            e - sl == tp - e
            and e != sl
            and int(r["entry_close"]) >= int(r["setup_open"]) + 2 * TF
        )


def test_ordered_quote_evidence_price_symmetry_timing_and_pnl():
    folder = ROOT / "research/wickless_m15"
    result = json.loads((folder / "quote-smoke.json").read_text())
    rs = list(csv.DictReader((folder / "quote-ledger.csv").read_text().splitlines()))
    assert len(rs) == result["simulated_fills"] and len(
        {r["close_s"] for r in rs}
    ) == len(rs)
    money = Decimal(0)
    for row in rs:
        entry, sl, tp, x = [Decimal(row[k]) for k in ("entry", "SL", "TP", "exit")]
        assert tp - entry == entry - sl and entry != sl
        assert int(row["setup_open"]) + 1800 <= int(row["close_s"])
        assert 0 <= int(row["quote_ms"]) - int(row["close_s"]) * 1000 <= 8000
        assert int(row["exit_ms"]) >= int(row["quote_ms"])
        usd = (1 if row["side"] == "BUY" else -1) * (x - entry) * 1000
        assert usd == Decimal(row["pnl_usd"])
        money += usd
    assert float(money) == result["net_before_fees_usd"]
