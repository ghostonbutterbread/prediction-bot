"""Range forecast classification, not exchange observation quantization."""
import copy
import unittest

from bot.weather.thresholds import extract_forecast_interval, infer_range_forecast_outcome
from bot.replay_decision_input import build_replay_decision_input_v1
from bot.exchanges.kalshi import _market_from_raw
from test_auto_source_router_promotion import _collector_row


class RangeForecastTests(unittest.TestCase):
    def test_signed_range_side_and_negated_question(self):
        from bot.weather.thresholds import infer_question_side
        self.assertEqual(infer_question_side('Will temperature be -3 to -2°F?'), 'range')
        self.assertNotEqual(extract_forecast_interval('Will temperature not be 0-1°F?')['status'], 'supported')

    def test_closed_unrounded_endpoints(self):
        for lower, upper, values in [
            (92, 93, [91.5, 91.9, 92, 92.5, 93, 93.1, 93.5]),
            (-3, -2, [-3.5, -3, -2.5, -2, -1.5]),
            (0, 1, [-0.1, 0, 0.5, 1, 1.1]),
            (-0.5, 0.25, [-0.51, -0.5, 0, 0.25, 0.26]),
        ]:
            interval = extract_forecast_interval(f'Will temperature be {lower}-{upper}°?')
            self.assertEqual(interval['status'], 'supported')
            self.assertEqual(interval['forecast_transform'], 'none')
            self.assertFalse(interval['exchange_settlement_parity_verified'])
            for value in values:
                with self.subTest(lower=lower, upper=upper, value=value):
                    self.assertEqual(infer_range_forecast_outcome(value, interval),
                                     'YES' if lower <= value <= upper else 'NO')

    def test_evidence_and_rejections(self):
        cases = [
            ('temperature 93-92°', {}, 'range_reversed_bounds'),
            ('temperature 92° on 2026-09-12', {}, 'range_missing_bounds'),
            ('KXHIGHMIA-B92.5', {'threshold': 93}, 'range_missing_bounds'),
            ('temperature 92-93°', {'floor_strike': 91, 'cap_strike': 93}, 'range_conflicting_bounds'),
            ('temperature 0-1°', {'floor_strike': 0}, 'range_incomplete_strikes'),
            ('temperature 0-1°', {'floor_strike': 0, 'cap_strike': float('inf')}, 'range_nonfinite_bounds'),
            ('temperature 0-1°C', {}, 'range_unsupported_unit'),
            ('temperature 0-1°', {'rules_primary': 'Special alternate interval rules apply'}, 'range_unsupported_rules'),
            ('temperature 92-93°', {'rules_primary': 'Temperature rounded to nearest integer'}, 'range_quantization_unproven'),
        ]
        for question, metadata, reason in cases:
            with self.subTest(reason=reason):
                interval = extract_forecast_interval(question, metadata)
                self.assertEqual(interval['status'], reason)
                self.assertIsNone(infer_range_forecast_outcome(92, interval))
        self.assertEqual(extract_forecast_interval('', {'yes_subtitle': '-3 to -2°F'})['lower'], -3)
        self.assertEqual(extract_forecast_interval('', {'floor_strike': 0, 'cap_strike': 1, 'strike_type': 'between'})['lower'], 0)

    def test_recorded_comparison_rules_override_closed_default(self):
        interval = extract_forecast_interval('temperature 0-1°', {
            'rules_primary': 'Temperature greater than 0°F and less than or equal to 1°F.'})
        self.assertEqual(interval['status'], 'supported')
        self.assertEqual(infer_range_forecast_outcome(0, interval), 'NO')
        self.assertEqual(infer_range_forecast_outcome(1, interval), 'YES')
        self.assertEqual(extract_forecast_interval('temperature 0-1°', {
            'rules_primary': 'Temperature greater than 2°F and less than 3°F.'})['status'], 'range_conflicting_bounds')

    def test_contradictory_and_conditional_rules_are_not_substring_matched(self):
        for text in [
            'Temperature not greater than 0°F and less than 1°F.',
            'Temperature greater than 0°F or less than 1°F.',
            'Temperature between 0 and 1°F inclusive, except on weekends.',
            'Temperature between 0 and 1°F inclusive and exclusive.',
        ]:
            with self.subTest(text=text):
                self.assertNotEqual(extract_forecast_interval('temperature 0-1°F', {'rules_primary': text})['status'], 'supported')

    def test_returned_rules_and_strikes_survive_sanitizer(self):
        fields = {'strike_type': 'between', 'floor_strike': 0, 'cap_strike': 1,
                  'rules_primary': 'Temperature between 0 and 1 degrees Fahrenheit, inclusive.',
                  'rules_secondary': '', 'yes_subtitle': '0-1°F', 'no_subtitle': 'Outside range'}
        market = _market_from_raw({'ticker': 'KXHIGHSEA-B0.5', **fields})
        row = _collector_row()
        row['decision_artifact']['source_context']['data']['market_metadata'].update(market.metadata)
        built = build_replay_decision_input_v1(row, source_history_only=True)
        self.assertTrue(built.ok, built.errors)
        metadata = built.record['market']['market_metadata']
        for key, value in fields.items():
            self.assertEqual(metadata[key], value)
        self.assertNotIn('outcome', metadata)
        self.assertNotIn('result', metadata)


