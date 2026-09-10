"""Byte-bounded reverse hydration; all archives are temporary fixtures."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot.collector_replay_index as owner


class ReverseReadWindowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.raw = self.root / 'raw.jsonl'
        self.index = self.root / 'index.jsonl'
        self.manifest = self.root / 'manifest.json'

    @staticmethod
    def row(number, padding=0):
        return {'run_id': f'run-{number}', 'market_id': f'M{number}',
                'observed_at': '2026-07-01T00:00:00Z', 'padding': 'x' * padding}

    @staticmethod
    def encode(row):
        return (json.dumps(row, sort_keys=True) + '\n').encode()

    def build(self, rows):
        self.raw.write_bytes(b''.join(self.encode(row) for row in rows))
        owner.build_collector_replay_index(self.raw, self.index, self.manifest)

    def read_spy(self, calls):
        original_open = Path.open
        raw = self.raw

        class Reader:
            def __init__(self, handle):
                self.handle = handle

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()

            def read(self, size=-1):
                calls.append((self.handle.tell(), size))
                return self.handle.read(size)

        def open_path(path, mode='r', *args, **kwargs):
            handle = original_open(path, mode, *args, **kwargs)
            return Reader(handle) if path == raw and mode == 'rb' else handle

        return patch.object(Path, 'open', open_path)

    def test_contiguous_rows_share_read_without_losing_order_or_payload_hashes(self):
        rows = [self.row(number) for number in range(8)]
        self.build(rows)
        calls, hashed = [], []
        sha256 = hashlib.sha256

        def digest(payload=b''):
            hashed.append(payload)
            return sha256(payload)

        with self.read_spy(calls), patch.object(owner.hashlib, 'sha256', digest):
            actual = list(owner.load_indexed_collector_rows_reverse(self.index, self.manifest))
        self.assertEqual(actual, rows[::-1])
        for row in rows:
            self.assertIn(self.encode(row), hashed)
        self.assertEqual(calls, [(0, self.raw.stat().st_size)])

    def test_windows_gaps_oversized_rows_and_limits_match_forward_oracle(self):
        # At 256 bytes this exercises within-window rows, records straddling a
        # window edge, a record exactly at the cap, and isolated oversized rows.
        cap = 256
        exact = self.row(3, cap - len(self.encode(self.row(3))))
        rows = [self.row(0), self.row(1), self.row(2, 120), exact,
                self.row(4, 800), self.row(5), self.row(6), self.row(7, 700), self.row(8)]
        chunks = [self.encode(row) for row in rows]
        chunks.insert(2, b'not-json\n' + b' ' * 900 + b'\n')
        self.raw.write_bytes(b''.join(chunks) + b'{')
        owner.build_collector_replay_index(self.raw, self.index, self.manifest)
        expected = list(owner.load_indexed_collector_rows(self.index, self.manifest))[::-1]
        self.assertEqual(expected, rows[::-1])
        extent = json.loads(self.manifest.read_text())['indexed_source_bytes']
        oversized = {len(self.encode(row)) for row in rows if len(self.encode(row)) > cap}
        for limit in (None, 1, 2, 3, 5, 9, 20):
            with self.subTest(limit=limit):
                calls = []
                with patch.object(owner, '_REVERSE_RAW_READ_BYTES', cap), self.read_spy(calls):
                    actual = list(owner.load_indexed_collector_rows_reverse(
                        self.index, self.manifest, max_rows=limit))
                self.assertEqual(actual, expected[:limit])
                self.assertEqual(hashlib.sha256(b''.join(map(self.encode, actual))).hexdigest(),
                                 hashlib.sha256(b''.join(map(self.encode, expected[:limit]))).hexdigest())
                self.assertTrue(all(0 < size <= cap or size in oversized for _, size in calls))
                self.assertTrue(all(0 <= offset < offset + size <= extent for offset, size in calls))

    def rewrite_entries(self, entries):
        self.index.write_bytes(b''.join(self.encode(entry) for entry in entries))
        manifest = json.loads(self.manifest.read_text())
        manifest['committed_index_bytes'] = self.index.stat().st_size
        manifest['index_sha256'] = hashlib.sha256(self.index.read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps(manifest))

    def test_full_index_validation_precedes_even_first_bounded_raw_read(self):
        rows = [self.row(number) for number in range(4)]
        self.build(rows)
        original = [json.loads(line) for line in self.index.read_bytes().splitlines()]
        for defect in ('order', 'extent', 'schema', 'digest', 'incomplete'):
            with self.subTest(defect=defect):
                entries = [dict(entry) for entry in original]
                if defect == 'order':
                    entries[1]['row_number'] = entries[0]['row_number']
                elif defect == 'extent':
                    entries[0]['byte_length'] = self.raw.stat().st_size + 1
                elif defect == 'schema':
                    entries[0]['schema_name'] = 'wrong'
                self.rewrite_entries(entries)
                if defect == 'digest':
                    self.index.write_bytes(self.index.read_bytes().replace(b'M0', b'X0'))
                elif defect == 'incomplete':
                    self.index.write_bytes(self.index.read_bytes()[:-1])
                    manifest = json.loads(self.manifest.read_text())
                    manifest['committed_index_bytes'] -= 1
                    manifest['index_sha256'] = hashlib.sha256(self.index.read_bytes()).hexdigest()
                    self.manifest.write_text(json.dumps(manifest))
                calls = []
                with self.read_spy(calls), self.assertRaises(ValueError):
                    next(owner.load_indexed_collector_rows_reverse(self.index, self.manifest, max_rows=1))
                self.assertEqual(calls, [])

    def test_each_cached_payload_and_identity_fails_closed_at_its_row(self):
        rows = [self.row(number) for number in range(4)]
        self.build(rows)
        original_raw = self.raw.read_bytes()
        original_entries = [json.loads(line) for line in self.index.read_bytes().splitlines()]
        for defect in ('payload', 'identity', 'json'):
            for position in range(len(rows)):
                with self.subTest(defect=defect, position=position):
                    entries = [dict(entry) for entry in original_entries]
                    self.raw.write_bytes(original_raw)
                    if defect == 'identity':
                        entries[position]['shared_candidate_id'] = 'wrong'
                    else:
                        payload = self.encode(rows[position])
                        changed = payload.replace(f'M{position}'.encode(), f'X{position}'.encode())
                        if defect == 'json':
                            changed = b'!' + payload[1:]
                            entries[position]['payload_sha256'] = hashlib.sha256(changed).hexdigest()
                        self.raw.write_bytes(original_raw.replace(payload, changed))
                    self.rewrite_entries(entries)
                    iterator = owner.load_indexed_collector_rows_reverse(self.index, self.manifest)
                    for expected in reversed(rows[position + 1:]):
                        self.assertEqual(next(iterator), expected)
                    with self.assertRaises(ValueError):
                        next(iterator)
                    if position < len(rows) - 1:
                        self.assertEqual(list(owner.load_indexed_collector_rows_reverse(
                            self.index, self.manifest, max_rows=1)), [rows[-1]])

    def test_frozen_checkpoint_and_append_during_iteration_ignore_suffix(self):
        rows = [self.row(number) for number in range(3)]
        self.build(rows)
        frozen = self.root / 'frozen.json'
        frozen.write_bytes(self.manifest.read_bytes())
        iterator = owner.load_indexed_collector_rows_reverse(self.index, frozen)
        self.assertEqual(next(iterator), rows[-1])
        with self.raw.open('ab') as raw:
            raw.write(self.encode(self.row(9)))
        owner.update_collector_replay_index(self.raw, self.index, self.manifest)
        with self.index.open('ab') as index:
            index.write(b'uncommitted garbage')
        self.assertEqual(list(iterator), rows[-2::-1])
        self.assertEqual(list(owner.load_indexed_collector_rows_reverse(self.index, frozen)), rows[::-1])

    def test_empty_checkpoint_and_nonpositive_limits(self):
        self.build([])
        self.assertEqual(list(owner.load_indexed_collector_rows_reverse(self.index, self.manifest)), [])
        for limit in (0, -1):
            with self.assertRaisesRegex(ValueError, 'max_rows must be positive'):
                next(owner.load_indexed_collector_rows_reverse(self.index, self.manifest, max_rows=limit))
