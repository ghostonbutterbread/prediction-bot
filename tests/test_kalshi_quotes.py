"""Synthetic public-API shapes; no recorded historical prices or live access."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from bot.exchanges.kalshi import KalshiExchange, _market_from_raw


def exchange_for_quotes():
    exchange = KalshiExchange("synthetic-key-id", "/nonexistent/synthetic-key.pem", demo=True)
    exchange.client = SimpleNamespace(kalshi_auth=SimpleNamespace(create_auth_headers=lambda *_: {}))
    return exchange


def quote_response(payload):
    return SimpleNamespace(json=lambda: payload, raise_for_status=lambda: None)


class KalshiQuoteTests(unittest.TestCase):
    def fetch(self, payload):
        with patch("bot.exchanges.kalshi.http_get_with_retry", return_value=quote_response(payload)):
            return exchange_for_quotes().get_market_bid_ask("SYNTHETIC")

    def test_nested_dollars_and_legacy_cents_preserve_four_independent_quotes(self):
        # Ask sum is deliberately > 1: NO ask must never be 1 - YES ask.
        for fields in (
            {"yes_bid_dollars": "0.4100", "yes_ask_dollars": "0.4300",
             "no_bid_dollars": "0.5700", "no_ask_dollars": "0.5900"},
            {"yes_bid": 41, "yes_ask": 43, "no_bid": 57, "no_ask": 59},
        ):
            for nested in (False, True):
                with self.subTest(fields=fields, nested=nested):
                    result = self.fetch({"market": fields} if nested else fields)
                    self.assertIsNotNone(result)
                    for key, expected in (("yes_bid", .41), ("yes_ask", .43),
                                          ("no_bid", .57), ("no_ask", .59)):
                        self.assertAlmostEqual(result["best_" + key], expected)
                    self.assertAlmostEqual(result["mid_yes"], .42)
                    self.assertAlmostEqual(result["spread"], .02)
                    self.assertAlmostEqual(result["spread_pct"], .02 / .42 * 100)

    def test_raw_listing_and_detail_quotes_use_same_units(self):
        for fields in (
            {"yes_ask_dollars": "0.4300", "no_ask_dollars": "0.5900",
             "yes_bid_dollars": "0.4100", "no_bid_dollars": "0.5700"},
            {"yes_ask": 43, "no_ask": 59, "yes_bid": 41, "no_bid": 57},
        ):
            with self.subTest(fields=fields):
                market = _market_from_raw({"ticker": "SYNTHETIC", **fields})
                self.assertEqual((market.yes_price, market.no_price, market.yes_bid, market.no_bid),
                                 (.43, .59, .41, .57))
                self.assertEqual(market.metadata["recorded_book_asks"],
                                 {"best_yes_ask": .43, "best_no_ask": .59})

    def test_reference_fields_do_not_assert_recorded_asks(self):
        market = _market_from_raw({"ticker": "SYNTHETIC", "yes_price": .44,
                                   "no_price": .58, "no_ask_dollars": ".58"})
        self.assertEqual(market.metadata["recorded_book_asks"],
                         {"best_yes_ask": None, "best_no_ask": .58})

    def test_raw_ask_proof_cannot_be_overwritten_by_market_metadata(self):
        market = _market_from_raw(
            {"ticker": "SYNTHETIC", "yes_price": .44},
            metadata={"recorded_book_asks": {"best_yes_ask": .44}},
        )
        self.assertEqual(market.metadata["recorded_book_asks"],
                         {"best_yes_ask": None, "best_no_ask": None})

    def test_sdk_detail_malformed_no_ask_is_not_a_recorded_quote(self):
        market_payload = SimpleNamespace(
            ticker="SYNTHETIC", title="Synthetic market", yes_ask_dollars=".44",
            no_ask_dollars="NaN", yes_bid_dollars=".42", no_bid_dollars=None,
            volume_fp=100, liquidity_dollars=100, close_time=None,
        )
        with patch("bot.exchanges.kalshi.call_with_retry",
                   return_value=SimpleNamespace(market=market_payload)):
            market = exchange_for_quotes().get_market("SYNTHETIC")
        self.assertIsNotNone(market)
        self.assertEqual(market.metadata["recorded_book_asks"],
                         {"best_yes_ask": .44, "best_no_ask": None})
        self.assertEqual(market.no_price, 0.0)

    def test_sdk_detail_preserves_both_rule_fields_with_raw_detail_parity(self):
        rules = {
            "rules_primary": "The minimum temperature at CLIPHX on September 24, 2026 is reported by The Weather Company.",
            "rules_secondary": "Forecasts according to NWS are indicative only.",
        }
        payload = {
            "ticker": "KXLOWTPHX-26SEP24-T73", "title": "Will the low in Phoenix be below 73°F?",
            "series_ticker": "KXLOWTPHX", "yes_ask_dollars": ".44",
            "no_ask_dollars": ".58", "status": "open", "result": None,
            **rules,
        }
        exchange = exchange_for_quotes()
        exchange.client.get_market = lambda **_kwargs: SimpleNamespace(market=SimpleNamespace(**payload))
        with patch("bot.exchanges.kalshi.call_with_retry", side_effect=lambda fn, **_kwargs: fn()), \
             patch.object(exchange, "_fetch_market_raw") as raw_fetch:
            sdk_market = exchange.get_market(payload["ticker"])
        raw_fetch.assert_not_called()
        raw_market = _market_from_raw(payload)
        self.assertEqual({key: sdk_market.metadata[key] for key in rules}, rules)
        self.assertEqual({key: raw_market.metadata[key] for key in rules}, rules)

    def test_sdk_detail_without_rules_does_not_invent_rule_fields(self):
        payload = SimpleNamespace(ticker="KXLOWTPHX-26AUG01-T85", title="August low",
                                  series_ticker="KXLOWTPHX", yes_ask_dollars=".44")
        exchange = exchange_for_quotes()
        exchange.client.get_market = lambda **_kwargs: SimpleNamespace(market=payload)
        with patch("bot.exchanges.kalshi.call_with_retry", side_effect=lambda fn, **_kwargs: fn()), \
             patch.object(exchange, "_fetch_market_raw") as raw_fetch:
            market = exchange.get_market(payload.ticker)
        raw_fetch.assert_not_called()
        self.assertNotIn("rules_primary", market.metadata)
        self.assertNotIn("rules_secondary", market.metadata)

    def test_sdk_events_malformed_no_ask_is_not_a_recorded_quote(self):
        exchange = exchange_for_quotes()
        close_time = datetime.now(timezone.utc) + timedelta(days=2)
        market_payload = SimpleNamespace(
            ticker="SYNTHETIC", title="Synthetic weather market", yes_ask_dollars=".44",
            no_ask_dollars="NaN", volume_fp=100, liquidity_dollars=100,
            close_time=close_time, status="open",
        )
        exchange.client.get_events = lambda **kwargs: SimpleNamespace(
            events=[SimpleNamespace(event_ticker="SYNTHETIC", category="weather")], cursor=None,
        )
        exchange.client.get_markets = lambda **kwargs: SimpleNamespace(markets=[market_payload])
        with patch("bot.exchanges.kalshi.http_get_with_retry",
                   return_value=SimpleNamespace(status_code=503)), \
             patch("bot.exchanges.kalshi.call_with_retry", side_effect=lambda fn, **kwargs: fn()), \
             patch.object(exchange, "_market_allowed", return_value=True):
            markets = exchange.get_markets(limit=1)
        self.assertEqual(len(markets), 1)
        self.assertEqual(markets[0].metadata["recorded_book_asks"],
                         {"best_yes_ask": .44, "best_no_ask": None})

    def test_collector_persists_normalized_quotes_even_for_skips(self):
        import tempfile
        from datetime import datetime, timedelta, timezone

        from bot.decision_pipeline import DecisionPipelineEvaluator
        from bot.file_ops import load_jsonl
        from bot.prediction_lab import PredictionLab
        from bot.strategies.enhanced import StrategyTrace

        for fields, yes_ask, no_ask in (
            ({"yes_bid_dollars": "0.41", "yes_ask_dollars": "0.43",
              "no_bid_dollars": "0.57", "no_ask_dollars": "0.59"}, .43, .59),
            ({"yes_bid": 41, "yes_ask": 43, "no_bid": 57, "no_ask": 59}, .43, .59),
            ({"no_ask_dollars": ".59"}, 0, .59),
            ({"yes_price": .12, "no_price": .88}, 0, 0),
        ):
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as tmp:
                lab = PredictionLab({
                    "data_dir": tmp,
                    "scan": {"allowed_market_routes": ["weather.daily_temperature"]},
                    "prediction_lab": {"enabled": True, "mode": "collector", "groups": ["weather"],
                                       "collector_fetch_mode": "direct_markets", "score_only": False,
                                       "record_all_scored": True, "collector_record_predictions": True,
                                       "use_shared_pipeline": True},
                    "strategy": {"enable_news": False, "enable_social": False, "enable_ai": False},
                })
                signal = {"market_id": "KXHIGHNY-SYNTHETIC-T71", "exchange": "kalshi",
                          "direction": "BUY_NO", "model_probability": .2, "confidence": 0,
                          "edge": .1, "market_price": .88, "yes_market_price": .12,
                          "no_market_price": .88, "signals": {"synthetic": .2}}
                strategy = SimpleNamespace(analyze_market_with_trace=lambda *args, **kwargs: (
                    dict(signal), StrategyTrace(raw_signals={}, accepted_signals={}, rejected_signals={},
                                               ensemble_signal=dict(signal))))
                lab.decision_evaluator = DecisionPipelineEvaluator(lab.config, strategy=strategy)
                market = SimpleNamespace(
                    id="KXHIGHNY-SYNTHETIC-T71", exchange="kalshi",
                    question="Will the high temperature in New York exceed 71 degrees?",
                    category="KXHIGHNY", yes_price=.12, no_price=.88, volume=1000,
                    closes_at=datetime.now(timezone.utc) + timedelta(hours=6),
                    metadata={"market_group": "weather", "market_family": "daily_temperature",
                              "series_ticker": "KXHIGHNY", "series": "daily_temperature"})
                exchange = exchange_for_quotes()
                with patch.object(exchange, "get_markets_direct", return_value=[market]), \
                     patch("bot.exchanges.kalshi.http_get_with_retry", return_value=quote_response({"market": fields})):
                    lab.run(exchange)
                snapshots = load_jsonl(lab.market_snapshots_path)
                self.assertEqual(len(snapshots), 1)
                artifact = snapshots[0]["decision_artifact"]
                self.assertEqual(artifact["final_action"], "SKIP")
                self.assertEqual(artifact["execution_snapshot"]["direction"], "BUY_NO")
                self.assertEqual(artifact["execution_snapshot"]["estimated_fill_price"], no_ask)
                for book in (artifact["order_book"], artifact["order_book_snapshot"]["data"],
                             artifact["execution_snapshot"]):
                    self.assertEqual(book["best_yes_ask"], yes_ask)
                    self.assertEqual(book["best_no_ask"], no_ask)

    def test_invalid_quote_is_missing_without_discarding_the_other_side(self):
        for key in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
            for suffix, invalids in (("_dollars", (True, False, "NaN", "Infinity", -0.01, 1.01, "oops", [], {})),
                                     ("", (True, False, "NaN", "Infinity", -1, 101, "oops", [], {}))):
                for value in invalids:
                    with self.subTest(key=key, suffix=suffix, value=value):
                        result = self.fetch({key + suffix: value, "no_ask_dollars": "0.59"}
                                            if key != "no_ask" else {key + suffix: value, "yes_ask_dollars": "0.43"})
                        self.assertEqual(result["best_" + key], 0.0)
                        other = "yes_ask" if key == "no_ask" else "no_ask"
                        self.assertEqual(result["best_" + other], .43 if other == "yes_ask" else .59)

    def test_dollar_precedence_is_not_truthiness_or_magnitude_based(self):
        for dollars, expected in (("0.4350", .435), (0, 0), ("0", 0), ("bad", 0), (None, .43)):
            with self.subTest(dollars=dollars):
                result = self.fetch({"yes_ask_dollars": dollars, "yes_ask": 43})
                self.assertEqual(result["best_yes_ask"], expected)
        self.assertEqual(self.fetch({"yes_ask": 1})["best_yes_ask"], .01)
        self.assertEqual(self.fetch({"yes_ask_dollars": 1})["best_yes_ask"], 1)
        self.assertEqual(self.fetch({"yes_ask": 100})["best_yes_ask"], 1)

    def test_missing_sides_and_reference_prices_are_not_executable_quotes(self):
        for payload in ({}, {"yes_price": .4, "no_price": .6, "last_price_dollars": ".4"},
                        {"yes_bid_dollars": ".41", "no_bid_dollars": ".57"}):
            result = self.fetch(payload)
            self.assertEqual(result["best_yes_ask"], 0)
            self.assertEqual(result["best_no_ask"], 0)
            self.assertEqual(result["mid_yes"], 0)
        result = self.fetch({"no_ask_dollars": ".59"})
        self.assertEqual(result["best_no_ask"], .59)
        self.assertEqual(result["best_yes_ask"], 0)
        result = self.fetch({"yes_ask_dollars": ".43", "yes_bid_dollars": None})
        self.assertEqual(result["best_no_ask"], 0)
        self.assertEqual(result["mid_yes"], 0)
        self.assertEqual(result["spread"], 0)
        self.assertEqual(result["spread_pct"], 0)

    def test_crossed_quotes_are_retained_not_repaired_into_fictitious_prices(self):
        result = self.fetch({"yes_bid": 45, "yes_ask": 43, "no_bid": 57, "no_ask": 59})
        self.assertEqual(result["best_yes_bid"], .45)
        self.assertEqual(result["best_yes_ask"], .43)
        self.assertAlmostEqual(result["spread"], -.02)

    def test_invalid_response_envelopes_and_http_failure_return_none(self):
        for payload in ([], None, "oops", {"market": []}, {"market": None}):
            with self.subTest(payload=payload):
                self.assertIsNone(self.fetch(payload))
        with patch("bot.exchanges.kalshi.http_get_with_retry", return_value=None):
            self.assertIsNone(exchange_for_quotes().get_market_bid_ask("SYNTHETIC"))
        response = SimpleNamespace(raise_for_status=lambda: (_ for _ in ()).throw(RuntimeError("HTTP failure")))
        with patch("bot.exchanges.kalshi.http_get_with_retry", return_value=response):
            self.assertIsNone(exchange_for_quotes().get_market_bid_ask("SYNTHETIC"))


if __name__ == "__main__":
    unittest.main()
