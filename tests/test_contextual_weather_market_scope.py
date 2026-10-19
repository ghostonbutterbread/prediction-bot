"""Keep the weather-only observer gate out of unrelated market families."""
import unittest
from unittest.mock import patch

import test_contextual_weather_source_flow as fixtures
from bot.feeds.live_data import is_temperature_question


class ContextualWeatherMarketScopeTests(unittest.TestCase):
    def test_below_crypto_question_keeps_price_signal_in_both_strategy_paths(self):
        fixture = fixtures.ContextualWeatherSourceFlowTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        market = fixture.market()
        market.id = 'BTC-26OCT19-BELOW65000'
        market.category = 'Crypto'
        market.question = 'Will Bitcoin close below $65,000 on Oct 19, 2026?'
        market.metadata = {}
        price = {'signal_type': 'price', 'predicted_prob': .9, 'confidence': .9}
        legacy = fixture.strategy()
        enabled = fixture.strategy({'enabled': True,
            'history_metadata_path': str(fixture.root / 'missing-history.json')})
        with patch.object(fixture, 'market', return_value=market), \
             patch.object(legacy, '_live_data_signal', return_value=None), \
             patch.object(enabled, '_live_data_signal', return_value=None):
            control, _ = fixture.analyze(legacy, price=price)
            plain_control = fixture.analyze(legacy, price=price, traced=False)
            result, trace = fixture.analyze(enabled, price=price)
            plain = fixture.analyze(enabled, price=price, traced=False)
        self.assertIsNotNone(control)
        self.assertEqual(control['model_probability'], .9)
        self.assertIsNotNone(result, trace.skip_reason_code)
        self.assertEqual(result, control)
        self.assertIsNotNone(plain)
        self.assertEqual(plain, plain_control)
        self.assertEqual(fixture.requests, [])

    def test_shared_temperature_predicate_requires_temperature_terms_or_units(self):
        for question in (
            'Will Bitcoin close below $65,000?',
            'Will Bitcoin reach a new high?',
            'Will Bitcoin reach a new low?',
            'Will hiring be temporarily below expectations?',
        ):
            with self.subTest(question=question):
                self.assertFalse(is_temperature_question(question))
        for question in (
            fixtures.QUESTION,
            'Will the low temperature in Las Vegas be below 60?',
            'Will Las Vegas exceed 85 degrees?',
            'Will Las Vegas exceed 85°?',
            'Will the TEMP exceed 85 Fahrenheit?',
            'Will the temperature exceed 25 Celsius?',
        ):
            with self.subTest(question=question):
                self.assertTrue(is_temperature_question(question))

    def test_crypto_below_high_and_low_reach_crypto_not_weather_feed(self):
        fixture = fixtures.ContextualWeatherSourceFlowTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        strategy = fixture.strategy({'enabled': True})
        aggregator = strategy.live_feeds
        expected = {'signal_type': 'crypto', 'predicted_prob': .9, 'confidence': .9}
        with patch.object(aggregator.crypto, 'score_range_market', return_value=expected) as crypto, \
             patch.object(aggregator.weather, 'score_temperature_market_with_context') as weather:
            for question in (
                'Will Bitcoin close below $65,000?',
                'Will Bitcoin reach a new high?',
                'Will Bitcoin reach a new low?',
            ):
                with self.subTest(question=question):
                    self.assertEqual(aggregator.get_signal(question, .4, 'Crypto'), expected)
            self.assertEqual(crypto.call_count, 3)
            weather.assert_not_called()
        self.assertEqual(fixture.requests, [])
