"""Strict market routing for execution-eligible market families."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Mapping

from bot.weather_market_risk import classify_weather_market

DEFAULT_ALLOWED_MARKET_ROUTES = ("weather.daily_temperature",)
DAILY_TEMPERATURE_ROUTE = "weather.daily_temperature"
DAILY_TEMPERATURE_HANDLER = "weather.daily_temperature.v1"
DAILY_TEMPERATURE_PREFIXES = ("KXHIGH", "KXHIGHT", "KXLOW", "KXLOWT", "KXMINTEMP")
LEGACY_DAILY_TEMPERATURE_RE = re.compile(
    r"^(?:HIGH|LOW)[A-Z0-9]+-\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)\d{1,2}(?:-|$)"
)


@dataclass(frozen=True)
class MarketRoute:
    allowed: bool
    group: str
    family: str | None
    subcategory: str | None
    handler_id: str | None
    reason_code: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_allowed_market_routes(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = [part.strip() for part in value.split(",")]
    else:
        raw = [str(part).strip() for part in (value or [])]
    return [part.lower() for part in raw if part]


def route_allowed(route: MarketRoute) -> bool:
    return bool(route and route.allowed)


def route_market(market_or_signal: Any, config: Mapping[str, Any] | None = None) -> MarketRoute:
    config = config or {}
    scan_cfg = (config.get("scan") or {}) if isinstance(config, Mapping) else {}
    allowed_routes = set(normalize_allowed_market_routes(scan_cfg.get("allowed_market_routes")))
    facts = _extract_market_facts(market_or_signal)
    route = _route_weather_daily_temperature(facts, allowed_routes)
    if route is not None:
        return route
    return MarketRoute(
        allowed=False,
        group="unknown",
        family=None,
        subcategory=None,
        handler_id=None,
        reason_code="unknown_market_route",
        evidence=facts,
    )


def _route_weather_daily_temperature(facts: dict[str, Any], allowed_routes: set[str]) -> MarketRoute | None:
    prefix_value = _daily_temperature_prefix_value(facts)
    shape = classify_weather_market(str(facts.get("question") or ""), str(facts.get("market_ticker") or facts.get("market_id") or ""))
    if shape == "unknown" and prefix_value:
        shape = _shape_from_daily_temperature_prefix(prefix_value)
    semantics_match = _has_temperature_semantics(facts, shape)
    evidence = {
        **facts,
        "prefix_match": bool(prefix_value),
        "prefix_value": prefix_value,
        "temperature_semantics_match": semantics_match,
        "shape": shape,
    }
    if not prefix_value:
        return None
    if not semantics_match:
        return MarketRoute(
            allowed=False,
            group="weather",
            family="daily_temperature",
            subcategory=None,
            handler_id=DAILY_TEMPERATURE_HANDLER,
            reason_code="daily_temperature_semantics_missing",
            evidence=evidence,
        )
    if shape not in {"tail_high", "tail_low", "bucket"}:
        return MarketRoute(
            allowed=False,
            group="weather",
            family="daily_temperature",
            subcategory=None,
            handler_id=DAILY_TEMPERATURE_HANDLER,
            reason_code="daily_temperature_shape_unknown",
            evidence=evidence,
        )
    if DAILY_TEMPERATURE_ROUTE not in allowed_routes:
        return MarketRoute(
            allowed=False,
            group="weather",
            family="daily_temperature",
            subcategory=shape,
            handler_id=DAILY_TEMPERATURE_HANDLER,
            reason_code="market_route_not_allowed",
            evidence=evidence,
        )
    return MarketRoute(
        allowed=True,
        group="weather",
        family="daily_temperature",
        subcategory=shape,
        handler_id=DAILY_TEMPERATURE_HANDLER,
        reason_code="allowed_weather_daily_temperature",
        evidence=evidence,
    )


def _extract_market_facts(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        data = dict(value)
        metadata = dict(data.get("metadata") or data.get("market_metadata") or {})
        market = data.get("_market")
        if market is not None:
            metadata = {**dict(getattr(market, "metadata", {}) or {}), **metadata}
        get = data.get
        market_id = get("market_id") or get("ticker") or get("id") or getattr(market, "id", "")
        question = get("question") or get("title") or getattr(market, "question", "")
        category = get("category") or metadata.get("category") or getattr(market, "category", "")
    else:
        metadata = dict(getattr(value, "metadata", {}) or {})
        market_id = getattr(value, "id", "") or getattr(value, "ticker", "")
        question = getattr(value, "question", "") or getattr(value, "title", "")
        category = getattr(value, "category", "")

    top_get = data.get if isinstance(value, Mapping) else (lambda _key, _default=None: _default)
    series = (
        top_get("series_ticker")
        or top_get("series")
        or metadata.get("series_ticker")
        or metadata.get("series")
        or metadata.get("seriesTicker")
        or category
        or ""
    )
    event_ticker = top_get("event_ticker") or metadata.get("event_ticker") or metadata.get("eventTicker") or ""
    return {
        "series_ticker": str(series or "").strip(),
        "event_ticker": str(event_ticker or "").strip(),
        "market_ticker": str(market_id or "").strip(),
        "market_id": str(market_id or "").strip(),
        "question": str(question or "").strip(),
        "category": str(category or "").strip(),
        "classification_reason": metadata.get("classification_reason"),
        "market_group": top_get("market_group") or metadata.get("market_group"),
        "market_family": top_get("market_family") or metadata.get("market_family"),
    }


def _daily_temperature_prefix_value(facts: dict[str, Any]) -> str | None:
    for key in ("series_ticker", "category", "market_ticker", "market_id", "event_ticker"):
        value = str(facts.get(key) or "").strip().upper()
        if not value:
            continue
        if value.startswith(DAILY_TEMPERATURE_PREFIXES):
            return value
        if LEGACY_DAILY_TEMPERATURE_RE.search(value):
            return value
    return None


def _shape_from_daily_temperature_prefix(prefix_value: str) -> str:
    upper = str(prefix_value or "").upper()
    if upper.startswith(("KXHIGH", "KXHIGHT")):
        return "tail_high"
    if upper.startswith("HIGH"):
        return "tail_high"
    if upper.startswith(("KXLOW", "KXLOWT", "KXMINTEMP")):
        return "tail_low"
    if upper.startswith("LOW"):
        return "tail_low"
    return "unknown"


def _has_temperature_semantics(facts: dict[str, Any], shape: str) -> bool:
    text = " ".join(
        str(facts.get(key) or "")
        for key in ("question", "category", "series_ticker", "market_ticker")
        if facts.get(key)
    ).lower()
    has_temperature_word = bool(re.search(r"(temp|temperature|degrees?|°)", text))
    has_temperature_direction = bool(re.search(r"(high|low|maximum|max|minimum|min)", text))
    if shape == "bucket":
        return has_temperature_word
    return has_temperature_word and (has_temperature_direction or shape in {"tail_high", "tail_low"})
