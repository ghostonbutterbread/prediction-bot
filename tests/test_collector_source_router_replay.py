import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bot.weather.collector_source_router_replay import (
    build_sealed_source_probability_decisions,
    resolve_sealed_source_probability_decisions,
    run_collector_source_router_replay,
)
from bot.weather.source_history_manifest import SourceHistoryManifestError


ROOT = Path(__file__).resolve().parents[1]
DERIVED_ROOT = ROOT / "data" / "derived_reports"


def replay_input(
    *,
    market_id: str,
    observed_at: str,
    raw_hash: str,
    predicted_prob: float = 0.8,
    sources: list[dict] | None = None,
    event_ticker: str | None = None,
    contract_shape: str = "range",
    subcategory: str | None = None,
) -> dict:
    sources = sources or [{"source_id": "nws", "source_name": "NWS", "forecast_high": 82.0}]
    decision_key = {
        "shared_snapshot_id": f"snapshot-{market_id}",
        "shared_candidate_id": f"candidate-{market_id}",
        "market_id": market_id,
        "observed_at_utc": observed_at,
        "raw_row_sha256": raw_hash,
    }
    record = {
        "schema_name": "replay_decision_input",
        "schema_version": 1,
        "decision_key": decision_key,
        "shared_snapshot_id": decision_key["shared_snapshot_id"],
        "shared_candidate_id": decision_key["shared_candidate_id"],
        "market_id": market_id,
        "observed_at": observed_at,
        "market": {
            "question": "Will Miami high exceed 80F?",
            "market_metadata": {
                "city_id": "miami_fl", "market_kind": "high", "contract_shape": contract_shape,
                "event_ticker": event_ticker,
                "market_route": {"subcategory": subcategory} if subcategory else {},
            },
            "best_yes_ask": 0.40,
            "best_no_ask": 0.60,
        },
        "source_inputs": {
            "source_context": {
                "source": "weather",
                "data": {
                    "weather_source_snapshot": {
                        "predicted_prob": predicted_prob,
                        "forecast": {"threshold": 80.0, "question_side": "above"},
                        "sources": sources,
                    },
                },
            },
        },
    }
    record["canonical_input_sha256"] = hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return record


def finalized_outcome(record: dict, *, outcome: str, settlement_ts: str) -> dict:
    return {
        "schema_name": "finalized_source_probability_outcome",
        "schema_version": 1,
        "market_status": "finalized",
        "market_id": record["market_id"],
        "decision_key": record["decision_key"],
        "canonical_input_sha256": record["canonical_input_sha256"],
        "official_outcome": outcome,
        "settlement_ts": settlement_ts,
        "resolution_id": f"resolution-{record['market_id']}",
    }


def source_history_row(*, market_id: str, settlement_ts: str, outcome: str = "YES") -> dict:
    return {
        "market_id": market_id,
        "source_id": "nws",
        "source_name": "NWS",
        "city_id": "miami_fl",
        "market_kind": "high",
        "contract_shape": "tail",
        "question_side": "above",
        "forecast_temp_f": 82.0,
        "threshold": 80.0,
        "predicted_outcome": "YES",
        "actual_outcome": outcome,
        "yes_price": 0.40,
        "no_price": 0.60,
        "eligible_for_reliability": True,
        "source_correctness_eligibility": "eligible_strict_source_proof",
        "eligible_for_source_history": True,
        "strict_source_proof": {"status": "eligible", "reasons": []},
        "source_provenance": {"source_record_sha256": "a" * 64, "canonical_input_sha256": "b" * 64},
        "settlement_ts": settlement_ts,
    }


class CollectorSourceRouterReplayTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.output_dir = Path(tempfile.mkdtemp(prefix="test_source_probability_", dir=DERIVED_ROOT))

    def tearDown(self):
        shutil.rmtree(self.output_dir, ignore_errors=True)
        self.tempdir.cleanup()

    def _records_and_outcomes(self):
        old = replay_input(
            market_id="KXOLD", observed_at="2026-01-01T12:00:00+00:00", raw_hash="a" * 64,
        )
        future = replay_input(
            market_id="KXFUTURE", observed_at="2026-01-02T12:00:00+00:00", raw_hash="b" * 64,
            sources=[{"source_id": "open_meteo", "source_name": "Open Meteo", "forecast_high": 78.0}],
        )
        target = replay_input(
            market_id="KXTARGET", observed_at="2026-01-03T12:00:00+00:00", raw_hash="c" * 64,
            sources=[
                {"source_id": "nws", "source_name": "NWS", "forecast_high": 82.0},
                {"source_id": "open_meteo", "source_name": "Open Meteo", "forecast_high": 78.0},
            ],
        )
        outcomes = [
            finalized_outcome(old, outcome="YES", settlement_ts="2026-01-02T00:00:00+00:00"),
            finalized_outcome(future, outcome="NO", settlement_ts="2026-01-04T00:00:00+00:00"),
            finalized_outcome(target, outcome="YES", settlement_ts="2026-01-05T00:00:00+00:00"),
        ]
        return [old, future, target], outcomes

    def test_control_and_candidate_share_same_sanitized_input_and_have_no_outcomes(self):
        records, outcomes = self._records_and_outcomes()
        control, candidate, _ = build_sealed_source_probability_decisions(records, outcomes, min_sample_count=1)

        self.assertEqual(len(control), len(candidate))
        for control_row, candidate_row in zip(control, candidate):
            self.assertEqual(control_row["canonical_input_sha256"], candidate_row["canonical_input_sha256"])
            self.assertEqual(control_row["decision_key"], candidate_row["decision_key"])
            self.assertEqual(control_row["lane_id"], "source_probability_control_v1")
            self.assertEqual(control_row["research_status"], "offline_source_only_research")
            self.assertEqual(candidate_row["research_status"], "offline_source_only_research")
            self.assertNotIn("official_outcome", json.dumps(control_row))
            self.assertNotIn("official_outcome", json.dumps(candidate_row))
            self.assertNotIn("settlement_ts", json.dumps(control_row))
            self.assertNotIn("settlement_ts", json.dumps(candidate_row))

    def test_sealed_decisions_carry_safe_market_traits_and_explicit_event_unit_without_outcomes(self):
        record = replay_input(
            market_id="KXTRAITS", observed_at="2026-01-03T12:00:00+00:00", raw_hash="7" * 64,
            event_ticker="KXHIGHMIA-26AUG03", contract_shape="tail", subcategory="tail_high",
        )
        _, candidates, _ = build_sealed_source_probability_decisions(
            [record], [], history_ledger=[source_history_row(market_id="HIST", settlement_ts="2026-01-02T00:00:00+00:00")],
            min_sample_count=1,
        )

        candidate = candidates[0]
        self.assertEqual(candidate["event_ticker"], "KXHIGHMIA-26AUG03")
        self.assertEqual(candidate["unit_id"], "KXHIGHMIA-26AUG03")
        self.assertEqual(candidate["independence_quality"], "event_ticker")
        self.assertEqual(candidate["city_id"], "miami_fl")
        self.assertEqual(candidate["market_kind"], "high")
        self.assertEqual(candidate["contract_shape"], "tail")
        self.assertEqual(candidate["subcategory"], "tail_high")
        self.assertEqual(candidate["question_side"], "above")
        self.assertNotIn("official_outcome", json.dumps(candidate))
        self.assertNotIn("settlement_ts", json.dumps(candidate))

    def test_candidate_uses_only_strictly_earlier_settlement_history_and_target_outcome_cannot_change_it(self):
        records, outcomes = self._records_and_outcomes()
        history = [source_history_row(market_id="HIST-1", settlement_ts="2026-01-02T00:00:00+00:00")]
        _, candidate, _ = build_sealed_source_probability_decisions(
            records, outcomes, history_ledger=history, min_sample_count=1,
        )
        target = next(row for row in candidate if row["market_id"] == "KXTARGET")
        self.assertEqual(target["selected_source_id"], "nws")
        self.assertEqual(target["prior_sample_count"], 1)
        self.assertEqual(target["action"], "BUY_YES")

        changed = [dict(row) for row in outcomes]
        changed[-1] = {**changed[-1], "official_outcome": "NO"}
        _, altered_candidate, _ = build_sealed_source_probability_decisions(
            records, changed, history_ledger=history, min_sample_count=1,
        )
        altered_target = next(row for row in altered_candidate if row["market_id"] == "KXTARGET")
        self.assertEqual(target, altered_target)

    def test_candidate_skips_when_no_prior_resolved_source_history_exists(self):
        record = replay_input(market_id="KXONLY", observed_at="2026-01-01T12:00:00+00:00", raw_hash="d" * 64)
        _, candidate, _ = build_sealed_source_probability_decisions([record], [], min_sample_count=1)

        self.assertEqual(candidate[0]["action"], "SKIP")
        self.assertEqual(candidate[0]["skip_reason"], "insufficient_prior_history")

    def test_verified_history_ledger_makes_candidate_routeable_when_current_cohort_does_not(self):
        record = replay_input(market_id="KXHISTORY", observed_at="2026-01-03T12:00:00+00:00", raw_hash="9" * 64)
        without_history = build_sealed_source_probability_decisions([record], [], min_sample_count=5)[1][0]
        history = [
            source_history_row(market_id=f"HIST-{index}", settlement_ts="2026-01-02T00:00:00+00:00")
            for index in range(5)
        ]
        with_history = build_sealed_source_probability_decisions(
            [record], [], history_ledger=history, min_sample_count=5,
        )[1][0]

        self.assertEqual(without_history["action"], "SKIP")
        self.assertEqual(without_history["skip_reason"], "insufficient_prior_history")
        self.assertEqual(with_history["selected_source_id"], "nws")
        self.assertEqual(with_history["prior_sample_count"], 5)
        self.assertEqual(with_history["action"], "BUY_YES")

    def test_history_settlement_at_or_after_decision_is_excluded(self):
        record = replay_input(market_id="KXCUTOFF", observed_at="2026-01-03T12:00:00+00:00", raw_hash="8" * 64)
        history = [
            source_history_row(market_id=f"HIST-{index}", settlement_ts="2026-01-03T12:00:00+00:00")
            for index in range(5)
        ]
        candidate = build_sealed_source_probability_decisions(
            [record], [], history_ledger=history, min_sample_count=5,
        )[1][0]

        self.assertEqual(candidate["action"], "SKIP")
        self.assertEqual(candidate["skip_reason"], "insufficient_prior_history")

    def test_history_without_exact_target_proof_cannot_meet_minimum_samples(self):
        record = replay_input(market_id="KXTARGETPROOF", observed_at="2026-01-03T12:00:00+00:00", raw_hash="1" * 64)
        mismatch = source_history_row(market_id="HIST-MISMATCH", settlement_ts="2026-01-02T00:00:00+00:00")
        mismatch["source_correctness_eligibility"] = "unusable_legacy_target_mismatch"
        unproven = source_history_row(market_id="HIST-UNPROVEN", settlement_ts="2026-01-02T00:00:00+00:00")
        unproven["source_correctness_eligibility"] = "unusable_legacy_target_unproven"
        missing_marker = source_history_row(market_id="HIST-MISSING", settlement_ts="2026-01-02T00:00:00+00:00")
        missing_marker.pop("source_correctness_eligibility")

        _, candidates, stats = build_sealed_source_probability_decisions(
            [record], [], history_ledger=[mismatch, unproven, missing_marker], min_sample_count=1,
        )

        self.assertEqual(candidates[0]["action"], "SKIP")
        self.assertEqual(candidates[0]["skip_reason"], "insufficient_prior_history")
        history = stats["selector_history"]
        self.assertEqual(history["source_history_rows_rejected_target_mismatch"], 1)
        self.assertEqual(history["source_history_rows_rejected_target_unproven"], 1)
        self.assertEqual(history["source_history_rows_rejected_missing_exact_target_proof_marker"], 1)
        self.assertEqual(history["source_quality_interpretation"], "quarantined_rows_without_exact_target_proof")

    def test_v1_target_mismatch_is_reported_as_target_mismatch(self):
        record = replay_input(market_id="KXV1MISMATCH", observed_at="2026-01-03T12:00:00+00:00", raw_hash="3" * 64)
        v1_mismatch = source_history_row(market_id="HIST-V1-MISMATCH", settlement_ts="2026-01-02T00:00:00+00:00")
        v1_mismatch["source_correctness_eligibility"] = "unusable_v1_target_mismatch"
        legacy_mismatch = source_history_row(market_id="HIST-LEGACY-MISMATCH", settlement_ts="2026-01-02T00:00:00+00:00")
        legacy_mismatch["source_correctness_eligibility"] = "unusable_legacy_target_mismatch"
        v1_unproven = source_history_row(market_id="HIST-V1-UNPROVEN", settlement_ts="2026-01-02T00:00:00+00:00")
        v1_unproven["source_correctness_eligibility"] = "unusable_v1_target_unproven"
        v1_not_scoreable = source_history_row(market_id="HIST-V1-NOT-SCOREABLE", settlement_ts="2026-01-02T00:00:00+00:00")
        v1_not_scoreable["source_correctness_eligibility"] = "unusable_v1_forecast_not_scoreable"

        _, candidates, stats = build_sealed_source_probability_decisions(
            [record], [], history_ledger=[v1_mismatch, legacy_mismatch, v1_unproven, v1_not_scoreable], min_sample_count=1,
        )

        self.assertEqual(candidates[0]["action"], "SKIP")
        history = stats["selector_history"]
        self.assertEqual(history["source_history_rows_rejected_target_mismatch"], 2)
        self.assertEqual(history["source_history_rows_rejected_target_unproven"], 1)
        self.assertEqual(history["source_history_rows_rejected_v1_forecast_not_scoreable"], 1)

    def test_direct_history_ledger_requires_a_verified_manifest(self):
        record = replay_input(market_id="KXQUARANTINE", observed_at="2026-01-03T12:00:00+00:00", raw_hash="2" * 64)
        inputs_path = Path(self.tempdir.name) / "replay_inputs.jsonl"
        outcomes_path = Path(self.tempdir.name) / "outcomes.jsonl"
        history_path = Path(self.tempdir.name) / "legacy_history.jsonl"
        inputs_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        outcomes_path.write_text("", encoding="utf-8")
        history = source_history_row(market_id="HIST-LEGACY", settlement_ts="2026-01-02T00:00:00+00:00")
        history.pop("source_correctness_eligibility")
        history_path.write_text(json.dumps(history) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "history manifest"):
            run_collector_source_router_replay(
                replay_inputs_path=inputs_path, finalized_outcomes_path=outcomes_path,
                output_dir=self.output_dir, min_sample_count=1, history_ledger_path=history_path,
            )

    def test_direct_history_ledger_is_rejected_without_a_verified_manifest(self):
        record = replay_input(market_id="KXDIRECT", observed_at="2026-01-03T12:00:00+00:00", raw_hash="4" * 64)
        inputs_path = Path(self.tempdir.name) / "replay_inputs.jsonl"
        outcomes_path = Path(self.tempdir.name) / "outcomes.jsonl"
        history_path = Path(self.tempdir.name) / "history.jsonl"
        inputs_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        outcomes_path.write_text("", encoding="utf-8")
        history_path.write_text(json.dumps(source_history_row(market_id="HIST", settlement_ts="2026-01-02T00:00:00+00:00")) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "history manifest"):
            run_collector_source_router_replay(
                replay_inputs_path=inputs_path, finalized_outcomes_path=outcomes_path,
                output_dir=self.output_dir, history_ledger_path=history_path,
            )

    def test_replay_rejects_a_collector_input_mutated_after_its_hash_was_recorded(self):
        record = replay_input(market_id="KXEDIT", observed_at="2026-01-03T12:00:00+00:00", raw_hash="5" * 64)
        record["market"]["best_yes_ask"] = 0.99
        inputs_path = Path(self.tempdir.name) / "replay_inputs.jsonl"
        outcomes_path = Path(self.tempdir.name) / "outcomes.jsonl"
        inputs_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        outcomes_path.write_text("", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "canonical hash"):
            run_collector_source_router_replay(
                replay_inputs_path=inputs_path, finalized_outcomes_path=outcomes_path, output_dir=self.output_dir,
            )

    def test_manifest_sha_validation_failure_blocks_run_before_writing_artifacts(self):
        record = replay_input(market_id="KXMANIFEST", observed_at="2026-01-03T12:00:00+00:00", raw_hash="7" * 64)
        inputs_path = Path(self.tempdir.name) / "replay_decision_inputs.jsonl"
        outcomes_path = Path(self.tempdir.name) / "finalized_outcomes.jsonl"
        inputs_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        outcomes_path.write_text("", encoding="utf-8")
        history_paths = {}
        for key, filename in {
            "source_ledger": "source_ledger.jsonl",
            "strict_resolution": "strict_resolutions.jsonl",
            "raw_archive": "market_snapshots.jsonl",
            "replay_index": "collector_replay_index.jsonl",
            "replay_manifest": "collector_replay_index.manifest.json",
        }.items():
            path = Path(self.tempdir.name) / filename
            path.write_text("{}\n", encoding="utf-8")
            history_paths[key] = path
        manifest_path = Path(self.tempdir.name) / "source_history_manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_name": "source_history_manifest",
            "schema_version": 1,
            "historical_counterfactual_only": True,
            "non_mutating": True,
            "join_key": "market_id",
            "eligibility_filter": "eligible_for_reliability == true",
            "availability_field": "settlement_ts",
            **{f"{key}_path": str(path) for key, path in history_paths.items()},
            "sha256": {
                **{key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in history_paths.items()},
                "source_ledger": "0" * 64,
            },
        }), encoding="utf-8")
        output_dir = DERIVED_ROOT / "test_source_probability_bad_manifest"
        self.addCleanup(shutil.rmtree, output_dir, True)

        with self.assertRaises(SourceHistoryManifestError):
            run_collector_source_router_replay(
                replay_inputs_path=inputs_path, finalized_outcomes_path=outcomes_path,
                output_dir=output_dir, history_manifest_path=manifest_path,
            )
        self.assertFalse(output_dir.exists())

    def test_manifest_history_uses_strict_resolution_settlement_by_exact_market_id(self):
        record = replay_input(market_id="KXMANIFESTOK", observed_at="2026-01-03T12:00:00+00:00", raw_hash="6" * 64)
        inputs_path = Path(self.tempdir.name) / "replay_decision_inputs.jsonl"
        outcomes_path = Path(self.tempdir.name) / "finalized_outcomes.jsonl"
        inputs_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        outcomes_path.write_text("", encoding="utf-8")
        ledger = source_history_row(market_id="HIST-MANIFEST", settlement_ts="")
        ledger.pop("settlement_ts")
        paths = {
            "source_ledger": Path(self.tempdir.name) / "source_ledger.jsonl",
            "strict_resolution": Path(self.tempdir.name) / "strict_resolutions.jsonl",
            "raw_archive": Path(self.tempdir.name) / "market_snapshots.jsonl",
            "replay_index": Path(self.tempdir.name) / "collector_replay_index.jsonl",
            "replay_manifest": Path(self.tempdir.name) / "collector_replay_index.manifest.json",
        }
        paths["source_ledger"].write_text(json.dumps(ledger) + "\n", encoding="utf-8")
        paths["strict_resolution"].write_text(json.dumps({
            "market_id": "HIST-MANIFEST", "market_status": "finalized", "kalshi_result": "yes",
            "settlement_ts": "2026-01-02T00:00:00+00:00", "resolution_id": "strict-history-1",
        }) + "\n", encoding="utf-8")
        for key in ("raw_archive", "replay_index", "replay_manifest"):
            paths[key].write_text("{}\n", encoding="utf-8")
        manifest_path = Path(self.tempdir.name) / "source_history_manifest.json"
        manifest_path.write_text(json.dumps({
            "schema_name": "source_history_manifest",
            "schema_version": 1,
            "historical_counterfactual_only": True,
            "non_mutating": True,
            "join_key": "market_id",
            "eligibility_filter": "eligible_for_reliability == true",
            "availability_field": "settlement_ts",
            **{f"{key}_path": str(path) for key, path in paths.items()},
            "sha256": {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()},
        }), encoding="utf-8")

        result = run_collector_source_router_replay(
            replay_inputs_path=inputs_path, finalized_outcomes_path=outcomes_path,
            output_dir=self.output_dir, min_sample_count=1, history_manifest_path=manifest_path,
        )
        candidate = json.loads(result.candidate_decisions_path.read_text().strip())

        self.assertEqual(candidate["selected_source_id"], "nws")
        self.assertEqual(candidate["action"], "BUY_YES")
        provenance = result.metadata["inputs"]["selector_history"]
        self.assertEqual(provenance["verification"], "manifest_sha256_verified")
        self.assertEqual(provenance["strict_resolution_join_counts"]["source_history_rows_with_exact_authoritative_resolution"], 1)

    def test_resolution_requires_exact_identity_join_and_keeps_pnl_separate(self):
        records, outcomes = self._records_and_outcomes()
        control, candidate, _ = build_sealed_source_probability_decisions(records, outcomes, min_sample_count=1)
        report = resolve_sealed_source_probability_decisions(control + candidate, outcomes)

        self.assertEqual(report["summary"]["exactly_resolved_decisions"], len(control) + len(candidate))
        self.assertTrue(all("official_outcome" in row for row in report["resolved_decisions"]))
        self.assertTrue(all("executable_pnl_usd" in row for row in report["resolved_decisions"]))
        self.assertTrue(all("reference_price_proxy_pnl_usd" in row for row in report["resolved_decisions"]))

        mismatched = [dict(row) for row in outcomes]
        mismatched[0] = {**mismatched[0], "canonical_input_sha256": "f" * 64}
        blocked = resolve_sealed_source_probability_decisions(control + candidate, mismatched)
        self.assertEqual(blocked["summary"]["exactly_resolved_decisions"], len(control) + len(candidate) - 2)
        self.assertEqual(blocked["summary"]["unresolved_decisions"], 2)

    def test_source_selection_correctness_counts_edge_skips_but_not_unroutable_candidates(self):
        edge_skipped = replay_input(
            market_id="KXEDGE_SKIP", observed_at="2026-01-03T12:00:00+00:00", raw_hash="2" * 64,
        )
        edge_skipped["market"]["best_yes_ask"] = 0.98
        unavailable = replay_input(
            market_id="KXUNAVAILABLE", observed_at="2026-01-03T12:00:00+00:00", raw_hash="3" * 64,
            sources=[{"source_id": "nws", "source_name": "NWS", "forecast_high": None}],
        )
        insufficient = replay_input(
            market_id="KXINSUFFICIENT", observed_at="2026-01-03T12:00:00+00:00", raw_hash="4" * 64,
        )
        insufficient["market"]["market_metadata"]["city_id"] = "orlando_fl"
        outcomes = [
            finalized_outcome(edge_skipped, outcome="YES", settlement_ts="2026-01-04T00:00:00+00:00"),
            finalized_outcome(unavailable, outcome="YES", settlement_ts="2026-01-04T00:00:00+00:00"),
            finalized_outcome(insufficient, outcome="YES", settlement_ts="2026-01-04T00:00:00+00:00"),
        ]
        _, candidates, _ = build_sealed_source_probability_decisions(
            [edge_skipped, unavailable, insufficient], outcomes,
            history_ledger=[source_history_row(market_id="HIST-1", settlement_ts="2026-01-02T00:00:00+00:00")],
            min_sample_count=1,
        )
        by_market = {row["market_id"]: row for row in candidates}

        self.assertEqual(by_market["KXEDGE_SKIP"]["selected_source_id"], "nws")
        self.assertEqual(by_market["KXEDGE_SKIP"]["side"], "YES")
        self.assertEqual(by_market["KXEDGE_SKIP"]["action"], "SKIP")
        self.assertEqual(by_market["KXEDGE_SKIP"]["skip_reason"], "below_source_probability_edge_threshold")
        self.assertEqual(by_market["KXUNAVAILABLE"]["selected_source_id"], "nws")
        self.assertIsNone(by_market["KXUNAVAILABLE"]["side"])
        self.assertEqual(by_market["KXINSUFFICIENT"]["skip_reason"], "insufficient_prior_history")
        self.assertIsNone(by_market["KXINSUFFICIENT"]["selected_source_id"])
        self.assertNotIn("official_outcome", json.dumps(candidates))
        self.assertNotIn("source_correctness", json.dumps(candidates))

        report = resolve_sealed_source_probability_decisions(candidates, outcomes)
        correctness = report["source_correctness"]
        self.assertEqual(correctness["diagnostic_type"], "source_selection_correctness")
        self.assertEqual(correctness["total_routeable_resolved_selected_source_observations"], 2)
        self.assertEqual(correctness["correct"], 1)
        self.assertEqual(correctness["incorrect"], 0)
        self.assertEqual(correctness["unavailable"], 1)
        self.assertEqual(correctness["correctness_rate"], 1.0)
        self.assertEqual(correctness["per_source"], [{
            "selected_source_id": "nws", "selected_source_name": "NWS",
            "total_routeable_resolved_selected_source_observations": 2,
            "correct": 1, "incorrect": 0, "unavailable": 1, "correctness_rate": 1.0,
        }])
        edge_observation = next(
            row for row in correctness["observations"] if row["market_id"] == "KXEDGE_SKIP"
        )
        self.assertEqual(edge_observation["action"], "SKIP")
        self.assertEqual(edge_observation["source_selection_correctness"], "correct")

    def test_price_basis_distinguishes_executable_asks_from_reference_price_proxies(self):
        reference = replay_input(
            market_id="KXREFERENCE", observed_at="2026-01-01T12:00:00+00:00", raw_hash="e" * 64,
        )
        reference["market"].pop("best_yes_ask")
        reference["market"].pop("best_no_ask")
        reference["market"]["execution_snapshot"] = {"best_yes_ask": 0, "best_no_ask": 0}
        reference["market"].update({"yes_price": 0.40, "no_price": 0.60})

        executable = replay_input(
            market_id="KXEXECUTABLE", observed_at="2026-01-02T12:00:00+00:00", raw_hash="f" * 64,
        )
        executable["market"]["execution_snapshot"] = {"best_yes_ask": 0.40, "best_no_ask": 0.60}
        executable["market"].update({"yes_price": 0.41, "no_price": 0.59})

        missing = replay_input(
            market_id="KXMISSING", observed_at="2026-01-03T12:00:00+00:00", raw_hash="1" * 64,
        )
        missing["market"].pop("best_yes_ask")
        missing["market"].pop("best_no_ask")
        missing["market"]["execution_snapshot"] = {"best_yes_ask": 0, "best_no_ask": 0}
        missing["market"].update({"yes_price": 0, "no_price": -0.2})

        records = [reference, executable, missing]
        outcomes = [
            finalized_outcome(reference, outcome="YES", settlement_ts="2026-01-04T00:00:00+00:00"),
            finalized_outcome(executable, outcome="YES", settlement_ts="2026-01-04T00:00:00+00:00"),
            finalized_outcome(missing, outcome="YES", settlement_ts="2026-01-04T00:00:00+00:00"),
        ]
        control, _, _ = build_sealed_source_probability_decisions(records, outcomes, min_sample_count=1)
        by_market = {row["market_id"]: row for row in control}

        self.assertEqual(by_market["KXREFERENCE"]["action"], "BUY_YES")
        self.assertEqual(by_market["KXREFERENCE"]["price_basis"], "recorded_market_reference")
        self.assertEqual(by_market["KXREFERENCE"]["action_label"], "reference_price_diagnostic")
        self.assertEqual(by_market["KXEXECUTABLE"]["action"], "BUY_YES")
        self.assertEqual(by_market["KXEXECUTABLE"]["price_basis"], "recorded_executable_ask")
        self.assertIsNone(by_market["KXEXECUTABLE"]["action_label"])
        self.assertEqual(by_market["KXMISSING"]["action"], "SKIP")
        self.assertIsNone(by_market["KXMISSING"]["price_basis"])

        report = resolve_sealed_source_probability_decisions(control, outcomes)
        self.assertEqual(report["promotion_requirement"], "forward_paper_required_before_promotion")
        resolved = {row["market_id"]: row for row in report["resolved_decisions"]}
        self.assertEqual(resolved["KXREFERENCE"]["pnl_category"], "reference_price_proxy_pnl")
        self.assertIsNone(resolved["KXREFERENCE"]["executable_pnl_usd"])
        self.assertEqual(resolved["KXREFERENCE"]["reference_price_proxy_pnl_usd"], 0.6)
        self.assertEqual(resolved["KXEXECUTABLE"]["pnl_category"], "executable_pnl")
        self.assertEqual(resolved["KXEXECUTABLE"]["executable_pnl_usd"], 0.6)
        self.assertIsNone(resolved["KXEXECUTABLE"]["reference_price_proxy_pnl_usd"])
        self.assertIsNone(resolved["KXMISSING"]["pnl_category"])
        self.assertEqual(report["summary"]["executable_pnl_decisions"], 1)
        self.assertEqual(report["summary"]["reference_price_proxy_pnl_decisions"], 1)

    def test_run_and_cli_write_separate_labeled_artifacts(self):
        records, outcomes = self._records_and_outcomes()
        inputs_path = Path(self.tempdir.name) / "replay_decision_inputs.jsonl"
        outcomes_path = Path(self.tempdir.name) / "finalized_outcomes.jsonl"
        inputs_path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
        outcomes_path.write_text("".join(json.dumps(row) + "\n" for row in outcomes), encoding="utf-8")

        result = run_collector_source_router_replay(
            replay_inputs_path=inputs_path, finalized_outcomes_path=outcomes_path,
            output_dir=self.output_dir, min_sample_count=1,
        )
        self.assertTrue(result.control_decisions_path.is_file())
        self.assertTrue(result.candidate_decisions_path.is_file())
        self.assertTrue(result.resolution_report_path.is_file())
        self.assertTrue(result.cohort_report_path.is_file())
        self.assertIn("source_correctness_shape_cohort_report.json", result.metadata["output_artifacts"])
        self.assertEqual(result.metadata["control_lane_id"], "source_probability_control_v1")
        self.assertEqual(result.metadata["candidate_lane_id"], "source_router_candidate_v1")

        cli_output = Path(tempfile.mkdtemp(prefix="test_source_probability_cli_", dir=DERIVED_ROOT))
        self.addCleanup(shutil.rmtree, cli_output, True)
        completed = subprocess.run(
            [
                sys.executable, "scripts/collector_source_router_replay.py",
                "--replay-inputs", str(inputs_path), "--finalized-outcomes", str(outcomes_path),
                "--output-dir", str(cli_output), "--min-sample-count", "1",
            ], cwd=ROOT, check=True, capture_output=True, text=True,
        )
        self.assertIn("source_probability_control_v1", completed.stdout)
        self.assertTrue((cli_output / "sealed_control_decisions.jsonl").is_file())
