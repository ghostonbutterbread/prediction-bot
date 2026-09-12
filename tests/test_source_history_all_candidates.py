"""Pure forecast history must not depend on executable trading inputs."""
import copy
import unittest

from bot.replay_decision_input import build_replay_decision_input_v1
from test_auto_source_router_promotion import _collector_row


class AllCandidateSourceHistoryTests(unittest.TestCase):
    def test_quote_less_skip_is_projected_only_for_source_history(self):
        row = _collector_row()
        row.update(yes_price=None, no_price=None, direction="SKIP", edge=-1,
                   kelly_fraction=0, main_decision={"action": "SKIP", "reason_code": "risk_rejected"})
        row["decision_artifact"]["execution_snapshot"] = {}
        original = copy.deepcopy(row)
        self.assertFalse(build_replay_decision_input_v1(row).ok)
        built = build_replay_decision_input_v1(row, source_history_only=True)
        self.assertTrue(built.ok, built.errors)
        self.assertEqual(built.record["input_purpose"], "source_correctness_history")
        self.assertNotIn("yes_price", built.record["market"])
        self.assertEqual(row, original)

    def test_unproven_range_is_not_inferred_from_scalar_threshold(self):
        from bot.weather.source_observation_ledger import _source_implied_outcome
        self.assertIsNone(_source_implied_outcome({
            "forecast_temp_f": 76.7, "threshold": 77.0,
            "question_side": "range", "contract_shape": "range",
        }))

    def test_strict_executable_mode_cannot_be_relaxed_as_history(self):
        with self.assertRaisesRegex(ValueError, "not strict executable replay"):
            build_replay_decision_input_v1(_collector_row(), strict=True, source_history_only=True)

    def test_writer_and_direct_history_admit_quote_less_risk_skip(self):
        import json
        import tempfile
        from pathlib import Path
        from bot.auto_source_router_promotion import auto_populate_source_router_history
        from bot.collector_replay_index import build_collector_replay_index
        from bot.weather.source_router_direct_history import load_direct_strict_source_history
        from test_auto_source_router_promotion import _strict_resolution, _write_jsonl
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = _collector_row()
            row["shared_snapshot_id"] = "snapshot-1"
            row.update(yes_price=None, no_price=None, direction="SKIP", edge=-1,
                       kelly_fraction=0, main_decision={"action": "SKIP", "reason_code": "risk_rejected"})
            row["decision_artifact"]["execution_snapshot"] = {}
            archive, resolutions = root / "raw.jsonl", root / "resolutions.jsonl"
            _write_jsonl(archive, [row])
            _write_jsonl(resolutions, [_strict_resolution(row["market_id"])])
            index, manifest = root / "index.jsonl", root / "manifest.json"
            build_collector_replay_index(archive, index, manifest)
            direct = load_direct_strict_source_history(index_path=index, manifest_path=manifest,
                strict_resolutions_path=resolutions, as_of_decision_time="2026-08-06T00:00:00+00:00")
            self.assertEqual(direct.counts["settled_strict_observations"], 1, direct.counts)
            writer = auto_populate_source_router_history(collector_snapshots_path=archive,
                strict_resolutions_path=resolutions, output_root=root / "data/derived_reports/history", storage_root=root)
            import hashlib
            from bot.auto_source_router_promotion import PIPELINE_SCHEMA_VERSION, _canonical_bytes
            legacy_generation = hashlib.sha256(_canonical_bytes({
                "schema_name": "auto_source_router_history_promotion",
                "schema_version": PIPELINE_SCHEMA_VERSION,
                "collector_snapshots_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "strict_resolutions_sha256": hashlib.sha256(resolutions.read_bytes()).hexdigest(),
            })).hexdigest()
            self.assertNotEqual(writer.generation_dir.name, legacy_generation)
            rows = [json.loads(line) for line in writer.scoreboard_path.read_text().splitlines()]
            self.assertEqual(rows[0]["sample_count"], 1)
            for score in [rows[0], direct.scorecard_rows[0]]:
                score["provenance"].pop("source_history_sha256", None)
            self.assertEqual(rows, direct.scorecard_rows)


if __name__ == "__main__":
    unittest.main()
