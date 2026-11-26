import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import scripts.source_onboarding as onboarding
from scripts.source_onboarding import Rejected


def city(city_id, name):
    return {'city_id': city_id, 'city': name, 'timezone': 'America/New_York', 'aliases': [], 'watch_only': []}


def registry(*sources):
    return {'version': 1, 'cities': [city('atlanta_ga', 'Atlanta'), city('miami_fl', 'Miami')],
            'sources': [dict(s) for s in sources]}


def local(source_id, city_id='atlanta_ga', **extra):
    return {'source_id': source_id, 'city_id': city_id, 'name': 'Local Weather', 'type': 'local_met', 'role': 'forecast',
            'url': 'https://example.test/weather', 'status': 'watch_only', 'trust_score': 0, 'verified': False, **extra}


CATALOG = [{'source_id': 'nws'}, {'source_id': 'open_meteo'}]


class MechanicalChecksTest(unittest.TestCase):
    def test_scope_allows_only_source_paths(self):
        allowed = onboarding.DEFAULTS['allowed_paths']
        onboarding.check_scope([onboarding.REGISTRY, 'bot/feeds/weather_local_new.py', 'tests/test_weather_local_new.py'],
                               allowed)
        for path in ('bot/feeds/weather_pro.py', 'scripts/prediction_lab_collect.py', 'bot/weather/source_catalog.yaml'):
            with self.subTest(path), self.assertRaisesRegex(Rejected, 'outside'):
                onboarding.check_scope([onboarding.REGISTRY, path], allowed)
        with self.assertRaisesRegex(Rejected, 'nothing'):
            onboarding.check_scope([], allowed)

    def test_registry_accepts_additions_and_patches_of_onboarded_sources_only(self):
        old = registry(local('src_local_old_weather'), local('src_local_mine_weather'))
        added, patched = onboarding.check_registry(old, registry(*old['sources'], local('src_local_new_weather')), set())
        self.assertEqual((added, patched), ({'src_local_new_weather'}, set()))
        moved = registry(local('src_local_old_weather'), local('src_local_mine_weather', url='https://example.test/v2'))
        self.assertEqual(onboarding.check_registry(old, moved, {'src_local_mine_weather'}), (set(), {'src_local_mine_weather'}))
        bad = {
            'removal': (registry(local('src_local_mine_weather')), 'removed'),
            'foreign patch': (registry(local('src_local_old_weather', url='https://x.test'), local('src_local_mine_weather')),
                              'onboarded by this pipeline'),
            'trust promotion': (registry(*old['sources'], local('src_local_new_weather', trust_score=90)), 'trust_score 0'),
            'bad id': (registry(*old['sources'], local('local_new')), 'src_local_'),
            'unknown city': (registry(*old['sources'], local('src_local_new_weather', city_id='nowhere')), 'city_id'),
            'plain http': (registry(*old['sources'], local('src_local_new_weather', url='http://x.test')), 'https'),
            'official type': (registry(*old['sources'], local('src_local_new_weather', type='official')), 'local_met'),
            'patch flips status': (registry(local('src_local_old_weather'), local('src_local_mine_weather', status='trusted')),
                                   'may not change'),
            'city edit': ({**old, 'cities': [city('atlanta_ga', 'Atlanta')]}, 'cities'),
        }
        for name, (new, message) in bad.items():
            with self.subTest(name), self.assertRaisesRegex(Rejected, message):
                onboarding.check_registry(old, new, {'src_local_mine_weather'})

    def test_cap_counts_catalog_local_sources_and_the_station_observation(self):
        old = registry(local('src_local_a_weather'))
        self.assertEqual(onboarding.city_source_counts(old, CATALOG), {'atlanta_ga': 4, 'miami_fl': 3})
        one_more = registry(*old['sources'], local('src_local_b_weather'))
        self.assertEqual(onboarding.check_cap(old, one_more, CATALOG, 5), {'atlanta_ga': 5})
        with self.assertRaisesRegex(Rejected, 'over the cap'):
            onboarding.check_cap(old, one_more, CATALOG, 4)
        # A city already at the cap does not block additions elsewhere.
        self.assertEqual(onboarding.check_cap(old, registry(*old['sources'], local('src_local_m_weather', 'miami_fl')),
                                              CATALOG, 4), {'miami_fl': 4})


