import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from bot.collector_replay_index import build_collector_replay_index
from scripts.collector_lane_replay import auto_resolve_collector_lane_replay, build_collector_lane_replay


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class CollectorLaneReplayTests(unittest.TestCase):
    def test_saved_router_guard_combination_names_existing_replay_components(self):
        from scripts.paper_shadow_lane_compose_replay import _load_config, _normalize_composition
        config = _normalize_composition(_load_config(
            Path(__file__).resolve().parent.parent / "lane_compositions" / "stable_router_guard_kelly.yaml"))
        self.assertEqual(config["base_lane"], "control_stable")
        self.assertEqual(config["action_lane"], "control_stable")
        self.assertEqual(config["price_lane"], "shadow_fee_aware_loss_guard")
        self.assertEqual(config["vetoes"], [
            {"lane": "shadow_source_router", "mode": "require_agreement", "required": True},
            {"lane": "shadow_fee_aware_loss_guard", "mode": "require_agreement", "required": True},
        ])

    def test_saved_combination_runs_its_existing_lanes_on_one_collector_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshots = root / "collector.jsonl"
            config = root / "combination.yaml"
            output = root / "data" / "derived_reports" / "combination"
            _write_jsonl(snapshots, [{
                "run_id": "collector-1", "shared_candidate_id": "candidate-1",
                "market_id": "KXCOMBO", "observed_at": "2026-07-01T12:00:00+00:00",
                "yes_price": 0.30, "no_price": 0.70, "confidence": 0.91,
                "main_decision": {"action": "BUY_NO", "size": 5.0},
            }])
            config.write_text("\n".join([
                "composition:", "  name: stable_with_confidence_veto",
                "  base_lane: control_stable", "  action_lane: control_stable",
                "  sizing_lane: control_stable", "  price_lane: control_stable",
                "  vetoes:", "    - lane: shadow_confidence_floor",
                "      mode: require_agreement", "      required: true", "",
            ]), encoding="utf-8")
            result = build_collector_lane_replay(
                snapshot_path=snapshots, output_dir=output,
                enabled_lanes=[], composition_config=config,
            )
            rows = [json.loads(line) for line in result.lane_decision_path.read_text().splitlines()]
            composed = [json.loads(line) for line in (output / "composition_rows.jsonl").read_text().splitlines()]
            self.assertEqual({row["policy"] for row in rows}, {"control_stable", "shadow_confidence_floor"})
            self.assertEqual(result.summary["composition"]["candidate_groups"], 1)
            self.assertEqual(result.summary["composition"]["configuration_sha256"], hashlib.sha256(config.read_bytes()).hexdigest())
            self.assertEqual(len(composed), 1)
            self.assertEqual(composed[0]["shared_candidate_id"], "candidate-1")
            self.assertFalse(result.summary["promotion_eligible"])

    def test_combination_rejects_unknown_lane_before_creating_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshots = root / "collector.jsonl"
            config = root / "combination.yaml"
            output = root / "data" / "derived_reports" / "invalid"
            _write_jsonl(snapshots, [{"run_id": "c1", "shared_candidate_id": "one", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00"}])
            config.write_text("composition:\n  base_lane: typo_not_a_lane\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown combination lane"):
                build_collector_lane_replay(snapshot_path=snapshots, output_dir=output,
                                            enabled_lanes=[], composition_config=config)
            self.assertFalse(output.exists())

    def test_replays_selected_index_rows_with_index_and_manifest_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshots = root / "collector_snapshots.jsonl"
            index_path = root / "collector_replay_index.jsonl"
            manifest_path = root / "collector_replay_index.manifest.json"
            output = root / "data" / "derived_reports" / "indexed_replay"
            _write_jsonl(
                snapshots,
                [
                    {
                        "run_id": "collector-001",
                        "shared_candidate_id": "candidate-001",
                        "market_id": "KXONE",
                        "observed_at": "2026-07-01T12:00:00+00:00",
                        "yes_price": 0.30,
                        "no_price": 0.70,
                        "main_decision": {"action": "BUY_NO", "size": 5.0},
                    },
                    {
                        "run_id": "collector-002",
                        "shared_candidate_id": "candidate-002",
                        "market_id": "KXTWO",
                        "observed_at": "2026-07-02T12:00:00+00:00",
                        "yes_price": 0.40,
                        "no_price": 0.60,
                        "main_decision": {"action": "BUY_YES", "size": 5.0},
                    },
                ],
            )
            build_collector_replay_index(snapshots, index_path, manifest_path)

            result = build_collector_lane_replay(
                index_path=index_path,
                manifest_path=manifest_path,
                output_dir=output,
                enabled_lanes=["control_stable"],
                market_ids=["KXTWO"],
                max_rows=1,
            )

            rows = [json.loads(line) for line in result.lane_decision_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["market_id"], "KXTWO")
            self.assertEqual(rows[0]["collector_source_row_index"], 2)
            self.assertEqual(rows[0]["collector_replay_index_path"], str(index_path))
            self.assertEqual(rows[0]["collector_replay_index_manifest_path"], str(manifest_path))
            self.assertEqual(result.summary["collector_rows"], 1)
            self.assertEqual(result.summary["index_selection"]["market_ids"], ["KXTWO"])
            self.assertEqual(result.summary["index_selection"]["max_rows"], 1)
            self.assertEqual(result.summary["replay_index"]["manifest_sha256"], hashlib.sha256(manifest_path.read_bytes()).hexdigest())
            self.assertNotIn("decision_artifact", index_path.read_text(encoding="utf-8"))

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
            _write_jsonl(resolutions, [{"market_id": "KXTEST-1", "outcome": "NO",
                                        "settlement_ts": "2026-07-02T12:00:00+00:00"}])

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
            self.assertEqual(result.summary["methodology"], "recorded_decision_fixed_notional_diagnostic")
            self.assertFalse(result.summary["paper_parity_claim"])
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
            # A finalized market-only API outcome without a settlement clock
            # remains directional evidence, not a paper cash-flow receipt.
            self.assertEqual(refreshed["pnl"]["pnl_calculable_rows"], 0)
            self.assertEqual(refreshed["pnl"]["blocker_counts"], {"missing_settlement_timestamp": 2})

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
