import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from bot.weather.collector_source_router_replay import _sealed_decision

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/research_feedback.py'


def encode(rows):
    return b''.join((json.dumps(row, sort_keys=True) + '\n').encode() for row in rows)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class ResearchFeedbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / 'collector/data/derived_reports/research'
        self.store.mkdir(parents=True)
        self.output = self.root / 'collector/data/derived_reports/feedback/result.json'
        self.output.parent.mkdir()
        self.empty_hash = digest(b'')
        self.input_hash = 'e' * 64
        self.key = {'shared_snapshot_id': 'snapshot', 'shared_candidate_id': 'candidate', 'market_id': 'market',
                    'observed_at_utc': '2026-01-01T12:00:00+00:00', 'raw_row_sha256': 'a' * 64}
        record = {'canonical_input_sha256': 'b' * 64, 'decision_key': self.key, 'market_id': 'market',
                  'observed_at': self.key['observed_at_utc'], 'market': {}}
        def decision(action):
            return _sealed_decision(record, lane_id='test', probability=.8, side='YES', action=action,
                                    entry_price=.4 if action == 'BUY_YES' else None,
                                    price_basis='recorded_executable_ask', skip_reason='test' if action == 'SKIP' else None,
                                    extra={})
        self.rows = {'baseline': [decision('BUY_YES')], 'candidate': [decision('SKIP')]}
        self.outcomes = self.root / 'outcomes.jsonl'
        self.outcomes.write_bytes(encode([{'decision_key': self.key, 'canonical_input_sha256': 'b' * 64,
                                           'market_id': 'market', 'market_status': 'finalized',
                                           'official_outcome': 'NO', 'settlement_ts': '2026-01-02T12:00:00+00:00',
                                           'resolution_id': 'resolution'}]))
        with sqlite3.connect(self.store / 'experiments.sqlite3') as db:
            db.execute('CREATE TABLE experiments (experiment_id TEXT PRIMARY KEY, git_commit TEXT, input_sha256 TEXT, outcome_sha256 TEXT, config_json TEXT, result_json TEXT, output_dir TEXT)')
            for name, rows in self.rows.items():
                directory = self.store / 'runs' / name
                directory.mkdir(parents=True)
                raw = encode(rows)
                (directory / 'sealed_control_decisions.jsonl').write_bytes(raw)
                config = {'min_sample_count': 5, 'max_rows': 1, 'history': None, 'cohort_per_shape_target': 30}
                result = {'artifacts': {'sealed_control_decisions.jsonl': digest(raw)}, 'economic_status':'excluded_quote_only'}
                db.execute('INSERT INTO experiments VALUES (?,?,?,?,?,?,?)', (name, 'c'*40, self.input_hash,
                           self.empty_hash, json.dumps(config), json.dumps(result), str(directory)))

    def run_cli(self, *, outcome_hash=None, output=None):
        env = dict(os.environ, HOME=str(self.root), TMPDIR=str(self.root),
                   PREDICTION_BOT_COLLECTOR_ROOT=str(self.root / 'collector'), PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT))
        return subprocess.run([sys.executable, str(SCRIPT), '--store', str(self.store), '--baseline', 'baseline',
            '--candidate', 'candidate', '--outcomes', str(self.outcomes), '--outcome-sha256', outcome_hash or digest(self.outcomes.read_bytes()),
            '--max-outcome-bytes', '65536', '--max-decision-bytes', '65536', '--output', str(output or self.output)],
            env=env, cwd=ROOT, capture_output=True, text=True)

    def test_scores_later_outcome_after_empty_outcome_decision_runs(self):
        run = self.run_cli()
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(self.output.read_text())
        self.assertFalse(result['diagnostic_eligible'])
        self.assertEqual(result['runs'][0]['directionally_incorrect_rows'], 1)
        self.assertEqual(result['runs'][1]['resolved_skip_rows'], 1)
        self.assertEqual(result['changed_resolved_decisions'][0]['baseline_direction'], 'incorrect')
        self.assertEqual(result['changed_resolved_decisions'][0]['count'], 1)
        self.assertNotIn('pnl', json.dumps(result).lower())
        self.assertEqual(self.outcomes.read_bytes(), encode([{'decision_key': self.key, 'canonical_input_sha256': 'b' * 64,
                 'market_id': 'market', 'market_status': 'finalized', 'official_outcome': 'NO',
                 'settlement_ts': '2026-01-02T12:00:00+00:00', 'resolution_id': 'resolution'}]))

    def test_rejects_changed_artifact_and_wrong_outcome_digest_without_output(self):
        (self.store / 'runs/baseline/sealed_control_decisions.jsonl').write_text('{}\n')
        run = self.run_cli()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('artifact hash mismatch', run.stderr)
        self.assertFalse(self.output.exists())
        (self.store / 'runs/baseline/sealed_control_decisions.jsonl').write_bytes(encode(self.rows['baseline']))
        run = self.run_cli(outcome_hash='0'*64)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('outcome hash mismatch', run.stderr)
        self.assertFalse(self.output.exists())

    def test_mismatched_identities_do_not_create_report(self):
        changed = dict(self.rows['candidate'][0], canonical_input_sha256='d' * 64)
        raw = encode([changed])
        (self.store / 'runs/candidate/sealed_control_decisions.jsonl').write_bytes(raw)
        with sqlite3.connect(self.store / 'experiments.sqlite3') as db:
            result = json.loads(db.execute("SELECT result_json FROM experiments WHERE experiment_id='candidate'").fetchone()[0])
            result['artifacts']['sealed_control_decisions.jsonl'] = digest(raw)
            db.execute("UPDATE experiments SET result_json=? WHERE experiment_id='candidate'", (json.dumps(result),))
        run = self.run_cli()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('selected decision identities differ', run.stderr)
        self.assertFalse(self.output.exists())

    def test_inconsistent_buy_side_is_not_reported_as_correct(self):
        row = dict(self.rows['baseline'][0], side='NO')
        row.pop('decision_id')
        row['decision_id'] = 'sha256:' + hashlib.sha256(json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        raw = encode([row])
        (self.store / 'runs/baseline/sealed_control_decisions.jsonl').write_bytes(raw)
        with sqlite3.connect(self.store / 'experiments.sqlite3') as db:
            result = json.loads(db.execute("SELECT result_json FROM experiments WHERE experiment_id='baseline'").fetchone()[0])
            result['artifacts']['sealed_control_decisions.jsonl'] = digest(raw)
            db.execute("UPDATE experiments SET result_json=? WHERE experiment_id='baseline'", (json.dumps(result),))
        run = self.run_cli()
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(self.output.read_text())
        self.assertEqual(result['runs'][0]['resolved_buy_rows'], 0)
        self.assertEqual(result['runs'][0]['invalid_action_side_rows'], 1)
        self.assertEqual(result['changed_resolved_decisions'][0]['baseline_direction'], 'not_scored')

    def test_empty_later_outcomes_are_reported_as_unresolved(self):
        self.outcomes.write_bytes(b'')
        run = self.run_cli()
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(self.output.read_text())
        self.assertEqual(result['outcome_rows'], 0)
        self.assertEqual(result['runs'][0]['resolved_rows'], 0)
        self.assertEqual(result['runs'][0]['unresolved_rows'], 1)
        self.assertEqual(result['changed_resolved_decisions'], [])

    def test_refuses_original_nonempty_outcomes_and_preserves_existing_output(self):
        with sqlite3.connect(self.store / 'experiments.sqlite3') as db:
            db.execute("UPDATE experiments SET outcome_sha256=? WHERE experiment_id='candidate'", ('d'*64,))
        run = self.run_cli()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('original outcome ledger was not empty', run.stderr)
        self.assertFalse(self.output.exists())
        with sqlite3.connect(self.store / 'experiments.sqlite3') as db:
            db.execute("UPDATE experiments SET outcome_sha256=? WHERE experiment_id='candidate'", (self.empty_hash,))
        self.output.write_text('preserve')
        run = self.run_cli()
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(self.output.read_text(), 'preserve')


if __name__ == '__main__':
    unittest.main()
