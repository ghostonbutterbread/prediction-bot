"""Provider capture and completed aggregate clocks have different owners."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_contextual_weather_source_flow as flow
import test_contextual_predictive_source_history as predictive
from bot.prediction_lab import PredictionLab
from bot.replay_decision_input import build_replay_decision_input_v1
from bot.weather.source_observation_ledger import _retained_forecast_clocks


class AdvancingClock(flow.Clock):
    calls = 0

    @classmethod
    def now(cls, tz=None):
        cls.calls += 1
        value = cls(2026, 10, 18, 19, tzinfo=timezone.utc) + timedelta(microseconds=cls.calls * 100)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class WeatherCaptureClockSemanticsTests(unittest.TestCase):
    def test_issue_ordering_still_uses_own_and_enclosing_capture_boundaries(self):
        cases = (
            ({'source_as_of': '2026-10-18T18:01:00+00:00',
              'source_fetched_at': '2026-10-18T18:00:00+00:00'},
             {'as_of': '2026-10-18T19:00:00+00:00', 'fetched_at': '2026-10-18T19:00:00+00:00'}),
            ({'source_as_of': '2026-10-18T19:00:00.500000+00:00'},
             {'as_of': '2026-10-18T19:00:00+00:00', 'fetched_at': '2026-10-18T19:00:00+00:00'}),
            ({'source_as_of': '2026-10-18T18:00:00+00:00',
              'source_fetched_at': '2026-10-18T18:00:00+00:00'},
             {'as_of': '2026-10-18T19:00:00.500000+00:00', 'fetched_at': '2026-10-18T19:00:00+00:00'}),
        )
        for source, snapshot in cases:
            with self.subTest(source=source, snapshot=snapshot):
                evidence, reasons = _retained_forecast_clocks(source, snapshot, flow.OBSERVED)
                self.assertIn('source_issue_after_declared_capture', reasons)
                self.assertEqual(evidence['source_issue']['source_as_of'], source['source_as_of'])

    def test_future_invalid_and_conflicting_alias_clocks_remain_rejected(self):
        for owner in ('source', 'snapshot'):
            for changes, reason in (
                ({'source_as_of': '2026-10-18T19:00:02+00:00'},
                 'retained_source_clock_after_immutable_observed_at'),
                ({'source_fetched_at': '2026-10-18T19:00:02+00:00'},
                 'retained_source_clock_after_immutable_observed_at'),
                ({'as_of': '2026-10-18T18:59:00+00:00'},
                 'conflicting_retained_source_clock_aliases'),
                ({'fetched_at': '2026-10-18T18:59:00+00:00'},
                 'conflicting_retained_source_clock_aliases'),
                ({'fetched_at': 'not-a-time'}, 'invalid_retained_source_clock'),
                ({'fetched_at': '2026-10-18T19:00:00'}, 'invalid_retained_source_clock'),
            ):
                with self.subTest(owner=owner, changes=changes):
                    source = {'source_as_of': '2026-10-18T18:00:00+00:00',
                              'source_fetched_at': '2026-10-18T18:00:00+00:00'}
                    snapshot = {'source_as_of': '2026-10-18T19:00:00+00:00',
                                'source_fetched_at': '2026-10-18T19:00:00+00:00'}
                    (source if owner == 'source' else snapshot).update(changes)
                    _, reasons = _retained_forecast_clocks(source, snapshot, flow.OBSERVED)
                    self.assertIn(reason, reasons)

    def test_actual_producer_preserves_predictive_history_when_aggregation_finishes_later(self):
        for clock in (flow.Clock, AdvancingClock):
            with self.subTest(clock=clock.__name__):
                AdvancingClock.calls = 0
                helper = flow.ContextualWeatherSourceFlowTests()
                helper.setUp()
                self.addCleanup(helper.doCleanups)
                strategy = helper.strategy({'enabled': False})
                with patch('bot.feeds.weather_pro.datetime', clock), \
                     patch('bot.feeds.live_data.datetime', clock), \
                     patch('bot.strategies.signal_validator.datetime', clock), \
                     patch('bot.strategies.enhanced.datetime', clock), \
                     patch.object(strategy, '_price_signal', return_value=None), \
                     patch.object(strategy, '_volume_signal', return_value=None), \
                     patch.object(strategy, '_time_signal', return_value=None):
                    signal, trace = strategy.analyze_market_with_trace(helper.market())
                self.assertIsNotNone(signal)
                snapshot = PredictionLab.__new__(PredictionLab)._build_weather_source_snapshot(
                    {'strategy_signal': signal, 'strategy_trace': trace.to_dict()}, helper.market())
                assert snapshot is not None
                sources = [source for source in snapshot['sources']
                           if source['source_id'] in {'nws', flow.LOCAL}]
                self.assertEqual({source['source_id'] for source in sources}, {'nws', flow.LOCAL})
                aggregate = datetime.fromisoformat(snapshot['fetched_at'])
                for source in sources:
                    issue = datetime.fromisoformat(source['source_as_of'].replace('Z', '+00:00'))
                    capture = datetime.fromisoformat(source['source_fetched_at'].replace('Z', '+00:00'))
                    self.assertLessEqual(issue, capture)
                    self.assertLessEqual(capture, aggregate)
                if clock is AdvancingClock:
                    self.assertTrue(all(datetime.fromisoformat(source['source_fetched_at']) < aggregate
                                        for source in sources))
                self.assertLess(aggregate, datetime.fromisoformat(flow.OBSERVED))
                raw = {
                    'observed_at': flow.OBSERVED, 'market_id': helper.market().id,
                    'question': helper.market().question,
                    'decision_artifact': {'source_context': {'source': 'captured_weather', 'data': {
                        'market_metadata': helper.market().metadata, 'weather_source_snapshot': snapshot}}},
                }
                built = build_replay_decision_input_v1(raw, source_history_only=True)
                self.assertTrue(built.ok, built.errors)
                materializer = predictive.PredictiveMaterializationTests()
                materializer.root = helper.root
                _, rows = materializer.materialize(
                    [built.record], official='NO', known='2026-10-21T00:00:00+00:00')
                # Raw source_signal aliases are audited separately. Check the
                # actual normalized provider rows emitted by the Lab exporter.
                normalized = [row for row in rows if row['source_id'] in {'nws', flow.LOCAL}
                              and row['strict_source_proof'].get('source_as_of')]
                self.assertEqual(len(normalized), 2)
                self.assertTrue(all(row['eligible_for_predictive_history'] for row in normalized),
                                [(row['source_id'], row['predictive_source_proof']['reasons'])
                                 for row in normalized])
                self.assertTrue(all(row['resolver_knowledge_verified'] for row in normalized))
                self.assertTrue(all(row['known_after'] == '2026-10-21T00:00:00+00:00'
                                    for row in normalized))
