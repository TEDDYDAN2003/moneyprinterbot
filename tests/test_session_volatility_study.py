"""Public quote benchmark: no network, broker, MT5 or private trade data."""

import csv
import hashlib
import io
import json
import sys
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research/session_volatility"))
import study


@pytest.mark.parametrize(
    ("stamp", "expect"),
    [
        ("2026-10-01T00:00Z", ["Asia-Pacific", "Tokyo open"]),
        ("2026-10-01T01:00Z", ["Asia-Pacific"]),  # end exclusive
        (
            "2026-10-01T07:00Z",
            ["London", "Frankfurt open"],
        ),  # also Tokyo and London active
        ("2026-10-01T13:30Z", ["London + New York", "New York open"]),
        ("2026-10-26T08:00Z", ["London", "Frankfurt open"]),  # UK/DE back, US still DST
        ("2026-10-26T13:30Z", ["London + New York", "New York open"]),
        ("2026-11-18T14:30Z", ["London + New York", "New York open"]),
        ("2026-11-18T17:00Z", ["New York"]),
        ("2026-11-21T05:00Z", ["Off-session"]),  # Saturday
        ("2026-11-22T22:00Z", ["Asia-Pacific"]),  # Sydney Monday
    ],
)
def test_clock_windows(stamp, expect):
    assert (
        study.clock_windows(int(datetime.fromisoformat(stamp).timestamp()) // 60)
        == expect
    )


def write_ticks(path, quotes):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Exness", "Symbol", "Timestamp", "Bid", "Ask"])
    for minute, second, bid, ask in quotes:
        w.writerow(
            [
                "exness",
                "JP225m",
                f"2026-10-01 00:{minute:02}:{second:02}.000Z",
                bid,
                ask,
            ]
        )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.stem + ".csv", buf.getvalue())


def test_prices_not_pnl_chunk_edges_and_empty_minutes(tmp_path):
    p = tmp_path / "Exness_JP225m_2026_10.zip"
    # Crossed quote rejected. Two ticks at precisely the same time stay in order.
    # An intraminute jump contributes to range, but the quoted-spread sample is
    # exactly the *first valid* quote in each minute, not a tick-weighted average.
    write_ticks(
        p,
        [
            (0, 0, 110, 90),
            (0, 1, 99, 101),
            (0, 1, 108, 112),
            (1, 0, 99, 101),
            (2, 0, 99, 101),
            (3, 0, 99, 101),
            (4, 0, 99, 101),
            (5, 0, 99, 101),
            (9, 59, 99, 101),
        ],
    )
    b, a = study.quote_bars(p, "JP225m", "2026_10", chunksize=2)
    assert a == {
        "ticks": 9,
        "invalid_quotes": 1,
        "complete_bars": 1,
        "partial_nonempty_bars": 1,
    }
    assert b.iloc[0].to_dict() == {"range": 1000, "spr": 200}
    assert b.equals(study.quote_bars(p, "JP225m", "2026_10", chunksize=99)[0])
    agg = study.summarise(study.tagged_bars(b))
    assert set(agg) == {"Asia-Pacific", "Tokyo open"} and agg["Tokyo open"]["n"] == 1
    assert agg["Asia-Pacific"]["range"] == 1000 and agg["Asia-Pacific"]["spread"] == 200
    with pytest.raises(ValueError, match="Unexpected feed"):
        study.quote_bars(p, "US30m", "2026_10")


def test_pin_cannot_substitute_another_file(tmp_path):
    m, s = "2026_10", "JP225m"
    name = f"Exness_{s}_{m}.zip"
    p = tmp_path / name
    p.write_bytes(b"bad")
    pin = {
        "name": name,
        "url": f"https://ticks.ex2archive.com/ticks/{s}/2026/10/{name}",
        "sha256": hashlib.sha256(b"expected").hexdigest(),
    }
    with pytest.raises(ValueError, match="hash differs"):
        study.pinned_archive(tmp_path, s, m, pin, False)
    with pytest.raises(ValueError, match="manifest entry"):
        study.pinned_archive(
            tmp_path, s, m, {**pin, "url": "https://example.invalid/"}, True
        )


def test_configuration_archive_and_published_windows_agree():
    evidence = json.loads((ROOT / "HullJournal/ui/market_windows.json").read_text())
    profiles = yaml.safe_load((ROOT / "config.yaml").read_text())["hull"]["symbols"]
    assert {k for m in evidence["markets"] for k in m["key"].split(" / ")} == set(
        profiles
    ) - {"1HZ100V"}
    assert {m["symbol"] for m in evidence["markets"]} == {
        k for k, v in study.MARKETS.items() if v[0]
    }
    assert (
        len(
            [
                m
                for m in evidence["markets"]
                if "DE40" in m["aliases"] or "DE30" in m["aliases"]
            ]
        )
        == 1
    )
    pins = json.loads((study.HERE / "archives.json").read_text())
    assert len(pins) == 26
    with (study.HERE / "windows.csv").open() as f:
        wins = list(csv.DictReader(f))
    with (study.HERE / "windows_by_utc_date.csv").open() as f:
        days = list(csv.DictReader(f))
    counts = Counter()
    dates = Counter()
    for r in days:
        counts[r["broker_symbol"], r["window"]] += int(r["m5_bars"])
        dates[r["broker_symbol"], r["window"]] += 1
    for r in wins:
        assert counts[r["broker_symbol"], r["window"]] == int(r["m5_bars"]) and dates[
            r["broker_symbol"], r["window"]
        ] == int(r["utc_dates"])
    for m in evidence["markets"]:
        data = m["stats"]
        sym = m["symbol"]
        keys = study.MARKETS[sym][0]
        for k in keys:
            assert profiles[k].get("broker_symbol", sym) == sym
        for r in (x for x in wins if x["broker_symbol"] == sym):
            assert data[r["window"]] == {
                "n": int(r["m5_bars"]),
                "days": int(r["utc_dates"]),
                "range": float(r["median_range_bp"]),
                "spread": float(r["median_spread_bp"]),
            }
        months = [
            x
            for x in pins
            if x["name"] in [f"Exness_{sym}_{mo}.zip" for mo in study.MONTHS]
        ]
        assert len(months) == 2 and all(len(x["sha256"]) == 64 for x in months)
        # Cash-opening hours are nested slices of broad windows, never additional bars.
        assert sum(data[k]["n"] for k in study.BUCKETS if k in data) == sum(
            x["complete_bars"] for x in months
        )
