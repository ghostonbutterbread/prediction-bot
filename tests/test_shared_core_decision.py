import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from bot.shared_core import AccountState, TradeContext, build_trade_decision


class SharedCoreDecisionTests(unittest.TestCase):
    def test_build_trade_decision_uses_buy_no_price_and_available_cash_snapshot(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=25.0,
            reserved_capital=75.0,
            total_exposure=75.0,
            open_positions=3,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="market-1",
            question="Will test settle NO?",
            direction="BUY_NO",
            market_price=0.41,
            yes_price=0.41,
            no_price=0.63,
            model_probability=0.28,
            edge=0.12,
            confidence=0.87,
            account_state=account_state,
            source_context={"question": "Will test settle NO?"},
        )
        kelly_sizer = Mock()
        kelly_sizer.calculate.return_value = 12.0
        risk_policy = Mock()
        risk_policy.check_trade.return_value = SimpleNamespace(
            approved=True,
            reason="Approved (with 1 warnings)",
            adjusted_size=9.5,
            risk_score=0.2,
            warnings=["Available cash capped size to $9.50 ($25.00 spendable)"],
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.action, "BUY_NO")
        self.assertAlmostEqual(decision.entry_price, 0.63)
        self.assertAlmostEqual(decision.win_probability, 0.72)
        self.assertAlmostEqual(decision.position_size, 9.5)
        self.assertEqual(decision.reason_code, "approved")
        self.assertEqual(decision.reasoning["account_state"]["available_cash"], 25.0)
        kelly_sizer.calculate.assert_called_once_with(0.72, 0.63, 25.0)
        risk_policy.check_trade.assert_called_once_with(
            {"question": "Will test settle NO?"},
            12.0,
            available_cash=25.0,
        )

    def test_build_trade_decision_rejects_duplicate_same_event_market(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=40.0,
            reserved_capital=10.0,
            total_exposure=10.0,
            open_positions=1,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXHIGHNY-26APR16-T70",
            question="Will NYC high be below 70?",
            direction="BUY_YES",
            market_price=0.4,
            yes_price=0.4,
            no_price=0.6,
            model_probability=0.7,
            edge=0.2,
            confidence=0.9,
            account_state=account_state,
            source_context={"market_id": "KXHIGHNY-26APR16-T70"},
            metadata={
                "event_snapshot": {
                    "event_key": "KXHIGHNY-26APR16",
                    "event_position_count_before": 1,
                    "event_exposure_before": 10.0,
                    "held_market_ids": ["KXHIGHNY-26APR16-T70"],
                }
            },
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=Mock(),
            risk_policy=Mock(),
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "duplicate_market_id_open")

    def test_build_trade_decision_applies_retrade_decay_and_headroom(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=40.0,
            reserved_capital=10.0,
            total_exposure=10.0,
            open_positions=1,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXHIGHNY-26APR16-T72",
            question="Will NYC high be below 72?",
            direction="BUY_YES",
            market_price=0.4,
            yes_price=0.4,
            no_price=0.6,
            model_probability=0.7,
            edge=0.2,
            confidence=0.9,
            account_state=account_state,
            source_context={"market_id": "KXHIGHNY-26APR16-T72"},
            metadata={
                "event_snapshot": {
                    "event_key": "KXHIGHNY-26APR16",
                    "event_position_count_before": 2,
                    "event_exposure_before": 8.0,
                    "held_market_ids": ["KXHIGHNY-26APR16-T70", "KXHIGHNY-26APR16-T71"],
                },
                "retrade_policy": {
                    "max_event_exposure_pct": 0.10,
                    "max_event_positions": 3,
                    "retrade_edge_premium": 0.01,
                    "retrade_confidence_premium": 0.0,
                    "retrade_size_decay": 0.5,
                    "min_retrade_net_edge": 0.005,
                    "fee_rate": 0.07,
                },
            },
        )
        kelly_sizer = Mock()
        kelly_sizer.calculate.return_value = 20.0
        risk_policy = Mock()
        risk_policy.check_trade.return_value = SimpleNamespace(
            approved=True,
            reason="Approved",
            adjusted_size=2.0,
            risk_score=0.1,
            warnings=[],
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.requested_position_size, 2.0)
        self.assertEqual(decision.position_size, 2.0)
        self.assertAlmostEqual(decision.reasoning["retrade"]["size_decay_applied"], 0.25)

    def test_build_trade_decision_rejects_unviable_retrade_after_costs(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=40.0,
            reserved_capital=10.0,
            total_exposure=10.0,
            open_positions=1,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXHIGHNY-26APR16-T72",
            question="Will NYC high be below 72?",
            direction="BUY_YES",
            market_price=0.55,
            yes_price=0.55,
            no_price=0.45,
            model_probability=0.58,
            edge=0.02,
            confidence=0.9,
            account_state=account_state,
            source_context={"market_id": "KXHIGHNY-26APR16-T72", "liquidity": 20.0},
            metadata={
                "event_snapshot": {
                    "event_key": "KXHIGHNY-26APR16",
                    "event_position_count_before": 1,
                    "event_exposure_before": 2.0,
                    "held_market_ids": ["KXHIGHNY-26APR16-T70"],
                },
                "retrade_policy": {
                    "retrade_edge_premium": 0.0,
                    "retrade_size_decay": 1.0,
                    "min_retrade_net_edge": 0.01,
                    "fee_rate": 0.07,
                },
            },
        )
        kelly_sizer = Mock()
        kelly_sizer.calculate.return_value = 10.0
        risk_policy = Mock()
        risk_policy.check_trade.return_value = SimpleNamespace(
            approved=True,
            reason="Approved",
            adjusted_size=10.0,
            risk_score=0.1,
            warnings=[],
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.01,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "retrade_net_edge_below_threshold")

    def test_build_trade_decision_rejects_same_family_retrade_without_price_improvement(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=40.0,
            reserved_capital=10.0,
            total_exposure=10.0,
            open_positions=1,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXHIGHNY-26APR16-T72",
            question="Will NYC high be below 72?",
            direction="BUY_YES",
            market_price=0.41,
            yes_price=0.41,
            no_price=0.59,
            model_probability=0.7,
            edge=0.2,
            confidence=0.9,
            account_state=account_state,
            source_context={"market_id": "KXHIGHNY-26APR16-T72"},
            metadata={
                "event_snapshot": {
                    "event_key": "KXHIGHNY-26APR16",
                    "candidate_family_key": "KXHIGHNY-26APR16",
                    "event_position_count_before": 1,
                    "event_exposure_before": 5.0,
                    "same_family_markets": ["KXHIGHNY-26APR16-T70"],
                    "best_same_family_entry_price": 0.42,
                    "best_yes_ask": 0.41,
                },
                "retrade_policy": {
                    "strict_event_overlap": False,
                    "require_price_improvement_for_same_market_family": True,
                    "price_improvement_ticks": 0.03,
                },
            },
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=Mock(),
            risk_policy=Mock(),
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason_code, "same_family_price_not_improved")

    def test_build_trade_decision_tracks_slippage_aware_expected_profit(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=40.0,
            reserved_capital=10.0,
            total_exposure=10.0,
            open_positions=1,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="KXHIGHNY-26APR16-T72",
            question="Will NYC high be below 72?",
            direction="BUY_YES",
            market_price=0.40,
            yes_price=0.40,
            no_price=0.60,
            model_probability=0.75,
            edge=0.15,
            confidence=0.9,
            account_state=account_state,
            source_context={"market_id": "KXHIGHNY-26APR16-T72", "liquidity": 100.0},
            metadata={
                "event_snapshot": {
                    "event_key": "KXHIGHNY-26APR16",
                    "event_position_count_before": 1,
                    "event_exposure_before": 2.0,
                    "held_market_ids": ["KXHIGHNY-26APR16-T70"],
                    "best_yes_ask": 0.40,
                    "liquidity": 100.0,
                },
                "retrade_policy": {
                    "retrade_edge_premium": 0.0,
                    "retrade_size_decay": 1.0,
                    "min_retrade_net_edge": 0.01,
                    "min_retrade_expected_profit_usd": 0.25,
                    "fee_rate": 0.07,
                },
            },
        )
        kelly_sizer = Mock()
        kelly_sizer.calculate.return_value = 10.0
        risk_policy = Mock()
        risk_policy.check_trade.return_value = SimpleNamespace(
            approved=True,
            reason="Approved",
            adjusted_size=10.0,
            risk_score=0.1,
            warnings=[],
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.01,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertTrue(decision.approved)
        self.assertIn("expected_profit_usd", decision.reasoning["retrade"])
        self.assertGreater(decision.reasoning["retrade"]["estimated_slippage"], 0.0)
        self.assertGreater(decision.reasoning["retrade"]["estimated_fill_price"], 0.40)

    def test_build_trade_decision_normalizes_risk_rejection_reason_code(self):
        account_state = AccountState(
            starting_balance=100.0,
            current_balance=100.0,
            available_cash=25.0,
            reserved_capital=0.0,
            total_exposure=0.0,
            open_positions=0,
        )
        context = TradeContext(
            exchange="kalshi",
            market_id="market-2",
            question="Will test settle YES?",
            direction="BUY_YES",
            market_price=0.4,
            yes_price=0.4,
            no_price=0.6,
            model_probability=0.7,
            edge=0.2,
            confidence=0.9,
            account_state=account_state,
            source_context={"question": "Will test settle YES?"},
        )
        kelly_sizer = Mock()
        kelly_sizer.calculate.return_value = 10.0
        risk_policy = Mock()
        risk_policy.check_trade.return_value = SimpleNamespace(
            approved=False,
            reason="Max positions (15/15)",
            adjusted_size=0.0,
            risk_score=0.8,
            warnings=[],
        )

        decision = build_trade_decision(
            context,
            kelly_sizer=kelly_sizer,
            risk_policy=risk_policy,
            min_edge=0.05,
            min_confidence=0.5,
            max_entry_price=0.7,
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.action, "SKIP")
        self.assertEqual(decision.reason, "Max positions (15/15)")
        self.assertEqual(decision.reason_code, "risk_max_positions_15_15")


if __name__ == "__main__":
    unittest.main()
