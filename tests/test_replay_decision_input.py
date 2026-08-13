import hashlib
import json
import unittest
from pathlib import Path

from bot.replay_decision_input import build_replay_decision_input_v1, verify_replay_decision_input_v1


def _sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _snapshot_row(*, patch: dict | None = None) -> dict:
    row = {
        "shared_snapshot_id": "snapshot-20260812-001",
        "shared_candidate_id": "candidate-20260812-001",
        "market_id": "KXHIGHNY-26AUG12-T80",
        "observed_at": "2026-08-12T15:04:05Z",
        "question": "Will NYC high temperature exceed 80F?",
        "yes_price": 0.42,
        "no_price": 0.58,
        "collector_provenance": {
            "raw_payload_sha256": _sha256("raw-payload"),
            "collector_index_entry_sha256": _sha256("index-entry"),
        },
        "replay_decision_context": {
            "strategy_input_schema_version": "weather-input-v3",
            "policy_config_sha256": _sha256("policy"),
            "strategy_logic_sha256": _sha256("logic"),
            "kelly_config_sha256": _sha256("kelly"),
            "risk_config_sha256": _sha256("risk"),
            "execution_price_policy_sha256": _sha256("execution"),
        },
        "replay_derived_features": {
            "schema_version": "weather-features-v2",
            "values": {"forecast_high_f": 84.0, "threshold_f": 80.0},
        },
        "decision_artifact": {
            "source_context": {
                "source": "provided",
                "mode": "prediction_lab",
                "as_of": "2026-08-12T15:04:05+00:00",
                "data": {
                    "market_metadata": {
                        "market_group": "weather",
                        "series": "daily_temperature",
                        "event_ticker": "KXHIGHNY-26AUG12",
                        "market_route": {"family": "weather", "series": "daily_temperature"},
                    },
                    "weather_source_snapshot": {
                        "artifact_version": 1,
                        "mode": "recorded_as_of",
                        "source_name": "weather",
                        "signal_type": "weather",
                        "market_date": "2026-08-12",
                        "target_forecast_date": "2026-08-12",
                        "as_of": "2026-08-12T15:00:00+00:00",
                        "source_fetched_at": "2026-08-12T15:00:00+00:00",
                        "confidence": 0.91,
                        "predicted_prob": 0.82,
                        "settlement_source": "nws",
                        "station_id": "KNYC",
                        "station_cli": "NYC",
                        "station_mapping": "exact",
                        "station_resolution": {
                            "mapping": "exact",
                            "city_code": "NYC",
                            "city_id": "new_york_ny",
                            "city": "New York",
                            "state": "NY",
                            "station_id": "KNYC",
                            "station_cli": "NYC",
                            "source": "static_baseline_station_cache",
                            "reason": "Static station mapping",
                            "matched_from": "ticker",
                        },
                        "forecast": {"high": 84.0, "low": 65.0, "current": 73.0, "threshold": 80.0, "question_side": "above"},
                        "sources": [
                            {
                                "source_name": "nws",
                                "role": "settlement_primary",
                                "forecast_high": 84.0,
                                "forecast_low": 65.0,
                                "current_forecast": 73.0,
                                "fetched_at": "2026-08-12T15:00:00+00:00",
                                "as_of": "2026-08-12T15:00:00+00:00",
                                "settlement_source": "nws",
                                "source_metadata": {"office": "OKX", "grid_x": 33, "grid_y": 37},
                            }
                        ],
                    },
                },
            },
            "source_snapshots": [{"source": "weather", "as_of": "2026-08-12T15:00:00+00:00"}],
            "execution_snapshot": {
                "source": "book",
                "yes_price": 0.42,
                "no_price": 0.58,
                "best_yes_ask": 0.43,
                "best_no_ask": 0.59,
            },
        },
    }
    if patch:
        row.update(patch)
    return row


