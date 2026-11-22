"""Read-only quote export, including exact interval boundaries and same-ms ticks."""

import csv
import gzip
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import numpy as np
import pytest

from bot.hull.quote_export import export

T = int(datetime(2026, 11, 3, 12, tzinfo=UTC).timestamp())
DT = [("time_msc", "i8"), ("bid", "f8"), ("ask", "f8")]


class Feed:
    COPY_TICKS_ALL = 42

    def __init__(self, rows):
        self.data = np.array(rows, dtype=DT)
        self.calls = 0

    def symbol_select(self, *args):
        return True

    def symbol_info(self, *args):
        return NS(
            point=0.001,
            trade_tick_size=0.001,
            trade_contract_size=100,
            currency_profit="USD",
            trade_stops_level=0,
            trade_freeze_level=0,
        )

    def copy_ticks_range(self, name, lo, hi, kind):
        self.calls += 1
        m = self.data["time_msc"]
        return self.data[
            (m >= int(lo.timestamp()) * 1000) & (m <= int(hi.timestamp()) * 1000)
        ]

    def symbol_info_tick(self, name):
        r = self.data[-1]
        return NS(time_msc=int(r["time_msc"]), bid=float(r["bid"]), ask=float(r["ask"]))


def test_exact_broker_export_keeps_same_ms_and_half_open_hour_boundary(tmp_path):
    rows = [
        (T * 1000 + 5, 4000, 4000.24),
        ((T + 3600) * 1000, 3999, 3999.24),
        ((T + 3600) * 1000, 3999.1, 3999.3),
        ((T + 7200) * 1000, 4001, 4001.2),
    ]
    mt = Feed(rows)
    out = tmp_path / "gold.csv.gz"
    r = export(mt, "XAUUSDm", "2026-11-03T12:00:00Z", "2026-11-03T14:00:00Z", out)
    assert mt.calls == 2 and r["ticks"] == 3 and r["broker_symbol"] == "XAUUSDm"
    with gzip.open(out, "rt") as f:
        rows = list(csv.DictReader(f))
    assert [float(x["bid"]) for x in rows] == [4000, 3999, 3999.1]
    assert rows[1]["timestamp"] == rows[2]["timestamp"]
    assert out.with_name(out.name + ".manifest.json").exists()
    with pytest.raises(FileExistsError):
        export(mt, "X", "2026-11-03T12:00Z", "2026-11-03T14:00Z", out)


def test_no_bad_or_empty_export_is_published(tmp_path):
    p = tmp_path / "bad.csv"
    mt = Feed([(T * 1000, 4001.0, 4000.0)])
    with pytest.raises(ValueError, match="Malformed"):
        export(mt, "x", "2026-11-03T12:00Z", "2026-11-03T13:00Z", p)
    assert not p.exists() and not list(tmp_path.glob("*.partial"))
    with pytest.raises(ValueError, match="No historical"):
        export(mt, "x", "2026-10-01", "2026-10-02", p)
