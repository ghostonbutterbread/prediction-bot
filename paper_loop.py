#!/usr/bin/env python3
"""
Persistent simulation runner — scans markets and logs signals.
NO real orders placed. For backtesting and learning.

Usage:
    SIMULATE_ONLY=true python paper_loop.py
"""
import sys
import os
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

# Setup logging
log_dir = Path(__file__).parent / "data"
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(log_dir / "paper_loop.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("paper-loop")

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from bot.runner import PredictionBot

INTERVAL = int(os.getenv("PAPER_SCAN_INTERVAL", "120"))  # 2 min default
SIMULATE_ONLY = os.getenv("SIMULATE_ONLY", "true").lower() == "true"

def get_config():
    return {
        "strategy": {
            "min_edge": float(os.getenv("MIN_EDGE", "0.05")),
            "min_confidence": float(os.getenv("MIN_CONFIDENCE", "0.50")),
            "news_weight": float(os.getenv("NEWS_WEIGHT", "0.15")),
            "ai_weight": float(os.getenv("AI_WEIGHT", "0.20")),
            "enable_news": False,  # Force off - Google News blocked
            "enable_ai": False,  # Force off - calls news
            "enable_social": False,  # Force off - also calls Google News
        },
        "kelly_fraction": float(os.getenv("KELLY_FRACTION", "0.5")),
        "max_position_pct": float(os.getenv("MAX_POSITION_PCT", "0.10")),
        "log_dir": os.getenv("LOG_DIR", "data"),
    }

def create_bot():
    config = get_config()
    bot = PredictionBot(config)

    api_key = os.getenv("KALSHI_API_KEY_ID")
    private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key")
    # Use live API for market data only — no orders placed in simulation mode
    demo = False

    if not api_key:
        raise RuntimeError("KALSHI_API_KEY_ID not set in .env")

    bot.add_kalshi(api_key, private_key_path, demo=demo)
    results = bot.connect_all()

    if not any(results.values()):
        raise RuntimeError(f"Kalshi connection failed: {results}")

    return bot

def run_simulate_scan(bot, scan_num):
    """Run a single scan and log signals — NO orders placed."""
    from bot.strategies.enhanced import EnhancedStrategyEngine, KellySizer

    config = get_config()
    strategy = EnhancedStrategyEngine(config.get("strategy", {}))
    kelly = KellySizer(fraction=config.get("kelly_fraction", 0.5))

    signals_logged = 0

    for exchange_name, exchange in bot.exchanges.items():
        markets = exchange.get_markets(limit=20)
        if not markets:
            continue

        logger.info(f"  [{exchange_name}] Scanning {len(markets)} markets...")

        for market in markets:
            try:
                signal = strategy.analyze_market(market)
                if not signal:
                    continue

                edge = signal.get("edge", 0)
                confidence = signal.get("confidence", 0)

                # Check thresholds
                if confidence < 0.50 or edge < 0.05:
                    continue

                # Calculate size
                balance = exchange.get_balance()
                size = kelly.calculate(
                    signal["model_probability"],
                    signal["market_price"],
                    balance,
                )

                if size < 1:
                    continue

                # Get actual market price
                market_bid_ask = exchange.get_market_bid_ask(market.id)
                if market_bid_ask and market_bid_ask.get("best_yes_ask", 0) > 0:
                    price = market_bid_ask.get("best_yes_ask", 0)
                else:
                    price = signal.get("market_price", 0.50)

                direction = signal["direction"]
                side = "YES" if direction == "BUY_YES" else "NO"

                # LOG what we WOULD buy — no order placed
                logger.info(
                    f"  📋 WOULD BUY: {side} ${size:.2f} @ ${price:.4f} "
                    f"on {market.id} | Edge: {edge*100:.1f}% | Conf: {confidence*100:.1f}%"
                )

                # Write to simulated trades log for later analysis
                with open(log_dir / "simulated_trades.csv", "a") as f:
                    f.write(
                        f"{datetime.now(timezone.utc).isoformat()},"
                        f"{scan_num},{market.id},{direction},"
                        f"{signal['model_probability']:.4f},{price:.4f},"
                        f"{size:.2f},{edge*100:.2f},{confidence*100:.2f},"
                        f"{market.question[:80]}\n"
                    )

                signals_logged += 1

            except Exception as e:
                logger.debug(f"Error scanning market {market.id}: {e}")

    return signals_logged

def run():
    mode = "SIMULATION" if SIMULATE_ONLY else "PAPER (orders allowed)"
    logger.info(f"🚀 Starting continuous paper trading [{mode}] — interval: {INTERVAL}s")
    logger.info("   Press Ctrl+C to stop\n")

    # Init CSV log for simulated trades
    csv_path = log_dir / "simulated_trades.csv"
    if not csv_path.exists():
        with open(csv_path, "w") as f:
            f.write("timestamp,scan,market_id,direction,model_prob,price,size_kelly,edge_pct,conf_pct,question\n")

    consecutive_errors = 0
    max_errors = 5
    scan_num = 0

    while True:
        try:
            bot = create_bot()
            consecutive_errors = 0
            scan_num += 1

            logger.info(f"\n{'='*60}")
            logger.info(f"  SCAN #{scan_num} @ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
            logger.info(f"{'='*60}")

            if SIMULATE_ONLY:
                # Just scan and log signals — no orders
                count = run_simulate_scan(bot, scan_num)
                logger.info(f"  → Logged {count} simulated signals")
            else:
                # Run the loop (indefinite)
                bot.run_loop(interval_seconds=INTERVAL, max_scans=None)

            bot.close()
            logger.info(f"  ✅ Scan complete. Next scan in {INTERVAL}s\n")
            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            logger.info("⏹ Stopped by user")
            break

        except Exception as e:
            consecutive_errors += 1
            logger.error(f"❌ Crash ({consecutive_errors}/{max_errors}): {e}", exc_info=True)

            if consecutive_errors >= max_errors:
                logger.critical("💥 Too many consecutive crashes — stopping for manual review")
                break

            wait = min(60 * consecutive_errors, 300)
            logger.info(f"⏳ Restarting in {wait}s...")
            time.sleep(wait)

if __name__ == "__main__":
    run()
