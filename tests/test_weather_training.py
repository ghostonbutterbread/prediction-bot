import csv
import json
import tempfile
import unittest
from pathlib import Path

from bot.weather import WeatherRegistry
from bot.weather.analysis import WeatherSampleRecord
from bot.weather.training import (
    StructuralTrainingPolicy,
    TemperatureTrainingPolicy,
    TemperatureTrainingSample,
    apply_price_aware_training_updates,
    build_weather_training_examples,
    run_structural_training_from_examples,
    run_temperature_training_from_samples,
)
from scripts.weather_train import (
    _build_batches,
    _select_records,
    main as weather_train_main,
)


def make_training_sample(
    day: str,
    suffix: str,
    *,
    yes_price: float,
    outcome: float,
    city_id: str = "miami_fl",
    source_id: str = "src_nws_mfl",
    market_type: str = "high_temp",
) -> TemperatureTrainingSample:
    return TemperatureTrainingSample(
        market_id=f"KXHIGHMIA-{day.replace('-', '')}-{suffix}",
        city_id=city_id,
        source_id=source_id,
        market_type=market_type,
        event_date=day,
        yes_price=yes_price,
        outcome=outcome,
        observed_at=f"{day}T20:00:00+00:00",
        resolved_at=f"{day}T23:00:00+00:00",
    )


def make_history_record(
    day: str,
    suffix: str,
    *,
    outcome: str,
    yes_price: float | None = None,
    city: str = "Miami",
    source_path: str = "history.csv",
) -> WeatherSampleRecord:
    payload = {
        "sample_kind": "historical_csv",
        "source_path": source_path,
        "observed_at": f"{day}T20:00:00+00:00",
        "resolved_at": f"{day}T23:00:00+00:00",
        "market_id": f"KXHIGHMIA-{day.replace('-', '')}-{suffix}",
        "category": "KXHIGHMIA",
        "question": f"Will the high temp in {city} be above 84° on {day}?",
        "yes_price": yes_price,
        "no_price": None if yes_price is None else round(1.0 - yes_price, 2),
        "volume": 1000.0,
        "outcome": outcome,
        "metadata": {
            "market_subtitle": ">84°",
            "yes_subtitle": "Yes",
            "no_subtitle": "No",
        },
    }
    return WeatherSampleRecord(**payload)


