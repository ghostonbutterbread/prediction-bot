"""Focused synthetic collector-to-projection regressions; no evaluator adapter."""
import copy
import unittest
from types import SimpleNamespace

from bot.replay_decision_input import build_replay_decision_input_v1, verify_replay_decision_input_record_v1
from bot.shared_market_feed import build_shared_market_candidate_row
from test_replay_decision_input import _snapshot_row


def candidate(row):
    market = SimpleNamespace(id=row['market_id'], question=row['question'], exchange='kalshi',
                             yes_price=.42, no_price=.58, volume=456, category='Weather',
                             closes_at='2026-08-13T00:00:00+00:00', metadata={})
    row['shared_candidate'] = build_shared_market_candidate_row(
        run_id='run', market=market, decision_artifact=row['decision_artifact'],
        source_runtime='prediction_lab', observed_at=row['observed_at'])
    row['shared_candidate_id'] = row['shared_candidate']['candidate_id']
    return row['shared_candidate']


def project(row):
    result = build_replay_decision_input_v1(row)
    if not result.ok:
        raise AssertionError(result.errors)
    assert result.record is not None
    assert verify_replay_decision_input_record_v1(result.record)
    return result.record


class StrategyProjectionRegressionTests(unittest.TestCase):
    def test_shared_producer_synthetic_source_is_authoritative(self):
        row = _snapshot_row()
        artifact = row['decision_artifact']
        artifact['order_book_snapshot'] = {'source': 'book', 'data': {'best_yes_ask': .43}}
        artifact['provenance'] = {'sources': [{'field': 'order_book_snapshot',
                                               'path': 'decision_artifact.execution_snapshot'}]}
        shared = candidate(row)
        before = copy.deepcopy(row)
        self.assertEqual(shared['prices']['order_book_snapshot']['source'], 'synthetic')
        market = project(row)['market']
        self.assertEqual(market['order_book_snapshot']['source'], 'synthetic')
        self.assertEqual(market['book_projection_provenance']['order_book_snapshot'],
                         {'path': 'shared_candidate.prices.order_book_snapshot', 'timing': 'generic'})
        self.assertEqual(row, before)

    def test_shared_synthetic_correction_applies_to_pre_logic_book(self):
        row = _snapshot_row()
        artifact = row['decision_artifact']
        artifact['pre_logic_order_book_snapshot'] = {'source': 'book', 'data': {'best_yes_ask': .43}}
        artifact['provenance'] = {'sources': [{'field': 'order_book_snapshot',
                                               'path': 'decision_artifact.execution_snapshot'}]}
        shared = candidate(row)
        self.assertEqual(shared['prices']['order_book_snapshot']['source'], 'synthetic')
        market = project(row)['market']
        self.assertEqual(market['pre_logic_order_book_snapshot']['source'], 'synthetic')
        self.assertEqual(market['book_projection_provenance']['pre_logic_order_book_snapshot'],
                         {'path': 'shared_candidate.prices.order_book_snapshot', 'timing': 'pre_logic'})

    def test_invalid_root_volume_does_not_hide_shared_numeric_volume(self):
        from bot.prediction_lab_replay import ReplayArtifactInput, _market_from_record
        for bad in ('broken', True, 'nan', '', None):
            with self.subTest(bad=bad):
                row = _snapshot_row(patch={'volume': bad})
                candidate(row)
                projected = project(row)
                market = projected['market']
                self.assertEqual(market['volume'], 456)
                if bad is not None:
                    self.assertIn({'path': '$.volume', 'category': 'invalid_numeric'},
                                  projected['sanitization']['omitted_fields'])
                adapter = {'market_id': row['market_id'], 'question': market['question'],
                           'yes_price': market['yes_price'],
                           'shared_candidate': {'schema_name': 'shared_market_candidate', 'market': market}}
                self.assertEqual(_market_from_record(ReplayArtifactInput(row=adapter, artifact={})).volume, 456)
        for valid in (0, 123, 1.5, '12.25', -3):
            with self.subTest(valid=valid):
                row = _snapshot_row(patch={'volume': valid})
                candidate(row)
                self.assertEqual(project(row)['market']['volume'], valid)

    def test_book_numeric_scalars_are_coerced_or_explicitly_unavailable(self):
        from bot.prediction_lab_replay import _recorded_order_book
        from bot.strategies.enhanced import EnhancedStrategyEngine
        row = _snapshot_row()
        row['decision_artifact']['pre_logic_order_book_snapshot'] = {
            'source': 'book', 'data': {'best_yes_ask': '0.43', 'best_no_ask': .59,
                                       'spread_pct': '2.5', 'spread': '0.01', 'mid_yes': 0}}
        market = project(row)['market']
        book = _recorded_order_book({'pre_logic_order_book_snapshot': market['pre_logic_order_book_snapshot']})
        self.assertEqual(book, {'best_yes_ask': .43, 'best_no_ask': .59, 'spread_pct': 2.5, 'spread': .01, 'mid_yes': 0})
        signal = EnhancedStrategyEngine._price_signal(None, SimpleNamespace(yes_price=.42, volume=10000, category='Weather'), book)
        self.assertIsNotNone(signal)
        for bad in ('broken', 'nan', 'inf', True, None, [], {}):
            with self.subTest(bad=bad):
                row = _snapshot_row()
                row['decision_artifact']['pre_logic_order_book_snapshot'] = {
                    'source': 'book', 'data': {'best_yes_ask': .43, 'spread_pct': bad}}
                projected = project(row)
                self.assertNotIn('pre_logic_order_book_snapshot', projected['market'])
                self.assertIn({'path': 'decision_artifact.pre_logic_order_book_snapshot.data.spread_pct',
                               'category': 'invalid_numeric'}, projected['sanitization']['omitted_fields'])
                self.assertIn({'path': 'decision_artifact.pre_logic_order_book_snapshot',
                               'category': 'invalid_book_spread'}, projected['sanitization']['omitted_fields'])

    def test_nested_outcome_stripping_and_hash_tampering_remain_intact(self):
        row = _snapshot_row()
        row['shared_candidate'] = {
            'schema_name': 'shared_market_candidate',
            'market': {'volume': 55, 'route': {'family': 'weather', 'outcome': 'YES',
                                                'evidence': {'winner': 'YES', 'series': 'x'}}},
            'prices': {'order_book_snapshot': {'source': 'book', 'finalOutcome': 'YES',
                                              'data': {'best_yes_ask': .4, 'winningSide': 'YES'}}},
            'evidence': {'pre_logic_order_book_snapshot': {'source': 'book',
                                                           'data': {'best_no_ask': .5, 'result': 'YES'}}}}
        row['decision_artifact']['order_book'] = {'best_yes_ask': .4, 'payout': 1}
        record = project(row)
        market = record['market']
        self.assertNotIn('outcome', market['route'])
        self.assertNotIn('winner', market['route']['evidence'])
        self.assertNotIn('winningSide', market['order_book_snapshot']['data'])
        self.assertNotIn('result', market['pre_logic_order_book_snapshot']['data'])
        self.assertNotIn('payout', market['order_book'])
        self.assertGreaterEqual(sum(x['category'] == 'outcome_or_future'
                                    for x in record['sanitization']['omitted_fields']), 5)
        market['volume'] = 56
        self.assertFalse(verify_replay_decision_input_record_v1(record))

    def test_direct_book_matching_post_logic_data_is_not_projected(self):
        row = _snapshot_row()
        row['decision_artifact']['post_logic_order_book_snapshot'] = {
            'source': 'book', 'data': {'best_yes_ask': .44, 'spread_pct': 3}}
        row['decision_artifact']['order_book'] = {'best_yes_ask': .44, 'spread_pct': 3}
        projected = project(row)
        self.assertNotIn('order_book', projected['market'])
        self.assertIn({'path': 'decision_artifact.order_book', 'category': 'post_logic_book'},
                      projected['sanitization']['omitted_fields'])
        candidate(row)
        del row['decision_artifact']['post_logic_order_book_snapshot']
        projected = project(row)
        self.assertNotIn('order_book', projected['market'])
        self.assertIn({'path': 'decision_artifact.order_book', 'category': 'post_logic_book'},
                      projected['sanitization']['omitted_fields'])

    def test_post_logic_books_never_enter_usable_projection(self):
        from bot.prediction_lab_replay import _recorded_order_book
        for explicit_stage in (True, False):
            with self.subTest(explicit_stage=explicit_stage):
                row = _snapshot_row()
                post = {'source': 'book', 'data': {'best_yes_ask': .44}}
                if explicit_stage:
                    post['stage'] = 'post_logic'
                row['decision_artifact']['post_logic_order_book_snapshot'] = post
                shared = candidate(row)
                self.assertEqual(shared['prices']['order_book_snapshot'], post)
                projected = project(row)
                market = projected['market']
                self.assertNotIn('order_book_snapshot', market)
                self.assertNotEqual((_recorded_order_book(market) or {}).get('best_yes_ask'), .44)
                self.assertIn({'path': 'shared_candidate.prices.order_book_snapshot',
                               'category': 'post_logic_book'}, projected['sanitization']['omitted_fields'])
        row = _snapshot_row()
        row['decision_artifact']['order_book_snapshot'] = {
            'source': 'book', 'stage': 'post_logic', 'data': {'best_yes_ask': .44}}
        self.assertNotIn('order_book_snapshot', project(row)['market'])
