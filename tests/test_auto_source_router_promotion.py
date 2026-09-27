import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.collector_paths import COLLECTOR_ROOT_ENV, auto_source_router_history_root
from bot.auto_source_router_promotion import (
    _validate_current_generation, auto_populate_source_router_history,
    load_verified_strict_scorecard_rows,
)
from bot.weather.source_history_manifest import SourceHistoryManifestError, load_source_history_manifest
from bot.paper_shadow_lanes import _LaneDefinition, _source_router_decision
from bot.paper_shadow_lanes import _load_compatible_strict_scorecard_rows
from bot.weather.collector_source_router_replay import run_collector_source_router_replay
from scripts.weather_source_router_replay import _load_router_ledger_rows


ROOT = Path(__file__).resolve().parents[1]
OBSOLETE_GENERATIONS = Path("/home/ryushe/projects/prediction-bot-beta-integration/data/derived_reports/auto_source_router_history/generations")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _collector_row(*, strict: bool = True, market_id: str = "KXHIGHSEA-26AUG03-T70") -> dict:
    source = {
        "source_id": "nws", "source_name": "NWS", "source_location_city": "Seattle",
        "forecast_measurement_kind": "high", "contract_shape": "tail", "question_side": "above",
        "forecast_high": 75.0, "source_as_of": "2026-08-01T11:54:00+00:00",
        "source_evidence_version": 1, "evidence_type": "forecast", "scoreable_forecast": True,
        "target_mapping": {
            "market_target_date": "2026-08-03", "source_target_date": "2026-08-03",
            "source_timezone": "America/Los_Angeles",
        },
    }
    if not strict:
        source.pop("source_location_city")
    return {
        "market_id": market_id,
        "observed_at": "2026-08-01T12:00:00+00:00",
        "question": "Will Seattle high temperature be above 70°?",
        "yes_price": 0.42, "no_price": 0.58,
        "decision_artifact": {"source_context": {"source": "provided", "mode": "prediction_lab", "as_of": "2026-08-01T11:55:00+00:00", "data": {
            "market_metadata": {"event_ticker": market_id.rsplit("-", 1)[0], "city": "Seattle", "city_id": "seattle_wa", "market_kind": "high", "contract_shape": "threshold"},
            "weather_source_snapshot": {
                "market_id": market_id, "question": "Will Seattle high temperature be above 70°?",
                "market_date": "2026-08-03", "station_resolution": {"city_id": "seattle_wa", "city": "Seattle"},
                "sources": [source],
            },
        }}},
    }


def _strict_resolution(market_id: str) -> dict:
    return {
        "market_id": market_id, "requested_market_id": market_id, "returned_market_id": market_id,
        "market_status": "finalized", "kalshi_result": "yes", "settlement_ts": "2026-08-04T00:00:00Z",
        "resolution_id": "authoritative-resolution-1",
    }


class AutoSourceRouterPromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.collector_root = self.root / "collector"
        self._collector_root_env = patch.dict(os.environ, {COLLECTOR_ROOT_ENV: str(self.collector_root)})
        self._collector_root_env.start()
        self.derived_root = self.collector_root / "data" / "derived_reports"
        self.archive = self.root / "immutable_snapshots.jsonl"
        self.resolutions = self.root / "authoritative_resolutions.jsonl"
        self.derived_root.mkdir(parents=True, exist_ok=True)
        self.output_root = Path(tempfile.mkdtemp(prefix="test_auto_source_router_", dir=self.derived_root))

    def tearDown(self) -> None:
        self._collector_root_env.stop()
        self.tempdir.cleanup()

    def run_pipeline(self, rows: list[dict], resolutions: list[dict]):
        _write_jsonl(self.archive, rows)
        _write_jsonl(self.resolutions, resolutions)
        return auto_populate_source_router_history(
            collector_snapshots_path=self.archive,
            strict_resolutions_path=self.resolutions,
            output_root=self.output_root,
        )

    def relocated_v1_generation(self):
        """Generate tiny complete evidence, then change only v1 locator metadata."""
        self.output_root = self.derived_root / "auto_source_router_history"
        row = _collector_row()
        first = self.run_pipeline([row], [_strict_resolution(row["market_id"])])
        # The current producer emits v2. Its tiny synthetic output stands in for
        # a published v1 after changing the promotion version, not its evidence.
        generation_dir = first.generation_dir
        old_dir = OBSOLETE_GENERATIONS / generation_dir.name

        def stale(value):
            return str(old_dir / Path(value).relative_to(generation_dir))

        nested = json.loads(first.history_manifest_path.read_text(encoding="utf-8"))
        for key in ("source_ledger", "strict_resolution", "raw_archive", "replay_index", "replay_manifest"):
            nested[f"{key}_path"] = stale(nested[f"{key}_path"])
        nested_bytes = (json.dumps(nested, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        first.history_manifest_path.write_bytes(nested_bytes)

        promotion = json.loads(first.manifest_path.read_text(encoding="utf-8"))
        promotion["schema_version"] = 1
        for key, value in promotion["artifacts"].items():
            promotion["artifacts"][key] = stale(value)
        for key in ("history_ledger_path", "scoreboard_path", "history_manifest_path"):
            promotion["runtime_consumption"][key] = stale(promotion["runtime_consumption"][key])
        promotion["source_history_manifest"]["path"] = stale(promotion["source_history_manifest"]["path"])
        promotion["artifact_sha256"]["source_history_manifest"] = hashlib.sha256(nested_bytes).hexdigest()
        first.manifest_path.write_text(json.dumps(promotion, sort_keys=True) + "\n", encoding="utf-8")
        return first, promotion, nested

    def test_relocated_v1_rejects_unsupported_promotion_schema_even_with_contained_artifacts(self) -> None:
        first, promotion, nested = self.relocated_v1_generation()
        # Contained paths isolate the schema contract from the locator guard.
        old_dir = OBSOLETE_GENERATIONS / first.generation_dir.name
        def contained(value):
            return str(first.generation_dir / Path(value).relative_to(old_dir))
        promotion["artifacts"] = {key: contained(value) for key, value in promotion["artifacts"].items()}
        promotion["runtime_consumption"] = {
            key: contained(value) if key in ("history_ledger_path", "history_manifest_path", "scoreboard_path") else value
            for key, value in promotion["runtime_consumption"].items()
        }
        promotion["source_history_manifest"]["path"] = str(first.history_manifest_path)
        for key in ("source_ledger", "strict_resolution", "raw_archive", "replay_index", "replay_manifest"):
            nested[f"{key}_path"] = contained(nested[f"{key}_path"])
        nested_bytes = (json.dumps(nested, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        first.history_manifest_path.write_bytes(nested_bytes)
        promotion["artifact_sha256"]["source_history_manifest"] = hashlib.sha256(nested_bytes).hexdigest()
        for version in (None, True, 3):
            with self.subTest(version=version):
                promotion["schema_version"] = version
                first.manifest_path.write_text(json.dumps(promotion), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "unsupported promotion manifest schema"):
                    _validate_current_generation(self.output_root)

    def test_relocated_v1_rejects_unqualified_nested_manifest_with_updated_digest(self) -> None:
        first, promotion, nested = self.relocated_v1_generation()
        failures = (
            ("schema_version", 2), ("schema_version", None), ("schema_version", True),
            ("historical_counterfactual_only", False), ("non_mutating", False),
            ("historical_counterfactual_only", 1), ("non_mutating", 1),
            ("historical_counterfactual_only", 1.0), ("non_mutating", 1.0),
            ("join_key", "candidate_id"), ("availability_field", "resolved_at"),
            ("eligibility_filter", ""),
        )
        for field, invalid in failures:
            with self.subTest(field=field, invalid=invalid):
                changed = {**nested, field: invalid}
                payload = (json.dumps(changed, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
                first.history_manifest_path.write_bytes(payload)
                promotion["artifact_sha256"]["source_history_manifest"] = hashlib.sha256(payload).hexdigest()
                first.manifest_path.write_text(json.dumps(promotion), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "source history manifest contract"):
                    _validate_current_generation(self.output_root)

    def test_relocated_v1_current_verified_scorecard_and_reuse_resolve_present_paths(self) -> None:
        first, promotion, _ = self.relocated_v1_generation()
        _validate_current_generation(self.output_root)
        rows = load_verified_strict_scorecard_rows(first.scoreboard_path)
        self.assertIsNotNone(rows)
        assert rows is not None
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schema_name"], "strict_finalized_source_router_scorecard")
        reused = auto_populate_source_router_history(
            collector_snapshots_path=self.archive, strict_resolutions_path=self.resolutions,
            output_root=self.output_root,
        )
        self.assertTrue(reused.reused)
        self.assertEqual((reused.history_path, reused.scoreboard_path, reused.history_manifest_path),
                         (first.history_path, first.scoreboard_path, first.history_manifest_path))
        self.assertNotEqual(str(reused.history_path), promotion["runtime_consumption"]["history_ledger_path"])

    def test_relocated_v1_standalone_history_manifest_loads_verified_present_paths(self) -> None:
        first, _, _ = self.relocated_v1_generation()
        loaded = load_source_history_manifest(first.history_manifest_path)
        self.assertEqual(loaded.source_ledger_path, first.history_path)
        self.assertTrue(loaded.raw_archive_path.is_file())

    def test_relocated_v1_does_not_use_preexisting_obsolete_location(self) -> None:
        first, _, nested = self.relocated_v1_generation()
        # Patch the known-prefix constant only inside this synthetic fixture:
        # never create or read anything at the real obsolete checkout prefix.
        old_generations = self.root / "obsolete-checkout" / "generations"
        old_ledger = old_generations / first.generation_dir.name / first.history_path.relative_to(first.generation_dir)
        old_ledger.parent.mkdir(parents=True)
        old_ledger.write_bytes(first.history_path.read_bytes())
        nested["source_ledger_path"] = str(old_ledger)
        for key in ("strict_resolution", "raw_archive", "replay_index", "replay_manifest"):
            nested[f"{key}_path"] = str(first.generation_dir / Path(nested[f"{key}_path"]).relative_to(
                OBSOLETE_GENERATIONS / first.generation_dir.name
            ))
        first.history_manifest_path.write_text(json.dumps(nested), encoding="utf-8")
        with patch("bot.weather.source_history_manifest._V1_OBSOLETE_GENERATIONS", old_generations):
            loaded = load_source_history_manifest(first.history_manifest_path)
        self.assertEqual(loaded.source_ledger_path, first.history_path)

    def test_relocated_v1_stays_incompatible_with_forward_paper_v2_lane(self) -> None:
        first, _, _ = self.relocated_v1_generation()
        with self.assertRaisesRegex(ValueError, "incompatible Source Router history schema version 1"):
            _load_compatible_strict_scorecard_rows(first.scoreboard_path)

    def test_relocated_v1_rejects_unrecognized_old_prefix(self) -> None:
        first, promotion, nested = self.relocated_v1_generation()
        promotion["artifacts"]["scoreboard"] = promotion["artifacts"]["scoreboard"].replace(
            "/prediction-bot-beta-integration/", "/another-checkout/"
        )
        first.manifest_path.write_text(json.dumps(promotion), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside generation"):
            _validate_current_generation(self.output_root)
        nested["source_ledger_path"] = nested["source_ledger_path"].replace(
            "/prediction-bot-beta-integration/", "/another-checkout/"
        )
        first.history_manifest_path.write_text(json.dumps(nested), encoding="utf-8")
        with self.assertRaisesRegex(SourceHistoryManifestError, "does not exist"):
            load_source_history_manifest(first.history_manifest_path)

    def test_relocated_v1_rejects_different_generation_id(self) -> None:
        first, promotion, nested = self.relocated_v1_generation()
        other_id = "f" * 64 if first.generation_dir.name != "f" * 64 else "e" * 64
        promotion["artifacts"]["scoreboard"] = promotion["artifacts"]["scoreboard"].replace(first.generation_dir.name, other_id)
        first.manifest_path.write_text(json.dumps(promotion), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "relocated v1 generation id mismatch"):
            _validate_current_generation(self.output_root)
        nested["source_ledger_path"] = nested["source_ledger_path"].replace(first.generation_dir.name, other_id)
        first.history_manifest_path.write_text(json.dumps(nested), encoding="utf-8")
        with self.assertRaisesRegex(SourceHistoryManifestError, "relocated v1 generation id mismatch"):
            load_source_history_manifest(first.history_manifest_path)

    def test_relocated_v1_rejects_foreign_generation_id_symlinked_to_current(self) -> None:
        first, promotion, nested = self.relocated_v1_generation()
        other_id = "f" * 64 if first.generation_dir.name != "f" * 64 else "e" * 64
        old_generations = self.root / "obsolete-checkout" / "generations"
        old_generations.mkdir(parents=True)
        (old_generations / other_id).symlink_to(first.generation_dir, target_is_directory=True)
        original = OBSOLETE_GENERATIONS / first.generation_dir.name

        def foreign(value):
            return str(old_generations / other_id / Path(value).relative_to(original))

        promotion["artifacts"] = {key: foreign(value) for key, value in promotion["artifacts"].items()}
        for key in ("history_ledger_path", "history_manifest_path", "scoreboard_path"):
            promotion["runtime_consumption"][key] = foreign(promotion["runtime_consumption"][key])
        promotion["source_history_manifest"]["path"] = foreign(promotion["source_history_manifest"]["path"])
        for key in ("source_ledger", "strict_resolution", "raw_archive", "replay_index", "replay_manifest"):
            nested[f"{key}_path"] = foreign(nested[f"{key}_path"])
        nested_bytes = (json.dumps(nested, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        first.history_manifest_path.write_bytes(nested_bytes)
        promotion["artifact_sha256"]["source_history_manifest"] = hashlib.sha256(nested_bytes).hexdigest()
        first.manifest_path.write_text(json.dumps(promotion), encoding="utf-8")
        with patch("bot.weather.source_history_manifest._V1_OBSOLETE_GENERATIONS", old_generations):
            with self.assertRaisesRegex(ValueError, "relocated v1 generation id mismatch"):
                _validate_current_generation(self.output_root)
            with self.assertRaisesRegex(SourceHistoryManifestError, "relocated v1 generation id mismatch"):
                load_source_history_manifest(first.history_manifest_path)

    def test_relocated_v1_rejects_parent_segment_before_foreign_id_symlink(self) -> None:
        first, promotion, nested = self.relocated_v1_generation()
        old_generations = self.root / "obsolete-checkout" / "generations"
        old_generations.mkdir(parents=True)
        (old_generations / first.generation_dir.name).mkdir()
        other_id = "f" * 64 if first.generation_dir.name != "f" * 64 else "e" * 64
        (old_generations / other_id).symlink_to(first.generation_dir, target_is_directory=True)
        original = OBSOLETE_GENERATIONS / first.generation_dir.name

        def evasive(value):
            return str(old_generations / first.generation_dir.name / ".." / other_id / Path(value).relative_to(original))

        promotion["artifacts"] = {key: evasive(value) for key, value in promotion["artifacts"].items()}
        for key in ("history_ledger_path", "history_manifest_path", "scoreboard_path"):
            promotion["runtime_consumption"][key] = evasive(promotion["runtime_consumption"][key])
        promotion["source_history_manifest"]["path"] = evasive(promotion["source_history_manifest"]["path"])
        for key in ("source_ledger", "strict_resolution", "raw_archive", "replay_index", "replay_manifest"):
            nested[f"{key}_path"] = evasive(nested[f"{key}_path"])
        nested_bytes = (json.dumps(nested, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        first.history_manifest_path.write_bytes(nested_bytes)
        promotion["artifact_sha256"]["source_history_manifest"] = hashlib.sha256(nested_bytes).hexdigest()
        first.manifest_path.write_text(json.dumps(promotion), encoding="utf-8")
        with patch("bot.weather.source_history_manifest._V1_OBSOLETE_GENERATIONS", old_generations):
            with self.assertRaisesRegex(ValueError, "v1 generation locator contains parent traversal"):
                _validate_current_generation(self.output_root)
            with self.assertRaisesRegex(SourceHistoryManifestError, "v1 generation locator contains parent traversal"):
                load_source_history_manifest(first.history_manifest_path)

    def test_relocated_v1_rejects_unrelated_prefix_symlinked_to_current(self) -> None:
        first, promotion, nested = self.relocated_v1_generation()
        unrelated = self.root / "unrelated-checkout" / "generations" / first.generation_dir.name
        unrelated.parent.mkdir(parents=True)
        unrelated.symlink_to(first.generation_dir, target_is_directory=True)
        old_dir = OBSOLETE_GENERATIONS / first.generation_dir.name

        def aliased(value):
            return str(unrelated / Path(value).relative_to(old_dir))

        promotion["artifacts"] = {key: aliased(value) for key, value in promotion["artifacts"].items()}
        for key in ("history_ledger_path", "history_manifest_path", "scoreboard_path"):
            promotion["runtime_consumption"][key] = aliased(promotion["runtime_consumption"][key])
        promotion["source_history_manifest"]["path"] = aliased(promotion["source_history_manifest"]["path"])
        for key in ("source_ledger", "strict_resolution", "raw_archive", "replay_index", "replay_manifest"):
            nested[f"{key}_path"] = aliased(nested[f"{key}_path"])
        nested_bytes = (json.dumps(nested, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        first.history_manifest_path.write_bytes(nested_bytes)
        promotion["artifact_sha256"]["source_history_manifest"] = hashlib.sha256(nested_bytes).hexdigest()
        first.manifest_path.write_text(json.dumps(promotion), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unrecognized v1 generation locator"):
            _validate_current_generation(self.output_root)
        with self.assertRaisesRegex(SourceHistoryManifestError, "unrecognized v1 generation locator"):
            load_source_history_manifest(first.history_manifest_path)

    def test_relocated_v1_rejects_symlink_escape_even_when_external_bytes_match(self) -> None:
        first, _, _ = self.relocated_v1_generation()
        external = self.root / "matching-ledger.jsonl"
        external.write_bytes(first.history_path.read_bytes())
        first.history_path.unlink()
        first.history_path.symlink_to(external)
        with self.assertRaisesRegex(ValueError, "outside generation"):
            _validate_current_generation(self.output_root)
        with self.assertRaisesRegex(SourceHistoryManifestError, "outside generation"):
            load_source_history_manifest(first.history_manifest_path)

    def test_relocated_v1_rejects_missing_and_changed_artifact_bytes(self) -> None:
        first, _, _ = self.relocated_v1_generation()
        original = first.scoreboard_path.read_bytes()
        first.scoreboard_path.unlink()
        with self.assertRaisesRegex(ValueError, "artifact hash mismatch: scoreboard"):
            _validate_current_generation(self.output_root)
        first.scoreboard_path.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "artifact hash mismatch: scoreboard"):
            _validate_current_generation(self.output_root)
        first.history_path.write_bytes(first.history_path.read_bytes() + b" ")
        with self.assertRaisesRegex(SourceHistoryManifestError, "sha256 mismatch for source_ledger"):
            load_source_history_manifest(first.history_manifest_path)

    def test_relocated_v1_rejects_malformed_scorecard_even_if_hash_is_updated(self) -> None:
        first, promotion, _ = self.relocated_v1_generation()
        malformed = b"{invalid json}\n"
        first.scoreboard_path.write_bytes(malformed)
        promotion["artifact_sha256"]["scoreboard"] = hashlib.sha256(malformed).hexdigest()
        first.manifest_path.write_text(json.dumps(promotion), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "strict scorecard is invalid JSON"):
            _validate_current_generation(self.output_root)

    def test_relocated_v1_requires_runtime_and_artifact_path_equality(self) -> None:
        first, promotion, _ = self.relocated_v1_generation()
        promotion["runtime_consumption"]["scoreboard_path"] = promotion["runtime_consumption"]["history_ledger_path"]
        first.manifest_path.write_text(json.dumps(promotion), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "scoreboard_path does not match"):
            _validate_current_generation(self.output_root)

    def test_new_promotion_version_cannot_use_v1_relocation(self) -> None:
        first, promotion, nested = self.relocated_v1_generation()
        promotion["schema_version"] = 3
        first.manifest_path.write_text(json.dumps(promotion), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported promotion manifest schema"):
            _validate_current_generation(self.output_root)
        nested["schema_version"] = 2
        first.history_manifest_path.write_text(json.dumps(nested), encoding="utf-8")
        with self.assertRaisesRegex(SourceHistoryManifestError, "unsupported source history manifest schema"):
            load_source_history_manifest(first.history_manifest_path)

    def test_boolean_schema_version_is_not_v1_relocation_authority(self) -> None:
        first, promotion, nested = self.relocated_v1_generation()
        promotion["schema_version"] = True
        first.manifest_path.write_text(json.dumps(promotion), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unsupported promotion manifest schema"):
            _validate_current_generation(self.output_root)
        nested["schema_version"] = True
        first.history_manifest_path.write_text(json.dumps(nested), encoding="utf-8")
        with self.assertRaisesRegex(SourceHistoryManifestError, "unsupported source history manifest schema"):
            load_source_history_manifest(first.history_manifest_path)

    def test_default_output_root_uses_the_collector_volume(self) -> None:
        row = _collector_row()
        _write_jsonl(self.archive, [row])
        _write_jsonl(self.resolutions, [_strict_resolution(row["market_id"])])

        result = auto_populate_source_router_history(
            collector_snapshots_path=self.archive,
            strict_resolutions_path=self.resolutions,
        )

        self.assertEqual(auto_source_router_history_root(), self.collector_root / "data" / "derived_reports" / "auto_source_router_history")
        self.assertTrue(result.generation_dir.is_relative_to(auto_source_router_history_root()))
        self.assertFalse(result.generation_dir.is_relative_to(ROOT / "data"))

    def test_explicit_root_disk_output_is_rejected(self) -> None:
        row = _collector_row()
        _write_jsonl(self.archive, [row])
        _write_jsonl(self.resolutions, [_strict_resolution(row["market_id"])])

        with self.assertRaisesRegex(ValueError, "output root must be below"):
            auto_populate_source_router_history(
                collector_snapshots_path=self.archive,
                strict_resolutions_path=self.resolutions,
                output_root=ROOT / "data" / "derived_reports" / "auto_source_router_history",
            )

    def test_no_resolutions_writes_no_router_history(self) -> None:
        result = self.run_pipeline([_collector_row()], [])

        self.assertEqual(result.status, "no_router_history")
        self.assertEqual(result.counts["eligible"], 0)
        self.assertEqual(result.history_path.read_text(encoding="utf-8"), "")
        self.assertEqual(result.counts["unresolved"], 1)

    def test_exact_finalized_settlement_creates_strict_history_with_provenance_and_chronology(self) -> None:
        row = _collector_row()
        result = self.run_pipeline([row], [_strict_resolution(row["market_id"])])

        [history] = [json.loads(line) for line in result.history_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(result.status, "history_ready")
        self.assertEqual(result.counts["eligible"], 1)
        self.assertTrue(history["eligible_for_source_history"])
        self.assertEqual(history["settlement_ts"], "2026-08-04T00:00:00Z")
        self.assertEqual(history["known_after"], history["settlement_ts"])
        self.assertIn("source_record_sha256", history["source_provenance"])
        self.assertIn("strict_resolution_source_sha256", history["resolution_provenance"])
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["runtime_consumption"]["history_ledger_path"], str(result.history_path))
        self.assertEqual(manifest["chronology"]["availability_field"], "settlement_ts")

    def test_generated_history_is_accepted_by_the_existing_router_history_loader(self) -> None:
        row = _collector_row()
        result = self.run_pipeline([row], [_strict_resolution(row["market_id"])])

        loaded, stats = _load_router_ledger_rows(
            [], history_ledger_paths=[result.history_path], source_paths=[], decision_paths=[],
        )

        self.assertEqual(stats["history_ledger_rows"], 1)
        self.assertTrue(loaded[0]["source_router_history_only"])
        self.assertEqual(loaded[0]["settlement_ts"], loaded[0]["known_after"])

    def test_generation_is_accepted_by_verified_collector_consumer_with_exact_manifest_ledger(self) -> None:
        row = _collector_row()
        result = self.run_pipeline([row], [_strict_resolution(row["market_id"])])
        promotion = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        replay_output = Path(tempfile.mkdtemp(prefix="test_source_router_consumer_", dir=self.derived_root))
        self.addCleanup(shutil.rmtree, replay_output, True)

        consumed = run_collector_source_router_replay(
            replay_inputs_path=promotion["artifacts"]["replay_inputs"],
            finalized_outcomes_path=promotion["artifacts"]["finalized_outcomes"],
            output_dir=replay_output,
            min_sample_count=1,
            history_manifest_path=result.history_manifest_path,
            history_ledger_path=result.history_path,
        )

        provenance = consumed.metadata["inputs"]["selector_history"]
        self.assertEqual(provenance["verification"], "manifest_sha256_verified")
        self.assertEqual(provenance["history_ledger_path"], str(result.history_path))
        self.assertEqual(provenance["history_manifest_source_ledger_sha256"], promotion["source_history_manifest"]["sha256"]["source_ledger"])

    def test_promotion_binds_provenance_to_materialized_input_bytes_when_source_changes(self) -> None:
        row = _collector_row()
        _write_jsonl(self.archive, [row])
        _write_jsonl(self.resolutions, [_strict_resolution(row["market_id"])])
        from bot.auto_source_router_promotion import export_collector_replay_inputs as real_export

        def append_before_export(**kwargs):
            with self.archive.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(_collector_row(market_id="KXHIGHSEA-26AUG04-T71"), sort_keys=True) + "\n")
            return real_export(**kwargs)

        with patch("bot.auto_source_router_promotion.export_collector_replay_inputs", side_effect=append_before_export):
            result = auto_populate_source_router_history(
                collector_snapshots_path=self.archive,
                strict_resolutions_path=self.resolutions,
                output_root=self.output_root,
            )

        history_manifest = json.loads(result.history_manifest_path.read_text(encoding="utf-8"))
        materialized_archive = Path(history_manifest["raw_archive_path"])
        self.assertNotEqual(materialized_archive.read_bytes(), self.archive.read_bytes())
        self.assertEqual(history_manifest["sha256"]["raw_archive"], hashlib.sha256(materialized_archive.read_bytes()).hexdigest())
        self.assertEqual(history_manifest["sha256"]["raw_archive"], json.loads(result.manifest_path.read_text())["input_sha256"]["collector_snapshots"])

    def test_failed_partial_generation_can_be_retried_without_publishing_incomplete_history(self) -> None:
        row = _collector_row()
        _write_jsonl(self.archive, [row])
        _write_jsonl(self.resolutions, [_strict_resolution(row["market_id"])])
        with patch("bot.auto_source_router_promotion.materialize_strict_source_history_collapse", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                auto_populate_source_router_history(
                    collector_snapshots_path=self.archive,
                    strict_resolutions_path=self.resolutions,
                    output_root=self.output_root,
                )

        retried = auto_populate_source_router_history(
            collector_snapshots_path=self.archive,
            strict_resolutions_path=self.resolutions,
            output_root=self.output_root,
        )

        self.assertFalse(retried.reused)
        self.assertTrue(retried.history_manifest_path.is_file())
        self.assertTrue(retried.history_path.is_file())

    def test_cli_reports_required_promotion_counts(self) -> None:
        row = _collector_row()
        _write_jsonl(self.archive, [row])
        _write_jsonl(self.resolutions, [_strict_resolution(row["market_id"])])
        completed = subprocess.run(
            [
                sys.executable, "scripts/auto_populate_source_router_history.py",
                "--collector-snapshots", str(self.archive),
                "--strict-resolutions", str(self.resolutions),
            ],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )

        self.assertIn("status=history_ready", completed.stdout)
        for name in ("pending=", "unresolved=", "invalid=", "eligible=", "collapsed="):
            self.assertIn(name, completed.stdout)

    def test_repeat_invocation_reuses_the_same_generation_without_rewriting_history(self) -> None:
        row = _collector_row()
        first = self.run_pipeline([row], [_strict_resolution(row["market_id"])])
        before = first.history_path.read_bytes()
        second = self.run_pipeline([row], [_strict_resolution(row["market_id"])])

        self.assertTrue(second.reused)
        self.assertEqual(first.generation_dir, second.generation_dir)
        self.assertEqual(before, second.history_path.read_bytes())

    def test_current_scoreboard_handoff_only_moves_after_successful_immutable_generation_publish(self) -> None:
        first_rows = [_collector_row(market_id=f"KXHIGHSEA-26AUG{index:02}-T70") for index in range(1, 101)]
        first = self.run_pipeline(first_rows, [_strict_resolution(row["market_id"]) for row in first_rows])
        current = self.output_root / "current"
        current_scoreboard = current / "source_router_scoreboard" / "strict_finalized_source_scoreboard.jsonl"
        first_scoreboard_bytes = first.scoreboard_path.read_bytes()

        self.assertTrue(current.is_symlink())
        self.assertEqual(current_scoreboard.resolve(), first.scoreboard_path)
        self.assertEqual(current_scoreboard.read_bytes(), first_scoreboard_bytes)
        first_decision = _source_router_decision(
            _LaneDefinition("shadow_source_router", lane_type="source_router", parameters={"scoreboard_path": str(current_scoreboard)}),
            {
                "market_id": "KXHIGHSEA-26AUG03-T70", "observed_at": "2026-08-05T00:00:00+00:00", "question": "Will Seattle high temperature be above 70°?",
                "city_id": "seattle_wa", "market_kind": "high", "contract_shape": "tail", "question_side": "above",
                "threshold": 70.0, "source_details": [{"source_id": "nws", "source_name": "NWS", "forecast_high": 75.0}],
            },
            None,
            {"market_id": "KXHIGHSEA-26AUG03-T70", "observed_at": "2026-08-05T00:00:00+00:00", "market": {"id": "KXHIGHSEA-26AUG03-T70", "question": "Will Seattle high temperature be above 70°?"}},
        )
        self.assertEqual(first_decision["action"], "BUY_YES")
        self.assertEqual(first_decision["source_router"]["scoreboard_path"], str(current_scoreboard))

        second_rows = [_collector_row(market_id=f"KXHIGHSEA-26SEP{index:02}-T70") for index in range(1, 101)]
        with patch("bot.auto_source_router_promotion.materialize_strict_source_history_collapse", side_effect=RuntimeError("interrupted second promotion")):
            with self.assertRaisesRegex(RuntimeError, "interrupted second promotion"):
                self.run_pipeline(second_rows, [_strict_resolution(row["market_id"]) for row in second_rows])

        self.assertEqual(current_scoreboard.resolve(), first.scoreboard_path)
        self.assertEqual(current_scoreboard.read_bytes(), first_scoreboard_bytes)
        self.assertEqual(first.scoreboard_path.read_bytes(), first_scoreboard_bytes)

        second = self.run_pipeline(second_rows, [_strict_resolution(row["market_id"]) for row in second_rows])

        self.assertNotEqual(first.generation_dir, second.generation_dir)
        self.assertEqual(current_scoreboard.resolve(), second.scoreboard_path)
        self.assertEqual(first.scoreboard_path.read_bytes(), first_scoreboard_bytes)
        self.assertTrue(first.manifest_path.is_file())
        self.assertTrue(second.manifest_path.is_file())

    def test_actual_paper_router_fails_closed_when_current_strict_scorecard_is_replaced_after_publish(self) -> None:
        rows = [_collector_row(market_id=f"KXHIGHSEA-26AUG{index:02}-T70") for index in range(1, 101)]
        result = self.run_pipeline(rows, [_strict_resolution(row["market_id"]) for row in rows])
        current_scoreboard = self.output_root / "current" / "source_router_scoreboard" / "strict_finalized_source_scoreboard.jsonl"
        # This remains a valid legacy scoreboard and would previously drive BUY_YES,
        # despite replacing the published strict artifact after its manifest hash.
        _write_jsonl(current_scoreboard, [{
            "source_id": "nws", "source_name": "NWS", "city_id": "seattle_wa",
            "market_kind": "high", "contract_shape": "tail", "sample_count": 100,
            "threshold_sample_count": 100, "threshold_correct_count": 100,
            "threshold_direction_accuracy": 1.0,
        }])

        decision = _source_router_decision(
            _LaneDefinition("shadow_source_router", lane_type="source_router", parameters={"scoreboard_path": str(current_scoreboard)}),
            {
                "market_id": "KXHIGHSEA-26AUG03-T70", "observed_at": "2026-08-05T00:00:00+00:00",
                "question": "Will Seattle high temperature be above 70°?", "city_id": "seattle_wa",
                "market_kind": "high", "contract_shape": "tail", "question_side": "above", "threshold": 70.0,
                "source_details": [{"source_id": "nws", "source_name": "NWS", "forecast_high": 75.0}],
            },
            None,
            {"market_id": "KXHIGHSEA-26AUG03-T70", "observed_at": "2026-08-05T00:00:00+00:00", "market": {"id": "KXHIGHSEA-26AUG03-T70", "question": "Will Seattle high temperature be above 70°?"}},
        )

        self.assertEqual(decision["action"], "SKIP")
        self.assertFalse(decision["source_router"]["available"])
        self.assertEqual(decision["source_router"]["reason_code"], "strict_scorecard_verification_failed")
        self.assertEqual(result.scoreboard_path, current_scoreboard.resolve())

    def test_actual_paper_router_fails_closed_for_external_current_target(self) -> None:
        rows = [_collector_row(market_id=f"KXHIGHSEA-26AUG{index:02}-T70") for index in range(1, 101)]
        self.run_pipeline(rows, [_strict_resolution(row["market_id"]) for row in rows])
        current = self.output_root / "current"
        current.unlink()
        current.symlink_to(self.root)
        scoreboard = current / "source_router_scoreboard" / "strict_finalized_source_scoreboard.jsonl"

        decision = _source_router_decision(
            _LaneDefinition("shadow_source_router", lane_type="source_router", parameters={"scoreboard_path": str(scoreboard)}),
            {"observed_at": "2026-08-05T00:00:00+00:00", "question": "Will Seattle high temperature be above 70°?", "city_id": "seattle_wa", "market_kind": "high", "contract_shape": "tail", "question_side": "above", "threshold": 70.0, "source_details": [{"source_id": "nws", "source_name": "NWS", "forecast_high": 75.0}]},
            None, {"observed_at": "2026-08-05T00:00:00+00:00", "market": {"question": "Will Seattle high temperature be above 70°?"}},
        )

        self.assertEqual(decision["action"], "SKIP")
        self.assertFalse(decision["source_router"]["available"])

    def test_actual_paper_router_fails_closed_for_invalid_verified_strict_scorecard_json(self) -> None:
        rows = [_collector_row(market_id=f"KXHIGHSEA-26AUG{index:02}-T70") for index in range(1, 101)]
        result = self.run_pipeline(rows, [_strict_resolution(row["market_id"]) for row in rows])
        malformed = b"{not json}\n"
        result.scoreboard_path.write_bytes(malformed)
        # Even a malformed scorecard whose mutable manifest hash is rewritten
        # cannot be parsed or used by the strict runtime consumer.
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        manifest["artifact_sha256"]["scoreboard"] = hashlib.sha256(malformed).hexdigest()
        result.manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")

        decision = _source_router_decision(
            _LaneDefinition("shadow_source_router", lane_type="source_router", parameters={"scoreboard_path": str(result.scoreboard_path)}),
            {"observed_at": "2026-08-05T00:00:00+00:00", "question": "Will Seattle high temperature be above 70°?", "city_id": "seattle_wa", "market_kind": "high", "contract_shape": "tail", "question_side": "above", "threshold": 70.0, "source_details": [{"source_id": "nws", "source_name": "NWS", "forecast_high": 75.0}]},
            None, {"observed_at": "2026-08-05T00:00:00+00:00", "market": {"question": "Will Seattle high temperature be above 70°?"}},
        )

        self.assertEqual(decision["action"], "SKIP")
        self.assertFalse(decision["source_router"]["available"])

    def test_published_strict_scorecard_drives_actual_paper_source_router(self) -> None:
        rows = [_collector_row(market_id=f"KXHIGHSEA-26AUG{index:02}-T70") for index in range(1, 101)]
        result = self.run_pipeline(rows, [_strict_resolution(row["market_id"]) for row in rows])

        decision = _source_router_decision(
            _LaneDefinition("shadow_source_router", lane_type="source_router", parameters={"scoreboard_path": str(result.scoreboard_path)}),
            {
                "market_id": "KXHIGHSEA-26AUG03-T70", "observed_at": "2026-08-05T00:00:00+00:00",
                "question": "Will Seattle high temperature be above 70°?",
                "city_id": "seattle_wa",
                "market_kind": "high",
                "contract_shape": "tail",
                "question_side": "above",
                "threshold": 70.0,
                "source_details": [{"source_id": "nws", "source_name": "NWS", "forecast_high": 75.0}],
            },
            None,
            {"market_id": "KXHIGHSEA-26AUG03-T70", "observed_at": "2026-08-05T00:00:00+00:00", "market": {"id": "KXHIGHSEA-26AUG03-T70", "question": "Will Seattle high temperature be above 70°?"}},
        )

        [scorecard] = [json.loads(line) for line in result.scoreboard_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(scorecard["source_id"], "nws")
        self.assertEqual(scorecard["sample_count"], 100)
        self.assertEqual(scorecard["threshold_direction_accuracy"], 1.0)
        self.assertEqual(decision["action"], "BUY_YES")
        self.assertEqual(decision["source_router"]["scoreboard_path"], str(result.scoreboard_path))

    def test_actual_paper_router_excludes_scorecard_settled_at_or_after_candidate_time(self) -> None:
        rows = [_collector_row(market_id=f"KXHIGHSEA-26AUG{index:02}-T70") for index in range(1, 101)]
        result = self.run_pipeline(rows, [_strict_resolution(row["market_id"]) for row in rows])

        decision = _source_router_decision(
            _LaneDefinition("shadow_source_router", lane_type="source_router", parameters={"scoreboard_path": str(result.scoreboard_path)}),
            {
                "market_id": "KXHIGHSEA-26AUG03-T70", "observed_at": "2026-08-02T00:00:00+00:00",
                "question": "Will Seattle high temperature be above 70°?", "city_id": "seattle_wa",
                "market_kind": "high", "contract_shape": "tail", "question_side": "above", "threshold": 70.0,
                "source_details": [{"source_id": "nws", "source_name": "NWS", "forecast_high": 75.0}],
            },
            None,
            {"market_id": "KXHIGHSEA-26AUG03-T70", "observed_at": "2026-08-02T00:00:00+00:00", "market": {"id": "KXHIGHSEA-26AUG03-T70", "question": "Will Seattle high temperature be above 70°?"}},
        )

        [scorecard] = [json.loads(line) for line in result.scoreboard_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(scorecard["sample_count"], 100)
        self.assertEqual(len(scorecard["provenance"]["settled_observations"]), 100)
        self.assertEqual(decision["action"], "SKIP")
        self.assertEqual(decision["reason_code"], "no_usable_reliability_after_backoff")

    def test_promotion_excludes_source_evidence_claimed_after_immutable_observation(self) -> None:
        row = _collector_row()
        row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]["sources"][0]["source_as_of"] = "2026-08-01T12:01:00+00:00"

        result = self.run_pipeline([row], [_strict_resolution(row["market_id"])])

        self.assertEqual(result.status, "no_router_history")
        self.assertEqual(result.counts["eligible"], 0)

    def test_reuse_rejects_tampered_source_history_manifest(self) -> None:
        row = _collector_row()
        first = self.run_pipeline([row], [_strict_resolution(row["market_id"])])
        first.history_manifest_path.write_text("{}\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "source history manifest"):
            self.run_pipeline([row], [_strict_resolution(row["market_id"])])

    def test_reuse_rejects_tampered_scorecard(self) -> None:
        row = _collector_row()
        first = self.run_pipeline([row], [_strict_resolution(row["market_id"])])
        first.scoreboard_path.write_text("{}\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "artifact hash mismatch"):
            self.run_pipeline([row], [_strict_resolution(row["market_id"])])

    def test_reuse_rejects_manifest_artifact_path_outside_generation(self) -> None:
        row = _collector_row()
        first = self.run_pipeline([row], [_strict_resolution(row["market_id"])])
        manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["scoreboard"] = str(self.root / "external-scoreboard.jsonl")
        first.manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "outside generation"):
            self.run_pipeline([row], [_strict_resolution(row["market_id"])])

    def test_reuse_rejects_current_link_outside_a_complete_generation(self) -> None:
        row = _collector_row()
        self.run_pipeline([row], [_strict_resolution(row["market_id"])])
        current = self.output_root / "current"
        current.unlink()
        current.symlink_to(self.root)

        with self.assertRaisesRegex(ValueError, "current.*complete generation"):
            self.run_pipeline([row], [_strict_resolution(row["market_id"])])

    def test_published_helper_metadata_never_retains_random_staging_paths(self) -> None:
        row = _collector_row()
        result = self.run_pipeline([row], [_strict_resolution(row["market_id"])])

        metadata_paths = list(result.generation_dir.rglob("*.json"))
        metadata_paths.extend(result.generation_dir.rglob("*.metadata.json"))
        for path in metadata_paths:
            self.assertNotIn("/.staging/", path.read_text(encoding="utf-8"), path)

    def test_malformed_ambiguous_and_unproven_evidence_stays_out_of_router_history(self) -> None:
        unproven = _collector_row(strict=False)
        ambiguous = _strict_resolution(unproven["market_id"])
        result = self.run_pipeline([unproven], [ambiguous, dict(ambiguous, resolution_id="conflict")])

        self.assertEqual(result.status, "no_router_history")
        self.assertEqual(result.counts["eligible"], 0)
        self.assertGreater(result.counts["unresolved"], 0)
        self.assertEqual(result.history_path.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