def range_row(kind='high', quoted=False):
    row = _collector_row(market_id=('KXHIGHSEA' if kind == 'high' else 'KXLOWTSEA') + '-26AUG03-B0.5')
    row['shared_snapshot_id'] = 'range-' + kind
    row['question'] = f'Will Seattle {kind} temperature be 0-1°?'
    row['main_decision'] = {'action': 'SKIP', 'reason_code': 'zero_kelly'}
    if not quoted:
        row.update(yes_price=None, no_price=None)
        row['decision_artifact']['execution_snapshot'] = {}
    data = row['decision_artifact']['source_context']['data']
    data['market_metadata'].update(market_kind=kind, contract_shape='range')
    snap = data['weather_source_snapshot']
    snap.update(question=row['question'])
    source = snap['sources'][0]
    source.update(contract_shape='range', question_side='range',
                  forecast_measurement_kind=kind, forecast_high=0.5, forecast_low=-0.5)
    source['future_outcome'] = {'result': 'OUTCOME_SENTINEL'}
    return row


class RangePipelineTests(unittest.TestCase):
    def test_missing_quote_skip_high_low_duplicate_void_and_cutoff(self):
        import hashlib
        import json
        import tempfile
        from pathlib import Path
        from bot.auto_source_router_promotion import auto_populate_source_router_history, load_verified_strict_scorecard_rows
        from bot.collector_replay_index import build_collector_replay_index
        from bot.weather.source_router_direct_history import load_direct_strict_source_history
        from bot.weather.source_observation_ledger import is_eligible_for_future_history
        from test_auto_source_router_promotion import _strict_resolution, _write_jsonl
        for quoted in (False, True):
            for void in (False, True):
                with self.subTest(quoted=quoted, void=void), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    rows = [range_row(kind, quoted) for kind in ('high', 'low')]
                    originals = copy.deepcopy(rows)
                    for row in rows:
                        built = build_replay_decision_input_v1(row, source_history_only=True)
                        self.assertTrue(built.ok, built.errors)
                        self.assertNotIn('OUTCOME_SENTINEL', built.canonical_input_json.decode())
                        self.assertEqual('yes_price' in built.record['market'], quoted)
                    raw, resolutions = root/'raw.jsonl', root/'resolutions.jsonl'
                    duplicate = copy.deepcopy(rows[0])
                    duplicate['shared_snapshot_id'] = 'later-poll'
                    duplicate['observed_at'] = '2026-08-01T12:01:00+00:00'
                    _write_jsonl(raw, rows + [duplicate])
                    receipts = [_strict_resolution(r['market_id']) for r in rows]
                    if void:
                        for r in receipts:
                            r.update(kalshi_result='void')
                    _write_jsonl(resolutions, receipts)
                    before = hashlib.sha256(raw.read_bytes()).hexdigest()
                    writer = auto_populate_source_router_history(collector_snapshots_path=raw,
                        strict_resolutions_path=resolutions, output_root=root/'data/derived_reports/history', storage_root=root)
                    self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), before)
                    self.assertEqual(rows, originals)
                    history = [json.loads(x) for x in writer.history_path.read_text().splitlines()]
                    scores = load_verified_strict_scorecard_rows(writer.scoreboard_path)
                    if void:
                        self.assertFalse(history)
                        self.assertEqual(len((writer.history_path.parent / 'void_source_observations.jsonl').read_text().splitlines()), 3)
                    else:
                        self.assertEqual(len(history), 3)
                        self.assertEqual(sum(r['sample_count'] for r in scores), 2)
                        for r in history:
                            self.assertEqual(r['forecast_interval']['lower'], 0)
                            self.assertEqual(r['source_implied_outcome'], 'YES' if r['market_kind'] == 'high' else 'NO')
                            self.assertEqual(r['direction_correct'], r['market_kind'] == 'high')
                            self.assertFalse(is_eligible_for_future_history(r, '2026-08-04T00:00:00Z'))
                            self.assertTrue(is_eligible_for_future_history(r, '2026-08-04T00:00:01Z'))
                    index, manifest = root/'index.jsonl', root/'manifest.json'
                    build_collector_replay_index(raw, index, manifest)
                    direct = load_direct_strict_source_history(index_path=index, manifest_path=manifest,
                        strict_resolutions_path=resolutions, as_of_decision_time='2026-08-06T00:00:00Z')
                    def semantic(items):
                        items = copy.deepcopy(items)
                        for r in items:
                            r['provenance'].pop('source_history_sha256', None)
                        return sorted(items, key=lambda r: json.dumps(r, sort_keys=True))
                    self.assertEqual(semantic(scores or []), semantic(direct.scorecard_rows))


if __name__ == '__main__':
    unittest.main()
