from types import SimpleNamespace
from datetime import datetime, timezone

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


def test_weather_directional_mismatch_guard_is_opt_in_by_default():
    engine = _engine_with_weather_signal(enable_guard=False)

    signal, trace = engine.analyze_market_with_trace(_cheap_tail_market())

    assert signal is not None
    assert signal["direction"] == "BUY_YES"
    assert trace.skip_reason_code is None


def test_weather_directional_mismatch_guard_stable_off_preserves_trade_even_when_config_true():
    engine = _engine_with_weather_signal(enable_guard=True)

    signal, trace = engine.analyze_market_with_trace(_cheap_tail_market())

    assert signal is not None
    assert signal["direction"] == "BUY_YES"
    assert trace.skip_reason_code is None
    gate = trace.gate_metadata["weather_directional_mismatch_guard"]
    assert gate["would_reject"] is True
    assert gate["enforced"] is False
    assert gate["policy"]["version"] == "stable"


def test_weather_directional_mismatch_guard_shadow_records_but_preserves_trade():
    engine = _engine_with_weather_signal(enable_guard=True, policy_mode="shadow")

    signal, trace = engine.analyze_market_with_trace(_cheap_tail_market())

    assert signal is not None
    assert signal["direction"] == "BUY_YES"
    assert trace.skip_reason_code is None
    assert "weather_directional_mismatch_guard_shadow" in trace.warnings
    gate = trace.gate_metadata["weather_directional_mismatch_guard"]
    assert gate["shadow"] is True
    assert gate["enforced"] is False


def test_weather_directional_mismatch_guard_blocks_cheap_yes_when_beta_enforce():
    engine = _engine_with_weather_signal(enable_guard=True, policy_mode="enforce")

    signal, trace = engine.analyze_market_with_trace(_cheap_tail_market())

    assert signal is None
    assert trace.skip_reason_code == "weather_directional_mismatch_guard"
    assert trace.gate_metadata["weather_directional_mismatch_guard"]["enforced"] is True
    assert trace.ensemble_signal["direction"] == "BUY_YES"
    assert trace.accepted_signals["live"]["predicted_prob"] == 0.05


def test_weather_directional_mismatch_guard_is_symmetric_for_buy_no():
    signal = {
        "signal_type": "weather",
        "predicted_prob": 0.91,
        "confidence": 0.80,
        "question_side": "above",
        "data": {"forecast_high": 92.0, "threshold": 85.0},
    }

    assert EnhancedStrategyEngine._weather_live_signal_rejects_direction(signal, "BUY_NO") is True
    assert EnhancedStrategyEngine._weather_live_signal_rejects_direction(signal, "BUY_YES") is False


def test_news_source_failure_can_fail_closed_after_lookup():
    engine = EnhancedStrategyEngine(
        {
            "enable_news": False,
            "enable_social": False,
            "enable_ai": False,
            "min_edge": 0.01,
            "fail_closed_on_news_source_failure": True,
        }
    )
    engine.enable_news = True
    engine.news = _FailingNewsFeed()
    engine._price_signal = lambda market, order_book=None: {
        "signal_type": "price",
        "predicted_prob": 0.70,
        "confidence": 0.90,
    }
    engine._live_data_signal = lambda market: None
    engine._volume_signal = lambda market: None
    engine._time_signal = lambda market: None

    signal, trace = engine.analyze_market_with_trace(_generic_market())

    assert signal is None
    assert trace.skip_reason_code == "news_sources_failed_fail_closed"
    assert trace.raw_signals["price"]["predicted_prob"] == 0.70


def test_news_source_failure_default_keeps_existing_degraded_behavior():
    engine = EnhancedStrategyEngine(
        {
            "enable_news": False,
            "enable_social": False,
            "enable_ai": False,
            "min_edge": 0.01,
        }
    )
    engine.enable_news = True
    engine.news = _FailingNewsFeed()
    engine._price_signal = lambda market, order_book=None: {
        "signal_type": "price",
        "predicted_prob": 0.70,
        "confidence": 0.90,
    }
    engine._live_data_signal = lambda market: None
    engine._volume_signal = lambda market: None
    engine._time_signal = lambda market: None

    signal, trace = engine.analyze_market_with_trace(_generic_market())

    assert signal is not None
    assert signal["direction"] == "BUY_YES"
    assert trace.skip_reason_code is None


class _FailingNewsFeed:
    all_sources_failed = True

    def get_news_for_market(self, question):
        return []

    def assess_signal_quality(self, items):
        return {"confidence_penalty": 0.0, "warnings": []}


def _engine_with_weather_signal(*, enable_guard: bool, policy_mode: str = "off") -> EnhancedStrategyEngine:
    policy = {"version": "stable", "beta_mode": "off", "features": {}}
    if policy_mode in {"shadow", "enforce"}:
        policy = {
            "version": "beta",
            "beta_mode": policy_mode,
            "features": {"hidden_gem_lane_gates": True},
        }
    engine = EnhancedStrategyEngine(
        {
            "enable_news": False,
            "enable_social": False,
            "enable_ai": False,
            "min_edge": 0.01,
            "enable_weather_directional_mismatch_guard": enable_guard,
            "strategy_policy_normalized": policy,
        }
    )
    live_signal = {
        "signal_type": "weather",
        "predicted_prob": 0.05,
        "confidence": 0.80,
        "source_timestamp": datetime.now(timezone.utc).isoformat(),
        "question_side": "above",
        "data": {
            "forecast_high": 79.0,
            "actual_temp_used": 79.0,
            "threshold": 85.0,
            "agreement": 0.80,
        },
    }
    engine._price_signal = lambda market, order_book=None: None
    engine._live_data_signal = lambda market: dict(live_signal)
    engine._volume_signal = lambda market: None
    engine._time_signal = lambda market: None
    return engine


def _cheap_tail_market():
    return SimpleNamespace(
        id="KXHIGHATL-26MAY05-T85",
        exchange="kalshi",
        question="Will Atlanta high temperature be above 85 degrees?",
        yes_price=0.02,
        no_price=0.98,
        volume=5000,
        liquidity=5000,
        category="KXHIGHATL",
        closes_at=None,
    )


def _generic_market():
    return SimpleNamespace(
        id="GENERIC-1",
        exchange="kalshi",
        question="Will a generic event happen?",
        yes_price=0.40,
        no_price=0.60,
        volume=5000,
        liquidity=5000,
        category="news",
        closes_at=None,
    )
