"""Review regressions at collection and both immutable direct-history readers."""
import copy
import json
from itertools import product
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_collector_weather_identity as live
import test_weather_identity_reconciliation as history
from test_source_observation_ledger import _seal
from bot.feeds.weather_pro import CITY_COORDS, WeatherSnapshot
from bot.weather.market_mapping import WeatherMarketCityMapper
from bot.weather.source_observation_ledger import _decision_identity, _pending_rows_for_input


class WeatherReviewRegressionTests(unittest.TestCase):
    def fixture(self):
        fixture = live.CollectorRequestIdentityTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def collect(self, question, family):
        fixture = self.fixture()
        strategy = fixture.strategy()
        market = SimpleNamespace(id=family + 'SAN-26OCT22-T75', category=family + 'SAN',
                                 yes_price=.4, metadata={}, question=question)
        with patch('bot.feeds.weather_pro.datetime', live.IdentityClock), \
             patch('bot.feeds.live_data.datetime', live.IdentityClock):
            signal = strategy._live_data_signal(market)
        return signal, fixture

    def test_formatted_unsupported_city_never_reaches_collection_transport(self):
        for family, word in (('KXHIGHT', 'high'), ('KXLOWT', 'low')):
            for marker in ('**', '__', '*', '_'):
                for metric, location in ((f'{marker}{word} temperature{marker}', 'in Sacramento'),
                                         (f'{word} temperature', f'{marker}in Sacramento{marker}')):
                    with self.subTest(family=family, metric=metric, location=location):
                        signal, fixture = self.collect(
                            f'Will the {metric} {location} be above 75° on Oct 22, 2026?', family)
                        self.assertIsNone(signal)
                        self.assertEqual(fixture.requests, [])

    def test_formatted_matching_generic_and_station_assertions_remain_usable(self):
        mapper = WeatherMarketCityMapper()
        for marker in ('**', '__', '*', '_'):
            for location in ('in San Diego', ''):
                question = f'Will the {marker}high temperature{marker} {location} be above 75°?'
                context = mapper.resolve(question, 'KXHIGHTSAN')
                assert context is not None
                self.assertEqual(context.city_id, 'san_diego_ca')
            context = mapper.resolve('Will high temperature be above 75°?', 'KXHIGHTSAN',
                market_metadata={'rules_primary': f'Maximum temperature recorded at {marker}CLISAN{marker}.'})
            assert context is not None
            self.assertEqual(context.city_id, 'san_diego_ca')
        # Identifier underscores are not Markdown decoration.
        context = mapper.resolve('Will high temperature in San_Diego be above 75°?', 'KXHIGHTSAN')
        assert context is not None
        self.assertEqual(context.city_id, 'san_diego_ca')
        for family, word, value in (('KXHIGHT', 'high', 84), ('KXLOWT', 'low', 56)):
            for location in ('in San Diego', ''):
                signal, fixture = self.collect(
                    f'Will the **{word} temperature** {location} be above 75° on Oct 22, 2026?', family)
                assert signal is not None
                self.assertEqual(signal['data']['predicted_temp'], value)
                self.assertTrue(fixture.requests)

    def test_formatted_unsupported_city_rejects_in_both_actual_history_readers(self):
        for marker in ('**', '__', '*', '_'):
            for metric, location in ((f'{marker}high temperature{marker}', 'in Sacramento'),
                                     ('high temperature', f'{marker}in Sacramento{marker}')):
                fixture = history.WeatherIdentityReconciliationTests(methodName='runTest')
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                row = history.bad_labels_record(full_day=True)
                row['market']['question'] = f'Will the {metric} {location} be above 70°?'
                row = _seal(row)
                for strict in (False, True):
                    with self.subTest(marker=marker, metric=metric, strict=strict):
                        result = fixture.history(row, strict=strict)
                        self.assertEqual([item['source_id'] for item in getattr(result,
                            'scorecard_rows' if strict else 'rows')], [])

    def test_nested_emphasis_keeps_city_assertions_at_actual_collection_boundary(self):
        for family, word, value in (('KXHIGHT', 'high', 84), ('KXLOWT', 'low', 56)):
            for city in ('Sacramento', 'San Diego'):
                for assertion in (
                    f'**_{word} temperature_** in {city}',
                    f'_**{word} temperature**_ in {city}',
                    f'**{word} temperature _in {city}_**',
                ):
                    with self.subTest(family=family, assertion=assertion):
                        signal, fixture = self.collect(
                            f'Will the {assertion} be above 75° on Oct 22, 2026?', family)
                        if city == 'Sacramento':
                            self.assertIsNone(signal)
                            self.assertEqual(fixture.requests, [])
                        else:
                            assert signal is not None
                            self.assertEqual(signal['data']['predicted_temp'], value)
                            self.assertTrue(fixture.requests)

    def test_nested_emphasis_rejects_unsupported_city_in_both_actual_history_readers(self):
        for city in ('Sacramento', 'Seattle'):
            for assertion in (
                f'**_high temperature_** in {city}',
                f'_**high temperature**_ in {city}',
                f'**high temperature _in {city}_**',
            ):
                fixture = history.WeatherIdentityReconciliationTests(methodName='runTest')
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                row = history.bad_labels_record(full_day=True)
                row['market']['question'] = f'Will the {assertion} be above 70°?'
                row = _seal(row)
                original = copy.deepcopy(row)
                expected = set() if city == 'Sacramento' else {'nws', 'seattle_local'}
                for strict in (False, True):
                    with self.subTest(assertion=assertion, strict=strict):
                        result = fixture.history(row, strict=strict)
                        self.assertEqual({item['source_id'] for item in getattr(result,
                            'scorecard_rows' if strict else 'rows')}, expected)
                self.assertEqual(row, original)

    def test_emphasis_compositions_preserve_location_and_station_operands(self):
        mapper = WeatherMarketCityMapper()
        for depth in (1, 2, 3):
            for markers in product(('**', '__', '*', '_'), repeat=depth):
                def decorate(value):
                    for marker in markers:
                        value = marker + value + marker
                    return value
                for city in ('Sacramento', 'San_Diego'):
                    for assertion in (
                        f'{decorate("high temperature")} in {city}',
                        f'high temperature {decorate("in " + city)}',
                        decorate(f'high temperature in {city}'),
                    ):
                        text = f'Will the {assertion} be above 75°?'
                        for origin in ('question', 'rules_primary', 'rules_secondary'):
                            with self.subTest(markers=markers, assertion=assertion, origin=origin):
                                context = mapper.resolve(text if origin == 'question' else
                                    'Will high temperature be above 75°?', 'KXHIGHTSAN',
                                    market_metadata={} if origin == 'question' else {origin: text})
                                self.assertEqual(context.city_id if context else None,
                                    None if city == 'Sacramento' else 'san_diego_ca')
                for text in (f'Will the {decorate("high temperature")} be above 75°?',
                             f'Will high temperature at {decorate("CLISAN")} be above 75°?'):
                    with self.subTest(markers=markers, positive=text):
                        context = mapper.resolve(text, 'KXHIGHTSAN')
                        self.assertEqual(context.city_id if context else None, 'san_diego_ca')

    def test_original_date_conflicts_are_rejected_without_changing_history_inputs(self):
        for case in ('baseline', 'matching_event', 'matching_question', 'conflicting_event',
                     'conflicting_question', 'conflicting_metadata_date'):
            fixture = history.WeatherIdentityReconciliationTests(methodName='runTest')
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            row = history.bad_labels_record(full_day=True)
            metadata = row['market']['market_metadata']
            if case in ('matching_event', 'conflicting_event'):
                metadata['event_ticker'] = 'KXHIGHSEA-26AUG03' if case == 'matching_event' else 'KXHIGHSEA-26AUG04'
            if case in ('matching_question', 'conflicting_question'):
                day = 3 if case == 'matching_question' else 4
                row['market']['question'] = f'Will Seattle highest temperature be above 70° on August {day}, 2026?'
            if case == 'conflicting_metadata_date':
                metadata['market_date'] = '2026-08-04'
            row = _seal(row)
            original = copy.deepcopy(row)
            expected = set() if case.startswith('conflicting') else {'nws', 'seattle_local'}
            for strict in (False, True):
                with self.subTest(case=case, strict=strict):
                    result = fixture.history(row, strict=strict)
                    self.assertEqual({item['source_id'] for item in getattr(result,
                        'scorecard_rows' if strict else 'rows')}, expected)
            if case.startswith('conflicting'):
                identity = _decision_identity(row)
                assert identity is not None
                pending = _pending_rows_for_input(row, identity)
                self.assertTrue(pending)
                for observation in pending:
                    self.assertIn('conflicting_original_market_dates', observation['strict_source_proof']['reasons'])
            self.assertEqual(row, original)

    def test_aggregator_binds_returned_snapshot_to_requested_city(self):
        for returned_city in ('san diego', 'miami'):
            with self.subTest(returned_city=returned_city):
                fixture = self.fixture()
                strategy = fixture.strategy()
                weather = strategy.live_feeds.weather
                lat, lon = CITY_COORDS[returned_city]
                snapshot = WeatherSnapshot(city=returned_city, high_temp_f=89, low_temp_f=66,
                    current_temp_f=77, source='open-meteo', fetched_at=live.IdentityClock.now(),
                    forecast_hours_ahead=24, confidence=.8, weather_date='2026-10-22', forecast_date='2026-10-22',
                    forecast_start='2026-10-22T00:00:00-07:00', forecast_end='2026-10-23T00:00:00-07:00',
                    source_details={'forecast_location': {'city': returned_city, 'latitude': lat, 'longitude': lon},
                                    'response_latitude': lat, 'response_longitude': lon})
                market = SimpleNamespace(id='KXHIGHTSAN-26OCT22-T75', category='KXHIGHTSAN', yes_price=.4,
                    metadata={}, question='Will maximum temperature in San Diego be above 75° on Oct 22, 2026?')
                with patch('bot.feeds.weather_pro.datetime', live.IdentityClock), \
                     patch('bot.feeds.live_data.datetime', live.IdentityClock), \
                     patch.object(weather.open_meteo, 'get_forecast', return_value=snapshot), \
                     patch.object(weather.nws, 'get_forecast', return_value=None):
                    signal = strategy._live_data_signal(market)
                assert signal is not None
                receipt = json.loads(fixture.store.read_receipt_verified(signal['data']['collection_receipt']))
                source = next(item for item in receipt['sources'] if item['source_id'] == 'open_meteo')
                admitted = returned_city == 'san diego'
                self.assertEqual(source['scoreable_forecast'], admitted)
                self.assertEqual('open-meteo' in signal['data']['sources'], admitted)
                self.assertEqual(source['forecast_high'], 89)
                self.assertEqual(snapshot.city, returned_city)
