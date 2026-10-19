"""Offline producer-to-sealed regressions; provider mutations are synthetic.

Default bodies are synthetic schema fixtures. The bounded original-capture
harness also calls this same path with hash-verified original-derived bodies.
No credentials, real market execution, or live HTTP is used here.
"""
import json
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from bot.feeds import weather_pro as wp
from bot.feeds.weather_capture_store import WeatherCaptureStore
from bot.file_ops import append_jsonl
from bot.prediction_lab import PredictionLab
from bot.replay_decision_input import (
    build_replay_decision_input_v1, verify_replay_decision_input_record_v1,
)
from bot.weather.registry import WeatherRegistry
from tests.test_weather_local_other import sinclair_page, scripps_page


def configured_source(sid):
    registry = WeatherRegistry.from_file()
    return next(s for c in registry.as_dict()['cities']
                for s in registry.collection_sources(c['city_id']) if s['source_id'] == sid)


@contextmanager
def captured_failure_pipeline(body, sid, kind, *, usable_neighbor=False, output_root=None):
    """Real configured feed/scorer, persisted receipt, Lab JSONL, sealed readback."""
    source = configured_source(sid)
    lifetime = nullcontext(output_root) if output_root is not None else tempfile.TemporaryDirectory(
        prefix='capture-failure-provenance-')
    with lifetime as directory:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        store = WeatherCaptureStore(storage_root=root)
        engine = wp.ProWeatherEngine(capture_store=store)
        try:
            engine.owm.available = False
            engine.nws.get_station_observation = lambda *a, **kw: None
            engine.nws.get_forecast = lambda *a, **kw: None
            if not usable_neighbor:
                engine.open_meteo.get_forecast = lambda *a, **kw: None
            else:
                engine.open_meteo.http.close()
                # Synthetic station-metadata fixture for this mock transport;
                # no production coordinate verification or geography is invented.
                def global_response(request):
                    if request.url.host == 'api.weather.gov':
                        station = request.url.path.rsplit('/', 1)[-1]
                        lat, lon = wp.CITY_COORDS[source['city'].lower()]
                        return httpx.Response(200, json={'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
                                                        'properties': {'stationIdentifier': station}})
                    assert request.url.host == 'api.open-meteo.com'
                    return httpx.Response(200, json={
                        'timezone': source['timezone'], 'hourly': {
                            'time': ['2026-10-19T06:00', '2026-10-19T15:00'],
                            'temperature_2m': [60, 90]}})
                engine.open_meteo.http = httpx.Client(transport=httpx.MockTransport(global_response))
            from bot.feeds.weather_local_other import OTHER_LOCAL_URLS
            url = OTHER_LOCAL_URLS.get(sid, source['url'])
            requested = []
            def respond(request):
                assert str(request.url) == url
                requested.append(str(request.url))
                return httpx.Response(200, content=body, headers={'content-type': 'text/html'})
            engine.nws.http.close()
            engine.nws.http = httpx.Client(transport=httpx.MockTransport(respond))
            question = f"Will {source['city']} {kind} temperature be above 85° on Oct 19, 2026?"
            # Synthetic decision clock; provider fields are never rewritten here.
            from datetime import datetime, timezone
            class DecisionClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    instant = cls(2026, 10, 19, 3, 30, tzinfo=timezone.utc)
                    return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
            with patch.object(wp, 'datetime', DecisionClock):
                signal = engine.score_temperature_market_with_context(question, .5)
            assert isinstance(signal, dict)
            assert requested == [url], requested
            detail = next(s for s in signal['data']['source_details'] if s['source_id'] == sid)
            collection_ref = signal['data']['collection_receipt']
            # Read the independently persisted collection, not the in-memory source.
            collection = json.loads(store.read_receipt_verified(collection_ref))
            retained = next(s for s in collection['sources'] if s['source_id'] == sid)
            lab = PredictionLab.__new__(PredictionLab)
            lab.config = {}; lab.collector_interval_seconds = 900; lab.observer_mode = True
            lab.mode = 'collector'; lab.paper_lab_mode = 'opportunity'; lab.hypothetical_mode = 'flat'
            lab.opportunity_bankroll_usd = 100; lab.fresh_wallet_bankroll_usd = 100
            market = SimpleNamespace(id=f'TEST-{sid}-{kind}', question=question,
                                     metadata={'market_group': 'weather'})
            strategy = {'direction': 'SKIP', 'signal_details': {'live': signal},
                        'question': question, 'market_id': market.id}
            artifact = {'strategy_signal': strategy, 'final_action': 'SKIP'}
            lab._attach_weather_source_snapshot(artifact, market)
            row = lab._build_market_snapshot_row('capture-failure-correctness-only', market, strategy,
                decision_type='skip', prediction_recorded=False, decision_artifact=artifact,
                observed_at='2026-10-19T03:30:00Z')
            path = root / 'market.jsonl'
            append_jsonl(path, row)
            written = json.loads(path.read_text())
            built = build_replay_decision_input_v1(written, source_history_only=True)
            assert built.ok, built.errors
            assert isinstance(built.record, dict)
            sealed_path = root / 'sealed.jsonl'
            append_jsonl(sealed_path, built.record)
            sealed = json.loads(sealed_path.read_text())
            assert verify_replay_decision_input_record_v1(sealed)
            snapshot = sealed['source_inputs']['source_context']['data']['weather_source_snapshot']
            replay = next(s for s in snapshot['sources'] if s['source_id'] == sid)
            assert snapshot['collection_receipt'] == collection_ref
            assert json.loads(store.read_receipt_verified(snapshot['collection_receipt'])) == collection
            yield store, signal, detail, retained, replay
        finally:
            engine.close()


