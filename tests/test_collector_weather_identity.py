"""Collector identity regressions; synthetic inputs/stores, never live I/O."""
import unittest

import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from bot.feeds.weather_capture_store import WeatherCaptureStore
from bot.feeds.weather_pro import CITY_COORDS, CITY_NWS, ProWeatherEngine
from bot.strategies.enhanced import EnhancedStrategyEngine
from bot.weather.market_mapping import WeatherMarketCityMapper
from bot.weather.registry import WeatherRegistry


class IdentityClock(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = cls(2026, 10, 21, 18, tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


class CollectorRequestIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = WeatherCaptureStore(storage_root=self.root)
        self.requests = []
        self.calls = []

    def strategy(self, enabled=False):
        config = {'runtime':{'storage_root':str(self.root)}, 'strategy':{
            'enable_news':False, 'enable_social':False, 'enable_ai':False,
            'signal_audit_path':str(self.root / 'audit.jsonl'),
            'weather_source_selection':{'enabled':enabled, 'history_metadata_path':str(self.root / 'absent.json')}}}
        engine = EnhancedStrategyEngine(config['strategy'], capture_store=self.store)
        self.addCleanup(engine.live_feeds.close)
        weather = engine.live_feeds.weather
        for feed in (weather.open_meteo, weather.nws, weather.owm):
            feed.http.close()
            feed.http = httpx.Client(transport=httpx.MockTransport(self.respond))
        weather.owm.available = False
        return engine

    def respond(self, request):
        self.requests.append(request)
        if request.url.path.startswith('/stations/') and not request.url.path.endswith('/observations/latest'):
            station = request.url.path.rsplit('/',1)[-1]
            return httpx.Response(200, json={'geometry':{'type':'Point','coordinates':[-100.25,40.25]},
                                             'properties':{'stationIdentifier':station}})
        if request.url.path.startswith('/points/'):
            return httpx.Response(200, json={'properties':{'gridId':'TST','gridX':1,'gridY':2}})
        if request.url.path == '/gridpoints/TST/1,2/forecast':
            return httpx.Response(200, json={'properties':{'updateTime':'2026-10-21T17:00:00Z','periods':[
                {'name':'Wednesday Night','temperature':56,'isDaytime':False,
                 'startTime':'2026-10-21T18:00:00-07:00','endTime':'2026-10-22T06:00:00-07:00'},
                {'name':'Thursday','temperature':84,'isDaytime':True,
                 'startTime':'2026-10-22T06:00:00-07:00','endTime':'2026-10-22T18:00:00-07:00'},
            ]}})
        if request.url.host == 'api.open-meteo.com':
            return httpx.Response(200, json={'timezone':'America/Los_Angeles',
                'hourly':{'time':['2026-10-22T06:00','2026-10-22T14:00'], 'temperature_2m':[58,82]}})
        return httpx.Response(503, text='Synthetic unavailable non-target provider')

    def fetch(self, *, alias='SAN', family='KXHIGHT', category=None, question=None, metadata=None, enabled=False):
        self.requests.clear()
        strategy = self.strategy(enabled)
        ticker = family + alias + '-26OCT22-T75'
        question = question or 'Will temperature be above 75° on Oct 22, 2026?'
        original = copy.deepcopy(metadata or {})
        market = SimpleNamespace(id=ticker, question=question, yes_price=.4, no_price=.6,
            exchange='kalshi', volume=10000, liquidity=10000,
            closes_at=datetime(2026, 10, 23, tzinfo=timezone.utc),
            category=family + alias if category is None else category, metadata=original)
        weather = strategy.live_feeds.weather
        with patch('bot.feeds.weather_pro.datetime', IdentityClock), \
             patch('bot.feeds.live_data.datetime', IdentityClock), \
             patch('bot.strategies.enhanced.datetime', IdentityClock), \
             patch('bot.strategies.signal_validator.datetime', IdentityClock), \
             patch.object(strategy, '_price_signal', return_value=None), \
             patch.object(strategy, '_volume_signal', return_value=None), \
             patch.object(strategy, '_time_signal', return_value=None), \
             patch.object(weather, 'score_temperature_market_with_context', wraps=weather.score_temperature_market_with_context) as scored, \
             patch.object(weather, 'get_forecast', wraps=weather.get_forecast) as requested:
            _, trace = strategy.analyze_market_with_trace(market)
            signal = trace.raw_signals.get('live')
        self.assertEqual(market.question, question)
        self.assertEqual(market.metadata, metadata or {})
        self.assertTrue(scored.called)
        self.assertTrue(all(call.args[0] == question for call in scored.call_args_list), scored.call_args_list)
        self.assertTrue(requested.called, signal)
        return signal, requested.call_args, strategy

    def assert_retained(self, signal, city, kind):
        self.assertIsNotNone(signal)
        data = signal['data']
        self.assertEqual(data['city'], city)
        self.assertEqual(data['market_target_date'], '2026-10-22')
        retained = json.loads(self.store.read_receipt_verified(data['collection_receipt']))
        self.assertEqual(retained['city'], city)
        self.assertEqual(retained['forecast_measurement_kind'], kind)
        self.assertEqual(retained['market_target_date'], '2026-10-22')
        for source in retained['sources']:
            self.assertEqual(source['source_location_city'], city)
            self.assertEqual(source['forecast_measurement_kind'], kind)
            metadata = source.get('source_metadata') or {}
            if isinstance(metadata.get('forecast_location'), dict):
                self.assertEqual(metadata['forecast_location']['city'], city)
            if metadata.get('capture'):
                self.assertTrue(self.store.read_verified(metadata['capture']))
        self.assertTrue(any(source.get('source_metadata',{}).get('capture') for source in retained['sources']))

    def test_original_strategy_question_requests_correct_city_and_kind_with_retained_proof(self):
        for alias, city in {'SAN':'san diego','SDF':'louisville','DC':'washington',
                            'EWR':'newark','TTN':'trenton','AUS':'austin'}.items():
            for enabled in (False, True):
                with self.subTest(alias=alias, selection=enabled):
                    signal, call, strategy = self.fetch(alias=alias, enabled=enabled)
                    self.assertEqual(call.args[0], city)
                    self.assertEqual(call.kwargs['measurement_kind'], 'high')
                    expected_station = CITY_NWS.get(city, (None, None, None, None))[3]
                    self.assertEqual(call.kwargs['station_resolution']['station_id'], expected_station)
                    if expected_station:
                        self.assertTrue(any(r.url.path == '/stations/' + expected_station for r in self.requests))
                    else:
                        request = next(r for r in self.requests if r.url.host == 'api.open-meteo.com')
                        self.assertEqual((float(request.url.params['latitude']), float(request.url.params['longitude'])),
                                         CITY_COORDS[city])
                    self.assert_retained(signal, city, 'high')
                    if enabled:
                        self.assertFalse(signal['data']['weather_source_selection']['routeable'])

    def test_secondary_maximum_minimum_template_keeps_actual_provider_kind(self):
        metadata = {'rules_secondary': (
            'The official and final value used to determine this market is the '
            'maximum/minimum temperature as reported by the Weather Company.')}
        for family, kind in (('KXHIGHT', 'high'), ('KXLOWT', 'low')):
            for enabled in (False, True):
                with self.subTest(kind=kind, selection=enabled):
                    signal, call, _ = self.fetch(family=family, metadata=metadata, enabled=enabled)
                    self.assertEqual(call.args[0], 'san diego')
                    self.assertEqual(call.kwargs['measurement_kind'], kind)
                    self.assert_retained(signal, 'san diego', kind)

    def test_unresolved_or_conflicting_identity_never_reaches_provider(self):
        strategy = self.strategy()
        cases = (
            ('KXHIGHTSAN-26OCT22-T75', 'KXHIGHAUS', 'Will temperature be above 75°?', {}),
            ('KXHIGHTSAN-26OCT22-T75', '', 'Will Austin high temperature be above 75°?', {}),
            ('KXHIGHTSAN-26OCT22-T75', '', 'Will low temperature be above 75°?', {}),
            ('KXHIGHUNKNOWN-26OCT22-T75', '', 'Will temperature be above 75°?', {}),
            ('QUESTION-ONLY', '', 'Will San Diego temperature be above 75°?', {}),
            ('KXHIGHTSAN-26OCT22-T75', '', 'Will temperature be above 75°?', {'custom_strike':{'City':'San Antonio'}}),
        )
        with patch.object(strategy.live_feeds.weather, 'get_forecast') as forecast:
            for market_id, category, question, metadata in cases:
                with self.subTest(market_id=market_id, question=question, category=category):
                    market = SimpleNamespace(id=market_id, category=category, question=question,
                                             metadata=metadata, yes_price=.4)
                    self.assertIsNone(strategy._live_data_signal(market))
            forecast.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_official_san_rules_preserve_unknown_station_and_weather_company_authority(self):
        metadata = {'custom_strike':{'City':'San Diego'},
                    'rules_primary':'If the maximum temperature recorded at San Diego (CLISAN) for Oct 22, 2026 is above 75° according to The Weather Company.'}
        signal, call, _ = self.fetch(metadata=metadata)
        self.assertEqual(call.args[0], 'san diego')
        self.assertEqual(call.kwargs['measurement_kind'], 'high')
        station = call.kwargs['station_resolution']
        self.assertEqual(station['station_origin'], 'market_explicit')
        self.assertEqual(station['station_mapping'], 'unknown')
        self.assertIsNone(station['station_id'])
        self.assertTrue(signal['collection_only'])
        self.assertNotIn('predicted_prob', signal)
        receipt = json.loads(self.store.read_receipt_verified(signal['data']['collection_receipt']))
        self.assertEqual(receipt['city'], 'san diego')
        self.assertEqual(receipt['forecast_measurement_kind'], 'high')
        # CLISAN is not permission to fabricate an exact KSAN settlement mapping.
        self.assertFalse(any((r.url.path.startswith('/stations/') and not r.url.path.endswith('/observations/latest'))
                             or r.url.path.startswith('/points/') for r in self.requests))
        from bot.feeds.weather_pro import _settlement_authority
        self.assertEqual(_settlement_authority(metadata)['source'], 'the_weather_company')

    def test_direct_provider_wrapper_resolves_full_id_without_aggregator_transport(self):
        strategy = self.strategy()
        weather = strategy.live_feeds.weather
        with patch('bot.feeds.weather_pro.datetime', IdentityClock), \
             patch.object(weather, 'get_forecast', wraps=weather.get_forecast) as requested:
            signal = weather.score_temperature_market_with_context('Will temperature be above 75°?', .4,
                category='KXHIGH', market_id='KXHIGHTSAN-26OCT22-T75',
                market_metadata={'custom_strike':{'City':'San Diego'}})
        self.assertEqual(requested.call_args.args[0], 'san diego')
        self.assertEqual(requested.call_args.kwargs['measurement_kind'], 'high')
        self.assert_retained(signal, 'san diego', 'high')

    def test_market_id_only_and_explicit_low_are_not_lost(self):
        for family, question, kind in (
            ('KXHIGH', 'Will temperature be above 75° on Oct 22, 2026?', 'high'),
            ('KXLOW', 'Will low temperature be above 75° on Oct 22, 2026?', 'low'),
        ):
            with self.subTest(kind=kind):
                signal, call, _ = self.fetch(alias='AUS', category='', family=family, question=question)
                self.assertEqual(call.args[0], 'austin')
                self.assertEqual(call.kwargs['measurement_kind'], kind)
                self.assert_retained(signal, 'austin', kind)
                self.assertEqual(signal['data']['predicted_temp'], 84 if kind == 'high' else 56)



class IngestionIdentityTests(unittest.TestCase):
    def test_listing_request_does_not_overwrite_returned_series_identity(self):
        from bot.exchanges.kalshi import KalshiExchange
        exchange = KalshiExchange('synthetic-not-used', 'synthetic-not-opened')
        raw = {'ticker':'KXHIGHTSAN-26OCT22-T75', 'series_ticker':'KXHIGHTSAN',
               'title':'Will maximum temperature be above 75°?', 'custom_strike':{'City':'San Diego'},
               'yes_ask_dollars':'0.40', 'no_ask_dollars':'0.60'}
        market = exchange._market_from_direct_listing(raw, source='fixture', series_ticker='KXHIGHAUS')
        self.assertIsNotNone(market)
        self.assertEqual(market.metadata['series_ticker'], raw['series_ticker'])
        self.assertIsNone(WeatherMarketCityMapper().resolve(market.question, market.category,
            market_id=market.id, market_metadata=market.metadata))

    def test_official_city_original_ids_and_rules_survive_constructors(self):
        from types import SimpleNamespace
        from bot.exchanges.kalshi import KalshiExchange, _market_from_raw
        raw = {'ticker':'KXHIGHTSAN-26OCT22-T75', 'series_ticker':'KXHIGHTSAN',
               'event_ticker':'KXHIGHTSAN-26OCT22', 'title':'Will the maximum temperature be >75° on Oct 22, 2026?',
               'custom_strike':{'City':'San Diego'}, 'rules_primary':'Maximum temperature at San Diego (CLISAN) according to The Weather Company.',
               'floor_strike':0, 'rules_secondary':'Original secondary rules', 'status':'active',
               'yes_ask_dollars':'0.40', 'no_ask_dollars':'0.60', 'close_time':'2026-10-23T08:00:00Z'}
        exchange = KalshiExchange('synthetic-not-used', 'synthetic-not-opened')
        exchange.client = SimpleNamespace(get_market=lambda **kwargs: SimpleNamespace(market=SimpleNamespace(**raw)))
        for market in (_market_from_raw(raw), exchange._market_from_direct_listing(raw, source='fixture'),
                       exchange.get_market(raw['ticker'])):
            with self.subTest(market=market):
                self.assertIsNotNone(market)
                self.assertEqual(market.id, raw['ticker'])
                self.assertEqual(market.question, raw['title'])
                for field in ('ticker','series_ticker','event_ticker','custom_strike','rules_primary','rules_secondary','floor_strike'):
                    self.assertEqual(market.metadata.get(field), raw[field])
                context = WeatherMarketCityMapper().resolve(market.question, market.category,
                    market_id=market.id, market_metadata=market.metadata)
                self.assertIsNotNone(context)
                self.assertEqual(context.city_id, 'san_diego_ca')


class MeasurementIdentityTests(unittest.TestCase):
    def test_kind_is_explicit_and_conflicts_never_default_to_low(self):
        from bot.weather import market_mapping
        resolve = getattr(market_mapping, 'resolve_weather_measurement_kind', None)
        self.assertTrue(callable(resolve))
        for question, category, metadata, expected in (
            ('Will temperature be above 75°?', 'KXHIGHAUS-26OCT22-T75', {}, 'high'),
            ('Will temperature be above 75°?', 'KXLOWTTN-26OCT22-T75', {}, 'low'),
            ('Will low temperature be above 75°?', 'KXLOWTTN', {}, 'low'),
            ('Will maximum temperature be above 75°?', '', {}, 'high'),
            ('Will minimum temperature be above 75°?', '', {}, 'low'),
            ('Will temperature be above 75°?', '', {'rules_primary':'The maximum temperature recorded at San Diego.'}, 'high'),
            ('Will temperature be above 75°?', '', {}, None),
            ('Will maximum temperature be above 75°?', 'KXLOWSAN', {}, None),
            ('Will low temperature be above 75°?', 'KXHIGHSAN', {}, None),
            ('Will temperature be above 75°?', 'KXHIGHSAN', {'series':'KXLOWSAN'}, None),
            ('Will high or low temperature be above 75°?', '', {}, None),
        ):
            with self.subTest(question=question, category=category, metadata=metadata):
                self.assertEqual(resolve(question, market_id=category, market_metadata=metadata), expected)


class RegistryIdentityTests(unittest.TestCase):
    def setUp(self):
        self.registry = WeatherRegistry.from_file()
        self.mapper = WeatherMarketCityMapper(self.registry)

    def test_every_registry_alias_matches_complete_series_and_contract(self):
        for city in self.registry.as_dict()['cities']:
            for alias in city.get('ticker_aliases', []):
                for family in ('KXHIGH', 'KXHIGHT', 'KXHIGHTEMP', 'KXLOW', 'KXLOWT', 'KXMINTEMP'):
                    for ending in ('', '-26OCT22-T75', '-26OCT22-B70.5'):
                        ticker = family + alias + ending
                        with self.subTest(ticker=ticker):
                            context = self.mapper.resolve('Will the temperature be above 75°?', ticker)
                            self.assertIsNotNone(context)
                            self.assertEqual(context.city_id, city['city_id'])
                            by_id = self.mapper.resolve('', market_id=ticker)
                            self.assertEqual(by_id, context)
                            from bot.weather.market_mapping import resolve_weather_measurement_kind
                            self.assertEqual(resolve_weather_measurement_kind('', market_id=ticker),
                                             'high' if 'HIGH' in family else 'low')

    def test_original_identity_and_official_city_rules_agree(self):
        import inspect
        self.assertIn('market_id', inspect.signature(self.mapper.resolve).parameters)
        metadata = {'series_ticker': 'KXHIGHTSAN', 'custom_strike': {'City': 'San Diego'},
                    'rules_primary': 'If the maximum temperature recorded at San Diego (CLISAN) is above 75 degrees according to The Weather Company.'}
        for question, category, market_id, details in (
            ('Will temperature be above 75°?', '', 'KXHIGHTSAN-26OCT22-T75', metadata),
            ('Will temperature be above 75°?', 'KXHIGH', 'KXHIGHTSAN-26OCT22-T75', {}),
            ('Will temperature be above 75°?', '', '', metadata),
            ('Will temperature be above 75°?', '', '', {'custom_strike': {'City': 'San Diego'}}),
            ('Will temperature be above 75°?', '', '', {'rules_primary': metadata['rules_primary']}),
        ):
            with self.subTest(market_id=market_id, details=details):
                context = self.mapper.resolve(question, category, market_id=market_id, market_metadata=details)
                self.assertIsNotNone(context)
                self.assertEqual(context.city_id, 'san_diego_ca')
                self.assertEqual(self.mapper.resolve_city_id(question, category, market_id=market_id,
                                                            market_metadata=details), context.city_id)

    def test_conflicting_or_ambiguous_city_evidence_is_not_first_match(self):
        import inspect
        self.assertIn('market_id', inspect.signature(self.mapper.resolve).parameters)
        for question, category in (
            ('Will Miami or Austin high temperature be above 75°?', ''),
            ('Will Austin high temperature be above 75°?', 'KXHIGHTSAN'),
        ):
            with self.subTest(question=question):
                self.assertIsNone(self.mapper.resolve(question, category))
        for metadata in (
            {'custom_strike': {'City': 'San Antonio'}},
            {'series_ticker': 'KXHIGHSDF'},
            {'rules_primary': 'The maximum temperature recorded at Austin (CLIAUS).'},
            {'custom_strike': {'City': 'Atlantis'}},
        ):
            with self.subTest(metadata=metadata):
                self.assertIsNone(self.mapper.resolve('Will temperature be above 75°?',
                    market_id='KXHIGHTSAN-26OCT22-T75', market_metadata=metadata))

    def test_unresolved_official_location_does_not_authorize_a_ticker_guess(self):
        for metadata in (
            {'rules_primary':'If the maximum temperature recorded at Atlantis (CLIATL) is above 75 degrees.'},
            {'custom_strike':{'City':'Not San Diego'}},
        ):
            with self.subTest(metadata=metadata):
                self.assertIsNone(self.mapper.resolve('Will temperature be above 75°?',
                    market_id='KXHIGHTSAN-26OCT22-T75', market_metadata=metadata))

    def test_registry_alias_collisions_fail_closed_instead_of_overwriting(self):
        data = self.registry.as_dict()
        data['cities'][0]['ticker_aliases'].append('SAN')
        mapper = WeatherMarketCityMapper(WeatherRegistry(data))
        self.assertIsNone(mapper.resolve('Will temperature be above 75°?', 'KXHIGHSAN'))
        data['cities'][0]['ticker_aliases'].append('TXX')
        data['cities'][1]['ticker_aliases'].append('XX')
        mapper = WeatherMarketCityMapper(WeatherRegistry(data))
        self.assertIsNone(mapper.resolve('Will temperature be above 75°?', 'KXHIGHTXX'))

    def test_legacy_registry_backed_aliases_remain_exact(self):
        for alias, expected in {'LA':'los_angeles_ca', 'SA':'san_antonio_tx', 'SD':'san_diego_ca',
                                'SF':'san_francisco_ca', 'MIAMI':'miami_fl', 'NEWYORK':'new_york_ny',
                                'LOSANGELES':'los_angeles_ca'}.items():
            with self.subTest(alias=alias):
                context = self.mapper.resolve('Will temperature be above 75°?', 'KXHIGH' + alias)
                self.assertIsNotNone(context)
                self.assertEqual(context.city_id, expected)
        for ticker in ('KXHIGHAUSMIA', 'KXHIGHSANITY', 'KXLOWNOTSDF', 'KXMINTEMPUNKNOWN', 'KXHIGHSANAUS',
                       'KXHIGHSAN-INVALID', 'KXHIGH-SAN', 'NOTKXHIGHSAN'):
            with self.subTest(ticker=ticker):
                self.assertIsNone(self.mapper.resolve('Will temperature be above 75°?', ticker))
