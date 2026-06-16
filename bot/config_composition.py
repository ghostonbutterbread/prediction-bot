"""Config composition helpers for small shadow config profiles."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping


CONFIG_COMPOSITION_KEY = "config_composition"
_MISSING = object()

_SHADOW_ALERTS_DISABLED = {
    "enabled": False,
    "trade_events": False,
    "single_trade_events": False,
    "resolution_events": False,
    "reconciliation_events": False,
    "status_events": False,
    "scan_summaries": False,
    "telegram_enabled": False,
}


BUILTIN_OVERLAYS: dict[str, dict[str, Any]] = {
    "beta_shadow_observability_all": {
        "strategy_policy": {
            "version": "beta",
            "beta": {
                "mode": "shadow",
                "features": {
                    "weather_hidden_gem_evidence_card": True,
                    "bucket_distribution_scoring": True,
                    "hidden_gem_lane_gates": True,
                    "confidence_slow_profit": True,
                    "lane_sizing_caps": True,
                },
            },
        },
        "strategy_lanes": {
            "enabled": True,
            "enabled_lanes": [
                "edge",
                "hidden_gem",
                "confidence_slow_profit",
            ],
            "sizing": {
                "edge": {
                    "max_position_usd": 5.0,
                    "max_position_pct": 0.05,
                },
                "hidden_gem": {
                    "size_multiplier": 0.5,
                    "max_position_usd": 3.0,
                    "max_position_pct": 0.03,
                },
                "confidence_slow_profit": {
                    "size_multiplier": 0.5,
                    "max_position_usd": 2.0,
                    "max_position_pct": 0.02,
                },
            },
            "confidence_slow_profit": {
                "enabled": True,
                "min_edge": 0.02,
                "min_confidence": 0.75,
            },
        },
        "scan": {
            "allowed_market_groups": ["weather"],
            "allowed_market_routes": ["weather.daily_temperature"],
        },
        "strategy": {
            "enable_news": False,
            "enable_social": False,
            "enable_ai": False,
        },
    },
    "paper_beta_shadow_runtime": {
        "runtime": {
            "base_dir": "data/beta_shadow",
            "isolated": True,
        },
        "storage": {
            "logs": {
                "enabled": True,
                "auto_prune": False,
                "include_paths": [
                    "data/beta_shadow/paper/paper_loop.log",
                    "data/beta_shadow/paper/prediction_lab/",
                    "data/beta_shadow/paper/archive/ops/",
                ],
                "exclude_paths": [
                    "data/paper/",
                    "data/live/",
                    "logs/",
                ],
            },
        },
        "trading": {
            "mode": "paper",
            "enabled": True,
        },
        "alerts": _SHADOW_ALERTS_DISABLED,
        "paper": {
            "shared_market_runtime_enabled": True,
            "shared_market_runtime_instance_id": "paper-beta-shadow-weather",
            "shared_market_max_snapshot_age_seconds": 1200,
            "shared_market_desired_interval_seconds": 900,
        },
        "shared_market": {
            "enabled": True,
            "runtime_root": "data/beta_shadow/shared_market_runtime",
            "default_interval_seconds": 900,
            "min_interval_seconds": 300,
            "publisher_lease_timeout_seconds": 300,
            "consumer_timeout_seconds": 300,
            "snapshot_ttl_seconds": 1200,
            "stop_when_idle": True,
        },
    },
    "prediction_lab_beta_shadow_runtime": {
        "runtime": {
            "base_dir": "data/beta_shadow",
            "isolated": True,
        },
        "storage": {
            "logs": {
                "enabled": True,
                "auto_prune": False,
                "include_paths": [
                    "data/beta_shadow/paper/paper_loop.log",
                    "data/beta_shadow/paper/prediction_lab/",
                    "data/beta_shadow/paper/archive/ops/",
                ],
                "exclude_paths": [
                    "data/paper/",
                    "data/live/",
                    "logs/",
                ],
            },
        },
        "trading": {
            "mode": "paper",
            "enabled": False,
            "trading_enabled": False,
        },
        "trading_enabled": False,
        "alerts": _SHADOW_ALERTS_DISABLED,
        "prediction_lab": {
            "enabled": True,
            "paused": False,
            "mode": "collector",
            "observer_mode": True,
            "groups": ["weather"],
            "max_markets_per_run": 1000,
            "max_new_predictions_per_seed": 500,
            "record_all_scored": True,
            "seed_daily_temp_first": True,
            "allow_non_weather": False,
            "replay_default_months": 2,
            "score_only": True,
            "hypothetical_notional_mode": "flat",
            "flat_notional_usd": 10,
            "use_sizing_logic": False,
            "use_shared_pipeline": True,
            "experiment_id": "weather-beta-shadow",
            "strategy_version": "beta-shadow",
            "min_confidence_to_record": 0.0,
            "min_edge_to_record": 0.0,
            "disable_news": True,
            "disable_social": True,
            "disable_ai": True,
            "continue_collecting": True,
            "collector_interval_seconds": 900,
            "shared_market_runtime_enabled": True,
            "shared_market_runtime_instance_id": "collector-beta-shadow-weather",
            "collector_fetch_mode": "direct_markets",
            "collector_direct_page_size": 200,
            "collector_max_pages": 10,
            "collector_record_market_snapshots": True,
            "collector_record_predictions": True,
            "collection_storage_cap_gb": 100,
            "collection_warning_threshold_pct": 90,
            "auto_pause_collection_on_storage_cap": True,
            "resolve_interval_seconds": 1800,
            "send_telegram_updates": False,
            "telegram_summary_on_pause": False,
        },
        "shared_market": {
            "enabled": True,
            "runtime_root": "data/beta_shadow/shared_market_runtime",
            "default_interval_seconds": 900,
            "min_interval_seconds": 300,
            "publisher_lease_timeout_seconds": 300,
            "consumer_timeout_seconds": 300,
            "snapshot_ttl_seconds": 1200,
            "stop_when_idle": True,
        },
    },
}


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Return base deeply merged with override without mutating either input."""
    result = deepcopy(dict(base))
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, Mapping):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _normalize_overlay_names(overlays: Any) -> list[str]:
    if overlays is None:
        return []
    if isinstance(overlays, str):
        return [overlays]
    if isinstance(overlays, Mapping):
        raise ValueError("config_composition.overlays must be a string or list of strings")
    if not isinstance(overlays, (list, tuple)):
        raise ValueError("config_composition.overlays must be a string or list of strings")

    overlay_names: list[str] = []
    for overlay_name in overlays:
        if not isinstance(overlay_name, str) or not overlay_name.strip():
            raise ValueError("config_composition.overlays must contain only non-empty strings")
        overlay_names.append(overlay_name)
    return overlay_names


