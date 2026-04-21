"""Config loader — reads config.yaml with .env override support.

Usage:
    from bot.config import load_config
    config = load_config()  # reads config.yaml, applies .env overrides

Config.yaml is the source of truth. .env values override for backward
compatibility with the existing setup.
"""

import os
import logging
from copy import deepcopy
from typing import Any
from pathlib import Path

logger = logging.getLogger(__name__)


def _default_storage_config() -> dict[str, Any]:
    return {
        "logs": {
            "enabled": True,
            "max_total_gb": 50,
            "warning_threshold_pct": 90,
            "hard_stop_threshold_pct": 105,
            "auto_prune": False,
            "prune_policy": "oldest_first",
            "report_in_status": True,
            "include_paths": [
                "data/paper_loop.log",
                "data/paper_loop_runtime.log",
                "data/watchdog.log",
                "data/watchdog_cron.log",
                "logs/",
                "data/archive/ops/",
            ],
            "exclude_paths": [
                "data/historical/",
                ".venv/",
            ],
        }
    }


def _default_scan_config() -> dict[str, Any]:
    return {
        "markets_per_exchange": 30,
        "summary_sample_per_exchange": 5,
    }


def _normalize_storage_config(config: dict) -> dict:
    storage = _deep_merge(_default_storage_config(), config.get("storage", {}) or {})
    logs = storage.get("logs", {}) or {}
    logs["enabled"] = bool(logs.get("enabled", True))
    logs["max_total_gb"] = float(logs.get("max_total_gb", 50) or 50)
    logs["warning_threshold_pct"] = float(logs.get("warning_threshold_pct", 90) or 90)
    logs["hard_stop_threshold_pct"] = float(logs.get("hard_stop_threshold_pct", 105) or 105)
    logs["auto_prune"] = bool(logs.get("auto_prune", False))
    logs["report_in_status"] = bool(logs.get("report_in_status", True))
    logs["prune_policy"] = str(logs.get("prune_policy", "oldest_first") or "oldest_first")
    logs["include_paths"] = [str(p) for p in (logs.get("include_paths") or [])]
    logs["exclude_paths"] = [str(p) for p in (logs.get("exclude_paths") or [])]
    storage["logs"] = logs
    config["storage"] = storage

    scan = _deep_merge(_default_scan_config(), config.get("scan", {}) or {})
    scan["markets_per_exchange"] = max(1, int(scan.get("markets_per_exchange", 30) or 30))
    scan["summary_sample_per_exchange"] = max(1, int(scan.get("summary_sample_per_exchange", 5) or 5))
    config["scan"] = scan
    return config

try:
    import yaml
except ImportError:
    yaml = None
    logger.warning("PyYAML not installed — will use .env-only config. Install: pip install pyyaml")


