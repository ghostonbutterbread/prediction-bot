#!/usr/bin/env python3
"""
Prediction Market Trading Bot — Multi-Exchange

Usage:
    python main.py demo              # Demo mode (Kalshi demo + paper trading)
    python main.py paper             # Single simulator-backed paper scan
    python main.py simulate [N] [s]  # Run N scans (every s seconds), audit trail
    python main.py audit [session]   # Review simulation results
    python main.py resolve [session] # Resolve open trades — check outcomes, compute P&L
    python main.py backtest [n] [m]  # Backtest on n markets
    python main.py live              # Live trading (real money!)
    python main.py live --config path/to/config.yaml
    python main.py status            # Show bot status
    python main.py markets           # List active markets
    python main.py prediction-lab-run       # Score hypothetical Prediction Lab positions
    python main.py prediction-lab-resolve   # Resolve matured Prediction Lab positions
    python main.py prediction-lab-report    # Show Prediction Lab summary
    python main.py news <query>      # Test news feed
"""

import sys
import os
import logging
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

from bot.status import format_status_message

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("prediction-bot")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def get_config():
    trading_enabled = _env_bool("TRADING_ENABLED", True)
    max_tradable_balance = _env_float("MAX_TRADABLE_BALANCE_USD", 0.0)
    max_position_size_usd = _env_float("MAX_POSITION_SIZE_USD", 0.0)
    mode = os.getenv("TRADING_MODE", "paper")

    return {
        "strategy": {
            "min_edge": float(os.getenv("MIN_EDGE", "0.05")),
            "min_confidence": float(os.getenv("MIN_CONFIDENCE", "0.50")),
            "news_weight": float(os.getenv("NEWS_WEIGHT", "0.15")),
            "social_weight": float(os.getenv("SOCIAL_WEIGHT", "0.10")),
            "ai_weight": float(os.getenv("AI_WEIGHT", "0.20")),
            "enable_news": _env_bool("ENABLE_NEWS", True),
            "enable_social": _env_bool("ENABLE_SOCIAL", True),
            "enable_ai": _env_bool("ENABLE_AI", True),
        },
        "kelly_fraction": float(os.getenv("KELLY_FRACTION", "0.5")),
        "max_position_pct": float(os.getenv("MAX_POSITION_PCT", "0.10")),
        "max_tradable_balance_usd": max_tradable_balance,
        "max_position_size_usd": max_position_size_usd,
        "trading_enabled": trading_enabled,
        "trading": {
            "mode": mode,
            "enabled": trading_enabled,
            "trading_enabled": trading_enabled,
        },
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


def cmd_paper(config_path: str = "config.yaml"):
    """Paper trading on live markets using simulator-backed paper state only."""
    from bot.runner import PredictionBot
    from bot.simulator import Simulator

    try:
        from bot.config import load_config as load_yaml_config
        config = load_yaml_config(config_path)
        config["_config_path"] = config_path
    except Exception:
        config = get_config()

    config.setdefault("trading", {})["mode"] = "paper"
    if "enabled" not in config.setdefault("trading", {}):
        config["trading"]["enabled"] = config.get("trading_enabled", True)
    os.environ["PAPER_MODE"] = "true"

    bot = PredictionBot(config)

    api_key = os.getenv("KALSHI_API_KEY_ID")
    private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key")

    if not api_key:
        print("❌ Set KALSHI_API_KEY_ID in .env")
        return

    bot.add_kalshi(api_key, private_key_path, demo=False)
    results = bot.connect_all()

    if not any(results.values()):
        print("❌ Connection failed")
        return

    sim = Simulator(config)
    exchange = list(bot.exchanges.values())[0]

    print("📝 Paper trading mode, simulator-backed bankroll, live market data only")
    print(f"   Session: {sim.session_id}")
    print(f"   Balance: ${sim.balance:.2f}")

    result = sim.scan(exchange)
    print(f"\n📊 Result: {result}")
    print(f"📁 Session saved to: {config.get('data_dir', 'data')}/sim_{sim.session_id}.json")

    bot.close()


def cmd_live(interval: int = 120, single_trade: bool = False, verbosity_override: str | None = None, config_path: str = "config.yaml"):
    """Live trading mode."""
    from bot.runner import PredictionBot

    try:
        from bot.config import load_config as load_yaml_config
        config = load_yaml_config(config_path)
        config["_config_path"] = config_path
    except Exception:
        config = get_config()
    config.setdefault("trading", {})["mode"] = "live"
    if "enabled" not in config.setdefault("trading", {}):
        config["trading"]["enabled"] = config.get("trading_enabled", True)
    if single_trade:
        config["trading"]["single_trade_mode"] = True
    if verbosity_override:
        config.setdefault("verbosity", {})["level"] = verbosity_override
    os.environ["PAPER_MODE"] = "false"
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


def cmd_status():
    """Show current runner status using the shared formatter."""
    from bot.runner import PredictionBot

    try:
        from bot.config import load_config as load_yaml_config
        config = load_yaml_config("config.yaml")
        config["_config_path"] = "config.yaml"
    except Exception:
        config = get_config()
    mode = config.get("trading", {}).get("mode", os.getenv("TRADING_MODE", "paper")).lower()
    os.environ["PAPER_MODE"] = "false" if mode == "live" else "true"
    bot = PredictionBot(config)

    snapshot = bot.build_status_snapshot(reason="manual status")
    print(format_status_message(snapshot, reason="manual status"))


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


def cmd_simulate(scans: int = 10, interval: int = 60, use_scheduler: bool = True, single_trade: bool = False, verbosity_override: str | None = None, config_path: str = "config.yaml"):
    """Run simulation mode — paper trades with full audit trail.

    With --use-scheduler (default), scan interval adapts dynamically based
    on time-to-close of nearest market. The interval parameter becomes the
    fallback when scheduler is disabled.
    """
    from bot.runner import PredictionBot
    from bot.simulator import Simulator
    from bot.scheduler import ScanScheduler
    from bot.researcher import OpenRouterClient, FeedbackTracker

    # Load config (config.yaml + .env overrides)
    try:
        from bot.config import load_config as load_yaml_config
        config = load_yaml_config(config_path)
        config["_config_path"] = config_path
        logger.info("📋 Loaded config.yaml")
        if single_trade:
            config.setdefault("trading", {})["single_trade_mode"] = True
        if verbosity_override:
            config.setdefault("verbosity", {})["level"] = verbosity_override
    except Exception:
        config = get_config()
        logger.info("📋 Using .env config (config.yaml not available)")

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

    # Initialize scheduler for dynamic scan intervals
    scheduler = ScanScheduler(config) if use_scheduler else None

    # Initialize researcher (OpenRouter direct API)
    researcher = None
    feedback = None
    try:
        researcher = OpenRouterClient(config.get("openrouter", {}))
        feedback = FeedbackTracker(config.get("logging", {}).get("log_dir", "data"))
        if researcher.is_available():
            logger.info(f"🔬 Researcher active: {researcher.model} ({researcher.daily_budget} calls/day)")
        else:
            logger.info("🔬 Researcher standby (no API key or budget exhausted)")
    except Exception as e:
        logger.debug(f"Researcher init failed: {e}")

    logger.info("🏀 Sports mode enabled — quick-resolution markets filtered")

    print(f"\n🧪 Simulation Mode")
    print(f"   Balance: ${sim.starting_balance:.2f}")
    print(f"   Scans: {scans}")
    if scheduler:
        print(f"   Scheduler: dynamic (15s-300s based on time-to-close)")
    else:
        print(f"   Interval: fixed {interval}s")
    print(f"   Min edge: {sim.min_edge:.2%}")
    print(f"   Min confidence: {sim.min_confidence:.2%}")
    if researcher and researcher.is_available():
        print(f"   Researcher: {researcher.model} ({researcher.daily_budget} calls/day)")
    print(f"\n   Running...\n")

    exchange = list(bot.exchanges.values())[0]

    for i in range(scans):
        try:
            result = sim.scan(exchange)
            print(f"   Scan {i+1}/{scans}: {result['trades']} trades taken")

            # Dynamic interval from scheduler
            if scheduler and i < scans - 1:
                try:
                    markets = exchange.get_markets(limit=30)
                    dynamic_interval, researcher_enabled = scheduler.auto_interval(markets, "sports")
                    if researcher_enabled and researcher and researcher.is_available():
                        # TODO: researcher analyzes interesting markets here
                        pass
                except Exception:
                    dynamic_interval = interval
            else:
                dynamic_interval = interval

        except Exception as e:
            logger.error(f"Scan error: {e}")
            dynamic_interval = interval

        if i < scans - 1:
            import time
            time.sleep(dynamic_interval)

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


def cmd_resolve():
    """Resolve open paper trades — check market outcomes and compute P&L."""
    from bot.resolver import TradeResolver
    from bot.runner import PredictionBot

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

    exchange = list(bot.exchanges.values())[0]
    resolver = TradeResolver(config.get("log_dir", "data"))

    session_id = sys.argv[2] if len(sys.argv) > 2 else None

    if session_id:
        print(f"🔍 Resolving session: {session_id}")
        result = resolver.resolve_session(session_id, exchange)
    else:
        print("🔍 Resolving all sessions with open trades...")
        results = resolver.resolve_all_open(exchange)
        if results:
            for r in results:
                print(f"\n  Session {r['session_id']}: "
                      f"{r['resolved_this_pass']} resolved, "
                      f"{r['still_open']} still open | "
                      f"P&L: ${r['session_pnl']:+.4f} | "
                      f"Balance: ${r['balance']:.2f}")
        else:
            print("  No open trades found in any session.")

    bot.close()


def cmd_prediction_lab_run(config_path: str = 'config.yaml'):
    from scripts.prediction_lab_run import main as prediction_lab_main
    import sys
    sys.argv = ['prediction_lab_run', '--config', config_path]
    prediction_lab_main()


def cmd_prediction_lab_resolve(config_path: str = 'config.yaml'):
    from scripts.prediction_lab_resolve import main as prediction_lab_resolve_main
    import sys
    sys.argv = ['prediction_lab_resolve', '--config', config_path]
    prediction_lab_resolve_main()


def cmd_prediction_lab_report(config_path: str = 'config.yaml'):
    from scripts.prediction_lab_report import main as prediction_lab_report_main
    import sys
    sys.argv = ['prediction_lab_report', '--config', config_path]
    prediction_lab_report_main()


def cmd_prediction_lab_collect(config_path: str = 'config.yaml'):
    from scripts.prediction_lab_collect import main as prediction_lab_collect_main
    import sys
    sys.argv = ['prediction_lab_collect', '--config', config_path]
    prediction_lab_collect_main()


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

    single_trade = "--single-trade" in sys.argv
    verbosity_override = "double_verbose" if "-vv" in sys.argv else "verbose" if "-v" in sys.argv else None
    config_path = "config.yaml"
    if "--config" in sys.argv:
        idx = sys.argv.index("--config")
        if idx + 1 < len(sys.argv):
            config_path = sys.argv[idx + 1]

    if cmd == "demo":
        cmd_demo()
    elif cmd == "paper":
        cmd_paper()
    elif cmd == "live":
        skip_next = False
        args = []
        for arg in sys.argv[2:]:
            if skip_next:
                skip_next = False
                continue
            if arg == "--config":
                skip_next = True
                continue
            if arg in {"--single-trade", "-v", "-vv"}:
                continue
            args.append(arg)
        interval = int(args[0]) if args else 120
        cmd_live(interval, single_trade=single_trade, verbosity_override=verbosity_override, config_path=config_path)
    elif cmd == "status":
        cmd_status()
    elif cmd == "markets":
        cmd_markets()
    elif cmd == "simulate":
        skip_next = False
        args = []
        for arg in sys.argv[2:]:
            if skip_next:
                skip_next = False
                continue
            if arg == "--config":
                skip_next = True
                continue
            if arg in {"--single-trade", "-v", "-vv"}:
                continue
            args.append(arg)
        scans = int(args[0]) if len(args) > 0 else 10
        interval = int(args[1]) if len(args) > 1 else 30
        cmd_simulate(scans, interval, single_trade=single_trade, verbosity_override=verbosity_override, config_path=config_path)
    elif cmd == "audit":
        session_id = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_audit(session_id)
    elif cmd == "backtest":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 30
        cmd_backtest(days, limit)
    elif cmd == "resolve":
        cmd_resolve()
    elif cmd == "prediction-lab-run":
        cmd_prediction_lab_run(config_path)
    elif cmd == "prediction-lab-resolve":
        cmd_prediction_lab_resolve(config_path)
    elif cmd == "prediction-lab-report":
        cmd_prediction_lab_report(config_path)
    elif cmd == "prediction-lab-collect":
        cmd_prediction_lab_collect(config_path)
    elif cmd == "news":
        query = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else None
        cmd_news(query)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
