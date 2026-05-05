"""Weather market shape and risk helpers.

This module is deliberately side-effect free: it only inspects an already-built
signal/context dict and returns metadata for the shared decision path.  The first
policy pass is conservative and config-driven so paper/live adapters can opt in
without adding network calls or station lookups here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Literal

MarketShape = Literal["bucket", "tail_low", "tail_high", "unknown"]
HiddenGemTier = Literal["none", "normal", "suspicious", "exceptional"]


DEFAULT_WEATHER_RISK_POLICY: dict[str, Any] = {
    "enabled": True,
    "narrow_bucket": {
        "enabled": True,
        "size_multiplier": 0.50,
        "max_position_pct": 0.02,
        "max_position_usd": 10.0,
        "volume_unknown_size_multiplier": 0.25,
        "low_volume_threshold": 500.0,
        "low_volume_size_multiplier": 0.50,
    },
    "hidden_gem": {
        "entry_price_cap": 0.05,
        "normal_multiple_min": 3.0,
        "normal_multiple_max": 10.0,
        "suspicious_multiple_max": 15.0,
        "suspicious_size_multiplier": 0.35,
        "exceptional_size_multiplier": 0.10,
        "exceptional_allow_tiny_probe": False,
        "exceptional_tiny_probe_multiplier": 0.05,
        "exceptional_requires": {
            "station_mapping": "exact",
            "min_weather_confidence": 0.90,
            "min_source_agreement": 0.85,
            "min_distribution_probability": 0.20,
            "allow_unknown_volume": False,
        },
    },
}


@dataclass(slots=True)
class WeatherRiskAssessment:
    shape: MarketShape
    probability_multiple: float | None = None
    hidden_gem_tier: HiddenGemTier = "none"
    volume_known: bool = False
    should_skip: bool = False
    reason_code: str | None = None
    reason: str | None = None
    size_multiplier: float = 1.0
    max_position_pct: float | None = None
    max_position_usd: float | None = None
    evidence_perfect: bool = False
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "probability_multiple": (
                round(self.probability_multiple, 4) if self.probability_multiple is not None else None
            ),
            "hidden_gem_tier": self.hidden_gem_tier,
            "volume_known": self.volume_known,
            "should_skip": self.should_skip,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "size_multiplier": round(self.size_multiplier, 4),
            "max_position_pct": self.max_position_pct,
            "max_position_usd": self.max_position_usd,
            "evidence_perfect": self.evidence_perfect,
            "flags": list(self.flags),
        }


def deep_merge_policy(overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Return default weather policy with nested dict overrides applied."""

    def merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        out = dict(base)
        for key, value in (patch or {}).items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = merge(out[key], value)
            else:
                out[key] = value
        return out

    return merge(DEFAULT_WEATHER_RISK_POLICY, overrides or {})


def classify_weather_market(question: str | None = None, ticker: str | None = None) -> MarketShape:
    """Classify a weather market as an exact bucket, low tail, high tail, or unknown."""

    text = " ".join(part for part in (question or "", ticker or "") if part).strip()
    if not text:
        return "unknown"
    lower = text.lower()

    # Exact/narrow buckets: 82-83°, 82° to 83°, ticker strike B82.5, etc.
    if re.search(r"\b-?\d+(?:\.\d+)?\s*(?:°|degrees?)?\s*(?:-|–|to)\s*-?\d+(?:\.\d+)?\s*(?:°|degrees?)?", lower):
        return "bucket"
    if ticker and re.search(r"-[Bb]-?\d+(?:\.\d+)?$", ticker):
        return "bucket"
    if ticker and re.search(r"-[Bb]-?\d+(?:\.\d+)?", ticker):
        return "bucket"

    # Tail direction from question text is more reliable than T tickers.
    if re.search(r"(<|less than|below|or below|under|at or below|no more than)\s*-?\d+", lower):
        return "tail_low"
    if re.search(r"(>|greater than|above|or above|over|exceed|exceeds|at least|at or above)\s*-?\d+", lower):
        return "tail_high"

    return "unknown"


