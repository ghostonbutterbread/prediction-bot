"""Synthetic immutable index -> real direct history identity regressions."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from bot.collector_replay_index import build_collector_replay_index
from bot.weather.source_router_direct_predictive import load_direct_predictive_source_history
from bot.weather.source_router_direct_history import load_direct_strict_source_history
from bot.weather.source_observation_ledger import _decision_identity, _pending_rows_for_input
from bot.replay_decision_input import build_replay_decision_input_v1, verify_replay_decision_input_record_v1
from test_contextual_predictive_source_history import predictor_input, resolution_for
from test_source_observation_ledger import _seal, _write_jsonl


def bad_labels_record(*, full_day=False):
    row = predictor_input()
    row['market']['market_metadata'].update(city='Austin', city_id='austin_tx')
    snapshot = row['source_inputs']['source_context']['data']['weather_source_snapshot']
    snapshot['station_resolution'] = {'city': 'Austin', 'city_id': 'austin_tx'}
    if full_day:
        for source in snapshot['sources']:
            source['forecast_start'] = source['target_mapping']['source_period_start'] = '2026-08-03T00:00:00-07:00'
            source['forecast_end'] = source['target_mapping']['source_period_end'] = '2026-08-04T00:00:00-07:00'
            source['forecast_type'] = 'daily_high'
    return _seal(row)


def raw_from_input(row):
    return {'observed_at': row['observed_at'], 'market_id': row['market_id'],
            'question': row['market']['question'], 'shared_snapshot_id': 'synthetic-snapshot',
            'decision_artifact': {'source_context': {'source': 'captured_weather', 'data': {
                'market_metadata': row['market']['market_metadata'],
                'weather_source_snapshot': row['source_inputs']['source_context']['data']['weather_source_snapshot']}}}}


class WeatherIdentityReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def history(self, row, *, strict=False):
        raw = raw_from_input(row)
        directory = self.root / ('strict' if strict else 'predictive')
        directory.mkdir(exist_ok=True)
        self.archive = directory / 'raw.jsonl'
        self.index = directory / 'index.jsonl'
        self.manifest = directory / 'manifest.json'
        self.resolutions = directory / 'resolutions.jsonl'
        _write_jsonl(self.archive, [raw])
        _write_jsonl(self.resolutions, [resolution_for(row)])
        build_collector_replay_index(self.archive, self.index, self.manifest)
        before = {path: path.read_bytes() for path in (self.archive, self.index, self.manifest, self.resolutions)}
        loader = load_direct_strict_source_history if strict else load_direct_predictive_source_history
        result = loader(index_path=self.index, manifest_path=self.manifest,
            strict_resolutions_path=self.resolutions, as_of_decision_time='2026-10-18T19:00:01+00:00')
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        return result

    def test_explicit_contradictory_response_or_invalid_coordinates_are_not_qualified(self):
        for metadata in (
            {'response_latitude': 30.2672, 'response_longitude': -97.7431},
            {'source_reported_location': {'city': 'Seattle', 'latitude': 'bad', 'longitude': -122.3}},
            {'capture': {'request_parameters': {'latitude': 'bad', 'longitude': -122.3}}},
        ):
            with self.subTest(metadata=metadata):
                row = bad_labels_record()
                source = row['source_inputs']['source_context']['data']['weather_source_snapshot']['sources'][0]
                source['source_metadata'] = metadata
                row = _seal(row)
                pending = _pending_rows_for_input(row, _decision_identity(row))
                bad = next(r for r in pending if r['source_id'] == 'nws')
                self.assertNotEqual(bad['predictive_source_proof']['status'], 'eligible_predictive_source_proof')

    def test_case_and_registry_aliases_are_normalization_not_wrong_city_repairs(self):
        row = predictor_input()
        row['market']['market_metadata'].update(city='seattle', city_id='seattle_wa')
        row['source_inputs']['source_context']['data']['weather_source_snapshot']['station_resolution'] = {
            'city': 'SEATTLE', 'city_id': 'seattle_wa'}
        row = _seal(row)
        pending = _pending_rows_for_input(row, _decision_identity(row))
        for observation in pending:
            receipt = observation['weather_identity_reconciliation']
            self.assertFalse(receipt['market_city_label_repaired'])
            self.assertTrue(receipt['market_city_label_normalized'])

    def test_retained_transport_url_geography_overrides_an_agreeing_derived_label(self):
        row = bad_labels_record()
        source = row['source_inputs']['source_context']['data']['weather_source_snapshot']['sources'][0]
        source['source_metadata'] = {'capture': {'retrieval_url':
            'https://api.open-meteo.com/v1/forecast?latitude=30.2672&longitude=-97.7431'}}
        row = _seal(row)
        pending = _pending_rows_for_input(row, _decision_identity(row))
        bad = next(r for r in pending if r['source_id'] == 'nws')
        self.assertIn('source_query_city_conflicts_with_original_market', bad['strict_source_proof']['reasons'])

    def test_original_explicit_question_supports_legacy_ids_but_not_conflicting_city(self):
        row = bad_labels_record()
        row['market_id'] += '-ALT'
        row['decision_key']['market_id'] = row['market_id']
        row['market']['market_metadata'].pop('event_ticker')
        row = _seal(row)
        pending = _pending_rows_for_input(row, _decision_identity(row))
        self.assertTrue(all(r['predictive_source_proof']['status'] == 'eligible_predictive_source_proof' for r in pending))
        self.assertTrue(all(r['market_id'] == row['market_id'] for r in pending))
        row['market_id'] = row['decision_key']['market_id'] = 'KXHIGHAUS-26AUG03-T70'
        row = _seal(row)
        pending = _pending_rows_for_input(row, _decision_identity(row))
        self.assertTrue(all(r['predictive_source_proof']['status'] != 'eligible_predictive_source_proof' for r in pending))

    def test_provider_city_cannot_salvage_unproven_or_conflicting_query_geography(self):
        for metadata in (
            {'source_reported_location': {'city': 'Seattle'},
             'capture': {'request_parameters': {'latitude': 0, 'longitude': 0}}},
            {'source_reported_location': {'city': 'Seattle'},
             'forecast_location': {'city': 'Unknown query city'}},
            {'source_reported_location': {'city': 'Austin'}},
        ):
            with self.subTest(metadata=metadata):
                row = bad_labels_record()
                sources = row['source_inputs']['source_context']['data']['weather_source_snapshot']['sources']
                sources[0].update(source_location_city='Austin', source_metadata=metadata)
                sealed = _seal(row)
                pending = _pending_rows_for_input(sealed, _decision_identity(sealed))
                bad = next(r for r in pending if r['source_id'] == 'nws')
                self.assertNotEqual(bad['predictive_source_proof']['status'], 'eligible_predictive_source_proof')
                good = next(r for r in pending if r['source_id'] == 'seattle_local')
                self.assertEqual(good['predictive_source_proof']['status'], 'eligible_predictive_source_proof')

    def test_derived_market_kind_cannot_override_original_high_contract(self):
        row = bad_labels_record()
        row['market']['market_metadata']['market_kind'] = 'low'
        row = _seal(row)
        pending = _pending_rows_for_input(row, _decision_identity(row))
        self.assertEqual({r['market_kind'] for r in pending}, {'high'})
        self.assertTrue(all(r['predictive_source_proof']['status'] == 'eligible_predictive_source_proof' for r in pending))

    def test_official_city_survives_sanitizer_without_unreviewed_custom_fields(self):
        raw = raw_from_input(bad_labels_record())
        raw['market_id'] = 'KXHIGHNEWCITY-26AUG03-T70'
        raw['question'] = 'Will the maximum temperature be above 70°?'
        data = raw['decision_artifact']['source_context']['data']
        data['market_metadata'] = {'city': 'Austin', 'city_id': 'austin_tx',
                                   'custom_strike': {'City': 'Seattle', 'outcome': 'YES'}}
        built = build_replay_decision_input_v1(raw, source_history_only=True)
        self.assertTrue(built.ok, built.errors)
        self.assertEqual(built.record['market']['market_metadata']['custom_strike'], {'City': 'Seattle'})
        pending = _pending_rows_for_input(built.record, _decision_identity(built.record))
        self.assertEqual({r['city_id'] for r in pending}, {'seattle_wa'})
        self.assertTrue(all(r['predictive_source_proof']['status'] == 'eligible_predictive_source_proof' for r in pending))

    def test_wrong_derived_source_label_is_salvaged_only_with_provider_geography(self):
        row = bad_labels_record()
        source = row['source_inputs']['source_context']['data']['weather_source_snapshot']['sources'][0]
        source['source_location_city'] = 'Austin'
        source['source_metadata'] = {'source_reported_location': {'city': 'Seattle'}}
        row = _seal(row)
        original = copy.deepcopy(row)
        result = self.history(row)
        self.assertEqual({r['source_id'] for r in result.rows}, {'nws', 'seattle_local'})
        pending = _pending_rows_for_input(row, _decision_identity(row))
        repaired = next(r for r in pending if r['source_id'] == 'nws')
        digest = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(repaired['source_provenance']['source_record_sha256'], digest)
        self.assertNotEqual(repaired['source_provenance']['projected_source_record_sha256'], digest)
        self.assertEqual(repaired['weather_identity_reconciliation']['status'], 'repaired_source_location_label')
        self.assertEqual(row, original)
        self.assertEqual(repaired['source_as_of'], source['source_as_of'])
        self.assertEqual(repaired['forecast_temp_f'], source['forecast_high'])
        self.assertTrue(verify_replay_decision_input_record_v1(row))

    def test_wrong_actual_query_is_rejected_per_source_even_when_labels_agree(self):
        row = bad_labels_record(full_day=True)
        sources = row['source_inputs']['source_context']['data']['weather_source_snapshot']['sources']
        sources[0]['source_metadata'] = {
            'forecast_location': {'city': 'Austin', 'latitude': 30.2672, 'longitude': -97.7431},
            'capture': {'retrieval_url': 'https://api.open-meteo.com/v1/forecast',
                        'request_parameters': {'latitude': 30.2672, 'longitude': -97.7431}}}
        row = _seal(row)
        result = self.history(row)
        self.assertEqual({r['source_id'] for r in result.rows}, {'seattle_local'})
        pending = _pending_rows_for_input(row, _decision_identity(row))
        bad = next(r for r in pending if r['source_id'] == 'nws')
        self.assertIn('source_query_city_conflicts_with_original_market', bad['strict_source_proof']['reasons'])
        self.assertEqual(bad['city_id'], 'seattle_wa')
        strict = self.history(row, strict=True)
        self.assertEqual({r['source_id'] for r in strict.scorecard_rows}, {'seattle_local'})

    def test_direct_reader_salvages_correct_source_despite_wrong_market_labels(self):
        row = bad_labels_record()
        original = copy.deepcopy(row)
        result = self.history(row)
        self.assertEqual(len(result.rows), 2)
        self.assertEqual({r['city_id'] for r in result.rows}, {'seattle_wa'})
        self.assertEqual({(r['source_id'], r['forecast_temp_f']) for r in result.rows}, {('nws', 75.), ('seattle_local', 65.)})
        self.assertTrue(all(not r['eligible_for_source_history'] for r in result.rows))
        self.assertEqual(row, original)
        self.assertTrue(verify_replay_decision_input_record_v1(row))


if __name__ == '__main__':
    unittest.main()
