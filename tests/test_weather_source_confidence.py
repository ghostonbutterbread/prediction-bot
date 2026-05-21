import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from bot.file_ops import append_jsonl, load_jsonl
from bot.weather.source_reliability import build_rolling_source_reliability_rows
from bot.weather.source_confidence import (
    GRADE_STRONG_NO,
    GRADE_STRONG_YES,
    GRADE_INSUFFICIENT_HISTORY,
    GRADE_NO_SOURCE_DATA,
    build_source_confidence_row,
    normalize_source_observations,
    summarize_source_confidence_rows,
)
from scripts.weather_source_confidence import main as source_confidence_main


class WeatherSourceConfidenceTests(unittest.TestCase):
    def test_trusted_support_allows_candidate_with_strong_yes_grade(self):
        row = {
            "shared_candidate_id": "candidate-support",
            "market_id": "market-support",
            "observed_at": "2026-05-17T13:00:00+00:00",
            "city_id": "seattle_wa",
            "market_kind": "high",
            "contract_shape": "tail",
            "question_side": "above",
            "predicted_outcome": "YES",
            "threshold": 70.0,
            "forecast_target": "daily_high",
            "source_details": [
                {
                    "source_name": "NWS",
                    "source_family": "noaa_nws",
                    "forecast_high": 72.0,
                }
            ],
        }
        reliability_rows = [
            {
                "source_id": "nws",
                "source_name": "nws",
                "city_id": "seattle_wa",
                "market_kind": "high",
                "contract_shape": "tail",
                "sample_count": 120,
                "threshold_direction_accuracy": 0.95,
                "tier": "trusted",
                "mae": 1.4,
                "mean_bias": -0.2,
            }
        ]

        result = build_source_confidence_row(row, reliability_table=reliability_rows)

        self.assertEqual(result["source_grade"], GRADE_STRONG_YES)
        self.assertEqual(result["source_direction"], "YES")
        self.assertEqual(result["recommended_action"], "ALLOW")
        self.assertEqual(result["reason_code"], "trusted_support")
        self.assertEqual(result["weighted_support"], 1.0)
        self.assertEqual(result["weighted_dissent"], 0.0)
        self.assertEqual(len(result["sources_used"]), 1)
        self.assertEqual(result["sources_used"][0]["vote"], "support")
        self.assertEqual(result["sources_used"][0]["specificity"], "source_id+city_id+market_kind+contract_shape")
        self.assertEqual(result["sources_used"][0]["threshold_direction_accuracy"], 0.95)
        self.assertEqual(result["sources_used"][0]["mae_f"], 1.4)
        self.assertEqual(result["sources_used"][0]["bias_f"], -0.2)

    def test_trusted_dissent_is_strong_no_and_blocks_by_policy(self):
        row = {
            "shared_candidate_id": "candidate-dissent",
            "market_id": "market-dissent",
            "observed_at": "2026-05-17T13:00:00+00:00",
            "city_id": "seattle_wa",
            "market_kind": "high",
            "contract_shape": "tail",
            "question_side": "above",
            "predicted_outcome": "YES",
            "threshold": 70.0,
            "forecast_target": "daily_high",
            "source_details": [{"source_name": "NWS", "forecast_high": 68.0}],
        }
        reliability_rows = [
            {
                "source_id": "nws",
                "source_name": "nws",
                "city_id": "seattle_wa",
                "market_kind": "high",
                "contract_shape": "tail",
                "sample_count": 140,
                "threshold_direction_accuracy": 0.94,
                "tier": "trusted",
            }
        ]

        result = build_source_confidence_row(row, reliability_table=reliability_rows)

        self.assertEqual(result["source_grade"], GRADE_STRONG_NO)
        self.assertEqual(result["source_direction"], "NO")
        self.assertEqual(result["recommended_action"], "BLOCK")
        self.assertEqual(result["reason_code"], "trusted_dissent")
        self.assertEqual(result["weighted_support"], 0.0)
        self.assertEqual(result["weighted_dissent"], 1.0)
        self.assertEqual(result["agreement_state"], "unanimous_dissent")

    def test_backoff_uses_global_source_reliability_when_specific_slice_missing(self):
        row = {
            "shared_candidate_id": "candidate-backoff",
            "market_id": "market-backoff",
            "observed_at": "2026-05-17T13:00:00+00:00",
            "city_id": "seattle_wa",
            "market_kind": "high",
            "contract_shape": "tail",
            "question_side": "above",
            "predicted_outcome": "YES",
            "threshold": 70.0,
            "forecast_target": "daily_high",
            "source_details": [{"source_name": "NWS", "forecast_high": 74.0}],
        }
        reliability_rows = [
            {
                "source_id": "nws",
                "source_name": "nws",
                "city_id": "unknown",
                "market_kind": "unknown",
                "contract_shape": "unknown",
                "sample_count": 220,
                "threshold_direction_accuracy": 0.96,
                "tier": "strong_trusted",
            }
        ]

        result = build_source_confidence_row(row, reliability_table=reliability_rows)

        self.assertEqual(result["source_grade"], GRADE_STRONG_YES)
        self.assertEqual(result["recommended_action"], "ALLOW")
        self.assertEqual(result["sources_used"][0]["specificity"], "source_id+unknown+unknown+unknown")
        self.assertEqual(
            result["sources_used"][0]["backoff_path"],
            " -> ".join(
                [
                    "source_id+city_id+market_kind+contract_shape",
                    "source_id+city_id+market_kind+unknown",
                    "source_id+city_id+unknown+unknown",
                    "source_id+unknown+market_kind+contract_shape",
                    "source_id+unknown+unknown+unknown",
                ]
            ),
        )

    def test_trusted_support_for_no_candidate_keeps_source_direction_no(self):
        row = {
            "shared_candidate_id": "candidate-no-support",
            "market_id": "market-no-support",
            "city_id": "seattle_wa",
            "market_kind": "high",
            "contract_shape": "tail",
            "question_side": "above",
            "predicted_outcome": "NO",
            "threshold": 70.0,
            "forecast_target": "daily_high",
            "source_details": [{"source_name": "NWS", "forecast_high": 68.0}],
        }
        reliability_rows = [
            {
                "source_id": "nws",
                "source_name": "nws",
                "city_id": "seattle_wa",
                "market_kind": "high",
                "contract_shape": "tail",
                "sample_count": 140,
                "threshold_direction_accuracy": 0.94,
                "tier": "trusted",
            }
        ]

        result = build_source_confidence_row(row, reliability_table=reliability_rows)

        self.assertEqual(result["source_grade"], GRADE_STRONG_NO)
        self.assertEqual(result["source_direction"], "NO")
        self.assertEqual(result["recommended_action"], "ALLOW")
        self.assertEqual(result["reason_code"], "trusted_support")
        self.assertEqual(result["source_confidence_score"], 1.0)
        summary = summarize_source_confidence_rows([result])
        self.assertEqual(summary["source_direction_counts"], {"NO": 1})
        self.assertEqual(summary["recommended_action_counts"], {"ALLOW": 1})
        self.assertEqual(result["sources_used"][0]["predicted_outcome"], "NO")
        self.assertEqual(result["sources_used"][0]["vote"], "support")

    def test_live_sources_array_is_source_data_not_no_source_data(self):
        row = {
            "shared_candidate_id": "candidate-sources-array",
            "market_id": "market-sources-array",
            "market_kind": "high",
            "forecast_target": "daily_high",
            "decision_artifact": {
                "strategy_trace": {
                    "raw_signals": {
                        "live": {
                            "data": {
                                "sources": ["nws", "open-meteo"],
                            }
                        }
                    }
                }
            },
        }

        result = build_source_confidence_row(row)

        self.assertEqual(result["source_grade"], GRADE_INSUFFICIENT_HISTORY)
        self.assertEqual(result["data_quality"]["source_observation_count"], 2)
        self.assertEqual([obs["source_id"] for obs in result["source_observations"]], ["nws", "open_meteo"])

    def test_source_family_self_conflict_is_excluded_not_global_disagree(self):
        row = {
            "shared_candidate_id": "candidate-family-conflict",
            "market_id": "market-family-conflict",
            "city_id": "seattle_wa",
            "market_kind": "high",
            "contract_shape": "tail",
            "question_side": "above",
            "predicted_outcome": "YES",
            "threshold": 70.0,
            "forecast_target": "daily_high",
            "source_details": [
                {"source_name": "NWS grid", "source_id": "nws_grid", "source_family": "noaa_nws", "forecast_high": 72.0},
                {"source_name": "NWS station", "source_id": "nws_station", "source_family": "noaa_nws", "forecast_high": 68.0},
            ],
        }
        reliability_rows = [
            {
                "source_id": "nws_grid",
                "source_name": "NWS grid",
                "city_id": "seattle_wa",
                "market_kind": "high",
                "contract_shape": "tail",
                "sample_count": 120,
                "threshold_direction_accuracy": 0.95,
                "tier": "trusted",
            },
            {
                "source_id": "nws_station",
                "source_name": "NWS station",
                "city_id": "seattle_wa",
                "market_kind": "high",
                "contract_shape": "tail",
                "sample_count": 120,
                "threshold_direction_accuracy": 0.95,
                "tier": "trusted",
            },
        ]

        result = build_source_confidence_row(row, reliability_table=reliability_rows)

        self.assertEqual(result["source_grade"], GRADE_INSUFFICIENT_HISTORY)
        self.assertEqual(result["recommended_action"], "BLOCK")
        self.assertEqual(result["sources_used"], [])
        self.assertFalse(result["data_quality"]["reliability_history_available"])
        self.assertEqual(result["sources_excluded"][0]["reason_code"], "source_family_self_conflict")

    def test_unknown_candidate_dimensions_are_reported_as_unknown_backoff_specificity(self):
        row = {
            "shared_candidate_id": "candidate-unknown-backoff",
            "market_id": "market-unknown-backoff",
            "city_id": "unknown",
            "market_kind": "high",
            "contract_shape": "unknown",
            "question_side": "above",
            "predicted_outcome": "YES",
            "threshold": 70.0,
            "forecast_target": "daily_high",
            "source_details": [{"source_name": "NWS", "forecast_high": 73.0}],
        }
        reliability_rows = [
            {
                "source_id": "nws",
                "source_name": "nws",
                "city_id": "unknown",
                "market_kind": "high",
                "contract_shape": "unknown",
                "sample_count": 120,
                "threshold_direction_accuracy": 0.95,
                "tier": "trusted",
            }
        ]

        result = build_source_confidence_row(row, reliability_table=reliability_rows)

        self.assertEqual(result["source_grade"], GRADE_STRONG_YES)
        self.assertEqual(result["sources_used"][0]["specificity"], "source_id+unknown+market_kind+unknown")
        self.assertEqual(result["sources_used"][0]["backoff_path"], "source_id+unknown+market_kind+unknown")

    def test_below_question_side_does_not_invert_yes_no_vote(self):
        row = {
            "shared_candidate_id": "candidate-below",
            "market_id": "market-below",
            "city_id": "seattle_wa",
            "market_kind": "low",
            "contract_shape": "tail",
            "question_side": "below",
            "predicted_outcome": "YES",
            "threshold": 60.0,
            "forecast_target": "daily_low",
            "source_details": [{"source_name": "NWS", "forecast_low": 58.0}],
        }
        reliability_rows = [
            {
                "source_id": "nws",
                "source_name": "nws",
                "city_id": "seattle_wa",
                "market_kind": "low",
                "contract_shape": "tail",
                "sample_count": 120,
                "threshold_direction_accuracy": 0.95,
                "tier": "trusted",
            }
        ]

        result = build_source_confidence_row(row, reliability_table=reliability_rows)

        self.assertEqual(result["source_grade"], GRADE_STRONG_YES)
        self.assertEqual(result["sources_used"][0]["vote"], "support")
        self.assertEqual(result["sources_used"][0]["predicted_outcome"], "YES")

    def test_builds_no_source_data_blocker_from_minimal_candidate(self):
        row = {
            "shared_candidate": {
                "candidate_id": "candidate-1",
                "market_id": "market-1",
                "observed_at": "2026-05-17T12:00:00+00:00",
            }
        }

        result = build_source_confidence_row(row)

        self.assertEqual(result["schema"], "weather_source_confidence_v1")
        self.assertEqual(result["shared_candidate_id"], "candidate-1")
        self.assertEqual(result["market_id"], "market-1")
        self.assertEqual(result["source_grade"], GRADE_NO_SOURCE_DATA)
        self.assertEqual(result["recommended_action"], "BLOCK")
        self.assertEqual(result["data_quality"]["source_observation_count"], 0)
        self.assertEqual(result["source_observations"], [])
        self.assertTrue(result["engine_inputs_hash"].startswith("sha256:"))

    def test_extracts_source_details_into_observations_and_marks_insufficient_history(self):
        row = {
            "shared_candidate_id": "candidate-2",
            "market_id": "market-2",
            "observed_at": "2026-05-17T13:00:00+00:00",
            "market_kind": "high",
            "forecast_target": "daily_high",
            "source_details": [
                {
                    "source_name": "NWS",
                    "source_family": "noaa_nws",
                    "source_location_basis": "gridpoint",
                    "forecast_high": 88.0,
                    "temperature_unit": "F",
                    "forecast_date": "2026-05-18",
                    "fetched_at": "2026-05-17T12:30:00+00:00",
                    "adapter_version": "nws_v1",
                    "known_at_time_assertion": True,
                }
            ],
        }

        observations = normalize_source_observations(row)
        result = build_source_confidence_row(row)

        self.assertGreater(len(observations), 0)
        self.assertEqual(observations[0]["source_id"], "nws")
        self.assertEqual(observations[0]["forecast_temp_f"], 88.0)
        self.assertEqual(observations[0]["normalizer_version"], "weather_source_observation_v1")
        self.assertEqual(result["source_grade"], GRADE_INSUFFICIENT_HISTORY)
        self.assertEqual(result["data_quality"]["source_observation_count"], 1)
        self.assertTrue(result["data_quality"]["known_at_time"])

    def test_extracts_live_candidate_strategy_trace_source_details(self):
        row = {
            "shared_candidate_id": "candidate-live",
            "market_id": "market-live",
            "observed_at": "2026-05-17T13:00:00+00:00",
            "decision_artifact": {
                "strategy_trace": {
                    "raw_signals": {
                        "live": {
                            "data": {
                                "market_kind": "high",
                                "forecast_target": "daily_high",
                                "source_details": [
                                    {"source_name": "open-meteo", "forecast_high": 86.0},
                                    {"source_name": "nws", "forecast_high": 87.0},
                                ],
                            }
                        }
                    }
                }
            },
        }

        result = build_source_confidence_row(row)

        self.assertEqual(result["source_grade"], GRADE_INSUFFICIENT_HISTORY)
        self.assertEqual(result["data_quality"]["source_observation_count"], 2)
        self.assertEqual(
            {observation["source_id"] for observation in result["source_observations"]},
            {"open_meteo", "nws"},
        )

    def test_cli_writes_one_row_per_input_and_prints_grade_counts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "input.jsonl"
            output_path = root / "output.jsonl"

            append_jsonl(
                input_path,
                {
                    "shared_candidate": {
                        "candidate_id": "candidate-1",
                        "market_id": "market-1",
                        "observed_at": "2026-05-17T12:00:00+00:00",
                    }
                },
            )
            append_jsonl(
                input_path,
                {
                    "shared_candidate_id": "candidate-2",
                    "market_id": "market-2",
                    "market_kind": "high",
                    "forecast_target": "daily_high",
                    "source_details": [{"source_name": "NWS", "forecast_high": 87.0}],
                },
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = source_confidence_main(
                    ["--input", str(input_path), "--output", str(output_path)]
                )

            stored_rows = load_jsonl(output_path)
            summary = summarize_source_confidence_rows(stored_rows)

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(stored_rows), 2)
            self.assertEqual(summary["no_source_data"], 1)
            self.assertEqual(summary["insufficient_history"], 1)
            self.assertIn("grade_counts=", stdout.getvalue())
            self.assertIn("NO_SOURCE_DATA", stdout.getvalue())
            self.assertIn("INSUFFICIENT_HISTORY", stdout.getvalue())

    def test_cli_accepts_reliability_rows_and_emits_directional_grade(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "input.jsonl"
            output_path = root / "output.jsonl"
            reliability_path = root / "reliability.json"
            summary_path = root / "summary.json"

            append_jsonl(
                input_path,
                {
                    "shared_candidate_id": "candidate-cli",
                    "market_id": "market-cli",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "question_side": "above",
                    "predicted_outcome": "YES",
                    "threshold": 70.0,
                    "forecast_target": "daily_high",
                    "source_details": [{"source_name": "NWS", "forecast_high": 72.0}],
                },
            )
            reliability_path.write_text(
                json.dumps(
                    [
                        {
                            "source_id": "nws",
                            "source_name": "nws",
                            "city_id": "seattle_wa",
                            "market_kind": "high",
                            "contract_shape": "tail",
                            "sample_count": 100,
                            "threshold_direction_accuracy": 0.95,
                            "tier": "trusted",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = source_confidence_main(
                    [
                        "--input",
                        str(input_path),
                        "--output",
                        str(output_path),
                        "--reliability",
                        str(reliability_path),
                        "--summary-output",
                        str(summary_path),
                    ]
                )

            stored_rows = load_jsonl(output_path)
            summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))

            self.assertEqual(exit_code, 0)
            self.assertEqual(stored_rows[0]["source_grade"], GRADE_STRONG_YES)
            self.assertEqual(stored_rows[0]["recommended_action"], "ALLOW")
            expected_summary_keys = {
                "schema",
                "row_count",
                "grade_counts",
                "reason_counts",
                "recommended_action_counts",
                "agreement_state_counts",
                "source_direction_counts",
                "confidence_type_counts",
                "confidence_score_range",
                "sources_used_count",
                "sources_excluded_count",
                "no_source_data",
                "insufficient_history",
                "per_source_counts",
                "per_source_used_counts",
                "source_exclusion_reason_counts",
                "source_used_vote_counts",
                "source_used_tier_counts",
                "source_excluded_tier_counts",
                "run_config",
            }
            self.assertEqual(set(summary_payload), expected_summary_keys)
            self.assertEqual(summary_payload["schema"], "weather_source_confidence_summary_v1")
            self.assertEqual(summary_payload["run_config"]["reliability_mode"], "static_reliability_rows")
            self.assertFalse(summary_payload["run_config"]["has_source_outcome_ledger"])
            self.assertEqual(summary_payload["row_count"], 1)
            self.assertEqual(summary_payload["recommended_action_counts"], {"ALLOW": 1})
            self.assertEqual(summary_payload["reason_counts"], {"trusted_support": 1})
            self.assertEqual(summary_payload["sources_used_count"], 1)
            self.assertEqual(summary_payload["source_used_vote_counts"], {"support": 1})
            self.assertEqual(summary_payload["source_used_tier_counts"], {"trusted": 1})
            self.assertNotIn("rows", summary_payload)
            self.assertNotIn("source_observation_rows", summary_payload)
            self.assertIn("actions=", stdout.getvalue())
            self.assertIn("reasons=", stdout.getvalue())
            self.assertIn("STRONG_YES", stdout.getvalue())


    def test_bucket_question_uses_inferred_range_bounds_for_source_vote(self):
        row = {
            "shared_candidate_id": "candidate-bucket-range",
            "city_id": "seattle_wa",
            "shared_candidate": {
                "market": {
                    "question": "Will the high temp in Seattle be 93-94° on May 17, 2026?",
                    "route": {"evidence": {"shape": "bucket"}},
                }
            },
            "predicted_outcome": "YES",
            "source_details": [{"source_name": "NWS", "forecast_high": 93.5}],
        }
        reliability_rows = [
            {
                "source_id": "nws",
                "source_name": "nws",
                "city_id": "seattle_wa",
                "market_kind": "high",
                "contract_shape": "bucket",
                "sample_count": 100,
                "threshold_direction_accuracy": 0.95,
                "tier": "trusted",
            }
        ]

        result = build_source_confidence_row(row, reliability_table=reliability_rows)

        self.assertEqual(result["question_side"], "range")
        self.assertEqual(result["threshold_low"], 93.0)
        self.assertEqual(result["threshold_high"], 94.0)
        self.assertEqual(result["source_direction"], "YES")
        self.assertEqual(result["source_grade"], GRADE_STRONG_YES)
        self.assertEqual(result["recommended_action"], "ALLOW")
        self.assertEqual(result["sources_used"][0]["predicted_outcome"], "YES")

    def test_bucket_question_source_outside_range_votes_no(self):
        row = {
            "shared_candidate_id": "candidate-bucket-outside",
            "city_id": "seattle_wa",
            "shared_candidate": {
                "market": {
                    "question": "Will the high temp in Seattle be 93-94° on May 17, 2026?",
                    "route": {"evidence": {"shape": "bucket"}},
                }
            },
            "predicted_outcome": "YES",
            "source_details": [{"source_name": "NWS", "forecast_high": 95.0}],
        }
        reliability_rows = [
            {
                "source_id": "nws",
                "source_name": "nws",
                "city_id": "seattle_wa",
                "market_kind": "high",
                "contract_shape": "bucket",
                "sample_count": 100,
                "threshold_direction_accuracy": 0.95,
                "tier": "trusted",
            }
        ]

        result = build_source_confidence_row(row, reliability_table=reliability_rows)

        self.assertEqual(result["source_direction"], "NO")
        self.assertEqual(result["source_grade"], GRADE_STRONG_NO)
        self.assertEqual(result["recommended_action"], "BLOCK")
        self.assertEqual(result["reason_code"], "trusted_dissent")
        self.assertEqual(result["sources_used"][0]["predicted_outcome"], "NO")

    def test_market_kind_infers_high_from_shared_candidate_question(self):
        row = {
            "shared_candidate_id": "candidate-question-kind",
            "shared_candidate": {
                "market": {"question": "Will the high temp in Austin be 93-94° on May 17, 2026?"}
            },
            "threshold": 94.0,
            "contract_shape": "bucket",
            "source_details": [{"source_name": "NWS", "forecast_high": 91.0}],
        }

        result = build_source_confidence_row(row, reliability_table=[])

        self.assertEqual(result["market_kind"], "high")
        self.assertEqual(result["source_observations"][0]["forecast_temp_f"], 91.0)

    def test_weather_context_fields_load_from_recorded_live_source_data(self):
        row = {
            "shared_candidate_id": "candidate-recorded-live-data",
            "decision_artifact": {
                "strategy_trace": {
                    "raw_signals": {
                        "live": {
                            "data": {
                                "threshold": 70.0,
                                "market_kind": "high",
                                "contract_shape": "tail",
                                "question_side": "above",
                                "predicted_outcome": "YES",
                                "forecast_target": "daily_high",
                                "source_details": [{"source_name": "NWS", "forecast_high": 72.0}],
                            }
                        }
                    }
                }
            },
        }
        reliability_rows = [
            {
                "source_id": "nws",
                "source_name": "nws",
                "city_id": "unknown",
                "market_kind": "high",
                "contract_shape": "tail",
                "sample_count": 100,
                "threshold_direction_accuracy": 0.95,
                "tier": "trusted",
            }
        ]

        result = build_source_confidence_row(row, reliability_table=reliability_rows)

        self.assertEqual(result["threshold"], 70.0)
        self.assertEqual(result["market_kind"], "high")
        self.assertEqual(result["contract_shape"], "tail")
        self.assertEqual(result["question_side"], "above")
        self.assertEqual(result["forecast_target"], "daily_high")
        self.assertEqual(result["predicted_outcome"], "YES")
        self.assertEqual(result["source_grade"], GRADE_STRONG_YES)
        self.assertEqual(result["recommended_action"], "ALLOW")

    def test_cli_report_output_is_aggregate_only_markdown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "input.jsonl"
            output_path = root / "output.jsonl"
            reliability_path = root / "reliability.json"
            report_path = root / "report.md"

            append_jsonl(
                input_path,
                {
                    "shared_candidate_id": "candidate-report-should-not-leak",
                    "market_id": "market-report-should-not-leak",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "question_side": "above",
                    "predicted_outcome": "YES",
                    "threshold": 70.0,
                    "forecast_target": "daily_high",
                    "source_details": [{"source_name": "NWS", "forecast_high": 72.0}],
                },
            )
            reliability_path.write_text(
                json.dumps(
                    [
                        {
                            "source_id": "nws",
                            "source_name": "nws",
                            "city_id": "seattle_wa",
                            "market_kind": "high",
                            "contract_shape": "tail",
                            "sample_count": 100,
                            "threshold_direction_accuracy": 0.95,
                            "tier": "trusted",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            exit_code = source_confidence_main(
                [
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                    "--reliability",
                    str(reliability_path),
                    "--report-output",
                    str(report_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("# Weather Source Confidence Report", report)
            self.assertIn("Source-only beta/shadow audit report", report)
            self.assertIn("not a trading, Kelly, PnL, wallet, or execution input", report)
            self.assertIn("- reliability_mode: static_reliability_rows", report)
            self.assertIn("## Grade counts", report)
            self.assertIn("- STRONG_YES: 1", report)
            self.assertIn("## Source used vote counts", report)
            self.assertIn("- support: 1", report)
            self.assertIn("## Source used tier counts", report)
            self.assertIn("- trusted: 1", report)
            self.assertIn("## Per-source observation counts", report)
            self.assertIn("- nws: 1", report)
            for forbidden in (
                "candidate-report-should-not-leak",
                "market-report-should-not-leak",
                "source_observations",
                "sources_used",
                "sources_excluded",
                "threshold",
                "forecast_high",
                "forecast_temp_f",
            ):
                self.assertNotIn(forbidden, report)

    def test_cli_report_output_renders_empty_counts_as_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "input.jsonl"
            output_path = root / "output.jsonl"
            report_path = root / "report.md"

            append_jsonl(input_path, {"shared_candidate_id": "candidate-empty-report", "source_details": []})

            exit_code = source_confidence_main(
                [
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                    "--report-output",
                    str(report_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("- NO_SOURCE_DATA: 1", report)
            self.assertIn("- no_source_data: 1", report)
            self.assertIn("## Source exclusion reason counts\n\n- none", report)
            self.assertIn("## Source used vote counts\n\n- none", report)
            self.assertIn("## Source used tier counts\n\n- none", report)
            self.assertIn("## Source excluded tier counts\n\n- none", report)
            self.assertIn("## Per-source observation counts\n\n- none", report)
            self.assertIn("## Per-source used counts\n\n- none", report)


    def test_rolling_reliability_merges_source_name_case_for_same_source_id(self):
        rows = []
        for index, source_name in enumerate(["NWS", "nws"]):
            rows.append(
                {
                    "source_id": "nws",
                    "source_name": source_name,
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "known_after": f"2026-05-16T0{index}:00:00+00:00",
                    "direction_correct": True,
                    "absolute_error_f": 1.0,
                    "bias_f": 0.0,
                    "eligible_for_reliability": True,
                    "observation_id": f"obs-{index}",
                }
            )

        reliability_rows = build_rolling_source_reliability_rows(
            rows,
            "2026-05-17T00:00:00+00:00",
            min_samples=2,
            trusted_samples=2,
        )

        self.assertEqual(len(reliability_rows), 1)
        self.assertEqual(reliability_rows[0]["source_id"], "nws")
        self.assertEqual(reliability_rows[0]["sample_count"], 2)
        self.assertEqual(reliability_rows[0]["tier"], "strong_trusted")
        self.assertEqual(reliability_rows[0]["source_name"], "nws")

    def test_reliability_lookup_does_not_cross_source_name_into_other_source_id(self):
        row = {
            "shared_candidate_id": "candidate-source-collision",
            "market_id": "market-source-collision",
            "observed_at": "2026-05-17T13:00:00+00:00",
            "city_id": "seattle_wa",
            "market_kind": "high",
            "contract_shape": "tail",
            "question_side": "above",
            "predicted_outcome": "YES",
            "threshold": 70.0,
            "forecast_target": "daily_high",
            "source_details": [
                {"source_id": "alias_source", "source_name": "NWS", "forecast_high": 72.0},
                {"source_id": "nws", "source_name": "Distinct Provider", "forecast_high": 65.0},
            ],
        }
        reliability_rows = [
            {
                "source_id": "alias_source",
                "source_name": "nws",
                "city_id": "seattle_wa",
                "market_kind": "high",
                "contract_shape": "tail",
                "sample_count": 200,
                "threshold_direction_accuracy": 0.99,
                "tier": "strong_trusted",
            },
            {
                "source_id": "nws",
                "source_name": "Distinct Provider",
                "city_id": "seattle_wa",
                "market_kind": "high",
                "contract_shape": "tail",
                "sample_count": 200,
                "threshold_direction_accuracy": 0.0,
                "tier": "excluded",
            },
        ]

        result = build_source_confidence_row(row, reliability_table=reliability_rows)

        used_by_id = {source["source_id"]: source for source in result["sources_used"]}
        excluded_by_id = {source["source_id"]: source for source in result["sources_excluded"]}
        self.assertEqual(used_by_id["alias_source"]["tier"], "strong_trusted")
        self.assertEqual(used_by_id["alias_source"]["vote"], "support")
        self.assertEqual(excluded_by_id["nws"]["tier"], "excluded")
        self.assertEqual(excluded_by_id["nws"]["reason_code"], "non_usable_reliability_tier")
        self.assertEqual(result["recommended_action"], "ALLOW")

    def test_rolling_reliability_merges_missing_source_id_by_slugged_source_name(self):
        rows = [
            {
                "source_id": "nws",
                "source_name": "NWS",
                "city_id": "seattle_wa",
                "market_kind": "high",
                "contract_shape": "tail",
                "known_after": "2026-05-16T00:00:00+00:00",
                "direction_correct": True,
                "eligible_for_reliability": True,
                "observation_id": "obs-explicit",
            },
            {
                "source_name": "NWS",
                "city_id": "seattle_wa",
                "market_kind": "high",
                "contract_shape": "tail",
                "known_after": "2026-05-16T01:00:00+00:00",
                "direction_correct": True,
                "eligible_for_reliability": True,
                "observation_id": "obs-derived",
            },
        ]

        reliability_rows = build_rolling_source_reliability_rows(
            rows,
            "2026-05-17T00:00:00+00:00",
            min_samples=2,
            trusted_samples=2,
        )

        self.assertEqual(len(reliability_rows), 1)
        self.assertEqual(reliability_rows[0]["source_id"], "nws")
        self.assertEqual(reliability_rows[0]["sample_count"], 2)
        self.assertEqual(reliability_rows[0]["tier"], "strong_trusted")

    def test_cli_builds_as_of_reliability_from_source_outcome_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "input.jsonl"
            output_path = root / "output.jsonl"
            ledger_path = root / "source_outcomes.jsonl"

            append_jsonl(
                input_path,
                {
                    "shared_candidate_id": "candidate-asof",
                    "market_id": "market-asof",
                    "observed_at": "2026-05-17T12:00:00+00:00",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "question_side": "above",
                    "predicted_outcome": "YES",
                    "threshold": 70.0,
                    "forecast_target": "daily_high",
                    "source_details": [{"source_name": "NWS", "forecast_high": 72.0}],
                },
            )
            for known_after in ("2026-05-16T09:00:00+00:00", "2026-05-16T10:00:00+00:00"):
                append_jsonl(
                    ledger_path,
                    {
                        "source_id": "nws",
                        "source_name": "NWS" if known_after.endswith("09:00:00+00:00") else "nws",
                        "city_id": "seattle_wa",
                        "market_kind": "high",
                        "contract_shape": "tail",
                        "known_after": known_after,
                        "direction_correct": True,
                        "absolute_error_f": 1.0,
                        "bias_f": 0.0,
                        "eligible_for_reliability": True,
                    },
                )
            append_jsonl(
                ledger_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "known_after": "2026-05-18T10:00:00+00:00",
                    "direction_correct": False,
                    "absolute_error_f": 10.0,
                    "bias_f": 10.0,
                    "eligible_for_reliability": True,
                },
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = source_confidence_main(
                    [
                        "--input",
                        str(input_path),
                        "--output",
                        str(output_path),
                        "--source-outcome-ledger",
                        str(ledger_path),
                        "--min-samples",
                        "2",
                        "--trusted-samples",
                        "2",
                    ]
                )

            stored_rows = load_jsonl(output_path)

            self.assertEqual(exit_code, 0)
            self.assertEqual(stored_rows[0]["source_grade"], GRADE_STRONG_YES)
            self.assertEqual(stored_rows[0]["recommended_action"], "ALLOW")
            self.assertEqual(stored_rows[0]["sources_used"][0]["sample_count"], 2)
            self.assertEqual(stored_rows[0]["sources_used"][0]["threshold_direction_accuracy"], 1.0)
            self.assertIn("reliability_mode=rolling_source_outcome_ledger", stdout.getvalue())

    def test_cli_source_outcome_ledger_without_as_of_blocks_as_insufficient_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "input.jsonl"
            output_path = root / "output.jsonl"
            ledger_path = root / "source_outcomes.jsonl"

            append_jsonl(
                input_path,
                {
                    "shared_candidate_id": "candidate-no-asof",
                    "market_id": "market-no-asof",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "question_side": "above",
                    "predicted_outcome": "YES",
                    "threshold": 70.0,
                    "forecast_target": "daily_high",
                    "source_details": [{"source_name": "NWS", "forecast_high": 72.0}],
                },
            )
            append_jsonl(
                ledger_path,
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "known_after": "2026-05-16T09:00:00+00:00",
                    "direction_correct": True,
                    "eligible_for_reliability": True,
                },
            )

            exit_code = source_confidence_main(
                [
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                    "--source-outcome-ledger",
                    str(ledger_path),
                    "--min-samples",
                    "1",
                ]
            )

            stored_rows = load_jsonl(output_path)

            self.assertEqual(exit_code, 0)
            self.assertEqual(stored_rows[0]["source_grade"], GRADE_INSUFFICIENT_HISTORY)
            self.assertEqual(stored_rows[0]["reason_code"], "no_usable_reliability_after_backoff")



if __name__ == "__main__":
    unittest.main()
