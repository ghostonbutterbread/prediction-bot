import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.runner import PredictionBot


class FakeExchange:
    def __init__(self):
        self.orders = []

    def get_markets(self, limit=30):
        return [
            SimpleNamespace(
                id="KXHIGHNY-26APR29-T80",
                question="Will NYC high temperature be above 80 degrees?",
                yes_price=0.4,
                no_price=0.6,
                metadata={"series_ticker": "KXHIGHNY", "event_ticker": "KXHIGHNY-26APR29"},
            )
        ]

    def get_order_book(self, market_id):
        return {"best_yes_ask": 0.40, "best_no_ask": 0.60}

    def get_market_bid_ask(self, market_id):
        return {"best_yes_ask": 0.40, "best_no_ask": 0.60}

    def get_balance(self):
        return 25.0

    def get_positions(self):
        return []

    def get_resting_orders(self):
        return []

    def get_market(self, market_id):
        return None

    def place_order(self, market_id, side, price, size):
        order = SimpleNamespace(id=f"ord-{len(self.orders)+1}")
        self.orders.append({"market_id": market_id, "side": side, "price": price, "size": size})
        return order

    def close(self):
        return None


class RunnerSingleTradeModeTests(unittest.TestCase):
    def _make_bot(self, tmpdir):
        config = {
            "log_dir": tmpdir,
            "data_dir": tmpdir,
            "trading": {"mode": "live", "enabled": True, "single_trade_mode": True},
            "strategy": {
                "min_edge": 0.05,
                "min_confidence": 0.5,
                "enable_news": False,
                "enable_social": False,
                "enable_ai": False,
            },
        }
        bot = PredictionBot(config)
        bot.exchanges["kalshi"] = FakeExchange()
        bot.risk.state.current_balance = 25.0
        bot.risk.state.peak_balance = 25.0
        bot.risk.state.session_starting_balance = 25.0
        bot.risk.state.session_peak_balance = 25.0
        bot.risk.state.available_cash = 25.0
        bot.risk.state.max_drawdown_halt = False
        return bot

    def test_single_trade_mode_blocks_new_entries_after_first_successful_trade(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            signal = {
                "exchange": "kalshi",
                "market_id": "KXHIGHNY-26APR29-T80",
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
            with patch.object(bot.kelly, "calculate", return_value=4.0):
                first_result = bot._process_signal(signal)
                bot.single_trade_completed = True
                second_result = bot._process_signal(signal)
                bot._log_lifecycle_event(
                    "single_trade_completed",
                    {
                        "open_positions": len(bot.open_positions),
                        "open_orders": len(bot.open_orders),
                        "behavior": "no_new_entries_continue_resolution_tracking",
                    },
                )

            self.assertIn("order", first_result)
            self.assertEqual(len(bot.exchanges["kalshi"].orders), 1)
            self.assertEqual(second_result["blocked_reason"], "single_trade_mode_completed")

            with open(bot.log_dir / "lifecycle.jsonl") as f:
                events = [json.loads(line) for line in f if line.strip()]
            self.assertTrue(any(event["event"] == "single_trade_completed" for event in events))


if __name__ == "__main__":
    unittest.main()
