"""Original daily HIGH/LOW conflicts cannot become qualified repaired history."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bot.collector_replay_index import build_collector_replay_index
from bot.replay_decision_input import build_replay_decision_input_v1
from bot.weather.market_mapping import resolve_weather_measurement_kind
from bot.weather.source_observation_ledger import _decision_identity, _pending_rows_for_input
from bot.weather.source_router_direct_history import load_direct_strict_source_history
from bot.weather.source_router_direct_predictive import load_direct_predictive_source_history
from test_contextual_predictive_source_history import predictor_input, resolution_for
from test_source_observation_ledger import _write_jsonl
from test_weather_identity_reconciliation import bad_labels_record, raw_from_input

ROOT = Path(__file__).resolve().parents[1]
REASON = 'unproven_or_conflicting_original_market_measurement_kind'


class HistoricalMeasurementIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def exercise(self, raw):
        inputs = self.root / 'evidence'
        inputs.mkdir()
        archive, index, manifest, outcomes = (inputs / name for name in (
            'raw.jsonl', 'index.jsonl', 'manifest.json', 'outcomes.jsonl'))
        built = build_replay_decision_input_v1(raw, source_history_only=True)
        self.assertTrue(built.ok, built.errors)
        assert built.record is not None
        _write_jsonl(archive, [raw])
        _write_jsonl(outcomes, [resolution_for(built.record)])
        build_collector_replay_index(archive, index, manifest)
        paths = (archive, index, manifest, outcomes)
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        predictive = load_direct_predictive_source_history(
            index_path=index, manifest_path=manifest, strict_resolutions_path=outcomes,
            as_of_decision_time='2026-10-18T19:00:01+00:00')
        strict = load_direct_strict_source_history(
            index_path=index, manifest_path=manifest, strict_resolutions_path=outcomes,
            as_of_decision_time='2026-10-18T19:00:01+00:00')
        output = self.root / 'data' / 'derived_reports' / 'audit'
        run = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/weather_identity_reconcile.py'),
            '--index', str(index), '--manifest', str(manifest), '--output-dir', str(output)],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads((output / 'report.json').read_text())
        identity = _decision_identity(built.record)
        assert identity is not None
        pending = _pending_rows_for_input(built.record, identity)
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})
        return {
            'predictive': sorted(row['source_id'] for row in predictive.rows),
            'strict': sorted(row['source_id'] for row in strict.scorecard_rows),
            'cli_predictive': report['counts'].get('predictive_proof_eligible', 0),
            'cli_strict': report['counts'].get('strict_proof_eligible', 0),
        }, pending, report

    def assert_conflict_rejected(self, raw):
        got, pending, report = self.exercise(raw)
        self.assertEqual(got, {'predictive': [], 'strict': [], 'cli_predictive': 0, 'cli_strict': 0})
        self.assertEqual(len(pending), 2)
        for observation in pending:
            self.assertIn(REASON, observation['strict_source_proof']['reasons'])
            self.assertNotEqual(observation['predictive_source_proof']['status'],
                                'eligible_predictive_source_proof')
        self.assertEqual(report['strict_rejection_counts'][REASON], 2)

    def test_high_id_conflicting_with_original_low_question_rejects_in_all_consumers(self):
        raw = raw_from_input(bad_labels_record(full_day=True))
        raw['question'] = 'Will Seattle low temperature be above 70°?'
        self.assert_conflict_rejected(raw)

    def test_high_id_conflicting_with_official_minimum_rule_rejects_in_all_consumers(self):
        raw = raw_from_input(bad_labels_record(full_day=True))
        raw['decision_artifact']['source_context']['data']['market_metadata']['rules_primary'] = (
            'If the minimum temperature recorded at Seattle (KSEA) on August 3, 2026 is above 70 degrees.')
        self.assert_conflict_rejected(raw)

    def test_contradictory_original_event_kind_is_not_ignored(self):
        raw = raw_from_input(bad_labels_record(full_day=True))
        raw['decision_artifact']['source_context']['data']['market_metadata']['event_ticker'] = 'KXLOWSEA-26AUG03'
        self.assert_conflict_rejected(raw)

    def test_correct_original_high_identity_still_repairs_derived_city(self):
        raw = raw_from_input(bad_labels_record(full_day=True))
        raw['decision_artifact']['source_context']['data']['market_metadata']['market_kind'] = 'low'
        got, pending, _ = self.exercise(raw)
        self.assertEqual(got, {'predictive': ['nws', 'seattle_local'],
                              'strict': ['nws', 'seattle_local'], 'cli_predictive': 2, 'cli_strict': 2})
        self.assertTrue(all(REASON not in row['strict_source_proof']['reasons'] for row in pending))

    def test_secondary_maximum_minimum_template_preserves_high_history(self):
        raw = raw_from_input(bad_labels_record(full_day=True))
        raw['decision_artifact']['source_context']['data']['market_metadata']['rules_secondary'] = (
            'The official and final value used to determine this market is the '
            'maximum/minimum temperature as reported by the Weather Company.')
        got, pending, _ = self.exercise(raw)
        self.assertEqual(got, {'predictive': ['nws', 'seattle_local'],
                              'strict': ['nws', 'seattle_local'], 'cli_predictive': 2, 'cli_strict': 2})
        self.assertTrue(all(REASON not in row['strict_source_proof']['reasons'] for row in pending))

    def low_raw(self, *, minimum_series=False):
        row = predictor_input(kind='low')
        if minimum_series:
            row['market_id'] = row['market_id'].replace('KXLOWSEA', 'KXMINTEMPSEA')
            row['market']['question'] = 'Will Seattle temperature be below 70°?'
        metadata = row['market']['market_metadata']
        metadata.update(event_ticker=row['market_id'].rsplit('-', 1)[0],
                        market_kind='high' if not minimum_series else 'unknown')
        snapshot = row['source_inputs']['source_context']['data']['weather_source_snapshot']
        for source in snapshot['sources']:
            source['forecast_start'] = source['target_mapping']['source_period_start'] = '2026-08-03T00:00:00-07:00'
            source['forecast_end'] = source['target_mapping']['source_period_end'] = '2026-08-04T00:00:00-07:00'
            source['forecast_type'] = 'daily_low'
        return raw_from_input(row)

    def test_original_low_identity_overrides_a_derived_high_label_in_all_consumers(self):
        got, pending, _ = self.exercise(self.low_raw())
        self.assertEqual(got, {'predictive': ['nws', 'seattle_local'],
                              'strict': ['nws', 'seattle_local'], 'cli_predictive': 2, 'cli_strict': 2})
        self.assertEqual({row['market_kind'] for row in pending}, {'low'})

    def test_secondary_maximum_minimum_template_preserves_low_history(self):
        raw = self.low_raw()
        raw['decision_artifact']['source_context']['data']['market_metadata']['rules_secondary'] = (
            'The official and final value used to determine this market is the '
            'maximum/minimum temperature as reported by the Weather Company.')
        got, pending, _ = self.exercise(raw)
        self.assertEqual(got, {'predictive': ['nws', 'seattle_local'],
                              'strict': ['nws', 'seattle_local'], 'cli_predictive': 2, 'cli_strict': 2})
        self.assertEqual({row['market_kind'] for row in pending}, {'low'})

    def test_secondary_pair_cannot_hide_a_specific_contradictory_minimum_rule(self):
        raw = raw_from_input(bad_labels_record(full_day=True))
        raw['decision_artifact']['source_context']['data']['market_metadata']['rules_secondary'] = (
            'The maximum/minimum temperature is reported by the Weather Company. '
            'This contract settles using the minimum temperature recorded at Seattle.')
        self.assert_conflict_rejected(raw)

    def test_secondary_pair_is_neutral_not_an_original_kind_or_primary_rule_override(self):
        pair = 'maximum/minimum temperature'
        high_id = 'KXHIGHSEA-26AUG03-T70'
        cases = (
            ('Daily temperature', '', {'rules_secondary': pair}),
            (pair, high_id, {}),
            ('Daily temperature', high_id, {'rules_primary': pair}),
            ('Daily temperature', high_id, {
                'rules_secondary': 'Both the maximum and minimum temperature determine this contract.'}),
        )
        for question, market_id, metadata in cases:
            with self.subTest(question=question, metadata=metadata):
                self.assertIsNone(resolve_weather_measurement_kind(
                    question, market_id=market_id, market_metadata=metadata))

    def test_minimum_series_generic_question_still_uses_low_forecasts(self):
        got, pending, _ = self.exercise(self.low_raw(minimum_series=True))
        self.assertEqual(got, {'predictive': ['nws', 'seattle_local'],
                              'strict': ['nws', 'seattle_local'], 'cli_predictive': 2, 'cli_strict': 2})
        self.assertEqual({row['market_kind'] for row in pending}, {'low'})


if __name__ == '__main__':
    unittest.main()
