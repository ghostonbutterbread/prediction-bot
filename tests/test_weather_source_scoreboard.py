import json
import tempfile
import unittest
from pathlib import Path

from bot.weather.source_scoreboard import (
    build_scoreboard_report,
    build_source_scoreboard,
    extract_source_forecast_observations,
    render_scoreboard_report_markdown,
)
from bot.weather.source_reliability import (
    SourceReliabilityTable,
    build_rolling_source_reliability_rows,
    build_rolling_source_reliability_table,
    build_source_edge_evaluation_rows,
    build_source_outcome_ledger_rows,
    classify_reliability_tier,
    evaluate_source_reliability_candidate,
    summarize_source_edge_evaluation_rows,
)
from scripts.weather_source_scoreboard import main as source_scoreboard_main
from scripts.weather_source_edge_validate import main as source_edge_validate_main


def weather_row(
    *,
    market_id: str = "KXHIGHSEA-26MAY15-T70",
    question: str = "Will the maximum temperature in Seattle be >70 on May 15, 2026?",
    city_id: str = "seattle_wa",
    city: str = "Seattle",
    market_date: str = "2026-05-15",
    threshold: float = 70.0,
    actual_temp: float | None = 73.0,
    sources: list[dict] | None = None,
) -> dict:
    row = {
        "market_id": market_id,
        "question": question,
        "market_date": market_date,
        "weather_risk": {
            "evidence": {
                "weather_station_resolution": {
                    "city_id": city_id,
                    "city": city,
                    "station_id": "KSEA",
                }
            }
        },
        "decision_artifact": {
            "strategy_trace": {
                "raw_signals": {
                    "live": {
                        "data": {
                            "threshold": threshold,
                            "question_side": "above",
                            "market_date": market_date,
                            "source_details": sources
                            if sources is not None
                            else [
                                {"source_name": "nws", "forecast_high": 72.0},
                                {"source_name": "open-meteo", "forecast_high": 67.0},
                            ],
                        }
                    }
                }
            }
        },
    }
    if actual_temp is not None:
        row["actual_temp_used"] = actual_temp
    return row


