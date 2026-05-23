import unittest

from scripts.paper_shadow_lane_compose_replay import compose_lane_replay


class PaperShadowLaneComposeReplayTests(unittest.TestCase):
    def test_composes_router_side_with_stable_sizing_and_prices(self):
        lane_rows = [
            {
                "policy": "control_stable",
                "shared_candidate_id": "candidate-1",
                "market_id": "KXCOMPOSE-1",
                "action": "BUY_YES",
                "approved_position_size_usd": 5.0,
                "provenance": {
                    "future_pnl_inputs": {
                        "shared_candidate_id": "candidate-1",
                        "market_id": "KXCOMPOSE-1",
                        "recommended_action": "BUY_YES",
                        "side": "YES",
                        "best_yes_ask": 0.40,
                        "best_no_ask": 0.65,
                        "approved_position_size_usd": 5.0,
                    }
                },
            },
            {
                "policy": "shadow_source_router",
                "shared_candidate_id": "candidate-1",
                "market_id": "KXCOMPOSE-1",
                "action": "BUY_NO",
                "approved_position_size_usd": 10.0,
                "provenance": {
                    "future_pnl_inputs": {
                        "shared_candidate_id": "candidate-1",
                        "market_id": "KXCOMPOSE-1",
                        "recommended_action": "BUY_NO",
                        "side": "NO",
                        "best_yes_ask": 0.40,
                        "best_no_ask": 0.65,
                        "approved_position_size_usd": 10.0,
                    }
                },
            },
        ]
        config = {
            "composition": {
                "name": "router_side_stable_size",
                "base_lane": "control_stable",
                "action_lane": "shadow_source_router",
                "sizing_lane": "control_stable",
                "price_lane": "shadow_source_router",
            }
        }

        result = compose_lane_replay(
            lane_rows=lane_rows,
            resolution_rows=[{"shared_candidate_id": "candidate-1", "market_id": "KXCOMPOSE-1", "outcome": "NO"}],
            config=config,
        )

        row = result["composition_rows"][0]
        self.assertEqual(row["action"], "BUY_NO")
        self.assertEqual(row["side"], "NO")
        self.assertEqual(row["approved_position_size_usd"], 5.0)
        self.assertEqual(row["entry_price"], 0.65)
        self.assertEqual(result["summary"]["pnl"]["winning_buy_rows"], 1)
        self.assertAlmostEqual(result["summary"]["pnl"]["total_pnl_usd"], 2.6923)

    def test_vetoes_side_conflict_without_mutating_source_rows(self):
        lane_rows = [
            {
                "policy": "control_stable",
                "shared_candidate_id": "candidate-2",
                "market_id": "KXCOMPOSE-2",
                "action": "BUY_YES",
                "approved_position_size_usd": 5.0,
                "provenance": {
                    "future_pnl_inputs": {
                        "shared_candidate_id": "candidate-2",
                        "market_id": "KXCOMPOSE-2",
                        "recommended_action": "BUY_YES",
                        "side": "YES",
                        "best_yes_ask": 0.40,
                        "best_no_ask": 0.65,
                        "approved_position_size_usd": 5.0,
                    }
                },
            },
            {
                "policy": "shadow_source_router",
                "shared_candidate_id": "candidate-2",
                "market_id": "KXCOMPOSE-2",
                "action": "BUY_NO",
                "provenance": {
                    "future_pnl_inputs": {
                        "shared_candidate_id": "candidate-2",
                        "market_id": "KXCOMPOSE-2",
                        "recommended_action": "BUY_NO",
                        "side": "NO",
                        "best_no_ask": 0.65,
                    }
                },
            },
        ]
        config = {
            "composition": {
                "name": "stable_requires_source_agreement",
                "base_lane": "control_stable",
                "action_lane": "control_stable",
                "sizing_lane": "control_stable",
                "vetoes": [{"lane": "shadow_source_router", "mode": "require_agreement"}],
            }
        }

        result = compose_lane_replay(
            lane_rows=lane_rows,
            resolution_rows=[{"shared_candidate_id": "candidate-2", "market_id": "KXCOMPOSE-2", "outcome": "YES"}],
            config=config,
        )

        row = result["composition_rows"][0]
        self.assertEqual(row["action"], "SKIP")
        self.assertEqual(row["reason_code"], "side_conflict:shadow_source_router")
        self.assertFalse(row["mutation_contract"]["mutates_accounting"])
        self.assertEqual(result["summary"]["diagnostics"], {"side_conflict:shadow_source_router": 1})
        self.assertEqual(result["resolved_rows"][0]["blocker"], None)
        self.assertEqual(result["summary"]["pnl"]["skip_rows"], 1)

    def test_summary_uses_emitted_resolved_rows_for_blockers(self):
        lane_rows = [
            {
                "policy": "control_stable",
                "shared_candidate_id": "candidate-3",
                "market_id": "KXCOMPOSE-3",
                "action": "BUY_YES",
                "approved_position_size_usd": 5.0,
                "provenance": {
                    "future_pnl_inputs": {
                        "shared_candidate_id": "candidate-3",
                        "market_id": "KXCOMPOSE-3",
                        "recommended_action": "BUY_YES",
                        "side": "YES",
                        "approved_position_size_usd": 5.0,
                    }
                },
            }
        ]
        config = {
            "composition": {
                "name": "stable_missing_price",
                "base_lane": "control_stable",
                "action_lane": "control_stable",
                "sizing_lane": "control_stable",
            }
        }

        result = compose_lane_replay(
            lane_rows=lane_rows,
            resolution_rows=[{"shared_candidate_id": "candidate-3", "market_id": "KXCOMPOSE-3", "outcome": "YES"}],
            config=config,
        )

        self.assertEqual(result["resolved_rows"][0]["blocker"], "missing_fill_price")
        self.assertEqual(result["summary"]["pnl"]["blocker_counts"], {"missing_fill_price": 1})
        self.assertEqual(result["summary"]["pnl"]["pnl_calculable_rows"], 0)


if __name__ == "__main__":
    unittest.main()
