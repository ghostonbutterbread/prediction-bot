"""Source-boundary controls: real adapters/strategy and both immutable history readers.

All transport is mocked; captures/indexes are confined to disposable fixtures.
"""
import copy
import json
from pathlib import Path
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import test_collector_weather_identity as live
import test_weather_identity_reconciliation as history
from test_source_observation_ledger import _seal
from bot.exchanges.kalshi import KalshiExchange, _market_from_raw
from bot.feeds.weather_pro import CITY_COORDS, _forecast_station_selection, _settlement_authority
from bot.weather.date_matcher import derive_market_date, validate_weather_date_match
from bot.weather.market_mapping import WeatherMarketCityMapper, resolve_weather_measurement_kind
from bot.weather.station_mapping import resolve_weather_station


class SourceBoundaryTests(unittest.TestCase):
    def fixture(self):
        fixture = live.CollectorRequestIdentityTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def run_live(self, *, metadata=None, word='maximum', family='KXHIGHT',
                 response_city=None, geometry_city=None):
        fixture = self.fixture()
        original = fixture.respond
        def respond(request):
            if geometry_city and request.url.path.startswith('/stations/') and not request.url.path.endswith('/observations/latest'):
                fixture.requests.append(request)
                lat, lon = CITY_COORDS[geometry_city]
                return httpx.Response(200, json={'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
                    'properties': {'stationIdentifier': request.url.path.rsplit('/', 1)[-1]}})
            if response_city and request.url.host == 'api.open-meteo.com':
                fixture.requests.append(request)
                lat, lon = CITY_COORDS[response_city] if response_city != 'nearby' else (32.74, -117.19)
                return httpx.Response(200, json={'latitude': lat, 'longitude': lon,
                    'timezone': 'America/Los_Angeles', 'hourly': {
                        'time': ['2026-10-22T06:00', '2026-10-22T14:00'], 'temperature_2m': [58, 82]}})
            return original(request)
        fixture.respond = respond
        strategy = fixture.strategy()
        market = SimpleNamespace(id=family + 'SAN-26OCT22-T75', category=family + 'SAN', yes_price=.4,
            question=f'Will the {word} temperature in San Diego be above 75° on Oct 22, 2026?', metadata=metadata or {})
        with patch('bot.feeds.weather_pro.datetime', live.IdentityClock), patch('bot.feeds.live_data.datetime', live.IdentityClock):
            signal = strategy._live_data_signal(market)
        receipt = json.loads(fixture.store.read_receipt_verified(signal['data']['collection_receipt'])) if signal else None
        if receipt:
            for source in receipt['sources']:
                capture = source.get('source_metadata', {}).get('capture')
                if capture:
                    self.assertTrue(fixture.store.read_verified(capture))
        return signal, receipt, fixture

    def test_verified_cli_bindings_have_city_owners_without_changing_settlement_provider(self):
        for city, cli, station, city_id in (
            ('san diego', 'SAN', 'KSAN', 'san_diego_ca'),
            ('washington', 'DCA', 'KDCA', 'washington_dc'),
            ('louisville', 'SDF', 'KSDF', 'louisville_ky'),
            ('newark', 'EWR', 'KEWR', 'newark_nj'),
            ('trenton', 'TTN', 'KTTN', 'trenton_nj'),
        ):
            with self.subTest(cli=cli):
                metadata = {'rules_primary': f'Maximum temperature recorded at {city} (CLI{cli}) according to The Weather Company.'}
                selected = _forecast_station_selection(city, metadata)
                self.assertEqual(selected['station_id'], station)
                self.assertNotIn('availability_reason', selected)
                self.assertEqual(resolve_weather_station({'station_cli': 'CLI' + cli}).city_id, city_id)
                self.assertEqual(_settlement_authority(metadata)['source'], 'the_weather_company')
        self.assertIsNone(_forecast_station_selection('san diego', {'station_cli': 'CLIZZZ'})['station_id'])

    def test_retained_catalog_high_low_rules_reach_actual_providers_for_all_five_bindings(self):
        fixture_data = json.loads((Path(__file__).parent / 'fixtures/weather_source_guard_contracts.json').read_text())
        class CatalogClock(live.IdentityClock):
            @classmethod
            def now(cls, tz=None):
                return super().now(tz).replace(day=22)
        expected = {'San Diego': 'KSAN', 'Washington DC': 'KDCA', 'Louisville': 'KSDF',
                    'Newark': 'KEWR', 'Trenton': 'KTTN'}
        seen = set()
        for contract in fixture_data['markets']:
            with self.subTest(ticker=contract['raw']['ticker']):
                fixture = self.fixture()
                original = fixture.respond
                def respond(request):
                    if request.url.path.startswith('/stations/') and not request.url.path.endswith('/observations/latest'):
                        fixture.requests.append(request)
                        return httpx.Response(200, json=fixture_data['stations'][request.url.path.rsplit('/', 1)[-1]])
                    response = original(request)
                    return httpx.Response(response.status_code, text=response.text.replace('2026-10-22', '2026-10-23')
                                          .replace('2026-10-21', '2026-10-22'))
                fixture.respond = respond
                strategy = fixture.strategy()
                market = _market_from_raw(copy.deepcopy(contract['raw']), category=contract['requested_series'])
                self.assertIsNotNone(market)
                original_metadata = copy.deepcopy(market.metadata)
                with patch('bot.feeds.weather_pro.datetime', CatalogClock), patch('bot.feeds.live_data.datetime', CatalogClock), \
                     patch.object(socket.socket, 'connect', side_effect=AssertionError('No real network in catalog controls')):
                    signal = strategy._live_data_signal(market)
                city = contract['raw']['custom_strike']['City']
                kind = contract['kind']
                seen.add((city, kind))
                self.assertIsNotNone(signal)
                self.assertEqual(signal['data']['predicted_temp'], 84 if kind == 'high' else 56)
                self.assertEqual(signal['data']['market_target_date'], '2026-10-23')
                self.assertEqual(signal['data']['settlement_source'], 'the_weather_company')
                self.assertEqual(market.metadata, original_metadata)
                receipt = json.loads(fixture.store.read_receipt_verified(signal['data']['collection_receipt']))
                self.assertEqual(receipt['forecast_measurement_kind'], kind)
                self.assertTrue(any(r.url.path == '/stations/' + expected[city] for r in fixture.requests))
                for source in receipt['sources']:
                    metadata = source.get('source_metadata', {})
                    if metadata.get('capture'):
                        self.assertTrue(fixture.store.read_verified(metadata['capture']))
                    if source['source_id'] in ('nws', 'open_meteo'):
                        self.assertTrue(source['scoreable_forecast'])
                        location = metadata['forecast_location']
                        self.assertEqual(location['station_id'], expected[city])
                        self.assertEqual([location['longitude'], location['latitude']],
                            fixture_data['stations'][expected[city]]['geometry']['coordinates'])
        self.assertEqual(seen, {(city, kind) for city in expected for kind in ('high', 'low')})

    def test_explicit_other_city_station_never_queries_foreign_geometry(self):
        for metadata in ({'station_id': 'KMIA'}, {'rules_primary': 'Maximum temperature in San Diego (KMIA).'}):
            with self.subTest(metadata=metadata):
                signal, receipt, fixture = self.run_live(metadata=metadata)
                self.assertTrue(signal['collection_only'])
                self.assertEqual(signal['data']['sources'], [])
                self.assertFalse(any('/stations/KMIA' in str(r.url) or '/points/' in r.url.path for r in fixture.requests))
                self.assertTrue(any(s['availability_reason'] == 'station_city_conflicts_with_market' for s in receipt['sources']))

    def test_matching_station_id_cannot_hide_other_city_geometry(self):
        signal, receipt, fixture = self.run_live(metadata={'station_id': 'KSAN'}, geometry_city='miami')
        self.assertTrue(signal['collection_only'])
        self.assertFalse(any('/points/' in r.url.path for r in fixture.requests))

    def test_response_geography_negative_and_nearby_positive_survive_capture(self):
        for city, admitted in (('miami', False), ('san diego', True), ('nearby', True)):
            with self.subTest(city=city):
                signal, receipt, _ = self.run_live(response_city=city)
                source = next(s for s in receipt['sources'] if s['source_id'] == 'open_meteo')
                self.assertEqual(source['scoreable_forecast'], admitted)
                self.assertEqual('open-meteo' in signal['data']['sources'], admitted)
                self.assertEqual(source['forecast_high'], 82)
                self.assertIn('response_latitude', source['source_metadata'])

    def test_direct_open_meteo_adapter_rejects_foreign_response_not_only_aggregator(self):
        fixture = self.fixture()
        strategy = fixture.strategy()
        feed = strategy.live_feeds.weather.open_meteo
        original = fixture.respond
        def respond(request):
            response = original(request)
            if request.url.host == 'api.open-meteo.com':
                data = response.json()
                data.update(latitude=CITY_COORDS['miami'][0], longitude=CITY_COORDS['miami'][1])
                return httpx.Response(200, json=data)
            return response
        feed.http.close()
        feed.http = httpx.Client(transport=httpx.MockTransport(respond))
        with patch('bot.feeds.weather_pro.datetime', live.IdentityClock):
            snapshot = feed.get_forecast('san diego', target_date='2026-10-22')
        self.assertFalse(snapshot.scoreable_forecast)
        self.assertEqual(snapshot.high_temp_f, 82)
        self.assertTrue(fixture.store.read_verified(snapshot.source_details['capture']))

    def test_highest_lowest_are_identity_not_threshold_direction(self):
        for family, word, kind in (('KXHIGHT', 'highest', 'high'), ('KXLOWT', 'lowest', 'low'),
                                   ('KXHIGHT', 'lowest', None), ('KXLOWT', 'highest', None)):
            with self.subTest(family=family, word=word):
                self.assertEqual(resolve_weather_measurement_kind(f'{word} temperature', market_id=family + 'SAN'), kind)
                signal, _, fixture = self.run_live(word=word, family=family)
                if kind is None:
                    self.assertIsNone(signal)
                    self.assertEqual(fixture.requests, [])
                else:
                    self.assertEqual(signal['data']['predicted_temp'], 84 if kind == 'high' else 56)
        self.assertEqual(resolve_weather_measurement_kind('highest temperature'), 'high')
        self.assertEqual(resolve_weather_measurement_kind('lowest temperature'), 'low')

    def test_event_only_date_is_forwarded_and_conflicting_dates_never_fetch(self):
        fixture = self.fixture()
        weather = fixture.strategy().live_feeds.weather
        with patch.object(weather, '_score_temperature_market', return_value={'ok': True}) as scored:
            result = weather.score_temperature_market_with_context('Will maximum temperature in San Diego be above 75°?', .4,
                category='KXHIGHTSAN', market_metadata={'event_ticker': 'KXHIGHTSAN-26OCT22'})
            self.assertEqual(result, {'ok': True})
            self.assertEqual(scored.call_args.kwargs['market_date'], '2026-10-22')
        for metadata, question in (
            ({'event_ticker': 'KXHIGHTSAN-26OCT23'}, 'Will maximum temperature in San Diego be above 75°?'),
            ({'market_date': '2026-10-23'}, 'Will maximum temperature in San Diego be above 75°?'),
            ({}, 'Will maximum temperature in San Diego be above 75° on Oct 23, 2026?'),
        ):
            with self.subTest(metadata=metadata, question=question), patch.object(weather, 'get_forecast') as fetch:
                self.assertIsNone(weather.score_temperature_market_with_context(question, .4,
                    market_id='KXHIGHTSAN-26OCT22-T75', market_metadata=metadata))
                fetch.assert_not_called()

    def test_contradictory_dates_cannot_validate_first_matching_assertion(self):
        for market in (
            {'market_date': '2026-10-22', 'event_date': '2026-10-23'},
            {'market_id': 'KXHIGHTSAN-26OCT22-T75', 'event_ticker': 'KXHIGHTSAN-26OCT23'},
            {'market_id': 'KXHIGHTSAN-26OCT22-T75', 'question': 'Temperature on October 23, 2026?'},
            {'market_date': '2026-10-22', 'metadata': {'market_date': '2026-10-23'}},
            {'market_id': 'KXHIGHTSAN-26OCT22-T75', 'question': 'Temperature on October 22, 2026 or October 23, 2026?'},
        ):
            with self.subTest(market=market):
                self.assertIsNone(derive_market_date(market).value)
                self.assertFalse(validate_weather_date_match(market, weather_date='2026-10-22').ok)
        self.assertEqual(derive_market_date({'metadata': {'event_ticker': 'KXHIGHTSAN-26OCT22'}}).isoformat, '2026-10-22')

    def test_unsupported_question_location_is_not_absent_evidence(self):
        fixture = self.fixture()
        strategy = fixture.strategy()
        for question in ('Will the high temperature in Sacramento be above 75°?',
                         'Will Sacramento high temperature be above 75°?'):
            market = SimpleNamespace(id='KXHIGHTSAN-26OCT22-T75', category='KXHIGHTSAN',
                question=question, metadata={}, yes_price=.4)
            with self.subTest(question=question), patch.object(strategy.live_feeds.weather, 'get_forecast') as fetch:
                self.assertIsNone(strategy._live_data_signal(market))
                fetch.assert_not_called()
        self.assertIsNotNone(WeatherMarketCityMapper().resolve('Will high temperature be above 75°?', 'KXHIGHTSAN'))
        self.assertIsNotNone(WeatherMarketCityMapper().resolve('Will the **high temp in NYC** be <51°?', 'KXHIGHNY'))

    def test_conditional_original_identity_fields_survive_all_exchange_constructors(self):
        raw = {'ticker': 'KXHIGHTSAN-26OCT22-T75', 'series_ticker': 'KXHIGHTSAN',
               'title': 'Will maximum temperature be above 75°?', 'yes_ask_dollars': '0.40', 'no_ask_dollars': '0.60',
               'city': 'Miami', 'city_id': 'miami_fl', 'market_date': '2026-10-23', 'market_kind': 'low',
               'event_date': '2026-10-23', 'target_date': '2026-10-23', 'weather_date': '2026-10-23'}
        exchange = KalshiExchange('synthetic-not-used', 'synthetic-not-opened')
        exchange.client = SimpleNamespace(get_market=lambda **kwargs: SimpleNamespace(market=SimpleNamespace(**raw)))
        for market in (_market_from_raw(raw), exchange._market_from_direct_listing(raw, source='fixture'), exchange.get_market(raw['ticker'])):
            for key in ('city', 'city_id', 'market_date', 'market_kind', 'event_date', 'target_date', 'weather_date'):
                with self.subTest(key=key):
                    self.assertEqual(market.metadata.get(key), raw[key])
            self.assertIsNone(WeatherMarketCityMapper().resolve(market.question, market.category,
                market_id=market.id, market_metadata=market.metadata))


