import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from bot.collector_replay_index import build_collector_replay_index
from scripts.collector_lane_replay import _prepare_snapshot, auto_resolve_collector_lane_replay, build_collector_lane_replay


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class CollectorLaneReplayTests(unittest.TestCase):
    def test_replay_config_identity_hashes_the_bytes_that_were_parsed(self):
        from scripts import collector_lane_replay as replay
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshots = root / "collector.jsonl"
            config = root / "idea.yaml"
            output = root / "data" / "derived_reports" / "identity"
            original = b"composition:\n  name: before\n  base_lane: control_stable\n"
            config.write_bytes(original)
            _write_jsonl(snapshots, [{"run_id": "c1", "shared_candidate_id": "one", "market_id": "KXONE",
                                      "observed_at": "2026-07-01T12:00:00+00:00"}])
            load = replay._load_config
            def concurrent_edit(path, **kwargs):
                parsed = load(path, **kwargs)
                path.write_text("composition:\n  name: after\n  base_lane: control_stable\n", encoding="utf-8")
                return parsed
            with patch.object(replay, "_load_config", side_effect=concurrent_edit):
                result = replay.build_collector_lane_replay(snapshot_path=snapshots, output_dir=output,
                                                             enabled_lanes=[], composition_config=config)
            self.assertEqual(result.summary["composition"]["composition"]["name"], "before")
            self.assertEqual(result.summary["composition"]["configuration_sha256"], hashlib.sha256(original).hexdigest())

    def test_composed_replay_input_does_not_carry_nested_resolution_or_market_result(self):
        raw = {
            "run_id": "collector-1", "shared_candidate_id": "c1", "market_id": "KXONE",
            "observed_at": "2026-07-01T12:00:00+00:00",
            "shared_candidate": {"market": {"id": "KXONE", "result": "YES", "official_outcome": "NO", "official_result": "YES", "winner": "YES", "resolved": "YES", "question": "Will it?"},
                                 "evidence": {"source": {"result": "NO", "market_outcome": "YES", "settlement_ts": "2026-07-02T00:00:00+00:00", "settlement_value_dollars": "1.00", "winning_side": "YES", "outcome_known_at": "2026-07-02T01:00:00+00:00", "payout": 1.0, "void": False}, "resolution": {"outcome": "YES"}}},
            "decision_artifact": {"strategy_signal": {"resolution": {"result": "YES"}, "winner": "YES"},
                                  "source_context": {"outcome": "YES"},
                                  "source_snapshots": [{"result": "NO"}]},
        }
        prepared = _prepare_snapshot(raw, candidate_dataset_path="collector.jsonl",
                                     replay_run_id="replay", default_notional_usd=5.0)
        assert prepared is not None
        _, wallet_input, _, _ = prepared
        def keys(value):
            if isinstance(value, dict):
                return set(value) | set().union(*(keys(v) for v in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(v) for v in value))
            return set()
        forbidden = {"outcome", "resolution", "result", "official_outcome", "market_outcome", "settlement_ts",
                     "official_result", "settlement_value_dollars", "winning_side", "outcome_known_at",
                     "winner", "payout", "resolved", "void"}
        self.assertFalse(forbidden & keys(wallet_input.signal))
        self.assertFalse(forbidden & keys(wallet_input.shared_candidate))
        self.assertEqual(wallet_input.shared_candidate["market"]["question"], "Will it?")

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
            self.assertEqual(result.summary["composition"]["wallet_intents_written"], 0)
            self.assertEqual(result.summary["composition"]["buy_without_wallet_intent"], 1)
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

    def test_misspelled_combination_or_veto_is_not_a_silent_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshots = root / "collector.jsonl"
            _write_jsonl(snapshots, [{"run_id": "c1", "shared_candidate_id": "one", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00"}])
            for text in ("compositon:\n  base_lane: control_stable\n",
                         "composition:\n  base_lane: control_stable\n  vetos: []\n"):
                config = root / "combination.yaml"
                config.write_text(text, encoding="utf-8")
                output = root / "data" / "derived_reports" / str(len(text))
                with self.assertRaisesRegex(ValueError, "unknown combination key"):
                    build_collector_lane_replay(snapshot_path=snapshots, output_dir=output,
                                                enabled_lanes=[], composition_config=config)
                self.assertFalse(output.exists())

    def test_invalid_gate_or_exposure_rejects_before_writing_replay_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshots = root / "collector.jsonl"
            _write_jsonl(snapshots, [{"run_id": "c1", "shared_candidate_id": "one", "market_id": "KXONE", "observed_at": "2026-07-01T12:00:00+00:00",
                                      "main_decision": {"action": "BUY_YES", "size": 5.0}}])
            for index, detail in enumerate(("  gates:\n    - field: edge\n      op: does_not_exist\n      value: 0.5\n",
                                            "  exposure:\n    max_rows_per_market: 1\n    selector: does_not_exist\n")):
                config = root / "combination.yaml"
                config.write_text("composition:\n  base_lane: control_stable\n" + detail, encoding="utf-8")
                output = root / "data" / "derived_reports" / str(index)
                with self.assertRaisesRegex(ValueError, "unknown composition (gate op|exposure selector)"):
                    build_collector_lane_replay(snapshot_path=snapshots, output_dir=output,
                                                enabled_lanes=[], composition_config=config)
                self.assertFalse(output.exists())

    def test_unknown_gate_field_rejects_before_writing_replay_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshots = root / "collector.jsonl"
            config = root / "idea.yaml"
            output = root / "data" / "derived_reports" / "bad_gate"
            _write_jsonl(snapshots, [{"run_id": "collector-1", "shared_candidate_id": "c1", "market_id": "KXONE",
                                      "observed_at": "2026-07-01T12:00:00+00:00"}])
            config.write_text("composition:\n  base_lane: control_stable\n  gates:\n"
                              "    - field: source_sampl_count\n      op: gte\n      value: 5\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown composition gate field: source_sampl_count"):
                build_collector_lane_replay(snapshot_path=snapshots, output_dir=output,
                                            enabled_lanes=[], composition_config=config)
            self.assertFalse(output.exists())

    def test_saved_combination_can_emit_a_sealed_wallet_intent_when_inputs_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshots = root / "collector.jsonl"
            output = root / "data" / "derived_reports" / "qualified"
            stamp = "2026-07-01T12:00:00+00:00"
            _write_jsonl(snapshots, [{
                "run_id": "collector-1", "shared_candidate_id": "candidate-1",
                "market_id": "KXQUALIFIED", "observed_at": stamp,
                "main_decision": {"action": "BUY_YES", "size": 5.0},
                "decision_artifact": {"strategy_signal": {
                    "direction": "BUY_YES", "market_id": "KXQUALIFIED",
                    "question": "Will it happen?", "exchange": "kalshi",
                    "market_route": {"allowed": True, "handler_id": "weather"},
                    "model_probability": 0.8, "confidence": 0.9,
                    "best_yes_ask": 0.4}},
                "shared_candidate": {
                    "candidate_id": "candidate-1", "market_id": "KXQUALIFIED",
                    "observed_at": stamp, "market": {"question": "Will it happen?"},
                    "prices": {"order_book_snapshot": {"source": "book", "market_id": "KXQUALIFIED",
                        "observed_at": stamp, "data": {"best_yes_ask": 0.4}}},
                },
            }])
            config = root / "idea.yaml"
            config.write_text("composition:\n  name: qualified\n  base_lane: control_stable\n"
                              "  action_lane: control_stable\n  price_lane: control_stable\n"
                              "  sizing_lane: control_stable\n", encoding="utf-8")
            result = build_collector_lane_replay(snapshot_path=snapshots, output_dir=output,
                                                 enabled_lanes=[], composition_config=config)
            intents = [json.loads(line) for line in (output / "wallet_intents.jsonl").read_text().splitlines()]
            self.assertEqual(len(intents), 1)
            self.assertEqual(intents[0]["action"], "BUY_YES")
            self.assertEqual(intents[0]["model_probability"], 0.8)
            self.assertEqual(intents[0]["entry_price"], 0.4)
            self.assertEqual(intents[0]["source_context"]["market_route"]["handler_id"], "weather")
            self.assertEqual(result.summary["composition"]["wallet_intents_written"], 1)
            self.assertEqual(result.summary["composition"]["buy_without_wallet_intent"], 0)
            self.assertFalse(result.summary["promotion_eligible"])

    def test_saved_forward_rule_replay_uses_same_strict_veto_on_identical_lane_rows(self):
        from bot.forward_composed_paper import compose_forward_rows
        from scripts.paper_shadow_lane_compose_replay import _load_config, validate_saved_combination
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshots = root / "collector.jsonl"
            output = root / "data" / "derived_reports" / "paired"
            stamp = "2026-07-01T12:00:00+00:00"
            _write_jsonl(snapshots, [{
                "run_id": "collector-1", "shared_candidate_id": "candidate-1", "market_id": "KXPAIRED",
                "observed_at": stamp, "main_decision": {"action": "BUY_YES", "size": 10.0},
                "decision_artifact": {"strategy_signal": {"direction": "BUY_YES", "market_id": "KXPAIRED",
                    "question": "Will it happen?", "exchange": "kalshi", "market_route": {"allowed": True, "handler_id": "weather"},
                    "model_probability": 0.8, "confidence": 0.9, "best_yes_ask": 0.4}},
                "shared_candidate": {"candidate_id": "candidate-1", "market_id": "KXPAIRED", "observed_at": stamp,
                    "market": {"question": "Will it happen?"},
                    "prices": {"order_book_snapshot": {"source": "book", "market_id": "KXPAIRED",
                        "observed_at": stamp, "data": {"best_yes_ask": 0.4}}}},
            }])
            config_path = Path(__file__).resolve().parent.parent / "lane_compositions" / "stable_router_guard_kelly.yaml"
            config = validate_saved_combination(_load_config(config_path))
            result = build_collector_lane_replay(snapshot_path=snapshots, output_dir=output,
                enabled_lanes=[], composition_config=config_path)
            lane_rows = [json.loads(line) for line in result.lane_decision_path.read_text().splitlines()]
            expected_pairs, expected_intents = compose_forward_rows(lane_rows, combination=config)
            actual = [json.loads(line) for line in (output / "composition_rows.jsonl").read_text().splitlines()]
            actual_intents = [json.loads(line) for line in (output / "wallet_intents.jsonl").read_text().splitlines()]
            self.assertEqual(len(actual), len(expected_pairs))
            self.assertEqual([(row["action"], row["reason_code"]) for row in actual],
                             [(row["combined_action"], row["reason_code"]) for row in expected_pairs])
            self.assertEqual(actual_intents, [row for row in expected_intents if row["lane_id"] == "forward_composed_paper"])
            self.assertEqual(actual[0]["action"], "SKIP")
            self.assertEqual(result.summary["composition"]["pnl"]["pnl_calculable_rows"], 0)

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
