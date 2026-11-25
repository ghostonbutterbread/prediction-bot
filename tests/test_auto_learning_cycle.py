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

from types import SimpleNamespace

from bot.collector_replay_index import build_collector_replay_index, update_collector_replay_index
from bot.shared_market_feed import build_shared_market_candidate_row
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
    # The real producer adds the shared market candidate, including a future close time.
    market = SimpleNamespace(id=market_id, question=row['question'], exchange='kalshi', yes_price=.42, no_price=.58,
                             volume=456, category='Weather', closes_at='2026-08-13T04:00:00+00:00', metadata={})
    row['shared_candidate'] = build_shared_market_candidate_row(
        run_id='run', market=market, decision_artifact=row['decision_artifact'], source_runtime='prediction_lab',
        observed_at=row['observed_at'])
    row['shared_candidate_id'] = row['shared_candidate']['candidate_id']
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
        # Operator mode: a passing proof is flagged for review instead of promoted.
        config.update(min_free_bytes=1, auto_promote=False, promotion_min_units=1)
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
        concluded = [json.loads(line) for line in (self.work / 'candidates' / registered['candidate_id'] /
                                                   'events.jsonl').read_text().splitlines()][-1]
        self.assertTrue(concluded['proof']['passes'])
        self.assertEqual(concluded['proof']['delta_usd'], 2.0)
        replicate = board['groups']['champion-replicate']
        self.assertEqual(replicate['changed_decisions'], 0)
        self.assertEqual(replicate['delta']['net_correct'], 0)
        self.assertIn('verdict **better**', (self.work / 'report.md').read_text())
        # Nothing is promoted automatically.
        self.assertEqual(json.loads((self.work / 'champion.json').read_text())['commit'], champion)
        # Moving the champion keeps the agent's checked-out champion tree in step (no staged reverts).
        subprocess.run(['git', 'checkout', '-q', 'champion'], cwd=agent, env=self.env, check=True)
        self.cli('set-champion', '--work', self.work, '--commit', commit, '--note', 'operator test')
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=agent, env=self.env, check=True,
                                capture_output=True, text=True).stdout
        self.assertEqual(status, '')
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=agent, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(head, commit)
        # A dirty trusted checkout fails loudly before any work.
        (self.repo / 'scratch.txt').write_text('dirty')
        failed = self.cli('run', '--work', self.work, expect=1).stdout
        self.assertTrue(failed.startswith('[CRON_FAILURE]\n'), failed)

    def test_broken_candidate_is_isolated_and_clock_guard_fails_closed(self):
        self.append_rows([collector_row(i, letter) for i, letter in enumerate('ABCD', 1)])
        self.cli('init', '--work', self.work, '--collector-root', self.collector, '--trusted', self.repo,
                 '--python', sys.executable, '--executor', 'local', '--index', self.index, '--manifest', self.manifest,
                 '--protected-root', self.source, '--resolutions', self.resolutions, '--page-rows', 4)
        config = json.loads((self.work / 'config.json').read_text())
        config.update(min_free_bytes=1, max_candidate_failures=1, pending_max_age_days=0)
        (self.work / 'config.json').write_text(json.dumps(config))
        agent = self.work / 'agent-repo'
        subprocess.run(['git', 'checkout', '-q', '-b', 'research/noisy', 'champion'], cwd=agent, env=self.env, check=True)
        script = agent / 'scripts/auto_research.py'
        text = script.read_text()
        script.write_text(text.replace("    print(json.dumps({'experiment_id'", "    print('debug line')\n    print(json.dumps({'experiment_id'", 1))
        self.assertNotEqual(script.read_text(), text)
        subprocess.run(['git', 'commit', '-qam', 'Noisy stdout'], cwd=agent, env=self.env, check=True)
        commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=agent, env=self.env, check=True,
                                capture_output=True, text=True).stdout.strip()
        candidate_id = json.loads(self.cli('register', '--work', self.work, '--commit', commit,
                                           '--hypothesis', 'noisy').stdout)['candidate_id']
        # One row has a late recorded_as_of, one an unreadable fetched_at: both must be excluded.
        late, unreadable = collector_row(5, 'A'), collector_row(6, 'B')
        late['decision_artifact']['source_context']['data']['weather_source_snapshot']['sources'][0]['as_of'] = \
            '2026-08-13T00:00:00+00:00'
        unreadable['decision_artifact']['source_context']['data']['weather_source_snapshot']['sources'][0]['fetched_at'] = \
            'yesterday'
        self.append_rows([late, unreadable])
        config.update(page_rows=6, min_new_rows=2)
        (self.work / 'config.json').write_text(json.dumps(config))
        out = self.cli('run', '--work', self.work, '--verbose').stdout
        self.assertIn('complete_with_warnings', out)
        self.assertIn(f'Candidate failure {candidate_id}', out)
        cycle = json.loads(sorted(self.work.glob('cycles/*/cycle.json'))[-1].read_text())
        self.assertEqual(cycle['snapshot']['audit']['source_clock_violation_rows'], 2, cycle['snapshot']['audit'])
        self.assertEqual(cycle['snapshot']['replay_rows'], 4)
        state = json.loads((self.work / 'state/feed.json').read_text())
        self.assertEqual(state['cycle_id'], cycle['cycle_id'])
        board = json.loads((self.work / 'scoreboard.json').read_text())
        self.assertEqual(board['groups'][candidate_id]['verdict'], 'broken')
        # The only candidate failed, so no pair was queued for this snapshot.
        self.assertEqual(list(self.work.glob('pending/*/events.jsonl')), [])
        self.assertIn('waiting_for_new_rows', self.cli('run', '--work', self.work, '--verbose').stdout)

    def test_parameter_candidate_is_promoted_on_proof_and_rolled_back_by_its_guard(self):
        self.append_rows([collector_row(i, letter) for i, letter in enumerate('ABCD', 1)])
        self.cli('init', '--work', self.work, '--collector-root', self.collector, '--trusted', self.repo,
                 '--python', sys.executable, '--executor', 'local', '--index', self.index, '--manifest', self.manifest,
                 '--protected-root', self.source, '--resolutions', self.resolutions, '--page-rows', 4, '--max-active', 3)
        config = json.loads((self.work / 'config.json').read_text())
        config.update(min_free_bytes=1, promotion_min_units=1)  # every fixture row is one event (one unit)
        (self.work / 'config.json').write_text(json.dumps(config))
        original = json.loads((self.work / 'champion.json').read_text())
        self.cli('run', '--work', self.work)

        # A lane file, not a branch: same code, a YES probability floor.
        bad = self.cli('register', '--work', self.work, '--parameters', '{"yes_floor": 0.7}', '--hypothesis', 'x',
                       expect=1).stdout
        self.assertIn('unknown source-probability parameter', bad)
        registered = json.loads(self.cli('register', '--work', self.work, '--parameters',
                                         '{"YES": {"min_probability": 0.70}}', '--hypothesis',
                                         'Skip weak YES source signals').stdout)
        self.assertEqual(registered['commit'], original['commit'])
        self.assertEqual(registered['parameters'], {'YES': {'min_probability': 0.7}})
        self.cli('register', '--work', self.work, '--parameters', '{"YES": {"min_probability": 0.7}}',
                 '--hypothesis', 'same version', expect=1)

        self.append_rows([collector_row(i, letter) for i, letter in zip(range(5, 9), 'ABCD')])
        self.assertIn(f'{registered["candidate_id"]}: 2/4 decisions changed',
                      self.cli('run', '--work', self.work, '--verbose').stdout)
        runs = list(self.work.glob(f'cycles/*/runs/{registered["candidate_id"]}/parameters.json'))
        self.assertEqual(len(runs), 1)

        # The two weak YES buys the candidate skipped both lost: proof passes and the loop promotes it.
        outcomes = {1: 'YES', 2: 'NO', 3: 'NO', 4: 'NO', 5: 'YES', 6: 'NO', 7: 'NO', 8: 'NO'}
        self.resolutions.write_text(''.join(json.dumps(resolution(f'KXHIGHNY-26AUG12-T8{i}', o)) + '\n'
                                            for i, o in outcomes.items()))
        promoted = self.cli('run', '--work', self.work, '--skip-snapshot').stdout  # notable: printed without --verbose
        self.assertIn(f'PROMOTED {registered["candidate_id"]}', promoted)
        champion = json.loads((self.work / 'champion.json').read_text())
        self.assertEqual((champion['commit'], champion['parameters']), (original['commit'], registered['parameters']))
        self.assertEqual(champion['set_by'], 'auto-promotion')
        self.assertTrue(champion['proof']['passes'])
        candidates = {json.loads(p.read_text())['candidate_id']: json.loads(p.read_text())
                      for p in self.work.glob('candidates/*/candidate.json')}
        guard = next(c for c in candidates.values() if c['kind'] == 'rollback_guard')
        self.assertEqual((guard['commit'], guard['parameters']), (original['commit'], {}))
        report = (self.work / 'report.md').read_text()
        self.assertIn('## Champion changes', report)
        self.assertIn('promoted automatically only when a candidate passes the proof', report)

        # A promotion interrupted before its candidate was concluded is repaired by the next cycle.
        with (self.work / 'candidates' / registered['candidate_id'] / 'events.jsonl').open('a') as stream:
            stream.write(json.dumps({'at': '2026-11-24T00:00:00+00:00', 'status': 'active'}) + '\n')

        # Later the old policy's weak YES buys win: the guard proves the old champion better and restores it.
        self.append_rows([collector_row(i, letter) for i, letter in zip(range(9, 13), 'BCBC')])
        self.assertIn(f'{guard["candidate_id"]}: 4/4 decisions changed',
                      self.cli('run', '--work', self.work, '--verbose').stdout)
        cycle = json.loads(sorted(self.work.glob('cycles/*/cycle.json'))[-1].read_text())
        self.assertEqual(cycle['reconciled'], [f'concluded {registered["candidate_id"]}'])
        outcomes.update({9: 'YES', 10: 'YES', 11: 'YES', 12: 'YES'})
        self.resolutions.write_text(''.join(json.dumps(resolution(f'KXHIGHNY-26AUG12-T8{i}', o)) + '\n'
                                            for i, o in outcomes.items()))
        restored = self.cli('run', '--work', self.work, '--skip-snapshot').stdout
        self.assertIn(f'ROLLED BACK to {guard["candidate_id"]}', restored)
        champion = json.loads((self.work / 'champion.json').read_text())
        self.assertEqual((champion['commit'], champion['parameters']), (original['commit'], {}))
        status = json.loads(self.cli('status', '--work', self.work).stdout)
        self.assertEqual(status['active_candidates'], [])  # a restoring guard does not spawn another guard
        history = [json.loads(line) for line in (self.work / 'champion-history.jsonl').read_text().splitlines()]
        self.assertEqual([h['set_by'] for h in history], ['init', 'auto-promotion', 'auto-promotion'])


