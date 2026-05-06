import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from bot.exchanges.base import Position, RestingOrder
from bot.runner import PredictionBot


class ChaosRestartExchange:
    def __init__(self, positions=None, orders=None, balance=25.0):
        self.positions = positions or []
        self.orders = orders or []
        self.balance = balance
        self.placed_orders = []
        self.calls = []

    def connect(self):
        self.calls.append("connect")
        return True

    def get_positions(self):
        self.calls.append("get_positions")
        return list(self.positions)

    def get_resting_orders(self):
        self.calls.append("get_resting_orders")
        return list(self.orders)

    def get_balance(self):
        self.calls.append("get_balance")
        return self.balance

    def get_market_bid_ask(self, market_id):
        self.calls.append("get_market_bid_ask")
        return {"best_yes_ask": 0.40, "best_no_ask": 0.60}

    def place_order(self, market_id, side, price, size):
        self.calls.append("place_order")
        order = SimpleNamespace(id=f"placed-{len(self.placed_orders) + 1}", status="open", filled_size=0.0, remaining_size=size)
        self.placed_orders.append({"market_id": market_id, "side": side, "price": price, "size": size})
        return order


class RestartReconnectChaosTests(unittest.TestCase):
    def _make_bot(self, tmpdir):
        bot = PredictionBot(
            {
                "log_dir": tmpdir,
                "data_dir": tmpdir,
                "trading_enabled": True,
                "max_tradable_balance_usd": 10.0,
                "max_position_size_usd": 4.0,
                "trading": {"mode": "live", "trading_enabled": True},
                "strategy": {
                    "min_edge": 0.05,
                    "min_confidence": 0.5,
                    "enable_news": False,
                    "enable_social": False,
                    "enable_ai": False,
                },
            }
        )
        bot.risk.state.current_balance = 25.0
        bot.risk.state.available_cash = 25.0
        bot.risk.state.peak_balance = 25.0
        bot.risk.state.session_starting_balance = 25.0
        bot.risk.state.session_peak_balance = 25.0
        return bot

    def _signal(self, market_id="KXHIGHNY-26APR29-T80"):
        return {
            "exchange": "kalshi",
            "market_id": market_id,
            "question": "Will NYC high temperature be above 80 degrees?",
            "series_ticker": "KXHIGHNY",
            "event_ticker": "KXHIGHNY-26APR29",
            "direction": "BUY_YES",
            "market_price": 0.40,
            "yes_price": 0.40,
            "no_price": 0.60,
            "model_probability": 0.70,
            "edge": 0.30,
            "confidence": 0.90,
        }

    def test_live_signal_reconciles_startup_state_before_first_order_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            exchange = ChaosRestartExchange()
            bot.exchanges["kalshi"] = exchange

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                result = bot._process_signal(self._signal())

            self.assertIn("order", result)
            self.assertEqual(len(exchange.placed_orders), 1)
            self.assertLess(exchange.calls.index("get_positions"), exchange.calls.index("get_market_bid_ask"))
            self.assertLess(exchange.calls.index("get_positions"), exchange.calls.index("place_order"))
            self.assertEqual(bot.startup_reconciliation_status["kalshi"]["status"], "safe")
            self.assertTrue(bot.startup_reconciliation_status["kalshi"]["completed"])

    def test_lazy_startup_reconciliation_blocks_trade_on_severe_uncertainty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            exchange = ChaosRestartExchange(
                positions=[
                    Position(
                        market_id="KXHIGHNY-26APR29-T81",
                        exchange="kalshi",
                        question="Over reserved?",
                        side="YES",
                        entry_price=0.40,
                        size=8.0,
                        current_price=0.40,
                        pnl=0.0,
                        opened_at=datetime(2026, 4, 20, 17, 0, tzinfo=timezone.utc),
                    )
                ],
                balance=2.0,
            )
            bot.exchanges["kalshi"] = exchange

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                result = bot._process_signal(self._signal("KXHIGHNY-26APR29-T82"))

            self.assertEqual(result["blocked_reason"], "reconciliation_state_blocked")
            self.assertIn("negative_available_cash_after_reconcile", result["reconciliation_issues"])
            self.assertEqual(exchange.placed_orders, [])
            self.assertEqual(bot.startup_reconciliation_status["kalshi"]["status"], "blocked")
            self.assertEqual(bot.live_runtime_state["state"], "blocked")
            self.assertEqual(bot.reconciliation_gate["kalshi"]["reason"], "reconciliation_state_blocked")
            self.assertEqual(bot.reconciliation_gate["kalshi"]["recovery_state"], "requires_safe_reconciliation")
            self.assertEqual(bot.live_runtime_state["recovery_state"], "requires_safe_reconciliation")
            self.assertEqual(bot.live_failure_streaks["kalshi"]["count"], 1)

    def test_lazy_startup_reconciliation_runs_before_local_invariant_pause(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.open_orders = [
                {
                    "order_id": "stale-local-order",
                    "exchange": "kalshi",
                    "market_id": "KXHIGHNY-26APR29-T83",
                    "direction": "BUY_YES",
                    "status": "open",
                    "remaining_size": 1.0,
                    "filled_size": 0.0,
                    "placed_size": 1.0,
                },
                {
                    "order_id": "stale-local-order-2",
                    "exchange": "kalshi",
                    "market_id": "KXHIGHNY-26APR29-T83",
                    "direction": "BUY_YES",
                    "status": "open",
                    "remaining_size": 1.0,
                    "filled_size": 0.0,
                    "placed_size": 1.0,
                },
            ]
            bot.risk.sync_account_state(
                current_balance=25.0,
                available_cash=23.0,
                reserved_capital=2.0,
                total_exposure=2.0,
                open_positions=0,
            )
            exchange = ChaosRestartExchange(balance=25.0)
            bot.exchanges["kalshi"] = exchange

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                result = bot._process_signal(self._signal("KXHIGHNY-26APR29-T83"))

            self.assertIn("order", result)
            self.assertTrue(bot.startup_reconciliation_status["kalshi"]["completed"])
            self.assertEqual(bot.startup_reconciliation_status["kalshi"]["status"], "degraded")
            self.assertNotIn("kalshi", bot.reconciliation_gate)

    def test_reconnect_classifies_safe_degraded_and_blocked_startup_states(self):
        cases = [
            ("safe", ChaosRestartExchange(balance=25.0), "safe", []),
            (
                "degraded",
                ChaosRestartExchange(
                    orders=[
                        RestingOrder(
                            order_id="ord-resting",
                            market_id="KXHIGHNY-26APR29-T84",
                            exchange="kalshi",
                            question="Existing resting order?",
                            side="YES",
                            requested_size=3.0,
                            filled_size=0.0,
                            remaining_size=3.0,
                            price=0.40,
                            status="open",
                            created_at=datetime(2026, 4, 20, 17, 0, tzinfo=timezone.utc),
                        )
                    ],
                    balance=25.0,
                ),
                "degraded",
                ["resting_orders_present"],
            ),
            (
                "blocked",
                ChaosRestartExchange(
                    positions=[
                        Position(
                            market_id="KXHIGHNY-26APR29-T82",
                            exchange="kalshi",
                            question="Blocked startup?",
                            side="YES",
                            entry_price=0.40,
                            size=8.0,
                            current_price=0.40,
                            pnl=0.0,
                            opened_at=datetime(2026, 4, 20, 17, 0, tzinfo=timezone.utc),
                        )
                    ],
                    balance=2.0,
                ),
                "blocked",
                ["negative_available_cash_after_reconcile"],
            ),
        ]

        for label, exchange, expected_state, expected_issues in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmpdir:
                bot = self._make_bot(tmpdir)
                bot.exchanges["kalshi"] = exchange

                bot.connect_all()

                self.assertEqual(bot.startup_reconciliation_status["kalshi"]["status"], expected_state)
                self.assertEqual(bot.live_runtime_state["exchange_states"]["kalshi"]["state"], expected_state)
                for issue in expected_issues:
                    self.assertIn(issue, bot.startup_reconciliation_status["kalshi"]["reconciliation_issues"])

    def test_canceled_partial_discovered_after_restart_is_not_kept_as_open_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.open_orders = [
                {
                    "order_id": "ord-canceled-partial",
                    "exchange": "kalshi",
                    "market_id": "KXHIGHNY-26APR29-T85",
                    "question": "Was the order partially canceled?",
                    "direction": "BUY_YES",
                    "status": "open",
                    "requested_size": 4.0,
                    "placed_size": 4.0,
                    "filled_size": 0.0,
                    "remaining_size": 4.0,
                    "reserved_capital": 4.0,
                    "price": 0.40,
                    "created_at": "2026-04-20T17:00:00+00:00",
                }
            ]
            bot.trade_history = [
                {
                    "trade_id": "ord-canceled-partial",
                    "order_id": "ord-canceled-partial",
                    "exchange": "kalshi",
                    "market_id": "KXHIGHNY-26APR29-T85",
                    "question": "Was the order partially canceled?",
                    "direction": "BUY_YES",
                    "status": "placed",
                    "lifecycle_state": "placed_open",
                    "requested_size": 4.0,
                    "approved_size": 4.0,
                    "placed_size": 4.0,
                    "filled_size": 0.0,
                    "remaining_size": 4.0,
                    "reserved_capital": 4.0,
                    "price": 0.40,
                    "market_price": 0.40,
                    "entry_price": 0.40,
                    "resolved": False,
                    "decision_reason_code": "approved",
                }
            ]
            bot.exchanges["kalshi"] = ChaosRestartExchange(
                orders=[
                    RestingOrder(
                        order_id="ord-canceled-partial",
                        market_id="KXHIGHNY-26APR29-T85",
                        exchange="kalshi",
                        question="Was the order partially canceled?",
                        side="YES",
                        requested_size=4.0,
                        filled_size=1.5,
                        remaining_size=0.0,
                        price=0.40,
                        status="canceled",
                        created_at=datetime(2026, 4, 20, 17, 0, tzinfo=timezone.utc),
                    )
                ],
                balance=25.0,
            )

            bot.connect_all()

            self.assertEqual(bot.open_orders, [])
            corrected = bot.trade_history[0]
            self.assertEqual(corrected["status"], "canceled")
            self.assertEqual(corrected["lifecycle_state"], "canceled_partial")
            self.assertEqual(corrected["filled_size"], 1.5)
            self.assertEqual(corrected["remaining_size"], 0.0)
            self.assertEqual(corrected["reserved_capital"], 1.5)
            self.assertEqual(bot.risk.state.reserved_capital, 0.0)

    def test_severe_restart_gate_blocks_until_explicit_safe_reconciliation_clears_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            exchange = ChaosRestartExchange()
            bot.exchanges["kalshi"] = exchange
            bot.reconciliation_gate["kalshi"] = {
                "verdict": "blocked",
                "issues": ["placement_confirmation_uncertain"],
                "reason": "placement_confirmation_uncertain",
                "recovery_state": "requires_safe_reconciliation",
            }

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                blocked = bot._process_signal(self._signal("KXHIGHNY-26APR29-T86"))

            self.assertEqual(blocked["blocked_reason"], "reconciliation_state_blocked")
            self.assertEqual(exchange.placed_orders, [])

            bot._reconcile_exchange_state("kalshi", exchange)

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                allowed = bot._process_signal(self._signal("KXHIGHNY-26APR29-T87"))

            self.assertIn("order", allowed)
            self.assertEqual(len(exchange.placed_orders), 1)
            self.assertNotIn("kalshi", bot.reconciliation_gate)


if __name__ == "__main__":
    unittest.main()
