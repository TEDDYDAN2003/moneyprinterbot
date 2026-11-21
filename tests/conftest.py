"""Shared synthetic demo desk. No real terminal, credentials or broker calls."""

import pytest
from hull_mt import MT, ROOT, T

from bot.config.loader import load_yaml
from bot.hull.config import HullConfig, selection
from bot.hull.desk import Desk, resolve


@pytest.fixture
def hull_desk(tmp_path, monkeypatch):
    cfg = load_yaml(ROOT / "config.yaml")
    cfg["run"]["mode"] = "demo"
    # Short fake clocks for legacy guard fixtures only, not production authorization.
    from bot.hull.config import SIGNAL_TF

    for m in (1, 2, 5):
        monkeypatch.setitem(SIGNAL_TF, f"M{m}", m)
    cfg["hull"]["eligible_timeframes"] = ["M1", "M2", "M5"]  # fake desk fixture ONLY
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("bot.hull.desk.time.time", lambda: T + 0.3)

    def go(rate="slow", armed=True, state=None, mt=None):
        mt = mt or MT()
        h = HullConfig.model_validate(cfg["hull"])
        prof = selection(h, ["XAUUSD"], rate=rate)
        syms = resolve(mt, prof)
        logs = []
        desk = Desk(
            mt,
            cfg,
            h,
            syms,
            (1, "t"),
            state or {},
            tmp_path / "state.json",
            lambda event, **kw: logs.append((event, kw)),
            armed,
        )
        return mt, desk, logs

    return go
