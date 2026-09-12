"""Independent-review regressions through parser and both history consumers."""
import json
from pathlib import Path
import tempfile
import unittest

from bot.auto_source_router_promotion import auto_populate_source_router_history, load_verified_strict_scorecard_rows
from bot.collector_replay_index import build_collector_replay_index
from bot.weather.source_router_direct_history import load_direct_strict_source_history
from bot.weather.thresholds import extract_forecast_interval, infer_range_forecast_outcome
from test_auto_source_router_promotion import _strict_resolution, _write_jsonl
from test_source_history_ranges import range_row


QUESTION = 'Will Seattle high temperature be 0-1°F?'
CASES = [
    ('subtitle_open_endpoints', QUESTION,
     {'yes_subtitle': 'Temperature greater than 0°F and less than 1°F.'}, 0, 'supported', 'NO'),
    ('subtitle_conflicting_bounds', QUESTION,
     {'yes_subtitle': 'Temperature greater than 2°F and less than 3°F.'}, .5, 'range_conflicting_bounds', None),
    ('rules_wrong_measurement', QUESTION,
     {'rules_primary': 'Minimum temperature between 0 and 1°F inclusive.'}, .5, 'range_conflicting_measurement', None),
    ('unsupported_unit', 'Will Seattle high temperature be 0-1°K?', {}, .5, 'range_unsupported_unit', None),
    ('conditional_question', 'Will Seattle high temperature be 0-1°F or 4°F?', {}, 4, 'range_ambiguous_rules', None),
    ('question_comparisons', 'Will Seattle high temperature be greater than 0°F and less than 1°F?',
     {'floor_strike': 0, 'cap_strike': 1}, 0, 'supported', 'NO'),
    ('question_conflicting_comparisons', 'Will Seattle high temperature be greater than 2°F and less than 3°F?',
     {'floor_strike': 0, 'cap_strike': 1}, .5, 'range_conflicting_bounds', None),
]


class ReviewRangeTests(unittest.TestCase):
    def test_parser_review_cases(self):
        for name, question, metadata, forecast, status, implied in CASES:
            with self.subTest(case=name):
                interval = extract_forecast_interval(question, metadata)
                self.assertEqual(interval['status'], status)
                self.assertEqual(infer_range_forecast_outcome(forecast, interval), implied)

    def test_review_cases_through_writer_and_direct_history(self):
        for name, question, metadata, forecast, status, implied in CASES:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                row = range_row()
                row['question'] = question
                data = row['decision_artifact']['source_context']['data']
                data['market_metadata'].update(metadata)
                data['weather_source_snapshot']['question'] = question
                data['weather_source_snapshot']['sources'][0]['forecast_high'] = forecast
                raw, resolutions = root/'raw.jsonl', root/'resolutions.jsonl'
                _write_jsonl(raw, [row])
                _write_jsonl(resolutions, [_strict_resolution(row['market_id'])])
                before = raw.read_bytes(), resolutions.read_bytes()
                writer = auto_populate_source_router_history(collector_snapshots_path=raw,
                    strict_resolutions_path=resolutions, output_root=root/'data/derived_reports/history', storage_root=root)
                manifest_data = json.loads(writer.manifest_path.read_text())
                self.assertEqual(manifest_data['source_projection'], 'all_candidates_range_forecast_v3')
                history = [json.loads(x) for x in writer.history_path.read_text().splitlines()]
                scores = load_verified_strict_scorecard_rows(writer.scoreboard_path)
                expected_count = int(implied is not None)
                self.assertEqual(len(history), expected_count)
                self.assertEqual(sum(r['sample_count'] for r in scores or []), expected_count)
                if history:
                    self.assertEqual(history[0]['source_implied_outcome'], implied)
                    self.assertEqual(history[0]['market_kind'], 'high')
                    self.assertEqual(history[0]['forecast_interval']['status'], status)
                index, manifest = root/'index.jsonl', root/'manifest.json'
                build_collector_replay_index(raw, index, manifest)
                direct = load_direct_strict_source_history(index_path=index, manifest_path=manifest,
                    strict_resolutions_path=resolutions, as_of_decision_time='2026-08-06T00:00:00Z')
                self.assertEqual(sum(r['sample_count'] for r in direct.scorecard_rows), expected_count)
                def semantic(items):
                    items = json.loads(json.dumps(items))
                    for item in items:
                        item['provenance'].pop('source_history_sha256', None)
                    return items
                self.assertEqual(semantic(scores or []), semantic(direct.scorecard_rows))
                self.assertEqual((raw.read_bytes(), resolutions.read_bytes()), before)

    def test_comparisons_and_valid_shapes_across_origins(self):
        for origin in ('question', 'subtitle', 'market_subtitle', 'yes_subtitle', 'rules_primary', 'rules_secondary'):
            for text in ('Temperature greater than 0°F and less than or equal to 1°F.',
                         'Maximum temperature between 0 and 1°F inclusive.'):
                with self.subTest(origin=origin, text=text):
                    metadata = {'floor_strike': 0, 'cap_strike': 1, 'market_kind': 'high'}
                    question = QUESTION
                    if origin == 'question':
                        question = text
                    else:
                        metadata[origin] = text
                    interval = extract_forecast_interval(question, metadata)
                    self.assertEqual(interval['status'], 'supported')
                    self.assertEqual(infer_range_forecast_outcome(1, interval), 'YES')
                    self.assertEqual(infer_range_forecast_outcome(0, interval), 'NO' if 'greater' in text else 'YES')
        for unit in ('°', '°F', 'degrees Fahrenheit'):
            self.assertEqual(extract_forecast_interval(f'Will Seattle high temperature be 0-1{unit} on September 12?')['status'], 'supported')
        for unit in ('°K', '°Kelvin', '°Rankine', 'degrees Kelvin'):
            self.assertEqual(extract_forecast_interval(f'Temperature 0-1{unit}')['status'], 'range_unsupported_unit')
        self.assertEqual(extract_forecast_interval('', {'market_kind': 'high',
            'rules_primary': 'Minimum temperature between 0 and 1°F inclusive.'})['status'], 'range_conflicting_measurement')
        self.assertEqual(extract_forecast_interval('Will Seattle low temperature be 0-1°F?', {
            'rules_primary': 'Maximum temperature between 0 and 1°F inclusive.'})['status'], 'range_conflicting_measurement')


if __name__ == '__main__':
    unittest.main()
