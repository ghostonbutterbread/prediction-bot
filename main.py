#!/usr/bin/env python3
"""
Prediction Market Trading Bot — Multi-Exchange

Usage:
    python main.py demo           # Demo mode (Kalshi demo + paper trading)
    python main.py paper          # Paper trading on live markets
    python main.py live           # Live trading (real money!)
    python main.py status         # Show bot status
    python main.py markets        # List active markets
    python main.py news <query>   # Test news feed
"""

import sys
import os
import logging
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("prediction-bot")


def get_config():
    return {
        "strategy": {
            "min_edge": float(os.getenv("MIN_EDGE", "0.05")),
            "min_confidence": float(os.getenv("MIN_CONFIDENCE", "0.50")),
            "news_weight": float(os.getenv("NEWS_WEIGHT", "0.30")),
            "enable_news": os.getenv("ENABLE_NEWS", "true").lower() == "true",
        },
        "kelly_fraction": float(os.getenv("KELLY_FRACTION", "0.5")),
        "max_position_pct": float(os.getenv("MAX_POSITION_PCT", "0.10")),
        "log_dir": os.getenv("LOG_DIR", "data"),
    }


def cmd_demo():
    """Demo mode — Kalshi demo account, paper trading."""
    from bot.runner import PredictionBot

    config = get_config()
    bot = PredictionBot(config)

    # Add Kalshi demo
    api_key = os.getenv("KALSHI_API_KEY_ID")
    private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key")

    if not api_key:
        print("❌ Set KALSHI_API_KEY_ID in .env")
        print("   Get demo keys at: https://demo.kalshi.co")
        return

    bot.add_kalshi(api_key, private_key_path, demo=True)
    results = bot.connect_all()

    print(f"\n🔌 Connection results: {results}")

    if any(results.values()):
        # Single scan
        result = bot.scan_once()
        print(f"\n📊 Scan result: {result}")

    bot.close()


def cmd_paper():
    """Paper trading on live markets."""
    from bot.runner import PredictionBot

    config = get_config()
    bot = PredictionBot(config)

    api_key = os.getenv("KALSHI_API_KEY_ID")
    private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key")

    if not api_key:
        print("❌ Set KALSHI_API_KEY_ID in .env")
        return

    bot.add_kalshi(api_key, private_key_path, demo=False)
    bot.connect_all()

    print("📝 Paper trading mode — analyzing live markets, no orders placed")
    result = bot.scan_once()
    print(f"\n📊 Result: {result}")

    bot.close()


def cmd_live(interval: int = 120):
    """Live trading mode."""
    from bot.runner import PredictionBot

    config = get_config()
    bot = PredictionBot(config)

    api_key = os.getenv("KALSHI_API_KEY_ID")
    private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key")

    if not api_key:
        print("❌ Set KALSHI_API_KEY_ID in .env")
        return

    bot.add_kalshi(api_key, private_key_path, demo=False)
    bot.connect_all()

    print(f"🔴 LIVE TRADING — scanning every {interval}s")
    print("   Press Ctrl+C to stop\n")

    try:
        bot.run_loop(interval_seconds=interval)
    except KeyboardInterrupt:
        bot.stop()
    finally:
        bot.close()


def cmd_markets():
    """List active markets."""
    from bot.runner import PredictionBot

    config = get_config()
    bot = PredictionBot(config)

    api_key = os.getenv("KALSHI_API_KEY_ID")
    private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key")

    if not api_key:
        print("❌ Set KALSHI_API_KEY_ID in .env")
        return

    bot.add_kalshi(api_key, private_key_path, demo=True)
    bot.connect_all()

    print("\n📊 Active Kalshi Markets:\n")
    for name, exchange in bot.exchanges.items():
        markets = exchange.get_markets(limit=20)
        for i, m in enumerate(markets, 1):
            print(f"{i}. {m.question}")
            print(f"   YES: ${m.yes_price:.2f} | NO: ${m.no_price:.2f} | Vol: ${m.volume:,.0f}")
            print(f"   ID: {m.id}")
            if m.closes_at:
                print(f"   Closes: {m.closes_at.strftime('%Y-%m-%d %H:%M UTC')}")
            print()

    bot.close()


def cmd_news(query: str = None):
    """Test news feed."""
    from bot.feeds.news import NewsFeed

    if not query:
        query = "Bitcoin price"

    feed = NewsFeed()
    print(f"\n📰 News for: '{query}'\n")

    items = feed.get_news_for_market(query)
    for item in items:
        sentiment = "📈" if item.sentiment > 0 else "📉" if item.sentiment < 0 else "➡️"
        print(f"{sentiment} [{item.source}] {item.title}")
        print(f"   Relevance: {item.relevance:.2f} | Sentiment: {item.sentiment:+.2f}")
        print()

    feed.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1].lower()

    if cmd == "demo":
        cmd_demo()
    elif cmd == "paper":
        cmd_paper()
    elif cmd == "live":
        interval = int(sys.argv[2]) if len(sys.argv) > 2 else 120
        cmd_live(interval)
    elif cmd == "markets":
        cmd_markets()
    elif cmd == "news":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else None
        cmd_news(query)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
