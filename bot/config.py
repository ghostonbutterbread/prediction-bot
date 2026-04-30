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

from bot.strategy_policy import normalize_strategy_policy

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
        "allowed_market_groups": ["weather", "sports"],
    }


def _default_prediction_lab_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "paused": False,
        "mode": "seed_and_watch",
        "observer_mode": False,
        "groups": ["weather"],
        "max_markets_per_run": 1000,
        "max_new_predictions_per_seed": 500,
        "score_only": True,
        "experiment_id": "default",
        "strategy_version": "v1",
        "hypothetical_notional_mode": "flat",
        "paper_lab_mode": "opportunity",
        "flat_notional_usd": 10.0,
        "opportunity_bankroll_usd": 100.0,
        "fresh_wallet_bankroll_usd": 100.0,
        "use_sizing_logic": False,
        "use_shared_pipeline": False,
        "min_confidence_to_record": 0.0,
        "min_edge_to_record": 0.0,
        "record_all_scored": True,
        "seed_daily_temp_first": True,
        "allow_non_weather": False,
        "disable_news": True,
        "disable_social": True,
        "disable_ai": True,
        "continue_collecting": False,
        "collector_interval_seconds": 900,
        "collector_record_market_snapshots": True,
        "collector_record_predictions": True,
        "collector_fetch_mode": "direct_markets",
        "collector_direct_page_size": 200,
        "collector_max_pages": 10,
        "collection_storage_cap_gb": 5,
        "collection_warning_threshold_pct": 90,
        "auto_pause_collection_on_storage_cap": True,
        "resolve_interval_seconds": 1800,
        "send_telegram_updates": True,
        "telegram_summary_on_pause": True,
    }


def _default_parity_mode_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "comparison_mode": "production",
        "record_revalidation_snapshot": True,
        "require_book_prices": False,
        "fallback_to_signal_prices": True,
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
    allowed_groups = scan.get("allowed_market_groups") or []
    if isinstance(allowed_groups, str):
        allowed_groups = [part.strip() for part in allowed_groups.split(",") if part.strip()]
    scan["allowed_market_groups"] = [str(group).strip().lower() for group in allowed_groups if str(group).strip()]
    config["scan"] = scan

    parity_mode = _deep_merge(_default_parity_mode_config(), config.get("parity_mode", {}) or {})
    parity_mode["enabled"] = bool(parity_mode.get("enabled", False))
    comparison_mode = str(
        parity_mode.get("comparison_mode", parity_mode.get("risk_mode", "production")) or "production"
    ).strip().lower()
    if comparison_mode not in {"production", "identical_risk"}:
        comparison_mode = "identical_risk" if comparison_mode in {"identical", "matched", "paper_equivalent"} else "production"
    parity_mode["comparison_mode"] = comparison_mode
    parity_mode["record_revalidation_snapshot"] = bool(parity_mode.get("record_revalidation_snapshot", True))
    parity_mode["require_book_prices"] = bool(parity_mode.get("require_book_prices", False))
    parity_mode["fallback_to_signal_prices"] = bool(parity_mode.get("fallback_to_signal_prices", True))
    if parity_mode["require_book_prices"]:
        parity_mode["fallback_to_signal_prices"] = False
    config["parity_mode"] = parity_mode

    raw_prediction_lab = config.get("prediction_lab", {}) or {}
    prediction_lab = _deep_merge(_default_prediction_lab_config(), raw_prediction_lab)
    groups = prediction_lab.get("groups") or []
    if isinstance(groups, str):
        groups = [part.strip() for part in groups.split(",") if part.strip()]
    prediction_lab["groups"] = [str(group).strip().lower() for group in groups if str(group).strip()]
    if "weather_only_daily_temp_first" in prediction_lab and "seed_daily_temp_first" not in prediction_lab:
        prediction_lab["seed_daily_temp_first"] = prediction_lab.get("weather_only_daily_temp_first")
    prediction_lab.pop("weather_only_daily_temp_first", None)

    prediction_lab["enabled"] = bool(prediction_lab.get("enabled", True))
    prediction_lab["paused"] = bool(prediction_lab.get("paused", False))
    prediction_lab["mode"] = str(prediction_lab.get("mode", "seed_and_watch") or "seed_and_watch").lower()
    prediction_lab["observer_mode"] = bool(prediction_lab.get("observer_mode", False))
    prediction_lab["max_markets_per_run"] = max(1, int(prediction_lab.get("max_markets_per_run", 1000) or 1000))
    prediction_lab["max_new_predictions_per_seed"] = max(1, int(prediction_lab.get("max_new_predictions_per_seed", 500) or 500))
    prediction_lab["experiment_id"] = str(prediction_lab.get("experiment_id", "default") or "default")
    prediction_lab["strategy_version"] = str(prediction_lab.get("strategy_version", "v1") or "v1")
    hypothetical_mode = str(prediction_lab.get("hypothetical_notional_mode", "flat") or "flat").strip().lower()
    prediction_lab["hypothetical_notional_mode"] = (
        "fresh_kelly" if hypothetical_mode in {"fresh_kelly", "kelly", "opportunity", "paper_lab"} else "flat"
    )
    paper_lab_mode = str(prediction_lab.get("paper_lab_mode", "opportunity") or "opportunity").strip().lower()
    prediction_lab["paper_lab_mode"] = "opportunity" if paper_lab_mode in {"opportunity", "paper_lab"} else paper_lab_mode
    prediction_lab["flat_notional_usd"] = float(prediction_lab.get("flat_notional_usd", 10.0) or 10.0)
    if "opportunity_bankroll_usd" in raw_prediction_lab:
        opportunity_bankroll = raw_prediction_lab.get("opportunity_bankroll_usd")
    else:
        opportunity_bankroll = prediction_lab.get("fresh_wallet_bankroll_usd", 100.0)
    opportunity_bankroll_value = float(100.0 if opportunity_bankroll is None else opportunity_bankroll)
    prediction_lab["opportunity_bankroll_usd"] = opportunity_bankroll_value
    if "fresh_wallet_bankroll_usd" in raw_prediction_lab:
        fresh_wallet_bankroll = raw_prediction_lab.get("fresh_wallet_bankroll_usd")
        prediction_lab["fresh_wallet_bankroll_usd"] = float(100.0 if fresh_wallet_bankroll is None else fresh_wallet_bankroll)
    else:
        prediction_lab["fresh_wallet_bankroll_usd"] = opportunity_bankroll_value
    prediction_lab["min_confidence_to_record"] = float(prediction_lab.get("min_confidence_to_record", 0.0) or 0.0)
    prediction_lab["min_edge_to_record"] = float(prediction_lab.get("min_edge_to_record", 0.0) or 0.0)
    prediction_lab["record_all_scored"] = bool(prediction_lab.get("record_all_scored", True))
    prediction_lab["use_shared_pipeline"] = bool(prediction_lab.get("use_shared_pipeline", False))
    prediction_lab["seed_daily_temp_first"] = bool(prediction_lab.get("seed_daily_temp_first", True))
    prediction_lab["allow_non_weather"] = bool(prediction_lab.get("allow_non_weather", False))
    prediction_lab["disable_news"] = bool(prediction_lab.get("disable_news", True))
    prediction_lab["disable_social"] = bool(prediction_lab.get("disable_social", True))
    prediction_lab["disable_ai"] = bool(prediction_lab.get("disable_ai", True))
    prediction_lab["continue_collecting"] = bool(prediction_lab.get("continue_collecting", False))
    prediction_lab["collector_interval_seconds"] = max(1, int(prediction_lab.get("collector_interval_seconds", 900) or 900))
    prediction_lab["collector_record_market_snapshots"] = bool(prediction_lab.get("collector_record_market_snapshots", True))
    prediction_lab["collector_record_predictions"] = bool(prediction_lab.get("collector_record_predictions", True))
    prediction_lab["collector_fetch_mode"] = str(prediction_lab.get("collector_fetch_mode", "direct_markets") or "direct_markets").lower()
    prediction_lab["collector_direct_page_size"] = max(1, int(prediction_lab.get("collector_direct_page_size", 200) or 200))
    prediction_lab["collector_max_pages"] = max(1, int(prediction_lab.get("collector_max_pages", 10) or 10))
    prediction_lab["collection_storage_cap_gb"] = float(prediction_lab.get("collection_storage_cap_gb", 5.0) or 5.0)
    prediction_lab["collection_warning_threshold_pct"] = float(prediction_lab.get("collection_warning_threshold_pct", 90) or 90)
    prediction_lab["auto_pause_collection_on_storage_cap"] = bool(prediction_lab.get("auto_pause_collection_on_storage_cap", True))
    prediction_lab["resolve_interval_seconds"] = max(1, int(prediction_lab.get("resolve_interval_seconds", 1800) or 1800))
    prediction_lab["send_telegram_updates"] = bool(prediction_lab.get("send_telegram_updates", True))
    prediction_lab["telegram_summary_on_pause"] = bool(prediction_lab.get("telegram_summary_on_pause", True))
    config["prediction_lab"] = prediction_lab
    return config


