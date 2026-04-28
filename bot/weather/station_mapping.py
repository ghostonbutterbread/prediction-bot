from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
from typing import Any, Mapping

from .registry import WeatherRegistry


WEATHER_SERIES_PREFIXES = ("KXLOWT", "KXHIGHT", "KXHIGH", "KXLOW")
STATION_ID_PATTERN = re.compile(r"\b(K[A-Z0-9]{3,4})\b")
STATION_CLI_PATTERN = re.compile(r"\bCLI([A-Z0-9]{3,4})\b")


@dataclass(frozen=True)
class WeatherStationResolution:
    mapping: str
    city_code: str | None = None
    city_id: str | None = None
    city: str | None = None
    state: str | None = None
    station_id: str | None = None
    station_cli: str | None = None
    source: str | None = None
    reason: str | None = None
    matched_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mapping": self.mapping,
            "city_code": self.city_code,
            "city_id": self.city_id,
            "city": self.city,
            "state": self.state,
            "station_id": self.station_id,
            "station_cli": self.station_cli,
            "source": self.source,
            "reason": self.reason,
            "matched_from": self.matched_from,
        }


STATIC_BASELINE_STATION_MAPPINGS: dict[str, dict[str, str]] = {
    "ATL": {
        "mapping": "exact",
        "city_id": "atlanta_ga",
        "station_id": "KATL",
        "station_cli": "ATL",
        "reason": "Static baseline cache for Atlanta daily-temperature Kalshi markets.",
    },
    "AUS": {
        "mapping": "exact",
        "city_id": "austin_tx",
        "station_id": "KAUS",
        "station_cli": "AUS",
        "reason": "Static baseline cache for Austin daily-temperature Kalshi markets.",
    },
    "CHI": {
        "mapping": "inferred",
        "city_id": "chicago_il",
        "reason": "Ticker city code maps Chicago, but the exact settlement station is not frozen here.",
    },
    "DAL": {
        "mapping": "inferred",
        "city_id": "dallas_tx",
        "reason": "Ticker city code maps Dallas, but local static sources do not justify an exact station claim.",
    },
    "DEN": {
        "mapping": "exact",
        "city_id": "denver_co",
        "station_id": "KDEN",
        "station_cli": "DEN",
        "reason": "Static baseline cache for Denver daily-temperature Kalshi markets.",
    },
    "HOU": {
        "mapping": "inferred",
        "city_id": "houston_tx",
        "reason": "Houston ticker code is city-level here; exact station is left unresolved.",
    },
    "LAX": {
        "mapping": "exact",
        "city_id": "los_angeles_ca",
        "station_id": "KLAX",
        "station_cli": "LAX",
        "reason": "Static baseline cache for Los Angeles daily-temperature Kalshi markets.",
    },
    "LV": {
        "mapping": "inferred",
        "city_id": "las_vegas_nv",
        "reason": "Las Vegas ticker shorthand is recognized, but the exact settlement station is not frozen here.",
    },
    "MIA": {
        "mapping": "exact",
        "city_id": "miami_fl",
        "station_id": "KMIA",
        "station_cli": "MIA",
        "reason": "Static baseline cache for Miami daily-temperature Kalshi markets.",
    },
    "MIN": {
        "mapping": "inferred",
        "city_id": "minneapolis_mn",
        "reason": "Ticker city code maps Minneapolis, but the exact settlement station is not frozen here.",
    },
    "NY": {
        "mapping": "exact",
        "city_id": "new_york_ny",
        "station_id": "KNYC",
        "station_cli": "NYC",
        "reason": "Static baseline cache for New York City daily-temperature Kalshi markets.",
    },
    "NYC": {
        "mapping": "exact",
        "city_id": "new_york_ny",
        "station_id": "KNYC",
        "station_cli": "NYC",
        "reason": "Static baseline cache for New York City daily-temperature Kalshi markets.",
    },
    "OKC": {
        "mapping": "exact",
        "city_id": "oklahoma_city_ok",
        "station_id": "KOKC",
        "station_cli": "OKC",
        "reason": "Static baseline cache for Oklahoma City daily-temperature Kalshi markets.",
    },
    "PHIL": {
        "mapping": "exact",
        "city_id": "philadelphia_pa",
        "station_id": "KPHL",
        "station_cli": "PHL",
        "reason": "Static baseline cache for Philadelphia daily-temperature Kalshi markets.",
    },
    "PHX": {
        "mapping": "exact",
        "city_id": "phoenix_az",
        "station_id": "KPHX",
        "station_cli": "PHX",
        "reason": "Static baseline cache for Phoenix daily-temperature Kalshi markets.",
    },
    "SATX": {
        "mapping": "exact",
        "city_id": "san_antonio_tx",
        "station_id": "KSAT",
        "station_cli": "SAT",
        "reason": "Static baseline cache for San Antonio daily-temperature Kalshi markets.",
    },
    "SEA": {
        "mapping": "exact",
        "city_id": "seattle_wa",
        "station_id": "KSEA",
        "station_cli": "SEA",
        "reason": "Static baseline cache for Seattle daily-temperature Kalshi markets.",
    },
    "SFO": {
        "mapping": "exact",
        "city_id": "san_francisco_ca",
        "station_id": "KSFO",
        "station_cli": "SFO",
        "reason": "Static baseline cache for San Francisco daily-temperature Kalshi markets.",
    },
}