class WeatherTrainingTests(unittest.TestCase):
    def test_select_records_and_build_batches_support_bounded_runs(self):
        records = list(range(10))

        selected = _select_records(records, max_records=7)
        batches = _build_batches(selected, batch_size=3, max_batches=2)

        self.assertEqual(selected, [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(batches, [[0, 1, 2], [3, 4, 5]])

    def test_builds_canonical_examples_without_requiring_price(self):
        examples = build_weather_training_examples(
            [
                make_history_record("2026-04-10", "A", outcome="YES", yes_price=None),
                make_history_record("2026-04-11", "B", outcome="NO", yes_price=0.42),
            ],
            registry=WeatherRegistry.from_file(),
        )

        self.assertEqual(len(examples), 2)
        self.assertIsNone(examples[0].yes_price)
        self.assertEqual(examples[0].series_ticker, "KXHIGHMIA")
        self.assertEqual(examples[0].city_id, "miami_fl")
        self.assertEqual(examples[0].source_id, "src_nws_mfl")
        self.assertEqual(examples[0].threshold_value, 84.0)
        self.assertEqual(examples[0].question_side, "above")
        self.assertEqual(examples[0].sample_kind, "historical_csv")
        self.assertEqual(examples[0].outcome, 1.0)
        self.assertEqual(examples[0].evidence["market_subtitle"], ">84°")

    def test_structural_training_scores_no_price_history(self):
        examples = build_weather_training_examples(
            [
                make_history_record("2026-04-10", "A", outcome="YES"),
                make_history_record("2026-04-10", "B", outcome="YES"),
                make_history_record("2026-04-11", "A", outcome="YES"),
                make_history_record("2026-04-11", "B", outcome="YES"),
                make_history_record("2026-04-12", "A", outcome="YES"),
                make_history_record("2026-04-12", "B", outcome="YES"),
                make_history_record("2026-04-13", "A", outcome="YES"),
                make_history_record("2026-04-13", "B", outcome="NO"),
            ],
            registry=WeatherRegistry.from_file(),
        )

        report = run_structural_training_from_examples(
            examples,
            registry=WeatherRegistry.from_file(),
            policy=StructuralTrainingPolicy(
                min_samples_per_city_source=8,
                min_unique_days=4,
                holdout_fraction=0.25,
            ),
            generated_at="2026-04-14T12:00:00+00:00",
        )

        self.assertEqual(report["summary"]["training_mode"], "structural")
        self.assertTrue(report["summary"]["dry_run"])
        self.assertFalse(report["summary"]["registry_mutated"])
        self.assertEqual(report["summary"]["records"]["training_examples"], 8)
        self.assertEqual(report["summary"]["records"]["groups_scored"], 1)
        self.assertEqual(report["candidate_updates"], [])

        [group] = report["group_reports"]
        self.assertIsNone(group["decision_reason"])
        self.assertEqual(group["market_type"], "high_temp")
        self.assertEqual(group["structural_probability"], 1.0)
        self.assertEqual(group["holdout_window"], {"start": "2026-04-13", "end": "2026-04-13"})
        self.assertEqual(group["metrics"]["holdout_direction_accuracy"], 0.5)
        self.assertEqual(group["metrics"]["holdout_brier"], 0.5)
        self.assertEqual(group["metrics"]["source_usefulness_score"], -0.25)
        self.assertEqual(group["metrics"]["source_usefulness_label"], "negative")

    def test_emits_dry_run_candidate_without_mutating_registry(self):
        registry = WeatherRegistry.from_file()
        before = registry.as_dict()
        samples = [
            make_training_sample("2026-04-10", "A", yes_price=0.95, outcome=1.0),
            make_training_sample("2026-04-10", "B", yes_price=0.05, outcome=0.0),
            make_training_sample("2026-04-10", "C", yes_price=0.92, outcome=1.0),
            make_training_sample("2026-04-11", "A", yes_price=0.94, outcome=1.0),
            make_training_sample("2026-04-11", "B", yes_price=0.08, outcome=0.0),
            make_training_sample("2026-04-11", "C", yes_price=0.91, outcome=1.0),
            make_training_sample("2026-04-12", "A", yes_price=0.93, outcome=1.0),
            make_training_sample("2026-04-12", "B", yes_price=0.07, outcome=0.0),
            make_training_sample("2026-04-12", "C", yes_price=0.90, outcome=1.0),
            make_training_sample("2026-04-13", "A", yes_price=0.95, outcome=1.0),
            make_training_sample("2026-04-13", "B", yes_price=0.06, outcome=0.0),
            make_training_sample("2026-04-13", "C", yes_price=0.91, outcome=1.0),
        ]
        policy = TemperatureTrainingPolicy(
            min_samples_per_city_source=8,
            min_unique_days=4,
            holdout_fraction=0.25,
            max_trust_score_delta_per_run=5.0,
            min_trust_score_delta_to_emit=1.0,
            trust_score_step=1.0,
        )

        report = run_temperature_training_from_samples(
            samples,
            registry=registry,
            policy=policy,
            generated_at="2026-04-14T12:00:00+00:00",
        )

        self.assertEqual(registry.as_dict(), before)
        self.assertTrue(report["summary"]["dry_run"])
        self.assertFalse(report["summary"]["registry_mutated"])
        self.assertEqual(report["summary"]["records"]["candidate_updates"], 1)

        [candidate] = report["candidate_updates"]
        self.assertEqual(candidate["city_id"], "miami_fl")
        self.assertEqual(candidate["source_id"], "src_nws_mfl")
        self.assertEqual(candidate["candidate_trust_score"], 95.0)
        self.assertEqual(candidate["trust_score_delta"], 5.0)
        self.assertEqual(candidate["train_window"], {"start": "2026-04-10", "end": "2026-04-12"})
        self.assertEqual(candidate["holdout_window"], {"start": "2026-04-13", "end": "2026-04-13"})
        self.assertTrue(candidate["gates"]["holdout_must_not_degrade"])

    def test_blocks_candidate_when_unique_days_gate_fails(self):
        samples = [
            make_training_sample("2026-04-10", "A", yes_price=0.95, outcome=1.0),
            make_training_sample("2026-04-10", "B", yes_price=0.05, outcome=0.0),
            make_training_sample("2026-04-11", "A", yes_price=0.94, outcome=1.0),
            make_training_sample("2026-04-11", "B", yes_price=0.06, outcome=0.0),
            make_training_sample("2026-04-12", "A", yes_price=0.93, outcome=1.0),
            make_training_sample("2026-04-12", "B", yes_price=0.07, outcome=0.0),
        ]
        report = run_temperature_training_from_samples(
            samples,
            registry=WeatherRegistry.from_file(),
            policy=TemperatureTrainingPolicy(
                min_samples_per_city_source=6,
                min_unique_days=4,
                holdout_fraction=0.25,
                max_trust_score_delta_per_run=5.0,
            ),
            generated_at="2026-04-14T12:00:00+00:00",
        )

        self.assertEqual(report["candidate_updates"], [])
        [group] = report["group_reports"]
        self.assertFalse(group["gates"]["min_unique_days"])
        self.assertEqual(group["decision_reason"], "min_unique_days")

    def test_blocks_candidate_when_holdout_degrades(self):
        samples = [
            make_training_sample("2026-04-10", "A", yes_price=0.95, outcome=1.0),
            make_training_sample("2026-04-10", "B", yes_price=0.05, outcome=0.0),
            make_training_sample("2026-04-11", "A", yes_price=0.94, outcome=1.0),
            make_training_sample("2026-04-11", "B", yes_price=0.06, outcome=0.0),
            make_training_sample("2026-04-12", "A", yes_price=0.93, outcome=1.0),
            make_training_sample("2026-04-12", "B", yes_price=0.07, outcome=0.0),
            make_training_sample("2026-04-13", "A", yes_price=0.95, outcome=0.0),
            make_training_sample("2026-04-13", "B", yes_price=0.05, outcome=1.0),
        ]
        report = run_temperature_training_from_samples(
            samples,
            registry=WeatherRegistry.from_file(),
            policy=TemperatureTrainingPolicy(
                min_samples_per_city_source=8,
                min_unique_days=4,
                holdout_fraction=0.25,
                max_trust_score_delta_per_run=5.0,
            ),
            generated_at="2026-04-14T12:00:00+00:00",
        )

        self.assertEqual(report["candidate_updates"], [])
        [group] = report["group_reports"]
        self.assertFalse(group["gates"]["holdout_must_not_degrade"])
        self.assertEqual(group["decision_reason"], "holdout_must_not_degrade")

    def test_caps_candidate_delta_to_policy_limit(self):
        samples = [
            make_training_sample("2026-04-10", "A", yes_price=0.95, outcome=1.0),
            make_training_sample("2026-04-10", "B", yes_price=0.05, outcome=0.0),
            make_training_sample("2026-04-10", "C", yes_price=0.92, outcome=1.0),
            make_training_sample("2026-04-11", "A", yes_price=0.94, outcome=1.0),
            make_training_sample("2026-04-11", "B", yes_price=0.06, outcome=0.0),
            make_training_sample("2026-04-11", "C", yes_price=0.93, outcome=1.0),
            make_training_sample("2026-04-12", "A", yes_price=0.95, outcome=1.0),
            make_training_sample("2026-04-12", "B", yes_price=0.04, outcome=0.0),
            make_training_sample("2026-04-12", "C", yes_price=0.91, outcome=1.0),
            make_training_sample("2026-04-13", "A", yes_price=0.96, outcome=1.0),
            make_training_sample("2026-04-13", "B", yes_price=0.05, outcome=0.0),
            make_training_sample("2026-04-13", "C", yes_price=0.92, outcome=1.0),
        ]
        report = run_temperature_training_from_samples(
            samples,
            registry=WeatherRegistry.from_file(),
            policy=TemperatureTrainingPolicy(
                min_samples_per_city_source=8,
                min_unique_days=4,
                holdout_fraction=0.25,
                max_trust_score_delta_per_run=2.0,
                min_trust_score_delta_to_emit=1.0,
            ),
            generated_at="2026-04-14T12:00:00+00:00",
        )

        [candidate] = report["candidate_updates"]
        self.assertEqual(candidate["candidate_trust_score"], 92.0)
        self.assertEqual(candidate["trust_score_delta"], 2.0)

    def test_apply_updates_mutates_registry_and_marks_report_non_dry_run(self):
        registry = WeatherRegistry.from_file()
        report = {
            "summary": {
                "generated_at": "2026-04-16T08:00:00+00:00",
                "dry_run": True,
                "registry_mutated": False,
                "records": {"candidate_updates": 1, "blocked_groups": 0},
            },
            "candidate_updates": [
                {
                    "city_id": "miami_fl",
                    "source_id": "src_nws_mfl",
                    "current_trust_score": 90.0,
                    "candidate_trust_score": 95.0,
                    "sample_size": 12,
                    "dry_run": True,
                    "reason": "Dry-run only; candidate trust score calibrated from resolved temperature-market prices without mutating the registry.",
                }
            ],
            "group_reports": [],
        }

        updated = apply_price_aware_training_updates(
            report,
            registry=registry,
            reviewed_at="2026-04-16T08:00:00+00:00",
        )

        self.assertFalse(updated["summary"]["dry_run"])
        self.assertTrue(updated["summary"]["registry_mutated"])
        self.assertEqual(updated["summary"]["records"]["applied_updates"], 1)
        [applied] = updated["candidate_updates"]
        self.assertFalse(applied["dry_run"])
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["reviewed_at"], "2026-04-16T08:00:00+00:00")
        self.assertEqual(applied["updated_source"]["trust_score"], 95.0)
        self.assertEqual(applied["updated_source"]["metrics"]["sample_size"], 12)
        source = next(source for source in registry.as_dict()["sources"] if source["source_id"] == "src_nws_mfl")
        self.assertEqual(source["trust_score"], 95.0)
        self.assertEqual(source["last_reviewed"], "2026-04-16T08:00:00+00:00")

    def test_cli_writes_summary_and_candidate_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "kalshi.csv"
            summary_path = root / "summary.json"
            candidate_path = root / "candidates.json"

            with input_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "EVENT_TICKER",
                        "MARKET_TICKER",
                        "MARKET_TITLE",
                        "RESULT",
                        "END_DT",
                        "CLOSED_DT",
                        "YES_PRICE",
                    ],
                )
                writer.writeheader()
                for row in [
                    ("2026-04-10", "A", "0.95", "yes"),
                    ("2026-04-10", "B", "0.05", "no"),
                    ("2026-04-10", "C", "0.92", "yes"),
                    ("2026-04-11", "A", "0.94", "yes"),
                    ("2026-04-11", "B", "0.06", "no"),
                    ("2026-04-11", "C", "0.91", "yes"),
                    ("2026-04-12", "A", "0.93", "yes"),
                    ("2026-04-12", "B", "0.07", "no"),
                    ("2026-04-12", "C", "0.90", "yes"),
                    ("2026-04-13", "A", "0.95", "yes"),
                    ("2026-04-13", "B", "0.06", "no"),
                    ("2026-04-13", "C", "0.91", "yes"),
                ]:
                    day, suffix, yes_price, result = row
                    writer.writerow(
                        {
                            "EVENT_TICKER": "KXHIGHMIA-26APR14",
                            "MARKET_TICKER": f"KXHIGHMIA-26APR14-{suffix}-{day}",
                            "MARKET_TITLE": f"Will the high temp in Miami be above 84° on {day}?",
                            "RESULT": result,
                            "END_DT": f"{day}T20:00:00Z",
                            "CLOSED_DT": f"{day}T23:00:00Z",
                            "YES_PRICE": yes_price,
                        }
                    )

            rc = weather_train_main(
                [
                    "--input",
                    str(input_path),
                    "--summary-output",
                    str(summary_path),
                    "--candidate-output",
                    str(candidate_path),
                    "--min-samples-per-city-source",
                    "8",
                    "--min-unique-days",
                    "4",
                ]
            )

            self.assertEqual(rc, 0)
            self.assertTrue(summary_path.exists())
            self.assertTrue(candidate_path.exists())

            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            candidates = json.loads(candidate_path.read_text(encoding="utf-8"))

            self.assertTrue(summary["dry_run"])
            self.assertFalse(summary["registry_mutated"])
            self.assertEqual(summary["records"]["candidate_updates"], 1)
            self.assertEqual(summary["evaluator"]["runtime"], "codex-cli")
            self.assertEqual(summary["evaluator"]["model"], "gpt-5.4")
            self.assertIn("codex exec -m gpt-5.4", summary["evaluator"]["codex_command"])
            self.assertEqual(len(candidates["candidate_updates"]), 1)
            self.assertEqual(candidates["evaluator"]["runtime"], "codex-cli")
            self.assertTrue(candidates["candidate_updates"][0]["dry_run"])

    def test_cli_apply_updates_persists_registry_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "kalshi.csv"
            summary_path = root / "summary.json"
            candidate_path = root / "candidates.json"
            registry_path = root / "registry.json"
            registry_path.write_text(Path("docs/weather/city_registry_starter.json").read_text(encoding="utf-8"), encoding="utf-8")

            with input_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "EVENT_TICKER",
                        "MARKET_TICKER",
                        "MARKET_TITLE",
                        "RESULT",
                        "END_DT",
                        "CLOSED_DT",
                        "YES_PRICE",
                    ],
                )
                writer.writeheader()
                for row in [
                    ("2026-04-10", "A", "0.95", "yes"),
                    ("2026-04-10", "B", "0.05", "no"),
                    ("2026-04-10", "C", "0.92", "yes"),
                    ("2026-04-11", "A", "0.94", "yes"),
                    ("2026-04-11", "B", "0.06", "no"),
                    ("2026-04-11", "C", "0.91", "yes"),
                    ("2026-04-12", "A", "0.93", "yes"),
                    ("2026-04-12", "B", "0.07", "no"),
                    ("2026-04-12", "C", "0.90", "yes"),
                    ("2026-04-13", "A", "0.95", "yes"),
                    ("2026-04-13", "B", "0.06", "no"),
                    ("2026-04-13", "C", "0.91", "yes"),
                ]:
                    day, suffix, yes_price, result = row
                    writer.writerow(
                        {
                            "EVENT_TICKER": "KXHIGHMIA-26APR14",
                            "MARKET_TICKER": f"KXHIGHMIA-26APR14-{suffix}-{day}",
                            "MARKET_TITLE": f"Will the high temp in Miami be above 84° on {day}?",
                            "RESULT": result,
                            "END_DT": f"{day}T20:00:00Z",
                            "CLOSED_DT": f"{day}T23:00:00Z",
                            "YES_PRICE": yes_price,
                        }
                    )

            rc = weather_train_main(
                [
                    "--input",
                    str(input_path),
                    "--registry",
                    str(registry_path),
                    "--summary-output",
                    str(summary_path),
                    "--candidate-output",
                    str(candidate_path),
                    "--min-samples-per-city-source",
                    "8",
                    "--min-unique-days",
                    "4",
                    "--apply-updates",
                ]
            )

            self.assertEqual(rc, 0)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            candidates = json.loads(candidate_path.read_text(encoding="utf-8"))
            registry_data = json.loads(registry_path.read_text(encoding="utf-8"))
            src = next(source for source in registry_data["sources"] if source["source_id"] == "src_nws_mfl")

            self.assertFalse(summary["dry_run"])
            self.assertTrue(summary["registry_mutated"])
            self.assertTrue(summary["registry_saved"])
            self.assertEqual(summary["records"]["applied_updates"], 1)
            self.assertEqual(len(candidates["applied_updates"]), 1)
            self.assertFalse(candidates["candidate_updates"][0]["dry_run"])
            self.assertTrue(candidates["candidate_updates"][0]["applied"])
            self.assertEqual(src["trust_score"], 95.0)
            self.assertEqual(src["metrics"]["sample_size"], 12)

    def test_cli_batches_training_and_tracks_batching_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_path = root / "kalshi.csv"
            summary_path = root / "summary.json"
            candidate_path = root / "candidates.json"

            with input_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "EVENT_TICKER",
                        "MARKET_TICKER",
                        "MARKET_TITLE",
                        "RESULT",
                        "END_DT",
                        "CLOSED_DT",
                        "YES_PRICE",
                    ],
                )
                writer.writeheader()
                rows = [
                    ("2026-04-10", "A", "0.95", "yes"),
                    ("2026-04-10", "B", "0.05", "no"),
                    ("2026-04-10", "C", "0.92", "yes"),
                    ("2026-04-11", "A", "0.94", "yes"),
                    ("2026-04-11", "B", "0.06", "no"),
                    ("2026-04-11", "C", "0.91", "yes"),
                    ("2026-04-12", "A", "0.93", "yes"),
                    ("2026-04-12", "B", "0.07", "no"),
                    ("2026-04-12", "C", "0.90", "yes"),
                    ("2026-04-13", "A", "0.95", "yes"),
                    ("2026-04-13", "B", "0.06", "no"),
                    ("2026-04-13", "C", "0.91", "yes"),
                ]
                for row in rows:
                    day, suffix, yes_price, result = row
                    writer.writerow(
                        {
                            "EVENT_TICKER": "KXHIGHMIA-26APR14",
                            "MARKET_TICKER": f"KXHIGHMIA-26APR14-{suffix}-{day}",
                            "MARKET_TITLE": f"Will the high temp in Miami be above 84° on {day}?",
                            "RESULT": result,
                            "END_DT": f"{day}T20:00:00Z",
                            "CLOSED_DT": f"{day}T23:00:00Z",
                            "YES_PRICE": yes_price,
                        }
                    )

            rc = weather_train_main(
                [
                    "--input",
                    str(input_path),
                    "--summary-output",
                    str(summary_path),
                    "--candidate-output",
                    str(candidate_path),
                    "--min-samples-per-city-source",
                    "4",
                    "--min-unique-days",
                    "2",
                    "--max-records",
                    "12",
                    "--batch-size",
                    "6",
                    "--max-batches",
                    "2",
                ]
            )

            self.assertEqual(rc, 0)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            candidates = json.loads(candidate_path.read_text(encoding="utf-8"))

            self.assertTrue(summary["dry_run"])
            self.assertEqual(summary["records"]["input_records"], 12)
            self.assertEqual(summary["records"]["batches_executed"], 2)
            self.assertEqual(summary["batching"]["enabled"], True)
            self.assertEqual(summary["batching"]["batches_executed"], 2)
            self.assertEqual(summary["batching"]["batch_size"], 6)
            self.assertEqual(summary["batching"]["total_records_selected"], 12)
            self.assertEqual(len(candidates["batch_reports"]), 2)


if __name__ == "__main__":
    unittest.main()
