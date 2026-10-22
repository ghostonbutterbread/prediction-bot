#!/usr/bin/env python3
"""Read-only streaming weather attribution audit, derived reports only.

No archive-apply/rewrite mode exists. Exit 0 means complete, 2 a bounded chunk,
3 unsupported-by-reader legacy evidence, and 1 an integrity/usage failure.
This audits location and existing source proofs, not independent outcomes or
router sample floors. It never claims that every observed forecast is history.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Support direct invocation without requiring an editable installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.collector_replay_index import (
    load_indexed_collector_rows, _validate_manifest, _verify_archive,
    _verify_index_extent, _verify_index_path,
)
from bot.replay_decision_input import build_replay_decision_input_v1
from bot.weather.source_observation_ledger import _decision_identity, _pending_rows_for_input


def _stat(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _index_digest(path, extent):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        remaining = extent
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError('index truncated during receipt read')
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _scan_index(path, extent, *, start_entry, max_rows, manifest, max_record_bytes):
    """Scan compact receipts only; retain at most one bounded chunk of IDs."""
    selected = set() if max_rows else None
    total = selected_count = last_entry = missing_hash = previous_number = previous_end = transitions = 0
    source_extent = manifest.get('indexed_source_bytes', manifest.get('source_size_bytes'))
    with path.open('rb') as stream:
        while stream.tell() < extent:
            line = stream.readline(min(1024 * 1024, extent - stream.tell()))
            if not line or not line.endswith(b'\n'):
                raise ValueError('index row is oversized or unterminated')
            entry = json.loads(line)
            offset, length, number = (entry.get(key) for key in ('byte_offset', 'byte_length', 'row_number'))
            if (any(type(value) is not int for value in (offset, length, number))
                    or offset < 0 or length <= 0 or number <= 0
                    or (type(source_extent) is int and offset + length > source_extent)):
                raise ValueError('invalid or unordered index locator')
            if number <= previous_number or offset < previous_end:
                if manifest['schema_version'] == 2:
                    raise ValueError('invalid committed index locator order')
                transitions += 1
            previous_number, previous_end = number, offset + length
            if not re.fullmatch('[a-fA-F0-9]{64}', str(entry.get('payload_sha256') or '')):
                if manifest['schema_version'] == 2:
                    raise ValueError('committed row lacks a valid payload SHA256')
                missing_hash += 1
            total += 1
            if total >= start_entry and (not max_rows or selected_count < max_rows):
                if length > max_record_bytes:
                    raise ValueError('selected record exceeds max_record_bytes resource guard')
                selected_count += 1
                last_entry = total
                if selected is not None:
                    selected.add(entry['row_number'])
    return selected, total, selected_count, last_entry, missing_hash, transitions


def _selected_locators(path, extent, selected):
    with path.open('rb') as stream:
        while stream.tell() < extent:
            line = stream.readline(min(1024 * 1024, extent - stream.tell()))
            if not line or not line.endswith(b'\n'):
                raise ValueError('index row changed or became oversized')
            entry = json.loads(line)
            if selected is None or entry['row_number'] in selected:
                yield entry


def _index_unchanged(path, before, extent, schema):
    after = _stat(path)
    # A frozen V2 receipt permits ordinary collector appends beyond its prefix.
    return ((after[:2] == before[:2] and after[2] >= extent) if schema == 2 else after == before)


def audit(*, index: Path, manifest: Path, output_dir: Path, sample_limit=3,
          max_rows=10000, start_entry=1, max_record_bytes=16 * 1024 * 1024):
    index, manifest, output_dir = index.resolve(), manifest.resolve(), output_dir.resolve()
    manifest_bytes = manifest.read_bytes()
    payload = json.loads(manifest_bytes)
    _validate_manifest(payload)
    source = Path(payload['source_path']).resolve()
    if 'derived_reports' not in output_dir.parts or any(
            output_dir.is_relative_to(path.parent) for path in (index, manifest, source)):
        raise ValueError('output must be in derived_reports outside every input/evidence directory')
    if output_dir.exists():
        raise ValueError('output directory must be new; refusing to overwrite artifacts')
    if sample_limit < 0:
        raise ValueError('sample_limit must be nonnegative')
    if max_rows < 0 or start_entry < 1 or (max_rows == 0 and start_entry != 1):
        raise ValueError('max_rows must be nonnegative; full scans start at entry 1')
    if max_record_bytes <= 0:
        raise ValueError('max_record_bytes must be positive')
    before = _stat(index)
    extent = payload.get('committed_index_bytes', before[2])
    index_sha256 = _index_digest(index, extent)
    if payload['schema_version'] == 2 and index_sha256 != payload['index_sha256']:
        raise ValueError('committed replay index digest mismatch')
    selected, total, selected_count, last_entry, missing_hash, transitions = _scan_index(
        index, extent, start_entry=start_entry, max_rows=max_rows, manifest=payload,
        max_record_bytes=max_record_bytes)
    if payload['schema_version'] == 2 and total != payload['indexed_rows']:
        raise ValueError('committed indexed_rows does not match actual prefix entries')
    if not _index_unchanged(index, before, extent, payload['schema_version']):
        raise ValueError('index changed during preflight')
    if not selected_count:
        # Empty/out-of-range chunks still attest the committed input contract.
        if payload['schema_version'] == 2:
            _verify_index_path(index, payload)
            with source.open('rb') as raw_stream, index.open('rb') as index_stream:
                _verify_archive(raw_stream, payload)
                _verify_index_extent(index_stream, payload)
        elif not source.is_file():
            raise FileNotFoundError(source)
    unsupported_order = bool(transitions and max_rows)
    output_dir.mkdir(parents=True)
    receipt_path = output_dir / 'input_manifest.receipt.json'
    receipt_path.write_bytes(manifest_bytes)
    counts = Counter(raw_rows_reconciled=0, source_observations=0, rejected_replay_inputs=0,
                     strict_proof_eligible=0, predictive_proof_eligible=0, derived_market_label_repaired=0,
                     compact_entries_seen=total, unsupported_missing_payload_sha256=missing_hash,
                     legacy_nonmonotonic_transitions=transitions)
    reasons, samples = Counter(), defaultdict(list)
    # Older whole-archive proof may exist, but this canonical reader requires
    # per-row hashes. Do not invent them, hash the raw archive, or claim repair.
    rows = load_indexed_collector_rows(index, receipt_path, row_numbers=selected) if selected_count and not missing_hash and not unsupported_order else ()
    locators = _selected_locators(index, extent, selected) if selected_count and not missing_hash and not unsupported_order else ()
    for raw, locator in zip(rows, locators, strict=True):
        counts['raw_rows_reconciled'] += 1
        built = build_replay_decision_input_v1(raw, source_history_only=True)
        if not built.ok or built.record is None:
            counts['rejected_replay_inputs'] += 1
            reasons.update(error.code for error in built.errors)
            continue
        identity = _decision_identity(built.record)
        if identity is None:
            raise ValueError('replay input failed its identity contract')
        pending = _pending_rows_for_input(built.record, identity)
        if not pending:
            counts['rows_without_source_observations'] += 1
        for observation in pending:
            counts['source_observations'] += 1
            reconciliation = observation['weather_identity_reconciliation']
            status = reconciliation['status']
            counts['identity_' + status] += 1
            counts['derived_market_label_repaired'] += int(reconciliation['market_city_label_repaired'])
            counts['strict_proof_eligible'] += int(observation['strict_source_proof']['status'] == 'eligible')
            counts['predictive_proof_eligible'] += int(observation['predictive_source_proof']['status'] == 'eligible_predictive_source_proof')
            reasons.update(observation['strict_source_proof']['reasons'])
            classification = status + ('_market_label_repaired' if reconciliation['market_city_label_repaired'] else '')
            if len(samples[classification]) < sample_limit:
                samples[classification].append({
                    'market_id': observation['market_id'], 'source_id': observation['source_id'],
                    'raw_row_sha256': identity['decision_key']['raw_row_sha256'],
                    'raw_locator': {key: locator[key] for key in ('row_number', 'byte_offset', 'byte_length', 'payload_sha256')},
                    'city_id': observation['city_id'], 'source_provenance': observation['source_provenance'],
                    'identity_status': status, 'strict_reasons': observation['strict_source_proof']['reasons'],
                })
    if (not _index_unchanged(index, before, extent, payload['schema_version'])
            or _index_digest(index, extent) != index_sha256):
        raise ValueError('index changed during audit')
    if receipt_path.read_bytes() != manifest_bytes:
        raise ValueError('manifest receipt changed during audit')
    report = {
        'schema_name': 'weather_identity_read_time_audit', 'schema_version': 1,
        'mode': 'read_only_derived_report', 'raw_mutated': False,
        'count_unit': 'index-entry/source-record appearances, not unique observations or independent events',
        'qualification_scope': 'location and existing source proofs only; no outcomes/chronological history assertion',
        'repair_count_definition': 'derived_market_label_repaired counts known wrong-city attribution, not case or registry-alias normalization',
        'source_hash_scope': 'source_record_sha256 is the original sealed sanitized source record before label projection; raw_locator.payload_sha256 binds the original raw bytes',
        'scope': {'complete': start_entry == 1 and counts['raw_rows_reconciled'] == total,
                  'status': ('unsupported_by_canonical_reader' if missing_hash else
                             'unsupported_bounded_legacy_locator_order' if unsupported_order else
                             'complete' if start_entry == 1 and counts['raw_rows_reconciled'] == total else 'bounded'),
                  'compact_scan_complete': True,
                  'requested_first_entry': start_entry, 'requested_last_entry': last_entry or None,
                  'first_entry': start_entry,
                  'last_entry': last_entry if counts['raw_rows_reconciled'] else None,
                  'next_entry': last_entry + 1 if counts['raw_rows_reconciled'] and last_entry < total else None,
                  'max_rows': max_rows, 'max_record_bytes': max_record_bytes},
        'input_receipt': {
            'index_path': str(index), 'manifest_path': str(manifest), 'source_path': str(source),
            'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
            'manifest_schema_version': payload['schema_version'],
            'consumer_compatibility': 'committed_direct_reader' if payload['schema_version'] == 2 else 'legacy_forward_audit_only',
            'declared_source_path': payload['source_path'],
            'declared_source_sha256': payload.get('source_sha256'),
            'whole_source_sha256_verified': False,
            'declared_archive_identity': payload.get('archive_identity'),
            'declared_indexed_rows': payload.get('indexed_rows'),
            'declared_indexed_rows_matches_actual': payload.get('indexed_rows') == total,
            'declared_source_extent': payload.get('indexed_source_bytes', payload.get('source_size_bytes')),
            'index_sha256': index_sha256, 'index_bytes': extent,
            'index_scope': 'committed_v2_prefix' if payload['schema_version'] == 2 else 'hash_pinned_legacy_index_at_audit',
            'raw_hash_scope': ('no raw payloads hydrated; canonical reader requires per-row hashes' if missing_hash else
                               'no raw payloads hydrated; bounded selection requires monotonic legacy locators' if unsupported_order else
                               'selected original payload hashes verified by canonical indexed reader; no full raw traversal'),
        },
        'counts': dict(sorted(counts.items())), 'strict_rejection_counts': dict(sorted(reasons.items())),
        'diagnostic_samples': dict(sorted(samples.items())), 'sample_limit_per_classification': sample_limit,
    }
    (output_dir / 'report.json').write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--sample-limit', default=3, type=int)
    parser.add_argument('--max-rows', default=10000, type=int,
                        help='raw hydration budget; 0 explicitly scans the full archive')
    parser.add_argument('--start-entry', default=1, type=int,
                        help='1-based compact-index entry for a bounded resumable chunk')
    parser.add_argument('--max-record-bytes', default=16 * 1024 * 1024, type=int,
                        help='fail before hydrating a selected oversized raw record')
    args = parser.parse_args()
    try:
        report = audit(index=args.index, manifest=args.manifest, output_dir=args.output_dir,
                       sample_limit=args.sample_limit, max_rows=args.max_rows, start_entry=args.start_entry,
                       max_record_bytes=args.max_record_bytes)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'audit failed: {exc}', file=sys.stderr)
        return 1
    print(json.dumps({'status': report['scope']['status'], 'counts': report['counts'],
                      'report': str(args.output_dir / 'report.json')}, sort_keys=True))
    if report['scope']['status'].startswith('unsupported_'):
        return 3
    return 0 if report['scope']['complete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
