"""Committed-prefix regressions; fixtures never touch a live collector."""
import json
import tempfile
import unittest
from pathlib import Path

from bot.collector_replay_index import (
    build_collector_replay_index, load_indexed_collector_rows,
    update_collector_replay_index,
)


class CollectorIndexCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / "raw.jsonl"
        self.index = self.root / "index.jsonl"
        self.manifest = self.root / "manifest.json"
        self.first = {"run_id": "run-1", "market_id": "ONE", "observed_at": "2026-07-01T12:00:00Z"}
        self.second = {"run_id": "run-2", "market_id": "TWO", "observed_at": "2026-07-02T12:00:00Z"}

    def encoded(self, row):
        return json.dumps(row, sort_keys=True).encode() + b"\n"

    def build(self):
        return build_collector_replay_index(self.raw, self.index, self.manifest)

    def update(self):
        return update_collector_replay_index(self.raw, self.index, self.manifest)

    def rows(self, manifest=None):
        return list(load_indexed_collector_rows(self.index, manifest or self.manifest))

    def test_frozen_manifest_excludes_later_committed_index_and_raw_append(self):
        self.raw.write_bytes(self.encoded(self.first))
        self.build()
        frozen = self.root / "frozen.json"
        frozen.write_bytes(self.manifest.read_bytes())
        with self.raw.open("ab") as handle:
            handle.write(self.encoded(self.second))
        self.update()
        self.assertEqual(self.rows(frozen), [self.first])
        self.assertEqual(self.rows(), [self.first, self.second])

    def test_corrupt_index_fails_before_filtered_hydration(self):
        self.raw.write_bytes(self.encoded(self.first) + self.encoded(self.second))
        self.build()
        self.index.write_bytes(self.index.read_bytes().replace(b'ONE', b'BAD'))
        with self.assertRaisesRegex(ValueError, 'index.*digest'):
            list(load_indexed_collector_rows(self.index, self.manifest, market_ids={'TWO'}))

    def test_interrupted_publication_preserves_checkpoint_and_retry_is_exact(self):
        from unittest.mock import patch
        self.raw.write_bytes(self.encoded(self.first))
        self.build()
        before = self.manifest.read_bytes()
        with self.raw.open('ab') as handle:
            handle.write(self.encoded(self.second))
        with patch('bot.collector_replay_index.atomic_write_json', create=True, side_effect=OSError('publication interrupted')):
            with self.assertRaisesRegex(OSError, 'publication interrupted'):
                self.update()
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(self.rows(), [self.first])
        self.assertEqual(self.update()['new_indexed_rows'], 1)
        self.assertEqual(self.rows(), [self.first, self.second])
        self.assertEqual(len(self.index.read_bytes().splitlines()), 2)
        self.assertEqual(self.update()['new_indexed_rows'], 0)

    def test_initial_publication_failure_can_retry_without_duplicate_rows(self):
        from unittest.mock import patch
        import bot.collector_replay_index as owner
        self.raw.write_bytes(self.encoded(self.first))
        original = getattr(owner, 'atomic_write_json')
        calls = []
        def interrupted(path, value):
            calls.append(value)
            if len(calls) == 2:
                raise OSError('initial publication interrupted')
            original(path, value)
        with patch.object(owner, 'atomic_write_json', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'initial publication interrupted'):
                self.build()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.update()['new_indexed_rows'], 1)
        self.assertEqual(self.rows(), [self.first])

    def test_replaced_raw_archive_fails_even_when_bytes_match(self):
        self.raw.write_bytes(self.encoded(self.first))
        self.build()
        replacement = self.root / 'replacement'
        replacement.write_bytes(self.raw.read_bytes())
        replacement.replace(self.raw)
        with self.assertRaisesRegex(ValueError, 'archive identity'):
            self.rows()
        with self.assertRaisesRegex(ValueError, 'archive identity'):
            self.update()

    def test_locator_candidate_identity_is_verified_not_just_market(self):
        import hashlib
        self.raw.write_bytes(self.encoded(self.first))
        self.build()
        entry = json.loads(self.index.read_text())
        entry['shared_candidate_id'] = 'wrong-candidate'
        self.index.write_text(json.dumps(entry) + '\n')
        manifest = json.loads(self.manifest.read_text())
        manifest['committed_index_bytes'] = self.index.stat().st_size
        manifest['index_sha256'] = hashlib.sha256(self.index.read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'row.*identity'):
            self.rows()

    def test_initial_source_digest_is_digest_of_complete_prefix(self):
        import hashlib
        complete = self.encoded(self.first)
        self.raw.write_bytes(complete + b'{')
        result = self.build()
        self.assertEqual(result['source_sha256'], hashlib.sha256(complete).hexdigest())

    def test_locator_must_stay_inside_committed_raw_extent(self):
        import hashlib
        self.raw.write_bytes(self.encoded(self.first))
        self.build()
        with self.raw.open('ab') as handle:
            handle.write(self.encoded(self.second))
        entry = json.loads(self.index.read_text())
        entry['byte_offset'] = len(self.encoded(self.first))
        entry['byte_length'] = len(self.encoded(self.second))
        entry['payload_sha256'] = hashlib.sha256(self.encoded(self.second)).hexdigest()
        entry['market_id'] = 'TWO'
        entry['shared_snapshot_id'] = 'run-2'
        entry['shared_candidate_id'] = 'run-2:TWO'
        entry['observed_at'] = self.second['observed_at']
        self.index.write_text(json.dumps(entry) + '\n')
        manifest = json.loads(self.manifest.read_text())
        manifest['committed_index_bytes'] = self.index.stat().st_size
        manifest['index_sha256'] = hashlib.sha256(self.index.read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'locator.*extent'):
            self.rows()

    def test_unsupported_manifest_schema_fails_closed(self):
        self.raw.write_bytes(self.encoded(self.first))
        self.build()
        manifest = json.loads(self.manifest.read_text())
        manifest['schema_version'] = 99
        self.manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'schema'):
            self.rows()
        with self.assertRaisesRegex(ValueError, 'schema'):
            self.update()

    def test_update_freezes_starting_size_while_collector_appends(self):
        from unittest.mock import patch
        self.raw.write_bytes(self.encoded(self.first))
        self.build()
        with self.raw.open('ab') as handle:
            handle.write(self.encoded(self.second))
        original_open = Path.open
        third = {**self.first, 'run_id': 'run-3'}
        raw = self.raw
        appended = []
        class AppendingReader:
            def __init__(self, handle):
                self.handle = handle
            def __getattr__(self, key):
                return getattr(self.handle, key)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.handle.close()
            def readline(inner, *args):
                value = inner.handle.readline(*args)
                if not appended:
                    with original_open(raw, 'ab') as writer:
                        writer.write(self.encoded(third))
                    appended.append(True)
                return value
        def open_path(path, mode='r', *args, **kwargs):
            handle = original_open(path, mode, *args, **kwargs)
            return AppendingReader(handle) if path == raw and mode == 'rb' else handle
        with patch.object(Path, 'open', open_path):
            result = self.update()
        self.assertEqual(result['new_indexed_rows'], 1)
        self.assertEqual(self.rows(), [self.first, self.second])
        self.assertEqual(self.update()['new_indexed_rows'], 1)
        self.assertEqual(self.rows(), [self.first, self.second, third])

    def test_missing_truncated_and_tampered_evidence_fail_closed(self):
        self.raw.write_bytes(self.encoded(self.first))
        self.build()
        original = self.raw.read_bytes()
        self.raw.write_bytes(original[:-1])
        with self.assertRaisesRegex(ValueError, 'truncated'):
            self.rows()
        self.raw.write_bytes(original.replace(b'ONE', b'BAD'))
        with self.assertRaisesRegex(ValueError, 'payload differs'):
            self.rows()
        self.raw.unlink()
        with self.assertRaises(FileNotFoundError):
            self.rows()

    def test_index_truncation_and_failed_update_leave_manifest_unchanged(self):
        self.raw.write_bytes(self.encoded(self.first))
        self.build()
        manifest = self.manifest.read_bytes()
        self.index.write_bytes(self.index.read_bytes()[:-1])
        with self.assertRaisesRegex(ValueError, 'index is truncated'):
            self.rows()
        with self.assertRaisesRegex(ValueError, 'index is truncated'):
            self.update()
        self.assertEqual(self.manifest.read_bytes(), manifest)

    def test_legacy_index_is_readable_but_not_silently_upgraded(self):
        self.raw.write_bytes(self.encoded(self.first))
        self.build()
        manifest = json.loads(self.manifest.read_text())
        manifest['schema_version'] = 1
        for field in ('archive_identity', 'committed_index_bytes', 'index_sha256'):
            manifest.pop(field)
        self.manifest.write_text(json.dumps(manifest))
        self.assertEqual(self.rows(), [self.first])
        with self.assertRaisesRegex(ValueError, 'explicit offline checkpoint migration'):
            self.update()

    def test_append_and_full_rebuild_have_identical_index_bytes_and_chain(self):
        self.raw.write_bytes(self.encoded(self.first) + b'not-json\n')
        self.build()
        with self.raw.open('ab') as handle:
            handle.write(self.encoded(self.second) + b'{')
        updated = self.update()
        rebuilt_index = self.root / 'rebuilt.jsonl'
        rebuilt_manifest = self.root / 'rebuilt.json'
        rebuilt = build_collector_replay_index(self.raw, rebuilt_index, rebuilt_manifest)
        self.assertEqual(self.index.read_bytes(), rebuilt_index.read_bytes())
        for field in ('index_sha256', 'append_chain_sha256', 'source_rows_seen', 'invalid_rows', 'indexed_rows', 'indexed_source_bytes'):
            self.assertEqual(updated[field], rebuilt[field], field)
        self.assertEqual(updated['invalid_rows'], 1)
        self.assertEqual(updated['source_rows_seen'], 3)

    def test_unchanged_update_reuses_exact_checkpoint_bytes(self):
        self.raw.write_bytes(self.encoded(self.first))
        self.build()
        before = self.manifest.read_bytes()
        self.assertEqual(self.update()['new_indexed_rows'], 0)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_wrong_index_path_rejected_even_if_content_matches(self):
        self.raw.write_bytes(self.encoded(self.first))
        self.build()
        other = self.root / 'other-index.jsonl'
        other.write_bytes(self.index.read_bytes())
        with self.assertRaisesRegex(ValueError, 'index path'):
            list(load_indexed_collector_rows(other, self.manifest))
        with self.assertRaisesRegex(ValueError, 'index path'):
            update_collector_replay_index(self.raw, other, self.manifest)

    def test_initial_partial_only_file_has_empty_complete_checkpoint(self):
        self.raw.write_bytes(b'{')
        result = self.build()
        self.assertEqual(result['unindexed_trailing_bytes'], 1)
        self.assertEqual(result['indexed_source_bytes'], 0)
        self.assertEqual(self.rows(), [])

    def test_empty_unpublished_initial_index_can_retry_after_process_death(self):
        self.raw.write_bytes(self.encoded(self.first))
        self.index.touch()
        self.assertEqual(self.update()['indexed_rows'], 1)
        self.assertEqual(self.rows(), [self.first])

    def test_initial_build_defers_even_parseable_unterminated_row(self):
        complete = self.encoded(self.first)
        self.raw.write_bytes(complete + self.encoded(self.second)[:-1])
        built = self.build()
        self.assertEqual(built["indexed_source_bytes"], len(complete))
        self.assertEqual(built["source_rows_seen"], 1)
        self.assertEqual(self.rows(), [self.first])
        with self.raw.open("ab") as handle:
            handle.write(b"\n")
        self.assertEqual(self.update()["new_indexed_rows"], 1)
        self.assertEqual(self.rows(), [self.first, self.second])
