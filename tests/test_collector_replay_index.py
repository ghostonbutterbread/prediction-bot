import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot.collector_replay_index import build_collector_replay_index, load_indexed_collector_rows, update_collector_replay_index
from bot.prediction_lab_collect import PredictionLabCollectorDaemon


class CollectorReplayIndexTests(unittest.TestCase):
    def test_index_references_raw_rows_without_copying_nested_payload_or_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_path = root / "market_snapshots.jsonl"
            index_path = root / "replay_index.jsonl"
            manifest_path = root / "replay_index.manifest.json"
            raw_rows = [
                {
                    "run_id": "run-001",
                    "market_id": "KXONE",
                    "observed_at": "2026-07-01T12:00:00+00:00",
                    "yes_price": 0.30,
                    "no_price": 0.70,
                    "confidence": 0.9,
                    "decision_artifact": {"source_context": {"very_large": "x" * 10_000}, "actual_outcome": "YES"},
                },
                {
                    "run_id": "run-002",
                    "market_id": "KXTWO",
                    "observed_at": "2026-07-02T12:00:00+00:00",
                    "yes_price": 0.40,
                    "no_price": 0.60,
                },
            ]
            raw_path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in raw_rows))

            result = build_collector_replay_index(raw_path, index_path, manifest_path)

            self.assertEqual(result["indexed_rows"], 2)
            index_rows = [json.loads(line) for line in index_path.read_text().splitlines()]
            self.assertEqual([row["market_id"] for row in index_rows], ["KXONE", "KXTWO"])
            self.assertNotIn("decision_artifact", json.dumps(index_rows))
            self.assertNotIn("actual_outcome", json.dumps(index_rows))
            self.assertLess(index_path.stat().st_size, raw_path.stat().st_size / 10)

            hydrated = list(load_indexed_collector_rows(index_path, manifest_path))
            self.assertEqual(hydrated, raw_rows)
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest["source_sha256"], result["source_sha256"])
            self.assertEqual(manifest["index_schema_name"], "collector_replay_index")

    def test_update_appends_only_new_raw_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_path = root / "market_snapshots.jsonl"
            index_path = root / "replay_index.jsonl"
            manifest_path = root / "replay_index.manifest.json"
            first = {"run_id": "run-001", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00"}
            second = {"run_id": "run-002", "market_id": "KXTWO", "observed_at": "2026-07-02T12:00:00+00:00"}
            raw_path.write_text(json.dumps(first) + "\n", encoding="utf-8")
            build_collector_replay_index(raw_path, index_path, manifest_path)
            with raw_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(second) + "\n")

            result = update_collector_replay_index(raw_path, index_path, manifest_path)

            self.assertEqual(result["new_indexed_rows"], 1)
            self.assertEqual(result["indexed_rows"], 2)
            self.assertEqual(list(load_indexed_collector_rows(index_path, manifest_path)), [first, second])

    def test_update_retries_unterminated_trailing_jsonl_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_path = root / "market_snapshots.jsonl"
            index_path = root / "replay_index.jsonl"
            manifest_path = root / "replay_index.manifest.json"
            first = {"run_id": "run-001", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00"}
            second = {"run_id": "run-002", "market_id": "KXTWO", "observed_at": "2026-07-02T12:00:00+00:00"}
            raw_path.write_text(json.dumps(first) + "\n", encoding="utf-8")
            build_collector_replay_index(raw_path, index_path, manifest_path)
            with raw_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(second)[:-1])
            incomplete = update_collector_replay_index(raw_path, index_path, manifest_path)
            self.assertEqual(incomplete["new_indexed_rows"], 0)
            self.assertGreater(incomplete["unindexed_trailing_bytes"], 0)
            with raw_path.open("a", encoding="utf-8") as handle:
                handle.write("}\n")
            completed = update_collector_replay_index(raw_path, index_path, manifest_path)
            self.assertEqual(completed["new_indexed_rows"], 1)
            self.assertEqual(list(load_indexed_collector_rows(index_path, manifest_path)), [first, second])

    def test_collector_hook_writes_compact_index_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_path = root / "market_snapshots.jsonl"
            raw_path.write_text(json.dumps({"run_id": "run-001", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00"}) + "\n", encoding="utf-8")
            lab = SimpleNamespace(root_dir=root, market_snapshots_path=raw_path)
            config = {"prediction_lab": {"replay_index": {"enabled": True}}}

            result = PredictionLabCollectorDaemon._update_replay_index(config, lab)

            self.assertEqual(result["indexed_rows"], 1)
            self.assertTrue((root / "replay_index" / "collector_replay_index.jsonl").is_file())
            self.assertEqual(PredictionLabCollectorDaemon._update_replay_index({"prediction_lab": {}}, lab), None)


if __name__ == "__main__":
    unittest.main()
