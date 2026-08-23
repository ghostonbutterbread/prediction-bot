import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from bot.weather.source_history_manifest import (
    SourceHistoryManifestError,
    collapse_strict_source_history_rows,
    materialize_strict_source_history_collapse,
    load_source_history_manifest,
)
from scripts.materialize_strict_source_history_collapse import main as collapse_main


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

    def test_collapse_keeps_earliest_source_as_of_per_strict_event_unit(self):
        base = {
            "eligible_for_source_history": True,
            "source_correctness_eligibility": "eligible_strict_source_proof",
            "source_id": "nws",
            "event_ticker": "KXHIGHMIA-26AUG02",
            "market_id": "KXHIGHMIA-26AUG02-T80",
            "city_id": "miami_fl",
            "market_kind": "high",
            "contract_shape": "threshold",
            "question_side": "above",
            "market_date": "2026-08-02",
        }
        later_poll = {**base, "source_as_of": "2026-08-01T12:00:00+00:00", "observed_at": "2026-08-01T12:01:00+00:00", "source_record_sha256": "b" * 64}
        earliest_poll = {**base, "source_as_of": "2026-08-01T08:00:00+00:00", "observed_at": "2026-08-01T12:02:00+00:00", "source_record_sha256": "a" * 64}

        collapsed, metadata = collapse_strict_source_history_rows([later_poll, earliest_poll])

        self.assertEqual(collapsed, [earliest_poll])
        self.assertEqual(metadata["strict_rows_seen"], 2)
        self.assertEqual(metadata["collapsed_repeat_polls"], 1)
        self.assertEqual(metadata["independent_rows"], 1)

    def test_collapse_uses_nested_source_provenance_hash_for_tied_times(self):
        base = {
            "eligible_for_source_history": True, "source_correctness_eligibility": "eligible_strict_source_proof",
            "source_id": "nws", "event_ticker": "KXHIGHMIA-26AUG02", "market_id": "KXHIGHMIA-26AUG02-T80",
            "city_id": "miami_fl", "market_kind": "high", "contract_shape": "threshold", "question_side": "above",
            "market_date": "2026-08-02", "source_as_of": "2026-08-01T08:00:00+00:00", "observed_at": "2026-08-01T08:01:00+00:00",
        }
        later_hash = {**base, "source_provenance": {"source_record_sha256": "b" * 64}}
        earlier_hash = {**base, "source_provenance": {"source_record_sha256": "a" * 64}}

        collapsed, metadata = collapse_strict_source_history_rows([later_hash, earlier_hash])

        self.assertEqual(collapsed, [earlier_hash])
        self.assertEqual(metadata["collapsed_repeat_polls"], 1)

    def test_collapse_materialization_writes_hash_bound_derived_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "settled.jsonl"
            source.write_text(json.dumps({
                "eligible_for_source_history": True, "source_correctness_eligibility": "eligible_strict_source_proof",
                "source_id": "nws", "event_ticker": "KXHIGHMIA-26AUG02", "market_id": "KXHIGHMIA-26AUG02-T80",
                "city_id": "miami_fl", "market_date": "2026-08-02", "market_kind": "high", "contract_shape": "threshold",
                "question_side": "above", "source_as_of": "2026-08-01T08:00:00+00:00", "observed_at": "2026-08-01T08:01:00+00:00",
                "source_record_sha256": "a" * 64,
            }) + "\n", encoding="utf-8")

            result = materialize_strict_source_history_collapse(source, root / "collapse")

            self.assertEqual(result.metadata["mode"], "offline_derived_strict_source_history_collapse")
            self.assertEqual(result.metadata["counts"]["independent_rows"], 1)
            self.assertEqual(result.metadata["inputs"]["settled_source_ledger_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertTrue(result.collapsed_path.is_file())
            self.assertTrue(result.metadata_path.is_file())

    def test_collapse_cli_reports_independent_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "settled.jsonl"
            source.write_text("\n", encoding="utf-8")
            from unittest.mock import patch
            with patch("sys.argv", ["collapse.py", "--settled-source-ledger", str(source), "--output-dir", str(root / "out")]):
                self.assertEqual(collapse_main(), 0)
            self.assertTrue((root / "out" / "strict_independent_source_history.jsonl").is_file())

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
