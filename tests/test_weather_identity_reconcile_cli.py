"""Audit CLI uses real locators, a derived receipt, and the actual reader rule."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bot.collector_replay_index import build_collector_replay_index
from test_source_observation_ledger import _write_jsonl
from test_weather_identity_reconciliation import bad_labels_record, raw_from_input

ROOT = Path(__file__).resolve().parents[1]


class WeatherIdentityAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inputs = self.root / 'evidence'
        self.inputs.mkdir()
        self.archive, self.index, self.manifest = (self.inputs / name for name in ('raw.jsonl', 'index.jsonl', 'manifest.json'))
        self.output = self.root / 'data' / 'derived_reports' / 'audit'

    def dataset(self, count=1):
        row = raw_from_input(bad_labels_record())
        _write_jsonl(self.archive, [row] * count)
        build_collector_replay_index(self.archive, self.index, self.manifest)
        self.before = {path: path.read_bytes() for path in (self.archive, self.index, self.manifest)}

    def run_cli(self, *args):
        return subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/weather_identity_reconcile.py'),
            '--index', str(self.index), '--manifest', str(self.manifest),
            '--output-dir', str(self.output), *args], capture_output=True, text=True, timeout=30, cwd=ROOT)

    def report(self):
        return json.loads((self.output / 'report.json').read_text())

    def make_legacy(self, *, missing_payload_hash=False):
        payload = json.loads(self.manifest.read_text())
        payload['schema_version'] = 1
        payload['source_size_bytes'] = self.archive.stat().st_size
        payload['source_sha256'] = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        for key in ('committed_index_bytes', 'index_sha256', 'indexed_source_bytes', 'source_rows_seen'):
            payload.pop(key, None)
        if missing_payload_hash:
            entries = [json.loads(line) for line in self.index.read_text().splitlines()]
            entries[0].pop('payload_sha256')
            _write_jsonl(self.index, entries)
        self.manifest.write_text(json.dumps(payload))
        self.before = {p: p.read_bytes() for p in self.before}

    def test_empty_v2_receipt_still_requires_its_bound_raw_archive(self):
        self.dataset(0)
        self.archive.unlink()
        run = self.run_cli()
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertFalse(self.output.exists())

    def test_v2_declared_entry_count_mismatch_is_not_reported_complete(self):
        self.dataset()
        payload = json.loads(self.manifest.read_text())
        payload['indexed_rows'] += 1
        self.manifest.write_text(json.dumps(payload))
        run = self.run_cli()
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertIn('committed indexed_rows', run.stderr)
        self.assertFalse(self.output.exists())

    def test_record_size_guard_stops_before_raw_hydration(self):
        self.dataset()
        run = self.run_cli('--max-record-bytes', '1')
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertIn('record exceeds', run.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.before, {p: p.read_bytes() for p in self.before})

    def test_output_cannot_be_inside_input_evidence_even_via_symlink(self):
        self.dataset()
        link = self.root / 'derived_reports'
        link.symlink_to(self.inputs, target_is_directory=True)
        self.output = link / 'evil-audit'
        run = self.run_cli()
        self.assertEqual(run.returncode, 1)
        self.assertIn('outside every input/evidence directory', run.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.before, {p: p.read_bytes() for p in self.before})

    def test_v2_unselected_tampered_index_prefix_fails_before_hydration(self):
        self.dataset(2)
        with self.index.open('r+b') as stream:
            stream.seek(-4, os.SEEK_END)
            stream.write(b'bad')
        run = self.run_cli('--max-rows', '1')
        self.assertEqual(run.returncode, 1)
        self.assertIn('digest mismatch', run.stderr)
        self.assertFalse(self.output.exists())

    def test_legacy_nonmonotonic_appearances_are_not_claimed_as_independent_events(self):
        self.dataset(2)
        self.make_legacy()
        with self.index.open('ab') as stream:
            stream.write(self.index.read_bytes().splitlines(keepends=True)[0])
        self.before = {p: p.read_bytes() for p in self.before}
        run = self.run_cli('--max-rows', '0')
        self.assertEqual(run.returncode, 0, run.stderr)
        report = self.report()
        self.assertEqual(report['counts']['legacy_nonmonotonic_transitions'], 1)
        self.assertEqual(report['counts']['compact_entries_seen'], 3)
        self.assertEqual(report['counts']['raw_rows_reconciled'], 3)
        self.assertEqual(report['counts']['source_observations'], 6)
        self.assertEqual(report['count_unit'], 'index-entry/source-record appearances, not unique observations or independent events')
        self.assertEqual(self.before, {p: p.read_bytes() for p in self.before})

    def test_legacy_per_row_hashes_work_without_silent_v2_upgrade(self):
        self.dataset()
        self.make_legacy()
        run = self.run_cli()
        self.assertEqual(run.returncode, 0, run.stderr)
        report = self.report()
        self.assertEqual(report['input_receipt']['index_scope'], 'hash_pinned_legacy_index_at_audit')
        self.assertEqual(report['input_receipt']['consumer_compatibility'], 'legacy_forward_audit_only')
        self.assertEqual(report['counts']['source_observations'], 2)
        self.assertEqual(self.before, {p: p.read_bytes() for p in self.before})

    def test_legacy_missing_row_hash_is_unsupported_not_resealed_or_declared_proofless(self):
        self.dataset(2)
        self.make_legacy(missing_payload_hash=True)
        run = self.run_cli()
        self.assertEqual(run.returncode, 3, run.stderr)
        report = self.report()
        self.assertFalse(report['scope']['complete'])
        self.assertEqual(report['scope']['status'], 'unsupported_by_canonical_reader')
        self.assertEqual(report['counts']['raw_rows_reconciled'], 0)
        self.assertEqual(report['counts']['unsupported_missing_payload_sha256'], 1)
        self.assertEqual(report['input_receipt']['declared_source_sha256'], hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.assertFalse(report['input_receipt']['whole_source_sha256_verified'])
        self.assertEqual(self.before, {p: p.read_bytes() for p in self.before})

    def test_bounded_resumable_chunk_does_not_claim_complete_archive(self):
        self.dataset(3)
        run = self.run_cli('--start-entry', '2', '--max-rows', '1', '--sample-limit', '1')
        self.assertEqual(run.returncode, 2, run.stderr)
        report = self.report()
        self.assertFalse(report['scope']['complete'])
        self.assertEqual(report['scope']['status'], 'bounded')
        self.assertEqual(report['scope']['first_entry'], 2)
        self.assertEqual(report['scope']['last_entry'], 2)
        self.assertEqual(report['scope']['next_entry'], 3)
        self.assertEqual(report['counts']['raw_rows_reconciled'], 1)
        self.assertEqual(report['counts']['compact_entries_seen'], 3)
        self.assertTrue(all(len(values) <= 1 for values in report['diagnostic_samples'].values()))
        self.assertEqual(self.before, {p: p.read_bytes() for p in self.before})

    def test_complete_audit_uses_same_repaired_identity_without_copying_forecasts(self):
        self.dataset()
        run = self.run_cli()
        self.assertEqual(run.returncode, 0, run.stderr)
        report = self.report()
        self.assertTrue(report['scope']['complete'])
        self.assertEqual(report['counts']['raw_rows_reconciled'], 1)
        self.assertEqual(report['counts']['source_observations'], 2)
        self.assertEqual(report['counts']['predictive_proof_eligible'], 2)
        self.assertEqual(report['counts']['strict_proof_eligible'], 0)
        self.assertEqual(report['counts']['derived_market_label_repaired'], 2)
        sample = next(iter(report['diagnostic_samples'].values()))[0]
        first_locator = json.loads(self.index.read_text().splitlines()[0])
        self.assertEqual(sample['raw_locator']['payload_sha256'], first_locator['payload_sha256'])
        self.assertEqual(sample['raw_locator']['byte_offset'], first_locator['byte_offset'])
        self.assertEqual(report['input_receipt']['index_scope'], 'committed_v2_prefix')
        self.assertEqual(report['input_receipt']['manifest_sha256'], hashlib.sha256(self.before[self.manifest]).hexdigest())
        self.assertEqual((self.output / 'input_manifest.receipt.json').read_bytes(), self.before[self.manifest])
        self.assertEqual(self.before, {p: p.read_bytes() for p in self.before})
        self.assertNotIn('forecast_high', json.dumps(report))
        self.assertEqual(set(p.name for p in self.output.iterdir()), {'report.json', 'input_manifest.receipt.json'})


if __name__ == '__main__':
    unittest.main()
