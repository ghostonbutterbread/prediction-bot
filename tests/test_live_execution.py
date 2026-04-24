import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.live_execution import RunnerLiveExecutionAdapter
from bot.shared_core import build_execution_snapshot
from bot.runner import LivePosition, PredictionBot


class FakeExchange:
    def __init__(self):
        self.orders = []

    def get_balance(self):
        return 25.0

    def get_market_bid_ask(self, market_id):
        return {"best_yes_ask": 0.40, "best_no_ask": 0.60, "best_yes_bid": 0.39, "best_no_bid": 0.59}

    def place_order(self, market_id, side, price, size):
        order = SimpleNamespace(id=f"ord-{len(self.orders)+1}")
        self.orders.append({"market_id": market_id, "side": side, "price": price, "size": size})
        return order


class LiveExecutionTests(unittest.TestCase):
    def test_build_execution_snapshot_uses_book_prices_and_side_specific_market_price(self):
        yes_snapshot = build_execution_snapshot(
            {"market_price": 0.40},
            direction="BUY_YES",
            bid_ask={"best_yes_ask": 0.41, "best_no_ask": 0.59, "best_yes_bid": 0.40, "best_no_bid": 0.58},
        )
        self.assertEqual(yes_snapshot["market_price"], 0.41)
        self.assertEqual(yes_snapshot["source"], "book")

        no_snapshot = build_execution_snapshot(
            {"market_price": 0.40},
            direction="BUY_NO",
            bid_ask={"best_yes_ask": 0.41, "best_no_ask": 0.59, "best_yes_bid": 0.40, "best_no_bid": 0.58},
        )
        self.assertEqual(no_snapshot["market_price"], 0.59)

    def _make_bot(self, tmpdir):
        bot = PredictionBot(
            {
                "log_dir": tmpdir,
                "data_dir": tmpdir,
                "trading": {"mode": "live", "enabled": True},
                "strategy": {
                    "min_edge": 0.05,
                    "min_confidence": 0.5,
                    "enable_news": False,
                    "enable_social": False,
                    "enable_ai": False,
                },
                "max_tradable_balance_usd": 10.0,
                "max_position_size_usd": 4.0,
            }
        )
        bot.risk.state.current_balance = 25.0
        bot.risk.state.available_cash = 25.0
        bot.risk.state.peak_balance = 25.0
        bot.risk.state.session_starting_balance = 25.0
        bot.risk.state.session_peak_balance = 25.0
        bot.risk.state.max_drawdown_halt = False
        return bot

    def test_build_trade_context_uses_live_account_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange()
            signal = {
                "exchange": "kalshi",
                "market_id": "m1",
                "question": "Will rain happen?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.30,
                "confidence": 0.90,
            }

            context = adapter.build_trade_context(signal, exchange, bot.config)
            self.assertEqual(context.account_state.current_balance, 25.0)
            self.assertEqual(context.account_state.metadata["effective_tradable_cash"], 10.0)

    def test_build_trade_context_counts_pending_same_event_exposure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.open_positions = [
                LivePosition(
                    market_id="KXHIGHNY-26APR16-T70",
                    question="Will NYC high be below 70?",
                    direction="BUY_YES",
                    price=0.40,
                    size=2.0,
                    order_id="pos-1",
                    created_at="2026-04-20T00:00:00+00:00",
                    event_key="KXHIGHNY-26APR16",
                )
            ]
            bot.open_orders = [
                {
                    "order_id": "ord-open",
                    "market_id": "KXHIGHNY-26APR16-T71",
                    "question": "Will NYC high be below 71?",
                    "direction": "BUY_YES",
                    "remaining_size": 3.0,
                    "price": 0.42,
                    "event_key": "KXHIGHNY-26APR16",
                }
            ]
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange()
            signal = {
                "exchange": "kalshi",
                "market_id": "KXHIGHNY-26APR16-T72",
                "question": "Will NYC high be below 72?",
                "direction": "BUY_YES",
                "market_price": 0.43,
                "yes_price": 0.43,
                "no_price": 0.57,
                "model_probability": 0.70,
                "edge": 0.30,
                "confidence": 0.90,
            }

            context = adapter.build_trade_context(signal, exchange, bot.config)
            snapshot = context.metadata["event_snapshot"]
            self.assertEqual(snapshot["event_position_count_before"], 2)
            self.assertEqual(snapshot["event_exposure_before"], 5.0)
            self.assertEqual(snapshot["filled_event_exposure_before"], 2.0)
            self.assertEqual(snapshot["pending_event_exposure_before"], 3.0)
            self.assertIn("KXHIGHNY-26APR16-T71", snapshot["held_market_ids"])

    def test_execute_places_order_and_updates_runner_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange()
            signal = {
                "exchange": "kalshi",
                "market_id": "m1",
                "question": "Will rain happen?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.30,
                "confidence": 0.90,
            }
            decision = SimpleNamespace(
                action="BUY_YES",
                position_size=2.5,
                entry_price=0.40,
                reason="ok",
                reason_code="ok",
                requested_position_size=2.5,
                reasoning={},
            )

            result = adapter.execute(signal, decision, exchange)
            self.assertIsNotNone(result)
            self.assertEqual(len(bot.open_positions), 1)
            self.assertEqual(len(bot.trade_history), 1)
            self.assertEqual(exchange.orders[0]["size"], 1.0)
            self.assertIn("refresh", result)
            self.assertEqual(result["refresh"]["balance"], 25.0)

    def test_live_context_and_execution_snapshot_match_under_identical_prices(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange()
            signal = {
                "exchange": "kalshi",
                "market_id": "m-parity",
                "question": "Will parity hold?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.70,
                "edge": 0.30,
                "confidence": 0.90,
            }

            snapshot = build_execution_snapshot(
                signal,
                direction="BUY_YES",
                bid_ask=exchange.get_market_bid_ask("m-parity"),
            )
            context = adapter.build_trade_context({**signal, **snapshot}, exchange, bot.config)

            self.assertEqual(context.market_price, snapshot["market_price"])
            self.assertEqual(context.yes_price, snapshot["yes_price"])
            self.assertEqual(context.metadata["event_snapshot"]["execution_snapshot_source"], "fallback")

    def test_execute_revalidates_against_live_ask_before_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.config["max_entry_price"] = 0.70
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange()
            exchange.get_market_bid_ask = lambda market_id: {"best_yes_ask": 0.75, "best_no_ask": 0.25, "best_yes_bid": 0.74, "best_no_bid": 0.24}
            signal = {
                "exchange": "kalshi",
                "market_id": "m1",
                "question": "Will rain happen?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.90,
                "edge": 0.30,
                "confidence": 0.90,
            }
            decision = SimpleNamespace(
                action="BUY_YES",
                approved=True,
                position_size=4.0,
                entry_price=0.40,
                reason="ok",
                reason_code="approved",
                requested_position_size=4.0,
                reasoning={},
            )

            result = adapter.execute(signal, decision, exchange)

            self.assertIsNone(result)
            self.assertEqual(exchange.orders, [])

    def test_build_trade_context_threads_price_improvement_and_book_inputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.risk.require_price_improvement_for_same_market_family = True
            bot.risk.price_improvement_ticks = 0.03
            bot.open_positions = [
                LivePosition(
                    market_id="KXHIGHNY-26APR16-T70",
                    question="Will NYC high be below 70?",
                    direction="BUY_YES",
                    price=0.42,
                    size=2.0,
                    order_id="pos-1",
                    created_at="2026-04-20T00:00:00+00:00",
                    event_key="KXHIGHNY-26APR16",
                )
            ]
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange()
            signal = {
                "exchange": "kalshi",
                "market_id": "KXHIGHNY-26APR16-T72",
                "question": "Will NYC high be below 72?",
                "direction": "BUY_YES",
                "market_price": 0.39,
                "yes_price": 0.39,
                "no_price": 0.61,
                "best_yes_ask": 0.39,
                "best_no_ask": 0.61,
                "best_yes_bid": 0.38,
                "best_no_bid": 0.60,
                "liquidity": 50.0,
                "model_probability": 0.70,
                "edge": 0.30,
                "confidence": 0.90,
            }

            context = adapter.build_trade_context(signal, exchange, bot.config)
            snapshot = context.metadata["event_snapshot"]
            self.assertEqual(snapshot["best_same_family_entry_price"], 0.42)
            self.assertEqual(snapshot["best_yes_ask"], 0.39)
            self.assertEqual(snapshot["liquidity"], 50.0)
            self.assertTrue(context.metadata["retrade_policy"]["require_price_improvement_for_same_market_family"])

    def test_execute_rechecks_same_event_exposure_before_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.risk.max_event_exposure_pct = 0.10
            bot.open_orders = [
                {
                    "order_id": "ord-open",
                    "market_id": "KXHIGHNY-26APR16-T71",
                    "question": "Will NYC high be below 71?",
                    "direction": "BUY_YES",
                    "remaining_size": 2.5,
                    "price": 0.42,
                    "event_key": "KXHIGHNY-26APR16",
                }
            ]
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange()
            signal = {
                "exchange": "kalshi",
                "market_id": "KXHIGHNY-26APR16-T72",
                "question": "Will NYC high be below 72?",
                "direction": "BUY_YES",
                "market_price": 0.40,
                "yes_price": 0.40,
                "no_price": 0.60,
                "model_probability": 0.90,
                "edge": 0.30,
                "confidence": 0.90,
            }
            decision = SimpleNamespace(
                action="BUY_YES",
                approved=True,
                position_size=4.0,
                entry_price=0.40,
                reason="ok",
                reason_code="approved",
                requested_position_size=4.0,
                reasoning={},
            )

            result = adapter.execute(signal, decision, exchange)

            self.assertIsNone(result)
            self.assertEqual(exchange.orders, [])


if __name__ == "__main__":
    unittest.main()
