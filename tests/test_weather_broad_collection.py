"""Offline broad collection contracts; live schema receipts live in task scratch."""
import unittest
import json
import hashlib
from datetime import datetime, timezone

import httpx
from bot.feeds import weather_pro


def gray_page(city='Las Vegas', timezone_name='America/Los_Angeles', rows=None):
    # Minimal fixture mirrors the actual Fusion.contentCache schema, not narrative HTML.
    rows = rows if rows is not None else [
        {'validTimeLocal': '2026-10-18T07:00:00-0700', 'temperatureMax': 79, 'temperatureMin': 63},
        {'validTimeLocal': '2026-10-19T07:00:00-0700', 'temperatureMax': 80, 'temperatureMin': 62},
    ]
    cache = {'wx-current-conditions-v3': {'{}': {'lastModified': 1792375876036,
        'data': {'imperial': {'location': {'city': city, 'ianaTimeZone': timezone_name,
        'latitude': 36.17, 'longitude': -115.14}, 'dailyForecast': rows,
        'currentObservation': {'temperature': 74}}}}}}
    return '<script id="fusion-metadata">var Fusion={};Fusion.contentCache=' + json.dumps(cache) + ';</script>'


class LocalCollectionTests(unittest.TestCase):
    def test_dated_local_high_retains_exact_payload_and_location_without_station_claim(self):
        self.assertTrue(hasattr(weather_pro, 'LocalForecastFeed'), 'local capture adapter is missing')
        body = gray_page()
        requests = []
        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(200, text=body, headers={'content-type': 'text/html'})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        source = WeatherRegistry.from_file().collection_sources('las vegas')[0]
        feed = weather_pro.LocalForecastFeed(client)
        snapshot = feed.get_forecast(source, target_date='2026-10-19', measurement_kind='high')
        self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
        self.assertEqual(snapshot.source, 'src_local_kvvu_weather')
        self.assertEqual(snapshot.high_temp_f, 80)
        self.assertEqual(snapshot.forecast_date, '2026-10-19')
        self.assertEqual(snapshot.source_details['forecast_location']['basis'], 'city')
        self.assertNotIn('station_id', snapshot.source_details['forecast_location'])
        self.assertEqual(snapshot.source_details['forecast_type'], 'local_daytime_high_predictor')
        self.assertEqual(snapshot.source_details['raw_forecast']['temperatureMin'], 62)
        self.assertEqual(snapshot.source_details['capture']['payload_text'], body)
        self.assertEqual(snapshot.source_details['capture']['payload_sha256'], hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual(snapshot.source_details['capture']['retrieval_url'], source['url'])
        self.assertEqual(len(requests), 1)

    def test_local_low_uses_previous_night_not_following_morning(self):
        source = WeatherRegistry.from_file().collection_sources('las vegas')[0]
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=gray_page())))
        self.addCleanup(client.close)
        snapshot = weather_pro.LocalForecastFeed(client).get_forecast(source, target_date='2026-10-19', measurement_kind='low')
        self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
        self.assertEqual(snapshot.low_temp_f, 63)
        self.assertEqual(snapshot.source_details['raw_forecast']['temperatureMin'], 63)
        self.assertEqual(snapshot.source_details['forecast_type'], 'local_prior_night_low_predictor')
        self.assertEqual(snapshot.source_details['target_mapping']['source_target_date'], '2026-10-19')
        self.assertEqual(snapshot.forecast_start, '2026-10-18T07:00:00-0700')

    def test_local_missing_low_keeps_high_and_original_night_value_without_relabelling(self):
        source = WeatherRegistry.from_file().collection_sources('las vegas')[0]
        rows = [{'validTimeLocal': '2026-10-19T07:00:00-0700', 'temperatureMax': 80, 'temperatureMin': 62}]
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=gray_page(rows=rows))))
        self.addCleanup(client.close)
        feed = weather_pro.LocalForecastFeed(client)
        high = feed.get_forecast(source, target_date='2026-10-19', measurement_kind='high')
        self.assertTrue(high.scoreable_forecast)
        self.assertIsNone(high.low_temp_f)
        self.assertEqual(high.source_details['raw_forecast']['temperatureMin'], 62)
        low = feed.get_forecast(source, target_date='2026-10-19', measurement_kind='low')
        self.assertFalse(low.scoreable_forecast)
        self.assertEqual(low.availability_reason, 'target_date_missing_low_forecast_period')
        self.assertIn('capture', low.source_details)

    def test_unsupported_page_retains_payload_and_precise_prerequisite(self):
        source = WeatherRegistry.from_file().collection_sources('austin')[0]
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text='<p>Current temperature 88. Tomorrow 90.</p>')))
        self.addCleanup(client.close)
        snapshot = weather_pro.LocalForecastFeed(client).get_forecast(source, target_date='2026-10-19', measurement_kind='high')
        self.assertFalse(snapshot.scoreable_forecast)
        self.assertEqual(snapshot.availability_reason, 'source_specific_numeric_adapter_unavailable')
        self.assertEqual(snapshot.source_details['prerequisite'], 'observed_dated_local_forecast_payload_and_parser')
        self.assertIn('capture', snapshot.source_details)
        self.assertEqual(snapshot.source_details['collection_attempts'][0]['http_status'], 200)

    def test_http_failure_retains_response_without_making_forecast(self):
        source = WeatherRegistry.from_file().collection_sources('las vegas')[0]
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404, text='Not found')))
        self.addCleanup(client.close)
        snapshot = weather_pro.LocalForecastFeed(client).get_forecast(source, target_date='2026-10-19', measurement_kind='high')
        self.assertFalse(snapshot.scoreable_forecast)
        self.assertEqual(snapshot.availability_reason, 'source_http_status_404')
        self.assertEqual(snapshot.source_details['capture']['payload_text'], 'Not found')

    def test_local_wrong_city_date_or_timezone_and_nonnumeric_value_are_not_scoreable(self):
        source = WeatherRegistry.from_file().collection_sources('las vegas')[0]
        cases = [
            (gray_page(city='New Orleans'), 'source_forecast_city_mismatch'),
            (gray_page(city=None), 'source_specific_forecast_schema_invalid'),
            (gray_page(rows=[{'validTimeLocal':'2026-10-20T07:00:00-0700','temperatureMax':80}]), 'target_date_not_in_source_forecast'),
            (gray_page(rows=[{'validTimeLocal':'2026-10-19T07:00:00','temperatureMax':80}]), 'source_forecast_period_timezone_ambiguous'),
            (gray_page(timezone_name='America/Chicago'), 'source_forecast_timezone_mismatch'),
            (gray_page(rows=[{'validTimeLocal':'2026-10-19T07:00:00-0700','temperatureMax':True}]), 'source_forecast_temperature_invalid'),
        ]
        for body, reason in cases:
            with self.subTest(reason=reason):
                client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)))
                self.addCleanup(client.close)
                snapshot = weather_pro.LocalForecastFeed(client).get_forecast(source, target_date='2026-10-19', measurement_kind='high')
                self.assertFalse(snapshot.scoreable_forecast)
                self.assertEqual(snapshot.availability_reason, reason)
                self.assertIn('capture', snapshot.source_details)