class StateProvenanceTests(unittest.TestCase):
    def assert_state_path(self, body, sid, kind, expected_location, *, available=False):
        with captured_failure_pipeline(body, sid, kind) as (store, signal, detail, retained, replay):
            for row in (detail, retained, replay):
                self.assertEqual(row['scoreable_forecast'], available)
                self.assertEqual(row['source_metadata']['source_reported_location'], expected_location)
                self.assertEqual(store.read_verified(row['source_metadata']['capture']), body)
                if not available:
                    self.assertEqual(row['availability_reason'], 'source_forecast_state_mismatch')
                    self.assertEqual(row.get('weight', 0), 0)
            if not available:
                self.assertTrue(signal['collection_only'])
                self.assertNotIn('predicted_prob', signal)

    def test_explicit_wrong_states_survive_unavailable_collection_and_seal(self):
        for sid, page, field in (
            ('src_local_kokh_weather', sinclair_page(), 'stateAbbreviation'),
            ('src_local_kgtv_weather', scripps_page(daily_only=True), 'state'),
        ):
            body = (sinclair_page(stateAbbreviation='NY').encode() if field == 'stateAbbreviation'
                    else page.replace('&quot;CA&quot;', '&quot;NY&quot;').encode())
            location = ({'city': 'Oklahoma City', 'stateAbbreviation': 'NY',
                         'latitude': '35.508', 'longitude': '-97.478', 'ianaTimeZone': 'America/Chicago'}
                        if field == 'stateAbbreviation' else
                        {'city': 'San Diego', 'state': 'NY', 'ianaTimeZone': 'America/Los_Angeles',
                         'selected_period_location': {'city': 'San Diego', 'state': 'NY'}})
            for kind in ('high', 'low'):
                with self.subTest(sid=sid, kind=kind):
                    self.assert_state_path(body, sid, kind, location)

    def test_all_supported_state_aliases_are_reconciled_not_first_selected(self):
        from bot.feeds.weather_local import local_geography_reason
        for key in ('adminDistrictCode', 'stateAbbreviation', 'state'):
            for wrong in ('NY', 42, True, ['OK']):
                location = {'adminDistrictCode': 'OK', 'stateAbbreviation': 'OK', 'state': 'OK', key: wrong}
                with self.subTest(key=key, wrong=wrong):
                    self.assertEqual(local_geography_reason(location, city='oklahoma city', city_id='oklahoma_city_ok'),
                                     'source_forecast_state_mismatch')
        self.assertIsNone(local_geography_reason({'stateAbbreviation': 'OK', 'state': 'OK'},
                          city='oklahoma city', city_id='oklahoma_city_ok'))

    def test_sinclair_original_full_state_name_agrees_with_abbreviation(self):
        body = sinclair_page(state='Oklahoma').encode()
        location = {'city': 'Oklahoma City', 'stateAbbreviation': 'OK', 'state': 'Oklahoma',
                    'latitude': '35.508', 'longitude': '-97.478', 'ianaTimeZone': 'America/Chicago'}
        for kind in ('high', 'low'):
            with self.subTest(kind=kind):
                self.assert_state_path(body, 'src_local_kokh_weather', kind, location, available=True)

    def test_sinclair_conflicting_alias_is_not_dropped_by_parser(self):
        body = sinclair_page(stateAbbreviation='OK', state='NY', adminDistrictCode='OK').encode()
        location = {'city': 'Oklahoma City', 'stateAbbreviation': 'OK', 'state': 'NY',
                    'adminDistrictCode': 'OK', 'latitude': '35.508', 'longitude': '-97.478',
                    'ianaTimeZone': 'America/Chicago'}
        for kind in ('high', 'low'):
            with self.subTest(kind=kind):
                self.assert_state_path(body, 'src_local_kokh_weather', kind, location)

    def test_scripps_selected_row_state_cannot_hide_behind_matching_anchor(self):
        body = scripps_page().replace('&quot;state&quot;: &quot;CA&quot;, &quot;date&quot;',
                                     '&quot;state&quot;: &quot;NY&quot;, &quot;date&quot;').encode()
        location = {'city': 'San Diego', 'state': 'CA', 'ianaTimeZone': 'America/Los_Angeles',
                    'selected_period_location': {'city': 'San Diego', 'state': 'NY'}}
        for kind in ('high', 'low'):
            with self.subTest(kind=kind):
                self.assert_state_path(body, 'src_local_kgtv_weather', kind, location)


