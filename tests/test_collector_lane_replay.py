import json
import tempfile
import unittest
from pathlib import Path

from scripts.collector_lane_replay import auto_resolve_collector_lane_replay, build_collector_lane_replay


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class CollectorLaneReplayTests(unittest.TestCase):
    def test_derives_non_mutating_lane_rows_from_collector_snapshot_and_scores_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshots = root / "collector_snapshots.jsonl"
            resolutions = root / "resolutions.jsonl"
            output = root / "data" / "derived_reports" / "collector_replay"
            _write_jsonl(
                snapshots,
                [
                    {
                        "run_id": "collector-001",
                        "shared_candidate_id": "candidate-001",
                        "market_id": "KXTEST-1",
                        "observed_at": "2026-07-01T12:00:00+00:00",
                        "yes_price": 0.30,
                        "no_price": 0.70,
                        "confidence": 0.91,
                        "edge": 0.12,
                        "main_decision": {
                            "action": "BUY_NO",
                            "reason_code": "selected",
                            "reason": "Recorded stable decision",
                            "size": 5.0,
                        },
                        "shared_candidate": {
                            "candidate_id": "candidate-001",
                            "market_id": "KXTEST-1",
                            "observed_at": "2026-07-01T12:00:00+00:00",
                            "market": {"id": "KXTEST-1", "question": "Will test resolve NO?"},
                            "decision": {"final_action": "BUY_NO", "confidence": 0.91},
                            "evidence": {"actual_temp_used": 999.0, "source": "recorded_as_of"},
                        },
                    }
                ],
            )
            _write_jsonl(resolutions, [{"market_id": "KXTEST-1", "outcome": "NO"}])

            result = build_collector_lane_replay(
                snapshot_path=snapshots,
                output_dir=output,
                enabled_lanes=["control_stable", "shadow_confidence_floor"],
                resolution_paths=[resolutions],
                default_notional_usd=5.0,
            )

            lane_rows = [json.loads(line) for line in result.lane_decision_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(lane_rows), 2)
            self.assertEqual({row["shared_snapshot_id"] for row in lane_rows}, {"collector-001"})
            self.assertEqual({row["collector_run_id"] for row in lane_rows}, {"collector-001"})
            self.assertEqual({row["collector_source_row_index"] for row in lane_rows}, {1})
            self.assertEqual({row["derived_replay_run_id"] for row in lane_rows}, {result.summary["replay_run_id"]})
            self.assertEqual({row["shared_candidate"]["snapshot_id"] for row in lane_rows}, {"collector-001"})
            self.assertEqual({row["action"] for row in lane_rows}, {"BUY_NO"})
            self.assertTrue(all(row["mutation_contract"]["places_orders"] is False for row in lane_rows))
            self.assertNotIn("actual_temp_used", json.dumps(lane_rows))
            self.assertEqual(result.summary["pnl"]["pnl_calculable_rows"], 2)
            self.assertAlmostEqual(result.summary["pnl"]["total_pnl_usd"], 4.2858)
            buy_rows = [json.loads(line) for line in result.buy_decision_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(buy_rows), 2)
            self.assertEqual(result.summary["collector_rows"], 1)
            self.assertEqual(result.summary["invalid_snapshot_rows"], 0)
            refreshed = auto_resolve_collector_lane_replay(
                output_dir=output,
                fetch_market=lambda market_id: {
                    "ticker": market_id,
                    "status": "finalized",
                    "result": "no",
                    "settlement_value_dollars": "1.0000",
                },
            )
            self.assertEqual(refreshed["resolution_feed"]["resolved_market_count"], 1)
            self.assertEqual(refreshed["pnl"]["pnl_calculable_rows"], 2)

    def test_hydrates_nested_collector_artifact_without_outcome_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshots = root / "collector_snapshots.jsonl"
            output = root / "data" / "derived_reports" / "nested_artifact_replay"
            _write_jsonl(
                snapshots,
                [
                    {
                        "run_id": "collector-nested-001",
                        "shared_candidate_id": "candidate-nested-001",
                        "market_id": "KXNESTED-1",
                        "observed_at": "2026-07-01T12:00:00+00:00",
                        "yes_price": 0.40,
                        "no_price": 0.60,
                        "decision_artifact": {
                            "strategy_signal": {"confidence": 0.88, "edge": 0.14, "model_probability": 0.30},
                            "source_context": {"source_details": [{"provider": "nws", "forecast": 72}]},
                            "source_snapshots": [{"provider": "nws", "forecast": 72, "actual_outcome": "NO"}],
                            "shared_core_decision": {"action": "BUY_NO", "reason_code": "nested_selected", "size": 7.0},
                            "final_action": "BUY_NO",
                            "final_reason": "nested recorded decision",
                        },
                        "normal_decision": {"action": "BUY_YES", "reason_code": "nested_beta", "size": 4.0},
                    }
                ],
            )

            result = build_collector_lane_replay(
                snapshot_path=snapshots,
                output_dir=output,
                enabled_lanes=["control_stable", "shadow_current_beta"],
            )

            rows = [json.loads(line) for line in result.lane_decision_path.read_text(encoding="utf-8").splitlines()]
            by_lane = {row["policy"]: row for row in rows}
            self.assertEqual(by_lane["control_stable"]["action"], "BUY_NO")
            self.assertEqual(by_lane["shadow_current_beta"]["action"], "BUY_YES")
            self.assertEqual(by_lane["control_stable"]["provenance"]["input_confidence"], 0.88)
            serialized = json.dumps(rows)
            self.assertNotIn("actual_outcome", serialized)
            self.assertEqual(result.summary["buy_rows"], 2)


if __name__ == "__main__":
    unittest.main()
