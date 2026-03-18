"""Config loader — reads config.yaml with .env override support.

Usage:
    from bot.config import load_config
    config = load_config()  # reads config.yaml, applies .env overrides

Config.yaml is the source of truth. .env values override for backward
compatibility with the existing setup.
"""

import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

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
    if strategy:
        overrides["strategy"] = strategy

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

    if overrides:
        config = _deep_merge(config, overrides)

    return config


def load_config() -> dict:
    """Load config from config.yaml with .env overrides."""
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
        },
        "risk": {
            "kelly_fraction": 0.75,
            "max_position_pct": 0.20,
        },
    }
