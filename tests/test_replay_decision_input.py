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
                                "source_evidence_version": 1,
                                "evidence_type": "forecast",
                                "forecast_availability": "available",
                                "scoreable_forecast": True,
                                "market_target_date": "2026-08-12",
                                "source_target_date": "2026-08-12",
                                "target_mapping": {
                                    "market_target_date": "2026-08-12",
                                    "source_target_date": "2026-08-12",
                                    "mapping": "exact_source_local_nws_period",
                                    "source_period_start": "2026-08-12T06:00:00-04:00",
                                    "source_period_end": "2026-08-12T18:00:00-04:00",
                                },
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
    def test_v2_collector_row_requires_recorded_shared_identity(self):
        row = _snapshot_row(patch={"collector_artifact_schema_version": 2})
        del row["shared_snapshot_id"]

        result = build_replay_decision_input_v1(row)

        self.assertFalse(result.ok)
        self.assertIn(
            ("missing_required_field", "shared_snapshot_id"),
            {(error.code, error.path) for error in result.errors},
        )

    def test_v2_collector_row_uses_recorded_identity_without_requiring_replay_run_metadata(self):
        row = _snapshot_row(patch={"collector_artifact_schema_version": 2})
        del row["collector_provenance"]
        del row["replay_decision_context"]
        del row["replay_derived_features"]

        result = build_replay_decision_input_v1(row)

        self.assertTrue(result.ok, [error.to_dict() for error in result.errors])
        assert result.record is not None
        self.assertEqual(result.record["input_mode"], "collector_v2_sanitized_v1")
        self.assertEqual(result.record["decision_key"]["shared_snapshot_id"], "snapshot-20260812-001")

    def test_v2_collector_row_requires_source_evidence_classification(self):
        row = _snapshot_row(patch={"collector_artifact_schema_version": 2})
        del row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]["sources"][0]["source_evidence_version"]

        result = build_replay_decision_input_v1(row)

        self.assertFalse(result.ok)
        self.assertIn(
            ("missing_required_field", "decision_artifact.source_context.data.weather_source_snapshot.sources[0].source_evidence_version"),
            {(error.code, error.path) for error in result.errors},
        )

    def test_preserves_strict_source_question_side_in_sealed_input(self):
        row = _snapshot_row(patch={"collector_artifact_schema_version": 2})
        source = row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]["sources"][0]
        source["question_side"] = "above"

        result = build_replay_decision_input_v1(row)

        self.assertTrue(result.ok, [error.to_dict() for error in result.errors])
        assert result.record is not None
        sealed_source = result.record["source_inputs"]["source_context"]["data"]["weather_source_snapshot"]["sources"][0]
        self.assertEqual(sealed_source["question_side"], "above")

    def test_strict_v2_collector_row_requires_source_evidence_classification(self):
        row = _snapshot_row(patch={"collector_artifact_schema_version": 2})
        del row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]["sources"][0]["source_evidence_version"]

        result = build_replay_decision_input_v1(row, strict=True)

        self.assertFalse(result.ok)
        self.assertIn(
            ("missing_required_field", "decision_artifact.source_context.data.weather_source_snapshot.sources[0].source_evidence_version"),
            {(error.code, error.path) for error in result.errors},
        )

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

    def test_strict_rejects_nested_outcome_like_fields_without_copying_or_stripping_them(self):
        row = _snapshot_row()
        row["decision_artifact"]["source_context"]["data"]["future_pnl_inputs"] = {"resolution": "YES"}

        result = build_replay_decision_input_v1(row, strict=True)

        self.assertFalse(result.ok)
        self.assertIsNone(result.record)
        self.assertEqual(result.errors[0].code, "forbidden_outcome_or_future_field")
        self.assertEqual(
            result.errors[0].path,
            "decision_artifact.source_context.data.future_pnl_inputs",
        )

    def test_strict_rejects_normalized_outcome_aliases_in_every_copied_subtree(self):
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

                result = build_replay_decision_input_v1(row, strict=True)

                self.assertFalse(result.ok)
                self.assertTrue(any(error.code == "forbidden_outcome_or_future_field" for error in result.errors))

    def test_strict_rejects_unknown_fields_in_copied_subtrees_but_ignores_unrelated_raw_fields(self):
        row = _snapshot_row()
        row["legacy_action"] = "BUY_YES"
        row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]["provider_debug_blob"] = {"x": 1}

        result = build_replay_decision_input_v1(row, strict=True)

        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0].code, "unknown_unallowlisted_input_field")
        self.assertEqual(
            result.errors[0].path,
            "decision_artifact.source_context.data.weather_source_snapshot.provider_debug_blob",
        )

    def test_strict_rejects_recorded_output_aliases_but_keeps_source_predicted_prob(self):
        aliases = ("action", "finalAction", "modelProbability", "positionSize", "requestedSize", "stake", "notional", "kellyFraction")
        for field in aliases:
            with self.subTest(field=field):
                row = _snapshot_row()
                row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"][field] = 1
                result = build_replay_decision_input_v1(row, strict=True)
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
        for forbidden_key in ('"model_probability":', '"position_size":', '"action":'):
            self.assertNotIn(forbidden_key, serialized)

    def test_strict_rejects_recorded_decision_fields_when_they_would_reach_lane_input(self):
        row = _snapshot_row()
        row["decision_artifact"]["source_context"]["data"]["main_decision"] = {"action": "BUY_YES"}

        result = build_replay_decision_input_v1(row, strict=True)

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

    def test_real_archive_first_row_builds_in_default_legacy_compatibility_mode(self):
        archive_path = Path(
            "/mnt/data-collection/prediction-bot/data/beta_shadow/forward_20260726T1810Z_all_lanes/"
            "paper/prediction_lab/market_snapshots.jsonl"
        )
        if not archive_path.is_file():
            self.skipTest(f"local real archive is unavailable: {archive_path}")
        with archive_path.open(encoding="utf-8") as handle:
            row = json.loads(next(line for line in handle if line.strip()))

        result = build_replay_decision_input_v1(row)

        self.assertTrue(result.ok, [error.to_dict() for error in result.errors])
        assert result.record is not None
        self.assertEqual(result.record["input_mode"], "legacy_sanitized_v1")
        self.assertTrue(result.record["decision_key"]["shared_snapshot_id"].startswith("legacy-snapshot-"))
        omitted = {
            (entry["path"], entry["category"])
            for entry in result.record["sanitization"]["omitted_fields"]
        }
        self.assertIn(
            ("decision_artifact.source_context.data.market_metadata.outcome", "outcome_or_future"),
            omitted,
        )
        self.assertIn(
            ("decision_artifact.source_context.data.market_metadata.status", "unallowlisted"),
            omitted,
        )
        self.assertIn(
            ("decision_artifact.source_context.data.weather_source_snapshot.veto.final_action", "recorded_decision"),
            omitted,
        )

    def test_default_mode_omits_legacy_decision_and_outcome_fields_from_lane_input(self):
        row = _snapshot_row()
        row["main_decision"] = {"action": "BUY_YES", "model_probability": 0.99, "position_size": 12}
        row["decision_artifact"]["source_context"]["data"]["market_metadata"].update(
            {"outcome": "YES", "result": "YES", "status": "closed"}
        )
        row["decision_artifact"]["source_context"]["data"]["weather_source_snapshot"]["veto"] = {
            "final_action": "BUY_YES"
        }

        result = build_replay_decision_input_v1(row)

        self.assertTrue(result.ok)
        assert result.record is not None
        serialized = json.dumps(result.record, sort_keys=True)
        for forbidden_key in ('"outcome":', '"result":', '"final_action":', '"action":', '"model_probability":', '"position_size":'):
            self.assertNotIn(forbidden_key, serialized)
        omitted = result.record["sanitization"]["omitted_fields"]
        self.assertIn({"path": "main_decision", "category": "recorded_decision"}, omitted)
        self.assertIn(
            {"path": "decision_artifact.source_context.data.market_metadata.outcome", "category": "outcome_or_future"},
            omitted,
        )
        self.assertIn(
            {"path": "decision_artifact.source_context.data.weather_source_snapshot.veto.final_action", "category": "recorded_decision"},
            omitted,
        )

    def test_default_mode_audits_unallowlisted_top_level_legacy_fields_without_copying_values(self):
        row = _snapshot_row()
        row["legacy_debug_blob"] = {"opaque": "do-not-copy"}

        result = build_replay_decision_input_v1(row)

        self.assertTrue(result.ok)
        assert result.record is not None
        self.assertIn(
            {"path": "legacy_debug_blob", "category": "unallowlisted"},
            result.record["sanitization"]["omitted_fields"],
        )
        self.assertNotIn("do-not-copy", json.dumps(result.record, sort_keys=True))

    def test_default_mode_audits_malformed_optional_artifact_fields_without_rejecting_row(self):
        cases = (
            ("source_snapshots", {"unexpected": "mapping"}, "decision_artifact.source_snapshots"),
            ("execution_snapshot", ["unexpected", "list"], "decision_artifact.execution_snapshot"),
        )
        for field, value, path in cases:
            with self.subTest(field=field):
                row = _snapshot_row()
                row["decision_artifact"][field] = value

                result = build_replay_decision_input_v1(row)

                self.assertTrue(result.ok)
                assert result.record is not None
                self.assertIn(
                    {"path": path, "category": "unallowlisted"},
                    result.record["sanitization"]["omitted_fields"],
                )
                if field == "source_snapshots":
                    self.assertNotIn(value, result.record["source_inputs"]["source_snapshots"])
                else:
                    self.assertNotIn("execution_snapshot", result.record["market"])

    def test_default_mode_audits_malformed_optional_root_fields_without_rejecting_row(self):
        cases = (
            ("collector_provenance", ["malformed-provenance"], "collector_provenance"),
            ("replay_derived_features", ["malformed-derived-features"], "replay_derived_features"),
            (
                "replay_derived_features",
                {"schema_version": "weather-features-v2", "values": ["malformed-derived-values"]},
                "replay_derived_features.values",
            ),
            (
                "replay_derived_features",
                {"schema_version": "weather-features-v2"},
                "replay_derived_features.values",
            ),
            ("replay_decision_context", ["malformed-decision-context"], "replay_decision_context"),
        )
        for field, value, path in cases:
            with self.subTest(field=field, path=path):
                row = _snapshot_row(patch={field: value})

                result = build_replay_decision_input_v1(row)

                self.assertTrue(result.ok)
                assert result.record is not None
                self.assertIn(
                    {"path": path, "category": "unallowlisted"},
                    result.record["sanitization"]["omitted_fields"],
                )
                self.assertNotIn("malformed", json.dumps(result.record, sort_keys=True))
                if field == "collector_provenance":
                    self.assertNotIn("raw_payload_sha256", result.record["snapshot_provenance"])
                elif path == "replay_derived_features.values":
                    self.assertNotIn("derived_features", result.record)
                elif field == "replay_derived_features":
                    self.assertNotIn("derived_features", result.record)
                else:
                    self.assertNotIn("decision_context", result.record)

    def test_default_mode_omits_and_audits_malformed_optional_mapping_members(self):
        row = _snapshot_row()
        row["collector_provenance"].update(
            {
                "raw_payload_sha256": "outcome",
                "collector_index_entry_sha256": "not-a-sha256",
            }
        )
        row["replay_derived_features"].update(
            {
                "schema_version": "finalOutcome",
                "values": {
                    "forecast_high_f": "result",
                    "threshold_f": 80.0,
                    "station_id": "resolution",
                    "question_side": "finalAction",
                    "finalOutcome": "outcome",
                },
            }
        )
        row["replay_decision_context"].update(
            {
                "strategy_input_schema_version": ["future"],
                "policy_config_sha256": "result",
                "strategy_logic_sha256": _sha256("valid-logic"),
                "finalOutcome": "outcome",
            }
        )

        result = build_replay_decision_input_v1(row)

        self.assertTrue(result.ok)
        assert result.record is not None
        replay_data = {
            key: result.record.get(key)
            for key in ("snapshot_provenance", "derived_features", "decision_context")
        }
        serialized = json.dumps(replay_data, sort_keys=True)
        for leaked_value in (
            "outcome", "result", "resolution", "finalOutcome", "finalAction", "future", "not-a-sha256",
        ):
            self.assertNotIn(leaked_value, serialized)
        self.assertNotIn("raw_payload_sha256", result.record["snapshot_provenance"])
        self.assertNotIn("collector_index_entry_sha256", result.record["snapshot_provenance"])
        self.assertEqual(
            result.record["derived_features"],
            {"values": {"threshold_f": 80.0}},
        )
        self.assertNotIn("strategy_input_schema_version", result.record["decision_context"])
        self.assertEqual(
            result.record["decision_context"]["strategy_logic_sha256"],
            _sha256("valid-logic"),
        )
        omitted = result.record["sanitization"]["omitted_fields"]
        self.assertIn(
            {"path": "collector_provenance.raw_payload_sha256", "category": "unallowlisted"},
            omitted,
        )
        self.assertIn(
            {"path": "collector_provenance.collector_index_entry_sha256", "category": "unallowlisted"},
            omitted,
        )
        self.assertIn(
            {"path": "replay_derived_features.schema_version", "category": "unallowlisted"},
            omitted,
        )
        self.assertIn(
            {"path": "replay_derived_features.values.forecast_high_f", "category": "outcome_or_future"},
            omitted,
        )
        self.assertIn(
            {"path": "replay_derived_features.values.station_id", "category": "outcome_or_future"},
            omitted,
        )
        self.assertIn(
            {"path": "replay_derived_features.values.question_side", "category": "recorded_decision"},
            omitted,
        )
        self.assertIn(
            {"path": "replay_derived_features.values.finalOutcome", "category": "outcome_or_future"},
            omitted,
        )
        self.assertIn(
            {"path": "replay_decision_context.strategy_input_schema_version", "category": "unallowlisted"},
            omitted,
        )
        self.assertIn(
            {"path": "replay_decision_context.policy_config_sha256", "category": "unallowlisted"},
            omitted,
        )
        self.assertIn(
            {"path": "replay_decision_context.finalOutcome", "category": "outcome_or_future"},
            omitted,
        )

    def test_default_mode_omits_each_invalid_optional_schema_version_type(self):
        cases = (
            ("replay_derived_features", "schema_version", ["future"]),
            ("replay_derived_features", "schema_version", {"value": "result"}),
            ("replay_derived_features", "schema_version", 3),
            ("replay_derived_features", "schema_version", ""),
            ("replay_decision_context", "strategy_input_schema_version", ["future"]),
            ("replay_decision_context", "strategy_input_schema_version", {"value": "result"}),
            ("replay_decision_context", "strategy_input_schema_version", 3),
            ("replay_decision_context", "strategy_input_schema_version", ""),
        )
        for mapping_field, schema_field, invalid_value in cases:
            with self.subTest(mapping_field=mapping_field, invalid_value=invalid_value):
                row = _snapshot_row()
                row[mapping_field][schema_field] = invalid_value

                result = build_replay_decision_input_v1(row)

                self.assertTrue(result.ok)
                assert result.record is not None
                record_field = "derived_features" if mapping_field == "replay_derived_features" else "decision_context"
                self.assertNotIn(schema_field, result.record[record_field])
                self.assertIn(
                    {"path": f"{mapping_field}.{schema_field}", "category": "unallowlisted"},
                    result.record["sanitization"]["omitted_fields"],
                )

    def test_default_mode_retains_valid_optional_mapping_members(self):
        row = _snapshot_row()

        result = build_replay_decision_input_v1(row)

        self.assertTrue(result.ok)
        assert result.record is not None
        self.assertEqual(
            result.record["snapshot_provenance"]["raw_payload_sha256"],
            _sha256("raw-payload"),
        )
        self.assertEqual(
            result.record["derived_features"]["schema_version"],
            "weather-features-v2",
        )
        self.assertEqual(
            result.record["decision_context"]["strategy_input_schema_version"],
            "weather-input-v3",
        )
        self.assertEqual(
            result.record["decision_context"]["policy_config_sha256"],
            _sha256("policy"),
        )

    def test_default_mode_audits_malformed_ids_and_uses_raw_hash_legacy_identities(self):
        cases = (
            ("shared_snapshot_id", ["malformed-snapshot-id"]),
            ("shared_candidate_id", {"value": "malformed-candidate-id"}),
        )
        for field, value in cases:
            with self.subTest(field=field):
                row = _snapshot_row(patch={field: value})

                result = build_replay_decision_input_v1(row)

                self.assertTrue(result.ok)
                assert result.record is not None
                expected_hash = hashlib.sha256(
                    json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                ).hexdigest()
                self.assertEqual(result.record["input_mode"], "legacy_sanitized_v1")
                self.assertEqual(
                    result.record["decision_key"]["shared_snapshot_id"],
                    f"legacy-snapshot-{expected_hash}",
                )
                self.assertEqual(
                    result.record["decision_key"]["shared_candidate_id"],
                    f"legacy-candidate-{expected_hash}",
                )
                self.assertIn(
                    {"path": field, "category": "unallowlisted"},
                    result.record["sanitization"]["omitted_fields"],
                )
                self.assertNotIn("malformed", json.dumps(result.record, sort_keys=True))

    def test_default_mode_requires_source_name_and_weather_source_snapshot(self):
        cases = (
            ("empty source", lambda row: row["decision_artifact"]["source_context"].update({"source": ""}), "decision_artifact.source_context.source"),
            ("missing weather snapshot", lambda row: row["decision_artifact"]["source_context"]["data"].pop("weather_source_snapshot"), "decision_artifact.source_context.data.weather_source_snapshot"),
        )
        for name, mutate, path in cases:
            with self.subTest(name=name):
                row = _snapshot_row()
                mutate(row)

                result = build_replay_decision_input_v1(row)

                self.assertFalse(result.ok)
                self.assertEqual(result.errors[0].code, "missing_required_field")
                self.assertEqual(result.errors[0].path, path)

    def test_legacy_row_without_v1_context_uses_deterministic_raw_hash_identities(self):
        row = _snapshot_row()
        for field in ("shared_snapshot_id", "shared_candidate_id", "collector_provenance", "replay_decision_context", "replay_derived_features"):
            row.pop(field)

        first = build_replay_decision_input_v1(row)
        second = build_replay_decision_input_v1(row)

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        assert first.record is not None
        assert second.record is not None
        expected_hash = hashlib.sha256(
            json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        self.assertEqual(first.record["input_mode"], "legacy_sanitized_v1")
        self.assertEqual(first.record["decision_key"]["shared_snapshot_id"], f"legacy-snapshot-{expected_hash}")
        self.assertEqual(first.record["decision_key"]["shared_candidate_id"], f"legacy-candidate-{expected_hash}")
        self.assertEqual(first.record["decision_key"], second.record["decision_key"])
        self.assertEqual(first.record["snapshot_provenance"]["raw_row_sha256"], expected_hash)

    def test_default_mode_fails_closed_for_missing_identity_price_or_source_context(self):
        cases = (
            ("market_id", {"market_id": ""}, "missing_required_field"),
            ("price", {"yes_price": None, "no_price": None}, "invalid_decision_time_price"),
            ("source", {"decision_artifact": {}}, "missing_required_field"),
        )
        for name, patch, expected_code in cases:
            with self.subTest(name=name):
                row = _snapshot_row(patch=patch)
                if name == "price":
                    row["decision_artifact"]["execution_snapshot"].pop("best_yes_ask")
                    row["decision_artifact"]["execution_snapshot"].pop("best_no_ask")
                result = build_replay_decision_input_v1(row)
                self.assertFalse(result.ok)
                self.assertTrue(any(error.code == expected_code for error in result.errors), result.errors)


if __name__ == "__main__":
    unittest.main()
