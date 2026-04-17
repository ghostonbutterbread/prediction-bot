from __future__ import annotations

import re
from dataclasses import dataclass

from .registry import WeatherRegistry


SPECIAL_CITY_ALIASES: dict[str, tuple[str, ...]] = {
    "miami_fl": ("miami", "mia"),
    "new_york_ny": ("new york", "new york city", "nyc"),
    "los_angeles_ca": ("los angeles", "lax"),
}

TICKER_CITY_ALIASES: dict[str, str] = {
    "MIAMI": "miami_fl",
    "MIA": "miami_fl",
    "NEWYORK": "new_york_ny",
    "NYC": "new_york_ny",
    "NY": "new_york_ny",
    "LOSANGELES": "los_angeles_ca",
    "LAX": "los_angeles_ca",
}

WEATHER_SERIES_PREFIXES = ("KXHIGHTEMP", "KXMINTEMP", "KXLOWT", "KXHIGHT", "KXHIGH", "KXLOW")
TEMPERATURE_QUESTION_ALIASES = (
    " temperature ",
    " temp ",
    " high temp ",
    " low temp ",
    " maximum ",
    " minimum ",
    " max temp ",
    " min temp ",
)


@dataclass(frozen=True)
class WeatherMarketContext:
    city_id: str
    city: str
    state: str
    primary_source_id: str | None = None


def _normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _normalize_ticker_fragment(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def looks_like_temperature_question(question: str) -> bool:
    normalized_question = f" {_normalize_text(question)} "
    return any(token in normalized_question for token in TEMPERATURE_QUESTION_ALIASES)


def _weather_series_suffix(value: str) -> str | None:
    normalized = _normalize_ticker_fragment(value)
    for prefix in WEATHER_SERIES_PREFIXES:
        if normalized.startswith(prefix):
            return normalized[len(prefix) :]
    return None


class WeatherMarketCityMapper:
    """Resolve obvious weather-city markets to registry-backed city ids."""

    def __init__(self, registry: WeatherRegistry | None = None):
        self.registry = registry or WeatherRegistry.from_file()
        registry_data = self.registry.as_dict()
        self._cities = {
            city["city_id"]: city
            for city in registry_data.get("cities", [])
            if isinstance(city, dict) and isinstance(city.get("city_id"), str)
        }
        self._aliases = self._build_aliases()
        self._ticker_aliases = self._build_ticker_aliases()

    def resolve(self, question: str, category: str = "") -> WeatherMarketContext | None:
        city_id = self.resolve_city_id(question, category)
        if not city_id:
            return None

        city = self._cities[city_id]
        primary_sources = city.get("trusted_primary", []) or []
        primary_source_id = primary_sources[0] if primary_sources else None
        return WeatherMarketContext(
            city_id=city["city_id"],
            city=city["city"],
            state=city["state"],
            primary_source_id=primary_source_id,
        )

    def resolve_city_id(self, question: str, category: str = "") -> str | None:
        normalized_question = f" {_normalize_text(question)} "
        for alias, city_id in self._aliases:
            if alias in normalized_question:
                return city_id

        if looks_like_temperature_question(question):
            city_id = self._resolve_category_city_id(category)
            if city_id:
                return city_id

        return None

    def _build_aliases(self) -> list[tuple[str, str]]:
        aliases: list[tuple[str, str]] = []
        for city_id, city in self._cities.items():
            variants = {
                city.get("city", ""),
                city_id.replace("_", " "),
            }
            variants.update(city.get("aliases", []) or [])
            variants.update(SPECIAL_CITY_ALIASES.get(city_id, ()))
            for variant in variants:
                normalized = _normalize_text(str(variant))
                if normalized:
                    aliases.append((f" {normalized} ", city_id))
        aliases.sort(key=lambda item: len(item[0]), reverse=True)
        return aliases

    def _build_ticker_aliases(self) -> list[tuple[str, str]]:
        aliases: dict[str, str] = {}
        for city_id, city in self._cities.items():
            for token in city.get("ticker_aliases", []) or []:
                normalized = _normalize_ticker_fragment(str(token))
                if normalized:
                    aliases[normalized] = city_id
        for token, city_id in TICKER_CITY_ALIASES.items():
            if city_id in self._cities:
                aliases.setdefault(_normalize_ticker_fragment(token), city_id)
        return sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True)

    def _resolve_category_city_id(self, category: str) -> str | None:
        suffix = _weather_series_suffix(category)
        if not suffix:
            return None

        for token, city_id in self._ticker_aliases:
            if suffix == token and city_id in self._cities:
                return city_id

        for token, city_id in self._ticker_aliases:
            if not suffix.endswith(token):
                continue
            prefix = suffix[: -len(token)]
            if prefix and any(prefix == other for other, _ in self._ticker_aliases):
                return city_id

        return None