def parse_weather_market_city_code(value: str | None) -> str | None:
    normalized = str(value or "").strip().upper()
    if not normalized:
        return None
    root = normalized.split("-", 1)[0]
    for prefix in WEATHER_SERIES_PREFIXES:
        if root.startswith(prefix) and len(root) > len(prefix):
            return root[len(prefix) :]
    return None


def resolve_weather_station(signal: Mapping[str, Any] | None) -> WeatherStationResolution:
    payload = signal or {}
    explicit = _resolve_explicit_station(payload)
    if explicit is not None:
        return explicit

    city_code = _first_city_code(payload)
    if city_code:
        cached = STATIC_BASELINE_STATION_MAPPINGS.get(city_code)
        city_info = _city_lookup().get(city_code) or {}
        if cached is not None:
            return WeatherStationResolution(
                mapping=str(cached.get("mapping") or "unknown"),
                city_code=city_code,
                city_id=str(cached.get("city_id") or city_info.get("city_id") or "") or None,
                city=str(city_info.get("city") or "") or None,
                state=str(city_info.get("state") or "") or None,
                station_id=str(cached.get("station_id") or "") or None,
                station_cli=str(cached.get("station_cli") or "") or None,
                source="static_baseline_station_cache",
                reason=str(cached.get("reason") or "") or None,
                matched_from="ticker",
            )
        if city_info:
            return WeatherStationResolution(
                mapping="inferred",
                city_code=city_code,
                city_id=str(city_info.get("city_id") or "") or None,
                city=str(city_info.get("city") or "") or None,
                state=str(city_info.get("state") or "") or None,
                source="weather_registry_ticker_alias",
                reason="Ticker city code matched the local registry, but no exact station is cached.",
                matched_from="ticker",
            )

    city_id = _explicit_city_id(payload)
    if city_id:
        city_info = _city_lookup_by_id().get(city_id) or {}
        return WeatherStationResolution(
            mapping="inferred",
            city_code=None,
            city_id=city_id,
            city=str(city_info.get("city") or "") or None,
            state=str(city_info.get("state") or "") or None,
            source="explicit_weather_context",
            reason="Signal already identified a weather city context, but no exact station was provided.",
            matched_from="context",
        )

    return WeatherStationResolution(mapping="unknown")


def _resolve_explicit_station(signal: Mapping[str, Any]) -> WeatherStationResolution | None:
    for value in _iter_signal_values(signal):
        station_id = _normalize_station_id(value)
        if not station_id:
            continue
        registry_match = _station_lookup().get(station_id) or _station_lookup().get(station_id[1:])
        city_code = _first_city_code(signal)
        return WeatherStationResolution(
            mapping="exact",
            city_code=city_code,
            city_id=str((registry_match or {}).get("city_id") or "") or None,
            city=str((registry_match or {}).get("city") or "") or None,
            state=str((registry_match or {}).get("state") or "") or None,
            station_id=station_id,
            station_cli=station_id[1:] if station_id.startswith("K") else station_id,
            source="explicit_station_field",
            reason="Signal contained an explicit station identifier.",
            matched_from="signal",
        )
    for value in _iter_signal_values(signal):
        station_cli = _normalize_station_cli(value)
        if not station_cli:
            continue
        registry_match = _station_lookup().get(station_cli)
        return WeatherStationResolution(
            mapping="exact",
            city_code=_first_city_code(signal),
            city_id=str((registry_match or {}).get("city_id") or "") or None,
            city=str((registry_match or {}).get("city") or "") or None,
            state=str((registry_match or {}).get("state") or "") or None,
            station_id=str((registry_match or {}).get("station_id") or "") or None,
            station_cli=station_cli,
            source="explicit_station_field",
            reason="Signal contained an explicit station CLI code.",
            matched_from="signal",
        )
    return None


