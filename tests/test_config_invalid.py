import pytest

from bot.config.schema import validate_config


def test_invalid_run_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        validate_config({"run": {"mode": "live-ish"}})
