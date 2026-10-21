"""Bounded collector index -> independent outcomes -> actual contextual consumer."""
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from bot.collector_replay_index import build_collector_replay_index
from bot.weather.contextual_source_flow import select_captured_weather_source
from bot.weather.source_router_direct_predictive import load_direct_predictive_source_history
from test_contextual_predictive_source_history import predictor_input, resolution_for
from test_source_observation_ledger import _seal, _write_jsonl


class ContextualDirectDatasetTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.archive = self.root / 'snapshots.jsonl'
        self.resolutions = self.root / 'resolutions.jsonl'
        self.index = self.root / 'index.jsonl'
        self.manifest = self.root / 'index.manifest.json'
        self.current = predictor_input('2026-10-19', raw_hash='f' * 64)
        self.current['market_id'] = self.current['decision_key']['market_id'] = 'KXHIGHSEA-26OCT19-T70'
        self.current['market']['question'] = 'Will Seattle high temperature be above 70°?'
        self.current = _seal(self.current)

    def dataset(self, count=30, *, knowledge=None, duplicate=False, partial=False, equal=False, reverse=False):
        raw, outcomes = [], []
        for i in range(count):
            day = (date(2026, 8, 3) + timedelta(days=i)).isoformat()
            row = predictor_input(day, raw_hash=f'{i:064x}')
            token = date.fromisoformat(day).strftime('%y%b%d').upper()
            row['market_id'] = row['decision_key']['market_id'] = f'KXHIGHSEA-{token}-T70'
            row['market']['question'] = f'Will Seattle high temperature be above 70° on {day}?'
            snapshot = row['source_inputs']['source_context']['data']['weather_source_snapshot']
            snapshot.update(market_id=row['market_id'], question=row['market']['question'])
            for source, sid, value in zip(snapshot['sources'], ('nws', 'seattle_local'), (75., 65.)):
                source.update(source_id=sid, source_name=sid, source_location_city='Seattle',
                              forecast_high=75. if equal else value)
                if partial and sid == 'seattle_local':
                    source['forecast_type'] = 'daytime_peak'
            row = _seal(row)
            raw.append({'observed_at': row['observed_at'], 'market_id': row['market_id'],
                'question': row['market']['question'], 'shared_snapshot_id': f'snapshot-{i}',
                'decision_artifact': {'source_context': {'source': 'captured_weather', 'data': {
                    'market_metadata': row['market']['market_metadata'],
                    'weather_source_snapshot': snapshot}}}})
            outcome = resolution_for(row, official='YES' if i < 5 else 'NO',
                known=knowledge or '2026-10-18T18:00:00+00:00')
            outcome['resolution_id'] = f'independent-{i}'
            outcomes.append(outcome)
        if reverse:
            raw.reverse()  # Newest event first, oldest backfill appended last.
        if duplicate:
            raw.append(raw[0])
        _write_jsonl(self.archive, raw)
        _write_jsonl(self.resolutions, outcomes)
        build_collector_replay_index(self.archive, self.index, self.manifest)

    def config(self):
        return {'collector_replay_index_path': str(self.index),
                'collector_replay_manifest_path': str(self.manifest),
                'strict_resolutions_path': str(self.resolutions)}

    def select(self):
        snapshot = self.current['source_inputs']['source_context']['data']['weather_source_snapshot']
        return select_captured_weather_source(
            {'data': {'source_details': snapshot['sources'], 'market_target_date': '2026-10-19', 'city': 'Seattle'}},
            question=self.current['market']['question'], market_id=self.current['market_id'],
            market_metadata=self.current['market']['market_metadata'],
            observed_at='2026-10-18T19:00:01+00:00', config=self.config())

    def test_actual_consumer_selects_numeric_forecast_without_materialized_history(self):
        self.dataset(30, duplicate=True)
        history = load_direct_predictive_source_history(index_path=self.index,
            manifest_path=self.manifest, strict_resolutions_path=self.resolutions,
            as_of_decision_time='2026-10-18T19:00:01+00:00')
        self.assertEqual(len(history.rows), 60)
        self.assertEqual({row['direction_correct'] for row in history.rows if row['source_id'] == 'nws'}, {True, False})
        chosen = self.select()
        self.assertTrue(chosen['routeable'], chosen)
        self.assertEqual(chosen['chosen_source_id'], 'seattle_local')
        self.assertEqual(chosen['selected_forecast_temp_f'], 65)
        self.assertEqual(chosen['prior_sample_count'], 30)
        self.assertEqual({(s['source_id'], s['wins'], s['losses']) for s in chosen['source_scores']},
                         {('nws', 5, 25), ('seattle_local', 25, 5)})
        self.assertNotIn('history_metadata_path', self.config())

    def test_outcomes_known_at_decision_do_not_train(self):
        self.dataset(30, knowledge='2026-10-18T19:00:01+00:00')
        history = load_direct_predictive_source_history(index_path=self.index,
            manifest_path=self.manifest, strict_resolutions_path=self.resolutions,
            as_of_decision_time='2026-10-18T19:00:01+00:00')
        self.assertEqual(history.rows, [])

    def test_partial_predictors_remain_predictive_without_strict_promotion(self):
        self.dataset(30, partial=True)
        history = load_direct_predictive_source_history(index_path=self.index,
            manifest_path=self.manifest, strict_resolutions_path=self.resolutions,
            as_of_decision_time='2026-10-18T19:00:01+00:00')
        self.assertEqual(len(history.rows), 60)
        self.assertTrue(all(row['eligible_for_predictive_history'] for row in history.rows))
        self.assertTrue(any(not row['eligible_for_source_history'] for row in history.rows))

    def test_29_events_abstain_and_unverified_index_cannot_use_legacy_fallback(self):
        self.dataset(29)
        self.assertIn('insufficient_prior_history', self.select()['blockers'])
        self.manifest.write_bytes(self.manifest.read_bytes() + b'{}\n')
        selected = self.select()
        self.assertFalse(selected['routeable'])
        self.assertEqual(selected['blockers'], ['unverified_direct_predictive_history'])

    def test_equal_support_abstains_without_source_id_tiebreak(self):
        self.dataset(30, equal=True)
        selected = self.select()
        self.assertFalse(selected['routeable'])
        self.assertIn('unresolved_contextual_source_tie', selected['blockers'])

    def test_event_budget_uses_newest_dates_not_collector_append_order(self):
        self.dataset(31, reverse=True)
        history = load_direct_predictive_source_history(index_path=self.index,
            manifest_path=self.manifest, strict_resolutions_path=self.resolutions,
            accepted_limit=30, as_of_decision_time='2026-10-18T19:00:01+00:00')
        self.assertEqual(len(history.rows), 60)
        self.assertNotIn('2026-08-03', {row['market_date'] for row in history.rows})
        self.assertIn('2026-09-02', {row['market_date'] for row in history.rows})


if __name__ == '__main__':
    unittest.main()