class ProofOfImprovementTest(unittest.TestCase):
    CFG = {'promotion_min_units': 20, 'promotion_min_confidence': 0.8, 'promotion_min_buy_ratio': 0.25,
           'promotion_bootstrap_samples': 2000}

    @staticmethod
    def rows(deltas):
        return [{'unit_id': unit, 'market_id': f'{unit}-m{i}', 'independence_quality': 'event_ticker',
                 'baseline': {'proxy_pnl_usd': 0.0}, 'candidate': {'proxy_pnl_usd': delta}}
                for i, (unit, delta) in enumerate(deltas)]

    def proof(self, rows, champion_buys=10, candidate_buys=8, **cfg):
        import scripts.auto_learning_cycle as cycle
        return cycle.proof_of_improvement(rows, champion_buys=champion_buys, candidate_buys=candidate_buys,
                                          cfg={**self.CFG, **cfg}, seed='seed')

    def test_rows_of_one_event_count_once(self):
        result = self.proof(self.rows([('KXHIGHAUS-26NOV24', 1.0)] * 30))
        self.assertEqual((result['units'], result['decisions'], result['delta_usd']), (1, 30, 30.0))
        self.assertFalse(result['passes'])
        self.assertIn('1/20 independent units', result['blockers'][0])

    def test_consistent_gains_across_enough_units_pass(self):
        result = self.proof(self.rows([(f'E{i}', 1.0) for i in range(20)]))
        self.assertEqual((result['units'], result['confidence']), (20, 1.0))
        self.assertTrue(result['passes'])

    def test_mixed_units_give_a_deterministic_intermediate_confidence(self):
        rows = self.rows([(f'E{i}', 1.0) for i in range(12)] + [(f'L{i}', -1.0) for i in range(10)])
        first, second = self.proof(rows), self.proof(list(reversed(rows)))
        self.assertEqual(first['confidence'], second['confidence'])
        self.assertTrue(0.5 < first['confidence'] < 0.9, first)
        self.assertEqual(first['delta_usd'], 2.0)

    def test_contracts_without_an_event_ticker_pool_into_one_unit(self):
        rows = [{**row, 'independence_quality': 'contract_only'} for row in self.rows([(f'M{i}', 1.0) for i in range(25)])]
        result = self.proof(rows)
        self.assertEqual((result['units'], result['pooled_contract_only_decisions']), (1, 25))
        self.assertFalse(result['passes'])

    def test_trading_nothing_unpriced_rows_and_losses_are_blocked(self):
        gains = [(f'E{i}', 1.0) for i in range(20)]
        self.assertIn('buys 10%', self.proof(self.rows(gains), champion_buys=10, candidate_buys=1)['blockers'][0])
        unpriced = self.rows(gains) + [{'unit_id': 'X', 'market_id': 'x', 'baseline': {'proxy_pnl_usd': 0.0},
                                        'candidate': {'proxy_pnl_usd': None}}]
        blocked = self.proof(unpriced)
        self.assertEqual(blocked['unpriced_decisions'], 1)
        self.assertFalse(blocked['passes'])  # an unverifiable price can never be dropped silently
        self.assertIn('unverified price', blocked['blockers'][0])
        losing = self.proof(self.rows([(f'E{i}', -1.0) for i in range(20)]))
        self.assertEqual(losing['confidence'], 0.0)
        self.assertIn('paired proxy P&L delta is not positive', losing['blockers'])
        self.assertTrue(self.proof([], champion_buys=0, candidate_buys=0)['blockers'])


