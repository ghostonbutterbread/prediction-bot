"""Real offline capture producer -> central source normalizer -> sealed replay.

Public forecast fields are captured fixtures, not invented provider responses.
Market envelopes/decision clocks are synthetic correctness fixtures. The store
stays alive through JSON readback so retained references are actually verified.
"""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import httpx

from bot.feeds import weather_pro
from bot.feeds.weather_capture_store import WeatherCaptureStore
from bot.prediction_lab import PredictionLab
from bot.replay_decision_input import (
    build_replay_decision_input_v1, verify_replay_decision_input_record_v1,
)
from bot.weather.registry import WeatherRegistry
from tests.test_weather_local_tegna import CAPTURES


@contextmanager
def captured_replay_row(capture=CAPTURES[0], *, kind='high', unavailable=False, capture_store=None) -> Iterator[tuple[dict[str, Any], WeatherCaptureStore, bytes, dict[str, Any]]]:
    """Use the actual scorer and Lab's shared normalizer, with no live HTTP."""
    source = next(s for s in WeatherRegistry.from_file().collection_sources(capture['city'])
                  if s['source_id'] == capture['source_id'])
    source['collection_adapter'] = 'tegna_public_html_v1'
    body = capture['html'].encode()
    expected_sha = capture['fixture_sha256']
    if os.environ.get('TEGNA_CAPTURE_DIR'):
        body = (Path(os.environ['TEGNA_CAPTURE_DIR']) / source['source_id'] / 'raw.bin').read_bytes()
        expected_sha = capture['sha256']
    assert hashlib.sha256(body).hexdigest() == expected_sha
    if unavailable:
        body = b'Captured unavailable response'
    class CapturedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = cls.fromisoformat(capture['fetched_at'])
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    now = CapturedClock.fromisoformat(capture['fetched_at'])
    requested = []

    def respond(request):
        assert str(request.url) == source['url'], request.url
        requested.append(str(request.url))
        return httpx.Response(503 if unavailable else 200, content=body,
                              headers={'content-type': 'text/html; charset=utf-8'})

    registry = WeatherRegistry.from_file()

    class RegistryView:
        def collection_catalog(self):
            return registry.collection_catalog()

        def collection_sources(self, city):
            return [dict(source)]

    with tempfile.TemporaryDirectory(prefix='capture-replay-') as root:
        store = capture_store or WeatherCaptureStore(storage_root=root)
        engine = weather_pro.ProWeatherEngine(capture_store=store)
        try:
            engine.registry = RegistryView()
            engine.open_meteo.get_forecast = lambda *a, **kw: None
            engine.nws.get_forecast = lambda *a, **kw: None
            engine.nws.get_station_observation = lambda *a, **kw: None
            engine.owm.available = False
            engine.nws.http.close()
            engine.nws.http = httpx.Client(transport=httpx.MockTransport(respond))
            question = f"Will {source['city']} {kind} temperature be above 85° on October 19, 2026?"
            with patch.object(weather_pro, 'datetime', CapturedClock):
                signal = engine.score_temperature_market_with_context(question, .5)
            assert signal is not None
            sources = PredictionLab._weather_snapshot_sources(signal['data'],
                settlement_source='unknown', market_date='2026-10-19', as_of=now.isoformat(),
                station_resolution={}, source_agreement=None)
            mid = f"KX{'HIGH' if kind == 'high' else 'LOW'}TEST-26OCT19-T85"
            row = dict(market_id=mid, question=question, observed_at='2026-10-19T03:00:00Z',
                yes_price=.5, no_price=.5, decision_artifact={'source_context': {
                    'source': 'captured_weather', 'as_of': now.isoformat(), 'data': {
                        'market_metadata': {'city':source['city'], 'city_id':source['city_id'],
                            'market_kind':kind, 'contract_shape':'tail', 'event_ticker':mid.rsplit('-',1)[0]},
                        'weather_source_snapshot': {'market_id':mid, 'question':question,
                            'market_date':'2026-10-19', 'station_resolution':{'city':source['city']},
                            'sources':sources, 'source_signal':signal,
                            'collection_receipt':signal['data']['collection_receipt']}}}})
            assert requested == [source['url']], requested
            yield row, store, body, signal
        finally:
            engine.close()


def replay_snapshot(record):
    return record['source_inputs']['source_context']['data']['weather_source_snapshot']


