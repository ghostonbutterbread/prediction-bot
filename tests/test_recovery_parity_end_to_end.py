import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from bot.exchanges.base import Market, Position, RestingOrder
from bot.runner import PredictionBot
from bot.shared_core import build_trade_decision
from bot.simulator import Simulator


class FakeExchangeWithRestartState:
    def __init__(self, positions=None, orders=None, balance=25.0, market_map=None):
        self._positions = positions or []
        self._orders = orders or []
        self._balance = balance
        self._market_map = market_map or {}

    def connect(self):
        return True

    def get_positions(self):
        return list(self._positions)

    def get_resting_orders(self):
        return list(self._orders)

    def get_balance(self):
        return self._balance

    def get_market_bid_ask(self, market_id):
        return {"best_yes_ask": 0.40, "best_no_ask": 0.60}

    def get_market(self, market_id):
        return self._market_map.get(market_id)

    def close(self):
        return None


class RecoveryParityEndToEndTests(unittest.TestCase):
    def test_paper_end_to_end_restart_reload_then_next_decision_preserves_retrade_context(self):
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
                                "question": "Will NYC high be below 70?",
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

            signal = {
                "exchange": "kalshi",
                "market_id": "KXHIGHNY-26APR16-T72",
                "question": "Will NYC high be below 72?",
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

            execution_snapshot = {
                "source": "book",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "best_yes_ask": 0.40,
                "best_no_ask": 0.60,
                "best_yes_bid": 0.39,
                "best_no_bid": 0.59,
                "estimated_fill_price": 0.40,
            }
            context = sim.state_adapter.build_trade_context_from_snapshot(signal, execution_snapshot=execution_snapshot)
            decision = build_trade_decision(
                context,
                kelly_sizer=kelly,
                risk_policy=risk,
                min_edge=0.01,
                min_confidence=0.5,
                max_entry_price=0.70,
            )

            self.assertTrue(decision.approved)
            self.assertEqual(context.account_state.reserved_capital, 8.0)
            self.assertEqual(context.metadata["event_snapshot"]["event_position_count_before"], 1)
            self.assertEqual(context.metadata["event_snapshot"]["event_exposure_before"], 8.0)
            self.assertEqual(decision.reasoning["retrade"]["event_exposure_before"], 8.0)
            self.assertEqual(decision.reasoning["retrade"]["event_position_count_before"], 1)

    def test_live_resolution_sync_updates_trade_history_with_canonical_resolved_fields(self):
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
            bot.exchanges["kalshi"] = FakeExchangeWithRestartState(
                positions=[
                    Position(
                        market_id="KXHIGHNY-26APR16-T70",
                        exchange="kalshi",
                        question="Will NYC high be below 70?",
                        side="YES",
                        entry_price=0.40,
                        size=3.0,
                        current_price=0.40,
                        pnl=0.0,
                        opened_at=datetime(2026, 4, 20, 18, 0, tzinfo=timezone.utc),
                    )
                ],
                market_map={
                    "KXHIGHNY-26APR16-T70": Market(
                        id="KXHIGHNY-26APR16-T70",
                        exchange="kalshi",
                        question="Will NYC high be below 70?",
                        yes_price=1.0,
                        no_price=0.0,
                        volume=0,
                        liquidity=0,
                        closes_at=datetime.now(timezone.utc),
                        category="weather",
                        metadata={"result": "YES"},
                        close_price=1.0,
                    )
                },
                balance=100.0,
            )

            bot.connect_all()
            bot._sync_resolved_positions()

            self.assertEqual(len(bot.open_positions), 0)
            resolved_row = next(row for row in bot.trade_history if row["trade_id"].startswith("reconciled:kalshi:KXHIGHNY-26APR16-T70"))
            self.assertEqual(resolved_row["status"], "resolved")
            self.assertEqual(resolved_row["lifecycle_state"], "resolved_position")
            self.assertTrue(resolved_row["resolved"])
            self.assertEqual(resolved_row["outcome"], "YES")
            self.assertIsNotNone(resolved_row["resolved_at"])
            self.assertEqual(resolved_row["settlement_value"], 4.8)
            self.assertEqual(resolved_row["exit_price"], 1.0)
            self.assertEqual(resolved_row["resolution_type"], "settled")
            self.assertEqual(resolved_row["resolution_result"], "won")
            self.assertEqual(resolved_row["pnl"], 1.8)

    def test_live_end_to_end_restart_reconcile_then_status_and_next_decision_preserve_constraints(self):
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
                    "parity_mode": {"enabled": True},
                }
            )
            bot.exchanges["kalshi"] = FakeExchangeWithRestartState(
                positions=[
                    Position(
                        market_id="KXHIGHNY-26APR16-T70",
                        exchange="kalshi",
                        question="Will NYC high be below 70?",
                        side="YES",
                        entry_price=0.40,
                        size=8.0,
                        current_price=0.40,
                        pnl=0.0,
                        opened_at=datetime(2026, 4, 20, 18, 0, tzinfo=timezone.utc),
                    )
                ],
                orders=[
                    RestingOrder(
                        order_id="ord-open-1",
                        market_id="KXHIGHNY-26APR16-T74",
                        exchange="kalshi",
                        side="YES",
                        requested_size=5.0,
                        filled_size=0.0,
                        remaining_size=5.0,
                        price=0.38,
                        status="open",
                        created_at=datetime(2026, 4, 20, 18, 1, tzinfo=timezone.utc),
                    )
                ],
                balance=100.0,
            )

            bot.connect_all()

            snapshot = bot.build_status_snapshot(reason="post-restart", scan_num=1)
            self.assertEqual(snapshot.extra["filled_event_exposure"], 8.0)
            self.assertEqual(snapshot.extra["pending_event_exposure"], 5.0)
            self.assertTrue(snapshot.extra["parity_summary"]["parity_mode_enabled"])

            signal = {
                "exchange": "kalshi",
                "market_id": "KXHIGHNY-26APR16-T72",
                "question": "Will NYC high be below 72?",
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

            context = bot.live_execution.build_trade_context(signal, bot.exchanges["kalshi"], bot.config)
            self.assertEqual(context.account_state.reserved_capital, 13.0)
            self.assertEqual(context.metadata["event_snapshot"]["event_position_count_before"], 2)
            self.assertEqual(context.metadata["event_snapshot"]["event_exposure_before"], 13.0)
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
            self.assertEqual(decision.reasoning["retrade"]["event_exposure_before"], 13.0)
            self.assertEqual(decision.reasoning["retrade"]["event_position_count_before"], 2)


if __name__ == "__main__":
    unittest.main()