FAKE_SYSTEMCTL = '''#!{python}
import json, pathlib, sys
here = pathlib.Path(__file__).parent
state_path = here / 'state.json'
state = json.loads(state_path.read_text())
with (here / 'systemctl.log').open('a') as log:
    log.write(' '.join(sys.argv[1:]) + '\\n')
if 'restart' in sys.argv:
    state['restarts'] += 1
    state['pid'] += 1
    state['shows'] = 0
elif 'show' in sys.argv:
    state['shows'] += 1
    pid = state['pid']
    if state['restarts'] in state['crash_on'] and state['shows'] >= 2:
        pid += 1000  # crashed after starting; systemd started a new process
    print(f'ActiveState=active\\nSubState=running\\nMainPID={{pid}}\\nNRestarts=0')
state_path.write_text(json.dumps(state))
'''
FAKE_CLAUDE = '''#!/bin/sh
cat "$(dirname "$0")/verdict.txt"
'''


class OnboardEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='source-onboarding-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        (self.bin / 'systemctl').write_text(FAKE_SYSTEMCTL.format(python=sys.executable))
        (self.bin / 'claude').write_text(FAKE_CLAUDE)
        for name in ('systemctl', 'claude'):
            (self.bin / name).chmod(0o755)
        self.state(crash_on=[])
        self.verdict('VERDICT: PASS\n')
        env = {'PATH': f'{self.bin}:{os.environ.get("PATH", "/usr/bin:/bin")}', 'HOME': str(self.tmp),
               'GIT_CONFIG_NOSYSTEM': '1', 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@x.invalid',
               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@x.invalid'}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.origin = self.tmp / 'origin.git'
        self.beta = self.tmp / 'beta'
        self.sh('git', 'init', '-q', '--bare', '-b', 'beta', str(self.origin))
        self.sh('git', 'clone', '-q', str(self.origin), str(self.beta))
        self.sh('git', 'checkout', '-q', '-b', 'beta', cwd=self.beta)
        files = {onboarding.REGISTRY: json.dumps(registry(local('src_local_old_weather')), indent=2),
                 onboarding.CATALOG: 'sources:\n  - source_id: nws\n  - source_id: open_meteo\n',
                 'bot/feeds/weather_local_other.py': 'OTHER_LOCAL_ADAPTERS = {}\n',
                 'tests/__init__.py': '', 'README.md': 'collector\n',
                 'tests/test_weather_local_dummy.py': 'import unittest\n\nclass T(unittest.TestCase):\n'
                                                       '    def test_ok(self):\n        pass\n'}
        for name, text in files.items():
            (self.beta / name).parent.mkdir(parents=True, exist_ok=True)
            (self.beta / name).write_text(text)
        self.sh('git', 'add', '-A', cwd=self.beta)
        self.sh('git', 'commit', '-qm', 'base', cwd=self.beta)
        self.sh('git', 'push', '-q', '-u', 'origin', 'beta', cwd=self.beta)
        self.work = self.tmp / 'work'
        self.run_cli('init', '--work', str(self.work), '--beta', str(self.beta), '--python', sys.executable,
                     '--unit', 'collector.service', '--replay-index', str(self.tmp / 'index.jsonl'),
                     '--market-snapshots', str(self.tmp / 'snapshots.jsonl'), '--capture-root', str(self.tmp / 'captures'))
        config = json.loads((self.work / 'config.json').read_text())
        config.update(probe=False, restart_settle_seconds=0, test_modules=['tests.test_weather_local_dummy'])
        (self.work / 'config.json').write_text(json.dumps(config))
        self.agent = self.work / 'agent-repo'

    def sh(self, *cmd, cwd=None):
        return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def state(self, **values):
        (self.bin / 'state.json').write_text(json.dumps({'pid': 100, 'restarts': 0, 'shows': 0, **values}))

    def verdict(self, text):
        (self.bin / 'verdict.txt').write_text(text)

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = onboarding.main(list(args))
        return code, out.getvalue()

    def propose(self, branch, edit):
        self.sh('git', 'checkout', '-q', '-b', branch, 'origin/beta', cwd=self.agent)
        edit(self.agent)
        self.sh('git', 'commit', '-qam', branch, cwd=self.agent)
        self.sh('git', 'checkout', '-q', 'beta', cwd=self.agent)
        code, out = self.run_cli('propose', '--work', str(self.work), '--branch', branch, '--note', f'{branch} test')
        self.assertEqual(code, 0)
        return json.loads(out)['id']

    def add_source(self, source_id):
        def edit(repo):
            path = repo / onboarding.REGISTRY
            data = json.loads(path.read_text())
            data['sources'].append(local(source_id))
            path.write_text(json.dumps(data, indent=2))
        return edit

    def proposal(self, proposal_id):
        return json.loads((self.work / 'proposals' / proposal_id / 'proposal.json').read_text())

    def origin_registry(self):
        return json.loads(self.sh('git', '--git-dir', str(self.origin), 'show', f'beta:{onboarding.REGISTRY}'))

    def restarts(self):
        log = self.bin / 'systemctl.log'
        return sum('restart' in line for line in log.read_text().splitlines()) if log.exists() else 0

    def test_passing_proposal_goes_live_and_is_watched(self):
        proposal_id = self.propose('source/new', self.add_source('src_local_new_weather'))
        code, out = self.run_cli('onboard', '--work', str(self.work))
        self.assertEqual(code, 0, out)
        self.assertIn('LIVE', out)
        record = self.proposal(proposal_id)
        self.assertEqual(record['status'], 'live')
        self.assertIn('src_local_new_weather', {s['source_id'] for s in self.origin_registry()['sources']})
        self.assertEqual(self.sh('git', 'rev-parse', 'HEAD', cwd=self.beta), self.sh('git', 'rev-parse', 'origin/beta', cwd=self.beta))
        self.assertEqual(self.restarts(), 1)
        watch = json.loads((self.work / 'watches.json').read_text())
        self.assertEqual([(w['source_id'], w['proposal']) for w in watch], [('src_local_new_weather', proposal_id)])
        self.assertEqual(self.sh('git', 'worktree', 'list', '--porcelain', cwd=self.beta).count('worktree '), 1)
        # A later patch of the onboarded source is accepted and recorded on its watch.
        def patch_url(repo):
            path = repo / onboarding.REGISTRY
            data = json.loads(path.read_text())
            next(s for s in data['sources'] if s['source_id'] == 'src_local_new_weather')['url'] = 'https://example.test/v2'
            path.write_text(json.dumps(data, indent=2))
        self.sh('git', 'fetch', '-q', 'origin', cwd=self.agent)
        patch_id = self.propose('source/fix', patch_url)
        self.assertEqual(self.run_cli('onboard', '--work', str(self.work))[0], 0)
        self.assertEqual(self.proposal(patch_id)['status'], 'live')
        self.assertEqual(len(json.loads((self.work / 'watches.json').read_text())[0]['patches']), 1)

    def test_review_block_and_scope_violation_are_rejected_without_deploying(self):
        self.verdict('VERDICT: BLOCK\n1. parser reads the wrong day\n')
        blocked = self.propose('source/blocked', self.add_source('src_local_new_weather'))
        self.run_cli('onboard', '--work', str(self.work))
        self.assertEqual(self.proposal(blocked)['status'], 'rejected')
        self.assertIn('review BLOCK', self.proposal(blocked)['reason'])
        def scope(repo):
            self.add_source('src_local_other_weather')(repo)
            (repo / 'README.md').write_text('changed\n')
        outside = self.propose('source/outside', scope)
        self.run_cli('onboard', '--work', str(self.work))
        self.assertIn('outside', self.proposal(outside)['reason'])
        self.assertEqual([s['source_id'] for s in self.origin_registry()['sources']], ['src_local_old_weather'])
        self.assertEqual(self.restarts(), 0)

    def test_collector_that_does_not_stay_up_is_rolled_back(self):
        self.state(crash_on=[1])
        proposal_id = self.propose('source/crash', self.add_source('src_local_new_weather'))
        code, out = self.run_cli('onboard', '--work', str(self.work))
        self.assertEqual(code, 0, out)
        record = self.proposal(proposal_id)
        self.assertEqual(record['status'], 'rejected')
        self.assertIn('did not stay up', record['reason'])
        self.assertEqual([s['source_id'] for s in self.origin_registry()['sources']], ['src_local_old_weather'])
        self.assertEqual(self.restarts(), 2)
        self.assertFalse((self.work / 'watches.json').exists())

    def test_busy_checkout_defers_and_a_moved_branch_is_rejected(self):
        proposal_id = self.propose('source/new', self.add_source('src_local_new_weather'))
        (self.beta / 'README.md').write_text('local edit\n')
        self.run_cli('onboard', '--work', str(self.work))
        self.assertEqual(self.proposal(proposal_id)['status'], 'pending')
        self.assertIn('uncommitted', self.proposal(proposal_id)['reason'])
        self.sh('git', 'checkout', '-q', 'README.md', cwd=self.beta)
        self.sh('git', 'checkout', '-q', 'source/new', cwd=self.agent)
        (self.agent / 'bot/feeds/weather_local_other.py').write_text('OTHER_LOCAL_ADAPTERS = {"x": None}\n')
        self.sh('git', 'commit', '-qam', 'later', cwd=self.agent)
        self.run_cli('onboard', '--work', str(self.work))
        self.assertIn('moved', self.proposal(proposal_id)['reason'])
        self.assertEqual(self.restarts(), 0)


class WatchTest(unittest.TestCase):
    def test_watch_reports_source_health_and_storage(self):
        tmp = Path(tempfile.mkdtemp(prefix='source-watch-test-'))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        moment = datetime.now(timezone.utc)
        snapshots, index = tmp / 'snapshots.jsonl', tmp / 'index.jsonl'
        offset = 0
        with snapshots.open('wb') as raw_out, index.open('w') as index_out:
            for i in range(6):
                observed = moment - timedelta(hours=2, minutes=-i)
                sources = [{'source_id': 'nws', 'scoreable_forecast': True, 'forecast_high': 80},
                           {'source_id': 'open_meteo', 'scoreable_forecast': True, 'forecast_high': 82},
                           {'source_id': 'src_local_new_weather', 'scoreable_forecast': i % 3 != 0,
                            'forecast_high': 95, 'market_target_date': '2026-11-26', 'source_target_date': '2026-11-26',
                            'source_fetched_at': (observed - timedelta(minutes=1)).isoformat()}]
                row = json.dumps({'observed_at': observed.isoformat(), 'decision_artifact': {'source_context': {
                    'data': {'weather_source_snapshot': {'sources': sources}}}}}).encode() + b'\n'
                raw_out.write(row)
                index_out.write(json.dumps({'observed_at': observed.isoformat(), 'byte_offset': offset,
                                            'byte_length': len(row)}) + '\n')
                offset += len(row)
        work = tmp / 'work'
        work.mkdir()
        (tmp / 'captures').mkdir()
        (tmp / 'captures' / 'accounting.json').write_text(json.dumps({'bytes': 1000}))
        (work / 'config.json').write_text(json.dumps({'replay_index': str(index), 'market_snapshots': str(snapshots),
                                                      'capture_root': str(tmp / 'captures')}))
        (work / 'watches.json').write_text(json.dumps([
            {'source_id': 'src_local_new_weather', 'city_id': 'atlanta_ga', 'live_at': (moment - timedelta(days=1)).isoformat(),
             'until': (moment + timedelta(days=13)).isoformat(), 'patches': []},
            {'source_id': 'src_local_done_weather', 'city_id': 'miami_fl', 'live_at': (moment - timedelta(days=15)).isoformat(),
             'until': (moment - timedelta(days=1)).isoformat(), 'patches': []}]))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(onboarding.main(['watch', '--work', str(work)]), 0)
        report = json.loads(next((work / 'reports').glob('watch-*.json')).read_text())
        entry = report['sources']['src_local_new_weather']
        self.assertEqual((entry['rows'], entry['scoreable'], entry['median_gap_f']), (6, 4, 14.0))
        self.assertIn('median gap to other sources 14.0F', entry['issues'])
        self.assertEqual(report['completed'], ['src_local_done_weather'])
        self.assertIn('src_local_new_weather', out.getvalue())
        self.assertEqual(len((work / 'storage-history.jsonl').read_text().splitlines()), 1)


if __name__ == '__main__':
    unittest.main()
