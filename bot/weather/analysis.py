from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from bot.feeds.live_data import LiveFeedAggregator
from bot.feeds.weather_pro import CITY_COORDS
from bot.market_classification import is_weather_market as canonical_is_weather_market

from .historical import load_historical_weather_records
from .market_mapping import WeatherMarketCityMapper, looks_like_temperature_question
from .registry import WeatherRegistry


WEATHER_SERIES_PREFIXES = ("KXHIGH", "KXLOW")


@dataclass(frozen=True)
class WeatherSampleRecord:
    sample_kind: str
    source_path: str
    observed_at: str | None
    resolved_at: str | None
    market_id: str
    category: str
    question: str
    yes_price: float | None = None
    no_price: float | None = None
    volume: float | None = None
    outcome: str | None = None
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "sample_kind": self.sample_kind,
            "source_path": self.source_path,
            "observed_at": self.observed_at,
            "resolved_at": self.resolved_at,
            "market_id": self.market_id,
            "category": self.category,
            "question": self.question,
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "volume": self.volume,
            "outcome": self.outcome,
            "metadata": dict(self.metadata),
        }


def is_weather_market(market_id: str = "", question: str = "", category: str = "") -> bool:
    return canonical_is_weather_market(market_id=market_id, question=question, category=category)


def category_from_market_id(market_id: str) -> str:
    return str(market_id or "").split("-", 1)[0]


def legacy_market_type(question: str) -> str:
    q = f" {str(question or '').lower()} "
    if " high " in q:
        return "high_temp"
    if " low " in q:
        return "low_temp"
    return "temperature"


def normalized_market_type(question: str) -> str:
    q = f" {str(question or '').lower()} "
    if any(token in q for token in (" high ", " high temp ", " maximum ", " maximum temperature ", " max temp ")):
        return "high_temp"
    if any(token in q for token in (" low ", " low temp ", " minimum ", " minimum temperature ", " min temp ")):
        return "low_temp"
    return "temperature"


def legacy_city_context(question: str, category: str = "") -> tuple[str | None, str | None]:
    lowered = str(question or "").lower()
    for city in sorted(CITY_COORDS, key=len, reverse=True):
        if city in lowered:
            return city, "question_text"

    ticker_city = LiveFeedAggregator._city_from_ticker(None, category)  # type: ignore[misc]
    if ticker_city:
        return ticker_city, "series_ticker"

    return None, None


def load_snapshot_samples(path: str | Path) -> list[WeatherSampleRecord]:
    snapshot_path = Path(path)
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    observed_at = payload.get("timestamp")
    records: list[WeatherSampleRecord] = []
    for market in payload.get("markets", []):
        market_id = str(market.get("id", "") or "")
        category = str(market.get("category") or category_from_market_id(market_id))
        question = str(market.get("question", "") or "")
        if not is_weather_market(market_id, question, category):
            continue
        records.append(
            WeatherSampleRecord(
                sample_kind="snapshot",
                source_path=str(snapshot_path),
                observed_at=_string_or_none(observed_at),
                resolved_at=None,
                market_id=market_id,
                category=category,
                question=question,
                yes_price=_float_or_none(market.get("yes_price")),
                no_price=_float_or_none(market.get("no_price")),
                volume=_float_or_none(market.get("volume")),
            )
        )
    return records


def load_scan_samples(path: str | Path) -> list[WeatherSampleRecord]:
    scan_path = Path(path)
    records: list[WeatherSampleRecord] = []
    for line in scan_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        observed_at = payload.get("timestamp")
        for signal in payload.get("top_signals", []):
            market_id = str(signal.get("market_id", "") or "")
            category = str(signal.get("category") or category_from_market_id(market_id))
            question = str(signal.get("question", "") or "")
            if not is_weather_market(market_id, question, category):
                continue
            records.append(
                WeatherSampleRecord(
                    sample_kind="scan_signal",
                    source_path=str(scan_path),
                    observed_at=_string_or_none(observed_at),
                    resolved_at=None,
                    market_id=market_id,
                    category=category,
                    question=question,
                    yes_price=_float_or_none(signal.get("market_price")),
                    no_price=_float_or_none(signal.get("no_market_price")),
                )
            )
    return records