FAKE_HERMES = '''#!/bin/sh
state="$FAKE_HERMES_STATE"
case "$2" in
  list)
    echo "warning: install out of sync"
    while read -r id st; do
      printf '  %s [%s]\\n    Name:      job %s\\n    Next run:  soon\\n\\n' "$id" "$st" "$id"
    done < "$state" ;;
  pause|resume)
    [ "$2" = pause ] && new=paused || new=active
    echo "$2 $3" >> "$state.calls"
    sed -i "s/^$3 .*/$3 $new/" "$state" ;;
  *) exit 2 ;;
esac
'''


class AutoLearningCronSwitchTest(unittest.TestCase):
    def test_cron_up_down_switches_every_loop_job_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / 'bin').mkdir()
            (tmp / 'bin/hermes').write_text(FAKE_HERMES)
            (tmp / 'bin/hermes').chmod(0o755)
            state = tmp / 'state'
            state.write_text('aaaaaa111111 active\nbbbbbb222222 paused\ncccccc333333 active\n')
            work = tmp / 'work'
            work.mkdir()
            (work / 'config.json').write_text(json.dumps({'cron_jobs': ['aaaaaa111111', 'bbbbbb222222']}))
            env = dict(os.environ, PATH=f'{tmp / "bin"}:{os.environ["PATH"]}', FAKE_HERMES_STATE=str(state))

            def cron(action):
                return subprocess.run([sys.executable, str(ROOT / 'scripts/auto_learning_cycle.py'), 'cron', action,
                                       '--work', str(work)],
                                      env=env, capture_output=True, text=True)

            down = cron('down')
            self.assertEqual(down.returncode, 0, down.stdout + down.stderr)
            jobs = json.loads(down.stdout)['jobs']
            self.assertEqual({job: s['state'] for job, s in jobs.items()},
                             {'aaaaaa111111': 'paused', 'bbbbbb222222': 'paused'})
            self.assertIn('cccccc333333 active', state.read_text())  # unrelated job untouched
            self.assertEqual((tmp / 'state.calls').read_text(), 'pause aaaaaa111111\n')  # already-paused job skipped
            up = cron('up')
            self.assertEqual(up.returncode, 0, up.stdout + up.stderr)
            self.assertEqual({s['state'] for s in json.loads(up.stdout)['jobs'].values()}, {'active'})
            self.assertEqual(json.loads(cron('status').stdout)['jobs']['bbbbbb222222']['name'], 'job bbbbbb222222')
            # The main CLI switch drives the same jobs with a human summary.
            def learning(*args):
                return subprocess.run([sys.executable, str(ROOT / 'main.py'), 'learning', *args, '--work', str(work)],
                                      env=env, capture_output=True, text=True, cwd=ROOT)

            off = learning('off')
            self.assertEqual(off.returncode, 0, off.stdout + off.stderr)
            self.assertIn('Auto-learning loop: OFF (0/2 jobs active)', off.stdout)
            self.assertIn('job aaaaaa111111 [aaaaaa111111]: paused', off.stdout)
            self.assertIn('Loop status unavailable', off.stdout)  # this fixture has no loop config
            self.assertIn('cccccc333333 active', state.read_text())
            on = learning('on')
            self.assertEqual(on.returncode, 0, on.stdout + on.stderr)
            self.assertIn('Auto-learning loop: ON (2/2 jobs active)', on.stdout)
            (work / 'config.json').write_text(json.dumps({'cron_jobs': ['aaaaaa111111'], 'schema': 'x'}))
            (work / 'cycles/c1').mkdir(parents=True)
            (work / 'cycles/c1/cycle.json').write_text('[]')  # malformed shapes degrade, never crash
            self.assertIn('Loop status unavailable', learning('status').stdout)
            gone = subprocess.run([sys.executable, str(ROOT / 'main.py'), 'learning', 'off', '--work', str(tmp / 'nope')],
                                  env=env, capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(gone.returncode, 1)
            self.assertIn('not found', gone.stdout)
            (work / 'config.json').write_text(json.dumps({'cron_jobs': ['aaaaaa111111', 'bbbbbb222222']}))
            self.assertEqual(learning('status').returncode, 0)
            self.assertEqual(learning('sideways').returncode, 2)
            (work / 'config.json').write_text(json.dumps({'cron_jobs': ['dddddd444444']}))
            missing = cron('down')
            self.assertEqual(missing.returncode, 1)
            self.assertIn('not found', missing.stdout)


if __name__ == '__main__':
    unittest.main()
