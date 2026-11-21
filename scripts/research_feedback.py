#!/usr/bin/env python3
"""Retrospectively score two previously sealed source-only research runs.

This process is trusted analysis code, separate from candidate code. It does
not establish source-time or outcome authority, trading profit, or promotion.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot.collector_paths import derived_reports_root
from bot.weather import collector_source_router_replay as resolver
from bot.weather.collector_source_router_replay import resolve_sealed_source_probability_decisions

EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()
ARTIFACT = 'sealed_control_decisions.jsonl'
REPLAY_SETTINGS = ('min_sample_count', 'max_rows', 'history', 'cohort_per_shape_target')


def read_bounded(path: Path, maximum: int) -> bytes:
    if maximum < 1:
        raise ValueError('byte limits must be positive')
    with path.open('rb') as stream:
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError(f'{path} exceeds byte limit {maximum}')
    return value


def read_jsonl(content: bytes) -> list[dict]:
    rows = []
    for i, raw in enumerate(content.splitlines(), 1):
        if not raw.strip():
            continue
        row = json.loads(raw)
        if not isinstance(row, dict):
            raise ValueError(f'JSONL line {i} is not an object')
        rows.append(row)
    return rows


def identity(row: dict) -> str:
    key, digest = row.get('decision_key'), row.get('canonical_input_sha256')
    if not isinstance(key, dict) or not isinstance(digest, str) or len(digest) != 64:
        raise ValueError('decision lacks complete input identity')
    return json.dumps([key, digest], sort_keys=True, separators=(',', ':'))


def load_run(db: sqlite3.Connection, store: Path, experiment: str, maximum: int) -> dict:
    row = db.execute('SELECT git_commit,input_sha256,outcome_sha256,config_json,result_json,output_dir FROM experiments WHERE experiment_id=?', (experiment,)).fetchone()
    if row is None:
        raise ValueError(f'unknown experiment: {experiment}')
    commit, input_hash, old_outcome_hash, config_json, result_json, directory = row
    if old_outcome_hash != EMPTY_SHA256:
        raise ValueError(f'original outcome ledger was not empty: {experiment}')
    config, result = json.loads(config_json), json.loads(result_json)
    if result.get('economic_status') != 'excluded_quote_only':
        raise ValueError('experiment is not a directional-only research run')
    directory = Path(directory).resolve(strict=True)
    if not directory.is_relative_to((store / 'runs').resolve()):
        raise ValueError('experiment output must be inside this research store')
    path = directory / ARTIFACT
    raw = read_bounded(path, maximum)
    observed_hash = hashlib.sha256(raw).hexdigest()
    if observed_hash != result.get('artifacts', {}).get(ARTIFACT):
        raise ValueError(f'artifact hash mismatch: {experiment}')
    decisions = read_jsonl(raw)
    identities = [identity(decision) for decision in decisions]
    if not decisions or len(identities) != len(set(identities)) or any(d.get('sealed') is not True for d in decisions):
        raise ValueError('empty, duplicate, or unsealed decision artifacts')
    for decision in decisions:
        content = dict(decision)
        claimed = content.pop('decision_id', None)
        expected = 'sha256:' + hashlib.sha256(resolver._canonical_json_bytes(content)).hexdigest()
        if claimed != expected:
            raise ValueError('decision seal mismatch')
    stats = result.get('decision_stats')
    names = ('input_records_seen', 'input_records_invalid_identity', 'sealed_control_decisions', 'sealed_candidate_decisions')
    if not isinstance(stats, dict) or any(type(stats.get(k)) is not int or stats[k] < 0 for k in names):
        coverage = {'status': 'unknown', 'reason': 'missing or invalid producer decision_stats'}
    else:
        seen, invalid, produced = (stats[k] for k in names[:3])
        complete = invalid == 0 and seen == produced == len(decisions)
        coverage = {'status': 'complete_for_producer_seen_rows' if complete else 'incomplete',
                    'input_records_seen': seen, 'input_records_invalid_identity': invalid,
                    'produced_control_decisions': produced,
                    'produced_candidate_decisions': stats['sealed_candidate_decisions'],
                    'artifact_control_decisions': len(decisions),
                    'counts_reconcile': seen == invalid + produced and produced == len(decisions)}
    return {'experiment_id': experiment, 'git_commit': commit, 'input_sha256': input_hash,
            'original_outcome_sha256': old_outcome_hash, 'config': config,
            'selected_coverage': coverage, 'sealed_decisions_sha256': observed_hash,
            'decisions': decisions, 'identities': set(identities)}


def valid_buy(row: dict) -> bool:
    return (row.get('action'), row.get('side')) in (('BUY_YES', 'YES'), ('BUY_NO', 'NO'))


def summarize(run: dict, outcomes: list[dict]) -> tuple[dict, dict]:
    report = resolve_sealed_source_probability_decisions(run['decisions'], outcomes, directional_only=True)
    resolved = report['resolved_decisions']
    scored = [r for r in resolved if valid_buy(r) and r['official_outcome'] in ('YES', 'NO')]
    counts = Counter('correct' if r['side'] == r['official_outcome'] else 'incorrect' for r in scored)
    result = {key: run[key] for key in ('experiment_id', 'git_commit', 'input_sha256', 'original_outcome_sha256', 'sealed_decisions_sha256')}
    result['selected_coverage'] = run['selected_coverage']
    result.update({'selected_rows': len(run['decisions']), 'resolved_rows': len(resolved),
                   'unresolved_rows': len(report['unresolved_decisions']),
                   'resolved_unique_market_ids': len({r['market_id'] for r in resolved}),
                   'resolved_buy_rows': len(scored), 'directionally_correct_rows': counts['correct'],
                   'directionally_incorrect_rows': counts['incorrect'],
                   'invalid_action_side_rows': sum(r['action'] in ('BUY_YES', 'BUY_NO') and not valid_buy(r) for r in resolved),
                   'resolved_skip_rows': sum(r['action'] == 'SKIP' for r in resolved),
                   'outcome_ledger_counts': report['summary']['outcome_ledger']})
    return result, {identity(row): row for row in resolved}


def scorer_identity() -> dict:
    # Git may be unavailable in a read-only source mount. File hashes bind the
    # named source files, not every imported dependency or the code in memory.
    try:
        commit = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL, text=True, timeout=5).strip()
        status = subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain', '--untracked-files=all'], stderr=subprocess.DEVNULL, text=True, timeout=5)
        dirty = bool(status.strip())
    except (OSError, subprocess.SubprocessError):
        commit, dirty = None, None
    return {'scorer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'resolver_sha256': hashlib.sha256(Path(resolver.__file__).read_bytes()).hexdigest(),
            'scorer_path': str(Path(__file__).resolve()), 'resolver_path': str(Path(resolver.__file__).resolve()),
            'git_commit': commit, 'tracked_or_untracked_dirty': dirty, 'python': sys.version,
            'limitations': 'File hashes are not producer authentication or proof of loaded code; Git status excludes ignored files and hashes do not bind other imports, installed dependencies, or runtime state.'}


def run(args: argparse.Namespace) -> dict:
    root, store = derived_reports_root().resolve(), args.store.resolve(strict=True)
    if store == root or not store.is_relative_to(root):
        raise ValueError('store must be a separate directory beneath data/derived_reports')
    output = args.output.resolve()
    if output == root or not output.is_relative_to(root) or output.is_relative_to(store):
        raise ValueError('output must be a separate path under data/derived_reports and outside store')
    if not output.parent.is_dir():
        raise ValueError('output parent directory must already exist')
    if output.exists():
        raise ValueError('output already exists')
    outcomes_raw = read_bounded(args.outcomes.resolve(strict=True), args.max_outcome_bytes)
    outcome_hash = hashlib.sha256(outcomes_raw).hexdigest()
    if outcome_hash != args.outcome_sha256:
        raise ValueError('outcome hash mismatch')
    outcomes = read_jsonl(outcomes_raw)
    dbpath = store / 'experiments.sqlite3'
    if not dbpath.is_file():
        raise ValueError('experiment database does not exist')
    with sqlite3.connect(dbpath.as_uri() + '?mode=ro', uri=True) as db:
        left = load_run(db, store, args.baseline, args.max_decision_bytes)
        right = load_run(db, store, args.candidate, args.max_decision_bytes)
    if left['input_sha256'] != right['input_sha256'] or any(left['config'].get(k) != right['config'].get(k) for k in REPLAY_SETTINGS):
        raise ValueError('experiments have different input or replay settings')
    if left['identities'] != right['identities']:
        raise ValueError('selected decision identities differ')
    a, am = summarize(left, outcomes)
    b, bm = summarize(right, outcomes)
    if set(am) != set(bm):
        raise ValueError('resolved decision identities differ')
    changes = Counter()
    for key in am:
        x, y = am[key], bm[key]
        if (x['official_outcome'], x['market_id']) != (y['official_outcome'], y['market_id']):
            raise ValueError('resolved outcome mismatch')
        if x['action'] != y['action']:
            baseline_direction = ('correct' if x['side'] == x['official_outcome'] else 'incorrect') if valid_buy(x) else 'not_scored'
            changes[(x['action'], y['action'], baseline_direction)] += 1
    result = {'schema_name': 'research_retrospective_directional_feedback', 'schema_version': 1,
              'diagnostic_eligible': False, 'source_time_status': 'unverified',
              'outcome_authority': 'caller-pinned bytes; producer and clock unverified',
              'outcome_sha256': outcome_hash, 'outcome_rows': len(outcomes),
              'sealed_decision_identities_matched': True, 'independent_opportunity_roster': False,
              'producer_coverage_counts_match': (left['selected_coverage'] == right['selected_coverage']
                  if all(r['selected_coverage']['status'] != 'unknown' for r in (left, right)) else None),
              'reproducibility': {**scorer_identity(), 'compared_replay_settings': {k: left['config'].get(k) for k in REPLAY_SETTINGS}},
              'runs': [a, b], 'changed_resolved_decisions': [
                  {'from': old, 'to': new, 'baseline_direction': direction, 'count': count}
                  for (old, new, direction), count in sorted(changes.items())],
              'limitations': ['retrospective directional feedback only, not profit or promotion evidence',
                              'matching market IDs does not prove independent physical events',
                              'original outcomes files were empty, but this alone does not prove complete blindness']}
    payload = (json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    # Publish only complete bytes and never replace an existing report.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.feedback-', dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {'output': str(output), 'sha256': hashlib.sha256(payload).hexdigest(), 'diagnostic_eligible': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--outcomes', type=Path, required=True)
    parser.add_argument('--outcome-sha256', required=True)
    parser.add_argument('--max-outcome-bytes', type=int, required=True)
    parser.add_argument('--max-decision-bytes', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args), sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(f'research_feedback: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
