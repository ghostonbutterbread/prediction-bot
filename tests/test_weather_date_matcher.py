import unittest

from bot.weather.date_matcher import derive_market_date, validate_weather_date_match


class WeatherDateMatcherTests(unittest.TestCase):
    def test_validation_passes_when_dates_match(self):
        result = validate_weather_date_match(
            {"market_date": "2026-03-25", "market_ticker": "KXHIGHNY-26MAR25-T51"},
            weather_date="2026-03-25T18:30:00Z",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "dates_match")
        self.assertEqual(result.market_date, "2026-03-25")
        self.assertEqual(result.weather_date, "2026-03-25")
        self.assertEqual(result.source, "field:market_date")

    def test_validation_fails_when_dates_mismatch(self):
        result = validate_weather_date_match(
            {"market_ticker": "KXHIGHNY-26MAR25-T51"},
            weather_date="2026-04-29",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "date_mismatch")
        self.assertEqual(result.market_date, "2026-03-25")
        self.assertEqual(result.weather_date, "2026-04-29")
        self.assertEqual(result.source, "ticker:market_ticker")

    def test_validation_fails_when_market_date_missing(self):
        result = validate_weather_date_match(
            {"market_ticker": "KXHIGHNY-T51", "question": "Will the high temp in NYC be above 51 degrees?"},
            weather_date="2026-03-25",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "missing_market_date")
        self.assertIsNone(result.market_date)
        self.assertEqual(result.weather_date, "2026-03-25")

    def test_validation_fails_when_weather_date_missing(self):
        result = validate_weather_date_match({"market_ticker": "KXHIGHNY-26MAR25-T51"})

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "missing_weather_date")
        self.assertEqual(result.market_date, "2026-03-25")
        self.assertIsNone(result.weather_date)

    def test_parses_kxhigh_archive_style_ticker_date(self):
        market_date = derive_market_date(
            {
                "MARKET_TICKER": "KXHIGHTSEA-26MAR14-T50",
                "MARKET_TITLE": "Will the maximum temperature be >50° on Mar 14, 2026?",
            }
        )

        self.assertEqual(market_date.isoformat, "2026-03-14")
        self.assertEqual(market_date.source, "ticker:MARKET_TICKER")

    def test_parses_kxlow_question_date_when_ticker_has_no_date(self):
        result = validate_weather_date_match(
            {
                "market_ticker": "KXLOWTMIA-T72",
                "question": "Will the minimum temperature be >72° on Apr 14, 2026?",
            },
            {"weather_date": "2026-04-14"},
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.market_date, "2026-04-14")
        self.assertEqual(result.weather_date, "2026-04-14")
        self.assertEqual(result.source, "question:question")

    def test_validation_derives_weather_date_from_forecast_start_metadata(self):
        result = validate_weather_date_match(
            {"market_ticker": "KXHIGHNY-26APR29-T80"},
            {"forecast_start": "2026-04-29T07:00:00-04:00"},
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "dates_match")
        self.assertEqual(result.weather_date, "2026-04-29")

    def test_validation_derives_weather_date_from_period_start_metadata(self):
        result = validate_weather_date_match(
            {"market_ticker": "KXHIGHNY-26APR29-T80"},
            {"forecast_period_start": "2026-04-29T06:00:00-04:00", "forecast_period_name": "Today"},
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "dates_match")
        self.assertEqual(result.weather_date, "2026-04-29")


if __name__ == "__main__":
    unittest.main()
