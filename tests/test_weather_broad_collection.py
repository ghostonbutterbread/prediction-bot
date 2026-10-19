"""Offline broad collection contracts; live schema receipts live in task scratch."""
import unittest
import json
import hashlib
import os
import tempfile
from unittest.mock import patch
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


class _CaptureTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {'PREDICTION_BOT_COLLECTOR_ROOT': temporary.name})
        environment.start()
        self.addCleanup(environment.stop)


def capture_text(capture):
    from bot.feeds.weather_capture_store import WeatherCaptureStore
    return WeatherCaptureStore(storage_root=weather_pro.collector_root()).read_verified(capture).decode('utf-8')


class LocalCollectionTests(_CaptureTestCase):
    def test_city_geography_does_not_require_centroid_equality_or_hide_explicit_state_conflicts(self):
        from bot.feeds.weather_local import local_geography_reason
        self.assertIsNone(local_geography_reason({'latitude':36.169, 'longitude':-115.141, 'adminDistrictCode':'NV'},
            city='Las Vegas', city_id='las_vegas_nv'))
        self.assertEqual(local_geography_reason({'latitude':36.169, 'longitude':-115.141, 'adminDistrictCode':'NY'},
            city='Las Vegas', city_id='las_vegas_nv'), 'source_forecast_state_mismatch')
        for latitude, longitude in [(True, -115), (91, 0), (36, None), ('NaN', -115)]:
            with self.subTest(latitude=latitude, longitude=longitude):
                self.assertEqual(local_geography_reason({'latitude':latitude, 'longitude':longitude},
                    city='Las Vegas', city_id='las_vegas_nv'), 'source_forecast_coordinates_invalid')

    def test_future_article_update_is_not_hidden_by_capture_based_source_clock(self):
        from tests.test_weather_local_other import abc_chicago_page
        source = WeatherRegistry.from_file().collection_sources('chicago')[0]
        body = abc_chicago_page(modified='2026-10-19T05:00:00Z')
        now = datetime(2026, 10, 19, 3, tzinfo=timezone.utc)
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body))) as client:
            with patch.object(weather_pro, 'datetime', wraps=datetime) as clock:
                clock.now.return_value = now
                snapshot = weather_pro.LocalForecastFeed(client).get_forecast(source, target_date='2026-10-19', measurement_kind='high')
        self.assertFalse(snapshot.scoreable_forecast)
        self.assertEqual(snapshot.high_temp_f, 66)
        self.assertEqual(snapshot.availability_reason, 'source_reported_clock_after_capture')
        self.assertEqual(snapshot.source_details['source_updated_at'], '2026-10-19T05:00:00+00:00')
        self.assertEqual(snapshot.source_details['source_clock_basis'], 'capture_time_provider_issue_time_unavailable')

    def test_observed_limitations_are_exported_only_after_city_page_capture(self):
        from bot.feeds.weather_local_other import OTHER_LOCAL_UNAVAILABLE_REASONS
        registry = WeatherRegistry.from_file()
        sources = {s['source_id']: s for c in registry.as_dict()['cities'] for s in registry.collection_sources(c['city_id'])}
        for source_id, reason in OTHER_LOCAL_UNAVAILABLE_REASONS.items():
            requested = []
            def handler(request):
                requested.append(str(request.url))
                return httpx.Response(200, text='<p>Captured page without usable dated location-bound forecast</p>')
            with self.subTest(source_id=source_id), httpx.Client(transport=httpx.MockTransport(handler)) as client:
                snapshot = weather_pro.LocalForecastFeed(client).get_forecast(sources[source_id], target_date='2026-10-19', measurement_kind='high')
                self.assertEqual(len(requested), 1)
                self.assertFalse(snapshot.scoreable_forecast)
                self.assertEqual(snapshot.availability_reason, reason)
                self.assertIn('capture', snapshot.source_details)
                self.assertEqual(snapshot.source_details['collection_attempts'][0]['http_status'], 200)

    def test_other_source_dispatch_uses_verified_url_and_preserves_original_registry_url(self):
        from tests.test_weather_local_other import sinclair_page, scripps_page, abc_chicago_page
        from bot.feeds.weather_local_other import OTHER_LOCAL_URLS
        registry = WeatherRegistry.from_file()
        cases = [('oklahoma city', sinclair_page(), 69, None),
                 ('san diego', scripps_page(daily_only=True), 81, 'source_calendar_date_label'),
                 ('chicago', abc_chicago_page(), 66, 'source_modified_clock_and_weekday_label'),
                 ('louisville', gray_page(city='Louisville', timezone_name='America/New_York', rows=[
                     {'validTimeLocal':'2026-10-19T07:00:00-0400', 'temperatureMax':70, 'temperatureMin':43}]), 70, None)]
        for city, body, high, period_basis in cases:
            source = registry.collection_sources(city)[0]
            requested = []
            def handler(request):
                requested.append(str(request.url))
                return httpx.Response(200, text=body)
            with self.subTest(city=city), httpx.Client(transport=httpx.MockTransport(handler)) as client:
                snapshot = weather_pro.LocalForecastFeed(client).get_forecast(source, target_date='2026-10-19', measurement_kind='high')
                self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
                self.assertEqual(snapshot.high_temp_f, high)
                url = OTHER_LOCAL_URLS.get(source['source_id'], source['url'])
                self.assertEqual(requested, [url])
                self.assertEqual(snapshot.source_details['registry_url'], source['url'])
                self.assertEqual(snapshot.source_details['capture']['retrieval_url'], url)
                self.assertEqual(snapshot.source_details['collection_attempts'][0]['url'], url)
                self.assertEqual(snapshot.source_details.get('period_basis'), period_basis)
                if city == 'chicago':
                    self.assertEqual(snapshot.source_details['source_updated_at'], '2026-10-19T02:48:33+00:00')
                self.assertEqual(capture_text(snapshot.source_details['capture']), body)

    def test_tegna_dispatch_preserves_partial_low_and_date_clock_semantics(self):
        from tests.test_weather_local_tegna import CAPTURES
        from bot.feeds.weather_local_tegna import parse_tegna_daily
        registry = WeatherRegistry.from_file()
        for capture in CAPTURES:
            source = next(s for s in registry.collection_sources(capture['city']) if s['source_id'] == capture['source_id'])
            source['collection_adapter'] = 'tegna_public_html_v1'
            now = datetime.fromisoformat(capture['fetched_at'])
            with self.subTest(source_id=source['source_id']), httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=capture['html']))) as client:
                for kind in ('high', 'low'):
                    expected = parse_tegna_daily(capture['html'], city=source['city'], target_date='2026-10-19',
                        measurement_kind=kind, timezone_name=source['timezone'], fetched_at=now)
                    with patch.object(weather_pro, 'datetime', wraps=datetime) as clock:
                        clock.now.return_value = now
                        snapshot = weather_pro.LocalForecastFeed(client).get_forecast(source, target_date='2026-10-19', measurement_kind=kind)
                    self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
                    self.assertEqual(snapshot.high_temp_f if kind == 'high' else snapshot.low_temp_f, capture['expected_' + kind])
                    for key in ('period_semantics', 'full_day_measurement_equivalent', 'date_mapping_basis', 'source_clock_basis', 'source_as_of'):
                        self.assertEqual(snapshot.source_details[key], expected[key], key)
                    self.assertEqual(snapshot.source_details['parser_payload_sha256'], capture['fixture_sha256'])
                    self.assertFalse(snapshot.source_details['full_day_measurement_equivalent'])
                    self.assertEqual(capture_text(snapshot.source_details['capture']), capture['html'])

    def test_fox_dispatch_collects_all_ten_dated_predictors_through_real_feed(self):
        from tests.test_weather_local_fox import CAPTURED
        registry = WeatherRegistry.from_file()
        for source_id, original_sha, city, zone, high, low, following_low, body in CAPTURED:
            source = next(s for s in registry.collection_sources(city) if s['source_id'] == source_id)
            source['collection_adapter'] = 'fox_nuxt_daily_v1'
            with self.subTest(source_id=source_id), httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body))) as client:
                feed = weather_pro.LocalForecastFeed(client)
                for kind, value, selected in [('high', high, 'day'), ('low', low, 'night')]:
                    snapshot = feed.get_forecast(source, target_date='2026-10-19', measurement_kind=kind)
                    self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
                    self.assertEqual(snapshot.high_temp_f if kind == 'high' else snapshot.low_temp_f, value)
                    self.assertEqual(snapshot.source_details['selected_period'], selected)
                    self.assertIsNone(snapshot.source_details['source_updated_at'])
                    self.assertEqual(capture_text(snapshot.source_details['capture']), body)

    def test_coordinate_identity_contradiction_is_not_verified_city_geography(self):
        source = WeatherRegistry.from_file().collection_sources('las vegas')[0]
        body = gray_page().replace('36.17', '40.7128').replace('-115.14', '-74.006')
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body))) as client:
            snapshot = weather_pro.LocalForecastFeed(client).get_forecast(source, target_date='2026-10-19', measurement_kind='high')
        self.assertFalse(snapshot.scoreable_forecast, 'explicit coordinates bound to another configured city contradict the source label')
        self.assertEqual(snapshot.availability_reason, 'source_forecast_coordinate_city_mismatch')
        self.assertEqual(snapshot.high_temp_f, 80)
        self.assertEqual(snapshot.source_details['source_reported_location']['latitude'], 40.7128)
        self.assertNotIn('latitude', snapshot.source_details['forecast_location'])
        self.assertIn('capture', snapshot.source_details)

    def test_future_cache_clock_retains_values_and_capture_but_is_ineligible(self):
        from unittest.mock import patch
        now = datetime(2026, 10, 19, 3, tzinfo=timezone.utc)
        future_ms = int(datetime(2026, 10, 21, 3, tzinfo=timezone.utc).timestamp() * 1000)
        body = gray_page().replace('1792375876036', str(future_ms))
        source = WeatherRegistry.from_file().collection_sources('las vegas')[0]
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body))) as client:
            with patch.object(weather_pro, 'datetime', wraps=datetime) as clock:
                clock.now.return_value = now
                snapshot = weather_pro.LocalForecastFeed(client).get_forecast(source, target_date='2026-10-19', measurement_kind='high')
        self.assertFalse(snapshot.scoreable_forecast, 'a future cache modification contradicts as-of capture')
        self.assertEqual(snapshot.availability_reason, 'source_reported_clock_after_capture')
        self.assertEqual(snapshot.high_temp_f, 80)
        self.assertEqual(snapshot.source_details['source_cache_updated_at_ms'], future_ms)
        self.assertEqual(snapshot.source_details['source_clock_basis'], 'capture_time_provider_issue_time_unavailable')
        self.assertEqual(snapshot.as_of, now)
        self.assertIn('capture', snapshot.source_details)

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
        self.assertEqual(capture_text(snapshot.source_details['capture']), body)
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
        self.assertEqual(capture_text(snapshot.source_details['capture']), 'Not found')

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


