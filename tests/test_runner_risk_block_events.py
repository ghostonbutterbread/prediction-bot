import json
import tempfile
import unittest
from unittest.mock import patch

from bot.runner import PredictionBot


class FakeExchange:
    def get_balance(self):
        return 25.0


class RunnerRiskBlockEventTests(unittest.TestCase):
    def _make_bot(self, tmpdir):
        config = {
            "log_dir": tmpdir,
            "data_dir": tmpdir,
            "trading_enabled": False,
            "trading": {"mode": "live", "trading_enabled": False},
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
        bot.risk.state.available_cash = 25.0
        bot.risk.state.trading_enabled = False
        return bot

    def test_risk_block_event_logged(self):
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

            with patch.object(bot.kelly, "calculate", return_value=5.0):
                result = bot._process_signal(signal)
                bot._log_risk_block_event(signal, result)

            with open(f"{tmpdir}/risk_blocks.jsonl") as f:
                entries = [json.loads(line) for line in f if line.strip()]

            self.assertEqual(entries[0]["blocked_reason"], "risk_trading_paused_by_operator")
            self.assertEqual(entries[0]["decision_reason_code"], "risk_trading_paused_by_operator")


if __name__ == "__main__":
    unittest.main()
