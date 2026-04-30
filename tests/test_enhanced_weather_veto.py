from bot.strategies.enhanced import EnhancedStrategyEngine


def test_station_weather_veto_blocks_buy_no_when_station_strong_yes():
    signal = {
        "signal_type": "weather",
        "predicted_prob": 0.85,
        "confidence": 0.98,
        "data": {"source_quality": "settlement_station_hourly", "station_id": "KNYC"},
    }

    assert EnhancedStrategyEngine._weather_live_signal_vetoes_direction(signal, "BUY_NO") is True
    assert EnhancedStrategyEngine._weather_live_signal_vetoes_direction(signal, "BUY_YES") is False


def test_station_weather_veto_blocks_buy_yes_when_station_strong_no():
    signal = {
        "signal_type": "weather",
        "predicted_prob": 0.10,
        "confidence": 0.98,
        "data": {"source_quality": "settlement_station_hourly", "station_id": "KNYC"},
    }

    assert EnhancedStrategyEngine._weather_live_signal_vetoes_direction(signal, "BUY_YES") is True
    assert EnhancedStrategyEngine._weather_live_signal_vetoes_direction(signal, "BUY_NO") is False


def test_weather_veto_ignores_low_confidence_or_non_station_sources():
    low_confidence = {
        "signal_type": "weather",
        "predicted_prob": 0.90,
        "confidence": 0.60,
        "data": {"source_quality": "settlement_station_hourly", "station_id": "KNYC"},
    }
    grid_source = {
        "signal_type": "weather",
        "predicted_prob": 0.90,
        "confidence": 0.98,
        "data": {"source_quality": "grid_fallback"},
    }

    assert EnhancedStrategyEngine._weather_live_signal_vetoes_direction(low_confidence, "BUY_NO") is False
    assert EnhancedStrategyEngine._weather_live_signal_vetoes_direction(grid_source, "BUY_NO") is False


def test_official_daily_veto_blocks_any_opposite_direction_even_when_moderate():
    signal = {
        "signal_type": "weather",
        "predicted_prob": 0.35,
        "confidence": 0.60,
        "data": {"source_quality": "settlement_station_official_daily", "station_id": "KNYC"},
    }

    assert EnhancedStrategyEngine._weather_live_signal_vetoes_direction(signal, "BUY_YES") is True
    assert EnhancedStrategyEngine._weather_live_signal_vetoes_direction(signal, "BUY_NO") is False
