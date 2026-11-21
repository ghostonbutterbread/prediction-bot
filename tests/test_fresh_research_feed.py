import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from bot.collector_replay_index import build_collector_replay_index, update_collector_replay_index
from tests.test_collector_replay_inputs import _legacy_collector_row

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts/fresh_research_feed.py'


class FreshResearchFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.raw = self.source / 'raw.jsonl'
        self.index = self.source / 'index.jsonl'
        self.manifest = self.source / 'manifest.json'
        self.out = self.root / 'derived'
        row = _legacy_collector_row()
        row['run_id'] = 'run-1'
        self.raw.write_text(json.dumps(row) + '\n')
        build_collector_replay_index(self.raw, self.index, self.manifest)

    def run_cli(self, *extra):
        return subprocess.run([sys.executable, str(CLI), '--index', str(self.index), '--manifest', str(self.manifest), '--output-root', str(self.out), '--protected-root', str(self.source), '--max-index-bytes', '2000000', '--max-inspected-rows', '5', '--max-raw-row-bytes', '200000', '--max-wall-seconds', '30', '--max-memory-bytes', '536870912', *map(str, extra)], cwd=ROOT, text=True, capture_output=True)

    def test_cli_builds_projected_snapshot_and_idempotently_reuses_it(self):
        first = self.run_cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        receipt = json.loads(first.stdout)
        directory = Path(receipt['directory'])
        result = json.loads((directory / 'receipt.json').read_text())
        inputs = (directory / 'inputs.jsonl').read_bytes()
        self.assertEqual(result['status'], 'initial')
        self.assertEqual(result['accepted_rows'], 1)
        self.assertEqual(hashlib.sha256(inputs).hexdigest(), result['inputs_sha256'])
        self.assertEqual((directory / 'manifest.json').read_bytes(), self.manifest.read_bytes())
        self.assertEqual((directory / 'outcomes.jsonl').read_bytes(), b'')
        self.assertNotIn('actual_outcome', inputs.decode())
        self.assertEqual(result['whole_index_digest_bytes'], self.index.stat().st_size)
        self.assertEqual(result['whole_index_structure_bytes'], self.index.stat().st_size)
        second = self.run_cli()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)['directory'], str(directory))

    def test_checkpoint_detects_unchanged_and_new_rows(self):
        first = self.run_cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        checkpoint = Path(json.loads(first.stdout)['directory']) / 'receipt.json'
        unchanged = self.run_cli('--previous', checkpoint)
        self.assertEqual(unchanged.returncode, 0, unchanged.stderr)
        self.assertEqual(json.loads(unchanged.stdout)['status'], 'unchanged')
        row = _legacy_collector_row(market_id='KXHIGHNY-26AUG13-T80')
        row['run_id'] = 'run-2'
        with self.raw.open('a') as f:
            f.write(json.dumps(row) + '\n')
        update_collector_replay_index(self.raw, self.index, self.manifest)
        new = self.run_cli('--previous', checkpoint)
        self.assertEqual(new.returncode, 0, new.stderr)
        receipt = json.loads((Path(json.loads(new.stdout)['directory']) / 'receipt.json').read_text())
        self.assertEqual(receipt['status'], 'new_indexed_data')
        self.assertEqual(receipt['accepted_rows'], 1)
        self.assertGreater(receipt['prior_prefix_digest_bytes'], 0)

    def test_manifest_change_without_new_indexed_data_is_not_called_new_data(self):
        first = self.run_cli()
        checkpoint = Path(json.loads(first.stdout)['directory']) / 'receipt.json'
        with self.raw.open('ab') as f:
            f.write(b'{')
        update_collector_replay_index(self.raw, self.index, self.manifest)
        result = self.run_cli('--previous', checkpoint)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'manifest_changed_no_new_indexed_data')

    def test_delta_membership_uses_index_extent_not_raw_watermark(self):
        row = _legacy_collector_row(market_id='KXHIGHNY-26AUG13-T80')
        row['run_id'] = 'run-2'
        with self.raw.open('a') as f:
            f.write(json.dumps(row) + '\n')
        update_collector_replay_index(self.raw, self.index, self.manifest)
        entries = self.index.read_bytes().splitlines(keepends=True)
        manifest = json.loads(self.manifest.read_bytes())
        self.index.write_bytes(entries[0])
        manifest['committed_index_bytes'] = len(entries[0])
        manifest['index_sha256'] = hashlib.sha256(entries[0]).hexdigest()
        manifest['indexed_rows'] = 1
        self.manifest.write_text(json.dumps(manifest))
        first = self.run_cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        checkpoint = Path(json.loads(first.stdout)['directory']) / 'receipt.json'
        self.index.write_bytes(b''.join(entries))
        manifest['committed_index_bytes'] = sum(map(len, entries))
        manifest['index_sha256'] = hashlib.sha256(b''.join(entries)).hexdigest()
        manifest['indexed_rows'] = 2
        self.manifest.write_text(json.dumps(manifest))
        second = self.run_cli('--previous', checkpoint)
        self.assertEqual(second.returncode, 0, second.stderr)
        result = json.loads((Path(json.loads(second.stdout)['directory']) / 'receipt.json').read_text())
        self.assertEqual(result['status'], 'new_indexed_data')
        self.assertEqual(result['inspected_rows'], 1)

    def test_wall_and_memory_limits_are_required(self):
        args = [sys.executable, str(CLI), '--index', str(self.index), '--manifest', str(self.manifest),
                '--output-root', str(self.out), '--protected-root', str(self.source),
                '--max-index-bytes', '2000000', '--max-inspected-rows', '5', '--max-raw-row-bytes', '200000']
        result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--max-wall-seconds', result.stderr)
        self.assertFalse(self.out.exists())

    def test_selected_raw_size_limit_and_index_corruption_fail_without_output(self):
        result = self.run_cli('--max-raw-row-bytes', '1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('raw row exceeds', result.stderr)
        self.assertFalse(self.out.exists())
        self.index.write_bytes(b'x' + self.index.read_bytes()[1:])
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('digest mismatch', result.stderr)
        self.assertFalse(self.out.exists())

    def test_truncated_bootstrap_can_checkpoint_newest_sample_without_claiming_coverage(self):
        row = _legacy_collector_row()
        row['run_id'] = 'run-2'
        with self.raw.open('a') as f:
            f.write(json.dumps(row) + '\n')
        update_collector_replay_index(self.raw, self.index, self.manifest)
        result = self.run_cli('--max-inspected-rows', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        checkpoint = Path(json.loads(result.stdout)['directory']) / 'receipt.json'
        receipt = json.loads(checkpoint.read_text())
        self.assertEqual(receipt['status'], 'truncated')
        self.assertFalse(receipt['complete'])
        self.assertTrue(receipt['checkpoint_eligible'])
        unchanged = self.run_cli('--previous', checkpoint)
        self.assertEqual(unchanged.returncode, 0, unchanged.stderr)
        self.assertEqual(json.loads(unchanged.stdout)['status'], 'unchanged')

    def test_truncated_selection_cannot_be_used_as_checkpoint(self):
        baseline = self.run_cli()
        previous = Path(json.loads(baseline.stdout)['directory']) / 'receipt.json'
        row = _legacy_collector_row()
        with self.raw.open('a') as f:
            for run in ('run-2', 'run-3'):
                row['run_id'] = run
                f.write(json.dumps(row) + '\n')
        update_collector_replay_index(self.raw, self.index, self.manifest)
        result = self.run_cli('--previous', previous, '--max-inspected-rows', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'truncated')
        checkpoint = Path(json.loads(result.stdout)['directory']) / 'receipt.json'
        receipt = json.loads(checkpoint.read_text())
        self.assertFalse(receipt['complete'])
        self.assertEqual(receipt['inspected_rows'], 1)
        result = self.run_cli('--previous', checkpoint)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not a complete', result.stderr)

    def test_prior_prefix_change_is_new_generation_even_with_valid_new_digest(self):
        result = self.run_cli()
        checkpoint = Path(json.loads(result.stdout)['directory']) / 'receipt.json'
        entry = json.loads(self.index.read_bytes())
        entry['confidence'] = 0.123
        data = (json.dumps(entry) + '\n').encode()
        self.index.write_bytes(data)
        manifest = json.loads(self.manifest.read_bytes())
        manifest['committed_index_bytes'] = len(data)
        manifest['index_sha256'] = hashlib.sha256(data).hexdigest()
        self.manifest.write_text(json.dumps(manifest))
        result = self.run_cli('--previous', checkpoint)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('new_generation', result.stderr)

    def test_explicit_invalid_checkpoint_is_not_silently_treated_as_bootstrap(self):
        previous = self.root / 'bad-receipt.json'
        previous.write_bytes(b'null')
        result = self.run_cli('--previous', previous)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('previous checkpoint', result.stderr)
        self.assertFalse(self.out.exists())

    def test_rejects_extent_cap_before_scan(self):
        result = self.run_cli('--max-index-bytes', '1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('index extent exceeds', result.stderr)
        self.assertFalse(self.out.exists())
