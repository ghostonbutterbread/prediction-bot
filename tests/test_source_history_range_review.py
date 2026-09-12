"""Independent-review regressions through parser and both history consumers."""
import hashlib
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
# Boundary families, not a denylist of the reviewer's exact spellings/operators.
CASES += [
    (f'unsupported_{marker}_{unit}', f'Will Seattle high temperature be 0-1{marker}{unit}?',
     {}, .5, 'range_unsupported_unit', None)
    for marker in ('°', '° ', ' degrees ')
    for unit in ('R', 'K', 'C', 'Rankine', 'Kelvin', 'Celsius', 'Reaumur', 'XYZ')
]
CASES += [
    (f'trailing_{joiner}_{predicate}', f'Will Seattle high temperature be 0-1°F {joiner} {predicate}?',
     {}, 4 if joiner == '/' else .5, 'range_ambiguous_rules', None)
    for joiner in ('and', '/', 'or', ',', ';', 'plus')
    for predicate in ('4°F', '4 degrees Fahrenheit', '0-1°F')
]
CASES += [
    (f'valid_{unit}', f'Will Seattle high temperature be -1.5 to 0{unit} on August 3, 2026?',
     {}, 0, 'supported', 'YES')
    for unit in ('°', ' degrees', '°F', '° F', '°Fahrenheit', ' degrees Fahrenheit', ' F', ' Fahrenheit')
]
CASES += [
    ('valid_bare_comparisons', 'Will Seattle high temperature be greater than -1.5° and less than or equal to 0° on 2026-08-03?',
     {}, 0, 'supported', 'YES'),
]

# Unit inheritance belongs to the whole clause, including unsupported suffixes.
CASES += [
    (f'inherited_suffix_{base}_{joiner}_{suffix}',
     f'Will Seattle high temperature be {base} {joiner} {suffix}?',
     {}, .5, 'range_ambiguous_rules', None)
    for base in ('0-1°F', 'greater than 0°F and less than 1°F')
    for joiner in ('and', '/', ',', ';', 'plus')
    for suffix in ('4', '-4.5', '.4', '4-5', '-5 to -4', 'greater than 4',
                   'less than or equal to 4', '4°F')
]
CASES += [
    (f'valid_inherited_{predicate}_{context}',
     f'Will Seattle high temperature {context} be {predicate}?',
     {}, .5, 'supported', 'YES')
    for predicate in ('greater than 0°F and less than 1',
                      'greater than 0 and less than 1°F',
                      '0°F to 1', 'between 0°F and 1')
    for context in ('on August 3, 2026', 'on 2026-08-03', 'in Seattle', 'on Aug 3, 2026')
]
CASES += [
    ('dated_suffix', 'Will Seattle high temperature be 0-1°F in Seattle on August 3, 2026?',
     {}, .5, 'supported', 'YES'),
    ('predicate_after_date', 'Will Seattle high temperature be 0-1°F on August 3, 2026 and 4?',
     {}, .5, 'range_ambiguous_rules', None),
]
CASES += [
    (f'calendar_{month}_{suffix}',
     f'Will Seattle high temperature be 0-1°F on {month} 3, 2026{suffix}?',
     {}, .5, 'range_ambiguous_rules' if suffix else 'supported', None if suffix else 'YES')
    for month in ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Sept', 'Oct', 'Nov', 'Dec', 'Aug.')
    for suffix in ('', ' and 4')
]


