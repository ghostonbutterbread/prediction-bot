import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.file_ops import load_jsonl
from bot.resolution_feed import normalize_resolution_feed_config, run_resolution_feed_once, write_incremental_market_refs


class ResolutionFeedDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = self.root / "markets.jsonl"
        self.output = self.root / "feed"

    def write(self, path, ids):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps({"market_id": mid}) + "\n" for mid in ids))

    def discover(self, **kwargs):
        refs = write_incremental_market_refs([self.raw], output_dir=self.output, **kwargs)
        return [row["market_id"] for row in load_jsonl(refs)]

    def checkpoint(self):
        return json.loads((self.output / "resolution_market_refs.discovery.json").read_text())

    def forbid_raw(self):
        original = Path.open

        def guarded(path, *args, **kwargs):
            if path == self.raw:
                raise AssertionError("unchanged raw archive must not be opened")
            return original(path, *args, **kwargs)

        return patch.object(Path, "open", guarded)

    def test_unchanged_refresh_and_restart_never_open_raw_archive(self):
        self.write(self.raw, ["A", "B", "A"])
        self.assertEqual(self.discover(), ["A", "B"])
        with self.forbid_raw():
            self.assertEqual(self.discover(), ["A", "B"])
            self.assertEqual(self.discover(), ["A", "B"])

    def test_append_reads_only_suffix_and_preserves_old_ids(self):
        self.write(self.raw, ["A"])
        self.discover()
        first_offset = self.raw.stat().st_size
        with self.raw.open("a") as handle:
            handle.write(json.dumps({"provenance": {"shared_candidate": {"market_id": "B"}}}) + "\n")
        original = Path.open
        offsets = []

        class Reader:
            def __init__(self, handle):
                self.handle = handle
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.handle.close()
            def __getattr__(self, key):
                return getattr(self.handle, key)
            def readline(self, limit):
                offsets.append(self.handle.tell())
                return self.handle.readline(limit)

        def tracked(path, *args, **kwargs):
            handle = original(path, *args, **kwargs)
            return Reader(handle) if path == self.raw else handle

        with patch.object(Path, "open", tracked):
            self.assertEqual(self.discover(), ["A", "B"])
        self.assertEqual(offsets, [first_offset])

    def test_raw_budget_and_partial_tail_resume_at_complete_lines(self):
        self.write(self.raw, ["A", "B", "C"])
        row_bytes = len(json.dumps({"market_id": "A"}) + "\n")
        self.assertEqual(self.discover(max_read_bytes=row_bytes), ["A"])
        self.assertEqual(self.discover(max_read_bytes=row_bytes), ["A", "B"])
        with self.raw.open("ab") as handle:
            handle.write(b'{"market_id": "D"')
        self.assertEqual(self.discover(), ["A", "B", "C"])
        with self.raw.open("ab") as handle:
            handle.write(b'}\n')
        self.assertEqual(self.discover(), ["A", "B", "C", "D"])

    def test_rotation_and_truncation_reset_cursor_preserve_retry_ids(self):
        self.write(self.raw, ["OLD", "OLDER"])
        self.discover()
        self.raw.rename(self.root / "rotated.jsonl")
        self.write(self.raw, ["NEW"])
        self.assertEqual(self.discover(), ["NEW", "OLD", "OLDER"])
        self.write(self.raw, ["X"])
        self.assertEqual(self.discover(), ["NEW", "OLD", "OLDER", "X"])

    def test_multiple_sources_and_newly_added_source_have_independent_offsets(self):
        self.write(self.raw, ["A"])
        self.discover()
        second = self.root / "second.jsonl"
        self.write(second, ["B"])
        with self.forbid_raw():
            refs = write_incremental_market_refs([self.raw, second], output_dir=self.output)
        self.assertEqual([row["market_id"] for row in load_jsonl(refs)], ["A", "B"])

    def make_index(self, ids, *, invalid_rows=0):
        index = self.root / "index.jsonl"
        self.write(index, ids)
        rows = load_jsonl(index)
        index.write_text("".join(json.dumps({**row, "schema_name": "collector_replay_index", "schema_version": 1}) + "\n" for row in rows))
        stat = self.raw.stat()
        manifest = self.root / "index.manifest.json"
        manifest.write_text(json.dumps({
            "schema_name": "collector_replay_index_manifest", "schema_version": 2,
            "index_schema_name": "collector_replay_index", "index_schema_version": 1,
            "source_path": str(self.raw.resolve()), "index_path": str(index.resolve()),
            "archive_identity": {"device": stat.st_dev, "inode": stat.st_ino},
            "committed_index_bytes": index.stat().st_size,
            "indexed_source_bytes": stat.st_size, "invalid_rows": invalid_rows,
        }))
        return index, manifest

    def test_bootstrap_reads_compact_committed_index_without_raw_scan(self):
        self.write(self.raw, ["A", "B"])
        index, manifest = self.make_index(["A", "B"])
        with index.open("a") as handle:
            handle.write(json.dumps({"market_id": "UNCOMMITTED"}) + "\n")
        with self.forbid_raw():
            self.assertEqual(self.discover(index_manifest_paths=[manifest]), ["A", "B"])
            self.assertEqual(self.discover(index_manifest_paths=[manifest]), ["A", "B"])
        with self.raw.open("a") as handle:
            handle.write(json.dumps({"market_id": "C"}) + "\n")
        self.assertEqual(self.discover(index_manifest_paths=[manifest]), ["A", "B", "C"])

    def test_capped_bootstrap_retains_existing_ids_and_does_not_claim_raw_coverage(self):
        self.write(self.raw, ["A", "B"])
        index, manifest = self.make_index(["A", "B"])
        self.write(self.output / "resolution_market_refs.jsonl", ["OLD"])
        line_bytes = len(index.read_bytes().splitlines(keepends=True)[0])
        with self.forbid_raw():
            self.assertEqual(self.discover(index_manifest_paths=[manifest], max_index_bootstrap_bytes=line_bytes), ["A", "OLD"])
            state = self.checkpoint()["sources"][str(self.raw.resolve())]
            self.assertEqual(state["offset"], 0)
            self.assertEqual(self.discover(index_manifest_paths=[manifest], max_index_bootstrap_bytes=line_bytes), ["A", "B", "OLD"])
            self.assertEqual(self.checkpoint()["sources"][str(self.raw.resolve())]["offset"], self.raw.stat().st_size)

    def test_incomplete_index_coverage_never_skips_raw_ids(self):
        self.write(self.raw, ["A", "OMITTED"])
        _, manifest = self.make_index(["A"], invalid_rows=1)
        self.assertEqual(self.discover(index_manifest_paths=[manifest]), ["A", "OMITTED"])

    def test_atomic_checkpoint_failure_can_retry_without_losing_ids(self):
        self.write(self.raw, ["A"])
        self.discover()
        with self.raw.open("a") as handle:
            handle.write(json.dumps({"market_id": "B"}) + "\n")
        with patch("bot.resolution_feed.atomic_write_json", side_effect=OSError("disk")):
            with self.assertRaisesRegex(OSError, "disk"):
                self.discover()
        self.assertEqual(self.discover(), ["A", "B"])

    def test_runtime_refresh_reuses_discovery_retries_unresolved_and_skips_resolved(self):
        a, b, c = [f"KXTEST-26JUN11-T{number}" for number in range(3)]
        self.write(self.raw, [a, b])
        config = {"resolution_feed": {
            "enabled": True, "mode": "incremental_unresolved",
            "market_ref_paths": [str(self.raw)], "output_dir": str(self.output),
            "max_fetch_attempts": 1, "retry_delay_seconds": 0,
        }}
        calls = []

        def fetch(mid):
            calls.append(mid)
            return {"ticker": mid, "status": "finalized", "result": "yes"} if mid == a else {"ticker": mid, "status": "open"}

        with patch("bot.scoreboard_resolution_backfill.time.sleep"):
            run_resolution_feed_once(config, force=True, fetch_market=fetch)
            with self.forbid_raw():
                run_resolution_feed_once(config, force=True, fetch_market=fetch)
            with self.raw.open("a") as handle:
                handle.write(json.dumps({"market_id": c}) + "\n")
            run_resolution_feed_once(config, force=True, fetch_market=fetch)
        self.assertEqual(calls, [a, b, b, b, c])
        state = json.loads((self.output / "state.json").read_text())
        self.assertEqual(state["discovery"]["remaining_raw_bytes"], 0)

    def test_runtime_config_finds_collector_custom_index_root_and_byte_budgets(self):
        cfg = normalize_resolution_feed_config({
            "prediction_lab": {"replay_index": {"root_dir": str(self.root / "custom_index")}},
            "resolution_feed": {"market_ref_paths": [str(self.raw)], "max_discovery_bytes": 1024,
                                "max_index_bootstrap_bytes": 2048},
        })
        self.assertEqual(cfg["market_ref_index_manifest_paths"], [str(self.root / "custom_index/collector_replay_index.manifest.json")])
        self.assertEqual(cfg["max_discovery_bytes"], 1024)
        self.assertEqual(cfg["max_index_bootstrap_bytes"], 2048)

    def test_unrelated_or_rotated_archive_index_cannot_skip_raw_discovery(self):
        self.write(self.raw, ["A"])
        _, manifest = self.make_index(["WRONG"])
        self.raw.rename(self.root / "old.jsonl")
        self.write(self.raw, ["NEW"])
        self.assertEqual(self.discover(index_manifest_paths=[manifest]), ["NEW"])

    def test_ongoing_committed_index_appends_avoid_raw_payload_reads(self):
        self.write(self.raw, ["A"])
        _, manifest = self.make_index(["A"])
        self.discover(index_manifest_paths=[manifest])
        with self.raw.open("a") as handle:
            handle.write(json.dumps({"market_id": "B"}) + "\n")
        self.make_index(["A", "B"])
        with self.forbid_raw():
            self.assertEqual(self.discover(index_manifest_paths=[manifest]), ["A", "B"])
            self.assertEqual(self.discover(index_manifest_paths=[manifest]), ["A", "B"])
        state = self.checkpoint()
        self.assertEqual(state["progress"]["raw_bytes_read"], 0)
        self.assertEqual(state["progress"]["index_bytes_read"], 0)

    def test_index_later_skips_rows_falls_back_to_saved_raw_offset(self):
        self.write(self.raw, ["A"])
        _, manifest = self.make_index(["A"])
        self.discover(index_manifest_paths=[manifest])
        with self.raw.open("a") as handle:
            handle.write(json.dumps({"market_id": "OMITTED"}) + "\n")
        self.make_index(["A"], invalid_rows=1)
        self.assertEqual(self.discover(index_manifest_paths=[manifest]), ["A", "OMITTED"])


if __name__ == "__main__":
    unittest.main()
