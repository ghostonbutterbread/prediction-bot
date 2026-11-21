#!/usr/bin/env python3
"""Bind later resolutions to completed source-only research inputs.

The separate resolution feed is read only. This produces diagnostic evidence,
not source authority, paper settlement, trading permission, or a profitability claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot.collector_paths import derived_reports_root
from bot.replay_outcome_binding import bind_replay_finalized_outcomes

EMPTY = hashlib.sha256(b'').hexdigest()


def fingerprint(path: Path, maximum: int) -> tuple[str, int]:
    if maximum <= 0:
        raise ValueError('byte limits must be positive')
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as f:
        while True:
            chunk = f.read(min(1024 * 1024, maximum + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                raise ValueError(f'file exceeds byte limit: {path}')
            digest.update(chunk)
    return digest.hexdigest(), size


def reject_json_constant(value: str):
    raise ValueError(f'invalid JSON constant: {value}')


def fingerprint_input(path: Path, maximum: int) -> tuple[str, int, int]:
    if maximum <= 0:
        raise ValueError('byte limits must be positive')
    digest, size, rows = hashlib.sha256(), 0, 0
    with path.open('rb') as stream:
        while True:
            line = stream.readline(maximum + 1 - size)
            if not line:
                break
            size += len(line)
            if size > maximum:
                raise ValueError(f'file exceeds byte limit: {path}')
            digest.update(line)
            if line.strip():
                if not isinstance(json.loads(line, parse_constant=reject_json_constant), dict):
                    raise ValueError('every nonblank input row must be a JSON object')
                rows += 1
    return digest.hexdigest(), size, rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comparison', required=True, type=Path)
    parser.add_argument('--inputs', required=True, type=Path)
    parser.add_argument('--resolutions', required=True, type=Path)
    parser.add_argument('--resolution-sha256', required=True)
    parser.add_argument('--max-input-bytes', required=True, type=int)
    parser.add_argument('--max-resolution-bytes', required=True, type=int)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        comparison, inputs, resolutions = (p.resolve(strict=True) for p in (args.comparison, args.inputs, args.resolutions))
        output = args.output.resolve()
        root = derived_reports_root().resolve()
        if output == root or not output.is_relative_to(root):
            raise ValueError('output must be a separate directory beneath data/derived_reports')
        if output.exists():
            raise ValueError('output already exists')
        if not output.parent.is_dir():
            raise ValueError('output parent must already exist')
        if any(path == output or path.is_relative_to(output) for path in (comparison, inputs, resolutions)):
            raise ValueError('output must not contain an input')
        identities = [(stat.st_dev, stat.st_ino) for stat in
                      (path.stat() for path in (comparison, inputs, resolutions))]
        if len(set(identities)) != 3:
            raise ValueError('comparison, inputs, and resolutions must be distinct files')
        with comparison.open('rb') as stream:
            comparison_bytes = stream.read(1024 * 1024 + 1)
        comparison_size = len(comparison_bytes)
        if comparison_size > 1024 * 1024:
            raise ValueError(f'file exceeds byte limit: {comparison}')
        comparison_hash = hashlib.sha256(comparison_bytes).hexdigest()
        comp = json.loads(comparison_bytes)
        if not isinstance(comp, dict) or comp.get('selected_rows_matched') is not True:
            raise ValueError('matched sealed comparison is required')
        if comp.get('outcome_sha256') != EMPTY:
            raise ValueError('original outcomes must be empty')
        runs = comp.get('runs')
        if (not isinstance(runs, list) or len(runs) != 2 or
                any(not isinstance(r, dict) or not isinstance(r.get('experiment_id'), str)
                    or not r['experiment_id'].strip() for r in runs) or
                runs[0]['experiment_id'] == runs[1]['experiment_id']):
            raise ValueError('comparison must identify two distinct experiments')
        matched = comp.get('matched_decisions')
        if type(matched) is not int or matched <= 0:
            raise ValueError('comparison must report a nonzero matched decision count')
        expected = {'input_records_seen': matched, 'input_records_invalid_identity': 0,
                    'sealed_control_decisions': matched, 'sealed_candidate_decisions': matched}
        for run in runs:
            coverage = run.get('coverage')
            if not isinstance(coverage, dict) or any(
                type(coverage.get(key)) is not int or coverage[key] != count
                for key, count in expected.items()
            ):
                raise ValueError('comparison coverage does not reconcile with matched decisions')
        input_hash, input_size, input_rows = fingerprint_input(inputs, args.max_input_bytes)
        if input_hash != comp.get('input_sha256'):
            raise ValueError('input hash mismatch')
        if input_rows != comp.get('matched_decisions'):
            raise ValueError('input rows differ from matched decisions; rerun both experiments and comparison with the complete selected feed page')
        resolution_hash, resolution_size = fingerprint(resolutions, args.max_resolution_bytes)
        if resolution_hash != args.resolution_sha256:
            raise ValueError('resolution hash mismatch')
        # A producer may append/replace live files. This CLI requires stable,
        # retained evidence; the recheck detects changes but is not a lock.
        output.mkdir(exist_ok=False)
        result = bind_replay_finalized_outcomes(replay_inputs_path=inputs, strict_resolutions_path=resolutions,
                                                output_dir=output / 'binding')
        if (fingerprint(inputs, args.max_input_bytes)[0] != input_hash or
                fingerprint(resolutions, args.max_resolution_bytes)[0] != resolution_hash or
                fingerprint(comparison, 1024 * 1024)[0] != comparison_hash):
            raise ValueError('input changed during binding; output is incomplete and must not be used')
        if (result.metadata['inputs']['replay_inputs_sha256'] != input_hash or
                result.metadata['inputs']['strict_resolutions_sha256'] != resolution_hash):
            raise ValueError('binder read different inputs; output is incomplete and must not be used')
        receipt = {'schema_name':'research_retrospective_outcome_binding', 'schema_version':1,
                   'comparison_sha256':comparison_hash, 'comparison_bytes':comparison_size,
                   'experiment_ids':[r['experiment_id'] for r in runs],
                   'input_path':str(inputs), 'input_sha256':input_hash, 'input_bytes':input_size,
                   'resolution_path':str(resolutions), 'resolution_sha256':resolution_hash,
                   'resolution_bytes':resolution_size, 'binding_counts':result.metadata['binding_counts'],
                   'outcomes_sha256':result.metadata['output_hash'], 'diagnostic_eligible':False,
                   'source_time_status':'unverified',
                   'resolution_authority':'caller-pinned retained bytes; producer and clock unverified',
                   'limitations':['retrospective only; not paper settlement, profit, or promotion evidence',
                                  'matching hashes do not authenticate source or establish complete blindness',
                                  'stable immutable source files are a caller responsibility; retain exact resolution evidence']}
        with (output / 'receipt.json').open('x', encoding='utf-8') as f:
            json.dump(receipt, f, sort_keys=True, indent=2, allow_nan=False)
            f.write('\n')
        print(json.dumps({'output':str(output), 'binding_counts':receipt['binding_counts'],
                          'outcomes_sha256':receipt['outcomes_sha256'], 'diagnostic_eligible':False}))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f'prepare_research_outcomes: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
