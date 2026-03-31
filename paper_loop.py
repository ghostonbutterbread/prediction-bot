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

# Setup logging — allow per-instance override via PAPER_LOG_FILE env var
_log_file = os.getenv("PAPER_LOG_FILE")
if _log_file:
    _log_path = Path(_log_file)
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    _log_handler = logging.FileHandler(_log_path)
else:
    log_dir = Path(__file__).parent / "data"
    log_dir.mkdir(exist_ok=True)
    _log_handler = logging.FileHandler(log_dir / "paper_loop.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        _log_handler,
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("paper-loop")

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from bot.runner import PredictionBot
from bot.simulator import Simulator
from bot.dashboard import render_simple

INTERVAL = int(os.getenv("PAPER_SCAN_INTERVAL", "120"))  # 2 min default
SIMULATE_ONLY = os.getenv("SIMULATE_ONLY", "true").lower() == "true"


def get_config():
    paper_mode = os.getenv("PAPER_MODE", "true").lower() == "true"
    mode_dir = "paper" if paper_mode else "live"

    return {
        "strategy": {
            "min_edge": float(os.getenv("MIN_EDGE", "0.05")),
            "min_confidence": float(os.getenv("MIN_CONFIDENCE", "0.50")),
            "news_weight": float(os.getenv("NEWS_WEIGHT", "0.15")),
            "ai_weight": float(os.getenv("AI_WEIGHT", "0.20")),
            # News uses fallback sources (Yahoo Finance RSS, Bing News RSS).
            # If all fail, the strategy degrades gracefully to price+volume signals.
            "enable_news": os.getenv("ENABLE_NEWS_FALLBACK", "true").lower() != "false",
            "enable_ai": False,   # Still off — AI calls depend on external LLM quota
            "enable_social": False,  # Still off — Twitter/X API not configured
        },
        "kelly_fraction": float(os.getenv("KELLY_FRACTION", "0.5")),
        "max_position_pct": float(os.getenv("MAX_POSITION_PCT", "0.10")),
        "max_entry_price": float(os.getenv("MAX_ENTRY_PRICE", "0.70")),  # Entry price cap
        "log_dir": os.getenv("LOG_DIR", f"data/{mode_dir}"),
        "data_dir": os.getenv("DATA_DIR", f"data/{mode_dir}"),
        "starting_balance": float(os.getenv("STARTING_BALANCE", "100.0")),
        "enable_time_decay_ranking": os.getenv("TIME_DECAY_RANKING", "true").lower() == "true",
        "paper_mode": paper_mode,
    }


def create_bot_and_sim():
    """Create exchange bot + simulator (shared state across scans)."""
    config = get_config()

    api_key = os.getenv("KALSHI_API_KEY_ID")
    private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key")
    demo = False

    if not api_key:
        raise RuntimeError("KALSHI_API_KEY_ID not set in .env")

    # PredictionBot (market data only — no orders placed)
    bot = PredictionBot(config)
    bot.add_kalshi(api_key, private_key_path, demo=demo)
    results = bot.connect_all()
    if not any(results.values()):
        raise RuntimeError(f"Kalshi connection failed: {results}")

    # Simulator handles all trade tracking, P&L, resolution
    sim = Simulator(config)

    return bot, sim


def run():
    config = get_config()
    mode = "PAPER (simulation)" if config["paper_mode"] else "LIVE (real orders)"
    logger.info(f"🚀 Starting [{mode}] — interval: {INTERVAL}s — data: {config['data_dir']}")
    logger.info("   Press Ctrl+C to stop\n")

    consecutive_errors = 0
    max_errors = 5
    scan_num = 0

    # Persistent bot + simulator (maintain state across scans)
    bot = None
    sim = None

    while True:
        try:
            # Init (or re-init after crash) — Simulator auto-loads latest session
            if bot is None:
                bot, sim = create_bot_and_sim()
                consecutive_errors = 0
                scan_num = sim.scan_count + 1  # display scan number (sim.scan_count already incremented in scan())

            scan_num += 1
            exchange = list(bot.exchanges.values())[0]  # Kalshi

            # Check if risk manager is in cooldown — skip this scan cycle
            if sim.risk.state.is_in_cooldown:
                cooldown_remaining = ""
                if sim.risk.state.cooldown_until:
                    remaining = datetime.fromisoformat(sim.risk.state.cooldown_until) - datetime.now(timezone.utc)
                    cooldown_remaining = f" ({remaining.total_seconds()/60:.1f} min remaining)"
                logger.info(f"  ⏸️  Cooldown active ({sim.risk.state.consecutive_losses} consecutive losses){cooldown_remaining} — skipping scan")
                time.sleep(30)  # Wait a bit before retrying
                continue

            logger.info(f"\n{'='*60}")
            logger.info(f"  SCAN #{scan_num} @ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
            logger.info(f"{'='*60}")

            if SIMULATE_ONLY:
                # Run simulator scan — tracks trades, balance, P&L
                result = sim.scan(exchange)
                logger.info(f"  → Markets: {result['markets']} | Signals: {result['signals']} | Trades: {result['trades']}")

                # Render live dashboard
                dashboard_str = render_simple(sim, scan_num=scan_num)
                print(dashboard_str)
                logger.info(f"  Balance: ${result['balance']:.2f}")

                # Check for 2-day losing streak → alert Ryushe
                alert, streak = sim.check_daily_loss_streak()
                if alert:
                    alert_msg = (
                        f"⚠️ *2-Day Losing Streak Alert*\n\n"
                        f"Consecutive losing days: *{streak}*\n"
                        f"Current balance: ${sim.balance:.2f}\n"
                        f"Session: `{sim.session_id}`\n\n"
                        f"Review your strategy."
                    )
                    import subprocess
                    subprocess.run(
                        ["python3", "scripts/send_alert.py", "-m", alert_msg],
                        cwd=Path(__file__).parent,
                        capture_output=True,
                    )
                    logger.warning(f"🚨 Sent 2-day loss streak alert (streak={streak})")

            else:
                # Live paper trading mode — run loop
                bot.run_loop(interval_seconds=INTERVAL, max_scans=None)

            bot.close()
            bot = None  # Force re-create next iteration
            logger.info(f"  ✅ Scan complete. Next scan in {INTERVAL}s\n")
            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            logger.info("⏹ Stopped by user")
            break

        except Exception as e:
            consecutive_errors += 1
            logger.error(f"❌ Crash ({consecutive_errors}/{max_errors}): {e}", exc_info=True)
            bot = None  # Force re-create

            if consecutive_errors >= max_errors:
                logger.critical("💥 Too many consecutive crashes — stopping for manual review")
                break

            wait = min(60 * consecutive_errors, 300)
            logger.info(f"⏳ Restarting in {wait}s...")
            time.sleep(wait)


if __name__ == "__main__":
    run()
