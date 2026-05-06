import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from bot.live_adapters import RunnerLiveStateAdapter
from bot.runner import LivePosition, PredictionBot
from bot.shared_core import build_execution_snapshot, build_trade_decision
from bot.simulator import Simulator


class RecoveryParityTests(unittest.TestCase):
    def test_paper_reload_preserves_event_exposure_and_retrade_decision_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            session_file = data_dir / "sim_20260423_000000.json"
            session_file.write_text(
                json.dumps(
                    {
                        "session_id": "20260423_000000",
                        "starting_balance": 100.0,
                        "balance": 100.0,
                        "available_cash": 92.0,
                        "reserved_capital": 8.0,
                        "scan_count": 4,
                        "trades": [
                            {
                                "id": "paper-open-1",
                                "timestamp": "2026-04-23T00:00:00+00:00",
                                "exchange": "kalshi",
                                "market_id": "KXHIGHNY-26APR16-T70",
                                "question": "Will NYC high temperature be below 70 degrees?",
                                "direction": "BUY_YES",
                                "model_probability": 0.70,
                                "market_price": 0.40,
                                "edge": 0.20,
                                "confidence": 0.90,
                                "position_size": 8.0,
                                "signals": {},
                                "resolved": False,
                                "event_key": "KXHIGHNY-26APR16"
                            }
                        ],
                    }
                )
            )

            sim = Simulator(
                {
                    "data_dir": tmpdir,
                    "enable_social": False,
                    "strategy": {
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                    "max_position_size_usd": 10.0,
                    "max_tradable_balance_usd": 100.0,
                }
            )
            sim.risk.state.current_balance = 100.0
            sim.risk.state.available_cash = sim.available_cash
            sim.risk.state.reserved_capital = sim.reserved_capital
            sim.risk.state.total_exposure = sim.reserved_capital
            sim.risk.state.open_positions = 1
            sim.risk.state.daily_pnl = 0.0
            sim.risk.state.consecutive_losses = 0
            sim.risk.state.consecutive_wins = 0
            sim.risk.state.trading_enabled = True
            sim.risk.state.standby_active = False
            sim.risk.state.standby_reason_codes = []
            sim.risk.state.standby_blocked_scan_count = 0

            account = sim.state_adapter.get_account_state()
            self.assertEqual(account.reserved_capital, 8.0)
            self.assertEqual(account.total_exposure, 8.0)
            self.assertEqual(account.open_positions, 1)

            signal = {
                "exchange": "kalshi",
                "market_id": "KXHIGHNY-26APR16-T72",
                "question": "Will NYC high temperature be below 72 degrees?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.75,
                "edge": 0.15,
                "confidence": 0.90,
                "signals": {},
                "liquidity": 100.0,
                "best_yes_ask": 0.40,
                "best_no_ask": 0.60,
            }
            execution_snapshot = build_execution_snapshot(
                signal,
                direction="BUY_YES",
                bid_ask={"best_yes_ask": 0.40, "best_no_ask": 0.60},
            )
            context = sim.state_adapter.build_trade_context_from_snapshot(signal, execution_snapshot=execution_snapshot)

            self.assertEqual(context.account_state.reserved_capital, 8.0)
            self.assertEqual(context.metadata["event_snapshot"]["event_key"], "KXHIGHNY-26APR16")
            self.assertEqual(context.metadata["event_snapshot"]["event_position_count_before"], 1)
            self.assertEqual(context.metadata["event_snapshot"]["event_exposure_before"], 8.0)
            self.assertEqual(context.metadata["event_snapshot"]["held_market_ids"], ["KXHIGHNY-26APR16-T70"])

            kelly = Mock()
            kelly.calculate.return_value = 20.0
            risk = Mock()
            risk.max_tradable_balance = 100.0
            risk.max_event_exposure_pct = 0.20
            risk.max_event_positions = 3
            risk.retrade_edge_premium = 0.0
            risk.retrade_confidence_premium = 0.0
            risk.retrade_size_decay = 1.0
            risk.strict_event_overlap = False
            risk.min_retrade_net_edge = 0.01
            risk.min_retrade_expected_profit_usd = 0.0
            risk.require_price_improvement_for_same_market_family = False
            risk.price_improvement_ticks = 0.0
            risk.check_trade.return_value = SimpleNamespace(
                approved=True,
                reason="Approved",
                adjusted_size=10.0,
                risk_score=0.1,
                warnings=[],
            )

            decision = build_trade_decision(
                context,
                kelly_sizer=kelly,
                risk_policy=risk,
                min_edge=0.01,
                min_confidence=0.5,
                max_entry_price=0.70,
            )

            self.assertTrue(decision.approved)
            self.assertEqual(decision.reasoning["retrade"]["event_key"], "KXHIGHNY-26APR16")
            self.assertEqual(decision.reasoning["retrade"]["event_position_count_before"], 1)
            self.assertEqual(decision.reasoning["retrade"]["event_exposure_before"], 8.0)

    def test_live_reloaded_positions_and_orders_preserve_account_and_event_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = PredictionBot(
                {
                    "log_dir": tmpdir,
                    "data_dir": tmpdir,
                    "trading": {"mode": "live", "enabled": True},
                    "strategy": {
                        "min_edge": 0.01,
                        "min_confidence": 0.5,
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                    "max_position_size_usd": 10.0,
                    "max_tradable_balance_usd": 100.0,
                }
            )
            bot.open_positions = [
                LivePosition(
                    market_id="KXHIGHNY-26APR16-T70",
                    question="Will NYC high temperature be below 70 degrees?",
                    direction="BUY_YES",
                    price=0.40,
                    size=8.0,
                    order_id="ord-pos-1",
                    created_at="2026-04-23T00:00:00+00:00",
                    event_key="KXHIGHNY-26APR16",
                )
            ]
            bot.open_orders = [
                {
                    "order_id": "ord-open-1",
                    "market_id": "KXHIGHNY-26APR16-T74",
                    "question": "Will NYC high temperature be below 74 degrees?",
                    "direction": "BUY_YES",
                    "status": "open",
                    "requested_size": 5.0,
                    "filled_size": 0.0,
                    "remaining_size": 5.0,
                    "price": 0.38,
                    "created_at": "2026-04-23T00:05:00+00:00",
                    "event_key": "KXHIGHNY-26APR16",
                }
            ]
            bot.risk.state.current_balance = 100.0
            bot.risk.state.starting_balance = 100.0
            bot.risk.state.available_cash = 87.0
            bot.risk.state.reserved_capital = 13.0
            bot.risk.state.total_exposure = 13.0
            bot.risk.state.open_positions = 1
            bot.risk.state.daily_pnl = 0.0
            bot.risk.state.consecutive_losses = 0
            bot.risk.state.consecutive_wins = 0

            account = RunnerLiveStateAdapter(bot).get_account_state()
            self.assertEqual(account.reserved_capital, 13.0)
            self.assertEqual(account.available_cash, 87.0)
            self.assertEqual(account.metadata["filled_event_exposure"], 8.0)
            self.assertEqual(account.metadata["pending_event_exposure"], 5.0)

            signal = {
                "exchange": "kalshi",
                "market_id": "KXHIGHNY-26APR16-T72",
                "question": "Will NYC high temperature be below 72 degrees?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "best_yes_ask": 0.40,
                "best_no_ask": 0.60,
                "model_probability": 0.75,
                "edge": 0.15,
                "confidence": 0.90,
                "signals": {},
                "liquidity": 100.0,
            }

            class Exchange:
                def get_balance(self):
                    return 100.0

                def get_market_bid_ask(self, market_id):
                    return {"best_yes_ask": 0.40, "best_no_ask": 0.60}

            context = bot.live_execution.build_trade_context(signal, Exchange(), bot.config)

            self.assertEqual(context.account_state.reserved_capital, 13.0)
            self.assertEqual(context.metadata["event_snapshot"]["event_key"], "KXHIGHNY-26APR16")
            self.assertEqual(context.metadata["event_snapshot"]["event_position_count_before"], 2)
            self.assertEqual(context.metadata["event_snapshot"]["event_exposure_before"], 13.0)
            self.assertEqual(
                sorted(context.metadata["event_snapshot"]["held_market_ids"]),
                ["KXHIGHNY-26APR16-T70", "KXHIGHNY-26APR16-T74"],
            )
            self.assertEqual(context.metadata["event_snapshot"]["pending_event_exposure_before"], 5.0)
            self.assertEqual(context.metadata["event_snapshot"]["filled_event_exposure_before"], 8.0)

            kelly = Mock()
            kelly.calculate.return_value = 20.0
            risk = bot.risk
            risk.max_event_exposure_pct = 0.25
            risk.max_event_positions = 4
            risk.retrade_edge_premium = 0.0
            risk.retrade_confidence_premium = 0.0
            risk.retrade_size_decay = 1.0
            risk.strict_event_overlap = False
            risk.min_retrade_net_edge = 0.01
            risk.min_retrade_expected_profit_usd = 0.0
            risk.require_price_improvement_for_same_market_family = False
            risk.price_improvement_ticks = 0.0
            risk.check_trade = Mock(return_value=SimpleNamespace(
                approved=True,
                reason="Approved",
                adjusted_size=10.0,
                risk_score=0.1,
                warnings=[],
            ))

            decision = build_trade_decision(
                context,
                kelly_sizer=kelly,
                risk_policy=risk,
                min_edge=0.01,
                min_confidence=0.5,
                max_entry_price=0.70,
            )

            self.assertFalse(decision.approved)
            self.assertEqual(decision.reason_code, "event_exposure_limit_reached")
            self.assertEqual(decision.reasoning["retrade"]["event_key"], "KXHIGHNY-26APR16")
            self.assertEqual(decision.reasoning["retrade"]["event_position_count_before"], 2)
            self.assertEqual(decision.reasoning["retrade"]["event_exposure_before"], 13.0)


if __name__ == "__main__":
    unittest.main()
