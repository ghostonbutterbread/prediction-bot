"""Configured local-source wiring, without test-only adapter overrides."""
from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch

import httpx

from bot.feeds import weather_pro
from bot.feeds.weather_capture_store import WeatherCaptureStore
from bot.weather.registry import WeatherRegistry
from tests.test_weather_local_fox import CAPTURED


class LocalRegistryWiringTests(unittest.TestCase):
    def test_fox_registry_dispatches_both_kinds_without_adapter_override(self):
        registry = WeatherRegistry.from_file()
        with tempfile.TemporaryDirectory() as root:
            store = WeatherCaptureStore(storage_root=root)
            for source_id, original_sha, city, zone, high, low, following_low, body in CAPTURED:
                source = next(s for s in registry.collection_sources(city) if s['source_id'] == source_id)
                with self.subTest(source_id=source_id), httpx.Client(
                        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))) as client:
                    feed = weather_pro.LocalForecastFeed(client, capture_store=store)
                    for kind, expected in (('high', high), ('low', low)):
                        snapshot = feed.get_forecast(source, target_date='2026-10-19', measurement_kind=kind)
                        self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
                        self.assertEqual(snapshot.high_temp_f if kind == 'high' else snapshot.low_temp_f, expected)
                        self.assertEqual(store.read_verified(snapshot.source_details['capture']).decode('utf-8'), body)
                    self.assertEqual(source['collection_adapter'], 'fox_nuxt_daily_v1')
                    self.assertIsNone(source.get('collection_blocker'))
                    self.assertEqual(source['status'], 'watch_only')


    def test_tegna_registry_dispatches_partial_predictors_without_adapter_override(self):
        from tests.test_weather_local_tegna import CAPTURES
        registry = WeatherRegistry.from_file()
        with tempfile.TemporaryDirectory() as root:
            store = WeatherCaptureStore(storage_root=root)
            for capture in CAPTURES:
                source = next(s for s in registry.collection_sources(capture['city'])
                              if s['source_id'] == capture['source_id'])
                now = datetime.fromisoformat(capture['fetched_at'])
                with self.subTest(source_id=source['source_id']), httpx.Client(
                        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=capture['html']))) as client:
                    feed = weather_pro.LocalForecastFeed(client, capture_store=store)
                    for kind in ('high', 'low'):
                        with patch.object(weather_pro, 'datetime', wraps=datetime) as clock:
                            clock.now.return_value = now
                            snapshot = feed.get_forecast(source, target_date='2026-10-19', measurement_kind=kind)
                        self.assertTrue(snapshot.scoreable_forecast, snapshot.availability_reason)
                        self.assertEqual(snapshot.high_temp_f if kind == 'high' else snapshot.low_temp_f,
                                         capture['expected_' + kind])
                        self.assertIs(snapshot.source_details['full_day_measurement_equivalent'], False)
                        self.assertEqual(store.read_verified(snapshot.source_details['capture']).decode('utf-8'), capture['html'])
                    self.assertEqual(source['collection_adapter'], 'tegna_public_html_v1')
                    self.assertIsNone(source.get('collection_blocker'))
                    self.assertEqual(source['status'], 'watch_only')


    def test_registry_reports_adapter_capability_without_promoting_trust(self):
        import json
        from bot.feeds.weather_local_other import OTHER_LOCAL_ADAPTERS, OTHER_LOCAL_UNAVAILABLE_REASONS
        registry = WeatherRegistry.from_file()
        assert registry.source_path is not None
        data = json.loads(registry.source_path.read_text())
        locals_by_id = {s['source_id']: s for s in data['sources'] if s['source_id'].startswith('src_local_')}
        self.assertEqual(len(locals_by_id), 24)
        self.assertEqual(len(OTHER_LOCAL_UNAVAILABLE_REASONS), 4)
        available = set(locals_by_id) - set(OTHER_LOCAL_UNAVAILABLE_REASONS)
        self.assertEqual(len(available), 20)
        for source_id in available:
            with self.subTest(source_id=source_id):
                source = locals_by_id[source_id]
                self.assertIsNone(source.get('collection_blocker'))
                self.assertEqual(source['collection_status'], 'dated_predictor_adapter_available')
                if source_id not in OTHER_LOCAL_ADAPTERS:
                    self.assertIn(source['collection_adapter'],
                                  ('fox_nuxt_daily_v1', 'tegna_public_html_v1', 'gray_fusion_daily_v1'))
        for source_id, reason in OTHER_LOCAL_UNAVAILABLE_REASONS.items():
            with self.subTest(unavailable=source_id):
                source = locals_by_id[source_id]
                self.assertEqual(source['collection_blocker'], reason)
                self.assertEqual(source['collection_status'], 'capture_only_dated_predictor_unavailable')
        for source in locals_by_id.values():
            self.assertEqual(source['status'], 'watch_only')
            self.assertEqual(source['trust_score'], 0)
            self.assertIs(source['verified'], False)
            self.assertEqual(source['metrics']['sample_size'], 0)


if __name__ == '__main__':
    unittest.main()