from bot.weather.registry import WeatherRegistry


class EngineCollectionTests(unittest.TestCase):
    def test_engine_collects_local_after_provider_crash_and_keeps_nonselected_and_unavailable(self):
        engine = weather_pro.ProWeatherEngine()
        self.addCleanup(engine.close)
        engine.open_meteo.get_forecast = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('provider bug'))
        now = datetime.now(timezone.utc)
        engine.nws.get_forecast = lambda *args, **kwargs: weather_pro.WeatherSnapshot(
            city='las vegas', source='nws', high_temp_f=90, low_temp_f=60,
            current_temp_f=75, fetched_at=now, forecast_hours_ahead=2, confidence=.8)
        engine.nws.get_station_observation = lambda *args: None
        engine.nws.http.close()
        engine.nws.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=gray_page())))
        engine.owm.available = False
        forecast = engine.get_forecast('las vegas', target_date='2026-10-19', measurement_kind='high', require_target=True)
        snapshots = {s.source: s for s in forecast.details['source_snapshots']}
        self.assertTrue(snapshots['src_local_kvvu_weather'].scoreable_forecast)
        self.assertEqual(snapshots['src_local_kvvu_weather'].high_temp_f, 80)
        self.assertFalse(snapshots['open-meteo'].scoreable_forecast)
        self.assertEqual(snapshots['open-meteo'].availability_reason, 'source_forecast_unexpected_error')
        self.assertEqual(snapshots['openweathermap'].availability_reason, 'missing_api_key')
        self.assertEqual(snapshots['openweathermap'].source_details['prerequisite'], 'OPENWEATHER_API_KEY')
        self.assertEqual(snapshots['openweathermap'].source_details['collection_attempts'], [])
        details = {s['source_id']: s for s in engine._source_contribution_details(forecast, city='las vegas', forecast_measurement_kind='high')}
        self.assertEqual(details['src_local_kvvu_weather']['forecast_high'], 80)
        self.assertEqual(details['src_local_kvvu_weather']['weight'], 0)
        self.assertIn('capture', details['src_local_kvvu_weather']['source_metadata'])
        self.assertEqual(details['open_meteo']['availability_reason'], 'source_forecast_unexpected_error')
        self.assertEqual(details['open_meteo']['source_location_city'], 'las vegas')
        self.assertEqual(details['open_meteo']['forecast_measurement_kind'], 'high')


    def test_global_forecast_retains_exact_original_payload_even_when_target_unavailable(self):
        body = json.dumps({'timezone':'America/Chicago', 'current':{'temperature_2m':72},
            'hourly':{'time':['2026-10-19T12:00'], 'temperature_2m':[81.5]}})
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)))
        self.addCleanup(client.close)
        feed = weather_pro.OpenMeteoFeed()
        feed.http.close()
        feed.http = client
        snapshot = feed.get_forecast('austin', target_date='2026-10-19', station_resolution={})
        self.assertEqual(snapshot.high_temp_f, 81.5)
        self.assertEqual(snapshot.source_details['capture']['payload_text'], body)
        self.assertEqual(snapshot.source_details['capture']['payload_sha256'], hashlib.sha256(body.encode()).hexdigest())
        unavailable = feed.get_forecast('austin', target_date='2026-10-20', station_resolution={})
        self.assertFalse(unavailable.scoreable_forecast)
        self.assertEqual(unavailable.source_details['capture']['payload_text'], body)
        self.assertEqual(unavailable.source_details['collection_attempts'][0]['http_status'], 200)

    def test_selected_stable_source_changes_numeric_forecast_without_erasing_competitors(self):
        self.assertTrue(hasattr(weather_pro.ProWeatherEngine, 'select_source_forecast'), 'numeric selection seam missing')
        now = datetime.now(timezone.utc)
        snapshots = [weather_pro.WeatherSnapshot(city='las vegas', source=source,
            high_temp_f=value, low_temp_f=None, current_temp_f=None, fetched_at=now,
            forecast_hours_ahead=0, confidence=.7) for source,value in [('nws',90),('src_local_kvvu_weather',80)]]
        forecast = weather_pro.MultiSourceForecast(city='las vegas', high_temp_f=90, low_temp_f=None,
            current_temp_f=None, sources_used=['nws','src_local_kvvu_weather'], confidence=.7, fetched_at=now,
            details={'source_snapshots':snapshots, 'forecast_driver':'nws',
                'individual_highs':{'nws':90,'src_local_kvvu_weather':80}})
        selected = weather_pro.ProWeatherEngine.select_source_forecast(forecast, 'src_local_kvvu_weather', measurement_kind='high')
        self.assertEqual(selected.high_temp_f,80)
        self.assertEqual(selected.details['forecast_driver'],'src_local_kvvu_weather')
        self.assertEqual(forecast.high_temp_f,90)
        self.assertEqual(selected.details['source_snapshots'],snapshots)
        details = {s['source_id']:s for s in weather_pro.ProWeatherEngine._source_contribution_details(selected)}
        self.assertEqual(details['src_local_kvvu_weather']['weight'],1)
        self.assertEqual(details['nws']['weight'],0)
        with self.assertRaises(ValueError):
            weather_pro.ProWeatherEngine.select_source_forecast(forecast, 'missing', measurement_kind='high')


    def test_nws_raw_capture_and_source_clock_remain_distinct_from_fetch_time(self):
        body = json.dumps({'properties': {'updateTime': '2026-10-18T20:00:00Z', 'periods': [
            {'name':'Monday', 'startTime':'2026-10-19T06:00:00-05:00', 'endTime':'2026-10-19T18:00:00-05:00',
             'isDaytime':True, 'temperature':81, 'temperatureUnit':'F'}]}})
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)))
        self.addCleanup(client.close)
        feed = weather_pro.NWSFeed()
        feed.http.close()
        feed.http = client
        feed._resolve_forecast_grid = lambda *args, **kwargs: ('EWX',152,91,None)
        snapshot = feed.get_forecast('austin', target_date='2026-10-19', measurement_kind='high', station_resolution={})
        self.assertEqual(snapshot.source_details['capture']['payload_text'],body)
        self.assertEqual(snapshot.source_details['source_reported_updated_at'],'2026-10-18T20:00:00Z')
        self.assertNotEqual(snapshot.source_details['source_reported_updated_at'],snapshot.fetched_at.isoformat())

    def test_local_only_forecast_survives_missing_current_reading_and_all_global_failures(self):
        engine = weather_pro.ProWeatherEngine()
        self.addCleanup(engine.close)
        engine.open_meteo.get_forecast = lambda *args, **kwargs: None
        engine.nws.get_forecast = lambda *args, **kwargs: None
        engine.nws.get_station_observation = lambda *args: None
        engine.owm.available = False
        engine.nws.http.close()
        engine.nws.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=gray_page())))
        result = engine.get_forecast('las vegas', target_date='2026-10-19', measurement_kind='high')
        self.assertEqual(result.sources_used,['src_local_kvvu_weather'])
        self.assertEqual(result.high_temp_f,80)
        self.assertIsNone(result.current_temp_f)
        self.assertEqual(len(result.details['source_snapshots']),7)


    def test_chosen_local_numeric_forecast_reaches_real_heuristic_probability(self):
        engine = weather_pro.ProWeatherEngine()
        self.addCleanup(engine.close)
        now = datetime.now(timezone.utc)
        snapshots = [weather_pro.WeatherSnapshot(city='las vegas', source=source,
            high_temp_f=value, low_temp_f=None, current_temp_f=None, fetched_at=now,
            forecast_hours_ahead=0, confidence=.7) for source,value in [('nws',90),('src_local_kvvu_weather',80)]]
        forecast = weather_pro.MultiSourceForecast(city='las vegas', high_temp_f=90, low_temp_f=None,
            current_temp_f=None, sources_used=['nws','src_local_kvvu_weather'], confidence=.7, fetched_at=now,
            details={'source_snapshots':snapshots,'forecast_driver':'nws',
                     'individual_highs':{'nws':90,'src_local_kvvu_weather':80}})
        engine.get_forecast = lambda *args, **kwargs: forecast
        question = 'Will Las Vegas high temperature be above 85° on Oct 19, 2026?'
        control = engine._score_temperature_market(question,.5,market_date='2026-10-19')
        selected = engine._score_temperature_market(question,.5,market_date='2026-10-19', selected_source_id='src_local_kvvu_weather')
        self.assertEqual(selected['data']['predicted_temp'],80)
        self.assertNotEqual(control['predicted_prob'], selected['predicted_prob'])
        self.assertEqual(selected['probability_evidence']['source_id'],'src_local_kvvu_weather')
        self.assertEqual(selected['probability_evidence']['calibration_status'],'not_established')
        self.assertEqual(len(selected['data']['source_details']),2)


    def test_every_catalog_candidate_has_truthful_status_without_network_or_credentials(self):
        engine = weather_pro.ProWeatherEngine()
        self.addCleanup(engine.close)
        result = engine.get_forecast('las vegas', target_date=None, require_target=True, measurement_kind='high')
        statuses = {s.source: s for s in result.details['source_snapshots']}
        self.assertEqual(set(statuses), {'nws','open-meteo','openweathermap','metar_asos','visual_crossing','tomorrow_io','src_local_kvvu_weather'})
        self.assertEqual(statuses['visual_crossing'].availability_reason,'source_adapter_unavailable')
        self.assertEqual(statuses['tomorrow_io'].availability_reason,'source_adapter_unavailable')
        self.assertEqual(statuses['metar_asos'].availability_reason,'current_observation_not_future_forecast')
        self.assertEqual(statuses['src_local_kvvu_weather'].availability_reason,'missing_market_target_date')
        self.assertEqual(statuses['visual_crossing'].source_details['collection_attempts'],[])
        self.assertEqual(statuses['visual_crossing'].source_details['prerequisite'],'implemented_adapter_and_authorized_provider_access')


    def test_implausible_original_forecast_is_retained_as_unusable_not_silently_dropped(self):
        now = datetime.now(timezone.utc)
        bad = weather_pro.WeatherSnapshot(city='austin', source='nws', high_temp_f=999,
            low_temp_f=60,current_temp_f=70,fetched_at=now,forecast_hours_ahead=2,confidence=.8)
        engine = weather_pro.ProWeatherEngine()
        self.addCleanup(engine.close)
        engine.nws.get_forecast = lambda *args, **kwargs: bad
        engine.nws.get_station_observation = lambda *args: None
        engine.open_meteo.get_forecast = lambda *args, **kwargs: None
        result = engine.get_forecast('not a city',target_date='2026-10-19',measurement_kind='high')
        self.assertFalse(result.sources_used)
        details = {s['source_id']:s for s in engine._source_contribution_details(result)}
        self.assertEqual(details['nws']['forecast_high'],999)
        self.assertFalse(details['nws']['scoreable_forecast'])
        self.assertEqual(details['nws']['availability_reason'],'implausible_or_missing_requested_forecast')


    def test_global_access_failure_retains_response_and_exact_http_status(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503, text='Service temporarily unavailable')))
        self.addCleanup(client.close)
        feed = weather_pro.OpenMeteoFeed()
        feed.http.close()
        feed.http = client
        snapshot = feed.get_forecast('austin',target_date='2026-10-19',station_resolution={})
        self.assertFalse(snapshot.scoreable_forecast)
        self.assertEqual(snapshot.availability_reason,'source_forecast_request_failed')
        self.assertEqual(snapshot.source_details['capture']['http_status'],503)
        self.assertEqual(snapshot.source_details['capture']['payload_text'],'Service temporarily unavailable')
        self.assertEqual(snapshot.source_details['adapter_error_type'],'HTTPStatusError')


