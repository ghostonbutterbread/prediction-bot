"""Control-lane parameters ("lane files") for the source-probability research replay."""
import unittest

from bot.weather.collector_source_router_replay import (
    build_sealed_source_probability_decisions,
    normalize_source_probability_parameters,
    source_probability_parameters_sha256,
)
from tests.test_collector_source_router_replay import replay_input


def control(records, parameters=None):
    decisions, _, stats = build_sealed_source_probability_decisions(records, [], control_parameters=parameters)
    return decisions, stats


def rows():
    # YES at p=0.80 (ask 0.40, edge 0.40); NO at p=0.20 (NO prob 0.80, ask 0.60, edge 0.20);
    # p=0.42 -> NO prob 0.58 vs ask 0.60 -> below the edge floor (SKIP).
    return [
        replay_input(market_id='KXHIGHMIA-26AUG12-B80.5', observed_at='2026-08-12T15:00:00+00:00', raw_hash='a' * 64,
                     predicted_prob=0.80, event_ticker='KXHIGHMIA-26AUG12'),
        replay_input(market_id='KXHIGHMIA-26AUG12-B82.5', observed_at='2026-08-12T15:01:00+00:00', raw_hash='b' * 64,
                     predicted_prob=0.20, event_ticker='KXHIGHMIA-26AUG12', contract_shape='threshold'),
        replay_input(market_id='KXHIGHMIA-26AUG12-B84.5', observed_at='2026-08-12T15:02:00+00:00', raw_hash='c' * 64,
                     predicted_prob=0.42, event_ticker='KXHIGHMIA-26AUG12'),
    ]


class SourceProbabilityParameterTests(unittest.TestCase):
    def test_no_parameters_is_byte_identical_to_the_default_policy(self):
        default, stats = control(rows())
        empty, empty_stats = control(rows(), {})
        self.assertEqual(default, empty)
        self.assertEqual([d['action'] for d in default], ['BUY_YES', 'BUY_NO', 'SKIP'])
        self.assertNotIn('control_parameters_sha256', default[0])
        self.assertNotIn('control_parameters_sha256', stats)
        self.assertEqual(stats, empty_stats)

    def test_side_bounds_actions_edges_and_filters_skip_with_named_reasons(self):
        cases = [
            ({'YES': {'min_probability': 0.85}}, ['SKIP', 'BUY_NO', 'SKIP'], 'parameter_yes_min_probability'),
            ({'YES': {'max_edge': 0.30}}, ['SKIP', 'BUY_NO', 'SKIP'], 'parameter_yes_max_edge'),
            ({'NO': {'max_price': 0.55}}, ['BUY_YES', 'SKIP', 'SKIP'], 'parameter_no_max_price'),
            ({'allowed_actions': ['BUY_NO']}, ['SKIP', 'BUY_NO', 'SKIP'], 'parameter_action_not_allowed'),
            ({'exclude_contract_shapes': ['threshold']}, ['BUY_YES', 'SKIP', 'SKIP'], 'parameter_contract_shape_excluded'),
            ({'include_cities': ['chicago_il']}, ['SKIP', 'SKIP', 'SKIP'], 'parameter_city_not_included'),
        ]
        for parameters, actions, reason in cases:
            with self.subTest(parameters=parameters):
                decisions, stats = control(rows(), parameters)
                self.assertEqual([d['action'] for d in decisions], actions)
                self.assertIn(reason, {d['skip_reason'] for d in decisions})
                digest = source_probability_parameters_sha256(normalize_source_probability_parameters(parameters))
                self.assertEqual({d['control_parameters_sha256'] for d in decisions}, {digest})
                self.assertEqual(stats['control_parameters_sha256'], digest)
        # A higher edge floor skips the NO buy (edge 0.20) with the original reason name.
        decisions, _ = control(rows(), {'min_edge': 0.25})
        self.assertEqual([d['action'] for d in decisions], ['BUY_YES', 'SKIP', 'SKIP'])
        self.assertEqual(decisions[1]['skip_reason'], 'below_source_probability_edge_threshold')

    def test_candidate_lane_is_unaffected_by_control_parameters(self):
        _, default_candidate, _ = build_sealed_source_probability_decisions(rows(), [])
        _, tuned_candidate, _ = build_sealed_source_probability_decisions(
            rows(), [], control_parameters={'allowed_actions': ['BUY_NO']})
        self.assertEqual(default_candidate, tuned_candidate)

    def test_invalid_parameters_are_rejected_with_the_offending_key(self):
        invalid = [
            ([], 'JSON object'), ({'yes_floor': 0.7}, 'yes_floor'), ({'YES': {'min_probability': 1.5}}, 'YES.min_probability'),
            ({'YES': {'min_price': 0.6, 'max_price': 0.5}}, 'YES.min_price'), ({'allowed_actions': []}, 'allowed_actions'),
            ({'allowed_actions': ['SELL']}, 'allowed_actions'), ({'exclude_cities': 'miami_fl'}, 'exclude_cities'),
            ({'min_edge': True}, 'min_edge'), ({'NO': {'colour': 1}}, 'NO.colour'),
        ]
        for parameters, message in invalid:
            with self.subTest(parameters=parameters):
                with self.assertRaisesRegex(ValueError, message):
                    normalize_source_probability_parameters(parameters)

    def test_normalized_parameters_have_a_stable_identity(self):
        a = normalize_source_probability_parameters({'exclude_cities': ['b', 'a', 'a'], 'YES': {'min_probability': 0.7}})
        b = normalize_source_probability_parameters({'YES': {'min_probability': 0.70}, 'exclude_cities': ['a', 'b']})
        self.assertEqual(a, b)
        self.assertEqual(source_probability_parameters_sha256(a), source_probability_parameters_sha256(b))


if __name__ == '__main__':
    unittest.main()
