"""Captured public Gray payload -> real producer -> predictive learning, offline.

Forecast values are captured public data, not invented measurements. Outcomes,
settlement/knowledge and candidate clocks are explicitly SYNTHETIC test fixtures.
The compact fixtures retain the exact captured wx-current-conditions-v3 entry;
only the inert HTML/JSON envelope is minimized. Set LOCAL_LOW_CAPTURE_DIR to run
against full public-response bytes (whose hashes are pinned below) instead.
"""
import base64
import copy
import hashlib
import json
import os
import tempfile
import unittest
import zlib
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from bot.collector_paths import COLLECTOR_ROOT_ENV
from bot.feeds import weather_pro
from bot.prediction_lab import PredictionLab
from bot.replay_decision_input import build_replay_decision_input_v1
from bot.weather.predictive_source_history import load_predictive_source_history, select_contextual_weather_source
import test_auto_source_router_promotion as promotion_fixtures


# Exact entry bytes extracted from the hash-verified public captures. Never eval.
CAPTURES = {
    'las vegas': {
        'source_id': 'src_local_kvvu_weather',
        'url': 'https://www.fox5vegas.com/weather/',
        'payload_sha256': '8b5cf3250054c1b98e4e5c4da841faf6d5574ac5b9e12ac8a4633bdab72bf9c1',
        'entry_sha256': 'ff1dfaf60d7d0a8775b5e792ebe25fcecdf6cea203217439d2c63c970dd36dc3',
        'entry_zlib_base64': (
            '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'
        ),
        'low_f': 63,
        'ticker': 'LV',
        'city_id': 'las_vegas_nv',
    },
    'new orleans': {
        'source_id': 'src_local_wvue_weather',
        'url': 'https://www.fox8live.com/weather/',
        'payload_sha256': '606819f9ad1f4f6808ab1c27ee7bfb1b6bd68e81fa2292e6d6c70c02e05dd648',
        'entry_sha256': 'aca144e8e9ada443d763306d78a4375cb5458e02aec732d55b533a19ac8d6733',
        'entry_zlib_base64': (
            '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'
            'bq80+k0qJYaIPj7+D1NxMog='
        ),
        'low_f': 56,
        'ticker': 'NOLA',
        'city_id': 'new_orleans_la',
    },
}


