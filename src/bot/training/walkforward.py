from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Fold:
    train: pd.Index
    test: pd.Index


def purged_walk_forward(
    index: pd.DatetimeIndex,
    train_days: int,
    test_days: int,
    embargo_minutes: int,
) -> Iterator[Fold]:
    if index.tz is None or not index.is_monotonic_increasing:
        raise ValueError("Walk-forward index must be sorted and timezone-aware")
    if min(train_days, test_days) < 1 or embargo_minutes < 0:
        raise ValueError("Window sizes must be positive and embargo cannot be negative")
    start = index[0]
    end = index[-1]
    while start + pd.Timedelta(days=train_days + test_days) <= end:
        train_end = start + pd.Timedelta(days=train_days)
        test_start = train_end + pd.Timedelta(minutes=embargo_minutes)
        test_end = test_start + pd.Timedelta(days=test_days)
        train_index = index[(index >= start) & (index < train_end)]
        test_index = index[(index >= test_start) & (index < test_end)]
        if len(train_index) and len(test_index):
            yield Fold(train_index, test_index)
        start = start + pd.Timedelta(days=test_days)
