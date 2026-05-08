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
import argparse
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from pathlib import Path


class CompactLogFormatter(logging.Formatter):
    """Single-line formatter that suppresses traceback dumps in the log file."""

    def formatException(self, ei):
        return ""


_RUNTIME_LOGGER_NAMES = (
    "bot.simulator",
    "bot.runner",
    "bot.exchanges.kalshi",
    "bot.risk",
    "bot.resolver",
    "bot.feeds.ai_signal",
)

logger = logging.getLogger("paper-loop")

from dotenv import load_dotenv

from bot.config import ensure_mode_storage_dir, load_config
from bot.runner import PredictionBot
from bot.simulator import Simulator
from bot.dashboard import render_simple
from bot.status import build_snapshot, format_status_message, send_status_update
from bot.strategy_policy import coerce_strategy_policy

INTERVAL = int(os.getenv("PAPER_SCAN_INTERVAL", "120"))  # 2 min default
SIMULATE_ONLY = os.getenv("SIMULATE_ONLY", "true").lower() == "true"
SUMMARY_SCAN_INTERVAL = int(os.getenv("PAPER_SUMMARY_SCAN_INTERVAL", "100"))
SUMMARY_LOG_SECONDS = int(os.getenv("PAPER_SUMMARY_LOG_SECONDS", "3600"))


def _resolve_paper_config_path(config_path: str | Path | None = None) -> Path | None:
    if config_path:
        return Path(config_path)
    env_path = os.getenv("PAPER_CONFIG")
    if env_path:
        return Path(env_path)
    return None