class WeatherSourceScoreboardTests(unittest.TestCase):
    def test_aggregates_per_source_city_kind_and_shape(self):
        rows = [
            weather_row(actual_temp=73.0),
            weather_row(
                market_id="KXHIGHSEA-26MAY16-T70",
                market_date="2026-05-16",
                actual_temp=66.0,
                sources=[
                    {"source_name": "nws", "forecast_high": 68.0},
                    {"source_name": "open-meteo", "forecast_high": 65.0},
                ],
            ),
            weather_row(
                market_id="KXLOWNY-26MAY16-T60",
                question="Will the minimum temperature in New York be >60 on May 16, 2026?",
                city_id="new_york_ny",
                city="New York",
                actual_temp=61.0,
                sources=[{"source_name": "nws", "forecast_low": 62.0}],
            ),
        ]

        report = build_source_scoreboard(rows)

        self.assertEqual(report["summary"]["input_rows"], 3)
        self.assertEqual(report["summary"]["observations_scored"], 5)
        by_key = {
            (row["source_id"], row["city_id"], row["market_kind"], row["contract_shape"]): row
            for row in report["slices"]
        }
        self.assertEqual(by_key[("nws", "seattle_wa", "high", "tail")]["sample_count"], 2)
        self.assertEqual(by_key[("open_meteo", "seattle_wa", "high", "tail")]["sample_count"], 2)
        self.assertEqual(by_key[("nws", "new_york_ny", "low", "tail")]["sample_count"], 1)

    def test_scores_mae_bias_and_threshold_direction_accuracy(self):
        rows = [
            weather_row(actual_temp=73.0),
            weather_row(
                market_id="KXHIGHSEA-26MAY16-T70",
                market_date="2026-05-16",
                actual_temp=66.0,
                sources=[
                    {"source_name": "nws", "forecast_high": 68.0},
                    {"source_name": "open-meteo", "forecast_high": 65.0},
                ],
            ),
        ]

        report = build_source_scoreboard(rows)
        by_source = {row["source_id"]: row for row in report["slices"]}

        self.assertEqual(by_source["nws"]["sample_count"], 2)
        self.assertEqual(by_source["nws"]["mae"], 1.5)
        self.assertEqual(by_source["nws"]["mean_bias"], 0.5)
        self.assertEqual(by_source["nws"]["threshold_direction_accuracy"], 1.0)
        self.assertEqual(by_source["nws"]["within_2f_rate"], 1.0)

        self.assertEqual(by_source["open_meteo"]["mae"], 3.5)
        self.assertEqual(by_source["open_meteo"]["mean_bias"], -3.5)
        self.assertEqual(by_source["open_meteo"]["threshold_direction_accuracy"], 0.5)
        self.assertEqual(by_source["open_meteo"]["within_3f_rate"], 0.5)

    def test_missing_unknown_rows_are_counted_without_crashing(self):
        rows = [
            {"market_id": "NO_SOURCE"},
            weather_row(actual_temp=None),
            weather_row(actual_temp=73.0, sources=[{"source_name": "nws"}]),
        ]

        report = build_source_scoreboard(rows)

        self.assertEqual(report["summary"]["input_rows"], 3)
        self.assertEqual(report["summary"]["rows_without_observations"], 1)
        self.assertEqual(report["summary"]["observations_extracted"], 3)
        self.assertEqual(report["summary"]["observations_missing_actual"], 2)
        self.assertEqual(report["summary"]["observations_missing_forecast"], 1)
        self.assertEqual(report["summary"].get("observations_scored", 0), 0)

    def test_caller_actual_lookup_can_supply_actual_temperature(self):
        row = weather_row(actual_temp=None)

        observations = extract_source_forecast_observations(
            row,
            actual_lookup={("seattle_wa", "2026-05-15", "high"): 73.0},
        )
        self.assertEqual(observations[0].actual_temp_f, 73.0)

        report = build_source_scoreboard(
            [row],
            actual_lookup={("seattle_wa", "2026-05-15", "high"): 73.0},
        )
        self.assertEqual(report["summary"]["observations_scored"], 2)

    def test_string_only_sources_are_counted_as_missing_forecast_observations(self):
        row = weather_row(actual_temp=73.0, sources=[])
        row["decision_artifact"]["strategy_trace"]["raw_signals"]["live"]["data"].pop("source_details")
        row["decision_artifact"]["strategy_trace"]["raw_signals"]["live"]["data"]["sources"] = ["nws", "open-meteo"]

        report = build_source_scoreboard([row])

        self.assertEqual(report["summary"]["observations_extracted"], 2)
        self.assertEqual(report["summary"]["observations_missing_forecast"], 2)
        self.assertEqual(report["summary"]["observations_scored"], 0)

    def test_non_finite_numbers_are_treated_as_missing(self):
        row = weather_row(actual_temp=float("nan"), sources=[{"source_name": "nws", "forecast_high": float("inf")}])

        report = build_source_scoreboard([row])

        self.assertEqual(report["summary"]["observations_extracted"], 1)
        self.assertEqual(report["summary"]["observations_missing_actual"], 1)
        self.assertEqual(report["summary"]["observations_missing_forecast"], 1)
        self.assertEqual(report["summary"]["observations_scored"], 0)

    def test_failed_date_validation_prevents_scoring(self):
        row = weather_row(actual_temp=73.0)
        row["decision_artifact"]["strategy_trace"]["raw_signals"]["live"]["data"]["date_validation"] = {
            "ok": False,
            "reason": "unit_mismatch",
            "market_date": "2026-05-15",
            "weather_date": "2026-05-14",
        }

        report = build_source_scoreboard([row])

        self.assertEqual(report["summary"]["observations_extracted"], 2)
        self.assertEqual(report["summary"]["observations_missing_actual"], 2)
        self.assertEqual(report["summary"]["observations_scored"], 0)
        self.assertIn("date_validation_failed:unit_mismatch", report["slices"][0]["missing"])

    def test_nested_source_context_weather_snapshot_is_parsed(self):
        row = {
            "market_id": "KXHIGHNY-26MAY15-T80",
            "question": "Will NYC high temperature be above 80 degrees?",
            "decision_artifact": {
                "source_context": {
                    "data": {
                        "weather_source_snapshot": {
                            "target_forecast_date": "2026-05-15",
                            "station_resolution": {"city_id": "new_york_ny", "city": "New York"},
                            "forecast": {"high": 84.0, "actual_temp_used": 85.0, "threshold": 80.0, "question_side": "above"},
                            "sources": [{"source_name": "nws", "forecast_high": 84.0}],
                        }
                    }
                },
                "source_snapshots": [
                    {"source": "weather", "snapshot_ref": "source_context.data.weather_source_snapshot"}
                ],
            },
        }

        report = build_source_scoreboard([row])

        self.assertEqual(report["summary"]["observations_scored"], 1)
        self.assertEqual(report["slices"][0]["city_id"], "new_york_ny")

    def test_report_generation_builds_ranked_slices_leaderboards_and_notes(self):
        rows = [
            weather_row(actual_temp=73.0),
            weather_row(
                market_id="KXHIGHSEA-26MAY16-T70",
                market_date="2026-05-16",
                actual_temp=66.0,
                sources=[
                    {"source_name": "nws", "forecast_high": 68.0},
                    {"source_name": "open-meteo", "forecast_high": 65.0},
                ],
            ),
            weather_row(
                market_id="KXLOWNY-26MAY16-T60",
                question="Will the minimum temperature in New York be >60 on May 16, 2026?",
                city_id="new_york_ny",
                city="New York",
                actual_temp=None,
                sources=[{"source_name": "nws", "forecast_low": 62.0}],
            ),
        ]

        scoreboard = build_source_scoreboard(rows)
        report = build_scoreboard_report(
            scoreboard,
            run_metadata={"generated_at": "2026-05-16T00:00:00+00:00", "mode": "offline_report_only"},
            limit=2,
        )

        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["generated_at"], "2026-05-16T00:00:00+00:00")
        self.assertEqual(len(report["best_slices"]), 2)
        self.assertEqual(len(report["worst_slices"]), 2)
        self.assertEqual(report["leaderboards"]["sources"][0]["label"], "nws")
        self.assertEqual(report["leaderboards"]["sources"][0]["sample_count"], 2)
        self.assertEqual(report["leaderboards"]["cities"][0]["city_id"], "seattle_wa")
        self.assertEqual(report["leaderboards"]["types"][0]["market_kind"], "high")
        self.assertIn(
            "missing_actual_temperatures",
            {note["code"] for note in report["missing_data_notes"]},
        )

        markdown = render_scoreboard_report_markdown(report)
        self.assertIn("# Weather Source Scoreboard Report", markdown)
        self.assertIn("## Missing Data Notes", markdown)
        self.assertIn("Source Leaderboard", markdown)


