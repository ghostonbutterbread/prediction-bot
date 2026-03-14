#!/usr/bin/env python3
"""
Prediction Market Trading Bot — Multi-Exchange

Usage:
    python main.py demo              # Demo mode (Kalshi demo + paper trading)
    python main.py paper             # Single paper scan
    python main.py simulate [N] [s]  # Run N scans (every s seconds), audit trail
    python main.py audit [session]   # Review simulation results
    python main.py backtest [n] [m]  # Backtest on n markets
    python main.py live              # Live trading (real money!)
    python main.py status            # Show bot status
    python main.py markets           # List active markets
    python main.py news <query>      # Test news feed
"""

import sys
import os
import logging
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
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
            "news_weight": float(os.getenv("NEWS_WEIGHT", "0.20")),
            "social_weight": float(os.getenv("SOCIAL_WEIGHT", "0.15")),
            "enable_news": os.getenv("ENABLE_NEWS", "true").lower() == "true",
            "enable_social": os.getenv("ENABLE_SOCIAL", "true").lower() == "true",
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


def cmd_simulate(scans: int = 10, interval: int = 60):
    """Run simulation mode — paper trades with full audit trail."""
    from bot.runner import PredictionBot
    from bot.simulator import Simulator

    config = get_config()
    bot = PredictionBot(config)

    api_key = os.getenv("KALSHI_API_KEY_ID")
    private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key")
    demo = os.getenv("KALSHI_USE_DEMO", "true").lower() == "true"

    if not api_key:
        print("❌ Set KALSHI_API_KEY_ID in .env")
        return

    bot.add_kalshi(api_key, private_key_path, demo=demo)
    results = bot.connect_all()

    if not any(results.values()):
        print("❌ Connection failed")
        return

    sim = Simulator(config)

    print(f"\n🧪 Simulation Mode")
    print(f"   Balance: ${sim.starting_balance:.2f}")
    print(f"   Scans: {scans} (every {interval}s)")
    print(f"   Min edge: {sim.min_edge:.2%}")
    print(f"   Min confidence: {sim.min_confidence:.2%}")
    print(f"\n   Running...\n")

    exchange = list(bot.exchanges.values())[0]

    for i in range(scans):
        try:
            result = sim.scan(exchange)
            print(f"   Scan {i+1}/{scans}: {result['trades']} trades taken")
        except Exception as e:
            logger.error(f"Scan error: {e}")

        if i < scans - 1:
            import time
            time.sleep(interval)

    # Final report
    sim.print_report()

    # Save trades for review
    print(f"\n📁 Session saved to: data/sim_{sim.session_id}.json")
    print(f"   Review trades: python main.py audit {sim.session_id}")

    bot.close()


def cmd_audit(session_id: str = None):
    """Review simulation results."""
    from bot.simulator import Simulator
    from pathlib import Path
    import json

    data_dir = Path("data")

    if session_id:
        session_file = data_dir / f"sim_{session_id}.json"
        if not session_file.exists():
            print(f"❌ Session not found: {session_file}")
            return
    else:
        # Find latest session
        sessions = sorted(data_dir.glob("sim_*.json"), reverse=True)
        if not sessions:
            print("❌ No simulation sessions found")
            return
        session_file = sessions[0]

    with open(session_file) as f:
        data = json.load(f)

    report = data.get("report", {})

    print(f"\n{'='*60}")
    print(f"📊 Audit Report — {report.get('session', 'unknown')}")
    print(f"{'='*60}")

    if report.get("total_trades", 0) == 0:
        print("No trades in this session.")
        return

    print(f"""
Starting Balance:  ${report.get('starting_balance', 0):.2f}
Current Balance:   ${report.get('current_balance', 0):.2f}
P&L:               ${report.get('pnl', 0):+.2f} ({report.get('pnl_pct', 0):+.1f}%)

Total Trades:      {report.get('total_trades', 0)}
Scans Run:         {report.get('scans_run', 0)}

Avg Edge:          {report.get('avg_edge', 0):.2%}
Max Edge:          {report.get('max_edge', 0):.2%}
Avg Confidence:    {report.get('avg_confidence', 0):.2%}

Direction Breakdown:""")

    for direction, count in report.get("by_direction", {}).items():
        print(f"  {direction}: {count}")

    # Show individual trades
    trades = data.get("trades", [])
    if trades:
        print(f"\n{'─'*60}")
        print("Individual Trades:")
        print(f"{'─'*60}")

        for i, t in enumerate(trades, 1):
            print(f"\n  #{i}: {t.get('direction', '')}")
            print(f"     Market: {t.get('question', '')[:60]}")
            print(f"     Model: {t.get('model_probability', 0):.2%} vs Market: ${t.get('market_price', 0):.2f}")
            print(f"     Edge: {t.get('edge', 0):.2%} | Conf: {t.get('confidence', 0):.2%}")
            print(f"     Size: ${t.get('position_size', 0):.2f}")
            print(f"     Signals: {t.get('signals', {})}")

    print(f"\n{'='*60}\n")


def cmd_backtest(days: int = 7, limit: int = 30):
    """Run backtest against current markets."""
    from bot.backtest import Backtester

    config = get_config()
    api_key = os.getenv("KALSHI_API_KEY_ID")
    private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key")
    demo = os.getenv("KALSHI_USE_DEMO", "true").lower() == "true"

    if not api_key:
        print("❌ Set KALSHI_API_KEY_ID in .env")
        return

    from bot.exchanges.kalshi import KalshiExchange
    exchange = KalshiExchange(api_key, private_key_path, demo=demo)

    if not exchange.connect():
        print("❌ Connection failed")
        return

    bt = Backtester(config)
    print(f"📊 Running backtest on {limit} markets...")
    markets = exchange.get_markets(limit=limit)
    result = bt.run(markets)
    bt.print_report(result)

    # Save results
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    result_file = data_dir / f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(result_file, "w") as f:
        json.dump(asdict(result), f, indent=2)
    print(f"📁 Results saved to: {result_file}")

    exchange.close()


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
    elif cmd == "simulate":
        scans = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        interval = int(sys.argv[3]) if len(sys.argv) > 3 else 30
        cmd_simulate(scans, interval)
    elif cmd == "audit":
        session_id = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_audit(session_id)
    elif cmd == "backtest":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 30
        cmd_backtest(days, limit)
    elif cmd == "news":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else None
        cmd_news(query)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