class ParserFailureProvenanceTests(unittest.TestCase):
    def assert_failure_path(self, body, sid, kind, reason, error_type, *, usable_neighbor=False):
        with captured_failure_pipeline(body, sid, kind, usable_neighbor=usable_neighbor) as (
                store, signal, detail, retained, replay):
            self.assertIsInstance(signal, dict)
            for row in (detail, retained, replay):
                self.assertFalse(row['scoreable_forecast'])
                self.assertEqual(row['availability_reason'], reason)
                self.assertIsNone(row.get('forecast_high'))
                self.assertIsNone(row.get('forecast_low'))
                metadata = row['source_metadata']
                self.assertEqual(store.read_verified(metadata['capture']), body)
                self.assertEqual(metadata['capture'], detail['source_metadata']['capture'])
                self.assertEqual(metadata['adapter_error_type'], error_type)
                self.assertEqual(len(metadata['collection_attempts']), 1)
                attempt = metadata['collection_attempts'][0]
                self.assertEqual(attempt['error_type'], error_type)
                self.assertEqual(attempt['url'], metadata['capture']['retrieval_url'])
                self.assertEqual(attempt['http_status'], 200)
                self.assertLessEqual(attempt['attempted_at'], attempt['fetched_at'])
                self.assertEqual(metadata['collection_attempts'], detail['source_metadata']['collection_attempts'])
                self.assertNotIn('DO_NOT_RETAIN_EXCEPTION_MESSAGE', json.dumps(row))
            if usable_neighbor:
                neighbor = next(s for s in signal['data']['source_details'] if s['source_id'] == 'open_meteo')
                self.assertTrue(neighbor['scoreable_forecast'])
                self.assertEqual(signal['data']['forecast_high' if kind == 'high' else 'forecast_low'],
                                 90 if kind == 'high' else 60)
                self.assertIn('predicted_prob', signal)
            else:
                self.assertTrue(signal['collection_only'])
                self.assertNotIn('predicted_prob', signal)

    def test_null_and_type_invalid_city_are_explicit_schema_unavailability(self):
        for invalid in (None, 42, [], {}):
            body = sinclair_page(city=invalid).encode()
            for kind in ('high', 'low'):
                with self.subTest(invalid=invalid, kind=kind):
                    self.assert_failure_path(body, 'src_local_kokh_weather', kind,
                        'source_specific_forecast_schema_invalid', 'LocalForecastUnavailable')

    def test_unexpected_parser_error_keeps_receipt_attempt_and_neighbor(self):
        from bot.feeds.weather_local_other import OTHER_LOCAL_ADAPTERS
        def fail(*args, **kwargs):
            raise RuntimeError('DO_NOT_RETAIN_EXCEPTION_MESSAGE')
        with patch.dict(OTHER_LOCAL_ADAPTERS, {'src_local_kokh_weather': fail}):
            for kind in ('high', 'low'):
                for usable_neighbor in (False, True):
                    with self.subTest(kind=kind, usable_neighbor=usable_neighbor):
                        self.assert_failure_path(sinclair_page().encode(), 'src_local_kokh_weather', kind,
                            'source_forecast_unexpected_error', 'RuntimeError', usable_neighbor=usable_neighbor)

    def test_body_and_collection_retention_errors_propagate_without_cache(self):
        from bot.feeds.weather_capture_store import CaptureRetentionError
        source = configured_source('src_local_kokh_weather')
        for method in ('retain', 'retain_receipt'):
            for kind in ('high', 'low'):
                with self.subTest(method=method, kind=kind), tempfile.TemporaryDirectory() as root:
                    store = WeatherCaptureStore(storage_root=root)
                    engine = wp.ProWeatherEngine(capture_store=store)
                    try:
                        engine.open_meteo.get_forecast = lambda *a, **kw: None
                        engine.nws.get_forecast = lambda *a, **kw: None
                        engine.owm.available = False
                        engine.nws.http.close()
                        engine.nws.http = httpx.Client(transport=httpx.MockTransport(
                            lambda request: httpx.Response(200, content=sinclair_page().encode())))
                        with patch.object(store, method, side_effect=CaptureRetentionError('synthetic storage failure')):
                            with self.assertRaises(CaptureRetentionError):
                                engine.get_forecast(source['city'], target_date='2026-10-19',
                                                    measurement_kind=kind, station_resolution={})
                        self.assertEqual(engine._cache, {})
                    finally:
                        engine.close()
