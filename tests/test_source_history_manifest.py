import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from bot.weather.source_history_manifest import SourceHistoryManifestError, load_source_history_manifest


class SourceHistoryManifestTests(unittest.TestCase):
    def _write_manifest(self, root: Path, **overrides) -> Path:
        ledger = root / "source_ledger.jsonl"
        resolution = root / "strict_resolutions.jsonl"
        raw = root / "market_snapshots.jsonl"
        index = root / "collector_replay_index.jsonl"
        replay_manifest = root / "collector_replay_index.manifest.json"
        for path in (ledger, resolution, raw, index, replay_manifest):
            path.write_text("{}\n", encoding="utf-8")
        paths = {
            "source_ledger_path": ledger,
            "strict_resolution_path": resolution,
            "raw_archive_path": raw,
            "replay_index_path": index,
            "replay_manifest_path": replay_manifest,
        }
        payload = {
            "schema_name": "source_history_manifest",
            "schema_version": 1,
            "historical_counterfactual_only": True,
            "non_mutating": True,
            "join_key": "market_id",
            "eligibility_filter": "eligible_for_reliability == true",
            "availability_field": "settlement_ts",
            **{key: str(path) for key, path in paths.items()},
            "sha256": {key.removesuffix("_path"): hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()},
        }
        payload.update(overrides)
        manifest_path = root / "source_history_manifest.json"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        return manifest_path

    def test_loads_hash_verified_paper_only_history_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._write_manifest(Path(tmp))

            manifest = load_source_history_manifest(manifest_path)

            self.assertTrue(manifest.historical_counterfactual_only)
            self.assertTrue(manifest.non_mutating)
            self.assertEqual(manifest.availability_field, "settlement_ts")
            self.assertEqual(manifest.join_key, "market_id")
            self.assertEqual(manifest.source_ledger_path.name, "source_ledger.jsonl")

    def test_rejects_hash_mismatch_before_history_is_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._write_manifest(Path(tmp))
            payload = json.loads(manifest_path.read_text())
            payload["sha256"]["source_ledger"] = "0" * 64
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(SourceHistoryManifestError, "sha256 mismatch.*source_ledger"):
                load_source_history_manifest(manifest_path)

    def test_rejects_non_settlement_history_availability_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = self._write_manifest(Path(tmp), availability_field="resolved_at")

            with self.assertRaisesRegex(SourceHistoryManifestError, "availability_field"):
                load_source_history_manifest(manifest_path)


if __name__ == "__main__":
    unittest.main()
