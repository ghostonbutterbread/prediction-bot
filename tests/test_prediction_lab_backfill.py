import json
import tempfile
import unittest
from pathlib import Path

from bot.file_ops import append_jsonl, load_jsonl
from bot.prediction_lab_backfill import (
    CANONICAL_ANALYSIS_LEDGER_NAME,
    TIER_COVERAGE_ONLY,
    TIER_REPLAY_GRADE_BACKFILLED_FROM_ARTIFACT,
    TIER_REPLAY_GRADE_ORIGINAL,
    run_prediction_lab_canonical_analysis,
    run_prediction_lab_backfill,
)
from bot.prediction_lab_replay import classify_replay_row_quality, classify_source_mode, validate_prediction_lab_tables


def _weather_snapshot() -> dict:
    return {
        "mode": "recorded_as_of",
        "source_name": "weather",
        "signal_type": "weather",
        "as_of": "2026-04-29T12:00:00+00:00",
        "predicted_prob": 0.82,
        "confidence": 0.92,
        "forecast": {"high": 84.0, "threshold": 80.0, "question_side": "above"},
        "date_validation": {
            "ok": True,
            "reason": "matched",
            "market_date": "2026-04-29",
            "weather_date": "2026-04-29",
        },
        "source_signal": {
            "signal_type": "weather",
            "predicted_prob": 0.82,
            "confidence": 0.92,
            "data": {"forecast_high": 84.0, "threshold": 80.0},
        },
    }


def _strict_row(market_id: str = "KXHIGHNY-26APR29-T80") -> dict:
    return {
        "timestamp": "2026-04-29T12:00:00+00:00",
        "observed_at": "2026-04-29T12:00:00+00:00",
        "market_id": market_id,
        "group": "weather",
        "series": "daily_temperature",
        "event_ticker": "KXHIGHNY-26APR29",
        "question": "Will NYC high temperature be above 80?",
        "direction": "BUY_YES",
        "decision_type": "buy_yes",
        "yes_market_price": 0.42,
        "no_market_price": 0.58,
        "decision_artifact": {
            "market_id": market_id,
            "as_of": "2026-04-29T12:00:00+00:00",
            "final_action": "BUY_YES",
            "final_reason_code": "approved",
            "source_context": {
                "source": "provided",
                "source_mode": "recorded_as_of",
                "data": {
                    "market_metadata": {
                        "market_group": "weather",
                        "series": "daily_temperature",
                        "event_ticker": "KXHIGHNY-26APR29",
                    },
                    "weather_source_snapshot": _weather_snapshot(),
                },
            },
            "source_snapshots": [
                {
                    "mode": "recorded_as_of",
                    "source": "weather",
                    "method": "_live_data_signal",
                    "snapshot_ref": "source_context.data.weather_source_snapshot",
                }
            ],
            "order_book_snapshot": {
                "source": "book",
                "data": {"best_yes_ask": 0.43, "best_yes_bid": 0.42, "best_no_ask": 0.59, "best_no_bid": 0.58},
            },
            "execution_snapshot": {"source": "book", "best_yes_ask": 0.43, "best_no_ask": 0.59},
            "execution_snapshot_source": "book",
            "execution_feasibility": {
                "artifact_version": 1,
                "mode": "passive_snapshot_comparison",
                "feasible": True,
                "status": "feasible",
                "action": "BUY_YES",
                "side": "yes",
                "pre_logic_ask": 0.43,
                "post_logic_ask": 0.43,
                "ask_delta": 0.0,
                "max_slippage": 0.01,
                "max_elapsed_ms": 2000,
                "decision_latency_ms": 12.5,
                "elapsed_ms": 13.0,
                "same_market": True,
                "market_open": True,
                "same_market_open": True,
                "same_side_ask_present": True,
                "ask_unchanged": True,
                "ask_within_slippage": True,
                "quantity_check_available": False,
                "sufficient_quantity": None,
                "elapsed_within_threshold": True,
                "failed_checks": [],
                "mutates_paper_state": False,
            },
        },
    }