def compose_config(
    config: Mapping[str, Any],
    *,
    config_path: str | Path | None,
    read_config: Callable[[Path], Mapping[str, Any]],
) -> dict[str, Any]:
    """Materialize a config_composition block into one concrete config dict."""
    explicit_config = dict(config)
    composition = explicit_config.pop(CONFIG_COMPOSITION_KEY, _MISSING)
    if composition is _MISSING:
        return deepcopy(explicit_config)
    if not isinstance(composition, Mapping):
        raise ValueError("config_composition must be a mapping")

    base = composition.get("base")
    if base is not None and (not isinstance(base, str) or not base.strip()):
        raise ValueError("config_composition.base must be a non-empty string")
    overlay_names = _normalize_overlay_names(composition.get("overlays"))
    if not base and not overlay_names:
        raise ValueError("config_composition must define base and/or overlays")

    materialized: dict[str, Any] = {}
    if base:
        if config_path is None:
            raise ValueError("config_composition.base requires a config_path")
        base_path = Path(base)
        if not base_path.is_absolute():
            base_path = Path(config_path).parent / base_path
        materialized = compose_config(
            read_config(base_path) or {},
            config_path=base_path,
            read_config=read_config,
        )

    for overlay_name in overlay_names:
        overlay = BUILTIN_OVERLAYS.get(overlay_name)
        if overlay is None:
            raise ValueError(f"Unknown config_composition overlay: {overlay_name}")
        materialized = deep_merge(materialized, overlay)

    materialized = deep_merge(
        materialized,
        {
            "config_profile": {
                "base": base or "",
                "overlays": overlay_names,
            }
        },
    )
    return deep_merge(materialized, explicit_config)
