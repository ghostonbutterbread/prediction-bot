import json
import tempfile
import unittest
from pathlib import Path

from bot.collector_replay_index import build_collector_replay_index, load_indexed_collector_rows


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


if __name__ == "__main__":
    unittest.main()
