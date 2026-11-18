from bot.config.schema import validate_config


def test_default_config_contract() -> None:
    config = validate_config({"run": {}, "data": {}})
    assert config.run.mode == "auto"
    assert config.data.history_years == 3
    assert config.data.timeframes == ["M1", "M3", "M5", "M10", "M15"]
