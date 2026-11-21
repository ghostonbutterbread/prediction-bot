import hashlib
import json
import os
from pathlib import Path
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
        self.derived = self.root / 'collector/data/derived_reports'
        self.derived.mkdir(parents=True)
        self.inputs = self.root / 'inputs.jsonl'
        self.inputs.write_bytes(jsonl(canonical_input()))
        self.resolutions = self.root / 'resolutions.jsonl'
        self.resolutions.write_bytes(jsonl({'market_id':'market','requested_market_id':'market','returned_market_id':'market',
            'market_status':'finalized','settlement_ts':'2026-08-10T01:02:03Z','resolution_id':'resolution','kalshi_result':'yes'}))
        self.comparison = self.root / 'comparison.json'
        self.comparison.write_text(json.dumps({'selected_rows_matched':True,'input_sha256':sha(self.inputs.read_bytes()),
            'outcome_sha256':sha(b''), 'runs':[{'experiment_id':'baseline'}, {'experiment_id':'candidate'}]}))
        self.output = self.derived / 'outcomes'

    def run_cli(self, resolution_hash=None):
        env = dict(os.environ, HOME=str(self.root), TMPDIR=str(self.root), PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1',
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
