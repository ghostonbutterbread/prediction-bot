import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AutoResearchCLITest(unittest.TestCase):
    def test_real_replay_records_matched_decisions_and_queues_without_paper_writes(self):
        self.assertTrue((ROOT / 'scripts/auto_research.py').is_file(), 'research CLI is missing')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / 'repo'
            repo.mkdir()
            shutil.copytree(ROOT / 'bot', repo / 'bot', ignore=shutil.ignore_patterns('__pycache__'))
            (repo / 'scripts').mkdir()
            for filename in ('auto_research.py',):
                src = ROOT / 'scripts' / filename
                if src.exists():
                    shutil.copy2(src, repo / 'scripts' / filename)
            env = dict(os.environ, HOME=str(root / 'home'), TMPDIR=str(root),
                       XDG_CONFIG_HOME=str(root / 'home/config'),
                       PREDICTION_BOT_COLLECTOR_ROOT=str(root / 'collector'), PYTHONPATH=str(repo),
                       GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                       GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
            Path(env['HOME']).mkdir()
            def git(*args):
                return subprocess.run(['git', *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()
            git('init', '-q')
            git('add', 'bot', 'scripts')
            git('commit', '-qm', 'synthetic research code')
            commit = git('rev-parse', 'HEAD')
            key = {'shared_snapshot_id': 's1', 'shared_candidate_id': 'c1', 'market_id': 'KX1',
                   'observed_at_utc': '2026-01-01T12:00:00+00:00', 'raw_row_sha256': 'a' * 64}
            record = {'schema_name': 'replay_decision_input', 'schema_version': 1, 'decision_key': key,
                      'shared_snapshot_id': 's1', 'shared_candidate_id': 'c1', 'market_id': 'KX1',
                      'observed_at': key['observed_at_utc'],
                      'market': {'question': 'Will Miami high exceed 80F?', 'best_yes_ask': 0.4, 'best_no_ask': 0.6,
                                 'market_metadata': {'city_id': 'miami_fl', 'market_kind': 'high', 'contract_shape': 'range'}},
                      'source_inputs': {'source_context': {'source': 'weather', 'data': {'weather_source_snapshot': {
                          'predicted_prob': 0.8, 'forecast': {'threshold': 80.0, 'question_side': 'above'},
                          'sources': [{'source_id': 'nws', 'source_name': 'NWS', 'forecast_high': 82.0}]}}}}}
            record['canonical_input_sha256'] = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            outcome = {'schema_name': 'finalized_source_probability_outcome', 'schema_version': 1,
                       'market_status': 'finalized', 'market_id': 'KX1', 'decision_key': key,
                       'canonical_input_sha256': record['canonical_input_sha256'], 'official_outcome': 'YES',
                       'settlement_ts': '2026-01-02T12:00:00+00:00', 'resolution_id': 'r1'}
            inputs, outcomes = root / 'inputs.jsonl', root / 'outcomes.jsonl'
            inputs.write_text(json.dumps(record) + '\n')
            outcomes.write_text(json.dumps(outcome) + '\n')
            store = root / 'collector/data/derived_reports/research'
            paper = root / 'collector/data/beta_shadow/paper/champion-and-pending.json'
            paper.parent.mkdir(parents=True)
            paper.write_text('{"champion":"old-v1","pending":{"position-1":"old-v1"}}')
            paper_before = paper.read_bytes()
            cmd = [sys.executable, str(repo / 'scripts/auto_research.py')]
            oversized = subprocess.run(cmd + ['run', '--store', str(store), '--inputs', str(inputs), '--outcomes', str(outcomes), '--idea', 'bounded', '--max-input-bytes', '65536', '--max-outcome-bytes', '1'], cwd=repo, env=env, capture_output=True, text=True)
            self.assertNotEqual(oversized.returncode, 0)
            self.assertIn('outcomes exceeds --max-outcome-bytes', oversized.stderr)
            self.assertFalse(store.exists())
            run_cmd = cmd + ['run', '--max-input-bytes', '65536', '--max-outcome-bytes', '65536']
            nonobject_inputs = root / 'nonobject.jsonl'
            nonobject_inputs.write_text('null\n' + json.dumps(record) + '\n')
            nonobject_store = root / 'collector/data/derived_reports/nonobject'
            nonobject = subprocess.run(run_cmd + ['--store', str(nonobject_store), '--inputs', str(nonobject_inputs), '--outcomes', str(outcomes), '--idea', 'invalid row coverage'], cwd=repo, env=env, capture_output=True, text=True)
            self.assertNotEqual(nonobject.returncode, 0, 'non-object selected rows must not silently disappear')
            self.assertIn('non-object', nonobject.stderr)
            self.assertFalse(nonobject_store.exists())
            run = subprocess.run(run_cmd + ['--store', str(store), '--inputs', str(inputs), '--outcomes', str(outcomes), '--idea', 'synthetic idea'], cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            receipt = json.loads(run.stdout)
            self.assertEqual(receipt['git_commit'], commit)
            with sqlite3.connect(store / 'experiments.sqlite3') as db:
                rows = db.execute('SELECT git_commit, input_sha256, outcome_sha256, result_json FROM experiments').fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][:3], (commit, hashlib.sha256(inputs.read_bytes()).hexdigest(), hashlib.sha256(outcomes.read_bytes()).hexdigest()))
            result = json.loads(rows[0][3])
            self.assertFalse(result['diagnostic_eligible'])
            self.assertEqual(result['source_time_status'], 'unverified')
            self.assertEqual(result['decision_stats']['sealed_control_decisions'], 1)
            self.assertEqual(result['resolution_summary']['exactly_resolved_decisions'], 2)
            run_dir = Path(receipt['output_dir'])
            resolved_report = json.loads((run_dir / 'post_decision_resolution_report.json').read_text())
            self.assertEqual(resolved_report['economic_status'], 'excluded_quote_only')
            self.assertFalse(resolved_report['diagnostic_eligible'])
            metadata = json.loads((run_dir / 'run_metadata.json').read_text())
            self.assertFalse(metadata['diagnostic_eligible'])
            self.assertEqual(metadata['source_time_status'], 'unverified')
            self.assertNotIn('pnl', json.dumps(resolved_report).lower())
            self.assertNotIn('pnl', json.dumps(result['resolution_summary']).lower())
            control = json.loads((run_dir / 'sealed_control_decisions.jsonl').read_text())
            candidate = json.loads((run_dir / 'sealed_candidate_decisions.jsonl').read_text())
            self.assertEqual(control['canonical_input_sha256'], candidate['canonical_input_sha256'])
            self.assertEqual(control['action'], 'BUY_YES')
            self.assertEqual(candidate['action'], 'SKIP')
            self.assertNotIn('official_outcome', json.dumps(control) + json.dumps(candidate))
            source = repo / 'bot/weather/collector_source_router_replay.py'
            original = source.read_text()
            self.assertIn('MIN_ABSOLUTE_EDGE = 0.05', original)
            source.write_text(original.replace('MIN_ABSOLUTE_EDGE = 0.05', 'MIN_ABSOLUTE_EDGE = 0.50'))
            git('add', 'bot/weather/collector_source_router_replay.py')
            git('commit', '-qm', 'change edge threshold')
            second = subprocess.run(run_cmd + ['--store', str(store), '--inputs', str(inputs), '--outcomes', str(outcomes), '--idea', 'higher edge'], cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            second_receipt = json.loads(second.stdout)
            self.assertNotEqual(second_receipt['git_commit'], commit)
            second_control = json.loads((Path(second_receipt['output_dir']) / 'sealed_control_decisions.jsonl').read_text())
            self.assertEqual(second_control['canonical_input_sha256'], control['canonical_input_sha256'])
            self.assertEqual(second_control['action'], 'SKIP')
            comparison = subprocess.run(cmd + ['compare', '--store', str(store), receipt['experiment_id'], second_receipt['experiment_id']], cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(comparison.returncode, 0, comparison.stderr)
            comparison_result = json.loads(comparison.stdout)
            self.assertTrue(comparison_result['selected_rows_matched'])
            self.assertFalse(comparison_result['matched_inputs_and_settings'])
            self.assertEqual(comparison_result['runs'][0]['coverage']['input_records_seen'], 1)
            with sqlite3.connect(store / 'experiments.sqlite3') as db:
                legacy = list(db.execute('SELECT * FROM experiments WHERE experiment_id=?', (receipt['experiment_id'],)).fetchone())
                legacy[0] = 'legacy'
                legacy_result = json.loads(legacy[8])
                legacy_result.pop('economic_status')
                legacy[8] = json.dumps(legacy_result)
                db.execute('INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', legacy)
            old_compare = subprocess.run(cmd + ['compare', '--store', str(store), 'legacy', receipt['experiment_id']], cwd=repo, env=env, capture_output=True, text=True)
            self.assertNotEqual(old_compare.returncode, 0)
            self.assertIn('legacy economic results', old_compare.stderr)
            empty_inputs = root / 'empty.jsonl'
            empty_inputs.write_text('')
            empty = subprocess.run(run_cmd + ['--store', str(store), '--inputs', str(empty_inputs), '--outcomes', str(outcomes), '--idea', 'empty'], cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(empty.returncode, 0, empty.stderr)
            empty_id = json.loads(empty.stdout)['experiment_id']
            empty_comparison = subprocess.run(cmd + ['compare', '--store', str(store), empty_id, empty_id], cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(empty_comparison.returncode, 0, empty_comparison.stderr)
            self.assertFalse(json.loads(empty_comparison.stdout)['matched_inputs_and_settings'])
            invalid_record = dict(record)
            invalid_record.pop('decision_key')
            invalid_record.pop('canonical_input_sha256')
            invalid_record['canonical_input_sha256'] = hashlib.sha256(json.dumps(invalid_record, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            partial_inputs = root / 'partial.jsonl'
            partial_inputs.write_text(json.dumps(record) + '\n' + json.dumps(invalid_record) + '\n')
            partial = subprocess.run(run_cmd + ['--store', str(store), '--inputs', str(partial_inputs), '--outcomes', str(outcomes), '--idea', 'partial'], cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(partial.returncode, 0, partial.stderr)
            partial_id = json.loads(partial.stdout)['experiment_id']
            partial_compare = subprocess.run(cmd + ['compare', '--store', str(store), partial_id, partial_id], cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(partial_compare.returncode, 0, partial_compare.stderr)
            partial_result = json.loads(partial_compare.stdout)
            self.assertFalse(partial_result['selected_rows_matched'])
            self.assertEqual(partial_result['runs'][0]['coverage']['input_records_invalid_identity'], 1)
            self.assertEqual(comparison_result['runs'][0]['lanes']['control']['resolved'], 1)
            self.assertEqual(comparison_result['runs'][0]['lanes']['candidate']['resolved'], 1)
            different = subprocess.run(run_cmd + ['--store', str(store), '--inputs', str(inputs), '--outcomes', str(outcomes), '--idea', 'different settings', '--min-sample-count', '1'], cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(different.returncode, 0, different.stderr)
            mismatch = subprocess.run(cmd + ['compare', '--store', str(store), receipt['experiment_id'], json.loads(different.stdout)['experiment_id']], cwd=repo, env=env, capture_output=True, text=True)
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn('different input, outcome, or replay settings', mismatch.stderr)
            queue = subprocess.run(cmd + ['queue', '--store', str(store), '--experiment', receipt['experiment_id'], '--note', 'manual review only'], cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(queue.returncode, 0, queue.stderr)
            self.assertFalse(json.loads(queue.stdout)['diagnostic_eligible'])
            with sqlite3.connect(store / 'experiments.sqlite3') as db:
                self.assertEqual(db.execute('SELECT experiment_id FROM candidate_queue').fetchall(), [(receipt['experiment_id'],)])
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute('DELETE FROM experiments')
            tracked_code = source.read_text()
            source.write_text(tracked_code + '\n# uncommitted experiment\n')
            dirty = subprocess.run(run_cmd + ['--store', str(store), '--inputs', str(inputs), '--outcomes', str(outcomes), '--idea', 'dirty'], cwd=repo, env=env, capture_output=True, text=True)
            self.assertNotEqual(dirty.returncode, 0)
            self.assertIn('checkout is dirty', dirty.stderr)
            source.write_text(tracked_code)
            self.assertEqual(paper.read_bytes(), paper_before)
            bad = dict(record)
            bad['market'] = dict(record['market'], market_metadata={'winner': 'YES'})
            bad.pop('canonical_input_sha256')
            bad['canonical_input_sha256'] = hashlib.sha256(json.dumps(bad, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            bad_inputs = root / 'bad-inputs.jsonl'
            bad_inputs.write_text(json.dumps(bad) + '\n')
            bad_run = subprocess.run(run_cmd + ['--store', str(store), '--inputs', str(bad_inputs), '--outcomes', str(outcomes), '--idea', 'outcome leakage'], cwd=repo, env=env, capture_output=True, text=True)
            self.assertNotEqual(bad_run.returncode, 0)
            self.assertIn('forbidden', bad_run.stderr)
            report = run_dir / 'post_decision_resolution_report.json'
            report.write_text('{}')
            tampered = subprocess.run(cmd + ['compare', '--store', str(store), receipt['experiment_id'], second_receipt['experiment_id']], cwd=repo, env=env, capture_output=True, text=True)
            self.assertNotEqual(tampered.returncode, 0)
            self.assertIn('artifact hash mismatch', tampered.stderr)


if __name__ == '__main__':
    unittest.main()