class WeatherSourceReliabilityTests(unittest.TestCase):
    def test_classifies_scoreboard_slices_conservatively(self):
        self.assertEqual(classify_reliability_tier(1.0, 100), "strong_trusted")
        self.assertEqual(classify_reliability_tier(0.91, 100), "trusted")
        self.assertEqual(classify_reliability_tier(0.91, 99), "neutral")
        self.assertEqual(classify_reliability_tier(None, 200), "neutral")
        self.assertEqual(classify_reliability_tier(0.60, 100), "weak")
        self.assertEqual(classify_reliability_tier(0.44, 100), "excluded")

    def test_excluded_dissent_does_not_override_trusted_support(self):
        table = SourceReliabilityTable(
            [
                {
                    "source_id": "nws",
                    "source_name": "nws",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 100,
                    "threshold_direction_accuracy": 0.95,
                },
                {
                    "source_id": "bad_model",
                    "source_name": "bad-model",
                    "city_id": "seattle_wa",
                    "market_kind": "high",
                    "contract_shape": "tail",
                    "sample_count": 100,
                    "threshold_direction_accuracy": 0.30,
                },
            ]
        )
        row = weather_row(
            sources=[
                {"source_name": "nws", "forecast_high": 72.0},
                {"source_name": "bad-model", "forecast_high": 68.0},
            ]
        )

        evaluation = evaluate_source_reliability_candidate(row, table, action="BUY_YES")

        self.assertEqual(evaluation.recommended_action, "BUY_YES")
        self.assertEqual(evaluation.reason_code, "trusted_support")
        self.assertEqual(evaluation.trusted_support_count, 1)
        self.assertEqual(evaluation.excluded_dissent_count, 1)
        self.assertEqual(evaluation.weighted_dissent, 0.0)

    def test_source_outcome_ledger_builder_creates_eligible_scored_row(self):
        row = weather_row(
            actual_temp=73.0,
            sources=[{"source_name": "nws", "forecast_high": 72.0}],
        )
        row["observed_at"] = "2026-05-14T12:00:00+00:00"
        row["resolved_at"] = "2026-05-16T13:00:00+00:00"
        row["shared_candidate_id"] = "candidate-1"

        ledger_rows = build_source_outcome_ledger_rows([row], source_row_path="input.jsonl")

        self.assertEqual(len(ledger_rows), 1)
        ledger_row = ledger_rows[0]
        self.assertEqual(ledger_row["schema_version"], 1)
        self.assertEqual(ledger_row["source_id"], "nws")
        self.assertEqual(ledger_row["source_name"], "nws")
        self.assertEqual(ledger_row["city_id"], "seattle_wa")
        self.assertEqual(ledger_row["market_kind"], "high")
        self.assertEqual(ledger_row["contract_shape"], "tail")
        self.assertEqual(ledger_row["market_id"], "KXHIGHSEA-26MAY15-T70")
        self.assertEqual(ledger_row["shared_candidate_id"], "candidate-1")
        self.assertEqual(ledger_row["observed_at"], "2026-05-14T12:00:00+00:00")
        self.assertEqual(ledger_row["resolved_at"], "2026-05-16T13:00:00+00:00")
        self.assertEqual(ledger_row["known_after"], "2026-05-16T13:00:00+00:00")
        self.assertEqual(ledger_row["forecast_temp_f"], 72.0)
        self.assertEqual(ledger_row["threshold"], 70.0)
        self.assertEqual(ledger_row["actual_temp_f"], 73.0)
        self.assertEqual(ledger_row["predicted_outcome"], "YES")
        self.assertEqual(ledger_row["actual_outcome"], "YES")
        self.assertTrue(ledger_row["direction_correct"])
        self.assertEqual(ledger_row["absolute_error_f"], 1.0)
        self.assertEqual(ledger_row["bias_f"], -1.0)
        self.assertTrue(ledger_row["eligible_for_reliability"])
        self.assertIsNone(ledger_row["exclusion_reason"])

    def test_rolling_reliability_excludes_rows_not_known_before_as_of(self):
        rows = [_ledger_row(i, correct=True, known_after=_known_after(i)) for i in range(100)]
        rows.append(_ledger_row(100, correct=False, known_after=_known_after(100)))

        stats_rows = build_rolling_source_reliability_rows(rows, _known_after(100))

        self.assertEqual(len(stats_rows), 1)
        self.assertEqual(stats_rows[0]["sample_count"], 100)
        self.assertEqual(stats_rows[0]["threshold_direction_accuracy"], 1.0)
        self.assertEqual(stats_rows[0]["tier"], "trusted")

    def test_rolling_reliability_uses_latest_max_window_rows_only(self):
        older_bad = [_ledger_row(i, correct=False, known_after=_known_after(i)) for i in range(50)]
        newer_good = [_ledger_row(i + 50, correct=True, known_after=_known_after(i + 50)) for i in range(200)]

        stats_rows = build_rolling_source_reliability_rows(
            older_bad + newer_good,
            _known_after(250),
            max_window=200,
        )

        self.assertEqual(stats_rows[0]["sample_count"], 200)
        self.assertEqual(stats_rows[0]["threshold_correct_count"], 200)
        self.assertEqual(stats_rows[0]["threshold_direction_accuracy"], 1.0)
        self.assertEqual(stats_rows[0]["tier"], "strong_trusted")

    def test_rolling_reliability_insufficient_samples_are_neutral(self):
        rows = [_ledger_row(i, correct=True, known_after=_known_after(i)) for i in range(99)]

        stats_rows = build_rolling_source_reliability_rows(rows, _known_after(99))

        self.assertEqual(stats_rows[0]["sample_count"], 99)
        self.assertEqual(stats_rows[0]["tier"], "neutral")

    def test_rolling_reliability_trusted_and_strong_trusted_thresholds(self):
        trusted_rows = [
            _ledger_row(i, correct=i < 90, known_after=_known_after(i))
            for i in range(100)
        ]
        strong_rows = [
            _ledger_row(i, correct=True, known_after=_known_after(i), source_id="open_meteo")
            for i in range(200)
        ]

        stats_rows = build_rolling_source_reliability_rows(
            trusted_rows + strong_rows,
            _known_after(200),
        )
        by_source = {row["source_id"]: row for row in stats_rows}

        self.assertEqual(by_source["nws"]["sample_count"], 100)
        self.assertEqual(by_source["nws"]["threshold_direction_accuracy"], 0.9)
        self.assertEqual(by_source["nws"]["tier"], "trusted")
        self.assertEqual(by_source["open_meteo"]["sample_count"], 200)
        self.assertEqual(by_source["open_meteo"]["threshold_direction_accuracy"], 1.0)
        self.assertEqual(by_source["open_meteo"]["tier"], "strong_trusted")

    def test_rolling_reliability_excluded_source_is_not_inverted(self):
        rows = [
            _ledger_row(i, correct=i < 44, known_after=_known_after(i), source_id="bad_model")
            for i in range(100)
        ]
        table = build_rolling_source_reliability_table(rows, _known_after(100))
        row = weather_row(sources=[{"source_name": "bad-model", "forecast_high": 68.0}])

        evaluation = evaluate_source_reliability_candidate(row, table, action="BUY_YES")

        self.assertEqual(evaluation.source_votes[0]["tier"], "excluded")
        self.assertEqual(evaluation.source_votes[0]["vote"], "dissent")
        self.assertEqual(evaluation.excluded_dissent_count, 1)
        self.assertEqual(evaluation.weighted_dissent, 0.0)
        self.assertEqual(evaluation.recommended_action, "SKIP")

    def test_source_edge_evaluation_compares_source_side_to_kalshi_outcome_and_price(self):
        ledger_rows = [
            {
                **_ledger_row(1, correct=True, known_after=_known_after(2), source_id="nws"),
                "predicted_outcome": "YES",
                "actual_outcome": None,
                "direction": "BUY_YES",
                "estimated_fill_price": 0.25,
            },
            {
                **_ledger_row(2, correct=False, known_after=_known_after(2), source_id="open_meteo"),
                "predicted_outcome": "NO",
                "actual_outcome": None,
                "direction": "BUY_NO",
                "estimated_fill_price": 0.40,
            },
        ]

        rows = build_source_edge_evaluation_rows(
            ledger_rows,
            outcome_lookup={
                "KXHIGHSEA-26MAY01-T70": {
                    "official_outcome": "YES",
                    "outcome_source": "kalshi_result",
                    "outcome_known_at": "2026-05-03T00:00:00+00:00",
                    "label_independence": "independent_kalshi_result",
                },
                "KXHIGHSEA-26MAY02-T70": {
                    "official_outcome": "YES",
                    "outcome_source": "kalshi_result",
                },
            },
        )

        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["eligible_for_edge_validation"])
        self.assertEqual(rows[0]["source_implied_side"], "YES")
        self.assertEqual(rows[0]["official_outcome"], "YES")
        self.assertTrue(rows[0]["win"])
        self.assertEqual(rows[0]["source_side_price"], 0.25)
        self.assertEqual(rows[0]["binary_edge_realized"], 0.75)
        self.assertEqual(rows[0]["flat_1usd_pnl"], 0.75)

        self.assertTrue(rows[1]["eligible_for_edge_validation"])
        self.assertEqual(rows[1]["source_implied_side"], "NO")
        self.assertEqual(rows[1]["official_outcome"], "YES")
        self.assertFalse(rows[1]["win"])
        self.assertEqual(rows[1]["binary_edge_realized"], -0.4)
        self.assertEqual(rows[1]["flat_1usd_pnl"], -0.4)

    def test_source_edge_evaluation_requires_explicit_official_outcome(self):
        row = {**_ledger_row(1, correct=True, known_after=_known_after(2), source_id="nws"), "predicted_outcome": "YES", "actual_outcome": "YES", "direction": "BUY_YES", "estimated_fill_price": 0.25}

        edge_row = build_source_edge_evaluation_rows([row])[0]

        self.assertFalse(edge_row["eligible_for_edge_validation"])
        self.assertIn("missing_official_outcome", edge_row["exclusion_reason"])
        self.assertIsNone(edge_row["official_outcome"])

    def test_source_edge_evaluation_does_not_use_wrong_side_fill_price(self):
        row = {**_ledger_row(1, correct=True, known_after=_known_after(2), source_id="nws"), "predicted_outcome": "NO", "direction": "BUY_YES", "estimated_fill_price": 0.25}

        edge_row = build_source_edge_evaluation_rows([row], outcome_lookup={"KXHIGHSEA-26MAY01-T70": "NO"})[0]

        self.assertFalse(edge_row["eligible_for_edge_validation"])
        self.assertIn("missing_source_side_price", edge_row["exclusion_reason"])
        self.assertIsNone(edge_row["source_side_price"])

    def test_source_outcome_ledger_preserves_future_price_inputs_for_edge_evaluation(self):
        row = weather_row(actual_temp=73.0, sources=[{"source_name": "nws", "forecast_high": 72.0}])
        row.update({"observed_at": "2026-05-14T12:00:00+00:00", "resolved_at": "2026-05-16T13:00:00+00:00", "direction": "BUY_YES"})
        row["provenance"] = {"future_pnl_inputs": {"estimated_fill_price": 0.25, "best_yes_ask": 0.25, "best_no_ask": 0.77}}

        ledger_row = build_source_outcome_ledger_rows([row])[0]

        self.assertEqual(ledger_row["action"], "BUY_YES")
        self.assertEqual(ledger_row["estimated_fill_price"], 0.25)
        self.assertEqual(ledger_row["best_yes_ask"], 0.25)
        self.assertEqual(ledger_row["best_no_ask"], 0.77)

    def test_source_edge_validate_cli_writes_report_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            ledger_path = tmp / "source_outcome_ledger.jsonl"
            outcome_path = tmp / "outcomes.jsonl"
            output_dir = tmp / "edge_report"
            ledger_rows = [
                {**_ledger_row(1, correct=True, known_after=_known_after(2), source_id="nws"), "predicted_outcome": "YES", "direction": "BUY_YES", "estimated_fill_price": 0.25},
                {**_ledger_row(2, correct=True, known_after=_known_after(2), source_id="nws"), "predicted_outcome": "NO", "direction": "BUY_NO", "estimated_fill_price": 0.40},
                {**_ledger_row(3, correct=True, known_after=_known_after(2), source_id="open_meteo"), "predicted_outcome": "YES", "direction": "BUY_YES", "estimated_fill_price": 0.10},
            ]
            _write_jsonl_test(ledger_path, ledger_rows)
            _write_jsonl_test(
                outcome_path,
                [
                    {"market_id": "KXHIGHSEA-26MAY01-T70", "official_outcome": "YES", "outcome_source": "kalshi_result", "outcome_known_at": "2026-05-03T00:00:00+00:00"},
                    {"ticker": "KXHIGHSEA-26MAY02-T70", "result": "YES", "outcome_source": "kalshi_result"},
                ],
            )

            rc = source_edge_validate_main([
                "--ledger-input", str(ledger_path),
                "--outcome-input", str(outcome_path),
                "--output-dir", str(output_dir),
                "--report-limit", "10",
            ])

            self.assertEqual(rc, 0)
            summary = json.loads((output_dir / "source_edge_summary.json").read_text())
            metadata = json.loads((output_dir / "run_metadata.json").read_text())
            edge_rows = [json.loads(line) for line in (output_dir / "source_edge_evaluation_rows.jsonl").read_text().splitlines() if line]
            report_md = (output_dir / "source_edge_report.md").read_text()

        self.assertEqual(metadata["mode"], "offline_edge_validation_only")
        self.assertFalse(metadata["network_access"])
        self.assertEqual(metadata["outcome_load_stats"]["outcomes_loaded"], 2)
        self.assertEqual(summary["summary"]["input_rows"], 3)
        self.assertEqual(summary["summary"]["eligible_rows"], 2)
        self.assertEqual(summary["summary"]["blocked_rows"], 1)
        self.assertEqual(summary["summary"]["reason_counts"], {"missing_official_outcome": 1})
        self.assertEqual(len(edge_rows), 3)
        self.assertIn("Weather Source Edge Validation Report", report_md)
        self.assertIn("missing_official_outcome", report_md)

    def test_source_edge_summary_groups_by_source_city_kind_shape(self):
        rows = build_source_edge_evaluation_rows(
            [
                {**_ledger_row(1, correct=True, known_after=_known_after(2), source_id="nws"), "predicted_outcome": "YES", "direction": "BUY_YES", "estimated_fill_price": 0.25},
                {**_ledger_row(2, correct=True, known_after=_known_after(2), source_id="nws"), "predicted_outcome": "NO", "direction": "BUY_NO", "estimated_fill_price": 0.40},
                {**_ledger_row(3, correct=True, known_after=_known_after(2), source_id="open_meteo"), "predicted_outcome": "NO"},
            ],
            outcome_lookup={
                "KXHIGHSEA-26MAY01-T70": "YES",
                "KXHIGHSEA-26MAY02-T70": "YES",
                "KXHIGHSEA-26MAY03-T70": "NO",
            },
        )

        summary = summarize_source_edge_evaluation_rows(rows)

        self.assertEqual(summary["summary"]["input_rows"], 3)
        self.assertEqual(summary["summary"]["eligible_rows"], 2)
        self.assertEqual(summary["summary"]["blocked_rows"], 1)
        nws = next(row for row in summary["slices"] if row["source_id"] == "nws")
        self.assertEqual(nws["eligible_count"], 2)
        self.assertEqual(nws["wins"], 1)
        self.assertEqual(nws["losses"], 1)
        self.assertEqual(nws["win_rate"], 0.5)
        self.assertEqual(nws["avg_binary_edge_realized"], 0.175)
        self.assertEqual(nws["flat_1usd_pnl"], 0.35)