class ReplayDecisionInputTests(unittest.TestCase):
    def test_builds_fresh_allowlisted_input_with_canonical_provenance_and_key(self):
        row = _snapshot_row()

        result = build_replay_decision_input_v1(row)

        self.assertTrue(result.ok)
        self.assertEqual(result.errors, ())
        assert result.record is not None
        record = result.record
        expected_raw_row_hash = hashlib.sha256(
            json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        self.assertEqual(record["schema_name"], "replay_decision_input")
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["observed_at"], "2026-08-12T15:04:05+00:00")
        self.assertEqual(record["snapshot_provenance"]["raw_row_sha256"], expected_raw_row_hash)
        self.assertEqual(
            record["decision_key"],
            {
                "shared_snapshot_id": "snapshot-20260812-001",
                "shared_candidate_id": "candidate-20260812-001",
                "market_id": "KXHIGHNY-26AUG12-T80",
                "observed_at_utc": "2026-08-12T15:04:05+00:00",
                "raw_row_sha256": expected_raw_row_hash,
            },
        )
        self.assertEqual(record["market"]["best_yes_ask"], 0.43)
        self.assertEqual(record["source_inputs"]["recorded_as_of"], "2026-08-12T15:04:05+00:00")
        self.assertIsInstance(result.canonical_input_json, bytes)
        self.assertTrue(verify_replay_decision_input_v1(record, result.canonical_input_json))

    def test_rejects_nested_outcome_like_fields_without_copying_or_stripping_them(self):
        row = _snapshot_row()
        row["decision_artifact"]["source_context"]["data"]["future_pnl_inputs"] = {"resolution": "YES"}

        result = build_replay_decision_input_v1(row)

        self.assertFalse(result.ok)
        self.assertIsNone(result.record)
        self.assertEqual(result.errors[0].code, "forbidden_outcome_or_future_field")
        self.assertEqual(
            result.errors[0].path,
            "decision_artifact.source_context.data.future_pnl_inputs",
        )

    def test_rejects_normalized_outcome_aliases_in_every_copied_subtree(self):
        cases = (
            ("decision_artifact.source_context.data.weather_source_snapshot", "futurePnlInputs"),
            ("decision_artifact.source_snapshots[0]", "settlementTimestamp"),
            ("replay_derived_features.values", "finalOutcome"),
            ("decision_artifact.source_context.data.market_metadata", "resolvedAt"),
        )
        for path, field in cases:
            with self.subTest(path=path, field=field):
                row = _snapshot_row()
                if path.startswith("decision_artifact.source_context.data.weather"):
                    row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"][field] = "no"
                elif path.startswith("decision_artifact.source_snapshots"):
                    row["decision_artifact"]["source_snapshots"][0][field] = "2026-08-13T00:00:00Z"
                elif path.startswith("replay_derived"):
                    row["replay_derived_features"]["values"][field] = "YES"
                else:
                    row["decision_artifact"]["source_context"]["data"]["market_metadata"][field] = "2026-08-13T00:00:00Z"

                result = build_replay_decision_input_v1(row)

                self.assertFalse(result.ok)
                self.assertTrue(any(error.code == "forbidden_outcome_or_future_field" for error in result.errors))

    def test_rejects_unknown_fields_in_copied_subtrees_but_ignores_unrelated_raw_fields(self):
        row = _snapshot_row()
        row["legacy_action"] = "BUY_YES"
        row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]["provider_debug_blob"] = {"x": 1}

        result = build_replay_decision_input_v1(row)

        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0].code, "unknown_unallowlisted_input_field")
        self.assertEqual(
            result.errors[0].path,
            "decision_artifact.source_context.data.weather_source_snapshot.provider_debug_blob",
        )

    def test_rejects_recorded_output_aliases_but_keeps_source_predicted_prob(self):
        aliases = ("action", "finalAction", "modelProbability", "positionSize", "requestedSize", "stake", "notional", "kellyFraction")
        for field in aliases:
            with self.subTest(field=field):
                row = _snapshot_row()
                row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"][field] = 1
                result = build_replay_decision_input_v1(row)
                self.assertFalse(result.ok)
                self.assertTrue(any(error.code == "forbidden_recorded_decision_field" for error in result.errors))

        accepted = build_replay_decision_input_v1(_snapshot_row())
        self.assertTrue(accepted.ok)
        assert accepted.record is not None
        self.assertEqual(
            accepted.record["source_inputs"]["source_context"]["data"]["weather_source_snapshot"]["predicted_prob"],
            0.82,
        )

    def test_rejects_missing_and_non_utc_observed_at(self):
        missing = build_replay_decision_input_v1(_snapshot_row(patch={"observed_at": ""}))
        offset = build_replay_decision_input_v1(_snapshot_row(patch={"observed_at": "2026-08-12T08:04:05-07:00"}))
        invalid = build_replay_decision_input_v1(_snapshot_row(patch={"observed_at": "not-a-timestamp"}))

        self.assertEqual(missing.errors[0].code, "missing_observed_at")
        self.assertEqual(offset.errors[0].code, "observed_at_not_utc")
        self.assertEqual(invalid.errors[0].code, "invalid_observed_at")

    def test_ignores_recorded_decisions_instead_of_reusing_action_probability_or_size(self):
        row = _snapshot_row(
            patch={
                "main_decision": {"action": "BUY_NO", "model_probability": 0.01, "position_size": 999.0},
                "normal_decision": {"action": "BUY_YES", "model_probability": 0.99, "position_size": 888.0},
                "action": "BUY_NO",
                "model_probability": 0.01,
                "position_size": 999.0,
            }
        )

        result = build_replay_decision_input_v1(row)

        self.assertTrue(result.ok)
        assert result.record is not None
        serialized = json.dumps(result.record, sort_keys=True)
        for forbidden_key in ("main_decision", "normal_decision", "model_probability", "position_size", '"action"'):
            self.assertNotIn(forbidden_key, serialized)

    def test_rejects_recorded_decision_fields_when_they_would_reach_lane_input(self):
        row = _snapshot_row()
        row["decision_artifact"]["source_context"]["data"]["main_decision"] = {"action": "BUY_YES"}

        result = build_replay_decision_input_v1(row)

        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0].code, "forbidden_recorded_decision_field")
        self.assertEqual(result.errors[0].path, "decision_artifact.source_context.data.main_decision")

    def test_reobservations_remain_distinct_by_canonical_raw_row_hash(self):
        first = _snapshot_row()
        second = _snapshot_row()
        second["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]["forecast"]["high"] = 85.0

        first_result = build_replay_decision_input_v1(first)
        second_result = build_replay_decision_input_v1(second)

        self.assertTrue(first_result.ok)
        self.assertTrue(second_result.ok)
        assert first_result.record is not None
        assert second_result.record is not None
        self.assertNotEqual(first_result.record["decision_key"], second_result.record["decision_key"])

    def test_canonical_bytes_and_hash_are_tamper_evident_after_returned_mapping_mutation(self):
        result = build_replay_decision_input_v1(_snapshot_row())

        self.assertTrue(result.ok)
        assert result.record is not None
        assert result.canonical_input_json is not None
        original_bytes = result.canonical_input_json
        original_hash = result.record["canonical_input_sha256"]
        result.record["source_inputs"]["source_context"]["data"]["weather_source_snapshot"]["forecast"]["high"] = 999.0

        self.assertEqual(result.canonical_input_json, original_bytes)
        self.assertEqual(result.record["canonical_input_sha256"], original_hash)
        self.assertFalse(verify_replay_decision_input_v1(result.record, result.canonical_input_json))

    def test_real_archive_weather_shape_smoke_reports_honest_v1_blockers(self):
        archive_path = Path(
            "/mnt/data-collection/prediction-bot/data/beta_shadow/forward_20260726T1810Z_all_lanes/"
            "paper/prediction_lab/market_snapshots.jsonl"
        )
        if not archive_path.is_file():
            self.skipTest(f"local real archive is unavailable: {archive_path}")
        with archive_path.open(encoding="utf-8") as handle:
            row = json.loads(next(line for line in handle if line.strip()))

        result = build_replay_decision_input_v1(row)
        blockers = [error.to_dict() for error in result.errors]

        self.assertFalse(result.ok, blockers)
        self.assertTrue(any(blocker["path"] == "replay_decision_context" for blocker in blockers), blockers)
        self.assertFalse(any(blocker["path"].startswith("main_decision") for blocker in blockers), blockers)
        self.assertFalse(any(blocker["path"].endswith("settlement_source") for blocker in blockers), blockers)


if __name__ == "__main__":
    unittest.main()