def load_simulation_samples(path: str | Path, *, resolved_only: bool = True) -> list[WeatherSampleRecord]:
    sim_path = Path(path)
    payload = json.loads(sim_path.read_text(encoding="utf-8"))
    records: list[WeatherSampleRecord] = []
    for trade in payload.get("trades", []):
        market_id = str(trade.get("market_id", "") or "")
        category = str(trade.get("category") or category_from_market_id(market_id))
        question = str(trade.get("question", "") or "")
        if not is_weather_market(market_id, question, category):
            continue
        if resolved_only and not trade.get("resolved"):
            continue
        records.append(
            WeatherSampleRecord(
                sample_kind="resolved_trade" if trade.get("resolved") else "trade",
                source_path=str(sim_path),
                observed_at=_string_or_none(trade.get("timestamp")),
                resolved_at=_string_or_none(trade.get("resolved_at")),
                market_id=market_id,
                category=category,
                question=question,
                yes_price=_float_or_none(trade.get("market_price")),
                volume=_float_or_none(trade.get("position_size")),
                outcome=_upper_or_none(trade.get("outcome")),
            )
        )
    return records


def load_historical_csv_samples(path: str | Path, *, one_per_series: bool = True) -> list[WeatherSampleRecord]:
    records: list[WeatherSampleRecord] = []
    for record in load_historical_weather_records(path, one_per_series=one_per_series):
        records.append(
            WeatherSampleRecord(
                sample_kind="historical_csv",
                source_path=record.source_path,
                observed_at=record.closes_at,
                resolved_at=record.resolved_at,
                market_id=record.market_ticker,
                category=record.series_ticker,
                question=record.question,
                yes_price=record.yes_price,
                no_price=record.no_price,
                volume=record.volume,
                outcome=record.outcome,
                metadata={
                    key: value
                    for key, value in {
                        "market_subtitle": record.market_subtitle,
                        "yes_subtitle": record.yes_subtitle,
                        "no_subtitle": record.no_subtitle,
                        "starts_at": record.starts_at,
                        "ingested_at": record.ingested_at,
                    }.items()
                    if value is not None
                },
            )
        )
    return records


def select_sample_records(
    records: Iterable[WeatherSampleRecord],
    *,
    max_records: int = 24,
    max_per_kind: int = 8,
) -> list[WeatherSampleRecord]:
    selected: list[WeatherSampleRecord] = []
    seen_market_ids: set[str] = set()
    by_kind: Counter[str] = Counter()

    for record in records:
        if len(selected) >= max_records:
            break
        if record.market_id in seen_market_ids:
            continue
        if by_kind[record.sample_kind] >= max_per_kind:
            continue
        selected.append(record)
        seen_market_ids.add(record.market_id)
        by_kind[record.sample_kind] += 1

    return selected


