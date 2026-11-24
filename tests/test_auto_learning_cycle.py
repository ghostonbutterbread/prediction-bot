"""Synthetic end-to-end test of the agentless auto-learning cycle.

Real code paths: collector index -> fresh feed snapshot -> champion and
candidate replays at distinct Git commits (local executor) -> trusted
comparison -> later outcome binding -> feedback scoring -> scoreboard/report.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from bot.collector_replay_index import build_collector_replay_index, update_collector_replay_index
from tests.test_replay_decision_input import _snapshot_row

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ('auto_learning_cycle.py', 'auto_research.py', 'fresh_research_feed.py',
           'prepare_research_outcomes.py', 'research_feedback.py')
# predicted YES probability per market; yes ask 0.43 / no ask 0.59 in the fixture.
PROBABILITIES = {'A': 0.82, 'B': 0.60, 'C': 0.52, 'D': 0.15}


def collector_row(index: int, letter: str, *, future_fetch: bool = False) -> dict:
    row = _snapshot_row()
    market_id = f'KXHIGHNY-26AUG12-T8{index}'
    row['market_id'] = market_id
    row['shared_candidate_id'] = f'candidate:{index}'
    row['shared_snapshot_id'] = f'snapshot:{index}'
    # Fixture source clocks are 15:04:05; decisions are observed after them.
    row['observed_at'] = f'2026-08-12T15:1{index % 10}:05Z'
    snapshot = row['decision_artifact']['source_context']['data']['weather_source_snapshot']
    snapshot['predicted_prob'] = PROBABILITIES[letter]
    if future_fetch:
        snapshot['source_fetched_at'] = '2026-08-13T15:00:00+00:00'
    return row


def resolution(market_id: str, outcome: str) -> dict:
    return {'market_id': market_id, 'requested_market_id': market_id, 'returned_market_id': market_id,
            'market_status': 'finalized', 'kalshi_result': outcome.lower(),
            'resolution': {'outcome': outcome, 'source': 'kalshi_result'},
            'resolution_id': f'synthetic:{market_id}', 'settlement_ts': '2026-08-13T12:00:00Z'}


class AutoLearningCycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='auto-learning-cycle-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(self.tmp / 'home'),
                    'TMPDIR': str(self.tmp / 'tmp'), 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
                    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
                    'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}
        for name in ('home', 'tmp'):
            (self.tmp / name).mkdir()
        self.repo = self.tmp / 'trusted'
        self.repo.mkdir()
        shutil.copytree(ROOT / 'bot', self.repo / 'bot', ignore=shutil.ignore_patterns('__pycache__'))
        (self.repo / 'scripts').mkdir()
        for script in SCRIPTS:
            shutil.copy2(ROOT / 'scripts' / script, self.repo / 'scripts' / script)
        for command in (['git', 'init', '-q'], ['git', 'add', 'bot', 'scripts'], ['git', 'commit', '-qm', 'fixture']):
            subprocess.run(command, cwd=self.repo, env=self.env, check=True, capture_output=True)
        self.collector = self.tmp / 'collector'
        self.source = self.collector / 'data' / 'raw'
        self.source.mkdir(parents=True)
        self.raw, self.index, self.manifest = (self.source / 'raw.jsonl', self.source / 'index.jsonl',
                                               self.source / 'manifest.json')
        self.resolutions = self.collector / 'data' / 'resolutions.jsonl'
        self.resolutions.write_text('')
        self.work = self.collector / 'data' / 'derived_reports' / 'auto-learning'

    def append_rows(self, rows):
        with self.raw.open('a') as stream:
            for row in rows:
                stream.write(json.dumps(row) + '\n')
        if self.index.exists():
            update_collector_replay_index(self.raw, self.index, self.manifest)
        else:
            build_collector_replay_index(self.raw, self.index, self.manifest)

    def cli(self, *args, expect=0):
        result = subprocess.run([sys.executable, str(self.repo / 'scripts' / 'auto_learning_cycle.py'), *map(str, args)],
                                cwd=self.tmp, env=self.env, capture_output=True, text=True, timeout=600)
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def test_cycle_replays_compares_binds_and_scores_candidate(self):
        self.append_rows([collector_row(i, letter) for i, letter in enumerate('ABCD', 1)])
        self.cli('init', '--work', self.work, '--collector-root', self.collector, '--trusted', self.repo,
                 '--python', sys.executable, '--executor', 'local', '--index', self.index, '--manifest', self.manifest,
                 '--protected-root', self.source, '--resolutions', self.resolutions, '--page-rows', 4,
                 '--max-active', 2)
        config = json.loads((self.work / 'config.json').read_text())
        config.update(min_free_bytes=1, conclude_min_resolved_changes=2)
        (self.work / 'config.json').write_text(json.dumps(config))
        champion = json.loads((self.work / 'champion.json').read_text())['commit']

        first = self.cli('run', '--work', self.work, '--verbose').stdout
        self.assertIn('replicate: 0/4 decisions changed', first)
        self.assertIn('waiting_for_new_rows', self.cli('run', '--work', self.work, '--verbose').stdout)

        # The research agent commits a stricter edge floor in its clone and registers it.
        agent = self.work / 'agent-repo'
        subprocess.run(['git', 'checkout', '-q', '-b', 'research/edge-floor', 'champion'], cwd=agent, env=self.env, check=True)
        replay_module = agent / 'bot/weather/collector_source_router_replay.py'
        text = replay_module.read_text()
        self.assertIn('MIN_ABSOLUTE_EDGE = 0.05', text)
        replay_module.write_text(text.replace('MIN_ABSOLUTE_EDGE = 0.05', 'MIN_ABSOLUTE_EDGE = 0.20'))
        subprocess.run(['git', 'commit', '-qam', 'Raise source edge floor'], cwd=agent, env=self.env, check=True)
        commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=agent, env=self.env, check=True,
                                capture_output=True, text=True).stdout.strip()
        registered = json.loads(self.cli('register', '--work', self.work, '--commit', commit, '--hypothesis',
                                         'A 20-point edge floor skips marginal source-probability buys').stdout)
        self.cli('register', '--work', self.work, '--commit', commit, '--hypothesis', 'duplicate', expect=1)

        # New collector rows, including one whose source fetch postdates the decision.
        self.append_rows([collector_row(i, letter) for i, letter in zip(range(5, 9), 'ABCD')] +
                         [collector_row(9, 'A', future_fetch=True)])
        config['page_rows'] = config['min_new_rows'] = 5
        (self.work / 'config.json').write_text(json.dumps(config))
        second = self.cli('run', '--work', self.work, '--verbose').stdout
        self.assertIn('source-clock violations 1', second)
        self.assertIn(f'{registered["candidate_id"]}: 2/4 decisions changed', second)

        # Outcomes arrive later: the marginal buys the candidate skipped both lost.
        outcomes = {5: 'YES', 6: 'NO', 7: 'NO', 8: 'NO', 1: 'YES', 2: 'NO', 3: 'NO', 4: 'NO'}
        self.resolutions.write_text(''.join(json.dumps(resolution(f'KXHIGHNY-26AUG12-T8{i}', o)) + '\n'
                                            for i, o in outcomes.items()))
        third = self.cli('run', '--work', self.work, '--skip-snapshot', '--verbose').stdout
        self.assertIn('2 pairs final', third)
        board = json.loads((self.work / 'scoreboard.json').read_text())
        group = board['groups'][registered['candidate_id']]
        self.assertEqual(group['resolved_changed'], 2)
        self.assertEqual(group['champion']['correct'], 2)
        self.assertEqual(group['champion']['incorrect'], 2)
        self.assertEqual(group['candidate']['incorrect'], 0)
        self.assertEqual(group['delta']['net_correct'], 2)
        self.assertGreater(group['delta']['quote_pnl_usd'], 0)
        self.assertEqual(group['verdict'], 'better')
        self.assertTrue(group['needs_operator_review'])
        replicate = board['groups']['champion-replicate']
        self.assertEqual(replicate['changed_decisions'], 0)
        self.assertEqual(replicate['delta']['net_correct'], 0)
        self.assertIn('verdict **better**', (self.work / 'report.md').read_text())
        # Nothing is promoted automatically.
        self.assertEqual(json.loads((self.work / 'champion.json').read_text())['commit'], champion)
        # A dirty trusted checkout fails loudly before any work.
        (self.repo / 'scratch.txt').write_text('dirty')
        failed = self.cli('run', '--work', self.work, expect=1).stdout
        self.assertTrue(failed.startswith('[CRON_FAILURE]\n'), failed)


if __name__ == '__main__':
    unittest.main()
