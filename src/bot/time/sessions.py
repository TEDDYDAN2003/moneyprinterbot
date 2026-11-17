from __future__ import annotations

from datetime import time

import pandas as pd


def session_flags(index: pd.DatetimeIndex) -> pd.DataFrame:
    if index.tz is None:
        raise ValueError("Session calculations require timezone-aware timestamps")
    utc = index.tz_convert("UTC")
    london = utc.tz_convert("Europe/London")
    new_york = utc.tz_convert("America/New_York")
    return pd.DataFrame(
        {
            "asia": _between(utc, time(0), time(7)),
            "london": _between(london, time(8), time(16)),
            "new_york": _between(new_york, time(8), time(17)),
            "london_open": _between(london, time(7), time(10)),
            "ny_open": _between(new_york, time(7), time(10)),
        },
        index=index,
    )


def _between(index: pd.DatetimeIndex, start: time, end: time) -> pd.Series:
    values = index.time
    return pd.Series([(start <= value < end) for value in values], index=index, dtype="boolean")
