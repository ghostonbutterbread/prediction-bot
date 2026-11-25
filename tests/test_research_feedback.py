import hashlib
import json
import os
import shutil
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import closing, redirect_stdout
from io import StringIO
from unittest.mock import patch

from scripts import auto_research, research_feedback

from bot.weather.collector_source_router_replay import _sealed_decision, _canonical_json_bytes

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
                result = {'artifacts': {'sealed_control_decisions.jsonl': digest(raw)}, 'economic_status':'excluded_quote_only',
                          'decision_stats': {'input_records_seen': 1, 'input_records_invalid_identity': 0,
                                             'sealed_control_decisions': 1, 'sealed_candidate_decisions': 1}}
                db.execute('INSERT INTO experiments VALUES (?,?,?,?,?,?,?)', (name, 'c'*40, self.input_hash,
                           self.empty_hash, json.dumps(config), json.dumps(result), str(directory)))

    def run_cli(self, *, outcome_hash=None, output=None, candidate_store=None, baseline='baseline', candidate='candidate'):
        env = dict(os.environ, HOME=str(self.root), TMPDIR=str(self.root),
                   PREDICTION_BOT_COLLECTOR_ROOT=str(self.root / 'collector'), PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT))
        cmd = [sys.executable, str(SCRIPT), '--store', str(self.store), '--baseline', baseline,
            '--candidate', candidate, '--outcomes', str(self.outcomes), '--outcome-sha256', outcome_hash or digest(self.outcomes.read_bytes()),
            '--max-outcome-bytes', '65536', '--max-decision-bytes', '65536', '--output', str(output or self.output)]
        if candidate_store is not None:
            cmd.extend(['--candidate-store', str(candidate_store)])
        return subprocess.run(cmd, env=env, cwd=ROOT, capture_output=True, text=True)

    def prepare_separate_stores(self):
        candidate_store = self.store.parent / 'candidate-store'
        shutil.copytree(self.store, candidate_store)
        baseline_id, candidate_id = 'a' * 32, 'b' * 32
        for store, keep, remove, new_id in ((self.store, 'baseline', 'candidate', baseline_id),
                                             (candidate_store, 'candidate', 'baseline', candidate_id)):
            shutil.rmtree(store / 'runs' / remove)
            (store / 'runs' / keep).rename(store / 'runs' / new_id)
            with sqlite3.connect(store / 'experiments.sqlite3') as db:
                db.execute('DELETE FROM experiments WHERE experiment_id=?', (remove,))
                db.execute('UPDATE experiments SET experiment_id=?, output_dir=? WHERE experiment_id=?',
                           (new_id, '/old/mount/runs/' + new_id, keep))
        return candidate_store, baseline_id, candidate_id

    def test_successful_auto_comparison_closes_both_connections(self):
        with closing(sqlite3.connect(self.store / 'experiments.sqlite3')) as db, db:
            for name in self.rows:
                directory = self.store / 'runs' / name
                control = (directory / 'sealed_control_decisions.jsonl').read_bytes()
                files = {'sealed_candidate_decisions.jsonl': control,
                         'post_decision_resolution_report.json': b'{"resolved_decisions": [], "unresolved_decisions": []}'}
                result = json.loads(db.execute('SELECT result_json FROM experiments WHERE experiment_id=?', (name,)).fetchone()[0])
                for filename, content in files.items():
                    (directory / filename).write_bytes(content)
                    result['artifacts'][filename] = digest(content)
                db.execute('UPDATE experiments SET result_json=? WHERE experiment_id=?', (json.dumps(result), name))
        opened, original_connect = [], sqlite3.connect

        def connect(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            opened.append(db)
            return db

        argv = ['auto_research', 'compare', '--store', str(self.store), 'baseline', 'candidate']
        with patch('bot.collector_paths.derived_reports_root', return_value=self.store.parent), \
             patch.object(auto_research.sqlite3, 'connect', side_effect=connect), \
             patch.object(sys, 'argv', argv), redirect_stdout(StringIO()):
            self.assertEqual(auto_research.main(), 0)
        self.assertEqual(len(opened), 2)
        for db in opened:
            with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed'):
                db.execute('SELECT 1')

    def test_feedback_closes_connections_on_success_and_load_failure(self):
        args = Namespace(store=self.store, candidate_store=None, baseline='baseline', candidate='candidate',
                         outcomes=self.outcomes, outcome_sha256=digest(self.outcomes.read_bytes()),
                         max_outcome_bytes=65536, max_decision_bytes=65536, output=self.output)
        opened = []
        original_connect = sqlite3.connect

        def connect(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            opened.append(db)
            return db

        with patch.object(research_feedback, 'derived_reports_root', return_value=self.store.parent), \
             patch.object(research_feedback.sqlite3, 'connect', side_effect=connect):
            research_feedback.run(args)
            self.assertEqual(len(opened), 2)
            for db in opened:
                with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed'):
                    db.execute('SELECT 1')
            opened.clear()
            args.output = self.output.parent / 'failure.json'
            with patch.object(research_feedback, 'load_run', side_effect=ValueError('injected load failure')):
                with self.assertRaisesRegex(ValueError, 'injected load failure'):
                    research_feedback.run(args)
            self.assertEqual(len(opened), 1)
            with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed'):
                opened[0].execute('SELECT 1')
            self.assertFalse(args.output.exists())

    def test_identical_experiment_ids_are_rejected_without_output(self):
        same = self.run_cli(baseline='baseline', candidate='baseline')
        self.assertNotEqual(same.returncode, 0)
        self.assertIn('distinct experiments', same.stderr)
        self.assertFalse(self.output.exists())

    def test_identical_experiment_ids_in_separate_stores_are_rejected(self):
        candidate_store = self.store.parent / 'candidate-store'
        shutil.copytree(self.store, candidate_store)
        experiment = 'a' * 32
        for store in (self.store, candidate_store):
            (store / 'runs' / 'baseline').rename(store / 'runs' / experiment)
            with closing(sqlite3.connect(store / 'experiments.sqlite3')) as db, db:
                db.execute('UPDATE experiments SET experiment_id=? WHERE experiment_id=?', (experiment, 'baseline'))
        separate = self.run_cli(candidate_store=candidate_store, baseline=experiment, candidate=experiment)
        self.assertNotEqual(separate.returncode, 0)
        self.assertIn('distinct experiments', separate.stderr)
        self.assertFalse(self.output.exists())

    def test_separate_stores_remap_and_same_store_compatibility(self):
        same = self.run_cli()
        self.assertEqual(same.returncode, 0, same.stderr)
        same_report = json.loads(self.output.read_text())
        candidate_store, baseline_id, candidate_id = self.prepare_separate_stores()
        separate_output = self.output.parent / 'separate.json'
        before = {p: p.read_bytes() for store in (self.store, candidate_store)
                  for p in store.rglob('*') if p.is_file()}
        run = self.run_cli(candidate_store=candidate_store, baseline=baseline_id, candidate=candidate_id, output=separate_output)
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(separate_output.read_text())
        self.assertEqual([r['experiment_id'] for r in report['runs']], [baseline_id, candidate_id])
        self.assertEqual(report['changed_resolved_decisions'], same_report['changed_resolved_decisions'])
        self.assertTrue(report['producer_coverage_counts_match'])
        self.assertFalse(report['diagnostic_eligible'])
        self.assertEqual(before, {p: p.read_bytes() for store in (self.store, candidate_store)
                                  for p in store.rglob('*') if p.is_file()})
        self.assertFalse((self.store / 'runs' / candidate_id).exists())

    def test_separate_stores_reject_overlaps_invalid_ids_and_internal_symlinks(self):
        candidate_store, baseline_id, candidate_id = self.prepare_separate_stores()
        for target in (self.store, self.store / 'runs', self.root):
            run = self.run_cli(candidate_store=target, baseline=baseline_id, candidate=candidate_id)
            self.assertNotEqual(run.returncode, 0, run.stderr)
            self.assertFalse(self.output.exists())
        run = self.run_cli(candidate_store=candidate_store, baseline='../escape', candidate=candidate_id)
        self.assertIn('invalid experiment identity', run.stderr)
        run = self.run_cli(candidate_store=candidate_store, baseline=baseline_id, candidate=candidate_id,
                           output=candidate_store / 'result.json')
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse((candidate_store / 'result.json').exists())
        artifact = candidate_store / 'runs' / candidate_id / 'sealed_control_decisions.jsonl'
        outside = self.root / 'outside.jsonl'
        outside.write_bytes(artifact.read_bytes())
        artifact.unlink()
        artifact.symlink_to(outside)
        run = self.run_cli(candidate_store=candidate_store, baseline=baseline_id, candidate=candidate_id)
        self.assertIn('symlink in experiment store', run.stderr)
        self.assertFalse(self.output.exists())

    def test_real_producer_two_stores_feed_retrospective_feedback(self):
        repo = self.root / 'producer-repo'
        repo.mkdir()
        shutil.copytree(ROOT / 'bot', repo / 'bot', ignore=shutil.ignore_patterns('__pycache__'))
        (repo / 'scripts').mkdir()
        shutil.copy2(ROOT / 'scripts/auto_research.py', repo / 'scripts/auto_research.py')
        env = dict(os.environ, HOME=str(self.root), TMPDIR=str(self.root),
                   XDG_CONFIG_HOME=str(self.root / 'config'), PREDICTION_BOT_COLLECTOR_ROOT=str(self.root / 'collector'),
                   PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(repo), GIT_CONFIG_NOSYSTEM='1',
                   GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                   GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        for command in (('git', 'init', '-q'), ('git', 'add', 'bot', 'scripts'), ('git', 'commit', '-qm', 'synthetic producer')):
            subprocess.run(command, cwd=repo, env=env, check=True, capture_output=True)
        record = {'schema_name': 'replay_decision_input', 'schema_version': 1, 'decision_key': self.key,
                  'shared_snapshot_id': 'snapshot', 'shared_candidate_id': 'candidate', 'market_id': 'market',
                  'observed_at': self.key['observed_at_utc'],
                  'market': {'question': 'Will Miami high exceed 80F?', 'best_yes_ask': .4, 'best_no_ask': .6,
                             'market_metadata': {'city_id': 'miami_fl', 'market_kind': 'high', 'contract_shape': 'range'}},
                  'source_inputs': {'source_context': {'source': 'weather', 'data': {'weather_source_snapshot': {
                      'predicted_prob': .8, 'forecast': {'threshold': 80.0, 'question_side': 'above'},
                      'sources': [{'source_id': 'nws', 'source_name': 'NWS', 'forecast_high': 82.0}]}}}}}
        record['canonical_input_sha256'] = digest(json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
        inputs, empty = self.root / 'inputs.jsonl', self.root / 'empty.jsonl'
        inputs.write_bytes(encode([record]))
        empty.write_bytes(b'')
        outcome = {'decision_key': self.key, 'canonical_input_sha256': record['canonical_input_sha256'],
                   'market_id': 'market', 'market_status': 'finalized', 'official_outcome': 'YES',
                   'settlement_ts': '2026-01-02T12:00:00+00:00', 'resolution_id': 'synthetic'}
        self.outcomes.write_bytes(encode([outcome]))
        stores = [self.store.parent / 'producer-baseline', self.store.parent / 'producer-candidate']
        ids = []
        for store in stores:
            result = subprocess.run([sys.executable, str(repo / 'scripts/auto_research.py'), 'run', '--store', str(store),
                '--inputs', str(inputs), '--outcomes', str(empty), '--idea', 'synthetic',
                '--max-input-bytes', '65536', '--max-outcome-bytes', '65536'], cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            ids.append(json.loads(result.stdout)['experiment_id'])
        self.store = stores[0]
        result = self.run_cli(candidate_store=stores[1], baseline=ids[0], candidate=ids[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.output.read_text())
        self.assertEqual([r['selected_rows'] for r in report['runs']], [1, 1])
        self.assertEqual([r['resolved_rows'] for r in report['runs']], [1, 1])
        self.assertTrue(report['producer_coverage_counts_match'])
        self.assertTrue(all(r['selected_coverage']['counts_reconcile'] for r in report['runs']))
        self.assertFalse(report['diagnostic_eligible'])
        self.assertFalse((stores[0] / 'runs' / ids[1]).exists())

    def test_scores_later_outcome_after_empty_outcome_decision_runs(self):
        run = self.run_cli()
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(self.output.read_text())
        self.assertFalse(result['diagnostic_eligible'])
        self.assertEqual(result['runs'][0]['selected_coverage']['status'], 'complete_for_producer_seen_rows')
        self.assertEqual(result['reproducibility']['compared_replay_settings']['min_sample_count'], 5)
        self.assertEqual(result['reproducibility']['scorer_sha256'], digest(SCRIPT.read_bytes()))
        self.assertEqual(result['reproducibility']['resolver_sha256'], digest((ROOT / 'bot/weather/collector_source_router_replay.py').read_bytes()))
        self.assertEqual(result['runs'][0]['directionally_incorrect_rows'], 1)
        self.assertEqual(result['runs'][1]['resolved_skip_rows'], 1)
        self.assertEqual(result['changed_resolved_decisions'][0]['baseline_direction'], 'incorrect')
        self.assertEqual(result['changed_resolved_decisions'][0]['count'], 1)
        # P&L appears only as the separately labelled hypothetical quote proxy, overall and per changed decision.
        self.assertEqual(result['schema_version'], 3)
        paired = result.pop('paired_changed_decisions')
        self.assertEqual(len(paired), 1)
        self.assertEqual((paired[0]['market_id'], paired[0]['official_outcome']), ('market', 'NO'))
        self.assertEqual(paired[0]['baseline']['proxy_pnl_usd'], -1.0)
        self.assertEqual((paired[0]['candidate']['action'], paired[0]['candidate']['proxy_pnl_usd']), ('SKIP', 0.0))
        self.assertTrue(paired[0]['unit_id'])
        self.assertFalse(result.pop('paired_changed_decisions_verified'))  # no --inputs: units/prices unverified
        directional = [{k: v for k, v in r.items() if k != 'hypothetical_quote_proxy'} for r in result['runs']]
        self.assertNotIn('pnl', json.dumps({**result, 'runs': directional}).lower())
        proxies = [r['hypothetical_quote_proxy'] for r in result['runs']]
        self.assertEqual(proxies[0]['hypothetical_ask_quote_pnl_total_usd'], -1.0)
        self.assertEqual(proxies[1]['hypothetical_ask_quote_pnl_decisions'], 0)
        self.assertIn('not a fill', proxies[0]['semantics'])
        self.assertEqual(self.outcomes.read_bytes(), encode([{'decision_key': self.key, 'canonical_input_sha256': 'b' * 64,
                 'market_id': 'market', 'market_status': 'finalized', 'official_outcome': 'NO',
                 'settlement_ts': '2026-01-02T12:00:00+00:00', 'resolution_id': 'resolution'}]))

    def test_paired_prices_and_stakes_must_match_trusted_inputs(self):
        import scripts.research_feedback as feedback
        from tests.test_collector_source_router_replay import replay_input
        record = replay_input(market_id='m', observed_at='2026-08-12T15:00:00+00:00', raw_hash='a' * 64)
        decision = {'action': 'BUY_YES', 'side': 'YES', 'entry_price': 0.40, 'price_basis': 'recorded_executable_ask',
                    'fixed_stake_usd': 1.0}
        proxied = {'hypothetical_ask_quote_pnl_usd': 1.5}
        self.assertEqual(feedback.proxy_side(decision, proxied, record)['proxy_pnl_usd'], 1.5)
        for change in ({'entry_price': 0.10}, {'price_basis': 'recorded_market_reference'}, {'fixed_stake_usd': 5.0}):
            with self.subTest(change=change):
                side = feedback.proxy_side({**decision, **change}, proxied, record)
                self.assertFalse(side['price_verified'])
                self.assertIsNone(side['proxy_pnl_usd'])
        self.assertEqual(feedback.proxy_side({'action': 'SKIP'}, {}, record)['proxy_pnl_usd'], 0.0)
        self.assertNotIn('price_verified', feedback.proxy_side(decision, proxied, None))

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
        changed.pop('decision_id')
        changed['decision_id'] = 'sha256:' + digest(_canonical_json_bytes(changed))
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
        row['decision_id'] = 'sha256:' + digest(_canonical_json_bytes(row))
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

    def test_incomplete_producer_coverage_is_disclosed(self):
        with sqlite3.connect(self.store / 'experiments.sqlite3') as db:
            for name in self.rows:
                result = json.loads(db.execute('SELECT result_json FROM experiments WHERE experiment_id=?', (name,)).fetchone()[0])
                result['decision_stats']['input_records_seen'] = 2
                result['decision_stats']['input_records_invalid_identity'] = 1
                db.execute('UPDATE experiments SET result_json=? WHERE experiment_id=?', (json.dumps(result), name))
        run = self.run_cli()
        self.assertEqual(run.returncode, 0, run.stderr)
        coverage = json.loads(self.output.read_text())['runs'][0]['selected_coverage']
        self.assertEqual(coverage['status'], 'incomplete')
        self.assertEqual(coverage['input_records_seen'], 2)
        self.assertEqual(coverage['input_records_invalid_identity'], 1)
        self.assertEqual(coverage['produced_control_decisions'], 1)
        self.assertTrue(coverage['counts_reconcile'])
        report = json.loads(self.output.read_text())
        self.assertTrue(report['producer_coverage_counts_match'])
        self.assertFalse(report['diagnostic_eligible'])

    def test_candidate_count_contradiction_is_not_reconciled(self):
        with sqlite3.connect(self.store / 'experiments.sqlite3') as db:
            for name in self.rows:
                result = json.loads(db.execute('SELECT result_json FROM experiments WHERE experiment_id=?', (name,)).fetchone()[0])
                result['decision_stats']['sealed_candidate_decisions'] = 0
                db.execute('UPDATE experiments SET result_json=? WHERE experiment_id=?', (json.dumps(result), name))
        run = self.run_cli()
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(self.output.read_text())
        for item in report['runs']:
            self.assertEqual(item['selected_coverage']['status'], 'incomplete')
            self.assertFalse(item['selected_coverage']['counts_reconcile'])
        self.assertIsNone(report['producer_coverage_counts_match'])
        self.assertFalse(report['diagnostic_eligible'])

    def test_valid_but_different_partial_coverage_counts_do_not_match(self):
        with sqlite3.connect(self.store / 'experiments.sqlite3') as db:
            result = json.loads(db.execute("SELECT result_json FROM experiments WHERE experiment_id='candidate'").fetchone()[0])
            result['decision_stats']['input_records_seen'] = 2
            result['decision_stats']['input_records_invalid_identity'] = 1
            db.execute("UPDATE experiments SET result_json=? WHERE experiment_id='candidate'", (json.dumps(result),))
        run = self.run_cli()
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(self.output.read_text())
        self.assertFalse(report['producer_coverage_counts_match'])
        self.assertTrue(report['runs'][1]['selected_coverage']['counts_reconcile'])
        self.assertEqual(report['runs'][1]['selected_coverage']['status'], 'incomplete')
        self.assertFalse(report['diagnostic_eligible'])

    def test_missing_replay_setting_in_either_or_both_configs_is_rejected(self):
        keys = ('min_sample_count', 'max_rows', 'history', 'cohort_per_shape_target')
        original = {'min_sample_count': 5, 'max_rows': None, 'history': None, 'cohort_per_shape_target': 30}
        for key in keys:
            for missing_from in (('baseline',), ('candidate',), ('baseline', 'candidate')):
                with self.subTest(key=key, missing_from=missing_from):
                    with sqlite3.connect(self.store / 'experiments.sqlite3') as db:
                        for name in self.rows:
                            config = dict(original)
                            if name in missing_from:
                                config.pop(key)
                            db.execute('UPDATE experiments SET config_json=? WHERE experiment_id=?', (json.dumps(config), name))
                    output = self.output.parent / (key + '-' + '-'.join(missing_from) + '.json')
                    run = self.run_cli(output=output)
                    self.assertNotEqual(run.returncode, 0)
                    self.assertIn('missing replay settings', run.stderr)
                    self.assertFalse(output.exists())
        with sqlite3.connect(self.store / 'experiments.sqlite3') as db:
            db.execute('UPDATE experiments SET config_json=?', (json.dumps(original),))
        run = self.run_cli()
        self.assertEqual(run.returncode, 0, run.stderr)
        settings = json.loads(self.output.read_text())['reproducibility']['compared_replay_settings']
        self.assertEqual(settings, original)

    def test_null_nonoptional_replay_settings_are_rejected(self):
        for key in ('min_sample_count', 'cohort_per_shape_target'):
            with self.subTest(key=key):
                with sqlite3.connect(self.store / 'experiments.sqlite3') as db:
                    for name in self.rows:
                        config = json.loads(db.execute('SELECT config_json FROM experiments WHERE experiment_id=?', (name,)).fetchone()[0])
                        config[key] = None
                        db.execute('UPDATE experiments SET config_json=? WHERE experiment_id=?', (json.dumps(config), name))
                output = self.output.parent / (key + '.json')
                run = self.run_cli(output=output)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn('invalid replay settings', run.stderr)
                self.assertFalse(output.exists())

    def test_stale_seal_rejected_even_when_artifact_hash_matches(self):
        row = dict(self.rows['baseline'][0], action='SKIP')
        raw = encode([row])
        (self.store / 'runs/baseline/sealed_control_decisions.jsonl').write_bytes(raw)
        with sqlite3.connect(self.store / 'experiments.sqlite3') as db:
            result = json.loads(db.execute("SELECT result_json FROM experiments WHERE experiment_id='baseline'").fetchone()[0])
            result['artifacts']['sealed_control_decisions.jsonl'] = digest(raw)
            db.execute("UPDATE experiments SET result_json=? WHERE experiment_id='baseline'", (json.dumps(result),))
        run = self.run_cli()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('decision seal mismatch', run.stderr)
        self.assertFalse(self.output.exists())

    def test_unknown_or_disagreeing_coverage_is_not_full(self):
        with sqlite3.connect(self.store / 'experiments.sqlite3') as db:
            result = json.loads(db.execute("SELECT result_json FROM experiments WHERE experiment_id='candidate'").fetchone()[0])
            result.pop('decision_stats')
            db.execute("UPDATE experiments SET result_json=? WHERE experiment_id='candidate'", (json.dumps(result),))
        run = self.run_cli()
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(self.output.read_text())
        self.assertEqual(report['runs'][1]['selected_coverage']['status'], 'unknown')
        self.assertIsNone(report['producer_coverage_counts_match'])
        self.output.unlink()
        with sqlite3.connect(self.store / 'experiments.sqlite3') as db:
            result['decision_stats'] = {'input_records_seen': 2, 'input_records_invalid_identity': 0,
                                        'sealed_control_decisions': 2, 'sealed_candidate_decisions': 2}
            db.execute("UPDATE experiments SET result_json=? WHERE experiment_id='candidate'", (json.dumps(result),))
        run = self.run_cli()
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(self.output.read_text())
        self.assertEqual(report['runs'][1]['selected_coverage']['status'], 'incomplete')
        self.assertFalse(report['runs'][1]['selected_coverage']['counts_reconcile'])
        self.assertIsNone(report['producer_coverage_counts_match'])

    def test_seal_requires_producer_newline(self):
        row = dict(self.rows['baseline'][0])
        row.pop('decision_id')
        row['decision_id'] = 'sha256:' + digest(_canonical_json_bytes(row).rstrip(b'\n'))
        raw = encode([row])
        (self.store / 'runs/baseline/sealed_control_decisions.jsonl').write_bytes(raw)
        with sqlite3.connect(self.store / 'experiments.sqlite3') as db:
            result = json.loads(db.execute("SELECT result_json FROM experiments WHERE experiment_id='baseline'").fetchone()[0])
            result['artifacts']['sealed_control_decisions.jsonl'] = digest(raw)
            db.execute("UPDATE experiments SET result_json=? WHERE experiment_id='baseline'", (json.dumps(result),))
        run = self.run_cli()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('decision seal mismatch', run.stderr)
        self.assertFalse(self.output.exists())

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
