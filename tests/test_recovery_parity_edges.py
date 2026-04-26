import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from bot.exchanges.base import Position, RestingOrder
from bot.runner import PredictionBot
from bot.shared_core import build_trade_decision


class FakeEdgeExchange:
    def __init__(self, positions=None, orders=None, balance=100.0, bid_ask=None):
        self._positions = positions or []
        self._orders = orders or []
        self._balance = balance
        self._bid_ask = bid_ask or {"best_yes_ask": 0.40, "best_no_ask": 0.60}

    def connect(self):
        return True

    def get_positions(self):
        return list(self._positions)

    def get_resting_orders(self):
        return list(self._orders)

    def get_balance(self):
        return self._balance

    def get_market_bid_ask(self, market_id):
        return dict(self._bid_ask)

    def close(self):
        return None


class RecoveryParityEdgeTests(unittest.TestCase):
    def _make_bot(self, tmpdir):
        return PredictionBot(
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

    def test_restart_reconciliation_keeps_partial_fill_and_position_without_double_counting_remaining_order_exposure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.exchanges["kalshi"] = FakeEdgeExchange(
                positions=[
                    Position(
                        market_id="KXHIGHNY-26APR16-T70",
                        exchange="kalshi",
                        question="Will NYC high be below 70?",
                        side="YES",
                        entry_price=0.40,
                        size=2.0,
                        current_price=0.40,
                        pnl=0.0,
                        opened_at=datetime(2026, 4, 20, 18, 0, tzinfo=timezone.utc),
                    )
                ],
                orders=[
                    RestingOrder(
                        order_id="ord-partial-1",
                        market_id="KXHIGHNY-26APR16-T70",
                        exchange="kalshi",
                        question="Will NYC high be below 70?",
                        side="YES",
                        requested_size=5.0,
                        filled_size=2.0,
                        remaining_size=3.0,
                        price=0.40,
                        status="open",
                        created_at=datetime(2026, 4, 20, 18, 1, tzinfo=timezone.utc),
                    )
                ],
                balance=100.0,
            )

            bot.connect_all()

            self.assertEqual(len(bot.open_positions), 1)
            self.assertEqual(len(bot.open_orders), 1)
            self.assertEqual(bot.risk.state.reserved_capital, 5.0)
            self.assertEqual(bot.risk.state.available_cash, 95.0)

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

            self.assertEqual(context.account_state.reserved_capital, 5.0)
            self.assertEqual(context.metadata["event_snapshot"]["event_exposure_before"], 5.0)
            self.assertEqual(context.metadata["event_snapshot"]["event_position_count_before"], 2)
            self.assertEqual(context.metadata["event_snapshot"]["filled_event_exposure_before"], 2.0)
            self.assertEqual(context.metadata["event_snapshot"]["pending_event_exposure_before"], 3.0)

    def test_restart_reconciliation_keeps_canceled_order_in_history_but_not_open_orders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.exchanges["kalshi"] = FakeEdgeExchange(
                positions=[],
                orders=[
                    RestingOrder(
                        order_id="ord-canceled-1",
                        market_id="KXHIGHNY-26APR16-T70",
                        exchange="kalshi",
                        question="Will NYC high be below 70?",
                        side="YES",
                        requested_size=5.0,
                        filled_size=0.0,
                        remaining_size=0.0,
                        price=0.40,
                        status="canceled",
                        created_at=datetime(2026, 4, 20, 18, 1, tzinfo=timezone.utc),
                    )
                ],
                balance=100.0,
            )

            bot.connect_all()

            self.assertEqual(len(bot.open_orders), 0)
            self.assertEqual(bot.risk.state.reserved_capital, 0.0)
            self.assertEqual(bot.risk.state.available_cash, 100.0)

            canceled_row = next(row for row in bot.trade_history if row["trade_id"] == "ord-canceled-1")
            self.assertEqual(canceled_row["status"], "canceled")
            self.assertEqual(canceled_row["lifecycle_state"], "canceled_unfilled")
            self.assertEqual(canceled_row["remaining_size"], 0.0)

            snapshot = bot.build_status_snapshot(reason="post-restart", scan_num=1)
            self.assertEqual(snapshot.extra["pending_event_exposure"], 0.0)

    def test_restart_context_separates_multiple_event_families(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.exchanges["kalshi"] = FakeEdgeExchange(
                positions=[
                    Position(
                        market_id="KXHIGHNY-26APR16-T70",
                        exchange="kalshi",
                        question="Will NYC high be below 70?",
                        side="YES",
                        entry_price=0.40,
                        size=4.0,
                        current_price=0.40,
                        pnl=0.0,
                        opened_at=datetime(2026, 4, 20, 18, 0, tzinfo=timezone.utc),
                    ),
                    Position(
                        market_id="KXLOWNY-26APR16-T50",
                        exchange="kalshi",
                        question="Will NYC low be above 50?",
                        side="YES",
                        entry_price=0.35,
                        size=6.0,
                        current_price=0.35,
                        pnl=0.0,
                        opened_at=datetime(2026, 4, 20, 18, 2, tzinfo=timezone.utc),
                    ),
                ],
                orders=[
                    RestingOrder(
                        order_id="ord-open-1",
                        market_id="KXLOWNY-26APR16-T52",
                        exchange="kalshi",
                        question="Will NYC low be above 52?",
                        side="YES",
                        requested_size=2.0,
                        filled_size=0.0,
                        remaining_size=2.0,
                        price=0.37,
                        status="open",
                        created_at=datetime(2026, 4, 20, 18, 3, tzinfo=timezone.utc),
                    )
                ],
                balance=100.0,
            )

            bot.connect_all()

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

            self.assertEqual(context.metadata["event_snapshot"]["event_key"], "KXHIGHNY-26APR16")
            self.assertEqual(context.metadata["event_snapshot"]["event_position_count_before"], 1)
            self.assertEqual(context.metadata["event_snapshot"]["event_exposure_before"], 4.0)
            self.assertEqual(context.metadata["event_snapshot"]["held_market_ids"], ["KXHIGHNY-26APR16-T70"])

            kelly = Mock()
            kelly.calculate.return_value = 20.0
            risk = bot.risk
            risk.max_event_exposure_pct = 0.10
            risk.max_event_positions = 3
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

            self.assertTrue(decision.approved)
            self.assertEqual(decision.reason_code, "approved")
            self.assertEqual(decision.reasoning["retrade"]["event_key"], "KXHIGHNY-26APR16")
            self.assertEqual(decision.reasoning["retrade"]["event_exposure_before"], 4.0)
            self.assertEqual(decision.reasoning["retrade"]["event_position_count_before"], 1)


if __name__ == "__main__":
    unittest.main()
