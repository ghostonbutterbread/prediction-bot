import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from bot.exchanges.kalshi import KalshiExchange


_FUTURE_YYMMDD = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%y%m%d")


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _future_ticker(series_ticker: str, suffix: str) -> str:
    return f"{series_ticker}-{_FUTURE_YYMMDD}-{suffix}"


def _raw_market(ticker: str, title: str, *, series_ticker: str, yes_ask: float = 0.56) -> dict:
    return {
        "ticker": ticker,
        "title": title,
        "series_ticker": series_ticker,
        "status": "open",
        "yes_ask": yes_ask,
        "no_ask": round(1 - yes_ask, 2),
        "yes_bid": round(max(0.0, yes_ask - 0.03), 2),
        "no_bid": round(min(1.0, 1 - yes_ask - 0.03), 2),
        "volume_fp": 1250,
        "liquidity": 5000,
        "event_ticker": f"{series_ticker}-EVENT",
        "subtitle": "Daily high temperature market",
        "close_time": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    }


class KalshiDirectMarketTests(unittest.TestCase):
    def _exchange(self) -> KalshiExchange:
        exchange = KalshiExchange("key-id", "/tmp/fake-key.pem", demo=True)
        exchange.client = SimpleNamespace(
            kalshi_auth=SimpleNamespace(create_auth_headers=lambda method, path: {"Authorization": "test"})
        )
        exchange.set_allowed_market_groups(["weather"])
        return exchange

    def test_configured_rate_limit_profile_overrides_account_limit_fetch(self):
        exchange = self._exchange()
        exchange.set_rate_limit_config(
            {
                "account_tier": "shared-runtime-throttle",
                "reads_per_second": 4.0,
                "writes_per_second": 2.0,
            }
        )

        with patch.object(exchange, "_fetch_account_limit_profile") as fetch_limits:
            exchange._refresh_rate_limit_profile()

        fetch_limits.assert_not_called()
        self.assertEqual(exchange._throttle.profile.account_tier, "shared-runtime-throttle")
        self.assertEqual(exchange._throttle.profile.reads_per_second, 4.0)
        self.assertEqual(exchange._throttle.profile.writes_per_second, 2.0)

    def test_get_markets_direct_queries_weather_series_before_generic_pagination(self):
        exchange = self._exchange()
        exchange._daily_series_tickers = ["KXHIGHNY", "KXBTC"]
        requested_urls: list[str] = []

        def fake_http_get(url, headers, throttle=None, timeout=0):
            requested_urls.append(url)
            if "series_ticker=KXHIGHNY" in url:
                return _FakeResponse(
                    200,
                    {
                        "markets": [
                            _raw_market(_future_ticker("KXHIGHNY", "T71"), "Will the high temperature in New York exceed 71 degrees?", series_ticker="KXHIGHNY"),
                            _raw_market(_future_ticker("KXHIGHNY", "T72"), "Will the high temperature in New York exceed 72 degrees?", series_ticker="KXHIGHNY"),
                        ]
                    },
                )
            raise AssertionError(f"unexpected URL: {url}")

        with patch("bot.exchanges.kalshi.http_get_with_retry", side_effect=fake_http_get):
            markets = exchange.get_markets_direct(limit=2, page_size=100, max_pages=2)

        self.assertEqual(
            requested_urls,
            [f"{exchange.host}/markets?status=open&limit=2&series_ticker=KXHIGHNY"],
        )
        self.assertEqual([market.id for market in markets], [_future_ticker("KXHIGHNY", "T71"), _future_ticker("KXHIGHNY", "T72")])
        self.assertTrue(all(m.metadata["source"] == "direct_series" for m in markets))
        self.assertTrue(all(m.metadata["series"] == "KXHIGHNY" for m in markets))
        self.assertTrue(all(m.metadata["market_group"] == "weather" for m in markets))

    def test_get_markets_direct_collects_weather_series_even_when_generic_pages_are_sports(self):
        exchange = self._exchange()
        exchange._daily_series_tickers = ["KXHIGHMIA"]
        requested_urls: list[str] = []

        def fake_http_get(url, headers, throttle=None, timeout=0):
            requested_urls.append(url)
            if "series_ticker=KXHIGHMIA" in url:
                return _FakeResponse(
                    200,
                    {
                        "markets": [
                            _raw_market(_future_ticker("KXHIGHMIA", "T88"), "Will the high temperature in Miami exceed 88 degrees?", series_ticker="KXHIGHMIA"),
                        ]
                    },
                )
            if "series_ticker=" not in url:
                return _FakeResponse(
                    200,
                    {
                        "markets": [
                            _raw_market("MVE-NBA-260506-COMBO", "Will the Knicks win tonight?", series_ticker="MVE"),
                            _raw_market("MVE-NBA-260506-COMBO-2", "Will the Lakers win tonight?", series_ticker="MVE"),
                        ]
                    },
                )
            raise AssertionError(f"unexpected URL: {url}")

        with patch("bot.exchanges.kalshi.http_get_with_retry", side_effect=fake_http_get):
            markets = exchange.get_markets_direct(limit=3, page_size=50, max_pages=1)

        self.assertGreaterEqual(len(requested_urls), 2)
        self.assertIn("series_ticker=KXHIGHMIA", requested_urls[0])
        self.assertEqual([market.id for market in markets], [_future_ticker("KXHIGHMIA", "T88")])
        self.assertEqual(markets[0].metadata["source"], "direct_series")
        self.assertTrue(all("MVE" not in market.id for market in markets))

    def test_get_markets_direct_dedupes_market_ids_across_series_and_generic_pages(self):
        exchange = self._exchange()
        exchange._daily_series_tickers = ["KXHIGHCHI"]

        def fake_http_get(url, headers, throttle=None, timeout=0):
            if "series_ticker=KXHIGHCHI" in url:
                return _FakeResponse(
                    200,
                    {
                        "markets": [
                            _raw_market(_future_ticker("KXHIGHCHI", "T67"), "Will the high temperature in Chicago exceed 67 degrees?", series_ticker="KXHIGHCHI"),
                        ]
                    },
                )
            if "series_ticker=" not in url:
                return _FakeResponse(
                    200,
                    {
                        "markets": [
                            _raw_market(_future_ticker("KXHIGHCHI", "T67"), "Will the high temperature in Chicago exceed 67 degrees?", series_ticker="KXHIGHCHI"),
                            _raw_market(_future_ticker("KXHIGHCHI", "T68"), "Will the high temperature in Chicago exceed 68 degrees?", series_ticker="KXHIGHCHI"),
                        ]
                    },
                )
            raise AssertionError(f"unexpected URL: {url}")

        with patch("bot.exchanges.kalshi.http_get_with_retry", side_effect=fake_http_get):
            markets = exchange.get_markets_direct(limit=2, page_size=50, max_pages=1)

        self.assertEqual([market.id for market in markets], [_future_ticker("KXHIGHCHI", "T67"), _future_ticker("KXHIGHCHI", "T68")])
        self.assertEqual(len({market.id for market in markets}), 2)

    def test_weather_series_filter_rejects_broad_wind_energy_series(self):
        self.assertFalse(KalshiExchange._is_weather_series_ticker("KXPRIMEENGCONSUMPTION-30-WIND"))
        self.assertTrue(KalshiExchange._is_weather_series_ticker("KXHIGHNY"))


if __name__ == "__main__":
    unittest.main()