class EngineCollectionTests(_CaptureTestCase):
    def test_nws_future_update_clock_retains_original_values_but_disqualifies_use(self):
        from unittest.mock import patch
        now = datetime(2026, 10, 19, 3, tzinfo=timezone.utc)
        future = '2026-10-21T03:00:00Z'
        body = json.dumps({'properties': {'updateTime': future, 'periods': [
            {'name':'Monday', 'startTime':'2026-10-19T06:00:00-05:00', 'endTime':'2026-10-19T18:00:00-05:00',
             'isDaytime':True, 'temperature':81, 'temperatureUnit':'F'}]}})
        with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body))) as client:
            feed = weather_pro.NWSFeed()
            feed.http.close(); feed.http = client
            feed._resolve_forecast_grid = lambda *a, **kw: ('EWX', 152, 91, None)
            with patch.object(weather_pro, 'datetime', wraps=datetime) as clock:
                clock.now.return_value = now
                snapshot = feed.get_forecast('austin', target_date='2026-10-19', measurement_kind='high', station_resolution={})
        self.assertFalse(snapshot.scoreable_forecast, 'future provider update must not be hidden behind fetch-time as_of')
        self.assertEqual(snapshot.availability_reason, 'source_reported_clock_after_capture')
        self.assertEqual(snapshot.high_temp_f, 81)
        self.assertEqual(snapshot.source_details['source_reported_updated_at'], future)
        self.assertEqual(snapshot.source_details['source_clock_basis'], 'capture_time_with_separate_provider_update_time')
        self.assertEqual(snapshot.as_of, now)
        self.assertIn('capture', snapshot.source_details)

    def test_station_conflict_does_not_gate_city_capture_but_disqualifies_use(self):
        engine = weather_pro.ProWeatherEngine()
        self.addCleanup(engine.close)
        engine.open_meteo.get_forecast = lambda *a, **kw: None
        engine.nws.get_forecast = lambda *a, **kw: None
        engine.nws.get_station_observation = lambda *a: None
        engine.owm.available = False
        requested = []
        def handler(request):
            requested.append(str(request.url))
            return httpx.Response(200, text=gray_page())
        engine.nws.http.close()
        engine.nws.http = httpx.Client(transport=httpx.MockTransport(handler))
        for station in ({'station_id':None, 'station_origin':'market_explicit', 'availability_reason':'conflicting_explicit_stations'},
                        {'station_id':None, 'station_origin':'market_explicit', 'station_mapping':'unknown'}):
            with self.subTest(station=station):
                forecast = engine.get_forecast('las vegas', target_date='2026-10-19', measurement_kind='high', station_resolution=station)
                local = next(s for s in forecast.details['source_snapshots'] if s.source == 'src_local_kvvu_weather')
                self.assertIn('capture', local.source_details, 'station ambiguity must not suppress independent city fetch')
                self.assertFalse(local.scoreable_forecast)
                self.assertEqual(local.high_temp_f, 80, 'preserve parsed values even when association disqualifies use')
                self.assertEqual(local.source_details['forecast_location']['basis'], 'city')
                self.assertEqual(local.source_details['market_station_association'], station)
                self.assertEqual(forecast.sources_used, [])
        self.assertEqual(len(requested), 2)

    def test_all_failed_attempts_persist_independently_of_numerical_scoring(self):
        from bot.feeds.weather_capture_store import WeatherCaptureStore
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            store = WeatherCaptureStore(storage_root=root)
            engine = weather_pro.ProWeatherEngine(capture_store=store)
            self.addCleanup(engine.close)
            engine.open_meteo.get_forecast = lambda *a, **kw: None
            engine.nws.get_forecast = lambda *a, **kw: None
            engine.nws.get_station_observation = lambda *a: None
            engine.owm.available = False
            engine.nws.http.close()
            engine.nws.http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text='<p>No numeric forecast</p>')))
            signal = engine.score_temperature_market_with_context('Will Austin high temperature be above 85° on Oct 19, 2026?', .5)
            self.assertIsInstance(signal, dict, 'all-failure collection must be exported even with no scoring signal')
            self.assertTrue(signal['collection_only'])
            self.assertFalse(signal['prediction_available'])
            self.assertNotIn('predicted_prob', signal)
            self.assertNotIn('probability_evidence', signal)
            self.assertEqual(signal['confidence'], 0)
            self.assertEqual(len(signal['data']['source_details']), 7)
            receipt = signal['data']['collection_receipt']
            retained = json.loads(store.read_receipt_verified(receipt))
            self.assertEqual(retained['sources'], signal['data']['source_details'])
            self.assertEqual(len(retained['sources']), 7)
            self.assertTrue(all(s['scoreable_forecast'] is False for s in retained['sources']))
            self.assertTrue((Path(root) / receipt['receipt_locator']).exists())
            local = next(s for s in retained['sources'] if s['source_id'] == 'src_local_ktbc_weather')
            self.assertEqual(store.read_verified(local['source_metadata']['capture']), b'<p>No numeric forecast</p>')

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
        self.assertEqual(capture_text(snapshot.source_details['capture']), body)
        self.assertEqual(snapshot.source_details['capture']['payload_sha256'], hashlib.sha256(body.encode()).hexdigest())
        unavailable = feed.get_forecast('austin', target_date='2026-10-20', station_resolution={})
        self.assertFalse(unavailable.scoreable_forecast)
        self.assertEqual(capture_text(unavailable.source_details['capture']), body)
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
        self.assertEqual(capture_text(snapshot.source_details['capture']),body)
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
        self.assertEqual(capture_text(snapshot.source_details['capture']),'Service temporarily unavailable')
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
