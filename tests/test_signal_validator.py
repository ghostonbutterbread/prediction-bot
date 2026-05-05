import unittest
from datetime import datetime, timedelta, timezone

from bot.exchanges.base import Market
from bot.feeds.weather_pro import MultiSourceForecast, NWSFeed, ProWeatherEngine, WeatherSnapshot
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
        self.assertEqual(capped.adjusted_confidence, 0.60)

        reject_market = make_market("Will BTC be above $100000 tomorrow?", volume=40)
        reject_market.liquidity = 50
        reject_market.yes_price = 0.0
        reject_market.no_price = 0.0
        reject_market.yes_bid = 0.0
        reject_market.no_bid = 0.0
        rejected = self.validator.validate(cap_signal, reject_market, "live")
        self.assertFalse(rejected.accepted)
        self.assertIn("too thin", rejected.rejection_reason)

    def test_zero_liquidity_with_real_quotes_is_soft_penalty_not_hard_reject(self):
        market = make_market("Will the high temp in NYC be below 79° tomorrow?", yes_price=0.02, volume=1700)
        market.liquidity = 0
        market.yes_bid = 0.01
        market.no_bid = 0.98
        signal = {
            "signal_type": "weather",
            "predicted_prob": 0.85,
            "confidence": 0.62,
            "question_side": "below",
            "data": {
                "actual_temp_used": 78.0,
                "predicted_temp": 78.0,
            },
        }

        result = self.validator.validate(signal, market, "live")
        self.assertTrue(result.accepted)
        self.assertGreaterEqual(result.adjusted_confidence, 0.50)
        self.assertTrue(any("reported $0 liquidity" in warning for warning in result.warnings))

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

    def test_weather_source_details_include_feed_timestamp_metadata_without_inventing_dates(self):
        fetched_at = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
        forecast = MultiSourceForecast(
            city="austin",
            high_temp_f=84.0,
            low_temp_f=63.0,
            current_temp_f=72.0,
            sources_used=["open-meteo", "nws"],
            confidence=0.9,
            fetched_at=fetched_at,
            source_agreement=0.95,
            details={
                "individual_highs": {"open-meteo": 83.0, "nws": 84.0},
                "individual_lows": {"open-meteo": 62.0, "nws": 63.0},
                "individual_currents": {"open-meteo": 71.0, "nws": 72.0},
                "source_confidences": {"open-meteo": 0.85, "nws": 0.85},
                "source_fetched_at": {"open-meteo": "2026-04-27T12:00:00+00:00", "nws": "2026-04-27T12:01:00+00:00"},
                "source_forecast_starts": {"open-meteo": "2026-04-27T07:00", "nws": "2026-04-27T06:00:00-05:00"},
                "source_forecast_ends": {"open-meteo": "2026-04-28T06:00", "nws": "2026-04-27T18:00:00-05:00"},
                "source_forecast_times": {"open-meteo": ["2026-04-27T07:00", "2026-04-27T08:00"]},
                "source_forecast_period_names": {"nws": "Today"},
                "source_forecast_period_starts": {"nws": "2026-04-27T06:00:00-05:00"},
                "source_forecast_period_ends": {"nws": "2026-04-27T18:00:00-05:00"},
                "settlement_source": "nws",
            },
        )

        details = ProWeatherEngine._source_contribution_details(forecast)
        by_source = {detail["source_name"]: detail for detail in details}

        self.assertEqual(by_source["open-meteo"]["forecast_start"], "2026-04-27T07:00")
        self.assertEqual(by_source["open-meteo"]["forecast_times"], ["2026-04-27T07:00", "2026-04-27T08:00"])
        self.assertEqual(by_source["nws"]["forecast_period_name"], "Today")
        self.assertNotIn("weather_date", by_source["open-meteo"])
        self.assertNotIn("forecast_date", by_source["nws"])

        snapshot = WeatherSnapshot(
            city="austin",
            high_temp_f=84.0,
            low_temp_f=63.0,
            current_temp_f=72.0,
            source="nws",
            fetched_at=fetched_at,
            forecast_hours_ahead=2,
            confidence=0.85,
            forecast_period_name="Today",
        )
        self.assertEqual(snapshot.forecast_period_name, "Today")

    def test_nws_forecast_metadata_records_distinct_high_and_low_periods(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "properties": {
                        "periods": [
                            {
                                "number": 1,
                                "name": "Today",
                                "startTime": "2026-04-27T06:00:00-05:00",
                                "endTime": "2026-04-27T18:00:00-05:00",
                                "isDaytime": True,
                                "temperature": 84,
                                "temperatureUnit": "F",
                                "shortForecast": "Sunny",
                            },
                            {
                                "number": 2,
                                "name": "Tonight",
                                "startTime": "2026-04-27T18:00:00-05:00",
                                "endTime": "2026-04-28T06:00:00-05:00",
                                "isDaytime": False,
                                "temperature": 63,
                                "temperatureUnit": "F",
                                "shortForecast": "Clear",
                            },
                        ]
                    }
                }

        class FakeHttp:
            def get(self, *args, **kwargs):
                return FakeResponse()

        feed = NWSFeed()
        feed.http = FakeHttp()
        feed._points_cache["austin"] = (datetime.now(timezone.utc), ("EWX", 152, 91))

        snapshot = feed.get_forecast("austin")

        self.assertIsNotNone(snapshot)
        details = snapshot.source_details
        self.assertEqual(details["periods_used"][0]["name"], "Today")
        self.assertEqual(details["periods_used"][1]["name"], "Tonight")
        self.assertEqual(details["high_period"]["number"], 1)
        self.assertEqual(details["high_period"]["temperature"], 84)
        self.assertEqual(details["low_period"]["number"], 2)
        self.assertEqual(details["low_period"]["temperature"], 63)


if __name__ == "__main__":
    unittest.main()
