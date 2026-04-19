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
