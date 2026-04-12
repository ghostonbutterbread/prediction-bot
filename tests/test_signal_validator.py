import unittest
from datetime import datetime, timedelta, timezone

from bot.exchanges.base import Market
from bot.strategies.signal_validator import SignalValidator


def make_market(question: str, yes_price: float = 0.5, volume: float = 1000) -> Market:
    return Market(
        id="mkt-1",
        exchange="kalshi",
        question=question,
        yes_price=yes_price,
        no_price=1 - yes_price,
        volume=volume,
        liquidity=1000,
        closes_at=datetime.now(timezone.utc) + timedelta(days=1),
        category="test",
        metadata={},
    )


class SignalValidatorTests(unittest.TestCase):
    def setUp(self):
        self.validator = SignalValidator()

    def test_shib_implausible_move_caps_prob_and_warns(self):
        market = make_market("Will SHIB be above $0.000014499 tomorrow?", yes_price=0.62)
        signal = {
            "signal_type": "crypto",
            "predicted_prob": 0.86,
            "confidence": 0.78,
            "question_side": "above",
            "data": {
                "current_price": 0.00000605,
                "daily_volatility": 7.0,
                "required_move_pct": (0.000014499 - 0.00000605) / 0.00000605,
                "days_to_expiry": 1.0,
                "answered_question_side": "above",
            },
        }

        result = self.validator.validate(signal, market, "live")

        self.assertTrue(result.accepted)
        self.assertLessEqual(result.adjusted_confidence, 0.25)
        self.assertEqual(result.adjusted_prob, 0.10)
        self.assertTrue(any("Implausible crypto move" in warning for warning in result.warnings))

    def test_weather_out_of_bounds_is_rejected(self):
        market = make_market("Will the high temp in Austin be above 100°?", yes_price=0.55)
        signal = {
            "signal_type": "weather",
            "predicted_prob": 0.92,
            "confidence": 0.88,
            "question_side": "above",
            "data": {
                "actual_temp_used": 150.0,
                "predicted_temp": 150.0,
            },
        }

        result = self.validator.validate(signal, market, "live")

        self.assertFalse(result.accepted)
        self.assertIn("outside plausible bounds", result.rejection_reason)

    def test_liquidity_floor_caps_and_rejects(self):
        cap_market = make_market("Will BTC be above $100000 tomorrow?", volume=300)
        cap_market.liquidity = 300
        cap_signal = {
            "signal_type": "crypto",
            "predicted_prob": 0.60,
            "confidence": 0.75,
            "question_side": "above",
            "data": {
                "daily_volatility": 5.0,
                "required_move_pct": 0.03,
                "days_to_expiry": 1.0,
                "answered_question_side": "above",
            },
        }

        capped = self.validator.validate(cap_signal, cap_market, "live")
        self.assertTrue(capped.accepted)
        self.assertEqual(capped.adjusted_confidence, 0.50)

        reject_market = make_market("Will BTC be above $100000 tomorrow?", volume=40)
        reject_market.liquidity = 50
        rejected = self.validator.validate(cap_signal, reject_market, "live")
        self.assertFalse(rejected.accepted)
        self.assertIn("too thin", rejected.rejection_reason)

    def test_cross_source_disagreement_reduces_both_confidences(self):
        market = make_market("Will the high temp in Austin be above 80°?", yes_price=0.50, volume=5000)
        signals = {
            "live": {
                "signal_type": "weather",
                "predicted_prob": 0.82,
                "confidence": 0.70,
                "question_side": "above",
                "data": {"actual_temp_used": 85.0},
            },
            "news": {
                "signal_type": "news",
                "predicted_prob": 0.22,
                "confidence": 0.60,
                "source_timestamp": datetime.now(timezone.utc).isoformat(),
                "data": {"sources": ["reuters", "bbc"]},
            },
        }

        results = self.validator.validate_all(signals, market)

        self.assertTrue(results["live"].accepted)
        self.assertTrue(results["news"].accepted)
        self.assertEqual(results["live"].adjusted_confidence, 0.60)
        self.assertEqual(results["news"].adjusted_confidence, 0.50)
        self.assertTrue(any("Weather and news disagree" in warning for warning in results["live"].warnings))
        self.assertTrue(any("Weather and news disagree" in warning for warning in results["news"].warnings))


if __name__ == "__main__":
    unittest.main()
