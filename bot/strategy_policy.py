"""Shared strategy policy config normalization.

This module intentionally only parses policy intent. Trading, paper, and
Prediction Lab behavior stays unchanged until callers explicitly consume it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


VALID_VERSIONS = {"stable", "beta"}
VALID_BETA_MODES = {"off", "shadow", "enforce"}
ACTIVE_BETA_MODES = {"shadow", "enforce"}
DEFAULT_FEATURES = {
    "weather_hidden_gem_evidence_card": False,
    "bucket_distribution_scoring": False,
    "hidden_gem_lane_gates": False,
}


class StrategyPolicy(dict):
    """Normalized, dict-compatible policy with helper accessors."""

    @property
    def is_beta(self) -> bool:
        """Compatibility alias for is_configured_beta."""
        return bool(self.get("is_beta", False))

    @property
    def is_configured_beta(self) -> bool:
        return bool(self.get("is_configured_beta", False))

    @property
    def is_active(self) -> bool:
        return bool(self.get("is_active", False))

    @property
    def is_shadow(self) -> bool:
        return bool(self.get("is_shadow", False))

    @property
    def is_enforce(self) -> bool:
        return bool(self.get("is_enforce", False))

    def feature_enabled(self, name: str) -> bool:
        if name not in DEFAULT_FEATURES:
            return False
        features = self.get("features", {}) or {}
        return bool(self.is_active and features.get(name, False))


def _as_string(value: Any) -> str:
    return str(value or "").strip().lower()


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return False


def _normalize_feature_flags(features: Mapping[str, Any] | None = None) -> dict[str, bool]:
    normalized_features = dict(DEFAULT_FEATURES)
    if isinstance(features, Mapping):
        for name, value in features.items():
            feature_name = str(name)
            if feature_name in DEFAULT_FEATURES:
                normalized_features[feature_name] = _as_bool(value)
    return normalized_features


def _stable_policy(configured_features: Mapping[str, Any] | None = None) -> StrategyPolicy:
    normalized_configured_features = _normalize_feature_flags(configured_features)
    return StrategyPolicy(
        {
            "version": "stable",
            "beta_mode": "off",
            "configured_features": normalized_configured_features,
            "features": dict(DEFAULT_FEATURES),
            "is_beta": False,
            "is_configured_beta": False,
            "is_active": False,
            "is_shadow": False,
            "is_enforce": False,
        }
    )


def normalize_strategy_policy(raw_policy: Any) -> StrategyPolicy:
    """Normalize strategy_policy config, failing closed on invalid input."""
    if not isinstance(raw_policy, Mapping):
        return _stable_policy()

    beta_cfg = raw_policy.get("beta", {}) or {}
    if not isinstance(beta_cfg, Mapping):
        return _stable_policy()

    features_cfg = beta_cfg.get("features", {}) or {}
    if not isinstance(features_cfg, Mapping):
        features_cfg = {}

    configured_features = _normalize_feature_flags(features_cfg)

    version = _as_string(raw_policy.get("version", "stable")) or "stable"
    mode = _as_string(beta_cfg.get("mode", raw_policy.get("beta_mode", "off"))) or "off"

    if version not in VALID_VERSIONS or mode not in VALID_BETA_MODES:
        return _stable_policy()

    if version != "beta":
        mode = "off"

    is_configured_beta = version == "beta"
    is_active = is_configured_beta and mode in ACTIVE_BETA_MODES
    is_shadow = is_active and mode == "shadow"
    is_enforce = is_active and mode == "enforce"
    active_features = configured_features if is_active else dict(DEFAULT_FEATURES)

    return StrategyPolicy(
        {
            "version": version,
            "beta_mode": mode,
            "configured_features": configured_features,
            "features": active_features,
            "is_beta": is_configured_beta,
            "is_configured_beta": is_configured_beta,
            "is_active": is_active,
            "is_shadow": is_shadow,
            "is_enforce": is_enforce,
        }
    )


__all__ = ["DEFAULT_FEATURES", "StrategyPolicy", "normalize_strategy_policy"]