class WeatherCaptureReplayTests(unittest.TestCase):
    def project(self, row):
        built = build_replay_decision_input_v1(row, source_history_only=True)
        self.assertTrue(built.ok, built.errors)
        record = json.loads(json.dumps(built.record, allow_nan=False))
        self.assertTrue(verify_replay_decision_input_record_v1(record))
        return replay_snapshot(record)

    def test_compact_global_attempt_context_survives_sealed_projection(self):
        with captured_replay_row() as (row, _, _, _):
            metadata = next(s for s in row['decision_artifact']['source_context']['data']['weather_source_snapshot']['sources']
                            if s['source_id'] == CAPTURES[0]['source_id'])['source_metadata']
            metadata['adapter_error_category'] = 'transport_failure'
            metadata['source_reported_location'] = {'city': 'Austin', 'displayName': 'Austin, MN', 'state': 'MN'}
            attempt = metadata['collection_attempts'][0]
            attempt.update(attempt_status='transport_failure', request_parameters={
                'latitude': 30.27, 'timezone': 'auto', 'api_key': 'not projected'})
            projected = next(s for s in self.project(row)['sources'] if s['source_id'] == CAPTURES[0]['source_id'])['source_metadata']
            self.assertEqual(projected['adapter_error_category'], 'transport_failure')
            self.assertEqual(projected['collection_attempts'][0]['attempt_status'], 'transport_failure')
            self.assertEqual(projected['collection_attempts'][0]['request_parameters'],
                             {'latitude': 30.27, 'timezone': 'auto'})
            self.assertEqual(projected['source_reported_location']['displayName'], 'Austin, MN')

    def test_actual_eligible_and_collection_only_capture_references_round_trip(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable), captured_replay_row(unavailable=unavailable) as (row, store, body, signal):
                snap = self.project(row)
                source = next(s for s in snap['sources'] if s['source_id'] == CAPTURES[0]['source_id'])
                original = next(s for s in signal['data']['source_details'] if s['source_id'] == source['source_id'])
                metadata = source['source_metadata']
                self.assertIn('capture', metadata)
                self.assertEqual(store.read_verified(metadata['capture']), body)
                self.assertEqual(metadata['capture'], original['source_metadata']['capture'])
                self.assertEqual(metadata['collection_attempts'], original['source_metadata']['collection_attempts'])
                for receipt in (snap['collection_receipt'], snap['source_signal']['data']['collection_receipt']):
                    collection = json.loads(store.read_receipt_verified(receipt))
                    self.assertEqual(collection['kind'], 'weather_collection')
                    self.assertEqual(collection['city'].lower(), CAPTURES[0]['city'].lower())
                self.assertEqual(source['scoreable_forecast'], not unavailable)
                if unavailable:
                    self.assertIs(snap['source_signal']['collection_only'], True)
                    self.assertIs(snap['source_signal']['prediction_available'], False)
                    self.assertNotIn('predicted_prob', snap['source_signal'])
                    self.assertEqual(snap['source_signal']['availability_reason'], 'no_eligible_forecast_sources')
                else:
                    for field in ('source_clock_basis', 'source_clock_eligibility', 'source_cache_updated_at_ms',
                                  'source_reported_location', 'market_station_association', 'period_semantics',
                                  'date_mapping_basis', 'full_day_measurement_equivalent', 'parser_payload_sha256'):
                        if field in original['source_metadata']:
                            self.assertEqual(metadata[field], original['source_metadata'][field], field)
                    self.assertEqual(metadata['forecast_location']['coordinate_basis'], 'source_reported_unverified')
                    self.assertNotIn('latitude', metadata['forecast_location'])

    def test_actual_lab_snapshot_row_replays_collection_only_and_eligible_evidence(self):
        from types import SimpleNamespace
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable), captured_replay_row(unavailable=unavailable) as (raw, store, body, signal):
                lab = PredictionLab.__new__(PredictionLab)
                lab.config = {}
                lab.collector_interval_seconds = 900
                lab.observer_mode = True
                lab.mode = 'collector'
                lab.paper_lab_mode = 'opportunity'
                lab.hypothetical_mode = 'flat'
                lab.opportunity_bankroll_usd = 100
                lab.fresh_wallet_bankroll_usd = 100
                metadata = raw['decision_artifact']['source_context']['data']['market_metadata']
                market = SimpleNamespace(id=raw['market_id'], question=raw['question'], metadata=metadata)
                strategy_signal = {'direction':'SKIP', 'signal_details':{'live':signal},
                                   'question':raw['question'], 'market_id':raw['market_id']}
                artifact = {'strategy_signal':strategy_signal, 'final_action':'SKIP'}
                lab._attach_weather_source_snapshot(artifact, market)
                row = lab._build_market_snapshot_row('capture-replay-test', market, strategy_signal,
                    decision_type='skip', prediction_recorded=False, decision_artifact=artifact,
                    observed_at=raw['observed_at'])
                snap = self.project(json.loads(json.dumps(row, allow_nan=False)))
                for sources in (snap['sources'], snap['source_signal']['data']['source_details']):
                    source = next(s for s in sources if s['source_id'] == CAPTURES[0]['source_id'])
                    self.assertIn('capture', source['source_metadata'])
                    self.assertEqual(store.read_verified(source['source_metadata']['capture']), body)
                self.assertEqual(snap['collection_receipt'], signal['data']['collection_receipt'])
                store.read_receipt_verified(snap['collection_receipt'])
                if unavailable:
                    self.assertTrue(snap['collection_only'])
                    self.assertIs(snap['prediction_available'], False)
                    self.assertTrue(snap['source_signal']['collection_only'])
                    self.assertNotIn('predicted_prob', snap['source_signal'])

    def test_strict_envelope_accepts_exact_real_projected_capture_references(self):
        from tests.test_replay_decision_input import _snapshot_row
        with captured_replay_row() as (raw, store, body, _):
            sealed = self.project(raw)
            # Only execution/policy/index metadata is synthetic; the entire
            # source snapshot is the actual producer's sealed projection.
            strict_row = _snapshot_row()
            for key in ('market_id', 'question', 'observed_at', 'yes_price', 'no_price'):
                strict_row[key] = raw[key]
            strict_row['decision_artifact']['source_context']['data']['weather_source_snapshot'] = sealed
            built = build_replay_decision_input_v1(strict_row, strict=True)
            self.assertTrue(built.ok, built.errors)
            assert built.record is not None
            source = next(s for s in replay_snapshot(built.record)['sources'] if s['source_id'] == CAPTURES[0]['source_id'])
            self.assertIn('capture', source['source_metadata'])
            self.assertEqual(store.read_verified(source['source_metadata']['capture']), body)
            source_metadata = next(s for s in sealed['sources'] if s['source_id'] == CAPTURES[0]['source_id'])['source_metadata']
            source_metadata['capture']['request_parameters']['authorization'] = 'SECRET'
            invalid = build_replay_decision_input_v1(strict_row, strict=True)
            self.assertFalse(invalid.ok)
            self.assertTrue(any(e.code == 'unknown_unallowlisted_input_field' and e.path.endswith('.authorization') for e in invalid.errors))

    def test_independent_attempt_receipts_share_only_the_blob(self):
        with captured_replay_row() as (first, store1, body, _), captured_replay_row(capture_store=store1) as (second, store2, _, _):
            refs = []
            for row, store in ((first, store1), (second, store2)):
                source = next(s for s in self.project(row)['sources'] if s['source_id'] == CAPTURES[0]['source_id'])
                self.assertIn('capture', source['source_metadata'])
                ref = source['source_metadata']['capture']
                self.assertEqual(store.read_verified(ref), body)
                refs.append(ref)
            self.assertEqual(refs[0]['raw_payload_locator'], refs[1]['raw_payload_locator'])
            self.assertNotEqual(refs[0]['capture_receipt_locator'], refs[1]['capture_receipt_locator'])
            self.assertIs(store1, store2)
            self.assertEqual(len(list((store1.storage_root / 'data/weather_captures/blobs').glob('*/*.bin'))), 1)
            a = self.project(first)['collection_receipt']
            b = self.project(second)['collection_receipt']
            self.assertNotEqual(a['receipt_locator'], b['receipt_locator'])
            store1.read_receipt_verified(a)
            store1.read_receipt_verified(b)

    def test_nested_unknown_raw_outcomes_and_credentials_are_not_projected(self):
        with captured_replay_row() as (row, _, _, _):
            metadata = next(s for s in row['decision_artifact']['source_context']['data']['weather_source_snapshot']['sources']
                                if s['source_id'] == CAPTURES[0]['source_id'])['source_metadata']
            metadata.update(payload_text='RAW_BODY', raw_forecast={'winner':'YES'},
                            provenance={'future_outcome':'YES', 'authorization':'SECRET'})
            metadata['capture'].update(payload_base64='RAW_BODY', headers={'Authorization':'SECRET'},
                outcome='YES', unknown={'forecast_high':999},
                request_parameters={'latitude':33.7, 'longitude':-84.4, 'timezone':'auto',
                                    'api_key':'SECRET', 'future_pnl':100, 'hourly':{'winner':'YES'}})
            snap = self.project(row)
            metadata = next(s for s in snap['sources'] if s['source_id'] == CAPTURES[0]['source_id'])['source_metadata']
            self.assertNotIn('payload_text', metadata)
            self.assertNotIn('raw_forecast', metadata)
            self.assertNotIn('provenance', metadata)
            self.assertIn('capture', metadata)
            ref = metadata['capture']
            self.assertEqual(ref['request_parameters'], {'latitude':33.7, 'longitude':-84.4, 'timezone':'auto'})
            self.assertFalse({'headers','payload_base64','outcome','unknown'} & ref.keys())
            strict = build_replay_decision_input_v1(row, strict=True)
            self.assertFalse(strict.ok)
            self.assertTrue(any(e.code == 'forbidden_outcome_or_future_field' for e in strict.errors))

    def test_capture_and_attempt_urls_cannot_carry_credentials(self):
        for url in ('https://user:SECRET@weather.example/forecast', 'https://weather.example/forecast?api_key=SECRET'):
            with self.subTest(url=url), captured_replay_row() as (row, _, _, _):
                metadata = next(s for s in row['decision_artifact']['source_context']['data']['weather_source_snapshot']['sources']
                                if s['source_id'] == CAPTURES[0]['source_id'])['source_metadata']
                metadata['capture']['retrieval_url'] = url
                metadata['collection_attempts'][0]['url'] = url
                projected = next(s for s in self.project(row)['sources'] if s['source_id'] == CAPTURES[0]['source_id'])['source_metadata']
                self.assertIn('capture', projected)
                self.assertNotIn('retrieval_url', projected['capture'])
                self.assertNotIn('url', projected['collection_attempts'][0])
