"""Real collector -> aggregator -> strategy -> Lab seams, using scratch evidence."""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import test_contextual_weather_source_flow as fixtures
from bot.feeds.weather_capture_store import CaptureRetentionError
from bot.prediction_lab import PredictionLab

EVIDENCE = {}


class WeatherCaptureConsumerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ContextualWeatherSourceFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def lab(self, enabled=False, *, configured_root=None):
        config = {'data_dir': str(self.root / 'lab'),
                  'runtime': {'storage_root': str(configured_root or self.root / 'configured')},
                  'strategy': {'enable_news': False, 'enable_social': False, 'enable_ai': False,
                               'min_edge': .01, 'signal_audit_path': str(self.root / 'audit.jsonl'),
                               'weather_source_selection': {'enabled': enabled}},
                  'prediction_lab': {'score_only': True}}
        # An explicit wrong fallback makes the RED safe and root mismatch visible.
        with patch('bot.feeds.weather_pro.collector_root', return_value=self.root / 'fallback'):
            lab = PredictionLab(config)
        self.addCleanup(lab.strategy.live_feeds.close)
        weather = lab.strategy.live_feeds.weather
        weather.owm.available = False
        for feed in (weather.nws, weather.open_meteo, weather.owm):
            feed.http.close()
            feed.http = httpx.Client(transport=httpx.MockTransport(self.unavailable))
        return lab

    def unavailable(self, request):
        # Resolve station identity normally, then fail each independent forecast
        # transport so the current producer must retain all three real bodies.
        if request.url.path == '/stations/KLAS' or request.url.path.startswith('/points/'):
            return self.fixture.http_handler(request)
        if request.url.path.endswith('/observations/latest'):
            return self.fixture.http_handler(request)
        self.fixture.requests.append(str(request.url))
        return httpx.Response(503, content=b'fixture-unavailable:' + request.url.host.encode())

    def test_lab_owns_one_configured_store_shared_through_every_consumer(self):
        lab = self.lab()
        weather = lab.strategy.live_feeds.weather
        self.assertEqual(weather.capture_store.storage_root, self.root / 'configured')
        store = weather.capture_store
        for consumer in (lab, lab.strategy, lab.strategy.live_feeds, weather.nws, weather.open_meteo):
            self.assertIs(consumer.capture_store, store)
        from bot.feeds.weather_pro import LocalForecastFeed
        original = LocalForecastFeed.__init__
        local_stores = []
        def local_init(feed, http, *, capture_store=None):
            local_stores.append(capture_store)
            original(feed, http, capture_store=capture_store)
        with patch.object(LocalForecastFeed, '__init__', local_init):
            result, trace = self.fixture.analyze(lab.strategy)
        self.assertEqual(local_stores, [store])
        self.assertIsNone(result)
        live = trace.raw_signals['live']
        self.assertTrue(store.read_receipt_verified(live['data']['collection_receipt']))
        self.assertFalse((self.root / 'fallback').exists())
        EVIDENCE['store_chain'] = {'effective_root': str(store.storage_root),
                                   'lab_strategy_aggregator_engine_global_local_same_instance': True,
                                   'collection_receipt': live['data']['collection_receipt']}

    def test_effective_environment_root_takes_precedence_over_configured_root(self):
        with patch.dict(os.environ, {'PREDICTION_BOT_COLLECTOR_ROOT': str(self.root / 'override')}):
            lab = self.lab()
        self.assertEqual(lab.capture_store.storage_root, self.root / 'override')
        self.assertIs(lab.capture_store, lab.strategy.live_feeds.weather.capture_store)
        EVIDENCE['effective_root_override'] = str(lab.capture_store.storage_root)

    def test_relative_configured_root_rejected_before_collection(self):
        with self.assertRaisesRegex(ValueError, 'absolute'):
            self.lab(configured_root=Path('relative-capture-root'))
        self.assertEqual(self.fixture.requests, [])

    def assert_collection_only(self, enabled):
        lab = self.lab(enabled)
        price = {'signal_type': 'price', 'predicted_prob': .9, 'confidence': .9}
        with patch.object(lab.strategy.validator, 'validate_all', wraps=lab.strategy.validator.validate_all) as validate:
            result, trace = self.fixture.analyze(lab.strategy, price=price)
            plain = self.fixture.analyze(lab.strategy, price=price, traced=False)
            self.assertIsNone(result, 'No weather odds exist; price must not manufacture permission')
            self.assertIsNone(plain)
            validate.assert_not_called()
        self.assertEqual(trace.skip_reason_code, 'weather_collection_only')
        self.assertEqual(trace.accepted_signals, {})
        live = trace.raw_signals['live']
        self.assertTrue(live['collection_only'])
        self.assertIs(live['prediction_available'], False)
        self.assertEqual(live['confidence'], 0)
        self.assertEqual(live['availability_reason'], 'no_eligible_forecast_sources')
        for field in ('predicted_prob', 'direction', 'probability_evidence'):
            self.assertNotIn(field, live)
        if enabled:
            audit = live['data']['weather_source_selection']
            self.assertIs(audit['routeable'], False)
            self.assertIn('no_eligible_forecast_sources', audit['blockers'])
        else:
            self.assertNotIn('weather_source_selection', live['data'])
        details = live['data']['source_details']
        self.assertGreaterEqual(len(details), 4)
        self.assertTrue(all(not detail['scoreable_forecast'] for detail in details))
        self.assertTrue(any(detail['source_id'] == fixtures.LOCAL for detail in details))
        captures = [detail['source_metadata']['capture'] for detail in details
                    if detail.get('source_metadata', {}).get('capture')]
        self.assertEqual(len(captures), 3)
        self.assertEqual({detail['source_id'] for detail in details
                          if detail.get('source_metadata', {}).get('capture')},
                         {'nws', 'open_meteo', fixtures.LOCAL})
        store = lab.strategy.live_feeds.weather.capture_store
        for capture in captures:
            self.assertTrue(store.read_verified(capture).startswith(b'fixture-unavailable:'))
            self.assertNotIn('payload_text', capture)
            self.assertNotIn('payload_base64', capture)
        receipt = live['data']['collection_receipt']
        collection = json.loads(store.read_receipt_verified(receipt))
        self.assertTrue(collection)
        artifact = {'strategy_trace': trace.to_dict(), 'final_action': 'SKIP',
                    'final_reason_code': trace.skip_reason_code}
        lab._attach_weather_source_snapshot(artifact, self.fixture.market())
        path = self.root / 'collection-only.jsonl'
        path.write_text(json.dumps(artifact, allow_nan=False) + '\n')
        recorded = json.loads(path.read_text())
        snapshot = recorded['source_context']['data']['weather_source_snapshot']
        self.assertTrue(snapshot['collection_only'])
        self.assertIs(snapshot['prediction_available'], False)
        self.assertEqual(snapshot['availability_reason'], live['availability_reason'])
        self.assertEqual(snapshot['collection_receipt'], receipt)
        self.assertEqual(snapshot['source_signal']['data'], live['data'])
        self.assertTrue(snapshot['source_signal']['collection_only'])
        for field in ('predicted_prob', 'direction', 'probability_evidence'):
            self.assertNotIn(field, snapshot['source_signal'])
            self.assertNotIn(field, snapshot)
        retained = [source['source_metadata']['capture'] for source in snapshot['sources']
                    if source.get('source_metadata', {}).get('capture')]
        self.assertEqual(retained, captures)
        for capture in retained:
            store.read_verified(capture)
        EVIDENCE['enabled' if enabled else 'disabled'] = {
            'effective_root': str(store.storage_root), 'traced_result': result, 'plain_result': plain,
            'validator_calls': validate.call_count, 'skip_reason': trace.skip_reason_code,
            'source_count': len(details), 'verified_capture_count': len(retained),
            'all_original_bodies_rehydrated': True, 'collection_receipt_verified': True,
            'snapshot': snapshot, 'collection': collection,
        }

    def test_enabled_all_unavailable_is_retained_without_numerical_scoring(self):
        self.assert_collection_only(True)

    def test_disabled_all_unavailable_is_retained_without_numerical_scoring(self):
        self.assert_collection_only(False)

    def test_retention_failure_is_not_ordinary_unavailable_in_either_api_or_mode(self):
        for enabled in (True, False):
            for traced in (True, False):
                with self.subTest(enabled=enabled, traced=traced):
                    lab = self.lab(enabled)
                    store = lab.strategy.live_feeds.weather.capture_store
                    with patch.object(store, 'retain', side_effect=CaptureRetentionError('fixture retention failed')):
                        with self.assertRaisesRegex(CaptureRetentionError, 'fixture retention failed'):
                            self.fixture.analyze(lab.strategy, traced=traced)
