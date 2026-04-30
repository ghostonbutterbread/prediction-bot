from __future__ import annotations

from datetime import datetime, timezone

from bot.archive_exchange import HistoricalKalshiArchiveExchange


def test_cents_to_unit_accepts_cents_and_unit_values():
    assert HistoricalKalshiArchiveExchange._cents_to_unit(37) == 0.37
    assert HistoricalKalshiArchiveExchange._cents_to_unit(0.42) == 0.42
    assert HistoricalKalshiArchiveExchange._cents_to_unit(0) is None
    assert HistoricalKalshiArchiveExchange._cents_to_unit(None) is None


def test_market_from_row_maps_archive_prices_and_outcome():
    exchange = HistoricalKalshiArchiveExchange(groups=["weather"])
    market = exchange._market_from_row(
        {
            "ticker": "KXHIGHNY-26APR29-T70",
            "event_ticker": "KXHIGHNY-26APR29",
            "market_type": "binary",
            "title": "Will NYC high temperature be above 70?",
            "status": "finalized",
            "yes_bid": 35,
            "yes_ask": 37,
            "no_bid": 62,
            "no_ask": 64,
            "last_price": 36,
            "volume": 123,
            "result": "yes",
            "created_time": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "open_time": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "close_time": datetime(2026, 4, 29, tzinfo=timezone.utc),
            "_fetched_at": datetime(2026, 4, 28, tzinfo=timezone.utc),
        }
    )

    assert market is not None
    assert market.id == "KXHIGHNY-26APR29-T70"
    assert market.exchange == "kalshi_archive"
    assert market.yes_price == 0.37
    assert market.no_price == 0.64
    assert market.close_price == 1.0
    assert market.metadata["market_group"] == "weather"
    assert market.metadata["source"] == "prediction_market_analysis_archive"


def test_get_order_book_uses_cached_raw_snapshot():
    exchange = HistoricalKalshiArchiveExchange()
    exchange._last_raw_by_ticker["TICKER"] = {
        "ticker": "TICKER",
        "yes_ask": 51,
        "yes_bid": 49,
        "no_ask": 52,
        "no_bid": 48,
    }

    book = exchange.get_order_book("TICKER")

    assert book["best_yes_ask"] == 0.51
    assert book["best_yes_bid"] == 0.49
    assert book["mid_yes"] == 0.5
