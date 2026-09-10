import unittest

from bot.shared_core import AccountState


class ComposedLaneWalletIntentTests(unittest.TestCase):
    def test_source_router_no_action_uses_stable_yes_probability_as_selected_side_probability(self):
        from bot.composed_lane_wallet import build_composed_trade_context

        context, receipt = build_composed_trade_context(
            {
                "decision_id": "composition:source-router:1",
                "lane_id": "composition:source_router_side_stable_size",
                "market_id": "KXTEST-1",
                "question": "Will test event occur?",
                "observed_at": "2026-09-01T12:00:00+00:00",
                "action": "BUY_NO",
                "entry_price": 0.40,
                "model_probability": 0.30,
                "confidence": 0.90,
                "source_context": {
                    "market_route": {"allowed": True, "handler_id": "weather"},
                },
            },
            AccountState(
                starting_balance=100.0,
                current_balance=100.0,
                available_cash=100.0,
                reserved_capital=0.0,
                total_exposure=0.0,
                open_positions=0,
            ),
        )

        self.assertEqual(context.direction, "BUY_NO")
        self.assertAlmostEqual(context.model_probability, 0.30)
        self.assertAlmostEqual(context.edge, 0.30)
        self.assertAlmostEqual(receipt["selected_side_probability"], 0.70)
        self.assertEqual(receipt["probability_provider"], "composition.model_probability")
        self.assertNotIn("outcome", receipt)

    def test_intent_rejects_nested_outcome_data_before_shared_core_projection(self):
        from bot.composed_lane_wallet import build_composed_trade_context

        with self.assertRaisesRegex(ValueError, "outcome-like"):
            build_composed_trade_context(
                {
                    "decision_id": "nested-outcome", "market_id": "KX-NESTED", "question": "Nested?",
                    "observed_at": "2026-09-01T12:00:00+00:00", "action": "BUY_YES", "entry_price": 0.5,
                    "model_probability": 0.7, "confidence": 0.9,
                    "source_context": {"market_route": {"allowed": True, "handler_id": "weather"},
                                       "resolution": {"outcome": "YES"}},
                },
                AccountState(100.0, 100.0, 100.0, 0.0, 0.0, 0),
            )

    def test_wallet_recomputes_size_and_holds_capital_until_exact_later_settlement(self):
        from bot.composed_lane_wallet import evaluate_composed_intents
        from bot.risk import RiskDecision

        class FixedKelly:
            def calculate(self, win_probability, entry_price, bankroll):
                return 25.0

        class ApprovingRisk:
            def check_trade(self, signal, position_size, *, available_cash=None):
                return RiskDecision(approved=True, adjusted_size=position_size, original_size=position_size)

        intents = [
            {"decision_id": "one", "shared_candidate_id": "candidate-one", "run_id": "run-1", "lane_id": "composition:source_router", "market_id": "KX-ONE", "question": "One?", "observed_at": "2026-09-01T12:00:00+00:00", "action": "BUY_YES", "entry_price": 0.50, "model_probability": 0.70, "confidence": 0.90, "source_context": {"market_route": {"allowed": True, "handler_id": "weather"}}},
            {"decision_id": "two", "shared_candidate_id": "candidate-two", "run_id": "run-1", "lane_id": "composition:shadow_gate", "market_id": "KX-TWO", "question": "Two?", "observed_at": "2026-09-01T12:30:00+00:00", "action": "BUY_YES", "entry_price": 0.50, "model_probability": 0.70, "confidence": 0.90, "source_context": {"market_route": {"allowed": True, "handler_id": "weather"}}},
        ]
        result = evaluate_composed_intents(
            intents=intents,
            resolutions=[
                {"decision_id": "one", "shared_candidate_id": "candidate-one", "run_id": "run-1", "market_id": "KX-ONE", "outcome": "YES", "settlement_ts": "2026-09-01T13:00:00+00:00", "authoritative": True, "resolution_row_sha256": "a" * 64},
                {"decision_id": "two", "shared_candidate_id": "candidate-two", "run_id": "run-1", "market_id": "KX-TWO", "outcome": "VOID", "settlement_ts": "2026-09-01T14:00:00+00:00", "authoritative": True, "resolution_row_sha256": "b" * 64},
            ],
            starting_balance_usd=100.0, kelly_sizer=FixedKelly(), risk_policy=ApprovingRisk(),
            min_edge=0.01, min_confidence=0.50, max_entry_price=0.99,
        )

        self.assertEqual([row["status"] for row in result["decision_rows"]], ["opened", "opened"])
        self.assertEqual(result["decision_rows"][1]["available_cash_before_usd"], 75.0)
        self.assertEqual(result["summary"]["reserved_capital_usd"], 0.0)
        self.assertEqual(result["summary"]["final_balance_usd"], 125.0)
        self.assertEqual([row["outcome"] for row in result["settlement_rows"]], ["YES", "VOID"])
        self.assertTrue(result["settlement_rows"][1]["void_resolution"])
        self.assertNotIn("stake_usd", result["settlement_rows"][1])

    def test_conflicting_exact_resolution_receipts_remain_unsettled(self):
        from bot.composed_lane_wallet import evaluate_composed_intents
        from bot.risk import RiskDecision

        class FixedKelly:
            def calculate(self, win_probability, entry_price, bankroll): return 25.0
        class ApprovingRisk:
            def check_trade(self, signal, position_size, *, available_cash=None):
                return RiskDecision(approved=True, adjusted_size=position_size, original_size=position_size)

        intent = {"decision_id": "one", "shared_candidate_id": "candidate-one", "run_id": "run-1", "lane_id": "shadow_gate", "market_id": "KX-ONE", "question": "One?", "observed_at": "2026-09-01T12:00:00+00:00", "action": "BUY_YES", "entry_price": 0.5, "model_probability": 0.7, "confidence": 0.9, "source_context": {"market_route": {"allowed": True, "handler_id": "weather"}}}
        resolution = {"decision_id": "one", "shared_candidate_id": "candidate-one", "run_id": "run-1", "market_id": "KX-ONE", "settlement_ts": "2026-09-01T13:00:00+00:00", "authoritative": True, "resolution_row_sha256": "a" * 64}
        result = evaluate_composed_intents(intents=[intent], resolutions=[{**resolution, "outcome": "YES"}, {**resolution, "outcome": "NO"}], starting_balance_usd=100.0, kelly_sizer=FixedKelly(), risk_policy=ApprovingRisk(), min_edge=0.01, min_confidence=0.5, max_entry_price=0.99)
        self.assertEqual(result["settlement_rows"], [])
        self.assertEqual(len(result["open_positions"]), 1)


if __name__ == "__main__":
    unittest.main()
