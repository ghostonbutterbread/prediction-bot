import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.live_execution import RunnerLiveExecutionAdapter
from bot.runner import PredictionBot


class FakeExchange:
    def __init__(self):
        self.orders = []

    def get_balance(self):
        return 25.0

    def get_market_bid_ask(self, market_id):
        return {"best_yes_ask": 0.40, "best_no_ask": 0.60}

    def place_order(self, market_id, side, price, size):
        order = SimpleNamespace(id=f"ord-{len(self.orders)+1}")
        self.orders.append({"market_id": market_id, "side": side, "price": price, "size": size})
        return order


class LiveExecutionTests(unittest.TestCase):
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

    def test_execute_places_order_and_updates_runner_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            adapter = RunnerLiveExecutionAdapter(bot)
            exchange = FakeExchange()
            signal = {
                "exchange": "kalshi",
                "market_id": "m1",
                "question": "Will rain happen?",
            }
            decision = SimpleNamespace(
                action="BUY_YES",
                position_size=4.0,
                reason="ok",
                reason_code="ok",
                requested_position_size=4.0,
            )

            result = adapter.execute(signal, decision, exchange)
            self.assertIsNotNone(result)
            self.assertEqual(len(bot.open_positions), 1)
            self.assertEqual(len(bot.trade_history), 1)
            self.assertEqual(exchange.orders[0]["size"], 4.0)
            self.assertIn("refresh", result)
            self.assertEqual(result["refresh"]["balance"], 25.0)


if __name__ == "__main__":
    unittest.main()