def _nested_recoverable_row() -> dict:
    row = _strict_row("KXHIGHNY-26APR29-T81")
    artifact = row["decision_artifact"]
    artifact.pop("source_context")
    artifact["source_snapshots"] = [
        {
            "mode": "recorded_as_of",
            "source": "weather",
            "method": "_live_data_signal",
            **_weather_snapshot(),
        }
    ]
    artifact.pop("order_book_snapshot")
    artifact.pop("execution_snapshot")
    artifact.pop("execution_snapshot_source")
    artifact.pop("execution_feasibility")
    artifact["order_book"] = {"best_yes_ask": 0.44, "best_yes_bid": 0.42, "best_no_ask": 0.58, "best_no_bid": 0.56}
    return row


def _historical_signal_recoverable_row() -> dict:
    row = _strict_row("KXHIGHNY-26APR29-T82")
    artifact = row["decision_artifact"]
    artifact["source_context"]["data"].pop("weather_source_snapshot")
    artifact.pop("source_snapshots")
    artifact["strategy_signal"] = {
        "signal_details": {
            "live": {
                "signal_type": "weather",
                "predicted_prob": 0.95,
                "confidence": 0.98,
                "source_timestamp": "2026-04-29T23:59:59+00:00",
                "ttl_seconds": 0,
                "question_side": "above",
                "data": {
                    "forecast_high": 86.0,
                    "forecast_low": 67.0,
                    "historical_high": 86.0,
                    "historical_low": 67.0,
                    "actual_temp_used": 86.0,
                    "predicted_temp": 86.0,
                    "threshold": 80.0,
                    "sources": ["noaa_daily_summaries_station"],
                    "source_quality": "settlement_station_official_daily",
                    "historical_replay": True,
                    "weather_date": "2026-04-29",
                    "date_validation": {
                        "ok": True,
                        "reason": "matched",
                        "market_date": "2026-04-29",
                        "weather_date": "2026-04-29",
                    },
                },
            }
        }
    }
    return row