def compare_sample_records(
    records: Iterable[WeatherSampleRecord],
    *,
    mapper: WeatherMarketCityMapper | None = None,
    registry: WeatherRegistry | None = None,
) -> list[dict]:
    registry = registry or WeatherRegistry.from_file()
    mapper = mapper or WeatherMarketCityMapper(registry)
    registry_cities = registry.as_dict().get("cities", [])
    registry_city_ids_by_name = {
        str(city.get("city", "")).strip().lower(): str(city.get("city_id", ""))
        for city in registry_cities
        if isinstance(city, dict)
    }

    comparisons: list[dict] = []
    for record in records:
        baseline_city, baseline_city_source = legacy_city_context(record.question, record.category)
        baseline_registry_city_id = registry_city_ids_by_name.get(str(baseline_city or "").lower()) if baseline_city else None
        mapped = mapper.resolve(record.question, record.category)
        mapped_city = registry.get_city(mapped.city_id) if mapped else None
        normalized_type = normalized_market_type(record.question)
        supports_market_type = (
            normalized_type in mapped_city.get("default_market_types", [])
            if mapped_city
            else False
        )

        comparisons.append(
            {
                **record.as_dict(),
                "baseline_city": baseline_city,
                "baseline_city_source": baseline_city_source,
                "baseline_market_type": legacy_market_type(record.question),
                "normalized_market_type": normalized_type,
                "registry_city_id": mapped.city_id if mapped else None,
                "registry_city": mapped.city if mapped else None,
                "registry_state": mapped.state if mapped else None,
                "registry_primary_source_id": mapped.primary_source_id if mapped else None,
                "registry_supports_market_type": supports_market_type,
                "city_fit": _city_fit(baseline_registry_city_id, mapped.city_id if mapped else None, baseline_city),
            }
        )

    return comparisons


def build_report(comparisons: list[dict]) -> dict:
    by_kind = Counter(record["sample_kind"] for record in comparisons)
    by_city_fit = Counter(record["city_fit"] for record in comparisons)
    mapped_cities = Counter(record["registry_city_id"] for record in comparisons if record.get("registry_city_id"))
    resolved_outcomes = Counter(record["outcome"] for record in comparisons if record.get("outcome"))
    market_type_changes = [record for record in comparisons if record["baseline_market_type"] != record["normalized_market_type"]]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "records_sampled": len(comparisons),
            "resolved_records": sum(1 for record in comparisons if record.get("outcome")),
            "registry_mapped_records": sum(1 for record in comparisons if record.get("registry_city_id")),
            "registry_source_assigned_records": sum(
                1 for record in comparisons if record.get("registry_primary_source_id")
            ),
            "baseline_market_type_misses": len(market_type_changes),
            "by_sample_kind": dict(by_kind),
            "by_city_fit": dict(by_city_fit),
            "mapped_cities": dict(mapped_cities),
            "resolved_outcomes": dict(resolved_outcomes),
        },
        "review_queues": {
            "registry_only_examples": _compact_examples(
                [record for record in comparisons if record["city_fit"] == "registry_only"]
            ),
            "mismatch_examples": _compact_examples(
                [record for record in comparisons if record["city_fit"] == "mismatch"]
            ),
            "needs_registry_expansion": _compact_examples(
                [record for record in comparisons if record["city_fit"] == "baseline_only"]
            ),
            "market_type_misses": _compact_examples(market_type_changes),
            "resolved_examples": _compact_examples(
                [record for record in comparisons if record.get("outcome")]
            ),
        },
        "records": comparisons,
    }


def _compact_examples(records: list[dict], *, limit: int = 5) -> list[dict]:
    examples: list[dict] = []
    for record in records[:limit]:
        examples.append(
            {
                "market_id": record["market_id"],
                "question": record["question"],
                "baseline_city": record.get("baseline_city"),
                "baseline_market_type": record.get("baseline_market_type"),
                "registry_city_id": record.get("registry_city_id"),
                "registry_primary_source_id": record.get("registry_primary_source_id"),
                "city_fit": record.get("city_fit"),
                "outcome": record.get("outcome"),
            }
        )
    return examples


def _city_fit(
    baseline_registry_city_id: str | None,
    registry_city_id: str | None,
    baseline_city: str | None,
) -> str:
    if baseline_city and registry_city_id:
        if baseline_registry_city_id:
            return "aligned" if baseline_registry_city_id == registry_city_id else "mismatch"
        return "mismatch"
    if registry_city_id:
        return "registry_only"
    if baseline_city:
        return "baseline_only"
    return "unmapped"


def _float_or_none(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _string_or_none(value) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _upper_or_none(value) -> str | None:
    if value is None or value == "":
        return None
    return str(value).upper()