class _CapturedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = cls(2026, 10, 19, 2, 40, tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


def captured_low(city):
    fixture = CAPTURES[city]
    entry = zlib.decompress(base64.b64decode(fixture['entry_zlib_base64']))
    assert hashlib.sha256(entry).hexdigest() == fixture['entry_sha256']
    if os.environ.get('LOCAL_LOW_CAPTURE_DIR'):
        page = (Path(os.environ['LOCAL_LOW_CAPTURE_DIR']) / fixture['source_id'] / 'raw.bin').read_bytes()
        assert hashlib.sha256(page).hexdigest() == fixture['payload_sha256']
        assert entry in page
    else:
        page = b'<script id="fusion-metadata">Fusion.contentCache={"wx-current-conditions-v3":' + entry + b'};</script>'
    used = []

    def respond(request):
        assert str(request.url) == fixture['url'], request.url
        used.append(str(request.url))
        return httpx.Response(200, content=page, headers={'content-type': 'text/html; charset=utf-8'})

    with tempfile.TemporaryDirectory(prefix='local-low-captures-') as capture_root:
        with patch.dict(os.environ, {COLLECTOR_ROOT_ENV: capture_root}):
            engine = weather_pro.ProWeatherEngine()
            try:
                engine.open_meteo.get_forecast = lambda *a, **kw: None
                engine.nws.get_forecast = lambda *a, **kw: None
                engine.nws.get_station_observation = lambda *a, **kw: None
                engine.owm.available = False
                engine.nws.http.close()
                engine.nws.http = httpx.Client(transport=httpx.MockTransport(respond))
                with patch.object(weather_pro, 'datetime', _CapturedClock):
                    forecast = engine.get_forecast(city, target_date='2026-10-19', measurement_kind='low', require_target=True)
                assert forecast is not None
                details = engine._source_contribution_details(forecast, city=city, forecast_measurement_kind='low', contract_shape='tail', question_side='above')
                sources = PredictionLab._weather_snapshot_sources(
                    {'source_details': details, 'forecast_driver': forecast.details.get('forecast_driver')},
                    settlement_source='unknown', market_date='2026-10-19', as_of=forecast.fetched_at.isoformat(),
                    station_resolution={}, source_agreement=None)
            finally:
                engine.close()
    assert used == [fixture['url']], used
    source = next(source for source in sources if source['source_id'] == fixture['source_id'])
    assert source['forecast_low'] == fixture['low_f']
    assert source['scoreable_forecast'] is True
    mid = f"KXLOW{fixture['ticker']}-26OCT19-T70"
    question = f'Will {city.title()} low temperature be above 70° on October 19, 2026?'
    raw = promotion_fixtures._collector_row(market_id=mid)
    raw.update(question=question, observed_at='2026-10-19T03:00:00Z')
    context = raw['decision_artifact']['source_context']
    context['as_of'] = '2026-10-19T02:40:00Z'
    data = context['data']
    data['market_metadata'].update(city=city.title(), city_id=fixture['city_id'], market_kind='low', event_ticker=mid.rsplit('-', 1)[0])
    data['weather_source_snapshot'].update(market_id=mid, question=question, market_date='2026-10-19',
        station_resolution={'city': city.title(), 'city_id': fixture['city_id']}, sources=[source])
    return raw


def _sources(raw):
    return raw['decision_artifact']['source_context']['data']['weather_source_snapshot']['sources']


def synthetic_resolution(raw, **changes):
    return dict(market_id=raw['market_id'], requested_market_id=raw['market_id'], returned_market_id=raw['market_id'],
        market_status='finalized', kalshi_result='yes', settlement_ts='2026-10-20T08:00:00Z',
        resolved_at='2026-10-20T09:00:00Z', resolution_id='SYNTHETIC-local-low-regression', **changes)


class LocalLowPredictivePipelineTests(unittest.TestCase):
    def publish(self, raw, resolution=None):
        fixture = promotion_fixtures.AutoSourceRouterPromotionTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        result = fixture.run_pipeline([raw], [resolution or synthetic_resolution(raw)])
        return result, load_predictive_source_history(result.history_path.parent / 'run_metadata.json')

    def select(self, raw, history, observed='2026-10-21T00:00:00Z'):
        current = copy.deepcopy(raw)
        current['observed_at'] = observed
        built = build_replay_decision_input_v1(current, source_history_only=True)
        self.assertTrue(built.ok, built.errors)
        assert built.record is not None
        return select_contextual_weather_source(built.record, history, min_sample_count=1)

    def test_captured_prior_night_lows_publish_and_route_without_invented_end(self):
        for city in CAPTURES:
            with self.subTest(city=city):
                raw = captured_low(city)
                source = _sources(raw)[0]
                self.assertEqual(source['forecast_type'], 'local_prior_night_low_predictor')
                self.assertEqual(source['forecast_start'][:10], '2026-10-18')
                self.assertIsNone(source.get('forecast_end'))
                self.assertIsNone(source['target_mapping'].get('source_period_end'))
                publication, history = self.publish(raw)
                self.assertEqual(publication.counts['predictive_eligible'], 1)
                self.assertEqual(len(history), 1)
                row = history[0]
                self.assertFalse(row['eligible_for_source_history'])
                self.assertTrue(row['eligible_for_predictive_history'])
                self.assertIn('partial_day_low_proxy_not_full_day', row['strict_source_proof']['reasons'])
                self.assertEqual(row['forecast_semantics']['forecast_type'], source['forecast_type'])
                self.assertEqual(row['forecast_semantics']['period_start'], source['forecast_start'])
                self.assertIsNone(row['forecast_semantics']['period_end'])
                self.assertEqual(row['forecast_temp_f'], CAPTURES[city]['low_f'])
                self.assertIs(row['direction_correct'], False)  # YES outcome; captured low predicts NO.
                self.assertEqual(row['known_after'], '2026-10-20T09:00:00+00:00')
                choice = self.select(raw, history)
                self.assertTrue(choice['routeable'], choice)
                self.assertEqual(choice['chosen_source_id'], source['source_id'])
                self.assertEqual(choice['selected_forecast_temp_f'], CAPTURES[city]['low_f'])
                self.assertEqual(choice['confidence_type'], 'rank_score_not_calibrated_probability')
                self.assertNotIn('probability_yes', choice)
                # Full default history policy is not bypassed by this test-only min=1.
                built = build_replay_decision_input_v1(dict(raw, observed_at='2026-10-21T00:00:00Z'), source_history_only=True)
                self.assertTrue(built.ok, built.errors)
                assert built.record is not None
                default_choice = select_contextual_weather_source(built.record, history)
                self.assertFalse(default_choice['routeable'])
                self.assertEqual(default_choice['min_sample_count'], 30)
                self.assertEqual(default_choice['max_history_events'], 100)

    def test_captured_prior_night_wrong_night_date_and_forged_semantics_reject(self):
        for city in CAPTURES:
            original = captured_low(city)
            cases = [
                ('following-night', {'forecast_start': '2026-10-19T07:00:00', 'source_period_start': '2026-10-19T07:00:00'}),
                ('two-nights-old', {'forecast_start': '2026-10-17T07:00:00', 'source_period_start': '2026-10-17T07:00:00'}),
                ('conflicting-start-alias', {'forecast_start': '2026-10-19T07:00:00'}),
                ('wrong-end', {'forecast_end': '2026-10-20T06:00:00', 'source_period_end': '2026-10-20T06:00:00'}),
                ('wrong-date', {'source_target_date': '2026-10-20'}),
                ('wrong-mapped-date', {'market_target_date': '2026-10-20'}),
                ('untyped', {'forecast_type': None}),
                ('daytime-semantics', {'forecast_type': 'local_daytime_high_predictor'}),
                ('forged-mapping', {'mapping': 'unproven_prior_night'}),
                ('legacy-label', {'source_evidence_version': None}),
                ('wrong-kind', {'forecast_measurement_kind': 'high'}),
                ('wrong-city', {'source_location_city': 'Miami'}),
                ('bad-zone', {'source_timezone': 'not/a-zone'}),
                ('future-as-of', {'source_as_of': '2026-10-19T03:00:01Z'}),
                ('future-fetch', {'source_fetched_at': '2026-10-19T03:00:01Z'}),
            ]
            mapping_fields = {'source_period_start', 'source_period_end', 'market_target_date', 'mapping', 'source_timezone'}
            for name, changes in cases:
                with self.subTest(city=city, case=name):
                    raw = copy.deepcopy(original)
                    source = _sources(raw)[0]
                    for key, value in changes.items():
                        (source['target_mapping'] if key in mapping_fields else source)[key] = value
                    _, history = self.publish(raw)
                    self.assertFalse(any(row['eligible_for_predictive_history'] for row in history), history)
                    self.assertFalse(self.select(original, history)['routeable'])

    def test_captured_prior_night_aliases_reconcile_in_source_local_time(self):
        for city in CAPTURES:
            with self.subTest(city=city):
                raw = captured_low(city)
                source = _sources(raw)[0]
                original = source['forecast_start']
                # Equivalent UTC/local aliases still identify the previous local row.
                source['forecast_start'] = datetime.fromisoformat(original).astimezone(timezone.utc).isoformat()
                _, history = self.publish(raw)
                self.assertEqual(len(history), 1)
                self.assertTrue(history[0]['eligible_for_predictive_history'])
                # Two different timestamps on that same prior date are not aliases.
                source['forecast_start'] = original.replace('T07:', 'T08:')
                _, conflicting = self.publish(raw)
                self.assertEqual(conflicting, [])

    def test_captured_prior_night_invalid_knowledge_and_outcomes_stay_unusable(self):
        for city in CAPTURES:
            original = captured_low(city)
            for case in ('missing-knowledge', 'malformed-knowledge', 'wrong-id', 'void', 'observation-at-settlement'):
                with self.subTest(city=city, case=case):
                    raw = copy.deepcopy(original)
                    receipt = synthetic_resolution(raw)
                    if case == 'missing-knowledge':
                        receipt.pop('resolved_at')
                    elif case == 'malformed-knowledge':
                        receipt['retrieved_at'] = 'invalid-clock'
                    elif case == 'wrong-id':
                        receipt['returned_market_id'] = 'OTHER'
                    elif case == 'void':
                        receipt['kalshi_result'] = 'void'
                    else:
                        raw['observed_at'] = receipt['settlement_ts']
                    _, history = self.publish(raw, receipt)
                    self.assertFalse(any(row['eligible_for_predictive_history'] for row in history))
                    self.assertFalse(self.select(original, history)['routeable'])

    def test_captured_prior_night_uses_actual_resolver_knowledge_and_exact_receipt(self):
        from bot.weather.source_observation_ledger import materialize_source_observation_ledger
        for city in CAPTURES:
            with self.subTest(city=city):
                raw = captured_low(city)
                resolution = synthetic_resolution(raw)
                resolution['retrieved_at'] = '2026-10-20T10:00:00Z'
                publication, history = self.publish(raw, resolution)
                self.assertEqual(len(history), 1)
                for observed, expected in [('2026-10-20T09:59:59Z', False), ('2026-10-20T10:00:00Z', False), ('2026-10-20T10:00:01Z', True)]:
                    self.assertEqual(self.select(raw, history, observed)['routeable'], expected)
                metadata_path = publication.history_path.parent / 'run_metadata.json'
                metadata = json.loads(metadata_path.read_text())
                inputs = metadata['inputs']
                manifest = json.loads(publication.history_manifest_path.read_text())
                original_receipt = Path(manifest['strict_resolution_path'])
                original_receipt.write_bytes(original_receipt.read_bytes() + b'\n')
                with self.assertRaisesRegex(ValueError, 'receipt mismatch'):
                    materialize_source_observation_ledger(replay_inputs_path=inputs['replay_inputs_path'],
                        finalized_outcomes_path=inputs['finalized_outcomes_path'], strict_resolutions_path=original_receipt,
                        output_dir=metadata_path.parent / 'tampered-receipt-rejected')