class PredictionLabBackfillTests(unittest.TestCase):
    def test_inventory_only_reports_counts_without_writing_upgraded_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            input_path = tmp / "market_snapshots.jsonl"
            output_dir = tmp / "backfill"
            append_jsonl(input_path, _strict_row())
            append_jsonl(
                input_path,
                {
                    "timestamp": "2026-04-29T12:01:00+00:00",
                    "market_id": "KXLEGACY-1",
                    "group": "weather",
                    "series": "daily_temperature",
                    "decision_type": "skip",
                    "direction": "SKIP",
                },
            )

            result = run_prediction_lab_backfill(input_path, output_dir, inventory_only=True)

            self.assertEqual(result.report["total_rows"], 2)
            self.assertEqual(result.report["tier_counts"][TIER_REPLAY_GRADE_ORIGINAL], 1)
            self.assertEqual(result.report["tier_counts"][TIER_COVERAGE_ONLY], 1)
            self.assertEqual(result.report["reason_counts"]["missing_decision_artifact"], 1)
            self.assertEqual(result.report["reason_counts"]["missing_source_snapshot"], 1)
            self.assertFalse((output_dir / "upgraded_market_snapshots.jsonl").exists())
            self.assertFalse((output_dir / "provenance_manifest.json").exists())

    def test_artifact_recovery_normalizes_nested_weather_order_book_and_execution_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            input_path = tmp / "market_snapshots.jsonl"
            output_dir = tmp / "backfill"
            append_jsonl(input_path, _nested_recoverable_row())

            result = run_prediction_lab_backfill(input_path, output_dir, artifact_recovery=True)
            upgraded = load_jsonl(output_dir / "upgraded_market_snapshots.jsonl")[0]
            artifact = upgraded["decision_artifact"]

            self.assertEqual(result.report["tier_counts"][TIER_COVERAGE_ONLY], 1)
            self.assertEqual(upgraded["provenance"]["tier"], TIER_COVERAGE_ONLY)
            self.assertIn("missing_execution_feasibility", upgraded["provenance"]["reasons"])
            self.assertIn("weather_source_snapshot", artifact["source_context"]["data"])
            self.assertEqual(artifact["order_book_snapshot"]["data"]["best_yes_ask"], 0.44)
            self.assertEqual(artifact["execution_snapshot"]["best_no_ask"], 0.58)
            recovered_fields = {source["field"] for source in upgraded["provenance"]["sources"]}
            self.assertEqual(
                recovered_fields,
                {"weather_source_snapshot", "order_book_snapshot", "execution_snapshot"},
            )
            self.assertTrue(validate_prediction_lab_tables([output_dir / "upgraded_market_snapshots.jsonl"]).ok)

    def test_artifact_recovery_reconstructs_historical_weather_snapshot_as_post_facto(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            input_path = tmp / "market_snapshots.jsonl"
            output_dir = tmp / "backfill"
            append_jsonl(input_path, _historical_signal_recoverable_row())

            result = run_prediction_lab_backfill(input_path, output_dir, artifact_recovery=True)
            upgraded = load_jsonl(output_dir / "upgraded_market_snapshots.jsonl")[0]
            artifact = upgraded["decision_artifact"]
            weather_snapshot = artifact["source_context"]["data"]["weather_source_snapshot"]
            quality = classify_replay_row_quality(artifact, upgraded)

            self.assertEqual(result.report["tier_counts"][TIER_COVERAGE_ONLY], 1)
            self.assertEqual(artifact["source_context"]["source_mode"], "historical_post_facto")
            self.assertEqual(weather_snapshot["mode"], "historical_post_facto")
            self.assertEqual(weather_snapshot["source_provenance"], "historical_post_facto_backfill")
            self.assertEqual(artifact["source_snapshots"][0]["mode"], "historical_post_facto")
            self.assertEqual(classify_source_mode(artifact, upgraded), "historical_post_facto")
            self.assertEqual(quality.category, "historical_post_facto")
            self.assertIn("source_mode_historical_post_facto", upgraded["provenance"]["reasons"])
            self.assertNotIn("missing_weather_snapshot", upgraded["provenance"]["reasons"])
            self.assertIn("weather_source_snapshot", {source["field"] for source in upgraded["provenance"]["sources"]})
            self.assertTrue(validate_prediction_lab_tables([output_dir / "upgraded_market_snapshots.jsonl"]).ok)

    def test_provenance_manifest_labels_original_and_artifact_backfilled_tiers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            input_path = tmp / "market_snapshots.jsonl"
            output_dir = tmp / "backfill"
            append_jsonl(input_path, _strict_row())
            append_jsonl(input_path, _nested_recoverable_row())

            run_prediction_lab_backfill(input_path, output_dir, artifact_recovery=True)
            manifest = json.loads((output_dir / "provenance_manifest.json").read_text(encoding="utf-8"))
            upgraded = load_jsonl(output_dir / "upgraded_market_snapshots.jsonl")

            self.assertFalse(manifest["raw_ledgers_mutated"])
            self.assertEqual(manifest["tier_counts"][TIER_REPLAY_GRADE_ORIGINAL], 1)
            self.assertEqual(manifest["tier_counts"][TIER_COVERAGE_ONLY], 1)
            self.assertEqual(upgraded[0]["provenance"]["tier"], TIER_REPLAY_GRADE_ORIGINAL)
            self.assertEqual(upgraded[1]["decision_artifact"]["provenance"]["tier"], TIER_COVERAGE_ONLY)

    def test_outcome_leakage_is_removed_from_derived_rows_and_raw_ledger_is_not_mutated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            input_path = tmp / "predictions.jsonl"
            output_dir = tmp / "backfill"
            leaked = _strict_row()
            leaked["resolution"] = {"outcome": "YES", "resolved_at": "2026-04-30T00:00:00+00:00"}
            leaked["decision_artifact"]["outcome"] = "YES"
            append_jsonl(input_path, leaked)
            raw_before = input_path.read_bytes()

            result = run_prediction_lab_backfill(input_path, output_dir, artifact_recovery=True)
            raw_after = input_path.read_bytes()
            upgraded_path = output_dir / "upgraded_market_snapshots.jsonl"
            upgraded = load_jsonl(upgraded_path)[0]
            validation = validate_prediction_lab_tables([upgraded_path]).to_dict()

            self.assertEqual(raw_after, raw_before)
            self.assertEqual(result.report["rows_with_outcome_leakage"], 1)
            self.assertEqual(result.report["reason_counts"]["possible_outcome_leakage"], 1)
            self.assertNotIn("resolution", upgraded)
            self.assertNotIn("outcome", upgraded["decision_artifact"])
            self.assertNotIn("outcome_leakage", validation["issue_counts"])
            self.assertEqual(upgraded["provenance"]["tier"], TIER_COVERAGE_ONLY)

    def test_canonical_analysis_writes_stable_analysis_outputs_and_validation_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            prediction_lab_dir = tmp / "prediction_lab"
            analysis_dir = prediction_lab_dir / "analysis"
            market_snapshots = prediction_lab_dir / "market_snapshots.jsonl"
            append_jsonl(market_snapshots, _strict_row())

            result = run_prediction_lab_canonical_analysis(
                prediction_lab_dir=prediction_lab_dir,
                analysis_dir=analysis_dir,
                validate_output=True,
            )

            self.assertEqual(result.output_path, analysis_dir / CANONICAL_ANALYSIS_LEDGER_NAME)
            self.assertTrue((analysis_dir / "market_snapshots_upgraded.jsonl").exists())
            self.assertTrue((analysis_dir / "backfill_report.json").exists())
            self.assertTrue((analysis_dir / "provenance_manifest.json").exists())
            self.assertTrue((analysis_dir / "validation_report.json").exists())
            self.assertTrue((analysis_dir / "latest_metadata.json").exists())
            validation = json.loads((analysis_dir / "validation_report.json").read_text(encoding="utf-8"))
            self.assertTrue(validation["ok"])
            self.assertFalse(validation["skipped"])
            self.assertEqual(validation["total_rows"], 1)
            self.assertEqual(result.report["output_ledger_name"], CANONICAL_ANALYSIS_LEDGER_NAME)

    def test_canonical_analysis_can_include_predictions_and_preserves_raw_ledgers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            prediction_lab_dir = tmp / "prediction_lab"
            analysis_dir = prediction_lab_dir / "analysis"
            market_snapshots = prediction_lab_dir / "market_snapshots.jsonl"
            predictions = prediction_lab_dir / "predictions.jsonl"
            append_jsonl(market_snapshots, _strict_row("KXHIGHNY-26APR29-T80"))
            append_jsonl(predictions, _nested_recoverable_row())
            raw_snapshots_before = market_snapshots.read_bytes()
            raw_predictions_before = predictions.read_bytes()

            result = run_prediction_lab_canonical_analysis(
                prediction_lab_dir=prediction_lab_dir,
                analysis_dir=analysis_dir,
                include_predictions=True,
                validate_output=True,
            )
            upgraded = load_jsonl(analysis_dir / CANONICAL_ANALYSIS_LEDGER_NAME)
            manifest = json.loads((analysis_dir / "provenance_manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(market_snapshots.read_bytes(), raw_snapshots_before)
            self.assertEqual(predictions.read_bytes(), raw_predictions_before)
            self.assertEqual(len(upgraded), 2)
            self.assertEqual(result.report["source_row_counts"][str(market_snapshots)], 1)
            self.assertEqual(result.report["source_row_counts"][str(predictions)], 1)
            self.assertEqual(manifest["tier_counts"][TIER_REPLAY_GRADE_ORIGINAL], 1)
            self.assertEqual(manifest["tier_counts"][TIER_COVERAGE_ONLY], 1)
            self.assertIn("nested_artifact_recovery", manifest["source_methods"])
            self.assertEqual(upgraded[0]["provenance"]["input_path"], str(market_snapshots))
            self.assertEqual(upgraded[1]["provenance"]["input_path"], str(predictions))


if __name__ == "__main__":
    unittest.main()
