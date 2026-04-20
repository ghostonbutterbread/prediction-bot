import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.runner import PredictionBot
from bot.shared_core import build_trade_decision


class FakeExchange:
    def __init__(self):
        self.orders = []

    def get_markets(self, limit=30):
        return []

    def get_order_book(self, market_id):
        return {"best_yes_ask": 0.40, "best_no_ask": 0.60}

    def get_market_bid_ask(self, market_id):
        return {"best_yes_ask": 0.40, "best_no_ask": 0.60}

    def get_balance(self):
        return 25.0

    def place_order(self, market_id, side, price, size):
        order = SimpleNamespace(id=f"ord-{len(self.orders)+1}")
        self.orders.append({"market_id": market_id, "side": side, "price": price, "size": size})
        return order

    def close(self):
        return None


class RunnerLivePathTests(unittest.TestCase):
    def _make_bot(self, tmpdir, **overrides):
        config = {
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
        config.update(overrides)
        bot = PredictionBot(config)
        bot.exchanges["kalshi"] = FakeExchange()
        bot.risk.state.current_balance = 25.0
        bot.risk.state.peak_balance = 25.0
        bot.risk.state.session_starting_balance = 25.0
        bot.risk.state.session_peak_balance = 25.0
        bot.risk.state.available_cash = 25.0
        bot.risk.state.max_drawdown_halt = False
        return bot

    def test_live_path_uses_shared_risk_caps_before_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
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

            with patch.object(bot.kelly, "calculate", return_value=10.0):
                result = bot._process_signal(signal)

            self.assertIn("order", result)
            self.assertEqual(len(bot.exchanges["kalshi"].orders), 1)
            self.assertEqual(bot.exchanges["kalshi"].orders[0]["size"], 4.0)

    def test_live_path_respects_trading_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir, trading_enabled=False, trading={"mode": "live", "trading_enabled": False})
            bot.risk.state.trading_enabled = False
            signal = {
                "exchange": "kalshi",
                "market_id": "m2",
                "question": "Will snow happen?",
                "direction": "BUY_YES",
                "market_price": 0.35,
                "yes_price": 0.35,
                "no_price": 0.65,
                "model_probability": 0.70,
                "edge": 0.20,
                "confidence": 0.90,
            }

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                result = bot._process_signal(signal)

            self.assertEqual(result["blocked_reason"], "risk_trading_paused_by_operator")
            self.assertEqual(len(bot.exchanges["kalshi"].orders), 0)

    def test_build_status_snapshot_uses_shared_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = self._make_bot(tmpdir)
            bot.trade_history = [
                {"resolved": False},
                {"resolved": True},
            ]
            bot.open_positions = [
                SimpleNamespace(size=4.0),
            ]
            snapshot = bot.build_status_snapshot(reason="manual status", scan_num=7)

            self.assertEqual(snapshot.scan_num, 7)
            self.assertEqual(snapshot.open_trades, 1)
            self.assertEqual(snapshot.resolved_trades, 1)
            self.assertEqual(snapshot.total_trades, 2)
            self.assertIn("source", snapshot.extra)


if __name__ == "__main__":
    unittest.main()
