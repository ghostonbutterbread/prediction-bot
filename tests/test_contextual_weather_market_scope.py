"""Keep the weather-only observer gate out of unrelated market families."""
import unittest
from unittest.mock import patch

import test_contextual_weather_source_flow as fixtures
from bot.feeds.live_data import is_temperature_question


class ContextualWeatherMarketScopeTests(unittest.TestCase):
    def fresh(self):
        fixture = fixtures.ContextualWeatherSourceFlowTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_college_degrees_preserves_matching_api_price_controls(self):
        fixture = self.fresh()
        market = fixture.market()
        market.id = 'COLLEGE-DEGREES'
        market.category = 'Education'
        market.metadata = {}
        market.question = 'Will the number of college degrees awarded reach a new high in 2026?'
        legacy = fixture.strategy()
        enabled = fixture.strategy({'enabled': True})
        price = {'signal_type': 'price', 'predicted_prob': .9, 'confidence': .9}
        with patch.object(fixture, 'market', return_value=market):
            for traced in (True, False):
                with self.subTest(traced=traced):
                    control = fixture.analyze(legacy, price=price, traced=traced)
                    actual = fixture.analyze(enabled, price=price, traced=traced)
                    if traced:
                        control, _ = control
                        actual, _ = actual
                    self.assertIsNotNone(control)
                    self.assertEqual(control['model_probability'], .9)
                    self.assertEqual(actual, control)
        self.assertEqual(fixture.requests, [])

    def test_numeric_degree_counts_preserve_matching_api_price_controls(self):
        questions = (
            'Will US colleges award more than 2000000 degrees in 2026?',
            'Will US colleges award more than 2,000,000 degrees in 2026?',
            'Will US colleges award more than 85 degrees in 2026?',
            'Will US colleges award more than 3 degrees in 2026?',
            'Will Las Vegas award more than 85 degrees in 2026?',
            'Will colleges in Las Vegas award more than 85 degrees in 2026?',
            'Will colleges in Las Vegas exceed 85 degrees awarded in 2026?',
        )
        price = {'signal_type': 'price', 'predicted_prob': .9, 'confidence': .9}
        for question in questions:
            fixture = self.fresh()
            market = fixture.market()
            market.id, market.category, market.metadata = 'COLLEGE-DEGREES', 'Education', {}
            market.question = question
            legacy = fixture.strategy()
            enabled = fixture.strategy({'enabled': True})
            with patch.object(fixture, 'market', return_value=market), \
                 patch.object(legacy.live_feeds.weather, 'score_temperature_market_with_context', return_value=None) as lw, \
                 patch.object(enabled.live_feeds.weather, 'score_temperature_market_with_context', return_value=None) as ew:
                for traced in (True, False):
                    with self.subTest(question=question, traced=traced):
                        control = fixture.analyze(legacy, price=price, traced=traced)
                        actual = fixture.analyze(enabled, price=price, traced=traced)
                        if traced:
                            control, _ = control
                            actual, _ = actual
                        self.assertIsNotNone(control)
                        self.assertEqual(control['model_probability'], .9)
                        self.assertEqual(actual, control)
                        self.assertFalse(is_temperature_question(question))
                        lw.assert_not_called()
                        ew.assert_not_called()
            self.assertEqual(fixture.requests, [])

    def test_question_only_temperature_semantics_preserve_both_api_observer_gates(self):
        questions = (
            'Will Las Vegas exceed 85 degrees?',
            'Will Las Vegas exceed 85 degree?',
            'Will Las Vegas exceed +85 degrees?',
            'Will Denver be below -5.5 degrees?',
            'Will Las Vegas high be above 85.5 degrees?',
            'Will Seattle low be below -0.5 degrees?',
            'Will the temperature be below -5.5?',
            'Will the TEMP exceed +85?',
            'Will Las Vegas exceed +85 Fahrenheit?',
            'Will Denver fall below -5.5 Celsius?',
            'Will Denver be below -5.5°?',
            'Will Las Vegas high be above +85°F?',
            'Will Denver low be below -5.5°C?',
        )
        price = {'signal_type': 'price', 'predicted_prob': .9, 'confidence': .9}
        for question in questions:
            fixture = self.fresh()
            market = fixture.market()
            market.id, market.category, market.metadata = 'QUESTION-ONLY', '', {}
            market.question = question
            legacy = fixture.strategy()
            enabled = fixture.strategy({'enabled': True})
            with patch.object(fixture, 'market', return_value=market), \
                 patch.object(legacy.live_feeds.weather, 'score_temperature_market_with_context', return_value=None), \
                 patch.object(enabled.live_feeds.weather, 'score_temperature_market_with_context', return_value=None) as weather:
                for traced in (True, False):
                    with self.subTest(question=question, traced=traced):
                        control = fixture.analyze(legacy, price=price, traced=traced)
                        actual = fixture.analyze(enabled, price=price, traced=traced)
                        if traced:
                            control, _ = control
                            actual, trace = actual
                            self.assertEqual(trace.skip_reason_code, 'contextual_weather_observer_only')
                        self.assertIsNotNone(control)
                        self.assertEqual(control['model_probability'], .9)
                        self.assertIsNone(actual)
                        self.assertTrue(is_temperature_question(question))
                # Classification still gates both APIs. A question-only request
                # without daily high/low must not silently query low.
                identified = any(f' {kind} ' in question.lower() for kind in ('high', 'low'))
                self.assertEqual(weather.call_count, 2 if identified else 0)
            self.assertEqual(fixture.requests, [])

    def question_only_api_probe(self, question, category=''):
        """Exercise collection and both consumers, mocking HTTP, never scoring."""
        fixture = self.fresh()
        market = fixture.market()
        market.id, market.category, market.metadata = 'QUESTION-ONLY', category, {}
        market.question = question
        legacy = fixture.strategy()
        enabled = fixture.strategy({'enabled': True})
        price = {'signal_type': 'price', 'predicted_prob': .9, 'confidence': .9}
        results = []
        lw, ew = legacy.live_feeds.weather, enabled.live_feeds.weather
        with patch.object(fixture, 'market', return_value=market), \
             patch.object(lw, 'score_temperature_market_with_context', wraps=lw.score_temperature_market_with_context) as control_weather, \
             patch.object(ew, 'score_temperature_market_with_context', wraps=ew.score_temperature_market_with_context) as enabled_weather:
            for traced in (True, False):
                control = fixture.analyze(legacy, price=price, traced=traced)
                actual = fixture.analyze(enabled, price=price, traced=traced)
                trace = None
                if traced:
                    control, _ = control
                    actual, trace = actual
                results.append((traced, control, actual, trace))
        return results, control_weather.call_count, enabled_weather.call_count, fixture.requests

    def test_complete_degree_noun_phrase_keeps_academic_counts_out_of_weather(self):
        questions = (
            'Will US colleges award more than 2000000 degrees in 2026?',
            'Will US colleges award more than 2,000,000 degrees in 2026?',
            'Will Las Vegas award more than 85 degrees in 2026?',
            'Will colleges in Las Vegas exceed 85 degrees awarded in 2026?',
            'Will Las Vegas reach 85 degrees awarded by local colleges on Oct 19, 2026?',
            'Will Las Vegas exceed 2000000 degrees conferred by its colleges on Oct 19, 2026?',
            'Will Las Vegas reach 85 degrees awarded by local colleges in 2026?',
            'Will Las Vegas exceed 2000000 degrees conferred by its colleges in 2026?',
            'Will Las Vegas reach 85 degrees in engineering on Oct 19, 2026?',
            'Will Las Vegas hit 85 degrees on diplomas in 2026?',
        )
        for category in ('Education', ''):
            for question in questions:
                results, control_calls, enabled_calls, requests = self.question_only_api_probe(question, category)
                for traced, control, actual, _ in results:
                    with self.subTest(question=question, category=category, traced=traced):
                        self.assertIsNotNone(control)
                        self.assertEqual(control['model_probability'], .9)
                        self.assertEqual(actual, control)
                        self.assertFalse(is_temperature_question(question))
                        self.assertEqual((control_calls, enabled_calls), (0, 0))
                        self.assertEqual(requests, [])

    def test_compositional_bare_temperature_forms_collect_and_gate_both_apis(self):
        questions = (
            'Will Las Vegas fall below 60 degrees on Oct 19, 2026?',
            'Will Las Vegas be at least 85 degrees on Oct 19, 2026?',
            'Will the high in Las Vegas be above 85 degrees on Oct 19, 2026?',
            'Will Las Vegas have a high above 85 degrees on Oct 19, 2026?',
            'Will Las Vegas reach 85 degrees on Oct 19, 2026?',
            'Will Las Vegas exceed 85 degrees on Oct 19, 2026?',
            'Will Las Vegas hit +85.5 degrees on Oct 19, 2026?',
            'Will Las Vegas low be below -5.5 degrees on Oct 19, 2026?',
            'Will the minimum in Las Vegas be at most -5.5 degrees on Oct 19, 2026?',
            'Will Las Vegas have a low under -5.5 degrees on Oct 19, 2026?',
            'Will Las Vegas fall below 60 degrees Fahrenheit on Oct 19, 2026?',
        )
        for question in questions:
            results, control_calls, enabled_calls, requests = self.question_only_api_probe(question)
            for traced, _, actual, trace in results:
                with self.subTest(question=question, traced=traced):
                    self.assertTrue(is_temperature_question(question))
                    identified = any(f' {kind} ' in question.lower() for kind in ('high', 'low', 'minimum'))
                    self.assertEqual((control_calls, enabled_calls), (2, 2) if identified else (0, 0))
                    self.assertEqual(bool(requests), identified)
                    self.assertIsNone(actual)
                    if traced:
                        self.assertIn(trace.skip_reason_code,
                                      ('contextual_weather_observer_only', 'weather_collection_only'))
                        self.assertNotIn('live', trace.accepted_signals)
        # Scope is independent of the collection clock, date format and category.
        for question in ('Will Denver fall below -5.5 degrees?',
                         'Will Seattle have a low below -0.5 degrees tomorrow?',
                         'Will the high in Austin be over 90 degrees on 2027-07-04?',
                         'Will Chicago be at most 85.5 degrees in July 2027?'):
            with self.subTest(question=question):
                self.assertTrue(is_temperature_question(question))

    def test_identified_weather_shorthand_without_current_cannot_trade_price(self):
        fixture = self.fresh()
        market = fixture.market()
        market.question = 'Will Las Vegas high be above 85 F on Oct 19, 2026?'
        legacy = fixture.strategy()
        enabled = fixture.strategy({'enabled': True})
        price = {'signal_type': 'price', 'predicted_prob': .9, 'confidence': .9}
        with patch.object(fixture, 'market', return_value=market), \
             patch.object(legacy.live_feeds.weather, 'score_temperature_market_with_context', return_value=None), \
             patch.object(enabled.live_feeds.weather, 'score_temperature_market_with_context', return_value=None) as weather:
            for traced in (True, False):
                with self.subTest(traced=traced):
                    control = fixture.analyze(legacy, price=price, traced=traced)
                    actual = fixture.analyze(enabled, price=price, traced=traced)
                    if traced:
                        control, _ = control
                        actual, trace = actual
                        self.assertEqual(trace.skip_reason_code, 'contextual_weather_observer_only')
                    self.assertIsNotNone(control)
                    self.assertEqual(control['model_probability'], .9)
                    self.assertIsNone(actual)
            self.assertEqual(weather.call_count, 2)
        self.assertFalse(is_temperature_question('Will the number of college degrees awarded reach a new high?'))
        self.assertFalse(is_temperature_question('degrees'))
        self.assertEqual(fixture.requests, [])

    def test_identified_shorthand_real_collector_never_invents_odds(self):
        fixture = self.fresh()
        market = fixture.market()
        market.question = 'Will Las Vegas high be above 85 F on Oct 19, 2026?'
        strategy = fixture.strategy({'enabled': True})
        price = {'signal_type': 'price', 'predicted_prob': .9, 'confidence': .9}
        with patch.object(fixture, 'market', return_value=market):
            actual, trace = fixture.analyze(strategy, price=price)
            self.assertIsNone(actual)
            self.assertIsNone(fixture.analyze(strategy, price=price, traced=False))
        self.assertTrue(fixture.requests)
        forecasts = [value[0] for value in strategy.live_feeds.weather._cache.values()]
        self.assertTrue(any(forecast.sources_used for forecast in forecasts))
        self.assertEqual(trace.skip_reason_code, 'contextual_weather_observer_only')
        self.assertEqual(trace.gate_metadata['weather_source_selection']['blockers'], ['no_current_weather_signal'])
        self.assertNotIn('live', trace.raw_signals)
        self.assertEqual(trace.accepted_signals, {})

    def test_unrelated_below_high_low_matrix_preserves_both_apis(self):
        price = {'signal_type': 'price', 'predicted_prob': .9, 'confidence': .9}
        questions = [
            ('Crypto', 'Will Bitcoin close below $65,000 on Oct 19, 2026?'),
            ('Crypto', 'Will Bitcoin reach a new high?'),
            ('Crypto', 'Will Bitcoin reach a new low?'),
            ('Economics', 'Will unemployment fall below 4%?'),
            ('Economics', 'Will hiring be temporarily below expectations?'),
            ('Economics', 'Will US GDP growth hit a new high?'),
            ('Economics', 'Will inflation reach a new low?'),
            ('Politics', 'Will the incumbent poll below 40%?'),
            ('Politics', 'Will voter turnout reach a new high?'),
            ('Politics', 'Will approval fall to a new low?'),
            ('Economics', 'Will the high-yield bond spread be below 5%?'),
            ('Politics', 'Will highway funding rise above $10 billion?'),
        ]
        for category, question in questions:
            with self.subTest(question=question):
                fixture = self.fresh()
                market = fixture.market()
                market.id, market.category, market.question, market.metadata = 'UNRELATED', category, question, {}
                legacy = fixture.strategy()
                enabled = fixture.strategy({'enabled': True})
                with patch.object(fixture, 'market', return_value=market), \
                     patch.object(legacy.live_feeds.crypto, 'score_range_market', return_value=None), \
                     patch.object(enabled.live_feeds.crypto, 'score_range_market', return_value=None), \
                     patch.object(legacy.live_feeds.forex, 'score_forex_market', return_value=None), \
                     patch.object(enabled.live_feeds.forex, 'score_forex_market', return_value=None), \
                     patch.object(legacy.live_feeds.weather, 'score_temperature_market_with_context') as lw, \
                     patch.object(enabled.live_feeds.weather, 'score_temperature_market_with_context') as ew:
                    control, _ = fixture.analyze(legacy, price=price)
                    actual, _ = fixture.analyze(enabled, price=price)
                    self.assertIsNotNone(control)
                    self.assertEqual(control['model_probability'], .9)
                    self.assertEqual(actual, control)
                    self.assertEqual(fixture.analyze(enabled, price=price, traced=False),
                                     fixture.analyze(legacy, price=price, traced=False))
                    lw.assert_not_called()
                    ew.assert_not_called()
                self.assertEqual(fixture.requests, [])

    def test_explicit_temperature_forms_without_current_stay_observer(self):
        price = {'signal_type': 'price', 'predicted_prob': .9, 'confidence': .9}
        for question in (fixtures.QUESTION, 'Will Las Vegas low temperature be below 60?',
                         'Will the TEMP in Las Vegas exceed 85 Fahrenheit?',
                         'Will Las Vegas exceed 25 Celsius?', 'Will Las Vegas exceed 85 degrees?',
                         'Will Las Vegas exceed 85 degree?', 'Will Las Vegas exceed 85°?',
                         'Will Las Vegas high be above 85°F?', 'Will Las Vegas low be below 15°C?'):
            with self.subTest(question=question):
                fixture = self.fresh()
                market = fixture.market()
                market.question = question
                strategy = fixture.strategy({'enabled': True})
                with patch.object(fixture, 'market', return_value=market), \
                     patch.object(strategy.live_feeds.weather, 'score_temperature_market_with_context', return_value=None) as weather:
                    actual, trace = fixture.analyze(strategy, price=price)
                    self.assertIsNone(actual)
                    self.assertEqual(trace.skip_reason_code, 'contextual_weather_observer_only')
                    self.assertIsNone(fixture.analyze(strategy, price=price, traced=False))
                    # The fixture is a HIGH contract: explicit LOW wording is a
                    # conflict, not permission to fetch LOW data.
                    self.assertEqual(weather.call_count, 0 if ' low ' in question.lower() else 2)
                self.assertEqual(fixture.requests, [])

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
