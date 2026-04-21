"""Canonical market classification helpers shared across scanning, reporting, and lab ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

WEATHER_KEYWORDS = (
    "temp",
    "temperature",
    "forecast",
    "rain",
    "snow",
    "wind",
    "hurricane",
    "high temp",
    "low temp",
    "degrees",
    "weather",
)

SPORTS_KEYWORDS = (
    "nba",
    "nfl",
    "mlb",
    "nhl",
    "soccer",
    "wnba",
    "ncaa",
    "game",
    "match",
    "player",
    "points",
    "rebounds",
    "assists",
    "touchdown",
    "goal",
)

DAILY_TEMP_SERIES_PREFIXES = (
    "kxhigh",
    "kxhight",
    "kxlow",
    "kxlowt",
    "kxmintemp",
)


@dataclass(frozen=True)
class MarketClassification:
    market_group: str
    family: Optional[str] = None
    reason: Optional[str] = None


def classify_market(*, market_id: str = "", question: str = "", category: str = "", series: str = "", event_ticker: str = "") -> Optional[MarketClassification]:
    normalized_category = str(category or "").lower()
    normalized_question = str(question or "").lower()
    normalized_series = str(series or "").lower()
    normalized_event_ticker = str(event_ticker or "").lower()
    normalized_market_id = str(market_id or "").lower()
    combined = " ".join(
        part for part in [normalized_category, normalized_question, normalized_series, normalized_event_ticker, normalized_market_id] if part
    )

    if "weather" in normalized_category or any(token in combined for token in WEATHER_KEYWORDS):
        return MarketClassification(
            market_group="weather",
            family=_infer_weather_family(normalized_question, normalized_series, normalized_market_id),
            reason="weather_keyword",
        )

    if any(token in combined for token in SPORTS_KEYWORDS):
        return MarketClassification(market_group="sports", family="sports_general", reason="sports_keyword")

    return None


def is_weather_market(*, market_id: str = "", question: str = "", category: str = "", series: str = "", event_ticker: str = "") -> bool:
    classification = classify_market(
        market_id=market_id,
        question=question,
        category=category,
        series=series,
        event_ticker=event_ticker,
    )
    return bool(classification and classification.market_group == "weather")


def classify_market_object(market: Any) -> Optional[MarketClassification]:
    metadata = dict(getattr(market, "metadata", {}) or {})
    return classify_market(
        market_id=getattr(market, "id", "") or "",
        question=getattr(market, "question", "") or "",
        category=getattr(market, "category", "") or "",
        series=metadata.get("series", "") or "",
        event_ticker=metadata.get("event_ticker", "") or "",
    )


def apply_classification_metadata(market: Any) -> Optional[MarketClassification]:
    classification = classify_market_object(market)
    metadata = dict(getattr(market, "metadata", {}) or {})
    if classification is None:
        market.metadata = metadata
        return None
    metadata["market_group"] = classification.market_group
    if classification.family:
        metadata["market_family"] = classification.family
    if classification.reason:
        metadata["classification_reason"] = classification.reason
    market.metadata = metadata
    return classification


def _infer_weather_family(question: str, series: str, market_id: str) -> str:
    combined = " ".join(part for part in [question, series, market_id] if part)
    if any(prefix in series for prefix in DAILY_TEMP_SERIES_PREFIXES) or any(token in combined for token in ("high temp", "low temp", "temperature", "degrees")):
        return "daily_temperature"
    if any(token in combined for token in ("rain", "precip", "snowfall", "snow")):
        return "precipitation"
    if any(token in combined for token in ("wind", "gust")):
        return "wind"
    if any(token in combined for token in ("hurricane", "storm", "tornado")):
        return "storm"
    return "weather_general"