def _normalize_strategy_policy_config(config: dict) -> dict:
    config["strategy_policy_normalized"] = normalize_strategy_policy(config.get("strategy_policy", {}) or {})
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
    if os.getenv("ALLOWED_MARKET_GROUPS"):
        overrides.setdefault("scan", {})["allowed_market_groups"] = os.getenv("ALLOWED_MARKET_GROUPS")
    if os.getenv("PREDICTION_LAB_GROUPS"):
        overrides.setdefault("prediction_lab", {})["groups"] = os.getenv("PREDICTION_LAB_GROUPS")
    if os.getenv("PREDICTION_LAB_MAX_MARKETS"):
        overrides.setdefault("prediction_lab", {})["max_markets_per_run"] = int(os.getenv("PREDICTION_LAB_MAX_MARKETS"))

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


def get_runtime_mode(config: dict) -> str:
    return _runtime_mode(config)


def get_parity_comparison_mode(config: dict) -> str:
    parity_mode = config.get("parity_mode", {}) or {}
    mode = str(parity_mode.get("comparison_mode", "production") or "production").strip().lower()
    return "identical_risk" if mode == "identical_risk" else "production"


def get_operating_mode_label(config: dict) -> str:
    runtime_mode = get_runtime_mode(config)
    parity_mode = config.get("parity_mode", {}) or {}
    parity_enabled = bool(parity_mode.get("enabled", False))
    comparison_mode = get_parity_comparison_mode(config)

    if runtime_mode == "live":
        return "identical-risk comparison" if parity_enabled and comparison_mode == "identical_risk" else "live"
    return "parity paper" if parity_enabled else "normal paper"


def ensure_mode_storage_dir(path: str | Path, mode: str) -> Path:
    base = Path(path)
    normalized_mode = "live" if str(mode or "").strip().lower() == "live" else "paper"
    if base.name == normalized_mode:
        return base
    if base.name in {"paper", "live"}:
        return base.parent / normalized_mode
    return base / normalized_mode


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
    config = _normalize_strategy_policy_config(config)
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
        "parity_mode": _default_parity_mode_config(),
        "risk": {
            "kelly_fraction": 0.75,
            "max_position_pct": 0.20,
        },
    }
