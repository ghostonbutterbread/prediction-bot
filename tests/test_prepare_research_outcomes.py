import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/prepare_research_outcomes.py'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical_input():
    key = {'shared_snapshot_id': 'snapshot', 'shared_candidate_id': 'candidate', 'market_id': 'market',
           'observed_at_utc': '2026-08-01T12:00:00+00:00', 'raw_row_sha256': 'a' * 64}
    row = {'schema_name': 'replay_decision_input', 'schema_version': 1, 'decision_key': key, 'market_id': 'market'}
    row['canonical_input_sha256'] = sha(json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
    return row


def jsonl(row):
    return (json.dumps(row, sort_keys=True) + '\n').encode()


class PrepareResearchOutcomesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'sitecustomize.py').write_text(
            "import socket\n"
            "def blocked(*args, **kwargs): raise RuntimeError('network disabled in synthetic tests')\n"
            "class OfflineSocket(socket.socket):\n"
            "    connect = connect_ex = bind = listen = blocked\n"
            "socket.socket = OfflineSocket\nsocket.create_connection = blocked\n")
        self.derived = self.root / 'collector/data/derived_reports'
        self.derived.mkdir(parents=True)
        self.inputs = self.root / 'inputs.jsonl'
        self.inputs.write_bytes(jsonl(canonical_input()))
        self.resolutions = self.root / 'resolutions.jsonl'
        self.resolutions.write_bytes(jsonl({'market_id':'market','requested_market_id':'market','returned_market_id':'market',
            'market_status':'finalized','settlement_ts':'2026-08-10T01:02:03Z','resolution_id':'resolution','kalshi_result':'yes'}))
        self.comparison = self.root / 'comparison.json'
        coverage = {'input_records_seen': 1, 'input_records_invalid_identity': 0,
                    'sealed_control_decisions': 1, 'sealed_candidate_decisions': 1}
        self.comparison.write_text(json.dumps({'selected_rows_matched':True,'input_sha256':sha(self.inputs.read_bytes()),
            'matched_decisions': 1, 'outcome_sha256':sha(b''),
            'runs':[{'experiment_id':'baseline', 'coverage':coverage},
                    {'experiment_id':'candidate', 'coverage':coverage}]}))
        self.output = self.derived / 'outcomes'

    def run_cli(self, resolution_hash=None):
        env = dict(os.environ, HOME=str(self.root), TMPDIR=str(self.root), PYTHONPATH=os.pathsep.join((str(ROOT), str(self.root))), PYTHONDONTWRITEBYTECODE='1',
                   PREDICTION_BOT_COLLECTOR_ROOT=str(self.root / 'collector'))
        return subprocess.run([sys.executable, str(SCRIPT), '--comparison', str(self.comparison), '--inputs', str(self.inputs),
            '--resolutions', str(self.resolutions), '--resolution-sha256', resolution_hash or sha(self.resolutions.read_bytes()), '--max-input-bytes', '65536', '--max-resolution-bytes', '65536',
            '--output', str(self.output)], env=env, cwd=ROOT, capture_output=True, text=True)

    def test_binds_outcome_after_recorded_matched_empty_outcome_comparison(self):
        inputs_before, resolution_before = self.inputs.read_bytes(), self.resolutions.read_bytes()
        run = self.run_cli()
        self.assertEqual(run.returncode, 0, run.stderr)
        receipt = json.loads((self.output / 'receipt.json').read_text())
        rows = [json.loads(line) for line in (self.output / 'binding/finalized_replay_outcomes.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['official_outcome'], 'YES')
        self.assertFalse(receipt['diagnostic_eligible'])
        self.assertEqual(receipt['resolution_sha256'], sha(resolution_before))
        self.assertEqual(receipt['binding_counts']['bound'], 1)
        self.assertEqual(self.inputs.read_bytes(), inputs_before)
        self.assertEqual(self.resolutions.read_bytes(), resolution_before)

    def test_comparison_mismatch_or_nonempty_original_outcome_stops_before_output(self):
        comp = json.loads(self.comparison.read_text())
        comp['input_sha256'] = '0' * 64
        self.comparison.write_text(json.dumps(comp))
        run = self.run_cli()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('input hash mismatch', run.stderr)
        self.assertFalse(self.output.exists())
        comp['input_sha256'] = sha(self.inputs.read_bytes())
        comp['outcome_sha256'] = '1' * 64
        self.comparison.write_text(json.dumps(comp))
        run = self.run_cli()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('original outcomes must be empty', run.stderr)
        self.assertFalse(self.output.exists())

    def test_actual_comparator_rejects_excess_unselected_input(self):
        # The dependency can be supplied from the integrated parent while this
        # child remains unmerged; after integration the default is the local CLI.
        revision = os.environ.get('TEST_DAILY_COMPARATOR_REV')
        comparator = (subprocess.run(['git', 'show', f'{revision}:scripts/auto_research.py'], cwd=ROOT,
                      check=True, capture_output=True).stdout if revision else
                      (ROOT / 'scripts/auto_research.py').read_bytes())
        repo = self.root / 'synthetic-repo'
        repo.mkdir()
        shutil.copytree(ROOT / 'bot', repo / 'bot', ignore=shutil.ignore_patterns('__pycache__'))
        (repo / 'scripts').mkdir()
        (repo / 'scripts/auto_research.py').write_bytes(comparator)
        env = dict(os.environ, HOME=str(self.root), TMPDIR=str(self.root), PYTHONPATH=os.pathsep.join((str(repo), str(self.root))),
                   PYTHONDONTWRITEBYTECODE='1', PREDICTION_BOT_COLLECTOR_ROOT=str(self.root / 'collector'),
                   GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                   GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        def run(command):
            completed = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return completed
        run(['git', 'init', '-q'])
        run(['git', 'add', 'bot', 'scripts'])
        run(['git', 'commit', '-qm', 'synthetic comparator'])
        first = canonical_input()
        second = canonical_input()
        second['decision_key']['shared_candidate_id'] = 'candidate-2'
        second['decision_key']['market_id'] = second['market_id'] = 'market-2'
        second.pop('canonical_input_sha256')
        second['canonical_input_sha256'] = sha(json.dumps(second, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
        self.inputs.write_bytes(jsonl(first) + jsonl(second))
        self.resolutions.write_bytes(self.resolutions.read_bytes() + jsonl({
            'market_id':'market-2', 'requested_market_id':'market-2', 'returned_market_id':'market-2',
            'market_status':'finalized', 'settlement_ts':'2026-08-10T01:02:03Z',
            'resolution_id':'resolution-2', 'kalshi_result':'no'}))
        empty = self.root / 'empty.jsonl'
        empty.write_bytes(b'')
        store = self.derived / 'experiments'
        cmd = [sys.executable, str(repo / 'scripts/auto_research.py')]
        ids = []
        for idea in ('baseline', 'candidate'):
            output = run(cmd + ['run', '--store', str(store), '--inputs', str(self.inputs), '--outcomes', str(empty),
                                '--idea', idea, '--max-rows', '1', '--max-input-bytes', '65536',
                                '--max-outcome-bytes', '65536'])
            ids.append(json.loads(output.stdout)['experiment_id'])
        comparison = run(cmd + ['compare', '--store', str(store), *ids])
        receipt = json.loads(comparison.stdout)
        self.assertTrue(receipt['selected_rows_matched'])
        self.assertEqual(receipt['matched_decisions'], 1)
        self.assertEqual(receipt['input_sha256'], sha(self.inputs.read_bytes()))
        self.comparison.write_text(comparison.stdout)
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('selected feed page', result.stderr)
        self.assertFalse(self.output.exists())
        # A selected page must be compared anew; truncating the hashed file
        # after comparison is not a substitute for a new comparison.
        self.inputs.write_bytes(b'\n' + jsonl(first) + b'  \n')
        stale = self.run_cli()
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn('input hash mismatch', stale.stderr)
        self.assertFalse(self.output.exists())
        ids = []
        for idea in ('page baseline', 'page candidate'):
            completed = run(cmd + ['run', '--store', str(store), '--inputs', str(self.inputs),
                                   '--outcomes', str(empty), '--idea', idea, '--max-rows', '1',
                                   '--max-input-bytes', '65536', '--max-outcome-bytes', '65536'])
            ids.append(json.loads(completed.stdout)['experiment_id'])
        comparison = run(cmd + ['compare', '--store', str(store), *ids])
        self.comparison.write_text(comparison.stdout)
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        bound = json.loads((self.output / 'receipt.json').read_text())
        self.assertEqual(bound['binding_counts']['candidate'], 1)
        self.assertEqual(bound['binding_counts']['bound'], 1)
        self.assertFalse(bound['diagnostic_eligible'])
        # The complete two-row feed is also valid when both rows were compared.
        self.inputs.write_bytes(jsonl(first) + jsonl(second))
        ids = []
        for idea in ('full baseline', 'full candidate'):
            completed = run(cmd + ['run', '--store', str(store), '--inputs', str(self.inputs),
                                   '--outcomes', str(empty), '--idea', idea,
                                   '--max-input-bytes', '65536', '--max-outcome-bytes', '65536'])
            ids.append(json.loads(completed.stdout)['experiment_id'])
        comparison = run(cmd + ['compare', '--store', str(store), *ids])
        self.assertEqual(json.loads(comparison.stdout)['matched_decisions'], 2)
        self.comparison.write_text(comparison.stdout)
        self.output = self.derived / 'two-row-outcomes'
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        bound = json.loads((self.output / 'receipt.json').read_text())
        self.assertEqual(bound['binding_counts']['candidate'], 2)
        self.assertEqual(bound['binding_counts']['bound'], 2)

    def test_rejects_missing_or_inconsistent_comparison_counts(self):
        original = json.loads(self.comparison.read_text())
        cases = [
            ('missing matched', lambda c: c.pop('matched_decisions')),
            ('zero matched', lambda c: c.update(matched_decisions=0)),
            ('boolean matched', lambda c: c.update(matched_decisions=True)),
            ('missing coverage', lambda c: c['runs'][0].pop('coverage')),
            ('missing field', lambda c: c['runs'][1]['coverage'].pop('sealed_control_decisions')),
            ('wrong seen', lambda c: c['runs'][0]['coverage'].update(input_records_seen=2)),
            ('wrong control', lambda c: c['runs'][0]['coverage'].update(sealed_control_decisions=0)),
            ('wrong candidate', lambda c: c['runs'][1]['coverage'].update(sealed_candidate_decisions=2)),
            ('invalid identity', lambda c: c['runs'][1]['coverage'].update(input_records_invalid_identity=1)),
            ('boolean count', lambda c: c['runs'][1]['coverage'].update(input_records_seen=True)),
            ('boolean invalid', lambda c: c['runs'][0]['coverage'].update(input_records_invalid_identity=False)),
        ]
        for name, mutate in cases:
            with self.subTest(name=name):
                comp = json.loads(json.dumps(original))
                mutate(comp)
                self.comparison.write_text(json.dumps(comp))
                run = self.run_cli()
                try:
                    self.assertNotEqual(run.returncode, 0, run.stdout)
                    self.assertFalse(self.output.exists())
                finally:
                    if self.output.exists():
                        shutil.rmtree(self.output)

    def test_comparison_requires_both_content_hashes(self):
        original = json.loads(self.comparison.read_text())
        for key in ('input_sha256', 'outcome_sha256'):
            with self.subTest(missing=key):
                comp = dict(original)
                comp.pop(key)
                self.comparison.write_text(json.dumps(comp))
                run = self.run_cli()
                self.assertNotEqual(run.returncode, 0, run.stdout)
                self.assertFalse(self.output.exists())

    def test_rejects_non_json_or_non_object_input_rows(self):
        original = json.loads(self.comparison.read_text())
        for data in (b'null\n', b'[]\n', b'{"value":NaN}\n', b'{"value":Infinity}\n', b'{broken}\n'):
            with self.subTest(data=data):
                self.inputs.write_bytes(data)
                comp = dict(original, input_sha256=sha(data))
                self.comparison.write_text(json.dumps(comp))
                run = self.run_cli()
                try:
                    self.assertNotEqual(run.returncode, 0, run.stdout)
                    self.assertFalse(self.output.exists())
                finally:
                    if self.output.exists():
                        shutil.rmtree(self.output)

    def test_requires_two_distinct_named_experiments(self):
        original = json.loads(self.comparison.read_text())
        for value in ('baseline', '', '  ', None):
            with self.subTest(candidate=value):
                comp = json.loads(json.dumps(original))
                comp['runs'][1]['experiment_id'] = value
                self.comparison.write_text(json.dumps(comp))
                run = self.run_cli()
                try:
                    self.assertNotEqual(run.returncode, 0, run.stdout)
                    self.assertFalse(self.output.exists())
                finally:
                    if self.output.exists():
                        shutil.rmtree(self.output)

    def test_rejects_hardlinked_evidence_paths(self):
        for first, second in ((self.inputs, self.resolutions), (self.comparison, self.inputs),
                              (self.comparison, self.resolutions)):
            with self.subTest(first=first.name, second=second.name):
                before = second.read_bytes()
                second.unlink()
                os.link(first, second)
                try:
                    run = self.run_cli()
                    self.assertNotEqual(run.returncode, 0, run.stdout)
                    self.assertIn('distinct files', run.stderr)
                    self.assertFalse(self.output.exists())
                finally:
                    second.unlink()
                    second.write_bytes(before)
                    if self.output.exists():
                        shutil.rmtree(self.output)

    def test_existing_output_is_never_overwritten(self):
        self.output.mkdir()
        (self.output / 'sentinel').write_text('keep')
        run = self.run_cli()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('output already exists', run.stderr)
        self.assertEqual((self.output / 'sentinel').read_text(), 'keep')

    def test_wrong_resolution_digest_and_incomplete_comparison_are_rejected(self):
        run = self.run_cli(resolution_hash='f' * 64)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('resolution hash mismatch', run.stderr)
        self.assertFalse(self.output.exists())
        comp = json.loads(self.comparison.read_text())
        comp['selected_rows_matched'] = False
        self.comparison.write_text(json.dumps(comp))
        run = self.run_cli()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('matched sealed comparison is required', run.stderr)
        self.assertFalse(self.output.exists())

    def test_empty_resolution_feed_reports_zero_bound(self):
        self.resolutions.write_bytes(b'')
        run = self.run_cli()
        self.assertEqual(run.returncode, 0, run.stderr)
        receipt = json.loads((self.output / 'receipt.json').read_text())
        self.assertEqual(receipt['binding_counts']['bound'], 0)
        self.assertEqual(receipt['binding_counts']['unbound'], 1)
        self.assertEqual((self.output / 'binding/finalized_replay_outcomes.jsonl').read_bytes(), b'')


if __name__ == '__main__':
    unittest.main()
