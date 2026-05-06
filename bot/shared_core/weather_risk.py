"""Shared-core weather risk shim plus pure evidence extraction helpers."""

from __future__ import annotations

from math import isfinite
from typing import Any, Mapping

from bot.weather.station_mapping import resolve_weather_station
from bot.weather_market_risk import (
    DEFAULT_WEATHER_RISK_POLICY,
    WeatherRiskAssessment,
    apply_weather_size_limits,
    assess_weather_market_risk,
    classify_weather_market,
    deep_merge_policy,
)

_EXACT_MAPPING_KEYS = (
    "station_id",
    "station_code",
    "station_cli",
    "official_station",
    "official_station_id",
    "resolution_station",
    "settlement_station",
    "nws_station",
    "kalshi_station",
    "weather_station_id",
    "weather_station_code",
)
_INFERRED_MAPPING_KEYS = (
    "city",
    "city_id",
    "weather_city",
    "weather_city_id",
    "weather_source_id",
    "source_id",
)
_NESTED_SIGNAL_KEYS = ("data", "weather_context", "weather_market_context", "weather", "metadata", "signal_details", "live")


def build_weather_source_confidence_evidence(signal: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return deterministic weather-risk evidence derived from an existing signal."""

    payload = dict(signal or {})
    station_resolution = resolve_weather_station(payload)
    station_mapping = _infer_station_mapping(payload, station_resolution=station_resolution)
    source_agreement = _derive_source_agreement(payload)
    distribution_probability = _extract_distribution_probability(payload)
    market_volume = _extract_volume(payload)
    volume_known = market_volume is not None and market_volume > 0
    weather_confidence = _derive_weather_confidence(
        payload,
        station_mapping=station_mapping,
        source_agreement=source_agreement,
    )

    return {
        "weather_station_mapping": station_mapping,
        "weather_station_resolution": {
            **station_resolution.to_dict(),
            "mapping": station_mapping,
        },
        "weather_confidence_score": weather_confidence,
        "source_agreement_score": source_agreement,
        "distribution_probability": distribution_probability,
        "market_volume": market_volume,
        "volume_known": volume_known,
    }


def _infer_station_mapping(
    signal: Mapping[str, Any],
    *,
    station_resolution,
) -> str:
    explicit = _normalize_station_mapping(
        signal.get("weather_station_mapping")
        or signal.get("station_mapping")
        or signal.get("station_mapping_quality")
    )
    if explicit is not None:
        return explicit

    resolved_mapping = _normalize_station_mapping(getattr(station_resolution, "mapping", None))
    if resolved_mapping is not None:
        return resolved_mapping

    question = str(signal.get("question") or signal.get("title") or "")
    ticker = str(signal.get("market_id") or signal.get("ticker") or "")
    if classify_weather_market(question, ticker) != "unknown":
        return "inferred"

    return "unknown"


def _derive_weather_confidence(
    signal: Mapping[str, Any],
    *,
    station_mapping: str,
    source_agreement: float,
) -> float:
    explicit = _bounded_float(signal.get("weather_confidence_score"))
    if explicit is not None:
        return explicit

    base = _first_bounded_float(
        signal,
        "weather_confidence",
        "confidence",
        nested=("weather_confidence", "confidence"),
        default=None,
    )
    if base is None:
        return 0.0

    agreement_for_confidence = source_agreement if source_agreement > 0 else base
    mapping_multiplier = {
        "exact": 1.0,
        "inferred": 0.8,
        "unknown": 0.55,
    }.get(station_mapping, 0.55)
    return round(max(0.0, min(1.0, min(base, agreement_for_confidence) * mapping_multiplier)), 4)


def _derive_source_agreement(signal: Mapping[str, Any]) -> float:
    explicit = _first_bounded_float(
        signal,
        "source_agreement_score",
        "agreement",
        nested=("source_agreement_score", "agreement"),
        default=None,
    )
    if explicit is not None:
        return explicit

    probabilities = _extract_probability_values(signal)
    if len(probabilities) < 2:
        return 0.0

    spread = max(probabilities) - min(probabilities)
    agreement = 1.0 - min(1.0, spread / 0.5)
    return round(max(0.0, min(1.0, agreement)), 4)


def _extract_distribution_probability(signal: Mapping[str, Any]) -> float | None:
    return _first_bounded_float(
        signal,
        "distribution_probability",
        nested=("distribution_probability",),
        default=None,
    )


def _extract_probability_values(signal: Mapping[str, Any]) -> list[float]:
    values: list[float] = []
    for key in ("signals", "source_probabilities", "validated_signals"):
        container = signal.get(key)
        if not isinstance(container, Mapping):
            continue
        for value in container.values():
            numeric = _bounded_float(value)
            if numeric is not None:
                values.append(numeric)
    return values


def _extract_volume(signal: Mapping[str, Any]) -> float | None:
    for key in ("market_volume", "volume", "liquidity"):
        value = _coerce_optional_float(signal.get(key), default=None)
        if value is not None:
            return value
    for nested_key in ("_market", "market", "metadata"):
        nested = signal.get(nested_key)
        if isinstance(nested, Mapping):
            for key in ("volume", "market_volume", "liquidity"):
                value = _coerce_optional_float(nested.get(key), default=None)
                if value is not None:
                    return value
        else:
            for key in ("volume", "liquidity"):
                value = _coerce_optional_float(getattr(nested, key, None), default=None)
                if value is not None:
                    return value
    return None


def _has_any_key(signal: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    for nested in _iter_search_mappings(signal):
        for key in keys:
            value = nested.get(key)
            if value not in (None, "", [], {}):
                return True
    return False


def _first_bounded_float(
    signal: Mapping[str, Any],
    *keys: str,
    nested: tuple[str, ...] = (),
    default: float | None = None,
) -> float | None:
    for nested_obj in _iter_search_mappings(signal):
        for key in (*keys, *nested):
            value = _bounded_float(nested_obj.get(key))
            if value is not None:
                return value
    return default


def _iter_search_mappings(signal: Mapping[str, Any]):
    seen: set[int] = set()

    def visit(mapping: Mapping[str, Any], depth: int):
        marker = id(mapping)
        if marker in seen:
            return
        seen.add(marker)
        yield mapping
        if depth <= 0:
            return
        for nested_key in _NESTED_SIGNAL_KEYS:
            nested_obj = mapping.get(nested_key)
            if isinstance(nested_obj, Mapping):
                yield from visit(nested_obj, depth - 1)
                for value in nested_obj.values():
                    if isinstance(value, Mapping):
                        yield from visit(value, depth - 1)

    yield from visit(signal, 3)


def _normalize_station_mapping(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"exact", "inferred", "unknown"}:
        return normalized
    return None


def _bounded_float(value: Any) -> float | None:
    numeric = _coerce_optional_float(value, default=None)
    if numeric is None:
        return None
    return max(0.0, min(1.0, numeric))


def _coerce_optional_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        coerced = float(value)
    except (TypeError, ValueError):
        return default
    return coerced if isfinite(coerced) else default


__all__ = [
    "DEFAULT_WEATHER_RISK_POLICY",
    "WeatherRiskAssessment",
    "apply_weather_size_limits",
    "assess_weather_market_risk",
    "build_weather_source_confidence_evidence",
    "classify_weather_market",
    "deep_merge_policy",
]
