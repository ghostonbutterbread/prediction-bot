import unittest
from types import SimpleNamespace

from bot.market_router import route_market


class MarketRouterTests(unittest.TestCase):
    def test_prime_energy_wind_market_is_not_weather_daily_temperature(self):
        route = route_market(
            {
                "market_id": "KXPRIMEENGCONSUMPTION-30-WIND",
                "question": "Will wind power account for at least 30% of prime energy consumption?",
                "category": "KXPRIMEENGCONSUMPTION",
            },
            {"scan": {"allowed_market_routes": ["weather.daily_temperature"]}},
        )

        self.assertFalse(route.allowed)
        self.assertEqual(route.reason_code, "unknown_market_route")
        self.assertEqual(route.group, "unknown")

    def test_daily_temperature_market_is_allowed(self):
        market = SimpleNamespace(
            id="KXHIGHNY-260506-T71",
            question="Will the high temperature in New York exceed 71 degrees?",
            category="KXHIGHNY",
            metadata={"series": "KXHIGHNY", "event_ticker": "KXHIGHNY-260506"},
        )

        route = route_market(market, {"scan": {"allowed_market_routes": ["weather.daily_temperature"]}})

        self.assertTrue(route.allowed)
        self.assertEqual(route.group, "weather")
        self.assertEqual(route.family, "daily_temperature")
        self.assertEqual(route.subcategory, "tail_high")

    def test_legacy_archive_high_temperature_market_is_allowed(self):
        route = route_market(
            {
                "market_id": "HIGHNY0-21JUL17-T90",
                "question": "Will the high temperature in New York City be over 90° on Saturday?",
                "category": "HIGHNY0-21JUL17",
            },
            {"scan": {"allowed_market_routes": ["weather.daily_temperature"]}},
        )

        self.assertTrue(route.allowed)
        self.assertEqual(route.group, "weather")
        self.assertEqual(route.family, "daily_temperature")
        self.assertEqual(route.subcategory, "tail_high")

    def test_daily_temperature_bucket_market_is_allowed(self):
        route = route_market(
            {
                "market_id": "KXLOWTDEN-26MAY06-B28.5",
                "question": "Will the low temperature in Denver be between 28.5 and 30.5 degrees?",
                "category": "KXLOWTDEN",
            },
            {"scan": {"allowed_market_routes": ["weather.daily_temperature"]}},
        )

        self.assertTrue(route.allowed)
        self.assertEqual(route.subcategory, "bucket")

    def test_prefix_without_temperature_semantics_is_rejected(self):
        route = route_market(
            {
                "market_id": "KXHIGHGDP-260506-T5",
                "question": "Will GDP growth exceed 5 percent?",
                "category": "KXHIGHGDP",
            },
            {"scan": {"allowed_market_routes": ["weather.daily_temperature"]}},
        )

        self.assertFalse(route.allowed)
        self.assertEqual(route.reason_code, "daily_temperature_semantics_missing")

    def test_legacy_archive_high_prefix_without_temperature_semantics_is_rejected(self):
        route = route_market(
            {
                "market_id": "HIGHNY0-21JUL17-T90",
                "question": "Will a New York index close over 90?",
                "category": "HIGHNY0-21JUL17",
            },
            {"scan": {"allowed_market_routes": ["weather.daily_temperature"]}},
        )

        self.assertFalse(route.allowed)
        self.assertEqual(route.reason_code, "daily_temperature_semantics_missing")

    def test_empty_allowed_routes_rejects_daily_temperature(self):
        route = route_market(
            {
                "market_id": "KXHIGHNY-260506-T71",
                "question": "Will the high temperature in New York exceed 71 degrees?",
                "category": "KXHIGHNY",
            },
            {"scan": {"allowed_market_routes": []}},
        )

        self.assertFalse(route.allowed)
        self.assertEqual(route.reason_code, "market_route_not_allowed")


if __name__ == "__main__":
    unittest.main()
