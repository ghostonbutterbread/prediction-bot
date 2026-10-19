"""Offline consumer tests: materialized predictors are not full-day measurements."""
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from test_source_observation_ledger import _input, _seal, _write_jsonl
from bot.weather.source_observation_ledger import materialize_source_observation_ledger


def predictor_input(day='2026-08-03', *, raw_hash='a' * 64, kind='high'):
    token = date.fromisoformat(day).strftime('%y%b%d').upper()
    record = _input(market_id=f'KX{kind.upper()}SEA-{token}-T70', raw_hash=raw_hash)
    question = f"Will Seattle {kind} temperature be {'above' if kind == 'high' else 'below'} 70°?"
    record['market']['question'] = question
    record['market']['market_metadata'].update(city='Seattle', city_id='seattle_wa', market_kind=kind)
    snapshot = record['source_inputs']['source_context']['data']['weather_source_snapshot']
    snapshot.update(question=question, market_date=day)
    start = day + 'T06:00:00-07:00' if kind == 'high' else (date.fromisoformat(day) - timedelta(days=1)).isoformat() + 'T18:00:00-07:00'
    end = day + ('T18:00:00-07:00' if kind == 'high' else 'T06:00:00-07:00')
    snapshot['sources'] = [dict(
        source_id=source, source_name=source, source_location_city='Seattle',
        forecast_measurement_kind=kind, contract_shape='tail', question_side='above' if kind == 'high' else 'below',
        **{f'forecast_{kind}': value},
        source_as_of='2026-08-01T11:54:00+00:00', source_fetched_at='2026-08-01T11:55:00+00:00',
        source_evidence_version=1, evidence_type='forecast', scoreable_forecast=True,
        forecast_start=start, forecast_end=end,
        forecast_type='daytime_period' if kind == 'high' else 'nighttime_period',
        target_mapping=dict(market_target_date=day, source_target_date=day,
                            source_period_start=start, source_period_end=end, source_timezone='America/Los_Angeles'),
    ) for source, value in [('nws', 75.), ('seattle_local', 65.)]]
    return _seal(record)


def resolution_for(record, *, official='YES', known='2026-08-07T00:00:00+00:00'):
    return dict(market_id=record['market_id'], requested_market_id=record['market_id'],
                returned_market_id=record['market_id'], market_status='finalized', kalshi_result=official.lower(),
                settlement_ts=(date.fromisoformat(record['source_inputs']['source_context']['data']['weather_source_snapshot']['market_date']) + timedelta(days=1)).isoformat() + 'T00:00:00+00:00', resolution_id='independent-resolution',
                resolution=dict(outcome=official, resolved_at=known, source='kalshi_api'))


def current_input(day='2026-10-16', observed='2026-10-15T12:00:00+00:00', *, kind='high'):
    current = predictor_input(day, kind=kind)
    current['observed_at'] = current['decision_key']['observed_at_utc'] = observed
    return _seal(current)


class PredictiveMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def materialize(self, records, *, official='YES', known='2026-08-07T00:00:00+00:00', name='derived', resolution_rows=None):
        from bot.replay_outcome_binding import bind_replay_finalized_outcomes
        inputs, resolutions = self.root / 'inputs.jsonl', self.root / 'resolutions.jsonl'
        _write_jsonl(inputs, records)
        _write_jsonl(resolutions, resolution_rows if resolution_rows is not None else [resolution_for(r, official=official, known=known) for r in records])
        # The real binder verifies independent exact requested/returned/saved IDs.
        # Its legacy projection drops resolver knowledge, so the materializer
        # must read the hash-bound original rather than substitute settlement.
        bound = bind_replay_finalized_outcomes(replay_inputs_path=inputs, strict_resolutions_path=resolutions,
            output_dir=self.root / 'data' / 'derived_reports' / (name + '-bound'), storage_root=self.root)
        result = materialize_source_observation_ledger(replay_inputs_path=inputs,
            finalized_outcomes_path=bound.outcomes_path, strict_resolutions_path=resolutions,
            output_dir=self.root / name)
        rows = [json.loads(line) for line in result.settled_path.read_text().splitlines()]
        return result, rows

    def test_partial_predictors_materialize_wins_and_losses_with_actual_knowledge(self):
        record = predictor_input()
        result, rows = self.materialize([record])
        self.assertEqual({r['source_id']: r['direction_correct'] for r in rows}, {'nws': True, 'seattle_local': False})
        nws = next(r for r in rows if r['source_id'] == 'nws')
        self.assertTrue(nws['eligible_for_predictive_history'])
        self.assertFalse(nws['eligible_for_source_history'])
        self.assertFalse(any(row['eligible_for_source_history'] for row in rows))
        self.assertIn('partial_day_high_proxy_not_full_day', nws['strict_source_proof']['reasons'])
        self.assertEqual(nws['predictive_source_proof']['status'], 'eligible_predictive_source_proof')
        self.assertEqual(nws['forecast_semantics']['forecast_type'], 'daytime_period')
        self.assertEqual(nws['forecast_semantics']['period_start'], record['source_inputs']['source_context']['data']['weather_source_snapshot']['sources'][0]['forecast_start'])
        self.assertEqual(nws['known_after'], '2026-08-07T00:00:00+00:00')
        self.assertEqual(nws['settlement_ts'], '2026-08-04T00:00:00Z')
        self.assertEqual(nws['predictive_availability_field'], 'known_after')
        self.assertEqual(result.metadata['counts']['predictive_history_eligible'], 2)

    def test_loaded_contextual_comparison_selects_local_predictor_with_30_prior_losses_and_wins(self):
        from bot.weather.predictive_source_history import load_predictive_source_history, select_contextual_weather_source
        records = [predictor_input((date(2026, 8, 3) + timedelta(days=i)).isoformat(), raw_hash=f'{i:064x}') for i in range(30)]
        result, rows = self.materialize(records, official='NO', known='2026-10-01T00:00:00+00:00')
        history = load_predictive_source_history(result.metadata_path)
        self.assertEqual(len(history), 60)
        current = predictor_input('2026-10-16')
        current['observed_at'] = current['decision_key']['observed_at_utc'] = '2026-10-15T12:00:00+00:00'
        current = _seal(current)
        selected = select_contextual_weather_source(current, history)
        self.assertTrue(selected['routeable'])
        self.assertEqual(selected['chosen_source_id'], 'seattle_local')
        self.assertEqual(selected['selected_forecast_temp_f'], 65.)
        self.assertEqual(selected['min_sample_count'], 30)
        self.assertEqual(selected['max_history_events'], 100)
        self.assertEqual(selected['prior_sample_count'], 30)
        self.assertEqual({r['source_id']: r['win_rate'] for r in selected['source_scores']}, {'nws': 0., 'seattle_local': 1.})
        self.assertEqual(selected['confidence_type'], 'rank_score_not_calibrated_probability')
        self.assertNotIn('probability_yes', selected)
        self.assertEqual(selected['selected_source_observation']['forecast_semantics']['forecast_type'], 'daytime_period')
        self.assertFalse(any(row['eligible_for_source_history'] for row in history if row['source_id'] == 'nws'))

    def test_future_current_fetch_is_excluded_and_missing_history_has_per_source_shortfall(self):
        from bot.weather.predictive_source_history import select_contextual_weather_source
        _, history = self.materialize([predictor_input()], official='NO')
        current = current_input()
        sources = current['source_inputs']['source_context']['data']['weather_source_snapshot']['sources']
        sources[1]['source_fetched_at'] = '2026-10-16T00:00:00+00:00'
        selected = select_contextual_weather_source(_seal(current), history, min_sample_count=1)
        self.assertEqual(selected['chosen_source_id'], 'nws')
        local = next(row for row in selected['current_source_observations'] if row['source_id'] == 'seattle_local')
        self.assertIn('source_fetched_at_after_immutable_observed_at', local['predictive_source_proof']['reasons'])
        insufficient = select_contextual_weather_source(current_input(), [])
        self.assertFalse(insufficient['routeable'])
        self.assertEqual(insufficient['blockers'], ['insufficient_prior_history'])
        self.assertEqual({row['source_id']: row['shortfall'] for row in insufficient['source_scores']}, {'nws': 30, 'seattle_local': 30})

    def test_predictive_consumer_rejects_conflicting_outcomes_and_sides(self):
        from bot.weather.predictive_source_history import select_contextual_weather_source
        _, history = self.materialize([predictor_input()], official='NO')
        for field, value in [('actual_outcome', 'YES'), ('predicted_outcome', 'YES')]:
            with self.subTest(field=field):
                corrupted = [dict(history[1], **{field: value})]
                selected = select_contextual_weather_source(current_input(), corrupted, min_sample_count=1)
                self.assertFalse(selected['routeable'])
                self.assertEqual(selected['prior_sample_count'], 0)

    def test_physical_events_are_deduplicated_before_recent_100_budget(self):
        from bot.weather.predictive_source_history import select_contextual_weather_source
        records = []
        for i in range(105):
            original = predictor_input((date(2026, 8, 3) + timedelta(days=i)).isoformat(), raw_hash=f'{i:064x}')
            strike = copy.deepcopy(original)
            strike['market_id'] += '-ALT'
            strike['decision_key']['market_id'] = strike['market_id']
            strike['market']['market_metadata'].pop('event_ticker')
            sources = strike['source_inputs']['source_context']['data']['weather_source_snapshot']['sources']
            for source in sources:
                source['source_as_of'] = '2026-08-01T11:56:00+00:00'
                source['forecast_high'] = 85.
            records += [original, _seal(strike)]
        result, rows = self.materialize(records, official='NO', known='2026-12-01T00:00:00+00:00')
        self.assertEqual(len(rows), 420)  # all raw polls/strikes remain retained
        selected = select_contextual_weather_source(current_input('2026-12-16', '2026-12-15T12:00:00+00:00'), rows)
        scores = {row['source_id']: row for row in selected['source_scores']}
        self.assertEqual({row['sample_count'] for row in scores.values()}, {100})
        self.assertEqual(scores['seattle_local']['win_rate'], 1.)
        self.assertEqual(selected['history_reobservation_excluded'], 210)
        self.assertEqual(selected['history_events_outside_budget'], 10)
        self.assertEqual(selected['history_independent_rows_used'], 200)
        expected = {row['source_observation_id'] for row in rows if row['source_id'] == 'seattle_local' and row['market_date'] >= '2026-08-08' and not row['market_id'].endswith('-ALT')}
        self.assertEqual(set(scores['seattle_local']['source_observation_ids']), expected)

    def test_only_prior_resolver_knowledge_is_available_not_backfilled_settlement(self):
        from bot.weather.predictive_source_history import select_contextual_weather_source
        record = predictor_input()
        resolution = resolution_for(record, official='NO', known='2026-08-07T00:00:00+00:00')
        resolution['retrieved_at'] = '2026-10-15T12:00:00+00:00'
        _, rows = self.materialize([record], resolution_rows=[resolution])
        self.assertFalse(select_contextual_weather_source(current_input(), rows, min_sample_count=1)['routeable'])
        after = current_input(observed='2026-10-15T12:00:01+00:00')
        self.assertTrue(select_contextual_weather_source(after, rows, min_sample_count=1)['routeable'])
        resolution['resolution'].pop('resolved_at')
        resolution.pop('retrieved_at')
        _, unknown = self.materialize([record], resolution_rows=[resolution], name='unknown')
        self.assertTrue(all(row['known_after'] is None for row in unknown))
        self.assertFalse(select_contextual_weather_source(after, unknown, min_sample_count=1)['routeable'])

    def test_wrong_location_date_kind_and_future_sources_do_not_enter_predictive_history(self):
        from bot.weather.predictive_source_history import select_contextual_weather_source
        for i, change in enumerate([{'source_location_city': 'Miami'}, {'source_target_date': '2026-08-04'},
                                    {'forecast_measurement_kind': 'low'}, {'source_as_of': '2026-08-01T12:01:00+00:00'},
                                    {'source_fetched_at': '2026-08-01T12:01:00+00:00'},
                                    {'evidence_type': 'observation', 'scoreable_forecast': False}]):
            with self.subTest(change=change):
                record = predictor_input()
                for source in record['source_inputs']['source_context']['data']['weather_source_snapshot']['sources']:
                    source.update(change)
                _, rows = self.materialize([_seal(record)], name=f'bad-{i}')
                selected = select_contextual_weather_source(current_input(), rows, min_sample_count=1)
                self.assertFalse(selected['routeable'])

    def test_low_predictor_targets_prior_night_not_following_night(self):
        from bot.weather.predictive_source_history import select_contextual_weather_source
        record = predictor_input(kind='low')
        _, rows = self.materialize([record])
        self.assertTrue(select_contextual_weather_source(current_input(kind='low'), rows, min_sample_count=1)['routeable'])
        self.assertFalse(select_contextual_weather_source(current_input(), rows, min_sample_count=1)['routeable'])
        for source in record['source_inputs']['source_context']['data']['weather_source_snapshot']['sources']:
            source['forecast_start'] = source['target_mapping']['source_period_start'] = '2026-08-03T18:00:00-07:00'
            source['forecast_end'] = source['target_mapping']['source_period_end'] = '2026-08-04T06:00:00-07:00'
        _, rows = self.materialize([_seal(record)], name='wrong-night')
        self.assertEqual(rows, [])

    def test_exact_independent_resolution_void_conflict_and_mismatched_ids_are_not_history(self):
        from bot.weather.predictive_source_history import select_contextual_weather_source
        record = predictor_input()
        valid = resolution_for(record)
        for name, resolutions in [('void', [resolution_for(record, official='VOID')]),
                                  ('conflict', [valid, resolution_for(record, official='NO')]),
                                  ('wrong-id', [dict(valid, returned_market_id='OTHER')])]:
            with self.subTest(name=name):
                result, rows = self.materialize([record], resolution_rows=resolutions, name=name)
                self.assertEqual(rows, [])
                self.assertFalse(select_contextual_weather_source(current_input(), rows, min_sample_count=1)['routeable'])
                if name == 'void':
                    self.assertEqual(result.metadata['counts']['void_resolution'], 2)

    def test_history_loader_rejects_changed_materialized_bytes(self):
        from bot.weather.predictive_source_history import load_predictive_source_history
        result, _ = self.materialize([predictor_input()])
        result.settled_path.write_bytes(result.settled_path.read_bytes() + b'{}\n')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            load_predictive_source_history(result.metadata_path)

    def test_current_source_representation_is_newest_captured_not_list_order(self):
        from bot.weather.predictive_source_history import select_contextual_weather_source
        _, history = self.materialize([predictor_input()], official='NO')
        current = current_input()
        sources = current['source_inputs']['source_context']['data']['weather_source_snapshot']['sources']
        old = copy.deepcopy(sources[1])
        old['forecast_high'] = 85.
        old['source_as_of'] = '2026-08-01T11:50:00+00:00'
        sources.append(old)
        selected = select_contextual_weather_source(_seal(current), history, min_sample_count=1)
        self.assertEqual(selected['selected_forecast_temp_f'], 65.)

    def test_unidentified_city_context_does_not_become_a_shared_unknown_bucket(self):
        from bot.weather.predictive_source_history import select_contextual_weather_source
        _, history = self.materialize([predictor_input()])
        current = current_input()
        current['market']['market_metadata']['city_id'] = 'unknown'
        current['source_inputs']['source_context']['data']['weather_source_snapshot']['station_resolution']['city_id'] = 'unknown'
        unknown = [dict(row, city_id='unknown') for row in history]
        selected = select_contextual_weather_source(_seal(current), unknown, min_sample_count=1)
        self.assertFalse(selected['routeable'])

    def test_predictive_context_pools_supported_tail_directions_not_tiny_side_buckets(self):
        from bot.weather.predictive_source_history import select_contextual_weather_source
        record = predictor_input()
        record['market']['question'] = 'Will Seattle high temperature be below 70°?'
        snapshot = record['source_inputs']['source_context']['data']['weather_source_snapshot']
        snapshot['question'] = record['market']['question']
        for source in snapshot['sources']:
            source['question_side'] = 'below'
        _, rows = self.materialize([_seal(record)], official='NO')
        choice = select_contextual_weather_source(current_input(), rows, min_sample_count=1)
        self.assertTrue(choice['routeable'])
        self.assertEqual(choice['chosen_source_id'], 'nws')
        self.assertEqual(choice['slice_key'], 'seattle_wa|high|tail')
