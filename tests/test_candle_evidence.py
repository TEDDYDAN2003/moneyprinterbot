"""Offline reconciliation; no historical price download needed."""

import runpy
from pathlib import Path


def test_all_frozen_window_ledgers_reconcile(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(root)
    evidence = root / "research/candle_retest"
    verify = runpy.run_path(str(evidence / "verify.py"))["verify"]
    assert verify(evidence / "evidence") == 104
