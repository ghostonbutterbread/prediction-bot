import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bot.file_ops import append_jsonl, load_jsonl
from bot.prediction_lab_shadow_delta import (
    build_shadow_delta,
    build_shadow_delta_compact_review,
    summarize_shadow_delta_rows,
    write_shadow_delta_compact_review_jsonl,
)


ROOT = Path(__file__).resolve().parents[1]


def _beta_shadow_policy(*features: str) -> dict:
    return {
        "version": "beta",
        "mode": "shadow",
        "shadow": True,
        "enabled_features": {name: True for name in features},
    }


def _artifact(*, action: str = "BUY_YES", reason_code: str = "approved", size=10.0, reasoning: dict | None = None) -> dict:
    return {
        "final_action": action,
        "final_reason_code": reason_code,
        "shared_core_decision": {
            "approved": action in {"BUY_YES", "BUY_NO"},
            "reason_code": reason_code,
            "requested_position_size": size,
            "reasoning": dict(reasoning or {}),
        },
    }


def _summary_delta(
    *,
    dedupe_key: str | None = "m1|r1|beta-shadow",
    status: str = "complete",
    changed: bool | None = True,
    action_changed: bool | None = True,
    lane_changed: bool | None = False,
    evidence_sources: list[str] | None = None,
) -> dict:
    return {
        key: value
        for key, value in {
            "schema_version": 1,
            "mode": "beta_shadow_delta",
            "status": status,
            "comparison_complete": status == "complete",
            "action_comparison_available": action_changed is not None,
            "stable": {"action": "SKIP", "selected_lane": "edge"},
            "shadow": {"action": "BUY_YES" if action_changed is not None else None, "selected_lane": "hidden_gem" if lane_changed else "edge"},
            "changed": changed,
            "action_changed": action_changed,
            "side_changed": action_changed,
            "buy_decision_changed": action_changed,
            "reason_changed": action_changed,
            "size_changed": False,
            "lane_changed": lane_changed,
            "dedupe_key": dedupe_key,
            "evidence_sources": evidence_sources or ["beta_lane_gate"],
        }.items()
        if value is not None or key not in {"dedupe_key"}
    }


