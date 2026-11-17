import pytest

from bot.data.downloader import fetch_mt5_m1


def test_mt5_download_rejects_invalid_years() -> None:
    with pytest.raises(ValueError):
        fetch_mt5_m1("XAUUSDm", 0)