# Every affirmative origin shares the contract unit, independent of field order.
ORIGINS = ('question', 'subtitle', 'market_subtitle', 'yes_subtitle', 'rules_primary', 'rules_secondary')
for unit_origin in ORIGINS:
    for predicate_origin in ORIGINS:
        if unit_origin == predicate_origin:
            continue
        for text, status, implied in (
            ('Temperature between 0 and 1 inclusive.', 'supported', 'YES'),
            ('Temperature greater than 0 and less than 1.', 'supported', 'YES'),
            ('Temperature between 2 and 3 inclusive.', 'range_conflicting_bounds', None),
            ('Temperature greater than 2 and less than 3.', 'range_conflicting_bounds', None),
            ('Temperature between 0 and 1°C inclusive.', 'range_unsupported_unit', None),
            ('Temperature between 0 and 1 Celsius inclusive.', 'range_unsupported_unit', None),
            ('Temperature between 0 and 1 C inclusive.', 'range_unsupported_unit', None),
            ('Minimum temperature between 0 and 1 inclusive.', 'range_conflicting_measurement', None),
        ):
            fields = {unit_origin: 'Maximum temperature 0-1°F', predicate_origin: text}
            # Returned rules use their supported direct-sentence grammar.
            if unit_origin.startswith('rules_'):
                fields[unit_origin] = 'Maximum temperature between 0 and 1°F inclusive.'
                if text == 'Temperature greater than 0 and less than 1.':
                    status, implied = 'range_conflicting_rules', None
            question = fields.pop('question', 'Will Seattle high temperature be 0-1 on August 3, 2026?')
            CASES.append((f'cross_{unit_origin}_{predicate_origin}_{text}', question, fields, .5, status, implied))
for origin in ORIGINS[1:]:
    for text in ('', 'See the station report.', 'Report published at 4 PM.', 'See pages 2-3 of the report.'):
        CASES.append((f'irrelevant_{origin}_{text}', QUESTION, {origin: text}, .5, 'supported', 'YES'))
    if not origin.startswith('rules_'):
        CASES.append((f'bare_cross_{origin}', QUESTION, {origin: '2-3'}, .5, 'range_conflicting_bounds', None))
    CASES.append((f'inclusion_conflict_{origin}',
                  'Will Seattle high temperature be greater than 0°F and less than 1°F?',
                  {origin: 'Temperature between 0 and 1 inclusive.'}, .5, 'range_conflicting_rules', None))
CASES += [
    ('no_side_prose', QUESTION, {'no_subtitle': 'Outside range'}, .5, 'supported', 'YES'),
    ('no_side_numeric', QUESTION, {'no_subtitle': '2-3'}, .5, 'range_unsupported_rules', None),
    ('point_before_range', 'Will Seattle high temperature be 4°F?', {'yes_subtitle': '0-1'}, .5, 'range_ambiguous_rules', None),
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
                self.assertEqual(manifest_data['source_projection'], 'all_candidates_range_forecast_v6')
                identity = {
                    'schema_name': 'auto_source_router_history_promotion', 'schema_version': 2,
                    'collector_snapshots_sha256': hashlib.sha256(before[0]).hexdigest(),
                    'strict_resolutions_sha256': hashlib.sha256(before[1]).hexdigest(),
                }
                def generation(version):
                    value = dict(identity, source_projection=f'all_candidates_range_forecast_v{version}')
                    return hashlib.sha256((json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()).hexdigest()
                self.assertEqual(writer.generation_dir.name, generation(6))
                for old_version in (3, 4, 5):
                    self.assertNotEqual(writer.generation_dir.name, generation(old_version))
                history = [json.loads(x) for x in writer.history_path.read_text().splitlines()]
                scores = load_verified_strict_scorecard_rows(writer.scoreboard_path)
                expected_count = int(implied is not None)
                with self.subTest(consumer='writer'):
                    self.assertEqual(len(history), expected_count)
                    self.assertEqual(sum(r['sample_count'] for r in scores or []), expected_count)
                if history and implied is not None:
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

    def test_boundary_checks_across_retained_origins(self):
        for origin in ('subtitle', 'market_subtitle', 'yes_subtitle', 'rules_primary', 'rules_secondary'):
            for text in ('Temperature 0-1° R', 'Temperature 0-1°F and 4°F', 'Temperature 0-1°F / 4°F'):
                with self.subTest(origin=origin, text=text):
                    interval = extract_forecast_interval(QUESTION, {origin: text})
                    self.assertNotEqual(interval['status'], 'supported')
                    self.assertIsNone(infer_range_forecast_outcome(.5, interval))


if __name__ == '__main__':
    unittest.main()
