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
    "LA": "los_angeles_ca",
    "SA": "san_antonio_tx",
    "SD": "san_diego_ca",
    "SF": "san_francisco_ca",
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


def _weather_series_component(value: str) -> str | None:
    """Separate the series from an event/contract without eating city letters."""
    match = re.fullmatch(
        r"(KX[A-Z0-9]+)(?:-\d{2}[A-Z]{3}\d{2}(?:-[TB]-?\d+(?:\.\d+)?)?)?",
        str(value or "").strip().upper(),
    )
    return match.group(1) if match else None


def _identity_values(category: str, market_id: str, metadata: dict) -> list[str]:
    return [value for value in (market_id, category, *(metadata.get(key) for key in
            ('ticker', 'market_id', 'series', 'series_ticker', 'event_ticker')))
            if isinstance(value, str) and value]


def resolve_weather_measurement_kind(question: str, category: str = "", *,
                                     market_id: str = "", market_metadata: dict | None = None) -> str | None:
    """Daily maximum/minimum identity, independent of above/below tail direction."""
    metadata = market_metadata if isinstance(market_metadata, dict) else {}
    kinds = set()
    for value in _identity_values(category, market_id, metadata):
        series = _weather_series_component(value)
        if series:
            if series.startswith(('KXHIGH',)):
                kinds.add('high')
            elif series.startswith(('KXLOW', 'KXMINTEMP')):
                kinds.add('low')
    # Secondary rules use this paired family description for both HIGH and LOW.
    # It asserts neither kind; retain every other word, including contradictions.
    secondary_rules = metadata.get('rules_secondary')
    if isinstance(secondary_rules, str):
        secondary_rules = re.sub(r'\bmaximum\s*/\s*minimum\s+temperature\b',
                                 'temperature', secondary_rules, flags=re.IGNORECASE)
    for value in (question, metadata.get('rules_primary'), secondary_rules,
                  metadata.get('market_kind')):
        if not isinstance(value, str):
            continue
        normalized = _normalize_text(value)
        if re.search(r'\b(?:high|highest|maximum|max)\b', normalized):
            kinds.add('high')
        if re.search(r'\b(?:low|lowest|minimum|min)\b', normalized):
            kinds.add('low')
    return next(iter(kinds)) if len(kinds) == 1 else None


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

    def resolve(self, question: str, category: str = "", *, market_id: str = "",
                market_metadata: dict | None = None) -> WeatherMarketContext | None:
        city_id = self.resolve_city_id(question, category, market_id=market_id,
                                       market_metadata=market_metadata)
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

    def resolve_city_id(self, question: str, category: str = "", *, market_id: str = "",
                        market_metadata: dict | None = None) -> str | None:
        """Require agreement of original identifiers and explicit city evidence.

        City resolution is forecast geography, not settlement-station authority.
        Official City/rules may identify a newly named series; a ticker fragment
        alone may not. Ambiguity never becomes a first/longest-match preference.
        """
        metadata = market_metadata if isinstance(market_metadata, dict) else {}
        matches = self._text_city_ids(question)
        official = set()
        custom = metadata.get('custom_strike')
        city_values = [metadata.get('city')]
        if isinstance(custom, dict):
            city_values.append(custom.get('City'))
        for value in city_values:
            if value:
                normalized = _normalize_text(str(value))
                found = {city_id for alias, city_id in self._aliases
                         if normalized in (alias.strip(),
                             f'{alias.strip()} {self._cities[city_id]["state"].lower()}')}
                if len(found) != 1:
                    return None
                official.update(found)
        if metadata.get('city_id'):
            if metadata['city_id'] not in self._cities:
                return None
            official.add(metadata['city_id'])
        for text in (question, metadata.get('rules_primary'), metadata.get('rules_secondary')):
            if isinstance(text, str):
                # Remove balanced emphasis to a fixed point: stripping an outer
                # layer can expose an inner delimiter beside the location syntax.
                # Every change removes delimiters, so no nesting-depth cap is needed.
                # Keep punctuation, station tokens and identifier-internal underscores.
                while True:
                    plain = re.sub(r'(?<!\w)(\*+|_+)(?=\S)(.+?)(?<=\S)\1(?!\w)', r'\2', text)
                    if plain == text:
                        break
                    text = plain
                official.update(self._text_city_ids(text))
                for location in re.finditer(
                    r'\b(?:temperature|temp)\s+(?:(?:recorded|measured)\s+)?(?:at|in)\s+'
                    r'(.+?)(?=\s*\(|\s+(?:for|on|is|will|be|according)\b|[.,;?]|$)', text, re.I,
                ):
                    name = location.group(1).strip()
                    # Station-only assertions remain the independent station
                    # resolver's responsibility, not inferred city aliases.
                    if not re.fullmatch(r'(?:CLI[A-Z0-9]{3,4}|K[A-Z0-9]{3,4})', name):
                        if not self._text_city_ids(name):
                            return None
                # Questions also put the named location before the metric.
                before_metric = re.search(
                    r'^\s*Will\s+(?:the\s+)?(.+?)\s+(?:(?:high|highest|low|lowest|maximum|minimum)\s+)?(?:temperature|temp)\b',
                    _normalize_text(text), re.I)
                if before_metric:
                    name = before_metric.group(1).strip()
                    if not re.fullmatch(r'(?:the\s+)?(?:daily\s+)?(?:high|highest|low|lowest|maximum|minimum)?|the', name, re.I):
                        if not self._text_city_ids(name):
                            return None
        matches.update(official)
        unresolved_series = False
        for value in _identity_values(category, market_id, metadata):
            city_id = self._resolve_category_city_id(value)
            if city_id:
                matches.add(city_id)
            elif (str(value).upper() not in WEATHER_SERIES_PREFIXES
                  and any(str(value).upper().startswith(prefix) for prefix in WEATHER_SERIES_PREFIXES)):
                unresolved_series = True
        if unresolved_series and not official:
            return None
        return next(iter(matches)) if len(matches) == 1 else None

    def _text_city_ids(self, value: str) -> set[str]:
        normalized = f" {_normalize_text(value)} "
        return {city_id for alias, city_id in self._aliases if alias in normalized}

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
        aliases: set[tuple[str, str]] = set()
        for city_id, city in self._cities.items():
            for token in city.get("ticker_aliases", []) or []:
                normalized = _normalize_ticker_fragment(str(token))
                if normalized:
                    aliases.add((normalized, city_id))
        for token, city_id in TICKER_CITY_ALIASES.items():
            if city_id in self._cities:
                aliases.add((_normalize_ticker_fragment(token), city_id))
        return sorted(aliases, key=lambda item: (-len(item[0]), item))

    def _resolve_category_city_id(self, category: str) -> str | None:
        series = _weather_series_component(category)
        matches = {city_id for prefix in WEATHER_SERIES_PREFIXES
                   for token, city_id in self._ticker_aliases
                   if series == prefix + token}
        return next(iter(matches)) if len(matches) == 1 else None
