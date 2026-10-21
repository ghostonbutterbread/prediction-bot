"""Bounded synthetic regressions for retained forecast proof and attempt receipts."""
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import httpx

from bot.feeds.weather_capture_store import WeatherCaptureStore
from bot.feeds.weather_pro import LocalForecastFeed, NWSFeed, OpenMeteoFeed
from bot.weather.registry import WeatherRegistry
from bot.weather.source_observation_ledger import _pending_rows_for_input
from test_forecast_selection_contract import ForecastSelectionContractTests, LOCAL, seal
from test_weather_local_fox import AUSTIN_PAGE


class ForecastProofGapRegressions(TestCase):
    def test_wrong_state_fox_page_retains_declaration_but_not_scoreability(self):
        source = next(item for item in WeatherRegistry.from_file().collection_sources('Austin')
                      if item['source_id'] == 'src_local_ktbc_weather')
        with tempfile.TemporaryDirectory() as root, httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=AUSTIN_PAGE.replace('Austin, TX', 'Austin, MN')))) as client:
            result = LocalForecastFeed(client, capture_store=WeatherCaptureStore(storage_root=Path(root))).get_forecast(
                source, target_date='2026-10-19', measurement_kind='high')
            self.assertFalse(result.scoreable_forecast)
            self.assertEqual(result.availability_reason, 'source_forecast_state_mismatch')
            self.assertEqual(result.source_details['source_reported_location']['displayName'], 'Austin, MN')
            self.assertEqual(result.source_details['source_reported_location']['state'], 'MN')
            self.assertEqual(result.high_temp_f, 79)
            self.assertIn('capture', result.source_details)

    def test_sealed_current_and_independent_history_reject_masked_clock_aliases(self):
        helper = ForecastSelectionContractTests(
            'test_equal_contextual_support_abstains_instead_of_claiming_id_order_superiority')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.addCleanup(helper.tearDown)
        _, history = helper.history()  # actual independently bound 30-event history
        for changes, reason in (
            ({'fetched_at': '2026-10-18T19:00:01+00:00'}, 'retained_source_clock_after_immutable_observed_at'),
            ({'as_of': '2026-10-18T19:00:01+00:00'}, 'retained_source_clock_after_immutable_observed_at'),
            ({'source_as_of': '2026-10-18T18:59:59+00:00',
              'source_fetched_at': '2026-10-18T18:59:58+00:00'}, 'source_issue_after_declared_capture'),
            ({'fetched_at': '2026-10-18T18:57:00+00:00'}, 'conflicting_retained_source_clock_aliases'),
        ):
            with self.subTest(changes=changes):
                current = helper.current()
                source = next(item for item in current['source_inputs']['source_context']['data']['weather_source_snapshot']['sources']
                              if item['source_id'] == LOCAL)
                source.update(changes)
                sealed = seal(current)
                choice = helper.selector()(sealed, history)
                self.assertNotEqual(choice['chosen_source_id'], LOCAL)
                observation = next(item for item in choice['current_source_observations'] if item['source_id'] == LOCAL)
                self.assertIn(reason, observation['predictive_source_proof']['reasons'])
                pending = next(item for item in _pending_rows_for_input(sealed, {
                    'market_id': sealed['market_id'], 'canonical_input_sha256': sealed['canonical_input_sha256'],
                    'decision_key': sealed['decision_key'],
                }) if item['source_id'] == LOCAL)
                self.assertEqual(pending['retained_clock_evidence']['source_capture'].get('fetched_at'), changes.get('fetched_at'))
                self.assertNotEqual(pending['predictive_source_proof']['status'], 'eligible_predictive_source_proof')

    def test_global_transport_and_http_parser_receipts(self):
        for feed_type in (OpenMeteoFeed, NWSFeed):
            for mode in ('transport', 'http', 'parser'):
                with self.subTest(feed=feed_type.__name__, mode=mode), tempfile.TemporaryDirectory() as root:
                    def respond(request):
                        if mode == 'transport':
                            raise httpx.ConnectTimeout('fixture', request=request)
                        return httpx.Response(503 if mode == 'http' else 200,
                                              text='unavailable' if mode == 'http' else '{bad json')
                    feed = feed_type(capture_store=WeatherCaptureStore(storage_root=Path(root)))
                    feed.http.close()
                    feed.http = httpx.Client(transport=httpx.MockTransport(respond))
                    self.addCleanup(feed.close)
                    location = dict(city='austin', basis='city', latitude=30.27, longitude=-97.74)
                    with patch('bot.feeds.weather_pro._resolve_forecast_location', return_value=location):
                        if feed_type is NWSFeed:
                            with patch.object(feed, '_resolve_forecast_grid', return_value=('EWX', 1, 1, 'KAUS')):
                                result = feed.get_forecast('austin', target_date='2026-10-19')
                        else:
                            result = feed.get_forecast('austin', target_date='2026-10-19')
                    details = result.source_details
                    [attempt] = details['collection_attempts']
                    self.assertTrue(attempt['attempted_at'])
                    self.assertEqual(attempt['attempt_status'], {'transport': 'transport_failure',
                        'http': 'http_status_failure', 'parser': 'parser_failure'}[mode])
                    self.assertEqual(details['adapter_error_category'], attempt['attempt_status'])
                    self.assertEqual(attempt['url'], details['forecast_query_url'])
                    if feed_type is OpenMeteoFeed:
                        self.assertEqual(attempt['request_parameters']['latitude'], 30.27)
                    if mode == 'transport':
                        self.assertNotIn('capture', details)
                        self.assertNotIn('fetched_at', attempt)
                        self.assertNotIn('http_status', attempt)
                    else:
                        self.assertEqual(attempt['http_status'], 503 if mode == 'http' else 200)
                        self.assertIn('capture', details)
                        self.assertIn('fetched_at', attempt)
