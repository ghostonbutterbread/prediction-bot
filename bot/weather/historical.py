from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from bot.feeds.live_data import LiveFeedAggregator
from bot.feeds.weather_pro import CITY_COORDS

from .market_mapping import looks_like_temperature_question
from .registry import WeatherRegistry


DEFAULT_KALSHI_HISTORY_PATH = Path("data/historical/kalshi.csv")


@dataclass(frozen=True)
class HistoricalWeatherMarketRecord:
    source_path: str
    series_ticker: str
    market_ticker: str
    question: str
    outcome: str | None = None
    market_subtitle: str | None = None
    yes_subtitle: str | None = None
    no_subtitle: str | None = None
    starts_at: str | None = None
    closes_at: str | None = None
    resolved_at: str | None = None
    ingested_at: str | None = None
    yes_price: float | None = None
    no_price: float | None = None
    volume: float | None = None


def infer_historical_city(question: str, series_ticker: str = "") -> str | None:
    normalized_question = f" {str(question or '').lower()} "
    for city in sorted(CITY_COORDS, key=len, reverse=True):
        if f" {city} " in normalized_question:
            return city

    if not looks_like_temperature_question(question):
        return None

    return LiveFeedAggregator._city_from_ticker(None, series_ticker)  # type: ignore[misc]


def load_historical_weather_records(
    path: str | Path,
    *,
    one_per_series: bool = False,
) -> list[HistoricalWeatherMarketRecord]:
    history_path = Path(path)
    records: list[HistoricalWeatherMarketRecord] = []
    seen_series: set[str] = set()

    with history_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            series_ticker = str(row.get("EVENT_TICKER", "") or "").split("-", 1)[0]
            market_ticker = str(row.get("MARKET_TICKER", "") or "")
            question = str(row.get("MARKET_TITLE", "") or "")
            if not series_ticker.startswith(("KXHIGH", "KXLOW")):
                continue
            if not looks_like_temperature_question(question):
                continue
            if one_per_series and series_ticker in seen_series:
                continue

            seen_series.add(series_ticker)
            outcome = str(row.get("RESULT", "") or "").upper() or None
            records.append(
                HistoricalWeatherMarketRecord(
                    source_path=str(history_path),
                    series_ticker=series_ticker,
                    market_ticker=market_ticker,
                    question=question,
                    outcome=outcome,
                    market_subtitle=_string_or_none(row.get("MARKET_SUBTITLE")),
                    yes_subtitle=_string_or_none(row.get("YES_SUBTITLE")),
                    no_subtitle=_string_or_none(row.get("NO_SUBTITLE")),
                    starts_at=_string_or_none(row.get("START_DT")),
                    closes_at=str(row.get("END_DT", "") or "") or None,
                    resolved_at=str(row.get("CLOSED_DT", "") or "") or None,
                    ingested_at=_string_or_none(row.get("INGESTION_DT")),
                    yes_price=_float_from_row(
                        row,
                        "YES_PRICE",
                        "yes_price",
                        "YES_ASK",
                        "yes_ask",
                        "MARKET_PRICE",
                        "market_price",
                        "LAST_PRICE",
                        "last_price",
                    ),
                    no_price=_float_from_row(
                        row,
                        "NO_PRICE",
                        "no_price",
                        "NO_ASK",
                        "no_ask",
                        "NO_MARKET_PRICE",
                        "no_market_price",
                    ),
                    volume=_float_from_row(
                        row,
                        "VOLUME",
                        "volume",
                        "OPEN_INTEREST",
                        "open_interest",
                    ),
                )
            )

    return records


def build_historical_city_coverage(
    records: Iterable[HistoricalWeatherMarketRecord],
    *,
    registry: WeatherRegistry | None = None,
) -> dict:
    registry = registry or WeatherRegistry.from_file()
    registry_city_ids_by_name = {
        str(city.get("city", "")).strip().lower(): str(city.get("city_id", ""))
        for city in registry.as_dict().get("cities", [])
        if isinstance(city, dict)
    }

    city_record_counts: Counter[str] = Counter()
    registry_city_record_counts: Counter[str] = Counter()
    city_series: dict[str, set[str]] = defaultdict(set)
    example_question_by_city: dict[str, str] = {}
    unresolved_series: Counter[str] = Counter()
    total_records = 0

    for record in records:
        total_records += 1
        city = infer_historical_city(record.question, record.series_ticker)
        if not city:
            unresolved_series[record.series_ticker] += 1
            continue

        city_record_counts[city] += 1
        city_series[city].add(record.series_ticker)
        example_question_by_city.setdefault(city, record.question)

        registry_city_id = registry_city_ids_by_name.get(city)
        if registry_city_id:
            registry_city_record_counts[registry_city_id] += 1

    covered_city_names = {
        city_name
        for city_name in city_record_counts
        if city_name in registry_city_ids_by_name
    }
    missing_city_names = sorted(set(city_record_counts) - covered_city_names)

    return {
        "summary": {
            "records_examined": total_records,
            "records_with_city": sum(city_record_counts.values()),
            "unique_historical_cities": len(city_record_counts),
            "registry_covered_cities": len(covered_city_names),
            "registry_missing_cities": len(missing_city_names),
            "registry_covered_records": sum(registry_city_record_counts.values()),
            "unresolved_series": dict(unresolved_series),
        },
        "missing_city_names": missing_city_names,
        "cities": [
            {
                "city": city,
                "registry_city_id": registry_city_ids_by_name.get(city),
                "records": city_record_counts[city],
                "series": sorted(city_series[city]),
                "example_question": example_question_by_city[city],
            }
            for city in sorted(city_record_counts)
        ],
        "registry_record_counts": dict(registry_city_record_counts),
    }


def _float_from_row(row: dict[str, str], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None