class HistoryBoundaryTests(unittest.TestCase):
    def history_case(self, metadata, *, admitted, question=None):
        fixture = history.WeatherIdentityReconciliationTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        row = history.bad_labels_record(full_day=True)
        if question:
            row['market']['question'] = question
        row['source_inputs']['source_context']['data']['weather_source_snapshot']['sources'][0]['source_metadata'] = metadata
        row = _seal(row)
        original = copy.deepcopy(row)
        for strict in (False, True):
            result = fixture.history(row, strict=strict)
            rows = result.scorecard_rows if strict else result.rows
            self.assertEqual({r['source_id'] for r in rows}, admitted)
        self.assertEqual(row, original)

    def test_duplicate_url_coordinates_preserve_negative_evidence_in_actual_readers(self):
        lat, lon = CITY_COORDS['seattle']
        mia_lat, mia_lon = CITY_COORDS['miami']
        url = f'https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}'
        for extra, admitted in ((f'&latitude={mia_lat}&longitude={mia_lon}', {'seattle_local'}),
                                (f'&latitude={lat}&longitude={lon}', {'nws', 'seattle_local'})):
            for parameters in ({}, {'latitude': lat, 'longitude': lon}):
                with self.subTest(extra=extra, parameters=parameters):
                    self.history_case({'capture': {'retrieval_url': url + extra, 'request_parameters': parameters}}, admitted=admitted)

    def test_all_provider_state_assertions_and_station_owners_reach_both_readers(self):
        for provider in ({'city': 'Seattle', 'state': 'TX'},
                         {'city': 'Seattle', 'state': 'WA', 'adminDistrictCode': 'TX'},
                         {'city': 'Seattle', 'state': 'WA', 'selected_period_location': {'city': 'Seattle', 'state': 'TX'}}):
            with self.subTest(provider=provider):
                self.history_case({'source_reported_location': provider}, admitted={'seattle_local'})
        self.history_case({'forecast_location': {'city': 'Seattle', 'station_id': 'KMIA', 'basis': 'station'}}, admitted={'seattle_local'})
        self.history_case({'source_reported_location': {'city': 'Seattle', 'state': 'WA'}}, admitted={'nws', 'seattle_local'})
        self.history_case({'source_reported_location': {'city': 'Seattle', 'latitude': 47.61, 'longitude': -122.33}}, admitted={'nws', 'seattle_local'})

    def test_highest_lowest_conflicts_reach_actual_history_readers(self):
        self.history_case({}, question='Will Seattle lowest temperature be above 70°?', admitted=set())
        self.history_case({}, question='Will Seattle highest temperature be above 70°?', admitted={'nws', 'seattle_local'})


if __name__ == '__main__':
    unittest.main()