def configure_logging():
    """Configure paper-loop runtime logging."""
    # Allow per-instance override via PAPER_LOG_FILE env var.
    log_file = os.getenv("PAPER_LOG_FILE")
    log_max_bytes = int(os.getenv("PAPER_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
    log_backups = int(os.getenv("PAPER_LOG_BACKUPS", "3"))
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        log_dir = Path(__file__).parent / "data"
        log_dir.mkdir(exist_ok=True)
        log_path = log_dir / "paper_loop.log"

    log_handler = RotatingFileHandler(
        log_path,
        maxBytes=log_max_bytes,
        backupCount=log_backups,
    )
    stream_handler = logging.StreamHandler()
    formatter = CompactLogFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for handler in (log_handler, stream_handler):
        handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.WARNING)
    root_logger.addHandler(log_handler)
    root_logger.addHandler(stream_handler)

    for logger_name in _RUNTIME_LOGGER_NAMES:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    logger.setLevel(logging.INFO)


def _refresh_runtime_settings():
    global INTERVAL, SIMULATE_ONLY, SUMMARY_SCAN_INTERVAL, SUMMARY_LOG_SECONDS
    INTERVAL = int(os.getenv("PAPER_SCAN_INTERVAL", "120"))  # 2 min default
    SIMULATE_ONLY = os.getenv("SIMULATE_ONLY", "true").lower() == "true"
    SUMMARY_SCAN_INTERVAL = int(os.getenv("PAPER_SUMMARY_SCAN_INTERVAL", "100"))
    SUMMARY_LOG_SECONDS = int(os.getenv("PAPER_SUMMARY_LOG_SECONDS", "3600"))


def load_runtime_env(dotenv_path: str | Path | None = None):
    # Ensure PAPER_MODE=true so RiskManager uses paper limits, not live limits.
    # KALSHI_USE_DEMO=false means "use real market data" (not demo API), but that
    # does not mean real-money trading — PAPER_MODE controls the risk layer.
    os.environ.setdefault("PAPER_MODE", "true")
    if dotenv_path is None:
        load_dotenv()
    else:
        load_dotenv(dotenv_path)
    _refresh_runtime_settings()


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


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def get_config(config_path: str | Path | None = None):
    resolved_config_path = _resolve_paper_config_path(config_path)
    config = load_config(resolved_config_path)
    if resolved_config_path is not None:
        config["_config_path"] = str(resolved_config_path)
    runtime_cfg = config.get("runtime", {}) or {}
    isolated_runtime = bool(runtime_cfg.get("isolated", False))
    loaded_trading = config.get("trading", {}) or {}
    if isolated_runtime and str(loaded_trading.get("mode", "paper")).strip().lower() == "paper":
        paper_mode = True
    else:
        paper_mode = os.getenv("PAPER_MODE", "true").lower() == "true"
    mode_dir = "paper" if paper_mode else "live"

    trading_enabled = _env_bool(
        "TRADING_ENABLED",
        bool(loaded_trading.get("enabled", loaded_trading.get("trading_enabled", config.get("trading_enabled", True)))),
    )
    max_tradable_balance = _env_float(
        "MAX_TRADABLE_BALANCE_USD",
        float(config.get("max_tradable_balance_usd", config.get("max_tradable_balance", 0.0)) or 0.0),
    )
    max_position_size_usd = _env_float(
        "MAX_POSITION_SIZE_USD",
        float(config.get("max_position_size_usd", 0.0) or 0.0),
    )
    loaded_alerts = config.get("alerts", {}) or {}
    status_update_interval_minutes = _env_int(
        "STATUS_UPDATE_INTERVAL_MINUTES",
        int(loaded_alerts.get("status_update_interval_minutes", config.get("status_update_interval_minutes", 60)) or 60),
    )

    strategy = dict(config.get("strategy", {}) or {})
    strategy.setdefault("min_edge", 0.05)
    strategy.setdefault("min_confidence", 0.50)
    strategy.setdefault("news_weight", 0.15)
    strategy.setdefault("ai_weight", 0.20)
    strategy["min_edge"] = float(os.getenv("MIN_EDGE", strategy["min_edge"]))
    strategy["min_confidence"] = float(os.getenv("MIN_CONFIDENCE", strategy["min_confidence"]))
    strategy["news_weight"] = float(os.getenv("NEWS_WEIGHT", strategy["news_weight"]))
    strategy["ai_weight"] = float(os.getenv("AI_WEIGHT", strategy["ai_weight"]))
    # News uses fallback sources (Yahoo Finance RSS, Bing News RSS). Paper mode
    # fails closed when those sources are exhausted so degraded feeds cannot
    # silently redistribute weight into weaker signals.
    strategy["enable_news"] = os.getenv("ENABLE_NEWS_FALLBACK", str(strategy.get("enable_news", True))).lower() != "false"
    strategy["fail_closed_on_news_source_failure"] = _env_bool(
        "FAIL_CLOSED_ON_NEWS_SOURCE_FAILURE",
        bool(strategy.get("fail_closed_on_news_source_failure", True)),
    )
    strategy["enable_weather_hidden_gem_safety_guard"] = _env_bool(
        "ENABLE_WEATHER_HIDDEN_GEM_SAFETY_GUARD",
        True,
    )
    policy = coerce_strategy_policy(config.get("strategy_policy_normalized") or config.get("strategy_policy"))
    directional_env = os.getenv("ENABLE_WEATHER_DIRECTIONAL_MISMATCH_GUARD")
    directional_policy_default = bool(policy.feature_enforced("hidden_gem_lane_gates"))
    if directional_env is None:
        strategy["enable_weather_directional_mismatch_guard"] = directional_policy_default
        strategy["weather_directional_mismatch_guard_explicit_override"] = False
    else:
        strategy["enable_weather_directional_mismatch_guard"] = _env_bool(
            "ENABLE_WEATHER_DIRECTIONAL_MISMATCH_GUARD",
            directional_policy_default,
        )
        strategy["weather_directional_mismatch_guard_explicit_override"] = bool(
            strategy["enable_weather_directional_mismatch_guard"]
        )
    strategy["strategy_policy_normalized"] = policy
    if "strategy_policy" in config:
        strategy["strategy_policy"] = dict(config.get("strategy_policy", {}) or {})
    strategy.setdefault("enable_ai", False)
    strategy.setdefault("enable_social", False)
    config["strategy"] = strategy

    config["kelly_fraction"] = float(os.getenv("KELLY_FRACTION", config.get("kelly_fraction", 0.5)))
    config["max_position_pct"] = float(os.getenv("MAX_POSITION_PCT", config.get("max_position_pct", 0.10)))
    config["max_entry_price"] = float(os.getenv("MAX_ENTRY_PRICE", config.get("max_entry_price", 0.70)))
    config["starting_balance"] = float(os.getenv("STARTING_BALANCE", config.get("starting_balance", 100.0)))
    config["enable_time_decay_ranking"] = os.getenv(
        "TIME_DECAY_RANKING",
        str(config.get("enable_time_decay_ranking", True)),
    ).lower() == "true"
    config["paper_mode"] = paper_mode
    config["trading_enabled"] = trading_enabled
    config["max_tradable_balance_usd"] = max_tradable_balance
    config["max_position_size_usd"] = max_position_size_usd
    config["status_update_interval_minutes"] = status_update_interval_minutes

    if isolated_runtime:
        data_dir = config.get("data_dir", f"data/{mode_dir}")
        log_dir = config.get("log_dir", f"data/{mode_dir}")
    else:
        data_dir = os.getenv("DATA_DIR", config.get("data_dir", f"data/{mode_dir}"))
        log_dir = os.getenv("LOG_DIR", config.get("log_dir", f"data/{mode_dir}"))
    config["data_dir"] = str(ensure_mode_storage_dir(data_dir, mode_dir))
    config["log_dir"] = str(ensure_mode_storage_dir(log_dir, mode_dir))
    config.setdefault("runtime", {})["mode"] = mode_dir
    config["runtime"]["mode_dir"] = config["data_dir"]
    config.setdefault("logging", {})["log_dir"] = config["log_dir"]

    trading = dict(config.get("trading", {}) or {})
    trading["mode"] = mode_dir
    trading["trading_enabled"] = trading_enabled
    trading["enabled"] = trading_enabled
    config["trading"] = trading

    alerts = dict(config.get("alerts", {}) or {})
    alerts["enabled"] = _env_bool("STATUS_ALERTS_ENABLED", bool(alerts.get("enabled", True)))
    alerts["status_update_interval_minutes"] = status_update_interval_minutes
    alerts["send_hourly_status"] = _env_bool("SEND_HOURLY_STATUS", bool(alerts.get("send_hourly_status", False)))
    config["alerts"] = alerts

    return config



def create_bot_and_sim(config_path: str | Path | None = None):
    """Create exchange bot + simulator (shared state across scans)."""
    config = get_config(config_path)

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


def _build_status_snapshot(simulator, scan_num: int):
    balance = getattr(simulator, "balance", 0.0)
    starting_balance = getattr(simulator, "starting_balance", 0.0)
    pnl = balance - starting_balance
    pnl_pct = (pnl / starting_balance * 100) if starting_balance else 0.0
    risk_status = simulator.risk.get_status()

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
    extra = {}
    if risk_status.get("standby_active"):
        extra["standby_active"] = True
        extra["standby_reason_codes"] = ",".join(risk_status.get("standby_reason_codes", []))
        extra["standby_blocked_scan_count"] = risk_status.get("standby_blocked_scan_count")
        extra["standby_entered_at"] = risk_status.get("standby_entered_at")
        extra["standby_useful_trade_capacity"] = risk_status.get("standby_useful_trade_capacity")
    elif risk_status.get("standby_last_resume_at"):
        extra["standby_last_resume_at"] = risk_status.get("standby_last_resume_at")
        extra["standby_last_resume_reason"] = risk_status.get("standby_last_resume_reason")
    return build_snapshot(
        mode=risk_status.get("mode"),
        trading_enabled=bool(risk_status.get("trading_enabled")),
        tradable_cap=risk_status.get("max_tradable_balance"),
        max_position_size=risk_status.get("max_position_size_usd"),
        balance=balance,
        available_cash=risk_status.get("available_cash"),
        reserved_capital=risk_status.get("reserved_capital"),
        exposure=risk_status.get("exposure"),
        pnl=pnl,
        pnl_pct=pnl_pct,
        win_rate_pct=win_rate * 100,
        total_trades=open_count + resolved_count,
        open_trades=open_count,
        resolved_trades=resolved_count,
        scan_num=scan_num,
        session_id=getattr(simulator, "session_id", ""),
        extra=extra,
    )


def _log_summary(simulator, scan_num: int, reason: str):
    """Emit a concise portfolio summary on a fixed cadence."""
    snapshot = _build_status_snapshot(simulator, scan_num=scan_num)
    risk_status = simulator.risk.get_status()
    logger.info(
        "SUMMARY [%s] scan=%s mode=%s enabled=%s tradable_cap=%s max_pos=%s balance=$%.2f avail=%s reserved=%s exposure=%s pnl=%+.2f (%+.1f%%) win_rate=%.0f%% trades=%s (%s open / %s resolved) standby=%s standby_reasons=%s useful_capacity=%s",
        reason,
        scan_num,
        risk_status.get("mode"),
        risk_status.get("trading_enabled"),
        risk_status.get("max_tradable_balance"),
        risk_status.get("max_position_size_usd"),
        snapshot.balance,
        risk_status.get("available_cash"),
        risk_status.get("reserved_capital"),
        risk_status.get("exposure"),
        snapshot.pnl,
        snapshot.pnl_pct,
        snapshot.win_rate_pct,
        snapshot.total_trades,
        snapshot.open_trades,
        snapshot.resolved_trades,
        risk_status.get("standby_active"),
        ",".join(risk_status.get("standby_reason_codes", [])),
        risk_status.get("standby_useful_trade_capacity"),
    )

    if simulator.config.get("alerts", {}).get("enabled") and simulator.config.get("alerts", {}).get("send_hourly_status"):
        send_status_update(format_status_message(snapshot, reason=reason), project_root=Path(__file__).parent)


def _log_blockers(scan_num: int, blocked_reasons: dict):
    if not blocked_reasons:
        return
    ranked = sorted(blocked_reasons.items(), key=lambda item: item[1], reverse=True)
    top = ", ".join(f"{name}={count}" for name, count in ranked[:4])
    logger.info("SCAN BLOCKERS scan=%s %s", scan_num, top)


def run(config_path: str | Path | None = None):
    config = get_config(config_path)
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
                bot, sim = create_bot_and_sim(config_path)
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
                    send_status_update(alert_msg, project_root=Path(__file__).parent)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run paper simulation loop")
    parser.add_argument(
        "--config",
        default=None,
        help="Config path for this paper loop. Defaults to PAPER_CONFIG, then config.yaml discovery.",
    )
    args = parser.parse_args(argv)

    configure_logging()
    load_runtime_env()
    run(args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
