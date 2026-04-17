from __future__ import annotations

import copy
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[2] / "docs" / "weather" / "city_registry_starter.json"

CITY_REQUIRED_FIELDS = {
    "city_id": str,
    "city": str,
    "state": str,
    "timezone": str,
    "status": str,
}

SOURCE_REQUIRED_FIELDS = {
    "source_id": str,
    "city_id": str,
    "name": str,
    "type": str,
    "role": str,
    "platform": str,
    "status": str,
    "trust_score": (int, float),
}

CITY_STATUSES = {"active", "watch", "inactive"}
SOURCE_TYPES = {"official", "local_met", "station", "community", "social"}
SOURCE_ROLES = {"forecast", "observation", "resolution_adjacent", "social_only"}
SOURCE_STATUSES = {"primary", "secondary", "watch_only", "rejected"}
CITY_SOURCE_LISTS = ("trusted_primary", "trusted_secondary", "watch_only", "rejected")
OPTIONAL_CITY_STRING_LISTS = ("aliases", "ticker_aliases")


class RegistryValidationError(ValueError):
    """Raised when the registry JSON does not meet the MVP contract."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_fields(item: dict[str, Any], required: dict[str, Any], label: str) -> None:
    for field, expected_type in required.items():
        if field not in item:
            raise RegistryValidationError(f"{label} missing required field '{field}'")
        if not isinstance(item[field], expected_type):
            raise RegistryValidationError(f"{label}.{field} must be {expected_type}")


def _require_string_list(item: dict[str, Any], field: str, label: str) -> None:
    values = item.get(field, [])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise RegistryValidationError(f"{label}.{field} must be a list[str]")


def validate_registry_structure(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise RegistryValidationError("registry root must be an object")
    if not isinstance(data.get("version"), int):
        raise RegistryValidationError("registry.version must be an integer")

    cities = data.get("cities")
    sources = data.get("sources")
    if not isinstance(cities, list):
        raise RegistryValidationError("registry.cities must be a list")
    if not isinstance(sources, list):
        raise RegistryValidationError("registry.sources must be a list")

    city_ids: set[str] = set()
    for index, city in enumerate(cities):
        label = f"city[{index}]"
        if not isinstance(city, dict):
            raise RegistryValidationError(f"{label} must be an object")
        _require_fields(city, CITY_REQUIRED_FIELDS, label)
        if city["status"] not in CITY_STATUSES:
            raise RegistryValidationError(f"{label}.status must be one of {sorted(CITY_STATUSES)}")
        if city["city_id"] in city_ids:
            raise RegistryValidationError(f"duplicate city_id '{city['city_id']}'")
        city_ids.add(city["city_id"])
        _require_string_list(city, "default_market_types", label)
        _require_string_list(city, "notes", label)
        for field in CITY_SOURCE_LISTS:
            _require_string_list(city, field, label)
        for field in OPTIONAL_CITY_STRING_LISTS:
            if field in city:
                _require_string_list(city, field, label)

    source_ids: set[str] = set()
    source_ids_by_city: dict[str, set[str]] = defaultdict(set)
    for index, source in enumerate(sources):
        label = f"source[{index}]"
        if not isinstance(source, dict):
            raise RegistryValidationError(f"{label} must be an object")
        _require_fields(source, SOURCE_REQUIRED_FIELDS, label)
        if source["type"] not in SOURCE_TYPES:
            raise RegistryValidationError(f"{label}.type must be one of {sorted(SOURCE_TYPES)}")
        if source["role"] not in SOURCE_ROLES:
            raise RegistryValidationError(f"{label}.role must be one of {sorted(SOURCE_ROLES)}")
        if source["status"] not in SOURCE_STATUSES:
            raise RegistryValidationError(f"{label}.status must be one of {sorted(SOURCE_STATUSES)}")
        if not _is_number(source["trust_score"]) or not 0 <= float(source["trust_score"]) <= 100:
            raise RegistryValidationError(f"{label}.trust_score must be between 0 and 100")
        if source["source_id"] in source_ids:
            raise RegistryValidationError(f"duplicate source_id '{source['source_id']}'")
        if source["city_id"] not in city_ids:
            raise RegistryValidationError(f"{label}.city_id references unknown city '{source['city_id']}'")
        source_ids.add(source["source_id"])
        source_ids_by_city[source["city_id"]].add(source["source_id"])
        _require_string_list(source, "coverage_area", label)
        _require_string_list(source, "notes", label)

        metrics = source.get("metrics", {})
        if metrics is not None:
            if not isinstance(metrics, dict):
                raise RegistryValidationError(f"{label}.metrics must be an object")
            for metric_name in (
                "accuracy",
                "timeliness",
                "specificity",
                "consistency",
                "resolution_alignment",
                "hype_penalty",
            ):
                if metric_name in metrics and not _is_number(metrics[metric_name]):
                    raise RegistryValidationError(f"{label}.metrics.{metric_name} must be numeric")
            if "sample_size" in metrics and not isinstance(metrics["sample_size"], int):
                raise RegistryValidationError(f"{label}.metrics.sample_size must be an integer")

    for city in cities:
        for field in CITY_SOURCE_LISTS:
            for source_id in city.get(field, []):
                if source_id not in source_ids_by_city[city["city_id"]]:
                    raise RegistryValidationError(
                        f"city '{city['city_id']}' field '{field}' references unknown source '{source_id}'"
                    )


class WeatherRegistry:
    """Lightweight in-memory weather registry with explicit persistence."""

    def __init__(self, data: dict[str, Any], source_path: str | Path | None = None):
        self._data = copy.deepcopy(data)
        validate_registry_structure(self._data)
        self.source_path = Path(source_path) if source_path is not None else None
        self._cities_by_id = {city["city_id"]: city for city in self._data["cities"]}
        self._sources_by_id = {source["source_id"]: source for source in self._data["sources"]}
        self._sources_by_city: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for source in self._data["sources"]:
            self._sources_by_city[source["city_id"]].append(source)

    @classmethod
    def from_file(cls, path: str | Path = DEFAULT_REGISTRY_PATH) -> "WeatherRegistry":
        registry_path = Path(path)
        with registry_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return cls(data, source_path=registry_path)

    def get_city(self, city_id: str) -> dict[str, Any]:
        try:
            return copy.deepcopy(self._cities_by_id[city_id])
        except KeyError as exc:
            raise KeyError(f"unknown city_id '{city_id}'") from exc

    def get_sources(self, city_id: str) -> list[dict[str, Any]]:
        if city_id not in self._cities_by_id:
            raise KeyError(f"unknown city_id '{city_id}'")
        return [copy.deepcopy(source) for source in self._sources_by_city.get(city_id, [])]

    def update_source_score(
        self,
        source_id: str,
        trust_score: float,
        *,
        reviewed_at: str | None = None,
        sample_size: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if not _is_number(trust_score) or not 0 <= float(trust_score) <= 100:
            raise ValueError("trust_score must be between 0 and 100")
        try:
            source = self._sources_by_id[source_id]
        except KeyError as exc:
            raise KeyError(f"unknown source_id '{source_id}'") from exc

        source["trust_score"] = float(trust_score)
        if reviewed_at is not None:
            source["last_reviewed"] = reviewed_at
        if sample_size is not None:
            if not isinstance(sample_size, int) or sample_size < 0:
                raise ValueError("sample_size must be a non-negative integer")
            source.setdefault("metrics", {})["sample_size"] = sample_size
        if reason:
            notes = source.setdefault("notes", [])
            if not notes or notes[-1] != reason:
                notes.append(reason)
        return copy.deepcopy(source)

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path is not None else self.source_path
        if target is None:
            raise ValueError("save path is required when registry was not loaded from a file")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2)
            handle.write("\n")
        return target
