import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from bot.strategies.enhanced import EnhancedStrategyEngine


class EnhancedStrategyEngineTests(unittest.TestCase):
    def _market(self, yes_price: float, no_price: float) -> SimpleNamespace:
        return SimpleNamespace(
            id="market-1",
            exchange="kalshi",
            question="Will test event happen?",
            yes_price=yes_price,
            no_price=no_price,
            volume=10000,
            category="news",
            closes_at=datetime.now(timezone.utc) + timedelta(days=2),
        )

    def test_analyze_market_prefers_buy_no_when_no_quote_has_better_edge(self):
        engine = EnhancedStrategyEngine(
            {
                "enable_news": False,
                "enable_social": False,
                "enable_ai": False,
            }
        )
        market = self._market(yes_price=0.48, no_price=0.40)

        with patch.object(engine, "_price_signal", return_value={"predicted_prob": 0.52, "confidence": 0.8}), \
             patch.object(engine, "_live_data_signal", return_value=None), \
             patch.object(engine, "_volume_signal", return_value=None), \
             patch.object(engine, "_time_signal", return_value=None), \
             patch.object(engine.validator, "validate_all", return_value={
                 "price": SimpleNamespace(
                     accepted=True,
                     adjusted_prob=0.52,
                     adjusted_confidence=0.8,
                     warnings=[],
                     rejection_reason=None,
                 )
             }):
            signal = engine.analyze_market(market)

        self.assertIsNotNone(signal)
        self.assertEqual(signal["direction"], "BUY_NO")
        self.assertAlmostEqual(signal["market_price"], 0.40)
        self.assertAlmostEqual(signal["no_market_price"], 0.40)
        self.assertAlmostEqual(signal["edge"], 0.08)

    def test_analyze_market_prefers_buy_yes_when_yes_quote_has_better_edge(self):
        engine = EnhancedStrategyEngine(
            {
                "enable_news": False,
                "enable_social": False,
                "enable_ai": False,
            }
        )
        market = self._market(yes_price=0.41, no_price=0.65)

        with patch.object(engine, "_price_signal", return_value={"predicted_prob": 0.52, "confidence": 0.8}), \
             patch.object(engine, "_live_data_signal", return_value=None), \
             patch.object(engine, "_volume_signal", return_value=None), \
             patch.object(engine, "_time_signal", return_value=None), \
             patch.object(engine.validator, "validate_all", return_value={
                 "price": SimpleNamespace(
                     accepted=True,
                     adjusted_prob=0.52,
                     adjusted_confidence=0.8,
                     warnings=[],
                     rejection_reason=None,
                 )
             }):
            signal = engine.analyze_market(market)

        self.assertIsNotNone(signal)
        self.assertEqual(signal["direction"], "BUY_YES")
        self.assertAlmostEqual(signal["market_price"], 0.41)
        self.assertAlmostEqual(signal["edge"], 0.11)


if __name__ == "__main__":
    unittest.main()
