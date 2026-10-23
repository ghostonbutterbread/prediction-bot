import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

from bot.exchanges.kalshi import DirectMarketFetchUnavailable, KalshiExchange


_FUTURE_YYMMDD = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%y%m%d")


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | list):
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
        # Synthetic modern API fields: dollar amounts must carry the suffix.
        "yes_ask_dollars": yes_ask,
        "no_ask_dollars": round(1 - yes_ask, 2),
        "yes_bid_dollars": round(max(0.0, yes_ask - 0.03), 2),
        "no_bid_dollars": round(min(1.0, 1 - yes_ask - 0.03), 2),
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
        self.assertTrue(all((m.yes_price, m.no_price, m.yes_bid, m.no_bid) ==
                            (0.56, 0.44, 0.53, 0.41) for m in markets))

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

    def test_get_markets_direct_raises_when_retries_exhaust_without_response(self):
        exchange = self._exchange()

        with patch("bot.exchanges.kalshi.http_get_with_retry", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "direct market pull unavailable after retries"):
                exchange.get_markets_direct(limit=1, page_size=50, max_pages=1)

    def test_get_markets_direct_returns_empty_list_for_a_successful_empty_response(self):
        exchange = self._exchange()

        with patch("bot.exchanges.kalshi.http_get_with_retry", return_value=_FakeResponse(200, {"markets": []})):
            markets = exchange.get_markets_direct(limit=1, page_size=50, max_pages=1)

        self.assertEqual(markets, [])

    def test_series_pagination_exhausts_more_than_ten_pages_below_global_limit(self):
        exchange = self._exchange()
        exchange._daily_series_tickers = ["KXHIGHNY"]
        rows = [
            _raw_market(_future_ticker("KXHIGHNY", f"T{degree}"),
                        f"Will the high temperature in New York exceed {degree} degrees?",
                        series_ticker="KXHIGHNY")
            for degree in range(60, 87)
        ]
        queries = []

        def fake_http_get(url, headers, throttle=None, timeout=0):
            query = parse_qs(urlsplit(url).query)
            queries.append(query)
            if "series_ticker" not in query:
                return _FakeResponse(200, {"markets": []})
            offset = int(query.get("cursor", ["0"])[0])
            size = int(query["limit"][0])
            stop = offset + size
            return _FakeResponse(200, {"markets": rows[offset:stop],
                                       "cursor": str(stop) if stop < len(rows) else ""})

        with patch("bot.exchanges.kalshi.http_get_with_retry", side_effect=fake_http_get):
            markets = exchange.get_markets_direct(limit=100, page_size=2, max_pages=1)

        self.assertEqual({market.id for market in markets}, {row["ticker"] for row in rows})
        series_queries = [q for q in queries if "series_ticker" in q]
        self.assertEqual(len(series_queries), 14)
        self.assertEqual([q.get("cursor", ["0"])[0] for q in series_queries],
                         [str(offset) for offset in range(0, 27, 2)])
        self.assertEqual(len(queries), 15)  # Generic fallback still has its own bound.
        self.assertTrue(all(q["limit"] == ["2"] for q in queries))

    def test_rejected_and_duplicate_rows_do_not_consume_global_quota(self):
        for series_pass in (True, False):
            with self.subTest(series_pass=series_pass):
                exchange = self._exchange()
                exchange._daily_series_tickers = ["KXHIGHNY", "KXHIGHMIA"] if series_pass else []

                def row(suffix):
                    return _raw_market(_future_ticker("KXHIGHNY", suffix),
                                       "Will the high temperature in New York exceed 72 degrees?",
                                       series_ticker="KXHIGHNY")

                closed = {**row("CLOSED"), "close_time": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}
                missing_close = {**row("MISSING"), "close_time": None}
                stale_date = (datetime.now(timezone.utc) - timedelta(days=4)).strftime("%y%m%d")
                stale = {**row("STALE"), "ticker": f"KXHIGHNY-{stale_date}-T71"}
                no_quote = {**row("NOQUOTE"), "yes_ask_dollars": 0}
                wrong_group = _raw_market("MVE-NBA-COMBO", "Will the Knicks win tonight?", series_ticker="MVE")
                first, second = row("T72"), row("T73")
                pages = [[closed, missing_close], [stale, no_quote], [wrong_group, None],
                         [first, first], [first, second]]
                queries = []

                def fake_http_get(url, headers, throttle=None, timeout=0):
                    query = parse_qs(urlsplit(url).query)
                    queries.append(query)
                    offset = int(query.get("cursor", ["0"])[0])
                    self.assertLess(offset, len(pages), "requested another page after accepted quota")
                    if series_pass:
                        self.assertEqual(query.get("series_ticker"), ["KXHIGHNY"])
                    return _FakeResponse(200, {"markets": pages[offset], "cursor": str(offset + 1)})

                with patch("bot.exchanges.kalshi.http_get_with_retry", side_effect=fake_http_get):
                    markets = exchange.get_markets_direct(limit=2, page_size=2, max_pages=10)

                self.assertEqual([market.id for market in markets], [first["ticker"], second["ticker"]])
                self.assertEqual(len(queries), 5)

    def test_series_pagination_failure_reports_incomplete_instead_of_partial_success(self):
        invalid_json = _FakeResponse(200, {})
        failures = [None, _FakeResponse(503, {}), _FakeResponse(200, []),
                    _FakeResponse(200, {"markets": {}}),
                    _FakeResponse(200, {"markets": [], "cursor": {"bad": "cursor"}}), invalid_json,
                    TimeoutError("transport failed")]
        for failure in failures:
            for after_first_page in (False, True):
                with self.subTest(failure=failure, after_first_page=after_first_page):
                    exchange = self._exchange()
                    exchange._daily_series_tickers = ["KXHIGHNY"]
                    first = _raw_market(_future_ticker("KXHIGHNY", "T72"),
                                        "Will the high temperature in New York exceed 72 degrees?",
                                        series_ticker="KXHIGHNY")
                    responses = [_FakeResponse(200, {"markets": [first], "cursor": "next"})] if after_first_page else []
                    responses += [failure, _FakeResponse(200, {"markets": []})]
                    page, fetched = (2, 1) if after_first_page else (1, 0)
                    diagnostic = rf"incomplete.*series=KXHIGHNY.*page={page}.*fetched={fetched}"
                    with patch.object(invalid_json, "json", side_effect=ValueError("bad json")), \
                         patch("bot.exchanges.kalshi.http_get_with_retry", side_effect=responses) as fetch, \
                         self.assertLogs("bot.exchanges.kalshi", level="WARNING") as logs:
                        with self.assertRaisesRegex(DirectMarketFetchUnavailable, diagnostic) as error:
                            exchange.get_markets_direct(limit=10)
                    self.assertEqual(fetch.call_count, page)
                    self.assertIsNotNone(error.exception.__cause__)
                    self.assertIn(f"accepted={int(after_first_page)}", logs.output[0])

    def test_series_pagination_rejects_repeated_and_cycling_cursors(self):
        for cursors in (["same", "same"], ["a", "b", "a"]):
            with self.subTest(cursors=cursors):
                exchange = self._exchange()
                exchange._daily_series_tickers = ["KXHIGHNY"]
                responses = [_FakeResponse(200, {"markets": [], "cursor": cursor}) for cursor in cursors]
                responses += [_FakeResponse(200, {"markets": []})]
                with patch("bot.exchanges.kalshi.http_get_with_retry", side_effect=responses) as fetch, \
                     self.assertLogs("bot.exchanges.kalshi", level="WARNING"):
                    with self.assertRaisesRegex(DirectMarketFetchUnavailable, "incomplete.*nonadvancing cursor"):
                        exchange.get_markets_direct(limit=10, max_pages=0)
                self.assertEqual(fetch.call_count, len(cursors))

    def test_series_empty_page_and_opaque_cursor_preserve_cross_pass_deduplication(self):
        exchange = self._exchange()
        exchange._daily_series_tickers = ["KXHIGHNY", "KXHIGHMIA"]
        opaque = "opaque +/=&?%#"
        rows = [
            _raw_market(_future_ticker("KXHIGHNY", f"T{degree}"),
                        f"Will the high temperature in New York exceed {degree} degrees?",
                        series_ticker="KXHIGHNY")
            for degree in range(70, 74)
        ]
        queries = []

        def fake_http_get(url, headers, throttle=None, timeout=0):
            query = parse_qs(urlsplit(url).query)
            queries.append(query)
            series = query.get("series_ticker", [None])[0]
            cursor = query.get("cursor", [None])[0]
            if series == "KXHIGHNY" and cursor is None:
                return _FakeResponse(200, {"markets": [], "cursor": opaque})
            if series == "KXHIGHNY":
                self.assertEqual(cursor, opaque)
                return _FakeResponse(200, {"markets": [rows[0], rows[0]]})
            if series == "KXHIGHMIA":
                return _FakeResponse(200, {"markets": [rows[0], rows[1]]})
            if cursor is None:
                return _FakeResponse(200, {"markets": [rows[1], rows[2]], "cursor": "generic-next"})
            self.assertEqual(cursor, "generic-next")
            return _FakeResponse(200, {"markets": [rows[2], rows[3]]})

        with patch("bot.exchanges.kalshi.http_get_with_retry", side_effect=fake_http_get):
            markets = exchange.get_markets_direct(limit=10, page_size=2, max_pages=2)

        self.assertEqual([market.id for market in markets], [row["ticker"] for row in rows])
        self.assertEqual(len(queries), 5)
        self.assertEqual([market.metadata["source"] for market in markets],
                         ["direct_series", "direct_series", "direct_paginated", "direct_paginated"])

    def test_series_continuation_retries_same_page_without_duplicate_results(self):
        exchange = self._exchange()
        exchange._daily_series_tickers = ["KXHIGHNY"]
        first, second = [
            _raw_market(_future_ticker("KXHIGHNY", f"T{degree}"),
                        f"Will the high temperature in New York exceed {degree} degrees?",
                        series_ticker="KXHIGHNY")
            for degree in (71, 72)
        ]
        responses = [httpx.Response(200, json={"markets": [first], "cursor": "next"}),
                     httpx.Response(429, headers={"Retry-After": "0"}),
                     httpx.ReadTimeout("synthetic timeout"),
                     httpx.Response(200, json={"markets": [first, second], "cursor": "unused"})]
        with patch("bot.http_rate_limit.httpx.get", side_effect=responses) as fetch, \
             patch("bot.http_rate_limit.time.sleep") as sleep, \
             patch.object(exchange._throttle, "wait") as throttle:
            markets = exchange.get_markets_direct(limit=2, page_size=2)

        self.assertEqual([market.id for market in markets], [first["ticker"], second["ticker"]])
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual(throttle.call_count, 4)
        self.assertEqual(sleep.call_count, 2)
        urls = [call.args[0] for call in fetch.call_args_list]
        self.assertEqual(urls[1:], [urls[1]] * 3)
        self.assertEqual(parse_qs(urlsplit(urls[1]).query)["cursor"], ["next"])

    def test_series_continuation_transport_retry_exhaustion_is_not_partial_success(self):
        exchange = self._exchange()
        exchange._daily_series_tickers = ["KXHIGHNY"]
        first = _raw_market(_future_ticker("KXHIGHNY", "T71"),
                            "Will the high temperature in New York exceed 71 degrees?", series_ticker="KXHIGHNY")
        responses = [httpx.Response(200, json={"markets": [first], "cursor": "next"})]
        responses += [httpx.ReadTimeout("synthetic timeout") for _ in range(3)]
        with patch("bot.http_rate_limit.httpx.get", side_effect=responses) as fetch, \
             patch("bot.http_rate_limit.time.sleep"), patch.object(exchange._throttle, "wait"), \
             self.assertLogs("bot.exchanges.kalshi", level="WARNING") as logs:
            with self.assertRaisesRegex(DirectMarketFetchUnavailable, "page=2.*fetched=1.*unavailable after retries"):
                exchange.get_markets_direct(limit=2)
        self.assertEqual(fetch.call_count, 4)
        self.assertIn("accepted=1", logs.output[0])

    def test_generic_pagination_keeps_its_explicit_page_bound(self):
        exchange = self._exchange()
        row = _raw_market(_future_ticker("KXHIGHNY", "T71"),
                          "Will the high temperature in New York exceed 71 degrees?", series_ticker="KXHIGHNY")
        with patch("bot.exchanges.kalshi.http_get_with_retry",
                   return_value=_FakeResponse(200, {"markets": [row], "cursor": "still-more"})) as fetch:
            markets = exchange.get_markets_direct(limit=100, max_pages=2)
        self.assertEqual(len(markets), 1)
        self.assertEqual(fetch.call_count, 2)

    def test_weather_series_filter_rejects_broad_wind_energy_series(self):
        self.assertFalse(KalshiExchange._is_weather_series_ticker("KXPRIMEENGCONSUMPTION-30-WIND"))
        self.assertTrue(KalshiExchange._is_weather_series_ticker("KXHIGHNY"))


if __name__ == "__main__":
    unittest.main()