def assess_weather_market_risk(
    signal: dict[str, Any],
    *,
    entry_price: float | None,
    win_probability: float | None,
    policy: dict[str, Any] | None = None,
) -> WeatherRiskAssessment:
    """Compute weather risk metadata and conservative size/skip hints.

    `signal` may include optional evidence keys:
    - weather_station_mapping: "exact" | "inferred" | "unknown"
    - weather_confidence_score: 0..1
    - source_agreement_score: 0..1
    - distribution_probability: 0..1
    - volume / market_volume / _market.volume
    """

    cfg = deep_merge_policy(policy)
    if not cfg.get("enabled", True):
        return WeatherRiskAssessment(shape="unknown")

    question = str(signal.get("question") or signal.get("title") or "")
    ticker = str(signal.get("market_id") or signal.get("ticker") or "")
    shape = classify_weather_market(question, ticker)
    assessment = WeatherRiskAssessment(shape=shape)

    entry = _coerce_float(entry_price)
    probability = _coerce_float(win_probability)
    if entry is not None and entry > 0 and probability is not None:
        assessment.probability_multiple = probability / entry

    volume = _extract_volume(signal)
    assessment.volume_known = volume is not None and volume > 0

    if shape == "bucket" and cfg.get("narrow_bucket", {}).get("enabled", True):
        bucket_cfg = cfg["narrow_bucket"]
        assessment.flags.append("narrow_bucket")
        assessment.size_multiplier *= _coerce_float(bucket_cfg.get("size_multiplier"), 1.0) or 1.0
        assessment.max_position_pct = _coerce_float(bucket_cfg.get("max_position_pct"))
        assessment.max_position_usd = _coerce_float(bucket_cfg.get("max_position_usd"))
        if not assessment.volume_known:
            assessment.flags.append("volume_unknown")
            assessment.size_multiplier *= _coerce_float(
                bucket_cfg.get("volume_unknown_size_multiplier"), 0.25
            ) or 0.25
        elif volume < (_coerce_float(bucket_cfg.get("low_volume_threshold"), 500.0) or 500.0):
            assessment.flags.append("low_volume")
            assessment.size_multiplier *= _coerce_float(bucket_cfg.get("low_volume_size_multiplier"), 0.50) or 0.50

    hidden_cfg = cfg.get("hidden_gem", {})
    entry_cap = _coerce_float(hidden_cfg.get("entry_price_cap"), 0.05) or 0.05
    normal_min = _coerce_float(hidden_cfg.get("normal_multiple_min"), 3.0) or 3.0
    normal_max = _coerce_float(hidden_cfg.get("normal_multiple_max"), 10.0) or 10.0
    suspicious_max = _coerce_float(hidden_cfg.get("suspicious_multiple_max"), 15.0) or 15.0

    multiple = assessment.probability_multiple
    if entry is not None and entry <= entry_cap and multiple is not None and multiple >= normal_min:
        assessment.flags.append("hidden_gem")
        if multiple < normal_max:
            assessment.hidden_gem_tier = "normal"
        elif multiple < suspicious_max:
            assessment.hidden_gem_tier = "suspicious"
            assessment.flags.append("extreme_disagreement_suspicious")
            assessment.size_multiplier *= _coerce_float(
                hidden_cfg.get("suspicious_size_multiplier"), 0.35
            ) or 0.35
        else:
            assessment.hidden_gem_tier = "exceptional"
            assessment.flags.append("extreme_disagreement_exceptional")
            assessment.evidence_perfect = _exceptional_evidence_passes(signal, hidden_cfg, assessment.volume_known)
            if assessment.evidence_perfect:
                assessment.size_multiplier *= _coerce_float(
                    hidden_cfg.get("exceptional_size_multiplier"), 0.10
                ) or 0.10
            elif hidden_cfg.get("exceptional_allow_tiny_probe", False):
                assessment.flags.append("exceptional_tiny_probe")
                assessment.size_multiplier *= _coerce_float(
                    hidden_cfg.get("exceptional_tiny_probe_multiplier"), 0.05
                ) or 0.05
            else:
                assessment.should_skip = True
                assessment.reason_code = "weather_extreme_disagreement_without_perfect_evidence"
                assessment.reason = "Exceptional hidden gem requires exact station, fresh/aligned sources, distribution support, and known volume"

    assessment.size_multiplier = max(0.0, min(1.0, assessment.size_multiplier))
    return assessment


def apply_weather_size_limits(requested_size: float, assessment: WeatherRiskAssessment, *, current_balance: float | None = None) -> float:
    """Apply weather assessment size multipliers and caps."""

    size = max(0.0, float(requested_size or 0.0)) * assessment.size_multiplier
    if assessment.max_position_pct is not None and current_balance is not None:
        size = min(size, max(0.0, float(current_balance)) * assessment.max_position_pct)
    if assessment.max_position_usd is not None:
        size = min(size, max(0.0, assessment.max_position_usd))
    return round(size, 4)


def _exceptional_evidence_passes(signal: dict[str, Any], hidden_cfg: dict[str, Any], volume_known: bool) -> bool:
    req = dict(hidden_cfg.get("exceptional_requires") or {})
    required_mapping = str(req.get("station_mapping", "exact")).lower()
    mapping = str(
        signal.get("weather_station_mapping")
        or signal.get("station_mapping")
        or signal.get("station_mapping_quality")
        or "unknown"
    ).lower()
    if required_mapping == "exact" and mapping != "exact":
        return False

    if not bool(req.get("allow_unknown_volume", False)) and not volume_known:
        return False

    weather_confidence = _coerce_float(signal.get("weather_confidence_score"), 0.0) or 0.0
    source_agreement = _coerce_float(signal.get("source_agreement_score"), 0.0) or 0.0
    distribution_probability = _coerce_float(signal.get("distribution_probability"), 0.0) or 0.0
    return (
        weather_confidence >= (_coerce_float(req.get("min_weather_confidence"), 0.90) or 0.90)
        and source_agreement >= (_coerce_float(req.get("min_source_agreement"), 0.85) or 0.85)
        and distribution_probability >= (_coerce_float(req.get("min_distribution_probability"), 0.20) or 0.20)
    )


def _extract_volume(signal: dict[str, Any]) -> float | None:
    for key in ("market_volume", "volume", "liquidity"):
        value = _coerce_float(signal.get(key), default=None)
        if value is not None:
            return value
    market = signal.get("_market")
    if isinstance(market, dict):
        for key in ("volume", "liquidity"):
            value = _coerce_float(market.get(key), default=None)
            if value is not None:
                return value
    return None


def _coerce_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
