#!/usr/bin/env python3
"""Select bounded frozen research inputs and verify declared completion evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


def read_bounded(path, limit):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError(f'not a regular file: {path}')
    with path.open('rb') as f:
        content = f.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f'file exceeds byte limit: {path}')
    return path, content


def digest(content):
    return hashlib.sha256(content).hexdigest()


def read_json(path, limit=1024 * 1024):
    try:
        return json.loads(read_bounded(path, limit)[1])
    except (ValueError, OSError) as exc:
        raise ValueError(f'missing or corrupt {path.name}: {exc}') from exc


def validate_selection(directory, pair, ih, oh, input_limit, outcome_limit):
    record = read_json(directory / 'selection.json')
    if not isinstance(record, dict) or record.get('pair_sha256') != pair or record.get('input_sha256') != ih or record.get('outcome_sha256') != oh:
        raise ValueError('selection identity or hash mismatch')
    if record.get('source_time_status') != 'unverified' or record.get('diagnostic_eligible') is not False:
        raise ValueError('corrupt selection safety status')
    for name, expected, limit in (('inputs.jsonl', ih, input_limit), ('outcomes.jsonl', oh, outcome_limit)):
        try:
            actual = digest(read_bounded(directory / name, limit)[1])
        except (ValueError, OSError) as exc:
            raise ValueError(f'missing or corrupt frozen {name}: {exc}') from exc
        if actual != expected:
            raise ValueError(f'frozen {name} hash mismatch')
    return record


def validate_completion(directory, pair, path=None):
    completion = read_json(path or directory / 'complete.json')
    if not isinstance(completion, dict) or completion.get('schema') != 'daily-research-completion-v1' or completion.get('pair_sha256') != pair:
        raise ValueError('corrupt completion receipt')
    artifacts = completion.get('artifacts')
    if not isinstance(artifacts, dict) or set(artifacts) != {'baseline.json', 'candidate.json', 'comparison.json'}:
        raise ValueError('completion artifacts missing or corrupt')
    records = {}
    for name, expected in artifacts.items():
        try:
            data = read_bounded(directory / name, 1024 * 1024)[1]
        except (OSError, ValueError) as exc:
            raise ValueError(f'completion artifact missing or corrupt: {name}') from exc
        if not isinstance(expected, str) or digest(data) != expected:
            raise ValueError(f'completion artifact hash mismatch: {name}')
        try:
            records[name] = json.loads(data)
        except ValueError as exc:
            raise ValueError(f'completion artifact corrupt: {name}') from exc
    a, b, comparison = (records[n] for n in ('baseline.json', 'candidate.json', 'comparison.json'))
    if not all(isinstance(x, dict) for x in (a, b, comparison)):
        raise ValueError('completion artifact must be a JSON object')
    runs = comparison.get('runs')
    if not isinstance(runs, list) or len(runs) != 2 or not all(isinstance(x, dict) for x in runs):
        raise ValueError('completion comparison runs missing')
    for receipt, run in zip((a, b), runs):
        if not isinstance(receipt.get('experiment_id'), str) or not receipt['experiment_id'] or receipt['experiment_id'] != run.get('experiment_id') or not isinstance(receipt.get('git_commit'), str) or receipt['git_commit'] != run.get('git_commit'):
            raise ValueError('completion experiment receipt mismatch')
    if a['experiment_id'] == b['experiment_id']:
        raise ValueError('completion requires two distinct experiments')
    if comparison.get('selected_rows_matched') is not True or comparison.get('diagnostic_eligible') is not False:
        raise ValueError('completion comparison not successfully matched')
    selection = read_json(directory / 'selection.json')
    if not isinstance(selection, dict) or comparison.get('input_sha256') != selection.get('input_sha256') or comparison.get('outcome_sha256') != digest(b''):
        raise ValueError('completion comparison does not bind frozen inputs and empty outcomes')
    return completion


def select(args):
    if min(args.max_input_bytes, args.max_outcome_bytes, args.max_catalog_bytes, args.max_total_bytes) < 1:
        raise ValueError('byte limits must be positive')
    catalog, catalog_bytes = read_bounded(args.catalog, args.max_catalog_bytes)
    entries = json.loads(catalog_bytes)
    if not isinstance(entries, list):
        raise ValueError('catalog must be a list')
    state = Path(args.state).resolve()
    chosen = None
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) and Path(entry[k]).is_absolute() for k in ('inputs', 'outcomes')):
            raise ValueError('catalog paths must be absolute strings')
        a, inputs = read_bounded(entry['inputs'], min(args.max_input_bytes, args.max_total_bytes - total))
        total += len(inputs)
        b, outcomes = read_bounded(entry['outcomes'], min(args.max_outcome_bytes, args.max_total_bytes - total))
        total += len(outcomes)
        if a.samefile(b) or not inputs.strip():
            raise ValueError('distinct files and nonempty inputs required')
        if any(p == state or p.is_relative_to(state) for p in (a, b, catalog)):
            raise ValueError('state must not contain catalog or source files')
        ih, oh = digest(inputs), digest(outcomes)
        pair = digest((ih + ':' + oh).encode())
        directory = state / pair
        if directory.exists():
            validate_selection(directory, pair, ih, oh, args.max_input_bytes, args.max_outcome_bytes)
            if (directory / 'complete.json').exists():
                validate_completion(directory, pair)
                continue
            if chosen is None:
                chosen = ('in_progress', directory, pair, a, b, ih, oh)
        elif chosen is None:
            chosen = ('selected', directory, pair, a, b, ih, oh)
    if chosen is None:
        return {'status': 'no_new_data'}
    status, directory, pair, a, b, ih, oh = chosen
    if status == 'in_progress':
        return {'status': status, 'directory': str(directory), 'pair_sha256': pair}
    # Reread only the chosen pair, and reject a source changed after validation.
    inputs = read_bounded(a, args.max_input_bytes)[1]
    outcomes = read_bounded(b, args.max_outcome_bytes)[1]
    if (digest(inputs), digest(outcomes)) != (ih, oh):
        raise ValueError('source changed during selection')
    directory.mkdir(parents=True)
    for name, content in (('inputs.jsonl', inputs), ('outcomes.jsonl', outcomes)):
        with (directory / name).open('xb') as f:
            f.write(content)
    record = {'pair_sha256': pair, 'input_source': str(a), 'outcome_source': str(b),
              'input_sha256': ih, 'outcome_sha256': oh,
              'source_time_status': 'unverified', 'diagnostic_eligible': False}
    with (directory / 'selection.json').open('x') as f:
        json.dump(record, f, sort_keys=True, indent=2)
    return {'status': status, 'directory': str(directory), **record}


def complete(args):
    directory = Path(args.directory).resolve(strict=True)
    record = read_json(directory / 'selection.json')
    if not isinstance(record, dict):
        raise ValueError('corrupt selection')
    ih, oh, pair = (record.get(k) for k in ('input_sha256', 'outcome_sha256', 'pair_sha256'))
    if not all(isinstance(x, str) and len(x) == 64 for x in (ih, oh, pair)) or digest((ih + ':' + oh).encode()) != pair or directory.name != pair:
        raise ValueError('selection identity mismatch')
    validate_selection(directory, pair, ih, oh, args.max_input_bytes, args.max_outcome_bytes)
    artifacts = {name: digest(read_bounded(directory / name, 1024 * 1024)[1]) for name in ('baseline.json', 'candidate.json', 'comparison.json')}
    completion = {'schema': 'daily-research-completion-v1', 'pair_sha256': pair, 'artifacts': artifacts}
    # Validate exactly the bytes that would be published, without replacing evidence.
    temporary = directory / 'complete.pending.json'
    with temporary.open('x') as f:
        json.dump(completion, f, sort_keys=True, indent=2)
    # Validate through the same reader before publishing; a pending receipt is never complete.
    try:
        validate_completion(directory, pair, temporary)
        os.link(temporary, directory / 'complete.json')
    finally:
        if temporary.exists():
            temporary.unlink()
    return {'status': 'complete', 'directory': str(directory), 'pair_sha256': pair}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('select')
    s.add_argument('--catalog', required=True)
    s.add_argument('--state', required=True)
    s.add_argument('--max-input-bytes', type=int, required=True)
    s.add_argument('--max-outcome-bytes', type=int, required=True)
    s.add_argument('--max-catalog-bytes', type=int, default=1024 * 1024)
    s.add_argument('--max-total-bytes', type=int, default=512 * 1024 * 1024)
    c = sub.add_parser('complete')
    c.add_argument('--directory', required=True)
    c.add_argument('--max-input-bytes', type=int, required=True)
    c.add_argument('--max-outcome-bytes', type=int, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(select(args) if args.command == 'select' else complete(args), sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'daily_research error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())