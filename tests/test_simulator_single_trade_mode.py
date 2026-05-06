import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.simulator import Simulator


class FakeExchange:
    def get_markets(self, limit=100):
        return [
            SimpleNamespace(
                id="KXHIGHNY-26APR29-T80",
                question="Will NYC high temperature be above 80 degrees?",
                yes_price=0.4,
                no_price=0.6,
                closes_at=None,
                metadata={"series_ticker": "KXHIGHNY", "event_ticker": "KXHIGHNY-26APR29"},
            )
        ]


class SimulatorSingleTradeModeTests(unittest.TestCase):
    def test_single_trade_mode_only_takes_one_trade(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sim = Simulator(
                {
                    "data_dir": tmpdir,
                    "trading": {"single_trade_mode": True},
                    "strategy": {
                        "min_edge": 0.05,
                        "min_confidence": 0.5,
                        "enable_news": False,
                        "enable_social": False,
                        "enable_ai": False,
                    },
                }
            )
            signal = {
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

            with patch.object(sim.strategy, "analyze_market", return_value=signal), patch.object(sim.kelly, "calculate", return_value=4.0):
                result = sim.scan(FakeExchange())
                second_trade = sim._create_trade(signal)

            self.assertEqual(result["trades"], 1)
            self.assertTrue(sim.single_trade_completed)
            self.assertIsNone(second_trade)


if __name__ == "__main__":
    unittest.main()
