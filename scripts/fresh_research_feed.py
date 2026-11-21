#!/usr/bin/env python3
"""Make bounded, outcome-free research inputs from a committed collector index.

Offline only. The previous receipt is a caller-trusted checkpoint, not publisher
identity or evidence that observations are independent. No scheduler is installed.
"""
import argparse
import hashlib
import json
import os
import resource
import signal
from pathlib import Path
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.collector_replay_index import (
    _index_entry, _iter_committed_index_lines_reverse, _validate_locator,
    _validate_manifest, _verify_archive, _verify_index_extent, _verify_index_path,
)
from bot.replay_decision_input import build_replay_decision_input_v1


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def prefix_digest(stream, size):
    stream.seek(0)
    h = hashlib.sha256()
    left = size
    while left:
        chunk = stream.read(min(left, 1024 * 1024))
        if not chunk:
            raise ValueError('prior committed index prefix truncated')
        h.update(chunk)
        left -= len(chunk)
    return h.hexdigest()


class ReadCounter:
    def __init__(self, stream):
        self.stream = stream
        self.read_bytes = 0

    def seek(self, *args):
        return self.stream.seek(*args)

    def read(self, *args):
        data = self.stream.read(*args)
        self.read_bytes += len(data)
        return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--protected-root', type=Path, action='append', required=True)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--max-index-bytes', type=int, required=True)
    parser.add_argument('--max-inspected-rows', type=int, required=True)
    parser.add_argument('--max-raw-row-bytes', type=int, required=True)
    parser.add_argument('--max-wall-seconds', type=int, required=True)
    parser.add_argument('--max-memory-bytes', type=int, required=True)
    args = parser.parse_args()
    if min(args.max_index_bytes, args.max_inspected_rows, args.max_raw_row_bytes, args.max_wall_seconds, args.max_memory_bytes) <= 0:
        raise ValueError('all limits must be positive')
    hard = resource.getrlimit(resource.RLIMIT_AS)[1]
    if hard != resource.RLIM_INFINITY and args.max_memory_bytes > hard:
        raise ValueError('memory limit exceeds process hard limit')
    resource.setrlimit(resource.RLIMIT_AS, (args.max_memory_bytes, hard))
    def deadline(_signum, _frame):
        raise TimeoutError('wall time limit exceeded')
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(args.max_wall_seconds)
    output = args.output_root.resolve()
    protected = [p.resolve() for p in args.protected_root]
    if any(output == p or output in p.parents or p in output.parents for p in protected):
        raise ValueError('output root overlaps a protected root')
    with args.manifest.open('rb') as manifest_file:
        manifest_bytes = manifest_file.read(1024 * 1024 + 1)
    if len(manifest_bytes) > 1024 * 1024:
        raise ValueError('manifest exceeds one MiB')
    manifest_hash = digest(manifest_bytes)
    manifest = json.loads(manifest_bytes)
    _validate_manifest(manifest)
    if manifest['schema_version'] != 2:
        raise ValueError('legacy replay index does not support reverse committed reads')
    extent = manifest['committed_index_bytes']
    if extent > args.max_index_bytes:
        raise ValueError('index extent exceeds configured maximum before scan')
    index_path = args.index.resolve()
    _verify_index_path(index_path, manifest)
    source_path = Path(str(manifest.get('source_path') or '')).resolve()
    if not all(any(p == x or p in x.parents for p in protected) for x in (source_path, index_path, args.manifest.resolve())):
        raise ValueError('all source files must be within a protected root')
    previous_bytes = args.previous.read_bytes() if args.previous is not None else None
    previous = json.loads(previous_bytes) if previous_bytes is not None else None
    if args.previous is not None:
        if not isinstance(previous, dict) or previous.get('schema_name') != 'fresh_research_feed' or previous.get('checkpoint_eligible') is not True:
            raise ValueError('previous checkpoint is not a complete or eligible bootstrap feed receipt')
        if previous.get('index_path') != str(index_path) or previous.get('archive_identity') != manifest.get('archive_identity'):
            raise ValueError('new_generation: index or archive identity changed')
        old_extent = previous['committed_index_bytes']
        if type(old_extent) is not int or old_extent < 0 or old_extent > extent:
            raise ValueError('new_generation: prior index extent truncated')
    else:
        old_extent = 0
    rows, provenance, rejected = [], [], {}
    raw_bytes = reverse_bytes = inspected = 0
    exhausted = True
    with source_path.open('rb') as source, index_path.open('rb') as index:
        _verify_archive(source, manifest)
        _verify_index_extent(index, manifest)
        prior_bytes = 0
        if previous:
            prior_bytes = old_extent
            if prefix_digest(index, old_extent) != previous['index_sha256']:
                raise ValueError('new_generation: prior committed prefix changed')
        if previous and previous['manifest_sha256'] == manifest_hash:
            print(json.dumps({'status': 'unchanged', 'manifest_sha256': manifest_hash,
                              'whole_index_digest_bytes': extent, 'whole_index_structure_bytes': extent,
                              'prior_prefix_digest_bytes': prior_bytes}, sort_keys=True))
            return
        counter = ReadCounter(index)
        iterator = iter(_iter_committed_index_lines_reverse(counter, extent))
        position = extent
        while position > old_extent and inspected < args.max_inspected_rows:
            try:
                line = next(iterator)
            except StopIteration:
                raise ValueError('committed index ended before checkpoint boundary')
            position -= len(line)
            if position < old_extent:
                raise ValueError('prior index extent is not a row boundary')
            entry = json.loads(line)
            _validate_locator(entry, manifest)
            inspected += 1
            length, offset = entry['byte_length'], entry['byte_offset']
            if length > args.max_raw_row_bytes:
                raise ValueError('raw row exceeds configured maximum')
            source.seek(offset)
            payload = source.read(length)
            raw_bytes += len(payload)
            if len(payload) != length or digest(payload) != entry.get('payload_sha256'):
                raise ValueError('raw collector payload differs from replay index')
            row = json.loads(payload)
            expected = _index_entry(row, row_number=entry['row_number'], byte_offset=offset, payload=payload)
            if expected is None or any(expected[k] != entry.get(k) for k in ('market_id', 'shared_candidate_id', 'shared_snapshot_id', 'observed_at')):
                raise ValueError('raw collector row does not match replay index row identity')
            projected = build_replay_decision_input_v1(row)
            item = {'row_number': entry['row_number'], 'byte_offset': offset, 'byte_length': length,
                    'raw_payload_sha256': entry['payload_sha256']}
            if projected.ok:
                record_bytes = encoded(projected.record)
                item['projected_sha256'] = digest(record_bytes)
                rows.append(record_bytes)
            else:
                for error in projected.errors:
                    key = error.code + ':' + error.path
                    rejected[key] = rejected.get(key, 0) + 1
            provenance.append(item)
        exhausted = position == old_extent
        reverse_bytes = counter.read_bytes
    rows.reverse()
    provenance.reverse()
    status = ('truncated' if not exhausted else
              'initial' if not previous else
              'new_indexed_data' if inspected else 'manifest_changed_no_new_indexed_data')
    inputs = b''.join(rows)
    key = digest(encoded({'manifest': manifest_hash, 'previous': digest(previous_bytes) if previous_bytes else None,
                          'max_inspected_rows': args.max_inspected_rows, 'max_raw_row_bytes': args.max_raw_row_bytes,
                          'max_index_bytes': args.max_index_bytes, 'max_wall_seconds': args.max_wall_seconds,
                          'max_memory_bytes': args.max_memory_bytes}))
    directory = output / key
    receipt = {'schema_name': 'fresh_research_feed', 'schema_version': 1, 'status': status,
               'complete': exhausted, 'checkpoint_eligible': exhausted or previous is None,
               'manifest_sha256': manifest_hash, 'index_sha256': manifest['index_sha256'],
               'index_path': str(index_path), 'source_path': str(source_path), 'archive_identity': manifest['archive_identity'],
               'committed_index_bytes': extent, 'indexed_source_bytes': manifest['indexed_source_bytes'],
               'whole_index_digest_bytes': extent, 'whole_index_structure_bytes': extent,
               'prior_prefix_digest_bytes': prior_bytes, 'reverse_index_read_bytes': reverse_bytes,
               'raw_payload_read_bytes': raw_bytes, 'inspected_rows': inspected, 'accepted_rows': len(rows),
               'rejections': rejected, 'row_provenance': provenance,
               'inputs_sha256': digest(inputs), 'outcomes_sha256': digest(b''),
               'limits': {'index_bytes': args.max_index_bytes, 'inspected_rows': args.max_inspected_rows,
                          'raw_row_bytes': args.max_raw_row_bytes, 'wall_seconds': args.max_wall_seconds,
                          'address_space_bytes': args.max_memory_bytes},
               'previous_receipt_sha256': digest(previous_bytes) if previous_bytes else None,
               'integrity_scope': 'whole committed index; selected raw payloads only',
               'qualification': 'research only; no independent events or outcomes asserted'}
    output.mkdir(parents=True, exist_ok=True)
    if directory.exists():
        old = json.loads((directory / 'receipt.json').read_bytes())
        if old != receipt or digest((directory / 'manifest.json').read_bytes()) != manifest_hash or digest((directory / 'inputs.jsonl').read_bytes()) != receipt['inputs_sha256'] or digest((directory / 'outcomes.jsonl').read_bytes()) != receipt['outcomes_sha256']:
            raise ValueError('existing feed generation differs from expected completion')
    else:
        stage = Path(tempfile.mkdtemp(prefix='.fresh-', dir=output))
        try:
            for name, data in [('inputs.jsonl', inputs), ('outcomes.jsonl', b''), ('manifest.json', manifest_bytes), ('receipt.json', encoded(receipt))]:
                with (stage / name).open('xb') as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
            fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(stage, directory)
            fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    print(json.dumps({'status': status, 'directory': str(directory)}, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, MemoryError) as exc:
        print('fresh_research_feed failure: ' + str(exc), file=sys.stderr)
        sys.exit(1)
