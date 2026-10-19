"""Captured TEGNA producer forecasts stay predictive, never strict full-day.

Outcomes and knowledge/decision clocks below are SYNTHETIC label-correctness
fixtures, not evidence of predictive skill, calibration, or profit.
"""
import copy
import json
import unittest

from bot.replay_decision_input import build_replay_decision_input_v1
from bot.weather.predictive_source_history import load_predictive_source_history, select_contextual_weather_source
from bot.weather.source_observation_ledger import strict_source_observation_proof
from tests.test_weather_capture_replay import CAPTURES, captured_replay_row, replay_snapshot
from tests import test_auto_source_router_promotion as promotion_fixtures


class TegnaPredictivePipelineTests(unittest.TestCase):
    def publish(self, raw):
        fixture = promotion_fixtures.AutoSourceRouterPromotionTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        mid = raw['market_id']
        resolution = dict(market_id=mid, requested_market_id=mid, returned_market_id=mid,
            market_status='finalized', kalshi_result='yes', settlement_ts='2026-10-20T08:00:00Z',
            resolved_at='2026-10-20T09:00:00Z', retrieved_at='2026-10-20T10:00:00Z',
            resolution_id='SYNTHETIC-tegna-label-correctness')
        publication = fixture.run_pipeline([raw], [resolution])
        history = load_predictive_source_history(publication.history_path.parent / 'run_metadata.json')
        return publication, history

    def test_actual_captured_tegna_high_and_partial_low_materialize_predictively_only(self):
        for capture in CAPTURES:
            for kind in ('high', 'low'):
                with self.subTest(source=capture['source_id'], kind=kind), captured_replay_row(capture, kind=kind) as (raw, store, body, _):
                    publication, history = self.publish(raw)
                    self.assertEqual(publication.counts['predictive_eligible'], 1)
                    history = [h for h in history if h['source_id'] == capture['source_id']]
                    self.assertTrue(history)
                    self.assertTrue(all(not h['eligible_for_source_history'] for h in history))
                    eligible = [h for h in history if h['eligible_for_predictive_history']]
                    self.assertEqual(len(eligible), 1)
                    row = eligible[0]
                    self.assertFalse(row['eligible_for_source_history'], row['strict_source_proof'])
                    self.assertTrue(row['eligible_for_predictive_history'])
                    self.assertIn(f'partial_day_{kind}_proxy_not_full_day', row['strict_source_proof']['reasons'])
                    self.assertEqual(row['forecast_temp_f'], capture['expected_' + kind])
                    self.assertEqual(row['known_after'], '2026-10-20T10:00:00+00:00')
                    self.assertNotIn('profit', row)
                    built = build_replay_decision_input_v1(raw, source_history_only=True)
                    self.assertTrue(built.ok, built.errors)
                    snap = replay_snapshot(json.loads(json.dumps(built.record)))
                    source = next(s for s in snap['sources'] if s['source_id'] == capture['source_id'])
                    self.assertIn('full_day_measurement_equivalent', source['source_metadata'])
                    self.assertIs(source['source_metadata']['full_day_measurement_equivalent'], False)
                    self.assertEqual(store.read_verified(source['source_metadata']['capture']), body)
                    for clock, expected in [('2026-10-20T09:59:59Z', False), ('2026-10-20T10:00:00Z', False), ('2026-10-20T10:00:01Z', True)]:
                        current = copy.deepcopy(raw)
                        current['observed_at'] = clock
                        projected = build_replay_decision_input_v1(current, source_history_only=True)
                        self.assertTrue(projected.ok, projected.errors)
                        assert projected.record is not None
                        choice = select_contextual_weather_source(projected.record, history, min_sample_count=1)
                        self.assertIs(choice['routeable'], expected, choice)
                    assert projected.record is not None
                    default = select_contextual_weather_source(projected.record, history)
                    self.assertFalse(default['routeable'])
                    self.assertEqual(default['min_sample_count'], 30)
                    self.assertEqual(default['max_history_events'], 100)

    def test_explicit_false_at_either_retained_alias_overrides_calendar_bounds(self):
        with captured_replay_row() as (raw, _, _, _):
            snapshot = raw['decision_artifact']['source_context']['data']['weather_source_snapshot']
            original = next(s for s in snapshot['sources'] if s['source_id'] == CAPTURES[0]['source_id'])
            for where in ('source', 'metadata'):
                with self.subTest(where=where):
                    source = copy.deepcopy(original)
                    source.pop('full_day_measurement_equivalent', None)
                    source['source_metadata'].pop('full_day_measurement_equivalent', None)
                    (source if where == 'source' else source['source_metadata'])['full_day_measurement_equivalent'] = False
                    proof = strict_source_observation_proof(source_record=source, snapshot=snapshot,
                        market_metadata={'city': CAPTURES[0]['city']}, market_date='2026-10-19', market_kind='high',
                        contract_shape='tail', question_side='above', observed_at=raw['observed_at'])
                    self.assertIn('partial_day_high_proxy_not_full_day', proof['reasons'])
                    self.assertNotEqual(proof['status'], 'eligible')
