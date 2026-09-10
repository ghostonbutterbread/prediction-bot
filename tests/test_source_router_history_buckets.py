import copy
import tempfile
import unittest
from pathlib import Path

from bot.collector_replay_index import build_collector_replay_index
from bot.weather.source_router_direct_history import load_direct_strict_source_history
from test_auto_source_router_promotion import _collector_row, _strict_resolution, _write_jsonl


def snapshot(day, poll=0):
    row = _collector_row(market_id=f'KXHIGHSEA-26AUG{day:02}-T70')
    date = f'2026-08-{day:02}'
    row['shared_snapshot_id'] = f'snapshot-{day}-{poll}'
    row['observed_at'] = f'{date}T12:{poll:02}:00+00:00'
    weather = row['decision_artifact']['source_context']['data']['weather_source_snapshot']
    weather['market_date'] = date
    source = weather['sources'][0]
    source['source_as_of'] = f'{date}T11:54:00+00:00'
    source['target_mapping'].update(market_target_date=date, source_target_date=date)
    return row


class DistinctHistoryBucketTests(unittest.TestCase):
    def test_lane_uses_named_per_bucket_event_limit(self):
        from bot.paper_shadow_lanes import _LaneDefinition, _direct_source_history_parameters
        lane = _LaneDefinition('shadow_source_router', parameters={
            'collector_replay_index_path': '/index',
            'collector_replay_manifest_path': '/manifest',
            'strict_resolutions_path': '/resolutions',
            'history_events_per_bucket': 3,
        })
        self.assertEqual(_direct_source_history_parameters(lane)['accepted_limit'], 3)

    def test_conflicting_limit_aliases_are_rejected(self):
        from bot.paper_shadow_lanes import _LaneDefinition, _direct_source_history_parameters
        lane = _LaneDefinition('shadow_source_router', parameters={
            'collector_replay_index_path': '/index',
            'collector_replay_manifest_path': '/manifest',
            'strict_resolutions_path': '/resolutions',
            'history_events_per_bucket': 3, 'history_row_limit': 4,
        })
        with self.assertRaises(ValueError):
            _direct_source_history_parameters(lane)

    def run_history(self, rows, limit=2):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw, resolutions = root/'raw.jsonl', root/'resolutions.jsonl'
            _write_jsonl(raw, rows)
            _write_jsonl(resolutions, [_strict_resolution(m) for m in sorted({r['market_id'] for r in rows})])
            index, manifest = root/'index.jsonl', root/'manifest.json'
            build_collector_replay_index(raw, index, manifest)
            return load_direct_strict_source_history(
                index_path=index, manifest_path=manifest,
                strict_resolutions_path=resolutions, accepted_limit=limit,
                as_of_decision_time='2026-08-08T00:00:00+00:00',
            )

    def test_reports_per_bucket_history_shortfall(self):
        result = self.run_history([snapshot(1)], limit=3)
        self.assertTrue(hasattr(result, 'bucket_coverage'), 'missing per-bucket coverage')
        [coverage] = result.bucket_coverage
        self.assertEqual(coverage['selected_units'], 1)
        self.assertEqual(coverage['requested_units'], 3)
        self.assertEqual(coverage['shortfall'], 2)
        self.assertEqual(coverage['source_id'], 'nws')

    def test_display_name_changes_preserve_combined_lookup_evidence(self):
        from bot.weather.source_reliability import SourceReliabilityTable
        from bot.weather.source_scoreboard import MarketContext, SourceForecastObservation

        observation = SourceForecastObservation(
            source_id='nws', source_name='NWS', forecast_temp_f=75.0,
            actual_temp_f=None,
            market=MarketContext(city_id='seattle_wa', market_kind='high', contract_shape='tail'),
        )
        for names in (('NWS', 'National Weather Service'),
                      ('National Weather Service', 'NWS'), ('NWS', 'NWS')):
            with self.subTest(names=names):
                rows = [snapshot(day) for day in (1, 2, 3)]
                for row, name in zip(rows, ('AAA outside budget', *names)):
                    source = row['decision_artifact']['source_context']['data']['weather_source_snapshot']['sources'][0]
                    source['source_name'] = name
                # Selected days 2 and 3 contain one incorrect and one correct
                # forecast. Day 1 is older and must not enter via its name.
                rows[1]['decision_artifact']['source_context']['data']['weather_source_snapshot']['sources'][0]['forecast_high'] = 65.0
                result = self.run_history(rows)
                [coverage] = result.bucket_coverage
                self.assertEqual(coverage['selected_units'], 2)
                self.assertEqual(coverage['earliest_source_as_of'][:10], '2026-08-02')
                self.assertEqual(coverage['latest_source_as_of'][:10], '2026-08-03')
                stats = SourceReliabilityTable(result.scorecard_rows).lookup(observation)
                self.assertIsNotNone(stats)
                self.assertEqual(stats.sample_count, 2)
                self.assertEqual(stats.direction_accuracy, 0.5)
                [scorecard] = result.scorecard_rows
                self.assertEqual(scorecard['source_name'], 'NWS')
                self.assertEqual(scorecard['threshold_correct_count'], 1)
                self.assertEqual(len(scorecard['provenance']['settled_observations']), 2)

    def test_each_source_gets_its_own_event_budget(self):
        rows = [snapshot(day) for day in (1, 2, 3)]
        for row in rows:
            weather = row['decision_artifact']['source_context']['data']['weather_source_snapshot']
            other = copy.deepcopy(weather['sources'][0])
            other.update(source_id='open_meteo', source_name='Open-Meteo')
            weather['sources'].append(other)
        result = self.run_history(rows)
        self.assertEqual(len(result.scorecard_rows), 2)
        self.assertEqual([r['sample_count'] for r in result.scorecard_rows], [2, 2])
        self.assertEqual(len(result.bucket_coverage), 2)

    def test_correlated_contracts_share_an_event_unit(self):
        first = snapshot(3)
        second = copy.deepcopy(first)
        second['market_id'] = second['market_id'].replace('T70', 'T71')
        second['shared_snapshot_id'] += '-other-contract'
        result = self.run_history([snapshot(2), first, second])
        self.assertEqual(result.scorecard_rows[0]['sample_count'], 2)
        self.assertEqual(result.bucket_coverage[0]['event_units'], 2)

    def test_optional_event_metadata_does_not_split_one_weather_event(self):
        first = snapshot(3)
        second = copy.deepcopy(first)
        second['shared_snapshot_id'] += '-without-event'
        second['decision_artifact']['source_context']['data']['market_metadata'].pop('event_ticker', None)
        result = self.run_history([snapshot(2), first, second])
        self.assertEqual(result.scorecard_rows[0]['sample_count'], 2)
        self.assertEqual(result.bucket_coverage[0]['earliest_source_as_of'][:10], '2026-08-02')

    def test_invalid_limits_are_rejected(self):
        for limit in (True, 1.5, '2', 0, -1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.run_history([snapshot(1)], limit=limit)

    def test_repeat_polls_do_not_exhaust_distinct_history_budget(self):
        rows = [snapshot(1), snapshot(2)] + [snapshot(3, poll) for poll in range(5)]
        result = self.run_history(rows)
        self.assertEqual(len(result.scorecard_rows), 1)
        self.assertEqual(result.scorecard_rows[0]['sample_count'], 2)