class CollectionRegistryTests(unittest.TestCase):
    def test_every_registered_city_can_query_global_coordinate_providers_without_invented_station(self):
        registry = WeatherRegistry.from_file()
        missing = [c['city'] for c in registry.as_dict()['cities'] if c['city'].lower() not in weather_pro.CITY_COORDS]
        self.assertEqual(missing, [])
        for city in ('washington','newark','louisville','trenton'):
            self.assertIsNone(weather_pro._configured_forecast_station(city)['station_id'])

    def test_registry_retains_every_local_candidate_without_nws_alias_votes(self):
        registry = WeatherRegistry.from_file()
        data = registry.as_dict()
        self.assertEqual(len(data['cities']), 24)
        self.assertEqual(len(data['sources']), 62)
        self.assertEqual(len({s['source_id'] for s in data['sources']}), 62)
        candidates = registry.collection_sources('Las Vegas')
        self.assertEqual([s['source_id'] for s in candidates], ['src_local_kvvu_weather'])
        self.assertEqual(registry.collection_sources('las_vegas_nv'), candidates)
        self.assertEqual(registry.collection_sources('vegas'), candidates)
        self.assertEqual(candidates[0]['role'], 'forecast')
        self.assertEqual(candidates[0]['trust_score'], 0)
        self.assertEqual(candidates[0]['collection_adapter'], 'gray_fusion_daily_v1')
        self.assertEqual(registry.collection_sources('not a city'), [])