def _iter_signal_values(signal: Mapping[str, Any]) -> list[Any]:
    direct_keys = (
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
    nested_keys = ("data", "weather_context", "weather_market_context", "weather", "metadata")
    values: list[Any] = []
    for key in direct_keys:
        values.append(signal.get(key))
    for nested_key in nested_keys:
        nested = signal.get(nested_key)
        if not isinstance(nested, Mapping):
            continue
        for key in direct_keys:
            values.append(nested.get(key))
    return values


def _normalize_station_id(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    match = STATION_ID_PATTERN.search(text)
    return match.group(1) if match else None


def _normalize_station_cli(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    cli_match = STATION_CLI_PATTERN.search(text)
    if cli_match:
        return cli_match.group(1)
    if text in STATIC_BASELINE_STATION_MAPPINGS:
        return text
    if re.fullmatch(r"[A-Z0-9]{3,4}", text):
        return text
    return None


def _first_city_code(signal: Mapping[str, Any]) -> str | None:
    for key in ("market_id", "ticker", "category", "series_ticker"):
        city_code = parse_weather_market_city_code(signal.get(key))
        if city_code:
            return city_code
    for nested_key in ("metadata", "weather_context", "weather_market_context"):
        nested = signal.get(nested_key)
        if not isinstance(nested, Mapping):
            continue
        for key in ("market_id", "ticker", "category", "series", "series_ticker"):
            city_code = parse_weather_market_city_code(nested.get(key))
            if city_code:
                return city_code
    return None


def _explicit_city_id(signal: Mapping[str, Any]) -> str | None:
    for key in ("city_id", "weather_city_id"):
        value = str(signal.get(key) or "").strip()
        if value:
            return value
    for nested_key in ("weather_context", "weather_market_context", "weather", "metadata"):
        nested = signal.get(nested_key)
        if not isinstance(nested, Mapping):
            continue
        for key in ("city_id", "weather_city_id"):
            value = str(nested.get(key) or "").strip()
            if value:
                return value
    return None


@lru_cache(maxsize=1)
def _city_lookup() -> dict[str, dict[str, str]]:
    registry_data = WeatherRegistry.from_file().as_dict()
    lookup: dict[str, dict[str, str]] = {}
    for city in registry_data.get("cities", []):
        if not isinstance(city, Mapping):
            continue
        info = {
            "city_id": str(city.get("city_id") or ""),
            "city": str(city.get("city") or ""),
            "state": str(city.get("state") or ""),
        }
        for token in city.get("ticker_aliases", []) or []:
            normalized = str(token or "").strip().upper()
            if normalized:
                lookup[normalized] = info
    return lookup


@lru_cache(maxsize=1)
def _city_lookup_by_id() -> dict[str, dict[str, str]]:
    registry_data = WeatherRegistry.from_file().as_dict()
    lookup: dict[str, dict[str, str]] = {}
    for city in registry_data.get("cities", []):
        if not isinstance(city, Mapping):
            continue
        city_id = str(city.get("city_id") or "")
        if city_id:
            lookup[city_id] = {
                "city_id": city_id,
                "city": str(city.get("city") or ""),
                "state": str(city.get("state") or ""),
            }
    return lookup


@lru_cache(maxsize=1)
def _station_lookup() -> dict[str, dict[str, str]]:
    registry_data = WeatherRegistry.from_file().as_dict()
    cities_by_id = _city_lookup_by_id()
    lookup: dict[str, dict[str, str]] = {}
    for source in registry_data.get("sources", []):
        if not isinstance(source, Mapping) or source.get("type") != "station":
            continue
        station_id = _normalize_station_id(source.get("name")) or _normalize_station_id(source.get("url"))
        if not station_id:
            continue
        city_id = str(source.get("city_id") or "")
        city_info = cities_by_id.get(city_id) or {}
        entry = {
            "city_id": city_id,
            "city": str(city_info.get("city") or ""),
            "state": str(city_info.get("state") or ""),
            "station_id": station_id,
        }
        lookup[station_id] = entry
        if station_id.startswith("K"):
            lookup[station_id[1:]] = entry
    return lookup


__all__ = [
    "STATIC_BASELINE_STATION_MAPPINGS",
    "WeatherStationResolution",
    "parse_weather_market_city_code",
    "resolve_weather_station",
]
