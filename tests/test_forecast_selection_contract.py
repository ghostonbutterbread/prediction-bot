"""Independent offline collect -> score -> compare -> select -> pass contract.

Runs unchanged on the repair-only dependency and the contextual feature. The
constructor shim only supplies supported keywords; it never emulates selection.
No existing test helpers, public captures, credentials or archive data are used.
"""
import copy
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import socket
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx

from bot.feeds.live_data import LiveFeedAggregator
from bot.prediction_lab import PredictionLab
from bot.replay_outcome_binding import bind_replay_finalized_outcomes
from bot.weather.source_observation_ledger import materialize_source_observation_ledger

LOCAL = 'src_local_kvvu_weather'
QUESTION = 'Will Las Vegas high temperature be above 85° on Oct 19, 2026?'
CURRENT_ID = 'KXHIGHLV-26OCT19-T85'
DECISION = '2026-10-18T19:00:00+00:00'
KNOWN = '2026-10-01T00:00:00+00:00'


def seal(record) -> dict[str, Any]:
    result = copy.deepcopy(record)
    result.pop('canonical_input_sha256', None)
    result['canonical_input_sha256'] = hashlib.sha256(json.dumps(
        result, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return result


def historical_input(day, index=0, *, observed='2026-08-01T12:00:00+00:00') -> dict[str, Any]:
    token = date.fromisoformat(day).strftime('%y%b%d').upper()
    market_id = f'KXHIGHLV-{token}-T85'
    question = f'Will Las Vegas high temperature be above 85° on {day}?'
    digest = hashlib.sha256(f'fixture-{index}-{day}-{observed}'.encode()).hexdigest()
    start, end = day + 'T06:00:00-07:00', day + 'T18:00:00-07:00'
    sources = []
    for source_id, value in [('nws', 90.), (LOCAL, 80.)]:
        sources.append(dict(source_id=source_id, source_name=source_id,
            source_location_city='Las Vegas', forecast_high=value,
            source_as_of='2026-08-01T11:54:00+00:00',
            source_fetched_at='2026-08-01T11:55:00+00:00',
            source_evidence_version=1, evidence_type='forecast', scoreable_forecast=True,
            forecast_measurement_kind='high', contract_shape='tail', question_side='above',
            forecast_type='daytime_period', forecast_start=start, forecast_end=end,
            target_mapping=dict(market_target_date=day, source_target_date=day,
                source_period_start=start, source_period_end=end, source_timezone='America/Los_Angeles')))
    return seal(dict(schema_name='replay_decision_input', schema_version=1,
        decision_key=dict(shared_snapshot_id='snapshot-' + digest,
            shared_candidate_id='candidate-' + digest, market_id=market_id,
            observed_at_utc=observed, raw_row_sha256=digest),
        market_id=market_id, observed_at=observed,
        market=dict(question=question, market_metadata=dict(city='Las Vegas',
            city_id='las_vegas_nv', market_kind='high', event_ticker=f'KXHIGHLV-{token}')),
        source_inputs=dict(recorded_as_of='2026-08-01T11:55:00+00:00',
            source_context=dict(as_of='2026-08-01T11:55:00+00:00', data=dict(
                weather_source_snapshot=dict(market_id=market_id, question=question,
                    market_date=day, station_resolution=dict(city='Las Vegas', city_id='las_vegas_nv'),
                    sources=sources))))))


def resolution(record, official='NO', known=KNOWN):
    day = record['source_inputs']['source_context']['data']['weather_source_snapshot']['market_date']
    return dict(market_id=record['market_id'], requested_market_id=record['market_id'],
        returned_market_id=record['market_id'], market_status='finalized', kalshi_result=official.lower(),
        settlement_ts=(date.fromisoformat(day) + timedelta(days=1)).isoformat() + 'T00:00:00+00:00',
        resolution_id='exchange-' + record['market_id'],
        resolution=dict(outcome=official, resolved_at=known, source='kalshi_api'))


class FrozenClock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls.fromisoformat(DECISION)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class ForecastSelectionContractTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.assertEqual(Path(inspect.getfile(LiveFeedAggregator)).resolve(),
            Path(__file__).resolve().parents[1] / 'bot' / 'feeds' / 'live_data.py',
            'tests imported another checkout instead of the selected snapshot')
        self.requests = []
        self.inputs = []
        self.independent = []
        self.readonly_inputs = {}
        env = patch.dict(os.environ, {'TMPDIR': str(self.root),
            'PREDICTION_BOT_COLLECTOR_ROOT': str(self.root)}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        # MockTransport never uses sockets. Any accidental nonfixture path fails.
        for method in ('connect', 'connect_ex', 'sendto'):
            guard = patch.object(socket.socket, method, side_effect=AssertionError('network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        for module in ('bot.feeds.weather_pro', 'bot.feeds.live_data'):
            clock = patch(module + '.datetime', FrozenClock)
            clock.start()
            self.addCleanup(clock.stop)

    def tearDown(self):
        for path, original in self.readonly_inputs.items():
            self.assertEqual(path.read_bytes(), original, f'raw fixture rewritten: {path}')

    def materialize(self, records, resolutions, name='history'):
        inputs = self.root / (name + '-inputs.jsonl')
        outcomes = self.root / (name + '-resolutions.jsonl')
        for path, rows in ((inputs, records), (outcomes, resolutions)):
            path.write_text(''.join(json.dumps(row, sort_keys=True) + '\n' for row in rows))
            self.readonly_inputs[path] = path.read_bytes()
        bound = bind_replay_finalized_outcomes(replay_inputs_path=inputs,
            strict_resolutions_path=outcomes, output_dir=self.root / 'data' / 'derived_reports' / (name + '-bound'),
            storage_root=self.root)
        kwargs = dict(replay_inputs_path=inputs, finalized_outcomes_path=bound.outcomes_path,
            output_dir=self.root / name)
        if 'strict_resolutions_path' in inspect.signature(materialize_source_observation_ledger).parameters:
            kwargs['strict_resolutions_path'] = outcomes
        result = materialize_source_observation_ledger(**kwargs)
        rows = [json.loads(line) for line in result.settled_path.read_text().splitlines()]
        return result, rows

    def history(self, count=30, known=KNOWN, repeats=1):
        records, resolutions = [], []
        for i in range(count):
            day = (date(2026, 8, 3) + timedelta(days=i)).isoformat()
            first = historical_input(day, i)
            official = 'YES' if i < 5 else 'NO'
            for repeat in range(repeats):
                record = copy.deepcopy(first)
                record['decision_key']['shared_snapshot_id'] += f'-poll-{repeat}'
                record['decision_key']['shared_candidate_id'] += f'-poll-{repeat}'
                record = seal(record)
                records.append(record)
            resolutions.append(resolution(first, official, known))
        self.inputs, self.independent = records, resolutions
        return self.materialize(records, resolutions)

    def selector(self):
        # Missing product API is a feature assertion, never a collection/import error.
        name = 'bot.weather.predictive_source_history'
        self.assertIsNotNone(importlib.util.find_spec(name), 'missing empirical predictive selection behavior')
        return importlib.import_module(name).select_contextual_weather_source

    def current(self):
        return historical_input('2026-10-19', 999, observed=DECISION)

    def body(self):
        data = {'wx-current-conditions-v3': {'{}': {
            'lastModified': int(FrozenClock.now(timezone.utc).timestamp() * 1000),
            'data': {'imperial': {'location': {'city': 'Las Vegas',
                'ianaTimeZone': 'America/Los_Angeles', 'latitude': 36.17, 'longitude': -115.14},
                'dailyForecast': [{'validTimeLocal': '2026-10-19T07:00:00-0700',
                    'temperatureMax': 80, 'temperatureMin': 62}],
                'currentObservation': {'temperature': 74}}}}}}
        return '<script id="fusion-metadata">var Fusion={};Fusion.contentCache=' + json.dumps(data) + ';</script>'

    def http(self, request):
        self.requests.append(str(request.url))
        if request.url.host == 'www.fox5vegas.com':
            return httpx.Response(200, text=self.body())
        if request.url.host == 'api.open-meteo.com':
            return httpx.Response(503, text='fixture unavailable')
        if request.url.path == '/stations/KLAS':
            payload = {'geometry': {'type': 'Point', 'coordinates': [-115.16, 36.08]},
                       'properties': {'stationIdentifier': 'KLAS'}}
        elif request.url.path.startswith('/points/'):
            payload = {'properties': {'gridId': 'VEF', 'gridX': 1, 'gridY': 1}}
        elif request.url.path.endswith('/observations/latest'):
            payload = {'properties': {}}
        elif request.url.path == '/gridpoints/VEF/1,1/forecast':
            payload = {'properties': {'updateTime': '2026-10-18T18:00:00Z', 'periods': [
                {'name': 'Monday', 'temperature': 86, 'temperatureUnit': 'F', 'isDaytime': True,
                 'startTime': '2026-10-19T06:00:00-07:00', 'endTime': '2026-10-19T18:00:00-07:00'}]}}
        else:
            raise AssertionError(f'unexpected fixture request: {request.url}')
        return httpx.Response(200, json=payload)

    def aggregator(self, history=None, enabled=True):
        config = dict(enabled=enabled, history_metadata_path=str(history.metadata_path) if history else str(self.root / 'missing.json'))
        # Baseline predates both constructor arguments. Do not supply unsupported
        # keywords or backport feature logic just to make the RED setup run.
        kwargs = {}
        parameters = inspect.signature(LiveFeedAggregator).parameters
        if 'weather_source_selection' in parameters:
            kwargs['weather_source_selection'] = config
        if 'capture_store' in parameters:
            from bot.feeds.weather_capture_store import WeatherCaptureStore
            kwargs['capture_store'] = WeatherCaptureStore(storage_root=self.root)
        aggregator = LiveFeedAggregator(**kwargs)
        aggregator.weather_source_selection = config
        self.addCleanup(aggregator.close)
        aggregator.weather.owm.available = False
        for feed in (aggregator.weather.nws, aggregator.weather.open_meteo, aggregator.weather.owm):
            feed.http.close()
            feed.http = httpx.Client(transport=httpx.MockTransport(self.http))
        return aggregator

    def score(self, aggregator) -> dict[str, Any]:
        result = aggregator.get_signal(QUESTION, .4, 'Weather', market_id=CURRENT_ID,
            market_metadata=dict(city='Las Vegas', city_id='las_vegas_nv', market_kind='high',
                event_ticker='KXHIGHLV-26OCT19'))
        self.assertIsInstance(result, dict)
        assert result is not None
        return result

    def audit(self, signal):
        self.assertIsInstance(signal, dict)
        audit = signal.get('data', {}).get('weather_source_selection')
        self.assertIsInstance(audit, dict, 'collector never compared/passed contextual source evidence')
        return audit

    def test_fixture_control_runs_existing_nws_scoring_without_feature(self):
        result = self.score(self.aggregator(enabled=False))
        self.assertEqual(result['data']['predicted_temp'], 86)
        self.assertEqual(result['predicted_prob'], .85)
        self.assertTrue(any('/forecast' in url for url in self.requests))

    def test_every_source_retained_with_provenance_and_unavailable_reason(self):
        result = self.score(self.aggregator())
        sources = {row.get('source_id') or row.get('source'): row for row in result['data']['source_details']}
        self.assertIn(LOCAL, sources, 'city-specific forecast was not collected')
        self.assertEqual(sources[LOCAL]['forecast_high'], 80)
        self.assertEqual(sources['nws']['forecast_high'], 86)
        self.assertFalse(sources['open_meteo']['scoreable_forecast'])
        self.assertTrue(sources['open_meteo']['availability_reason'])
        for key in (LOCAL, 'nws'):
            self.assertTrue(sources[key]['source_metadata']['capture'])
            self.assertTrue(sources[key].get('source_fetched_at') or sources[key].get('fetched_at'))
            self.assertEqual(sources[key]['target_mapping']['source_target_date'], '2026-10-19')

    def test_partial_history_scores_both_sources_without_strict_promotion(self):
        _, rows = self.history()
        self.assertEqual(len(rows), 60)
        self.assertEqual({row['source_id'] for row in rows}, {'nws', LOCAL})
        self.assertTrue(all(row.get('eligible_for_predictive_history') is True for row in rows),
            'partial forecasts cannot enter empirical history')
        self.assertTrue(all(row['eligible_for_source_history'] is False for row in rows))
        for source in ('nws', LOCAL):
            self.assertEqual({row['direction_correct'] for row in rows if row['source_id'] == source}, {True, False})
        self.assertEqual({row['known_after'] for row in rows}, {KNOWN})

    def test_duplicate_polls_retain_rows_but_never_mature_one_outcome_into_thirty(self):
        _, rows = self.history(count=1, repeats=35)
        self.assertEqual(len(rows), 70)
        choice = self.selector()(self.current(), rows)
        self.assertFalse(choice['routeable'])
        self.assertEqual(choice['blockers'], ['insufficient_prior_history'])
        self.assertEqual({score['sample_count'] for score in choice['source_scores']}, {1})
        self.assertEqual(choice['history_reobservation_excluded'], 68)

    def test_late_outcome_is_not_prior_knowledge_at_or_before_decision(self):
        _, rows = self.history(known=DECISION)
        choice = self.selector()(self.current(), rows)
        self.assertFalse(choice['routeable'])
        later = historical_input('2026-10-19', 1000, observed='2026-10-18T19:00:01+00:00')
        self.assertEqual(self.selector()(later, rows)['chosen_source_id'], LOCAL)

    def test_unrelated_city_history_cannot_supply_contextual_maturity(self):
        _, rows = self.history()
        wrong = [dict(row, city_id='miami_fl') for row in rows]
        choice = self.selector()(self.current(), wrong)
        self.assertFalse(choice['routeable'])
        self.assertEqual(choice['prior_sample_count'], 0)

    def test_historical_revisions_cannot_choose_the_winning_representative(self):
        self.history()
        revised = []
        for record in self.inputs:
            revision = copy.deepcopy(record)
            revision['observed_at'] = revision['decision_key']['observed_at_utc'] = '2026-08-01T12:05:00+00:00'
            revision['decision_key']['shared_snapshot_id'] += '-revision'
            revision['decision_key']['shared_candidate_id'] += '-revision'
            sources = revision['source_inputs']['source_context']['data']['weather_source_snapshot']['sources']
            for source in sources:
                source['source_as_of'] = '2026-08-01T12:03:00+00:00'
                source['source_fetched_at'] = '2026-08-01T12:04:00+00:00'
                source['forecast_high'] = 80. if source['source_id'] == 'nws' else 90.
            revised.append(seal(revision))
        _, rows = self.materialize(self.inputs + revised, self.independent, 'revisions')
        self.assertEqual(len(rows), 120)
        choose = self.selector()
        expected = choose(self.current(), rows)
        reverse = choose(self.current(), list(reversed(rows)))
        self.assertEqual(expected['chosen_source_id'], LOCAL)
        self.assertEqual(reverse['chosen_source_id'], LOCAL)
        self.assertEqual({s['sample_count'] for s in expected['source_scores']}, {30})
        self.assertEqual({s['source_id']: s['wins'] for s in expected['source_scores']}, {'nws': 5, LOCAL: 25})

    def test_insufficient_history_is_explicit_abstention_after_collection(self):
        history, _ = self.history(count=29)
        result = self.score(self.aggregator(history))
        audit = self.audit(result)
        self.assertFalse(audit['routeable'])
        self.assertIsNone(audit['chosen_source_id'])
        self.assertIn('insufficient_prior_history', audit['blockers'])
        self.assertIn(LOCAL, {s['source_id'] for s in result['data']['source_details']})

    def test_winner_reaches_downstream_without_rank_as_probability_or_archive_replay(self):
        history, _ = self.history()
        aggregator = self.aggregator(history)
        # The normal decision consumes the already-derived generation. Any
        # rediscovery/materialization would replay history, not incremental use.
        with patch('bot.weather.source_observation_ledger.materialize_source_observation_ledger',
                   side_effect=AssertionError('decision must not replay history')), \
             patch.object(Path, 'rglob', side_effect=AssertionError('archive scanning forbidden')), \
             patch.object(Path, 'glob', side_effect=AssertionError('archive scanning forbidden')):
            result = self.score(aggregator)
            repeat = self.score(aggregator)
        audit = self.audit(result)
        self.assertTrue(audit['routeable'])
        self.assertEqual(audit['chosen_source_id'], LOCAL)
        self.assertEqual(audit['selected_forecast_temp_f'], 80)
        self.assertEqual(audit['prior_sample_count'], 30)
        self.assertEqual(result['data']['predicted_temp'], 80)
        self.assertEqual(result['data']['forecast_driver'], LOCAL)
        self.assertEqual(result['predicted_prob'], .10)
        self.assertEqual(repeat['predicted_prob'], .10)
        self.assertEqual(audit['confidence_type'], 'rank_score_not_calibrated_probability')
        self.assertNotIn('probability_yes', audit)
        self.assertNotIn('yes_probability', audit)
        self.assertEqual(result['probability_evidence']['source_id'], LOCAL)
        self.assertEqual(result['probability_evidence']['calibration_status'], 'not_established')
        market = SimpleNamespace(id=CURRENT_ID, exchange='kalshi', question=QUESTION,
            metadata=dict(city='Las Vegas', city_id='las_vegas_nv', event_ticker='KXHIGHLV-26OCT19'))
        snapshot = PredictionLab.__new__(PredictionLab)._build_weather_source_snapshot(
            {'strategy_trace': {'raw_signals': {'live': result}}}, market)
        received = snapshot['source_signal']['data']['weather_source_selection']
        self.assertEqual(received, audit)
        scores = {score['source_id']: score for score in received['source_scores']}
        self.assertEqual((scores[LOCAL]['wins'], scores[LOCAL]['losses']), (25, 5))
        self.assertTrue(scores[LOCAL]['source_observation_ids'])
        self.assertEqual(scores['nws']['losses'], 25)

    def test_current_revision_uses_newest_known_forecast_not_future_payload(self):
        _, rows = self.history()
        current = self.current()
        sources = current['source_inputs']['source_context']['data']['weather_source_snapshot']['sources']
        local = next(source for source in sources if source['source_id'] == LOCAL)
        known = copy.deepcopy(local)
        known.update(forecast_high=79., source_as_of='2026-10-18T18:58:00+00:00',
            source_fetched_at='2026-10-18T18:59:00+00:00')
        future = copy.deepcopy(known)
        future.update(forecast_high=99., source_as_of='2026-10-18T19:00:01+00:00',
            source_fetched_at='2026-10-18T19:00:02+00:00')
        sources.extend([known, future])
        current = seal(current)
        before = copy.deepcopy(current)
        choice = self.selector()(current, rows)
        self.assertEqual(choice['chosen_source_id'], LOCAL)
        self.assertEqual(choice['selected_forecast_temp_f'], 79.)
        self.assertEqual(current, before, 'selection rewrote captured forecast input')
        self.assertEqual(len(choice['current_source_observations']), 4)
        excluded = [r for r in choice['current_source_observations'] if r['forecast_temp_f'] == 99.]
        self.assertEqual(len(excluded), 1)
        self.assertNotEqual(excluded[0]['predictive_source_proof']['status'], 'eligible_predictive_source_proof')

    def test_missing_resolver_knowledge_does_not_backdate_to_settlement(self):
        self.history()
        unknown = copy.deepcopy(self.independent)
        for outcome in unknown:
            outcome['resolution'].pop('resolved_at')
        _, rows = self.materialize(self.inputs, unknown, 'unknown-knowledge')
        self.assertEqual(len(rows), 60)
        self.assertEqual({row['known_after'] for row in rows}, {None})
        self.assertFalse(self.selector()(self.current(), rows)['routeable'])

    def test_equal_contextual_support_abstains_instead_of_claiming_id_order_superiority(self):
        # Inventory/evaluation contract D.4 requires an indistinguishable tie
        # to be observer-only. Stable ID order may order evidence, not invent
        # predictive superiority between equally supported alternatives.
        self.history()
        tied = copy.deepcopy(self.inputs)
        for record in tied:
            for source in record['source_inputs']['source_context']['data']['weather_source_snapshot']['sources']:
                source['forecast_high'] = 80.
        tied = [seal(record) for record in tied]
        _, rows = self.materialize(tied, self.independent, 'equal-support')
        choose = self.selector()
        for ordering in (rows, list(reversed(rows))):
            choice = choose(self.current(), ordering)
            scores = choice['source_scores']
            self.assertEqual({(score['sample_count'], score['wins'], score['losses']) for score in scores}, {(30, 25, 5)})
            self.assertFalse(choice['routeable'], 'source-ID order is not evidence of a uniquely best predictor')
            self.assertIsNone(choice['chosen_source_id'])
            self.assertTrue(choice['blockers'], 'tie abstention needs an explicit reason')

    def test_non_independent_or_mismatched_resolution_cannot_score(self):
        record = historical_input('2026-08-03')
        fake = resolution(record)
        fake['returned_market_id'] = 'OTHER-MARKET'
        _, rows = self.materialize([record], [fake], 'mismatched')
        self.assertEqual(rows, [])


if __name__ == '__main__':
    unittest.main()