def _ledger_row(
    index: int,
    *,
    correct: bool,
    known_after: str,
    source_id: str = "nws",
) -> dict:
    forecast = 72.0 if correct else 68.0
    return {
        "schema_version": 1,
        "observation_id": f"{source_id}-{index}",
        "source_id": source_id,
        "source_name": source_id.replace("_", "-"),
        "city_id": "seattle_wa",
        "market_kind": "high",
        "contract_shape": "tail",
        "market_id": f"KXHIGHSEA-26MAY{index:02d}-T70",
        "shared_candidate_id": f"candidate-{index}",
        "observed_at": "2026-04-30T12:00:00+00:00",
        "market_date": "2026-05-01",
        "resolved_at": known_after,
        "known_after": known_after,
        "forecast_temp_f": forecast,
        "threshold": 70.0,
        "question_side": "above",
        "actual_temp_f": 73.0,
        "predicted_outcome": "YES" if correct else "NO",
        "actual_outcome": "YES",
        "direction_correct": correct,
        "absolute_error_f": abs(forecast - 73.0),
        "bias_f": forecast - 73.0,
        "eligible_for_reliability": True,
        "exclusion_reason": None,
    }


def _known_after(index: int) -> str:
    return f"2026-05-{1 + index // 24:02d}T{index % 24:02d}:00:00+00:00"


