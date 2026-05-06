from datetime import datetime, timezone
from types import SimpleNamespace

from bot.strategies.enhanced import EnhancedStrategyEngine


class FailingNewsFeed:
    all_sources_failed = True

    def get_news_for_market(self, question):
        return []


class SearchFailedButUsableNewsFeed:
    all_sources_failed = True

    def get_news_for_market(self, question):
        return [
            SimpleNamespace(
                relevance=1.0,
                recency_weight=1.0,
                sentiment=0.2,
                published=datetime(2026, 5, 6, 16, 0, tzinfo=timezone.utc),
                source="static-feed",
            )
        ]

    def assess_signal_quality(self, news_items):
        return {"confidence_penalty": 0.0, "warnings": []}


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
    engine.news = FailingNewsFeed()
    engine._price_signal = lambda market, order_book=None: {
        "signal_type": "price",
        "predicted_prob": 0.70,
        "confidence": 0.90,
    }
    engine._live_data_signal = lambda market: None
    engine._volume_signal = lambda market: None
    engine._time_signal = lambda market: None

    assert engine.analyze_market(_generic_market()) is None


def test_search_source_failure_does_not_fail_closed_when_static_news_is_usable():
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
    engine.news = SearchFailedButUsableNewsFeed()
    engine._price_signal = lambda market, order_book=None: {
        "signal_type": "price",
        "predicted_prob": 0.70,
        "confidence": 0.90,
    }
    engine._live_data_signal = lambda market: None
    engine._volume_signal = lambda market: None
    engine._time_signal = lambda market: None

    signal = engine.analyze_market(_generic_market())

    assert signal is not None
    assert signal["direction"] == "BUY_YES"


def test_news_source_failure_default_preserves_degraded_behavior():
    engine = EnhancedStrategyEngine(
        {
            "enable_news": False,
            "enable_social": False,
            "enable_ai": False,
            "min_edge": 0.01,
        }
    )
    engine.enable_news = True
    engine.news = FailingNewsFeed()
    engine._price_signal = lambda market, order_book=None: {
        "signal_type": "price",
        "predicted_prob": 0.70,
        "confidence": 0.90,
    }
    engine._live_data_signal = lambda market: None
    engine._volume_signal = lambda market: None
    engine._time_signal = lambda market: None

    signal = engine.analyze_market(_generic_market())

    assert signal is not None
    assert signal["direction"] == "BUY_YES"


def test_weather_hidden_gem_guard_blocks_cheap_bucket_without_distribution_probability():
    engine = _engine_with_live_weather(
        {
            "signal_type": "weather",
            "predicted_prob": 0.75,
            "confidence": 0.80,
            "question_side": "range",
            "data": {
                "question_side": "range",
                "forecast_high": 84.0,
                "actual_temp_used": 84.0,
                "threshold": 85.0,
                "agreement": 0.90,
            },
        }
    )

    assert engine.analyze_market(_cheap_bucket_market()) is None


def test_weather_hidden_gem_guard_allows_bucket_with_distribution_probability():
    engine = _engine_with_live_weather(
        {
            "signal_type": "weather",
            "predicted_prob": 0.75,
            "confidence": 0.80,
            "question_side": "range",
            "data": {
                "question_side": "range",
                "forecast_high": 84.0,
                "actual_temp_used": 84.0,
                "threshold": 85.0,
                "agreement": 0.90,
                "distribution_probability": 0.24,
            },
        }
    )

    signal = engine.analyze_market(_cheap_bucket_market())

    assert signal is not None
    assert signal["direction"] == "BUY_YES"


def test_weather_hidden_gem_guard_blocks_tail_when_live_weather_rejects_candidate_side():
    engine = _engine_with_live_weather(
        {
            "signal_type": "weather",
            "predicted_prob": 0.10,
            "confidence": 0.86,
            "question_side": "above",
            "data": {
                "question_side": "above",
                "forecast_high": 79.0,
                "actual_temp_used": 79.0,
                "threshold": 85.0,
                "agreement": 0.99,
            },
        }
    )

    assert engine.analyze_market(_cheap_tail_market()) is None


def test_weather_hidden_gem_guard_does_not_block_low_confidence_tail_disagreement():
    engine = _engine_with_live_weather(
        {
            "signal_type": "weather",
            "predicted_prob": 0.10,
            "confidence": 0.55,
            "question_side": "above",
            "data": {
                "question_side": "above",
                "forecast_high": 79.0,
                "actual_temp_used": 79.0,
                "threshold": 85.0,
                "agreement": 0.99,
            },
        }
    )

    signal = engine.analyze_market(_cheap_tail_market())

    assert signal is not None
    assert signal["direction"] == "BUY_YES"


def _engine_with_live_weather(live_signal: dict) -> EnhancedStrategyEngine:
    engine = EnhancedStrategyEngine(
        {
            "enable_news": False,
            "enable_social": False,
            "enable_ai": False,
            "min_edge": 0.01,
            "enable_weather_hidden_gem_safety_guard": True,
        }
    )
    engine._price_signal = lambda market, order_book=None: None
    engine._live_data_signal = lambda market: dict(live_signal)
    engine._volume_signal = lambda market: None
    engine._time_signal = lambda market: None
    return engine


def _generic_market():
    return SimpleNamespace(
        id="GENERIC-1",
        exchange="kalshi",
        question="Will a generic event happen?",
        yes_price=0.40,
        no_price=0.61,
        volume=5000,
        liquidity=5000,
        category="generic",
        closes_at=None,
    )


def _cheap_bucket_market():
    return SimpleNamespace(
        id="KXHIGHMIA-26MAY06-B84.5",
        exchange="kalshi",
        question="Will the high temp in Miami be 84-85° on May 6, 2026?",
        yes_price=0.05,
        no_price=0.96,
        volume=5000,
        liquidity=5000,
        category="KXHIGHMIA",
        closes_at=None,
    )


def _cheap_tail_market():
    return SimpleNamespace(
        id="KXHIGHTATL-26MAY05-T85",
        exchange="kalshi",
        question="Will the maximum temperature be >85° on May 5, 2026?",
        yes_price=0.02,
        no_price=0.99,
        volume=5000,
        liquidity=5000,
        category="KXHIGHTATL",
        closes_at=None,
    )
