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


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--protected-root', type=Path, action='append', required=True)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--resume', type=Path, help='incomplete page receipt from this frozen delta')
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
    producer_manifest = json.loads(manifest_bytes)
    _validate_manifest(producer_manifest)
    resume = None
    resume_bytes = None
    if args.resume is not None:
        resume_path = args.resume.resolve()
        if args.previous is None or resume_path.name != 'receipt.json' or resume_path.parent.parent != output:
            raise ValueError('resume requires previous checkpoint and a page under output root')
        resume_bytes = resume_path.read_bytes()
        if resume_path.parent.name != digest(resume_bytes):
            raise ValueError('resume receipt bytes changed')
        resume = json.loads(resume_bytes)
        if not isinstance(resume, dict) or resume.get('schema_name') != 'fresh_research_feed' or resume.get('complete') is not False:
            raise ValueError('resume receipt is not an incomplete feed page')
        page = resume_path.parent
        manifest_bytes = (page / 'manifest.json').read_bytes()
        if len(manifest_bytes) > 1024 * 1024 or digest(manifest_bytes) != resume.get('manifest_sha256'):
            raise ValueError('resume pinned manifest missing or changed')
        for name, key in (('inputs.jsonl', 'inputs_sha256'), ('outcomes.jsonl', 'outcomes_sha256')):
            if digest((page / name).read_bytes()) != resume.get(key):
                raise ValueError('resume page snapshot changed')
    manifest_hash = digest(manifest_bytes)
    manifest = json.loads(manifest_bytes)
    _validate_manifest(manifest)
    if resume and (producer_manifest.get('archive_identity') != manifest.get('archive_identity') or
                   producer_manifest.get('source_path') != manifest.get('source_path') or
                   producer_manifest.get('committed_index_bytes', -1) < manifest.get('committed_index_bytes', 0) or
                   (producer_manifest.get('committed_index_bytes') == manifest.get('committed_index_bytes') and
                    producer_manifest.get('index_sha256') != manifest.get('index_sha256'))):
        raise ValueError('new_generation: producer snapshot changed or truncated')
    if manifest['schema_version'] != 2:
        raise ValueError('legacy replay index does not support reverse committed reads')
    extent = manifest['committed_index_bytes']
    if extent > args.max_index_bytes:
        raise ValueError('index extent exceeds configured maximum before scan')
    index_path = args.index.resolve()
    _verify_index_path(index_path, manifest)
    if resume:
        _verify_index_path(index_path, producer_manifest)
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
    cursor = 0
    if resume:
        if resume.get('previous_receipt_sha256') != digest(previous_bytes):
            raise ValueError('resume previous checkpoint changed')
        if resume.get('index_path') != str(index_path) or resume.get('archive_identity') != manifest.get('archive_identity'):
            raise ValueError('resume snapshot identity changed')
        start = resume.get('page_start_index_bytes')
        cursor = resume.get('next_index_bytes')
        if type(start) is not int or type(cursor) is not int or not old_extent <= cursor < start <= extent:
            raise ValueError('invalid resume cursor')
    else:
        start = extent
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
        if previous and not resume and previous['manifest_sha256'] == manifest_hash:
            print(json.dumps({'status': 'unchanged', 'manifest_sha256': manifest_hash,
                              'whole_index_digest_bytes': extent, 'whole_index_structure_bytes': extent,
                              'prior_prefix_digest_bytes': prior_bytes}, sort_keys=True))
            return
        counter = ReadCounter(index)
        if resume:
            checked = start
            entries = resume.get('row_provenance')
            if not isinstance(entries, list) or len(entries) != resume.get('inspected_rows') or not entries:
                raise ValueError('invalid resume page provenance')
            check = iter(_iter_committed_index_lines_reverse(counter, start))
            for item in reversed(entries):
                line = next(check, None)
                if line is None:
                    raise ValueError('resume cursor outside pinned index')
                checked -= len(line)
                entry = json.loads(line)
                if any(entry.get(k) != item.get(k) for k in ('row_number', 'byte_offset', 'byte_length')) or entry.get('payload_sha256') != item.get('raw_payload_sha256'):
                    raise ValueError('resume page provenance differs from pinned index')
            if checked != cursor:
                raise ValueError('resume cursor does not match page provenance')
            start = cursor
        iterator = iter(_iter_committed_index_lines_reverse(counter, start))
        position = start
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
    receipt = {'schema_name': 'fresh_research_feed', 'schema_version': 1, 'status': status,
               'complete': exhausted, 'checkpoint_eligible': exhausted or previous is None,
               'manifest_sha256': manifest_hash, 'index_sha256': manifest['index_sha256'],
               'index_path': str(index_path), 'source_path': str(source_path), 'archive_identity': manifest['archive_identity'],
               'committed_index_bytes': extent, 'indexed_source_bytes': manifest['indexed_source_bytes'],
               'page_start_index_bytes': start, 'next_index_bytes': position,
               'resume_receipt_sha256': digest(resume_bytes) if resume_bytes else None,
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
    directory = output / digest(encoded(receipt))
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
            sync_directory(stage)
            os.rename(stage, directory)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    # Also sync on reuse: a prior rename (or newly created output-root ancestor)
    # may have succeeded immediately before a failed directory fsync.
    for parent in (output, *output.parents):
        sync_directory(parent)
    print(json.dumps({'status': status, 'directory': str(directory)}, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, MemoryError) as exc:
        print('fresh_research_feed failure: ' + str(exc), file=sys.stderr)
        sys.exit(1)