def _review_artifact() -> dict:
    return {
        "mode": "prediction_lab",
        "market_id": "m-review",
        "observed_at": "2026-05-15T10:00:00+00:00",
        "final_action": "BUY_YES",
        "final_reason_code": "approved",
        "market_route": {"route_id": "weather.daily_temperature", "group": "weather"},
        "source_context": {
            "source": "provided",
            "source_mode": "recorded_as_of",
            "as_of": "2026-05-15T10:00:00+00:00",
            "data": {
                "market_metadata": {
                    "market_group": "weather",
                    "series": "daily_temperature",
                    "event_ticker": "KXTEST-26MAY15",
                    "market_route": {"route_id": "weather.daily_temperature"},
                },
                "weather_source_snapshot": {
                    "mode": "recorded_as_of",
                    "source_name": "weather",
                    "as_of": "2026-05-15T10:00:00+00:00",
                    "weather_date": "2026-05-15",
                    "station_id": "KNYC",
                    "date_validation": {
                        "ok": True,
                        "reason": "matched",
                        "market_date": "2026-05-15",
                        "weather_date": "2026-05-15",
                    },
                },
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
        "execution_snapshot_source": "book",
        "order_book_snapshot": {"source": "book", "data": {"best_yes_ask": 0.42}},
        "pre_logic_order_book_snapshot": {"source": "book", "data": {"best_yes_ask": 0.42}},
        "post_logic_order_book_snapshot": {"source": "book", "data": {"best_yes_ask": 0.42}},
    }


def _review_row(
    *,
    market_id: str = "m-review",
    run_id: str = "r-review",
    prediction_id: str | None = None,
    recorded_prediction: bool | None = None,
    shadow_delta: dict | None = None,
    decision_artifact: dict | None = None,
    patch: dict | None = None,
) -> dict:
    row = {
        "timestamp": "2026-05-15T10:00:00+00:00",
        "observed_at": "2026-05-15T10:00:00+00:00",
        "market_id": market_id,
        "run_id": run_id,
        "group": "weather",
        "series": "daily_temperature",
        "event_ticker": "KXTEST-26MAY15",
        "market_route": {"route_id": "weather.daily_temperature"},
        "snapshot_key": market_id,
        "direction": "BUY_YES",
        "decision_type": "buy_yes",
        "weather_risk": {"status": "ok"},
    }
    if prediction_id is not None:
        row["prediction_id"] = prediction_id
    if recorded_prediction is not None:
        row["recorded_prediction"] = recorded_prediction
    if shadow_delta is not None:
        row["shadow_delta"] = shadow_delta
    if decision_artifact is not None:
        row["decision_artifact"] = decision_artifact
    if patch:
        row.update(patch)
    return row


class PredictionLabShadowDeltaTests(unittest.TestCase):
    def test_omits_without_beta_shadow_policy_or_shadow_evidence(self):
        reasoning = {
            "strategy_policy_status": {"version": "stable", "mode": "off", "shadow": False},
            "lane_sizing": {
                "active": True,
                "shadow": True,
                "enforced": False,
                "beta_adjusted_size": 4.0,
            },
        }
        self.assertIsNone(build_shadow_delta(_artifact(reasoning=reasoning), "KXTEST", "run-1"))
        self.assertIsNone(
            build_shadow_delta(
                _artifact(reasoning={}),
                "KXTEST",
                "run-1",
                fallback_strategy_policy={
                    "version": "beta",
                    "beta": {"mode": "shadow", "features": {"lane_sizing_caps": True}},
                },
            )
        )

    def test_beta_lane_gate_allowed_false_changes_action_to_skip(self):
        reasoning = {
            "strategy_policy_status": _beta_shadow_policy("hidden_gem_lane_gates"),
            "strategy_lane": {
                "lane_id": "hidden_gem",
                "evidence": {
                    "beta_lane_gate": {
                        "active": True,
                        "shadow": True,
                        "enforced": False,
                        "lane_id": "confidence_slow_profit",
                        "allowed": False,
                        "reason_code": "strategy_lane_disabled",
                    }
                },
            },
        }

        delta = build_shadow_delta(_artifact(reasoning=reasoning), "KXTEST", "run-1")

        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertEqual(delta["status"], "complete")
        self.assertTrue(delta["comparison_complete"])
        self.assertTrue(delta["action_comparison_available"])
        self.assertEqual(delta["stable"]["action"], "BUY_YES")
        self.assertEqual(delta["shadow"]["action"], "SKIP")
        self.assertEqual(delta["shadow"]["reason_code"], "strategy_lane_disabled")
        self.assertTrue(delta["action_changed"])
        self.assertTrue(delta["buy_decision_changed"])
        self.assertEqual(delta["evidence_sources"], ["beta_lane_gate"])

    def test_beta_allowed_lane_from_stable_skip_is_partial_not_unchanged(self):
        reasoning = {
            "strategy_policy_status": _beta_shadow_policy("hidden_gem_lane_gates"),
            "strategy_lane": {
                "lane_id": "edge",
                "allowed": True,
                "reason_code": "edge_lane_selected",
                "evidence": {
                    "beta_lane_gate": {
                        "beta_behavior_enabled": True,
                        "beta_behavior_enforced": False,
                        "lane_id": "confidence_slow_profit",
                        "allowed": True,
                        "reason_code": "confidence_slow_profit_lane_selected",
                        "differs_from_final": True,
                    }
                },
            },
        }

        delta = build_shadow_delta(
            _artifact(action="SKIP", reason_code="edge_below_threshold", size=None, reasoning=reasoning),
            "KXTEST",
            "run-1",
        )

        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertEqual(delta["status"], "partial_beta_evidence")
        self.assertFalse(delta["comparison_complete"])
        self.assertFalse(delta["action_comparison_available"])
        self.assertEqual(delta["stable"]["action"], "SKIP")
        self.assertIsNone(delta["shadow"]["action"])
        self.assertEqual(delta["shadow"]["decision_type"], "unknown")
        self.assertEqual(delta["shadow"]["selected_lane"], "confidence_slow_profit")
        self.assertTrue(delta["changed"])
        self.assertTrue(delta["lane_changed"])
        self.assertIsNone(delta["action_changed"])
        self.assertIsNone(delta["buy_decision_changed"])
        self.assertEqual(delta["evidence_sources"], ["beta_lane_gate"])

    def test_weather_beta_rejection_changes_shadow_to_skip(self):
        reasoning = {
            "weather_risk": {
                "beta_gate": {
                    "policy": _beta_shadow_policy("weather_hidden_gem_evidence_card"),
                    "active": True,
                    "shadow": True,
                    "enforced": False,
                    "would_reject": True,
                    "reason_code": "weather_tail_hidden_gem_distribution_probability_below_threshold",
                }
            }
        }

        delta = build_shadow_delta(_artifact(reasoning=reasoning), "KXWEATHER", "run-2")

        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertEqual(delta["shadow"]["action"], "SKIP")
        self.assertEqual(
            delta["shadow"]["reason_code"],
            "weather_tail_hidden_gem_distribution_probability_below_threshold",
        )
        self.assertTrue(delta["action_changed"])
        self.assertTrue(delta["buy_decision_changed"])
        self.assertEqual(delta["evidence_sources"], ["weather_risk.beta_gate"])

    def test_weather_beta_sizing_changes_shadow_size_only(self):
        reasoning = {
            "weather_risk": {
                "beta_sizing_gate": {
                    "policy": _beta_shadow_policy("bucket_distribution_scoring"),
                    "active": True,
                    "shadow": True,
                    "enforced": False,
                    "would_adjust_size": True,
                    "requested_size": 10.0,
                    "beta_adjusted_size": 2.5,
                }
            }
        }

        delta = build_shadow_delta(_artifact(reasoning=reasoning), "KXWEATHER", "run-3")

        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertEqual(delta["stable"]["requested_position_size"], 10.0)
        self.assertEqual(delta["shadow"]["requested_position_size"], 2.5)
        self.assertFalse(delta["action_changed"])
        self.assertTrue(delta["size_changed"])
        self.assertEqual(delta["evidence_sources"], ["weather_risk.beta_sizing_gate"])

    def test_dedupe_key_uses_market_run_and_sources_describe_shadow_evidence(self):
        reasoning = {
            "lane_sizing": {
                "policy": _beta_shadow_policy("lane_sizing_caps"),
                "active": True,
                "shadow": True,
                "enforced": False,
                "beta_adjusted_size": 4.0,
            },
            "weather_risk": {
                "beta_sizing_gate": {
                    "active": True,
                    "shadow": True,
                    "enforced": False,
                    "beta_adjusted_size": 3.0,
                }
            },
        }

        delta = build_shadow_delta(_artifact(reasoning=reasoning), "KXTEST", "run-4")

        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertEqual(delta["dedupe_key"], "KXTEST|run-4|beta-shadow")
        self.assertEqual(delta["evidence_sources"], ["lane_sizing", "weather_risk.beta_sizing_gate"])
        self.assertEqual(delta["shadow"]["requested_position_size"], 3.0)

    def test_summary_dedupes_by_key_and_prefers_recorded_prediction(self):
        summary = summarize_shadow_delta_rows(
            [
                {
                    "run_id": "r1",
                    "market_id": "m1",
                    "recorded_prediction": False,
                    "shadow_delta": _summary_delta(changed=False, action_changed=False),
                },
                {
                    "run_id": "r1",
                    "market_id": "m1",
                    "recorded_prediction": True,
                    "shadow_delta": _summary_delta(changed=True, action_changed=True),
                },
            ],
            prediction_lab_rows=True,
        )

        self.assertEqual(summary["total_shadow_delta_rows"], 2)
        self.assertEqual(summary["total_shadow_delta_opportunities"], 1)
        self.assertEqual(summary["deduped_duplicate_rows"], 1)
        self.assertEqual(summary["changed_rows"], 1)
        self.assertEqual(summary["action_changed"], 1)
        self.assertEqual(summary["action_unchanged"], 0)

    def test_summary_fallback_key_and_decision_artifact_preference_do_not_overcount(self):
        summary = summarize_shadow_delta_rows(
            [
                {
                    "run_id": "r2",
                    "market_id": "m2",
                    "shadow_delta": _summary_delta(dedupe_key=None, changed=False, action_changed=False),
                },
                {
                    "run_id": "r2",
                    "market_id": "m2",
                    "decision_artifact": {"final_action": "BUY_YES"},
                    "shadow_delta": _summary_delta(dedupe_key=None, changed=True, action_changed=True),
                },
            ],
            prediction_lab_rows=True,
        )

        self.assertEqual(summary["total_shadow_delta_rows"], 2)
        self.assertEqual(summary["total_shadow_delta_opportunities"], 1)
        self.assertEqual(summary["changed_rows"], 1)
        self.assertEqual(summary["action_changed"], 1)

    def test_summary_partial_beta_evidence_counts_lane_but_not_unchanged_action(self):
        summary = summarize_shadow_delta_rows(
            [
                {
                    "run_id": "r3",
                    "market_id": "m3",
                    "shadow_delta": _summary_delta(
                        dedupe_key=None,
                        status="partial_beta_evidence",
                        changed=True,
                        action_changed=None,
                        lane_changed=True,
                    ),
                }
            ],
            prediction_lab_rows=True,
        )

        self.assertEqual(summary["total_shadow_delta_opportunities"], 1)
        self.assertEqual(summary["status_counts"], {"partial_beta_evidence": 1})
        self.assertEqual(summary["unavailable_action_comparisons"], 1)
        self.assertEqual(summary["lane_changed"], 1)
        self.assertEqual(summary["action_changed"], 0)
        self.assertEqual(summary["action_unchanged"], 0)

    def test_compact_review_excludes_unchanged_and_no_shadow_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            predictions = Path(tmpdir) / "predictions.jsonl"
            snapshots = Path(tmpdir) / "market_snapshots.jsonl"
            append_jsonl(predictions, _review_row(prediction_id="p-no-shadow", recorded_prediction=True))
            append_jsonl(
                snapshots,
                _review_row(
                    shadow_delta=_summary_delta(
                        dedupe_key="m-review|r-review|beta-shadow",
                        changed=False,
                        action_changed=False,
                    ),
                    recorded_prediction=False,
                ),
            )

            result = build_shadow_delta_compact_review(
                predictions_path=predictions,
                market_snapshots_path=snapshots,
            )

        self.assertEqual(result["total_input_rows"], 2)
        self.assertEqual(result["total_shadow_delta_rows"], 1)
        self.assertEqual(result["exported_rows"], 0)
        self.assertEqual(result["rows"], [])

    def test_compact_review_dedupes_prediction_snapshot_pair_and_prefers_prediction_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            predictions = Path(tmpdir) / "predictions.jsonl"
            snapshots = Path(tmpdir) / "market_snapshots.jsonl"
            append_jsonl(
                snapshots,
                _review_row(
                    recorded_prediction=False,
                    shadow_delta=_summary_delta(
                        dedupe_key="m-review|r-review|beta-shadow",
                        changed=True,
                        action_changed=True,
                    ),
                ),
            )
            append_jsonl(
                predictions,
                _review_row(
                    prediction_id="p-review",
                    recorded_prediction=True,
                    shadow_delta=_summary_delta(
                        dedupe_key="m-review|r-review|beta-shadow",
                        changed=True,
                        action_changed=True,
                        evidence_sources=["prediction_row_source"],
                    ),
                ),
            )

            result = build_shadow_delta_compact_review(
                predictions_path=predictions,
                market_snapshots_path=snapshots,
            )

        self.assertEqual(result["total_shadow_delta_rows"], 2)
        self.assertEqual(result["deduped_duplicate_rows"], 1)
        self.assertEqual(result["exported_rows"], 1)
        row = result["rows"][0]
        self.assertEqual(row["source_kind"], "prediction")
        self.assertEqual(row["prediction_id"], "p-review")
        self.assertTrue(row["prediction_row_available"])
        self.assertTrue(row["market_snapshot_row_available"])
        self.assertEqual(row["shadow_delta"]["evidence_sources"], ["prediction_row_source"])

    def test_compact_review_keeps_partial_beta_evidence_and_action_unavailable_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            predictions = Path(tmpdir) / "predictions.jsonl"
            snapshots = Path(tmpdir) / "market_snapshots.jsonl"
            append_jsonl(
                predictions,
                _review_row(
                    market_id="m-partial",
                    run_id="r-partial",
                    prediction_id="p-partial",
                    recorded_prediction=True,
                    shadow_delta=_summary_delta(
                        dedupe_key="m-partial|r-partial|beta-shadow",
                        status="partial_beta_evidence",
                        changed=False,
                        action_changed=None,
                        lane_changed=False,
                    ),
                ),
            )
            append_jsonl(
                snapshots,
                _review_row(
                    market_id="m-unavailable",
                    run_id="r-unavailable",
                    recorded_prediction=False,
                    shadow_delta=_summary_delta(
                        dedupe_key="m-unavailable|r-unavailable|beta-shadow",
                        status="complete",
                        changed=None,
                        action_changed=None,
                        lane_changed=False,
                    ),
                ),
            )

            result = build_shadow_delta_compact_review(
                predictions_path=predictions,
                market_snapshots_path=snapshots,
            )

        self.assertEqual(result["exported_rows"], 2)
        statuses = {row["market_id"]: row["shadow_delta"]["status"] for row in result["rows"]}
        self.assertEqual(statuses["m-partial"], "partial_beta_evidence")
        self.assertEqual(statuses["m-unavailable"], "complete")
        for row in result["rows"]:
            self.assertFalse(row["shadow_delta"]["action_comparison_available"])

    def test_compact_review_includes_metadata_without_mutating_or_synthesizing_replay_rows(self):
        source_row = _review_row(
            prediction_id="p-review",
            recorded_prediction=True,
            shadow_delta=_summary_delta(dedupe_key="m-review|r-review|beta-shadow", changed=True, action_changed=True),
            decision_artifact=_review_artifact(),
        )
        original = copy.deepcopy(source_row)
        with tempfile.TemporaryDirectory() as tmpdir:
            predictions = Path(tmpdir) / "predictions.jsonl"
            snapshots = Path(tmpdir) / "market_snapshots.jsonl"
            append_jsonl(predictions, source_row)
            snapshots.write_text("", encoding="utf-8")

            result = build_shadow_delta_compact_review(
                predictions_path=predictions,
                market_snapshots_path=snapshots,
            )
            stored_rows = load_jsonl(predictions)

        self.assertEqual(source_row, original)
        self.assertEqual(stored_rows, [original])
        row = result["rows"][0]
        self.assertEqual(row["decision_artifact_pointer"], "decision_artifact")
        self.assertTrue(row["decision_artifact_available"])
        self.assertEqual(row["route_metadata"]["market_route"]["route_id"], "weather.daily_temperature")
        self.assertTrue(row["weather_metadata"]["weather_source_snapshot_available"])
        self.assertEqual(row["weather_metadata"]["station_id"], "KNYC")
        self.assertEqual(row["source_metadata"]["source_snapshots"][0]["snapshot_ref"], "source_context.data.weather_source_snapshot")
        self.assertTrue(row["order_book_metadata"]["order_book_snapshot_available"])
        self.assertNotIn("decision_artifact", row)
        self.assertNotIn("original_action", row)
        self.assertNotIn("replayed_action", row)


    def test_compact_review_rejects_missing_explicit_inputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            predictions = Path(tmpdir) / "missing_predictions.jsonl"
            snapshots = Path(tmpdir) / "market_snapshots.jsonl"
            snapshots.write_text("", encoding="utf-8")

            with self.assertRaises(FileNotFoundError):
                build_shadow_delta_compact_review(
                    predictions_path=predictions,
                    market_snapshots_path=snapshots,
                )

    def test_compact_review_writer_rejects_output_alias_without_mutating_input(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            predictions = Path(tmpdir) / "predictions.jsonl"
            snapshots = Path(tmpdir) / "market_snapshots.jsonl"
            append_jsonl(
                predictions,
                _review_row(
                    prediction_id="p-review",
                    recorded_prediction=True,
                    shadow_delta=_summary_delta(dedupe_key="m-review|r-review|beta-shadow", changed=True, action_changed=True),
                ),
            )
            snapshots.write_text("", encoding="utf-8")
            before = predictions.read_text(encoding="utf-8")

            with self.assertRaises(ValueError):
                write_shadow_delta_compact_review_jsonl(
                    predictions,
                    predictions_path=predictions,
                    market_snapshots_path=snapshots,
                )

            self.assertEqual(predictions.read_text(encoding="utf-8"), before)

    def test_compact_review_cli_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            predictions = Path(tmpdir) / "predictions.jsonl"
            snapshots = Path(tmpdir) / "market_snapshots.jsonl"
            output = Path(tmpdir) / "shadow_review.jsonl"
            append_jsonl(
                predictions,
                _review_row(
                    prediction_id="p-review",
                    recorded_prediction=True,
                    shadow_delta=_summary_delta(dedupe_key="m-review|r-review|beta-shadow", changed=True, action_changed=True),
                ),
            )
            snapshots.write_text("", encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/prediction_lab_shadow_delta_review.py",
                    "--predictions",
                    str(predictions),
                    "--market-snapshots",
                    str(snapshots),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                check=False,
                text=True,
                capture_output=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["row_type"], "prediction_lab_shadow_delta_compact_review")
        self.assertEqual(rows[0]["source_kind"], "prediction")

    def test_compact_review_cli_rejects_output_input_alias(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            predictions = Path(tmpdir) / "predictions.jsonl"
            snapshots = Path(tmpdir) / "market_snapshots.jsonl"
            append_jsonl(
                predictions,
                _review_row(
                    prediction_id="p-review",
                    recorded_prediction=True,
                    shadow_delta=_summary_delta(dedupe_key="m-review|r-review|beta-shadow", changed=True, action_changed=True),
                ),
            )
            snapshots.write_text("", encoding="utf-8")
            before = predictions.read_text(encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/prediction_lab_shadow_delta_review.py",
                    "--predictions",
                    str(predictions),
                    "--market-snapshots",
                    str(snapshots),
                    "--output",
                    str(predictions),
                ],
                cwd=ROOT,
                check=False,
                text=True,
                capture_output=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(predictions.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
