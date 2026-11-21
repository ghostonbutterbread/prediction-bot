#!/usr/bin/env python3
"""Run and queue versioned offline source-only replay experiments.

This is an agent-operated research tool, not a scheduler or paper controller.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def require_local_file(root: Path, path: Path) -> None:
    """Reject store-internal links; the mounted root itself is caller-trusted."""
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f'symlink in experiment store: {current}')
    if not path.is_file():
        raise ValueError(f'experiment file missing: {path}')


def git(*args: str) -> str:
    return subprocess.run(['git', *args], cwd=ROOT, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def database(store: Path) -> sqlite3.Connection:
    db = sqlite3.connect(store / 'experiments.sqlite3')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS experiments (
            experiment_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, idea TEXT NOT NULL,
            git_commit TEXT NOT NULL, git_tree TEXT NOT NULL,
            input_sha256 TEXT NOT NULL, outcome_sha256 TEXT NOT NULL,
            config_json TEXT NOT NULL, result_json TEXT NOT NULL, output_dir TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidate_queue (
            experiment_id TEXT PRIMARY KEY REFERENCES experiments(experiment_id),
            created_at TEXT NOT NULL, note TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS experiment_no_update BEFORE UPDATE ON experiments
        BEGIN SELECT RAISE(ABORT, 'experiments are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS experiment_no_delete BEFORE DELETE ON experiments
        BEGIN SELECT RAISE(ABORT, 'experiments are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS queue_no_update BEFORE UPDATE ON candidate_queue
        BEGIN SELECT RAISE(ABORT, 'queue is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS queue_no_delete BEFORE DELETE ON candidate_queue
        BEGIN SELECT RAISE(ABORT, 'queue is append-only'); END;
    ''')
    db.execute('PRAGMA foreign_keys=ON')
    return db


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CronArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        super().error('[CRON_FAILURE] ' + message)


def main() -> int:
    parser = CronArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--store', type=Path, required=True, help='directory beneath collector data/derived_reports')
    run.add_argument('--inputs', type=Path, required=True, help='sanitized replay_decision_input JSONL')
    run.add_argument('--outcomes', type=Path, required=True, help='separate finalized-outcome JSONL')
    run.add_argument('--idea', required=True)
    run.add_argument('--min-sample-count', type=int, default=5)
    run.add_argument('--max-rows', type=int, default=None, help='maximum selected input object rows, not an I/O bound')
    run.add_argument('--max-input-bytes', type=int, required=True, help='maximum complete input file size to hash and read')
    run.add_argument('--max-outcome-bytes', type=int, required=True, help='maximum complete outcome file size to hash and load')
    queue = sub.add_parser('queue', help='record a manual-review handoff, never activate')
    queue.add_argument('--store', type=Path, required=True)
    queue.add_argument('--experiment', required=True)
    queue.add_argument('--note', required=True)
    compare = sub.add_parser('compare', help='compare two matched source-only runs')
    compare.add_argument('--store', type=Path, required=True)
    compare.add_argument('--candidate-store', type=Path, help='separate candidate store; remaps both run paths under their respective stores')
    compare.add_argument('experiments', nargs=2)
    args = parser.parse_args()

    from bot.collector_paths import derived_reports_root
    store = args.store.resolve()
    derived = derived_reports_root().resolve()
    if store == derived or not store.is_relative_to(derived):
        raise ValueError('store must be a separate directory beneath data/derived_reports')
    if args.command == 'compare':
        if args.experiments[0] == args.experiments[1]:
            raise ValueError('comparison requires two distinct experiments')
        candidate_store = args.candidate_store.resolve() if args.candidate_store else store
        if candidate_store == derived or not candidate_store.is_relative_to(derived):
            raise ValueError('candidate store must be beneath data/derived_reports')
        stores = (store, candidate_store)
        if args.candidate_store and (store == candidate_store or store.is_relative_to(candidate_store) or candidate_store.is_relative_to(store)):
            raise ValueError('baseline and candidate stores must be distinct and non-overlapping')
        rows = []
        for source_store, item in zip(stores, args.experiments):
            dbpath = source_store / 'experiments.sqlite3'
            require_local_file(source_store, dbpath)
            if args.candidate_store:
                if any((source_store / ('experiments.sqlite3' + suffix)).exists() for suffix in ('-wal', '-journal')):
                    raise ValueError('store has pending SQLite journal; reconcile before comparison')
                uri = dbpath.as_uri() + '?mode=ro&immutable=1'
            else:
                uri = dbpath.as_uri() + '?mode=ro'
            with sqlite3.connect(uri, uri=True) as db:
                rows.append(db.execute('SELECT git_commit, input_sha256, outcome_sha256, config_json, result_json, output_dir FROM experiments WHERE experiment_id=?', (item,)).fetchone())
        if any(row is None for row in rows):
            raise ValueError('unknown experiment')
        config_a, config_b = (json.loads(row[3]) for row in rows)
        keys = ('min_sample_count', 'max_rows', 'history', 'cohort_per_shape_target')
        if rows[0][1:3] != rows[1][1:3] or any(config_a[key] != config_b[key] for key in keys):
            raise ValueError('experiments have different input, outcome, or replay settings')
        summaries, identities = [], []
        for experiment_id, row, source_store in zip(args.experiments, rows, stores):
            result = json.loads(row[4])
            if args.candidate_store:
                if len(experiment_id) != 32 or any(c not in '0123456789abcdef' for c in experiment_id):
                    raise ValueError('invalid experiment identity')
                directory = source_store / 'runs' / experiment_id
            else:
                directory = Path(row[5])
            if result.get('economic_status') != 'excluded_quote_only':
                raise ValueError('legacy economic results are ineligible; rerun in a new output directory')
            required = {'sealed_control_decisions.jsonl', 'sealed_candidate_decisions.jsonl', 'post_decision_resolution_report.json'}
            if not required.issubset(result['artifacts']) or any(Path(name).name != name for name in result['artifacts']):
                raise ValueError('required artifact hashes missing or invalid')
            for artifact, expected_hash in result['artifacts'].items():
                if args.candidate_store:
                    require_local_file(source_store, directory / artifact)
                if sha256(directory / artifact) != expected_hash:
                    raise ValueError('artifact hash mismatch')
            lanes, lane_keys, lane_ids = {}, [], {}
            for name, label in (('sealed_control_decisions.jsonl', 'control'), ('sealed_candidate_decisions.jsonl', 'candidate')):
                path = directory / name
                decisions = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
                identity = [json.dumps([d['decision_key'], d['canonical_input_sha256']], sort_keys=True) for d in decisions]
                if len(set(identity)) != len(identity):
                    raise ValueError('duplicate decision identity')
                lane_keys.append(set(identity))
                actions = {}
                for decision in decisions:
                    action = decision['action']
                    actions[action] = actions.get(action, 0) + 1
                lane_ids[label] = {d['decision_id'] for d in decisions}
                lanes[label] = {'actions': actions}
            if lane_keys[0] != lane_keys[1]:
                raise ValueError('control and candidate identities differ')
            report = json.loads((directory / 'post_decision_resolution_report.json').read_text())
            for label in lanes:
                lanes[label]['resolved'] = sum(row['decision_id'] in lane_ids[label] for row in report['resolved_decisions'])
                lanes[label]['unresolved'] = sum(row['decision_id'] in lane_ids[label] for row in report['unresolved_decisions'])
            identities.append(lane_keys[0])
            stats = result['decision_stats']
            coverage = {key: stats[key] for key in ('input_records_seen', 'input_records_invalid_identity',
                                                       'sealed_control_decisions', 'sealed_candidate_decisions')}
            summaries.append({'experiment_id': experiment_id, 'git_commit': row[0],
                              'lanes': lanes, 'coverage': coverage})
        if identities[0] != identities[1]:
            raise ValueError('experiment decision identities differ')
        matched = bool(identities[0]) and all(
            run['coverage']['input_records_invalid_identity'] == 0
            and run['coverage']['input_records_seen'] == len(identities[0])
            for run in summaries
        )
        print(json.dumps({'matched_inputs_and_settings': False, 'selected_rows_matched': matched,
                          'input_sha256': rows[0][1], 'outcome_sha256': rows[0][2],
                          'matched_decisions': len(identities[0]),
                          'diagnostic_eligible': False,
                          'coverage_scope': 'selected input rows only; no independent opportunity roster',
                          'runs': summaries, 'warning': 'source availability unverified; asks are quotes, not fills; not promotion evidence'}))
        return 0
    if args.command == 'queue':
        if not (store / 'experiments.sqlite3').is_file():
            raise ValueError('experiment store does not exist')
        with database(store) as db:
            db.execute('INSERT INTO candidate_queue VALUES (?, ?, ?)', (args.experiment, now(), args.note))
        print(json.dumps({'queued': args.experiment, 'activation': 'manual_review_only',
                          'diagnostic_eligible': False, 'warning': 'source availability unverified'}))
        return 0

    if git('rev-parse', '--show-toplevel') != str(ROOT):
        raise ValueError('run from a Git checkout containing this CLI')
    if git('status', '--porcelain', '--untracked-files=all'):
        raise ValueError('commit all code and config changes before running; checkout is dirty')
    commit, tree = git('rev-parse', 'HEAD'), git('rev-parse', 'HEAD^{tree}')
    inputs, outcomes = args.inputs.resolve(), args.outcomes.resolve()
    if not inputs.is_file() or not outcomes.is_file() or inputs == outcomes:
        raise ValueError('distinct existing replay inputs and outcomes files are required')
    if args.min_sample_count < 1 or (args.max_rows is not None and args.max_rows < 1):
        raise ValueError('min-sample-count and max-rows must be positive')
    if args.max_input_bytes < 1 or args.max_outcome_bytes < 1:
        raise ValueError('byte limits must be positive')
    for name, path, limit, option in (('inputs', inputs, args.max_input_bytes, '--max-input-bytes'),
                                       ('outcomes', outcomes, args.max_outcome_bytes, '--max-outcome-bytes')):
        if path.stat().st_size > limit:
            raise ValueError(f'{name} exceeds {option} ({limit} bytes)')
    input_hash, outcome_hash = sha256(inputs), sha256(outcomes)
    # A self-consistent digest does not prove a caller actually projected away
    # outcomes or recorded decisions. Screen the supplied rows with the same
    # recursive classifier used by the canonical projector before replay.
    from bot.replay_decision_input import _copied_subtree_forbidden_errors
    with inputs.open(encoding='utf-8') as stream:
        accepted_rows = 0
        for number, line in enumerate(stream, 1):
            if args.max_rows is not None and accepted_rows >= args.max_rows:
                break
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f'non-object replay input at line {number}; selected coverage would be incomplete')
            accepted_rows += 1
            errors = _copied_subtree_forbidden_errors(payload, 'input')
            if errors:
                raise ValueError(f'forbidden replay field at line {number}: {errors[0].path}')
    config = {'min_sample_count': args.min_sample_count, 'max_rows': args.max_rows,
              'max_input_bytes': args.max_input_bytes, 'max_outcome_bytes': args.max_outcome_bytes,
              'resource_scope': 'whole input and outcome files; stable immutable regular files required',
              'history': None, 'cohort_per_shape_target': 30, 'python': sys.version,
              'inputs_path': str(inputs), 'outcomes_path': str(outcomes)}
    experiment_id = uuid.uuid4().hex
    output_dir = store / 'runs' / experiment_id

    from bot.weather.collector_source_router_replay import run_collector_source_router_replay
    result = run_collector_source_router_replay(
        replay_inputs_path=inputs, finalized_outcomes_path=outcomes, output_dir=output_dir,
        min_sample_count=args.min_sample_count, max_rows=args.max_rows, directional_only=True)
    if (sha256(inputs), sha256(outcomes)) != (input_hash, outcome_hash):
        raise ValueError('input changed during replay; artifacts are incomplete and not registered')
    if git('status', '--porcelain', '--untracked-files=all') or git('rev-parse', 'HEAD') != commit:
        raise ValueError('checkout changed during replay; artifacts are incomplete and not registered')
    result_data = {'diagnostic_eligible': False, 'source_time_status': 'unverified',
                   'economic_status': 'excluded_quote_only',
                   'decision_stats': result.metadata['decision_stats'],
                   'resolution_summary': result.metadata['resolution_summary'],
                   'artifacts': {path.name: sha256(path) for path in sorted(output_dir.iterdir()) if path.is_file()},
                   'interpretation': 'source-only directional research; source availability unverified; economics excluded'}
    store.mkdir(parents=True, exist_ok=True)
    with database(store) as db:
        db.execute('INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                   (experiment_id, now(), args.idea, commit, tree, input_hash, outcome_hash,
                    json.dumps(config, sort_keys=True), json.dumps(result_data, sort_keys=True), str(output_dir)))
    print(json.dumps({'experiment_id': experiment_id, 'git_commit': commit, 'output_dir': str(output_dir)}))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print(f'[CRON_FAILURE] auto_research: {exc}', file=sys.stderr)
        raise SystemExit(1)
