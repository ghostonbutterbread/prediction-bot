"""Strategy-lane selection for the shared trade-decision core."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from typing import Any, Mapping

from bot.strategy_policy import coerce_strategy_policy, strategy_policy_status


EDGE_LANE = "edge"
CONFIDENCE_SLOW_PROFIT_LANE = "confidence_slow_profit"
HIDDEN_GEM_LANE = "hidden_gem"
HIDDEN_GEM_LANE_GATES_FEATURE = "hidden_gem_lane_gates"
LANE_SIZING_CAPS_FEATURE = "lane_sizing_caps"
DEFAULT_ENABLED_LANES = (EDGE_LANE, HIDDEN_GEM_LANE)
SUPPORTED_LANES = {EDGE_LANE, CONFIDENCE_SLOW_PROFIT_LANE, HIDDEN_GEM_LANE}


@dataclass(frozen=True)
class StrategyLaneDecision:
    lane_id: str
    allowed: bool
    reason_code: str
    effective_min_edge: float
    effective_min_confidence: float
    behavior_enabled: bool
    new_behavior_enabled: bool
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_strategy_lane_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "enabled_lanes": list(DEFAULT_ENABLED_LANES),
        "sizing": {
            EDGE_LANE: _default_lane_sizing_config(),
            HIDDEN_GEM_LANE: _default_lane_sizing_config(),
            CONFIDENCE_SLOW_PROFIT_LANE: _default_lane_sizing_config(),
        },
        "confidence_slow_profit": {
            "enabled": False,
            "min_edge": None,
            "min_confidence": None,
        },
    }


def normalize_strategy_lane_config(value: Any) -> dict[str, Any]:
    config = default_strategy_lane_config()
    if not isinstance(value, Mapping):
        return config

    config["enabled"] = bool(value.get("enabled", config["enabled"]))
    enabled_lanes = value.get("enabled_lanes", config["enabled_lanes"])
    if isinstance(enabled_lanes, str):
        enabled_lanes = [part.strip() for part in enabled_lanes.split(",")]
    config["enabled_lanes"] = [
        _normalize_lane_id(lane)
        for lane in (enabled_lanes or [])
        if _normalize_lane_id(lane) in SUPPORTED_LANES
    ] or list(DEFAULT_ENABLED_LANES)

    if isinstance(value.get("sizing"), Mapping):
        sizing = {lane: dict(meta) for lane, meta in config["sizing"].items()}
        for lane, raw_lane_sizing in value["sizing"].items():
            lane_id = _normalize_lane_id(lane)
            if lane_id in SUPPORTED_LANES and isinstance(raw_lane_sizing, Mapping):
                sizing[lane_id] = _normalize_lane_sizing_config(raw_lane_sizing)
        config["sizing"] = sizing

    slow_profit = dict(config["confidence_slow_profit"])
    if isinstance(value.get("confidence_slow_profit"), Mapping):
        raw_slow_profit = value["confidence_slow_profit"]
        slow_profit["enabled"] = bool(raw_slow_profit.get("enabled", slow_profit["enabled"]))
        slow_profit["min_edge"] = _coerce_optional_float(raw_slow_profit.get("min_edge"))
        slow_profit["min_confidence"] = _coerce_optional_float(raw_slow_profit.get("min_confidence"))
    config["confidence_slow_profit"] = slow_profit
    if config["enabled"] and slow_profit["enabled"] and CONFIDENCE_SLOW_PROFIT_LANE not in config["enabled_lanes"]:
        config["enabled_lanes"].append(CONFIDENCE_SLOW_PROFIT_LANE)
    return config


def select_strategy_lane(
    *,
    entry_price: float,
    win_probability: float,
    edge: float,
    confidence: float,
    min_edge: float,
    min_confidence: float,
    hidden_gem_entry_price_cap: float,
    config: Mapping[str, Any] | None = None,
    strategy_policy: Mapping[str, Any] | None = None,
) -> StrategyLaneDecision:
    lane_config = normalize_strategy_lane_config(config)
    policy = coerce_strategy_policy(strategy_policy)
    configured_behavior_enabled = bool(lane_config.get("enabled", False))
    beta_behavior_enabled = configured_behavior_enabled and policy.feature_enabled(HIDDEN_GEM_LANE_GATES_FEATURE)
    behavior_enabled = configured_behavior_enabled and policy.feature_enforced(HIDDEN_GEM_LANE_GATES_FEATURE)
    slow_profit_config = dict(lane_config.get("confidence_slow_profit") or {})
    slow_profit_explicitly_enabled = behavior_enabled and bool(slow_profit_config.get("enabled", False))
    beta_slow_profit_explicitly_enabled = beta_behavior_enabled and bool(slow_profit_config.get("enabled", False))

    lane_id = EDGE_LANE
    reason_code = "edge_lane_selected"
    effective_min_edge = float(min_edge)
    effective_min_confidence = float(min_confidence)
    new_behavior_enabled = False

    if entry_price <= hidden_gem_entry_price_cap:
        lane_id = HIDDEN_GEM_LANE
        reason_code = "hidden_gem_lane_selected"
    elif _slow_profit_matches(
        edge=edge,
        confidence=confidence,
        min_edge=min_edge,
        min_confidence=min_confidence,
        config=slow_profit_config,
        enabled=slow_profit_explicitly_enabled,
    ):
        lane_id = CONFIDENCE_SLOW_PROFIT_LANE
        reason_code = "confidence_slow_profit_lane_selected"
        effective_min_edge = float(slow_profit_config["min_edge"])
        effective_min_confidence = float(slow_profit_config["min_confidence"])
        new_behavior_enabled = True

    enabled_lanes = set(lane_config.get("enabled_lanes") or DEFAULT_ENABLED_LANES)
    allowed = True
    if behavior_enabled and lane_id not in enabled_lanes:
        allowed = False
        reason_code = "strategy_lane_disabled"
    beta_lane_id, beta_reason_code, beta_allowed, beta_effective_min_edge, beta_effective_min_confidence = _select_beta_lane(
        entry_price=entry_price,
        edge=edge,
        confidence=confidence,
        min_edge=min_edge,
        min_confidence=min_confidence,
        hidden_gem_entry_price_cap=hidden_gem_entry_price_cap,
        enabled_lanes=enabled_lanes,
        behavior_enabled=beta_behavior_enabled,
        slow_profit_config=slow_profit_config,
        slow_profit_enabled=beta_slow_profit_explicitly_enabled,
    )
    lane_sizing = _lane_sizing_evidence(lane_id, lane_config)
    beta_lane_sizing = _lane_sizing_evidence(beta_lane_id, lane_config)

    return StrategyLaneDecision(
        lane_id=lane_id,
        allowed=allowed,
        reason_code=reason_code,
        effective_min_edge=effective_min_edge,
        effective_min_confidence=effective_min_confidence,
        behavior_enabled=behavior_enabled,
        new_behavior_enabled=new_behavior_enabled,
        evidence={
            "entry_price": round(float(entry_price), 6),
            "win_probability": round(float(win_probability), 6),
            "edge": round(float(edge), 6),
            "confidence": round(float(confidence), 6),
            "base_min_edge": round(float(min_edge), 6),
            "base_min_confidence": round(float(min_confidence), 6),
            "enabled_lanes": sorted(enabled_lanes),
            "confidence_slow_profit_enabled": slow_profit_explicitly_enabled,
            "confidence_slow_profit_min_edge": slow_profit_config.get("min_edge"),
            "confidence_slow_profit_min_confidence": slow_profit_config.get("min_confidence"),
            "lane_sizing": lane_sizing,
            "strategy_policy": strategy_policy_status(policy),
            "beta_lane_gate": {
                "feature": HIDDEN_GEM_LANE_GATES_FEATURE,
                "configured_behavior_enabled": configured_behavior_enabled,
                "beta_behavior_enabled": beta_behavior_enabled,
                "beta_behavior_enforced": behavior_enabled,
                "confidence_slow_profit_enabled": beta_slow_profit_explicitly_enabled,
                "lane_id": beta_lane_id,
                "allowed": beta_allowed,
                "reason_code": beta_reason_code,
                "effective_min_edge": beta_effective_min_edge,
                "effective_min_confidence": beta_effective_min_confidence,
                "lane_sizing": beta_lane_sizing,
                "differs_from_final": (
                    beta_lane_id != lane_id
                    or beta_allowed != allowed
                    or beta_effective_min_edge != effective_min_edge
                    or beta_effective_min_confidence != effective_min_confidence
                    or beta_lane_sizing != lane_sizing
                ),
            },
        },
    )


def _select_beta_lane(
    *,
    entry_price: float,
    edge: float,
    confidence: float,
    min_edge: float,
    min_confidence: float,
    hidden_gem_entry_price_cap: float,
    enabled_lanes: set[str],
    behavior_enabled: bool,
    slow_profit_config: Mapping[str, Any],
    slow_profit_enabled: bool,
) -> tuple[str, str, bool, float, float]:
    lane_id = EDGE_LANE
    reason_code = "edge_lane_selected"
    effective_min_edge = float(min_edge)
    effective_min_confidence = float(min_confidence)
    if entry_price <= hidden_gem_entry_price_cap:
        lane_id = HIDDEN_GEM_LANE
        reason_code = "hidden_gem_lane_selected"
    elif _slow_profit_matches(
        edge=edge,
        confidence=confidence,
        min_edge=min_edge,
        min_confidence=min_confidence,
        config=slow_profit_config,
        enabled=slow_profit_enabled,
    ):
        lane_id = CONFIDENCE_SLOW_PROFIT_LANE
        reason_code = "confidence_slow_profit_lane_selected"
        effective_min_edge = float(slow_profit_config["min_edge"])
        effective_min_confidence = float(slow_profit_config["min_confidence"])

    allowed = True
    if behavior_enabled and lane_id not in enabled_lanes:
        allowed = False
        reason_code = "strategy_lane_disabled"
    return lane_id, reason_code, allowed, effective_min_edge, effective_min_confidence


def _slow_profit_matches(
    *,
    edge: float,
    confidence: float,
    min_edge: float,
    min_confidence: float,
    config: Mapping[str, Any],
    enabled: bool,
) -> bool:
    if not enabled:
        return False
    lane_min_edge = _coerce_optional_float(config.get("min_edge"))
    lane_min_confidence = _coerce_optional_float(config.get("min_confidence"))
    if lane_min_edge is None or lane_min_confidence is None:
        return False
    if lane_min_edge >= min_edge:
        return False
    if lane_min_confidence <= min_confidence:
        return False
    return lane_min_edge <= edge < min_edge and confidence >= lane_min_confidence


def _normalize_lane_id(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace("/", "_")


def _default_lane_sizing_config() -> dict[str, float | None]:
    return {
        "size_multiplier": None,
        "max_position_usd": None,
        "max_position_pct": None,
    }


def _normalize_lane_sizing_config(value: Mapping[str, Any]) -> dict[str, float | None]:
    sizing = _default_lane_sizing_config()
    sizing["size_multiplier"] = _coerce_optional_bounded_float(
        value.get("size_multiplier"),
        minimum=0.0,
        maximum=1.0,
    )
    sizing["max_position_usd"] = _coerce_optional_bounded_float(
        value.get("max_position_usd"),
        minimum=0.0,
    )
    sizing["max_position_pct"] = _coerce_optional_bounded_float(
        value.get("max_position_pct"),
        minimum=0.0,
        maximum=1.0,
    )
    return sizing


def _lane_sizing_evidence(lane_id: str, lane_config: Mapping[str, Any]) -> dict[str, Any]:
    sizing_by_lane = lane_config.get("sizing") if isinstance(lane_config, Mapping) else None
    raw_sizing = {}
    if isinstance(sizing_by_lane, Mapping) and isinstance(sizing_by_lane.get(lane_id), Mapping):
        raw_sizing = dict(sizing_by_lane.get(lane_id) or {})
    configured = any(raw_sizing.get(key) is not None for key in _default_lane_sizing_config())
    return {
        "lane_id": lane_id,
        "configured": configured,
        "metadata_only": True,
        "size_multiplier": raw_sizing.get("size_multiplier"),
        "max_position_usd": raw_sizing.get("max_position_usd"),
        "max_position_pct": raw_sizing.get("max_position_pct"),
    }


def _coerce_optional_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_bounded_float(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    coerced = _coerce_optional_float(value)
    if coerced is None or not isfinite(coerced):
        return None
    if minimum is not None:
        coerced = max(float(minimum), coerced)
    if maximum is not None:
        coerced = min(float(maximum), coerced)
    return coerced
