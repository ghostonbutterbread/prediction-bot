#!/usr/bin/env python3
"""Choose a new bounded frozen input pair for a human/cron research agent."""
import argparse
import hashlib
import json
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


def select(args):
    if args.max_input_bytes < 1 or args.max_outcome_bytes < 1:
        raise ValueError('byte limits must be positive')
    catalog = Path(args.catalog).resolve(strict=True)
    entries = json.loads(catalog.read_text())
    if not isinstance(entries, list):
        raise ValueError('catalog must be a list')
    state = Path(args.state).resolve()
    # Validate every entry before choosing; an unavailable newer dataset is an
    # error, not an excuse to silently claim there is no new data.
    candidates = []
    for entry in entries:
        a, inputs = read_bounded(entry['inputs'], args.max_input_bytes)
        b, outcomes = read_bounded(entry['outcomes'], args.max_outcome_bytes)
        if a == b or not inputs.strip():
            raise ValueError('distinct files and nonempty inputs required')
        if any(p == state or p.is_relative_to(state) for p in (a, b, catalog)):
            raise ValueError('state must not contain catalog or source files')
        ih, oh = digest(inputs), digest(outcomes)
        pair = digest((ih + ':' + oh).encode())
        candidates.append((pair, a, b, inputs, outcomes, ih, oh))
    for pair, a, b, inputs, outcomes, ih, oh in candidates:
        directory = state / pair
        if (directory / 'complete.json').is_file():
            continue
        if directory.exists():
            return {'status': 'in_progress', 'directory': str(directory), 'pair_sha256': pair}
        directory.mkdir(parents=True)
        for name, content in (('inputs.jsonl', inputs), ('outcomes.jsonl', outcomes)):
            with (directory / name).open('xb') as f:
                f.write(content)
        record = {'pair_sha256': pair, 'input_source': str(a), 'outcome_source': str(b),
                  'input_sha256': ih, 'outcome_sha256': oh,
                  'source_time_status': 'unverified', 'diagnostic_eligible': False}
        with (directory / 'selection.json').open('x') as f:
            json.dump(record, f, sort_keys=True, indent=2)
        return {'status': 'selected', 'directory': str(directory), **record}
    return {'status': 'no_new_data'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('select')
    s.add_argument('--catalog', required=True)
    s.add_argument('--state', required=True)
    s.add_argument('--max-input-bytes', type=int, required=True)
    s.add_argument('--max-outcome-bytes', type=int, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(select(args), sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'daily_research error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
