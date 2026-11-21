import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from bot.collector_replay_index import build_collector_replay_index
from tests.test_collector_replay_inputs import _legacy_collector_row

ROOT = Path(__file__).resolve().parents[1]


class RealFeedProgressTests(unittest.TestCase):
    def test_real_feed_separate_runs_comparator_and_tampered_seal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / 'repo'
            repo.mkdir()
            shutil.copytree(ROOT / 'bot', repo / 'bot', ignore=shutil.ignore_patterns('__pycache__'))
            (repo / 'scripts').mkdir()
            shutil.copy2(ROOT / 'scripts/auto_research.py', repo / 'scripts/auto_research.py')
            env = dict(os.environ, HOME=str(root), TMPDIR=str(root), PYTHONDONTWRITEBYTECODE='1',
                       PYTHONPATH=str(repo), PREDICTION_BOT_COLLECTOR_ROOT=str(root / 'collector'),
                       GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                       GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
            def run(args, cwd=ROOT):
                result = subprocess.run([sys.executable, *map(str, args)], cwd=cwd, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            for command in (['git', 'init', '-q'], ['git', 'add', 'bot', 'scripts'], ['git', 'commit', '-qm', 'fixture']):
                subprocess.run(command, cwd=repo, env=env, check=True, capture_output=True)
            derived = root / 'collector/data/derived_reports'
            feed, acks = derived / 'feed', derived / 'acks'
            feed.mkdir(parents=True); acks.mkdir()
            source = root / 'source'; source.mkdir()
            raw, index, manifest = source / 'raw.jsonl', source / 'index.jsonl', source / 'manifest.json'
            row = _legacy_collector_row(); row['run_id'] = 'synthetic-run'
            raw.write_text(json.dumps(row) + '\n')
            build_collector_replay_index(raw, index, manifest)
            result = run([ROOT / 'scripts/fresh_research_feed.py', '--index', index, '--manifest', manifest,
                          '--output-root', feed, '--protected-root', source, '--max-index-bytes', '2000000',
                          '--max-inspected-rows', '1', '--max-raw-row-bytes', '200000', '--max-wall-seconds', '30',
                          '--max-memory-bytes', '536870912'])
            page = Path(result['directory']); self.assertEqual(json.loads((page/'receipt.json').read_text())['accepted_rows'], 1)
            stores = [derived/'baseline', derived/'candidate']
            receipts = []
            for store in stores:
                receipts.append(run([repo/'scripts/auto_research.py', 'run', '--store', store,
                                     '--inputs', page/'inputs.jsonl', '--outcomes', page/'outcomes.jsonl',
                                     '--idea', 'synthetic', '--max-input-bytes', '65536', '--max-outcome-bytes', '1'], cwd=repo))
            compare_args = [repo/'scripts/auto_research.py', 'compare', '--store', stores[0], '--candidate-store', stores[1],
                            *(r['experiment_id'] for r in receipts)]
            same = compare_args[:-1] + [receipts[0]['experiment_id']]
            same_result = subprocess.run([sys.executable, *map(str, same)], cwd=repo, env=env, capture_output=True, text=True)
            self.assertNotEqual(same_result.returncode, 0)
            self.assertIn('distinct experiments', same_result.stderr)
            comparison = run(compare_args, cwd=repo)
            compare_path = derived/'comparison.json'; compare_path.write_text(json.dumps(comparison))
            progress = ROOT/'scripts/research_feed_progress.py'
            ack_args = [progress, 'ack', '--page', page/'receipt.json', '--ack-dir', acks, '--comparison', compare_path,
                        '--baseline-store', stores[0], '--candidate-store', stores[1]]
            def store_bytes():
                return [{str(p.relative_to(store)): p.read_bytes() for p in store.rglob('*') if p.is_file()} for store in stores]
            before = store_bytes()
            forged = dict(comparison); forged['matched_decisions'] = True
            compare_path.write_text(json.dumps(forged))
            bad = subprocess.run([sys.executable, *map(str, ack_args)], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn('differs from comparison receipt', bad.stderr)
            self.assertFalse(list(acks.iterdir()))
            compare_path.write_text(json.dumps(comparison))
            run(ack_args)
            self.assertEqual(store_bytes(), before)
            forbidden = stores[0]/'must-not-write.json'
            forbidden_finish = [progress, 'finish', '--last-page', page/'receipt.json', '--ack-dir', acks, '--output', forbidden]
            bad_finish = subprocess.run([sys.executable, *map(str, forbidden_finish)], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertNotEqual(bad_finish.returncode, 0)
            self.assertFalse(forbidden.exists())
            artifact = Path(receipts[1]['output_dir'])/'sealed_candidate_decisions.jsonl'
            original = artifact.read_bytes(); artifact.write_bytes(original + b' ')
            checkpoint = derived/'checkpoint.json'
            finish_args = [progress, 'finish', '--last-page', page/'receipt.json', '--ack-dir', acks, '--output', checkpoint]
            failed = subprocess.run([sys.executable, *map(str, finish_args)], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0, failed.stdout)
            self.assertFalse(checkpoint.exists())
            artifact.write_bytes(original)
            run(finish_args)
            self.assertEqual(json.loads(checkpoint.read_text())['accepted_rows'], 1)
            self.assertEqual(store_bytes(), before)


if __name__ == '__main__':
    unittest.main()