class WeatherSourceScoreboardScriptTests(unittest.TestCase):
    def test_cli_writes_expected_artifacts_for_tiny_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "market_snapshots.jsonl"
            output_dir = root / "scoreboard"
            input_path.write_text(json.dumps(weather_row()) + "\n", encoding="utf-8")

            exit_code = source_scoreboard_main(
                [
                    "--input",
                    str(input_path),
                    "--output-dir",
                    str(output_dir),
                    "--limit",
                    "10",
                ]
            )

            self.assertEqual(exit_code, 0)
            for name in (
                "source_scoreboard.json",
                "source_scoreboard_by_slice.jsonl",
                "source_scoreboard_report.json",
                "source_scoreboard_report.md",
                "best_slices.jsonl",
                "best_slices.md",
                "worst_slices.jsonl",
                "worst_slices.md",
                "source_leaderboard.jsonl",
                "source_leaderboard.md",
                "city_leaderboard.jsonl",
                "city_leaderboard.md",
                "type_leaderboard.jsonl",
                "type_leaderboard.md",
                "run_metadata.json",
            ):
                self.assertTrue((output_dir / name).exists(), name)

            payload = json.loads((output_dir / "source_scoreboard.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["observations_scored"], 2)
            report = json.loads((output_dir / "source_scoreboard_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["leaderboards"]["sources"][0]["sample_count"], 1)
            self.assertIn("missing_data_notes", report)
            source_leaderboard_rows = [
                json.loads(line)
                for line in (output_dir / "source_leaderboard.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(source_leaderboard_rows), 2)
            metadata = json.loads((output_dir / "run_metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["mode"], "offline_report_only")
            self.assertFalse(metadata["network_access"])
            self.assertIn("source_scoreboard_report.md", metadata["artifacts"])

    def test_cli_optionally_writes_source_outcome_ledger_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "market_snapshots.jsonl"
            output_dir = root / "scoreboard"
            ledger_path = root / "ledger" / "source_outcome_ledger.jsonl"
            row = weather_row()
            row["observed_at"] = "2026-05-14T12:00:00+00:00"
            row["resolved_at"] = "2026-05-16T13:00:00+00:00"
            row["shared_candidate_id"] = "candidate-1"
            input_path.write_text(json.dumps(row) + "\n", encoding="utf-8")

            exit_code = source_scoreboard_main(
                [
                    "--input",
                    str(input_path),
                    "--output-dir",
                    str(output_dir),
                    "--ledger-output",
                    str(ledger_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            ledger_rows = [
                json.loads(line)
                for line in ledger_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(ledger_rows), 2)
            first_row = ledger_rows[0]
            for field in (
                "schema_version",
                "observation_id",
                "source_row_path",
                "source_line_number",
                "market_id",
                "shared_candidate_id",
                "source_id",
                "source_name",
                "city_id",
                "market_kind",
                "contract_shape",
                "observed_at",
                "market_date",
                "resolved_at",
                "known_after",
                "forecast_temp_f",
                "threshold",
                "question_side",
                "actual_temp_f",
                "predicted_outcome",
                "actual_outcome",
                "direction_correct",
                "absolute_error_f",
                "bias_f",
                "source_mode",
                "actual_source",
                "date_validation_ok",
                "eligible_for_reliability",
                "exclusion_reason",
            ):
                self.assertIn(field, first_row)
            self.assertEqual(first_row["schema_version"], 1)
            self.assertEqual(first_row["source_row_path"], str(input_path))
            self.assertEqual(first_row["source_line_number"], 1)
            self.assertEqual(first_row["market_id"], "KXHIGHSEA-26MAY15-T70")
            self.assertEqual(first_row["shared_candidate_id"], "candidate-1")
            self.assertEqual(first_row["city_id"], "seattle_wa")
            self.assertEqual(first_row["known_after"], "2026-05-16T13:00:00+00:00")
            self.assertTrue(first_row["eligible_for_reliability"])

            metadata = json.loads((output_dir / "run_metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["ledger_output"], str(ledger_path))
            self.assertEqual(metadata["ledger_rows"], 2)


def _write_jsonl_test(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


if __name__ == "__main__":
    unittest.main()
