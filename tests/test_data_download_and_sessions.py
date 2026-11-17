from datetime import time

import pandas as pd
import pytest

from bot.data.downloader import load_ohlcv_csv
from bot.time.sessions import session_flags


def test_csv_loader_normalizes_timestamp_and_columns(tmp_path) -> None:
    path = tmp_path / "bars.csv"
    pd.DataFrame(
        {
            "timestamp": ["2026-01-01T00:00:00Z"],
            "Open": [1],
            "High": [2],
            "Low": [0.5],
            "Close": [1.5],
        }
    ).to_csv(path, index=False)
    result = load_ohlcv_csv(path)
    assert result.index.tz is not None
    assert list(result.columns) == ["open", "high", "low", "close"]


def test_session_flags_require_timezone() -> None:
    index = pd.date_range("2026-01-01", periods=1, freq="min")
    with pytest.raises(ValueError):
        session_flags(index)


def test_session_flags_are_dst_aware() -> None:
    index = pd.DatetimeIndex(["2026-01-05T08:30:00Z", "2026-07-06T07:30:00Z"])
    flags = session_flags(index)
    assert flags["london_open"].tolist() == [True, True]