def _find_config() -> Path:
    """Find config.yaml relative to project root."""
    # Start from this file's location and search up
    here = Path(__file__).parent.parent
    candidates = [
        here / "config.yaml",
        here.parent / "config.yaml",
        Path.cwd() / "config.yaml",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override into base dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _apply_env_overrides(config: dict) -> dict:
    """Apply .env values for backward compatibility."""
    overrides = {}

    # Strategy
    strategy = {}
    if os.getenv("MIN_EDGE"):
        strategy["min_edge"] = float(os.getenv("MIN_EDGE"))
    if os.getenv("MIN_CONFIDENCE"):
        strategy["min_confidence"] = float(os.getenv("MIN_CONFIDENCE"))
    if os.getenv("NEWS_WEIGHT"):
        strategy["news_weight"] = float(os.getenv("NEWS_WEIGHT"))
    if os.getenv("SOCIAL_WEIGHT"):
        strategy["social_weight"] = float(os.getenv("SOCIAL_WEIGHT"))
    if os.getenv("AI_WEIGHT"):
        strategy["ai_weight"] = float(os.getenv("AI_WEIGHT"))
    if os.getenv("ENABLE_WEATHER_OBSERVATION_LOG"):
        strategy["enable_weather_observation_log"] = (
            os.getenv("ENABLE_WEATHER_OBSERVATION_LOG").lower() == "true"
        )
    if os.getenv("WEATHER_OBSERVATION_LOG_PATH"):
        strategy["weather_observation_log_path"] = os.getenv("WEATHER_OBSERVATION_LOG_PATH")
    if strategy:
        overrides["strategy"] = strategy

    # Trading runtime controls
    trading = {}
    if os.getenv("TRADING_MODE"):
        trading["mode"] = os.getenv("TRADING_MODE").strip().lower()
    if os.getenv("TRADING_ENABLED"):
        trading["enabled"] = os.getenv("TRADING_ENABLED").lower() == "true"
    if trading:
        overrides["trading"] = trading

    # Risk
    risk = {}
    if os.getenv("KELLY_FRACTION"):
        risk["kelly_fraction"] = float(os.getenv("KELLY_FRACTION"))
    if os.getenv("MAX_POSITION_PCT"):
        risk["max_position_pct"] = float(os.getenv("MAX_POSITION_PCT"))
    if os.getenv("DAILY_LOSS_LIMIT_PCT"):
        risk["daily_loss_limit_pct"] = float(os.getenv("DAILY_LOSS_LIMIT_PCT"))
    if os.getenv("MAX_DRAWDOWN_PCT"):
        risk["max_drawdown_pct"] = float(os.getenv("MAX_DRAWDOWN_PCT"))
    if os.getenv("MAX_OPEN_POSITIONS"):
        risk["max_open_positions"] = int(os.getenv("MAX_OPEN_POSITIONS"))
    if risk:
        overrides["risk"] = risk

    # Sports
    sports = {}
    if os.getenv("ENABLE_SPORTS"):
        sports["enabled"] = os.getenv("ENABLE_SPORTS").lower() == "true"
    if os.getenv("SPORTS_MAX_HOURS"):
        sports["max_hours_to_close"] = int(os.getenv("SPORTS_MAX_HOURS"))
    if sports and "market_types" in config and "sports" in config["market_types"]:
        if "market_types" not in overrides:
            overrides["market_types"] = {}
        overrides["market_types"]["sports"] = sports

    # OpenRouter
    if os.getenv("OPENROUTER_MODEL"):
        overrides["openrouter"] = {"model": os.getenv("OPENROUTER_MODEL")}
    if os.getenv("OPENROUTER_API_KEY"):
        if "openrouter" not in overrides:
            overrides["openrouter"] = {}
        # Don't put key in config, just flag that it exists

    # Logging
    if os.getenv("LOG_DIR"):
        overrides["logging"] = {"log_dir": os.getenv("LOG_DIR")}
    if os.getenv("LOG_STORAGE_MAX_GB"):
        overrides.setdefault("storage", {}).setdefault("logs", {})["max_total_gb"] = float(os.getenv("LOG_STORAGE_MAX_GB"))
    if os.getenv("LOG_STORAGE_WARNING_THRESHOLD_PCT"):
        overrides.setdefault("storage", {}).setdefault("logs", {})["warning_threshold_pct"] = float(os.getenv("LOG_STORAGE_WARNING_THRESHOLD_PCT"))
    if os.getenv("LOG_STORAGE_AUTO_PRUNE"):
        overrides.setdefault("storage", {}).setdefault("logs", {})["auto_prune"] = os.getenv("LOG_STORAGE_AUTO_PRUNE").lower() == "true"
    if os.getenv("MARKETS_PER_EXCHANGE"):
        overrides.setdefault("scan", {})["markets_per_exchange"] = int(os.getenv("MARKETS_PER_EXCHANGE"))
    if os.getenv("SCAN_SUMMARY_SAMPLE_PER_EXCHANGE"):
        overrides.setdefault("scan", {})["summary_sample_per_exchange"] = int(os.getenv("SCAN_SUMMARY_SAMPLE_PER_EXCHANGE"))

    if overrides:
        config = _deep_merge(config, overrides)

    trading_cfg = config.get("trading", {}) or {}
    if "enabled" in trading_cfg:
        config["trading_enabled"] = bool(trading_cfg.get("enabled"))
    elif "trading_enabled" in trading_cfg:
        config["trading_enabled"] = bool(trading_cfg.get("trading_enabled"))

    return config


def _runtime_mode(config: dict) -> str:
    trading = config.get("trading", {}) or {}
    mode = str(trading.get("mode") or config.get("TRADING_MODE") or os.getenv("TRADING_MODE") or "paper").strip().lower()
    return "live" if mode == "live" else "paper"


def _apply_runtime_paths(config: dict) -> dict:
    config = deepcopy(config)
    mode = _runtime_mode(config)
    runtime = config.setdefault("runtime", {})
    base_dir = Path(runtime.get("base_dir", config.get("data_dir", "data")))
    mode_dir = base_dir / mode
    runtime["mode"] = mode
    runtime["base_dir"] = str(base_dir)
    runtime["mode_dir"] = str(mode_dir)
    config["data_dir"] = str(mode_dir)
    config["log_dir"] = str(mode_dir)
    logging_cfg = config.setdefault("logging", {})
    logging_cfg["log_dir"] = str(mode_dir)
    strategy = config.get("strategy", {}) or {}
    if strategy.get("weather_observation_log_path", "").startswith("data/"):
        strategy["weather_observation_log_path"] = str(mode_dir / "weather_observations.jsonl")
    config["strategy"] = strategy
    return config


def load_config(config_path: str | Path | None = None) -> dict:
    """Load config from config.yaml with .env overrides."""
    if config_path is not None:
        config_path = Path(config_path)
    else:
        config_path = _find_config()

    if config_path and yaml:
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            logger.info(f"Loaded config from {config_path}")
        except Exception as e:
            logger.warning(f"Failed to load {config_path}: {e}")
            config = _default_config()
    else:
        config = _default_config()

    # Apply .env overrides
    config = _apply_env_overrides(config)
    config = _normalize_storage_config(config)
    config = _apply_runtime_paths(config)

    return config


def _default_config() -> dict:
    """Minimal default config if no config.yaml exists."""
    return {
        "openrouter": {
            "api_key_env": "OPENROUTER_API_KEY",
            "model": "google/gemini-2.5-flash",
            "daily_call_budget": 20,
        },
        "schedule": {
            "default_phases": {
                "quiet": {"max_hours_to_close": 999, "interval_seconds": 300, "researcher_enabled": False},
                "active": {"max_hours_to_close": 4, "interval_seconds": 120, "researcher_enabled": True},
                "hot": {"max_hours_to_close": 1, "interval_seconds": 30, "researcher_enabled": True},
                "live": {"max_hours_to_close": 0, "interval_seconds": 15, "researcher_enabled": False},
            },
        },
        "market_types": {
            "sports": {"enabled": True, "max_hours_to_close": 48},
        },
        "strategy": {
            "min_edge": 0.015,
            "min_confidence": 0.50,
            "enable_weather_observation_log": False,
            "weather_observation_log_path": "data/weather_observations.jsonl",
            "weather_observation_cooldown_seconds": 21600,
        },
        "runtime": {
            "base_dir": "data",
        },
        "storage": _default_storage_config(),
        "risk": {
            "kelly_fraction": 0.75,
            "max_position_pct": 0.20,
        },
    }
