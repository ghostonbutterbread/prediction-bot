"""Offline vertical source selection: retained independent history -> real consumers."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from bot.feeds.live_data import LiveFeedAggregator
from bot.prediction_lab import PredictionLab
from bot.replay_decision_input import build_replay_decision_input_v1, verify_replay_decision_input_record_v1
from bot.strategies.enhanced import EnhancedStrategyEngine, strategy_config_with_policy
from bot.weather.source_observation_ledger import materialize_source_observation_ledger
from bot.replay_outcome_binding import bind_replay_finalized_outcomes
from test_contextual_predictive_source_history import predictor_input, resolution_for
from test_source_observation_ledger import _seal, _write_jsonl
from test_weather_broad_collection import gray_page as _gray_page

LOCAL = 'src_local_kvvu_weather'
QUESTION = 'Will Las Vegas high temperature be above 85° on Oct 19, 2026?'
MARKET_ID = 'KXHIGHLV-26OCT19-T85'
OBSERVED = '2026-10-18T19:00:01+00:00'


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 10, 18, 19, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


def gray_page(**kwargs):
    # The shared parser fixture was fetched Oct 19; this decision is Oct 18.
    # Keep its real schema, but issue the test page at this fixture's capture
    # clock rather than bypassing the producer's future-clock ineligibility.
    issue_ms = int(Clock.now(timezone.utc).timestamp() * 1000)
    return _gray_page(**kwargs).replace('"lastModified": 1792375876036', f'"lastModified": {issue_ms}')


class ContextualWeatherSourceFlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {'TMPDIR': os.environ['TMPDIR']}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.requests = []

    def history(self, count=30, known='2026-10-18T18:00:00+00:00', local_id=LOCAL):
        records = []
        resolutions = []
        for i in range(count):
            day = (date(2026, 8, 3) + timedelta(days=i)).isoformat()
            record = predictor_input(day, raw_hash=f'{i:064x}')
            token = date.fromisoformat(day).strftime('%y%b%d').upper()
            record['market_id'] = record['decision_key']['market_id'] = f'KXHIGHLV-{token}-T85'
            question = f'Will Las Vegas high temperature be above 85° on {day}?'
            record['market']['question'] = question
            record['market']['market_metadata'].update(city='Las Vegas', city_id='las_vegas_nv', event_ticker=f'KXHIGHLV-{token}')
            snapshot = record['source_inputs']['source_context']['data']['weather_source_snapshot']
            snapshot.update(market_id=record['market_id'], question=question, station_resolution={'city_id':'las_vegas_nv','city':'Las Vegas'})
            for source, source_id, value in zip(snapshot['sources'], ('nws', local_id), (90., 80.)):
                source.update(source_id=source_id, source_name=source_id, source_location_city='Las Vegas', forecast_high=value)
            record = _seal(record)
            records.append(record)
            # Both winners and losers survive, including the never-selected NWS.
            resolution = resolution_for(record, official='YES' if i < 5 else 'NO', known=known)
            resolution['resolution_id'] = f'independent-kalshi-{i}'
            resolutions.append(resolution)
        inputs, outcomes = self.root / 'inputs.jsonl', self.root / 'resolutions.jsonl'
        _write_jsonl(inputs, records)
        _write_jsonl(outcomes, resolutions)
        bound = bind_replay_finalized_outcomes(replay_inputs_path=inputs, strict_resolutions_path=outcomes,
            output_dir=self.root / 'data' / 'derived_reports' / 'bound', storage_root=self.root)
        result = materialize_source_observation_ledger(replay_inputs_path=inputs,
            finalized_outcomes_path=bound.outcomes_path, strict_resolutions_path=outcomes, output_dir=self.root / 'history')
        rows = [json.loads(line) for line in result.settled_path.read_text().splitlines()]
        self.assertEqual(len(rows), count * 2)
        self.assertTrue(all(row['eligible_for_predictive_history'] for row in rows))
        return result, rows

    def strategy(self, selection=None):
        config = {'enable_news':False, 'enable_social':False, 'enable_ai':False,
            'min_edge':.01, 'signal_audit_path':str(self.root / 'audit.jsonl')}
        if selection is not None:
            config['weather_source_selection'] = selection
        from bot.feeds.weather_capture_store import WeatherCaptureStore
        engine = EnhancedStrategyEngine(strategy_config_with_policy({'strategy':config}),
            capture_store=WeatherCaptureStore(storage_root=self.root))
        self.addCleanup(engine.live_feeds.close)
        weather = engine.live_feeds.weather
        weather.owm.available = False
        for feed in (weather.nws, weather.open_meteo, weather.owm):
            feed.http.close()
            feed.http = httpx.Client(transport=httpx.MockTransport(self.http_handler))
        return engine

    def http_handler(self, request):
        self.requests.append(str(request.url))
        if request.url.host == 'www.fox5vegas.com':
            return httpx.Response(200, text=gray_page())
        if request.url.host == 'api.open-meteo.com':
            return httpx.Response(503, text='fixture provider unavailable')
        if request.url.path == '/stations/KLAS':
            payload = {'geometry':{'type':'Point','coordinates':[-115.16,36.08]}, 'properties':{'stationIdentifier':'KLAS'}}
        elif request.url.path.startswith('/points/'):
            payload = {'properties':{'gridId':'VEF','gridX':1,'gridY':1}}
        elif request.url.path.endswith('/observations/latest'):
            payload = {'properties':{}}
        elif request.url.path == '/gridpoints/VEF/1,1/forecast':
            payload = {'properties':{'updateTime':'2026-10-18T18:00:00Z', 'periods':[
                {'name':'Monday','temperature':86,'temperatureUnit':'F','isDaytime':True,
                 'startTime':'2026-10-19T06:00:00-07:00','endTime':'2026-10-19T18:00:00-07:00'}]}}
        else:
            self.fail(f'unexpected collection request {request.url}')
        return httpx.Response(200, json=payload)

    def market(self):
        return SimpleNamespace(id=MARKET_ID, exchange='kalshi', question=QUESTION,
            yes_price=.4, no_price=.4, volume=10000, liquidity=10000, category='Weather',
            metadata={'city':'Las Vegas','city_id':'las_vegas_nv','market_kind':'high',
                      'event_ticker':'KXHIGHLV-26OCT19'})

    def analyze(self, strategy, price=None, traced=True):
        with patch('bot.feeds.weather_pro.datetime', Clock), \
             patch('bot.feeds.live_data.datetime', Clock), \
             patch('bot.strategies.signal_validator.datetime', Clock), \
             patch('bot.strategies.enhanced.datetime', Clock), \
             patch.object(strategy, '_price_signal', return_value=price), \
             patch.object(strategy, '_volume_signal', return_value=None), \
             patch.object(strategy, '_time_signal', return_value=None):
            return (strategy.analyze_market_with_trace(self.market()) if traced
                    else strategy.analyze_market(self.market()))

    def assert_observer_only(self, config, reason):
        strategy = self.strategy({'enabled':True, **config})
        price = {'signal_type':'price','predicted_prob':.9,'confidence':.9}
        result, trace = self.analyze(strategy, price=price)
        self.assertIsNone(result)
        self.assertEqual(trace.skip_reason_code, 'contextual_weather_observer_only')
        live = trace.raw_signals['live']
        self.assertFalse(live['data']['weather_source_selection']['routeable'])
        self.assertIn(reason, live['data']['weather_source_selection']['blockers'])
        self.assertEqual(live['data']['predicted_temp'],86)
        self.assertEqual({s['source_id'] for s in live['data']['source_details'] if s.get('scoreable_forecast')}, {'nws', LOCAL})
        self.assertIsNone(self.analyze(strategy, price=price, traced=False))
        snapshot = PredictionLab.__new__(PredictionLab)._build_weather_source_snapshot(
            {'strategy_trace':trace.to_dict()}, self.market())
        self.assertEqual(snapshot['source_signal']['data']['weather_source_selection'],live['data']['weather_source_selection'])

    def test_insufficient_history_collects_but_blocks_price_fallback_in_both_strategy_paths(self):
        history, _ = self.history(count=29)
        self.assert_observer_only({'history_metadata_path':str(history.metadata_path)}, 'insufficient_prior_history')

    def test_missing_history_collects_but_is_observer_only(self):
        self.assert_observer_only({'history_metadata_path':str(self.root / 'missing.json')}, 'missing_predictive_history')

    def test_corrupt_history_collects_but_is_observer_only(self):
        history, _ = self.history()
        history.settled_path.write_bytes(history.settled_path.read_bytes() + b'{}\n')
        self.assert_observer_only({'history_metadata_path':str(history.metadata_path)}, 'corrupt_predictive_history')

    def test_stale_contextual_history_collects_but_is_observer_only(self):
        history, _ = self.history(known='2026-10-01T00:00:00+00:00')
        self.assert_observer_only({'history_metadata_path':str(history.metadata_path),
            'max_history_age_seconds':86400}, 'stale_predictive_history')

    def test_no_current_weather_cannot_fall_back_to_price_when_selection_enabled(self):
        strategy = self.strategy({'enabled':True, 'history_metadata_path':str(self.root / 'missing.json')})
        for feed in (strategy.live_feeds.weather.nws, strategy.live_feeds.weather.open_meteo):
            feed.http.close()
            feed.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        price = {'signal_type':'price','predicted_prob':.9,'confidence':.9}
        result, trace = self.analyze(strategy, price=price)
        self.assertIsNone(result)
        self.assertEqual(trace.skip_reason_code,'weather_collection_only')
        self.assertTrue(trace.raw_signals['live']['collection_only'])
        self.assertNotIn('predicted_prob', trace.raw_signals['live'])
        self.assertIsNone(self.analyze(strategy, price=price, traced=False))

    def test_current_local_date_and_city_failures_never_borrow_its_winning_history(self):
        history, _ = self.history()
        original = self.http_handler
        for body in (gray_page(city='New Orleans'), gray_page(rows=[
            {'validTimeLocal':'2026-10-20T07:00:00-0700','temperatureMax':80,'temperatureMin':62}])):
            with self.subTest(body=body[:20]):
                strategy = self.strategy({'enabled':True,'history_metadata_path':str(history.metadata_path)})
                client = httpx.Client(transport=httpx.MockTransport(lambda request:
                    httpx.Response(200,text=body) if request.url.host == 'www.fox5vegas.com' else original(request)))
                strategy.live_feeds.weather.nws.http.close()
                strategy.live_feeds.weather.nws.http = client
                _, trace = self.analyze(strategy)
                live = trace.raw_signals['live']
                self.assertEqual(live['data']['weather_source_selection']['chosen_source_id'],'nws')
                self.assertEqual(live['data']['predicted_temp'],86)
                local = next(s for s in live['data']['source_details'] if s['source_id'] == LOCAL)
                self.assertFalse(local['scoreable_forecast'])
                self.assertIn('capture',local['source_metadata'])

    def test_future_current_local_fetch_cannot_borrow_its_winning_history(self):
        from bot.feeds.weather_pro import LocalForecastFeed
        history, _ = self.history()
        strategy = self.strategy({'enabled':True,'history_metadata_path':str(history.metadata_path)})
        original = LocalForecastFeed.get_forecast
        def future_capture(feed, *args, **kwargs):
            snapshot = original(feed, *args, **kwargs)
            snapshot.fetched_at += timedelta(days=1)
            snapshot.as_of = snapshot.fetched_at
            return snapshot
        with patch.object(LocalForecastFeed,'get_forecast',future_capture):
            _, trace = self.analyze(strategy)
        live = trace.raw_signals['live']
        self.assertEqual(live['data']['weather_source_selection']['chosen_source_id'],'nws')
        self.assertEqual(live['data']['predicted_temp'],86)
        self.assertEqual(next(s for s in live['data']['source_details'] if s['source_id'] == LOCAL)['forecast_high'],80)

    def test_outcomes_known_exactly_at_decision_are_not_prior_history(self):
        history, _ = self.history(known=Clock.now(timezone.utc).isoformat())
        self.assert_observer_only({'history_metadata_path':str(history.metadata_path)},'insufficient_prior_history')

    def test_disabled_config_is_identical_to_legacy_and_does_not_open_history(self):
        legacy = self.strategy()
        disabled = self.strategy({'enabled':False,'history_metadata_path':str(self.root / 'missing.json')})
        # Compare the same cached collection, including unique durable receipt
        # identities; separate HTTP attempts intentionally have distinct IDs.
        disabled.live_feeds.weather._cache = legacy.live_feeds.weather._cache
        original, trace_original = self.analyze(legacy)
        result, trace = self.analyze(disabled)
        self.assertEqual(result,original)
        self.assertEqual(trace.to_dict(),trace_original.to_dict())
        self.assertNotIn('weather_source_selection',trace.raw_signals['live']['data'])

    def test_selected_probability_still_hits_actual_wallet_entry_price_gate(self):
        from test_weather_probability_evidence import WeatherProbabilityEvidenceTests
        from bot.forward_composed_paper import evaluate_forward_composition
        from bot.strategies.enhanced import KellySizer
        from bot.risk import RiskManager
        history, _ = self.history()
        strategy = self.strategy({'enabled':True,'history_metadata_path':str(history.metadata_path)})
        signal, _ = self.analyze(strategy)
        self.assertIsNotNone(signal)
        # Start at the existing lane serialization seam with actual strategy odds.
        fixture = WeatherProbabilityEvidenceTests()
        rows = fixture.rows(signal)
        for row in rows:
            row.update(question=QUESTION,event_ticker='KXHIGHLV-26OCT19')
        result = evaluate_forward_composition(rows, [], starting_balance_usd=100,
            kelly_sizer=KellySizer(kelly_fraction=.5,max_bet_pct=.1),
            risk_policy=RiskManager({'starting_balance':100,'data_dir':str(self.root / 'risk')}),
            min_edge=.01,min_confidence=.5,max_entry_price=.3)
        wallet = result['combined_wallet']
        self.assertEqual(wallet['open_positions'],[])
        self.assertEqual(wallet['decision_rows'][0]['reason_code'],'entry_price_above_cap')

    def test_direct_collector_index_selects_forecast_and_preserves_wallet_gate(self):
        from bot.collector_replay_index import build_collector_replay_index
        from bot.forward_composed_paper import evaluate_forward_composition
        from bot.strategies.enhanced import KellySizer
        from bot.risk import RiskManager
        from test_weather_probability_evidence import WeatherProbabilityEvidenceTests
        self.history()
        sealed = [json.loads(line) for line in (self.root / 'inputs.jsonl').read_text().splitlines()]
        raw = []
        for record in sealed:
            raw.append({'observed_at': record['observed_at'], 'market_id': record['market_id'],
                'question': record['market']['question'],
                'shared_snapshot_id': record['decision_key']['shared_snapshot_id'],
                'decision_artifact': {'source_context': {'source': 'captured_weather', 'data': {
                    'market_metadata': record['market']['market_metadata'],
                    'weather_source_snapshot': record['source_inputs']['source_context']['data']['weather_source_snapshot']}}}})
        archive, index, manifest = (self.root / name for name in ('snapshots.jsonl', 'index.jsonl', 'index.manifest.json'))
        _write_jsonl(archive, raw)
        build_collector_replay_index(archive, index, manifest)
        config = {'enabled': True, 'collector_replay_index_path': str(index),
                  'collector_replay_manifest_path': str(manifest),
                  'strict_resolutions_path': str(self.root / 'resolutions.jsonl')}
        strategy = self.strategy(config)
        signal, trace = self.analyze(strategy)
        self.assertIsNotNone(signal, trace.skip_reason_code)
        live = trace.raw_signals['live']
        receipt = live['data']['weather_source_selection']
        self.assertTrue(receipt['routeable'], receipt)
        self.assertEqual(receipt['history_input_kind'], 'committed_index_direct_read')
        self.assertEqual((receipt['chosen_source_id'], receipt['selected_forecast_temp_f'], receipt['prior_sample_count']),
                         (LOCAL, 80, 30))
        self.assertEqual(live['predicted_prob'], .10)
        rows = WeatherProbabilityEvidenceTests().rows(signal)
        for row in rows:
            row.update(question=QUESTION, event_ticker='KXHIGHLV-26OCT19')
        wallet = evaluate_forward_composition(rows, [], starting_balance_usd=100,
            kelly_sizer=KellySizer(kelly_fraction=.5,max_bet_pct=.1),
            risk_policy=RiskManager({'starting_balance':100,'data_dir':str(self.root / 'risk')}),
            min_edge=.01,min_confidence=.5,max_entry_price=.3)['combined_wallet']
        self.assertEqual(wallet['open_positions'], [])
        self.assertEqual(wallet['decision_rows'][0]['reason_code'], 'entry_price_above_cap')

    def test_stable_open_meteo_selection_reaches_hyphenated_engine_provider(self):
        history, _ = self.history(local_id='open_meteo')
        strategy = self.strategy({'enabled':True,'history_metadata_path':str(history.metadata_path)})
        original = self.http_handler
        def handler(request):
            if request.url.host == 'api.open-meteo.com':
                return httpx.Response(200,json={'timezone':'America/Los_Angeles','utc_offset_seconds':-25200,
                    'current':{'temperature_2m':75},
                    'hourly':{'time':['2026-10-19T12:00','2026-10-19T16:00'],'temperature_2m':[79,80]}})
            if request.url.host == 'www.fox5vegas.com':
                return httpx.Response(503)
            return original(request)
        for feed in (strategy.live_feeds.weather.nws,strategy.live_feeds.weather.open_meteo):
            feed.http.close()
            feed.http = httpx.Client(transport=httpx.MockTransport(handler))
        _, trace = self.analyze(strategy)
        live = trace.raw_signals['live']
        self.assertEqual(live['data']['weather_source_selection']['chosen_source_id'],'open_meteo')
        self.assertEqual(live['data']['forecast_driver'],'open-meteo')
        self.assertEqual(live['data']['predicted_temp'],80)
        self.assertEqual(live['probability_evidence']['source_id'],'open-meteo')

    def test_history_generation_change_during_load_is_observer_only(self):
        from bot.weather.predictive_source_history import load_predictive_source_history
        history, _ = self.history()
        def replaced_generation(path):
            rows = load_predictive_source_history(path)
            Path(path).write_bytes(Path(path).read_bytes() + b'\n')
            return rows
        with patch('bot.weather.contextual_source_flow.load_predictive_source_history',side_effect=replaced_generation):
            self.assert_observer_only({'history_metadata_path':str(history.metadata_path)},'predictive_history_generation_changed')

    def test_real_market_without_city_enrichment_uses_captured_location_through_lab_replay(self):
        from bot.weather.predictive_source_history import select_contextual_weather_source
        history, rows = self.history()
        strategy = self.strategy({'enabled':True,'history_metadata_path':str(history.metadata_path)})
        market = self.market()
        market.metadata = {'event_ticker':'KXHIGHLV-26OCT19'}
        with patch.object(self,'market',return_value=market):
            result, trace = self.analyze(strategy)
        live = trace.raw_signals['live']
        self.assertEqual(live['data']['weather_source_selection']['chosen_source_id'],LOCAL,live['data']['weather_source_selection'])
        snapshot = PredictionLab.__new__(PredictionLab)._build_weather_source_snapshot(
            {'strategy_signal':result,'strategy_trace':trace.to_dict()},market)
        raw = {'observed_at':OBSERVED,'market_id':MARKET_ID,'question':QUESTION,
            'decision_artifact':{'source_context':{'source':'captured_weather','data':{
                'market_metadata':market.metadata,'weather_source_snapshot':snapshot}}}}
        sealed = build_replay_decision_input_v1(raw,source_history_only=True)
        self.assertTrue(sealed.ok,sealed.errors)
        selected = select_contextual_weather_source(sealed.record,rows)
        self.assertTrue(selected['routeable'],selected['blockers'])
        self.assertEqual(selected['chosen_source_id'],LOCAL)

    def test_loaded_local_winner_changes_real_strategy_probability_and_prediction_lab_receipt(self):
        history, rows = self.history()
        self.assertEqual({row['source_id'] for row in rows}, {'nws', LOCAL})
        for source in ('nws', LOCAL):
            self.assertEqual({row['direction_correct'] for row in rows if row['source_id'] == source}, {True,False})
        control = self.strategy()
        control_signal, control_trace = self.analyze(control)
        selected = self.strategy({'enabled':True, 'history_metadata_path':str(history.metadata_path)})
        selected_signal, selected_trace = self.analyze(selected)
        live = selected_trace.raw_signals['live']
        self.assertEqual(live['data']['predicted_temp'], 80, live['data'].get('weather_source_selection'))
        self.assertEqual(live['data']['forecast_driver'], LOCAL)
        self.assertEqual(live['predicted_prob'], .10)
        self.assertEqual(control_trace.raw_signals['live']['predicted_prob'], .85)
        self.assertIsNotNone(selected_signal)
        self.assertIsNotNone(control_signal)
        self.assertNotEqual(selected_signal['model_probability'], control_signal['model_probability'])
        self.assertEqual(live['probability_evidence']['source_id'], LOCAL)
        self.assertEqual(live['probability_evidence']['yes_probability'], live['predicted_prob'])
        self.assertEqual(live['probability_evidence']['calibration_status'], 'not_established')
        audit = live['data']['weather_source_selection']
        self.assertTrue(audit['routeable'], audit)
        self.assertEqual(audit['prior_sample_count'], 30)
        self.assertEqual(audit['chosen_source_id'], LOCAL)
        self.assertEqual(audit['selected_forecast_temp_f'], 80)
        self.assertEqual(audit['min_sample_count'], 30)
        self.assertEqual(audit['max_history_events'], 100)
        self.assertTrue(audit['history_generation_sha256'])
        self.assertNotIn('current_source_observations', audit)
        self.assertNotIn('selected_source_observation', audit)
        scores = {score['source_id']:score for score in audit['source_scores']}
        self.assertEqual(scores[LOCAL]['wins'], 25)
        self.assertEqual(scores[LOCAL]['losses'], 5)
        self.assertLessEqual(len(scores[LOCAL]['source_observation_ids']), 3)
        # Collection is cached: the second score must not issue any HTTP calls.
        request_count = len(self.requests)
        again, _ = self.analyze(selected)
        self.assertEqual(len(self.requests), request_count)
        self.assertEqual(again['model_probability'], selected_signal['model_probability'])
        artifact = {'strategy_signal':selected_signal, 'strategy_trace':selected_trace.to_dict(), 'observed_at':OBSERVED}
        snapshot = PredictionLab.__new__(PredictionLab)._build_weather_source_snapshot(artifact, self.market())
        row = {'observed_at':OBSERVED, 'market_id':MARKET_ID, 'question':QUESTION,
            'decision_artifact':{'source_context':{'source':'captured_weather','data':{
                'market_metadata':self.market().metadata, 'weather_source_snapshot':snapshot}}}}
        sealed = build_replay_decision_input_v1(row, source_history_only=True)
        self.assertTrue(sealed.ok, sealed.errors)
        self.assertTrue(verify_replay_decision_input_record_v1(sealed.record))
        snapshot = sealed.record['source_inputs']['source_context']['data']['weather_source_snapshot']
        sources = {s['source_id']:s for s in snapshot['sources']}
        self.assertEqual(sources[LOCAL]['forecast_high'],80)
        self.assertEqual(sources['nws']['forecast_high'],86)
        self.assertEqual(sources[LOCAL]['weight'],1)
        self.assertEqual(sources['nws']['weight'],0)
        self.assertEqual(sources[LOCAL]['forecast_type'],'local_daytime_high_predictor')
        self.assertEqual(sources[LOCAL]['source_location_city'],'las vegas')
        self.assertEqual(sources[LOCAL]['source_fetched_at'],Clock.now(timezone.utc).isoformat())
        self.assertEqual(sources[LOCAL]['target_mapping']['source_target_date'],'2026-10-19')
        self.assertEqual(snapshot['source_signal']['probability_evidence']['source_id'],LOCAL)
        self.assertEqual(snapshot['source_signal']['data']['weather_source_selection'],audit)
