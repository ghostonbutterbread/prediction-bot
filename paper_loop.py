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
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from pathlib import Path

# Ensure PAPER_MODE=true so RiskManager uses paper limits, not live limits.
# KALSHI_USE_DEMO=false means "use real market data" (not demo API), but that
# does not mean real-money trading — PAPER_MODE controls the risk layer.
os.environ.setdefault("PAPER_MODE", "true")

# Setup logging — allow per-instance override via PAPER_LOG_FILE env var
_log_file = os.getenv("PAPER_LOG_FILE")
_log_max_bytes = int(os.getenv("PAPER_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
_log_backups = int(os.getenv("PAPER_LOG_BACKUPS", "3"))
if _log_file:
    _log_path = Path(_log_file)
    _log_path.parent.mkdir(parents=True, exist_ok=True)
else:
    log_dir = Path(__file__).parent / "data"
    log_dir.mkdir(exist_ok=True)
    _log_path = log_dir / "paper_loop.log"


class CompactLogFormatter(logging.Formatter):
    """Single-line formatter that suppresses traceback dumps in the log file."""

    def formatException(self, ei):
        return ""


_log_handler = RotatingFileHandler(
    _log_path,
    maxBytes=_log_max_bytes,
    backupCount=_log_backups,
)
_stream_handler = logging.StreamHandler()
_formatter = CompactLogFormatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
for _handler in (_log_handler, _stream_handler):
    _handler.setFormatter(_formatter)

_root_logger = logging.getLogger()
_root_logger.handlers.clear()
_root_logger.setLevel(logging.WARNING)
_root_logger.addHandler(_log_handler)
_root_logger.addHandler(_stream_handler)

for _logger_name in (
    "bot.simulator",
    "bot.runner",
    "bot.exchanges.kalshi",
    "bot.risk",
    "bot.resolver",
    "bot.feeds.ai_signal",
):
    logging.getLogger(_logger_name).setLevel(logging.WARNING)

logger = logging.getLogger("paper-loop")
logger.setLevel(logging.INFO)

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from bot.runner import PredictionBot
from bot.simulator import Simulator
from bot.dashboard import render_simple

INTERVAL = int(os.getenv("PAPER_SCAN_INTERVAL", "120"))  # 2 min default
SIMULATE_ONLY = os.getenv("SIMULATE_ONLY", "true").lower() == "true"
SUMMARY_SCAN_INTERVAL = int(os.getenv("PAPER_SUMMARY_SCAN_INTERVAL", "100"))
SUMMARY_LOG_SECONDS = int(os.getenv("PAPER_SUMMARY_LOG_SECONDS", "3600"))


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


def _log_trade_decisions(new_trades, balance: float):
    """Log only newly opened simulated trades."""
    for trade in new_trades:
        question = (getattr(trade, "question", "") or "").strip() or getattr(trade, "market_id", "unknown market")
        logger.info(
            "TRADE %s | %s | price=$%.2f | size=$%.2f | balance=$%.2f",
            getattr(trade, "direction", "UNKNOWN"),
            question,
            getattr(trade, "market_price", 0.0),
            getattr(trade, "position_size", 0.0),
            balance,
        )


def _log_summary(simulator, scan_num: int, reason: str):
    """Emit a concise portfolio summary on a fixed cadence."""
    balance = getattr(simulator, "balance", 0.0)
    starting_balance = getattr(simulator, "starting_balance", 0.0)
    pnl = balance - starting_balance
    pnl_pct = (pnl / starting_balance * 100) if starting_balance else 0.0

    resolved_count = 0
    open_count = 0
    wins = 0
    for trade in getattr(simulator, "trades", []):
        if getattr(trade, "resolved", False):
            resolved_count += 1
            if (getattr(trade, "pnl", 0.0) or 0.0) > 0:
                wins += 1
        else:
            open_count += 1

    win_rate = (wins / resolved_count) if resolved_count else 0.0
    logger.info(
        "SUMMARY [%s] scan=%s balance=$%.2f pnl=%+.2f (%+.1f%%) win_rate=%.0f%% trades=%s (%s open / %s resolved)",
        reason,
        scan_num,
        balance,
        pnl,
        pnl_pct,
        win_rate * 100,
        open_count + resolved_count,
        open_count,
        resolved_count,
    )


def _log_blockers(scan_num: int, blocked_reasons: dict):
    if not blocked_reasons:
        return
    ranked = sorted(blocked_reasons.items(), key=lambda item: item[1], reverse=True)
    top = ", ".join(f"{name}={count}" for name, count in ranked[:4])
    logger.info("SCAN BLOCKERS scan=%s %s", scan_num, top)


def run():
    config = get_config()
    mode = "PAPER (simulation)" if config["paper_mode"] else "LIVE (real orders)"
    logger.info("Starting [%s] interval=%ss data=%s", mode, INTERVAL, config["data_dir"])

    consecutive_errors = 0
    max_errors = 5
    scan_num = 0
    last_summary_at: float | None = None
    last_summary_scan = 0
    cooldown_logged = False

    # Persistent bot + simulator (maintain state across scans)
    bot = None
    sim = None

    while True:
        try:
            # Init (or re-init after crash) — Simulator auto-loads latest session
            if bot is None:
                bot, sim = create_bot_and_sim()
                consecutive_errors = 0
                scan_num = sim.scan_count

            exchange = list(bot.exchanges.values())[0]  # Kalshi

            # Check if risk manager is in cooldown — skip this scan cycle
            if sim.risk.state.is_in_cooldown:
                cooldown_remaining = ""
                if sim.risk.state.cooldown_until:
                    remaining = datetime.fromisoformat(sim.risk.state.cooldown_until) - datetime.now(timezone.utc)
                    cooldown_remaining = f" ({remaining.total_seconds()/60:.1f} min remaining)"
                if not cooldown_logged:
                    logger.warning(
                        "Cooldown active (%s consecutive losses)%s",
                        sim.risk.state.consecutive_losses,
                        cooldown_remaining,
                    )
                    cooldown_logged = True
                time.sleep(30)  # Wait a bit before retrying
                continue
            if cooldown_logged:
                logger.info("Cooldown cleared; resuming scans")
                cooldown_logged = False

            if SIMULATE_ONLY:
                # Run simulator scan — tracks trades, balance, P&L
                previous_trade_count = len(sim.trades)
                result = sim.scan(exchange)
                scan_num = sim.scan_count
                new_trades = sim.trades[previous_trade_count:]
                if new_trades:
                    _log_trade_decisions(new_trades, result["balance"])
                elif result.get("blocked_reasons"):
                    _log_blockers(scan_num, result["blocked_reasons"])

                # Render live dashboard
                dashboard_str = render_simple(sim, scan_num=scan_num)
                print(dashboard_str)

                now_ts = time.time()
                if (
                    last_summary_at is None or
                    (scan_num - last_summary_scan) >= SUMMARY_SCAN_INTERVAL or
                    (now_ts - last_summary_at) >= SUMMARY_LOG_SECONDS
                ):
                    reason = "startup" if last_summary_at is None else (
                        f"{SUMMARY_SCAN_INTERVAL}-scan cadence"
                        if (scan_num - last_summary_scan) >= SUMMARY_SCAN_INTERVAL
                        else "hourly cadence"
                    )
                    _log_summary(sim, scan_num=scan_num, reason=reason)
                    last_summary_at = now_ts
                    last_summary_scan = scan_num

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
                    logger.warning("Sent 2-day loss streak alert (streak=%s)", streak)

            else:
                # Live paper trading mode — run loop
                bot.run_loop(interval_seconds=INTERVAL, max_scans=None)

            bot.close()
            bot = None  # Force re-create next iteration
            time.sleep(INTERVAL)

        except KeyboardInterrupt:
            logger.info("Stopping paper loop")
            break

        except Exception as e:
            consecutive_errors += 1
            logger.error("Crash (%s/%s): %s", consecutive_errors, max_errors, e)
            bot = None  # Force re-create

            if consecutive_errors >= max_errors:
                logger.critical("Too many consecutive crashes; stopping for manual review")
                break

            wait = min(60 * consecutive_errors, 300)
            logger.warning("Restarting in %ss", wait)
            time.sleep(wait)


if __name__ == "__main__":
    run()
