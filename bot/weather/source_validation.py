from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .analysis import normalized_market_type
from .historical import DEFAULT_KALSHI_HISTORY_PATH, load_historical_weather_records
from .market_mapping import WeatherMarketCityMapper
from .registry import WeatherRegistry
from .thresholds import extract_threshold_value, infer_direction_from_value, infer_predicted_outcome, infer_question_side


DEFAULT_SOURCE_PILOT_DIR = Path(__file__).resolve().parents[2] / "data" / "weather" / "sources"
SOURCE_GROUPS = ("official_sources", "resolution_adjacent_sources", "candidate_sources")
CANDIDATE_SCOPES = {"local", "global"}
SOURCE_STATUSES = {"primary_reference", "settlement_reference", "candidate"}
VALIDATION_ENTRY_FIELDS = ("source_id", "market_ticker")
SOURCE_FIELDS = ("source_id", "name", "platform", "url", "status", "notes")


class SourceValidationError(ValueError):
    """Raised when a weather source validation pilot file is malformed."""


def load_source_validation_pilot(path: str | Path) -> dict[str, Any]:
    pilot_path = Path(path)
    data = json.loads(pilot_path.read_text(encoding="utf-8"))
    _validate_source_validation_pilot(data, pilot_path)
    return data


def load_source_validation_pilots(
    source_dir: str | Path = DEFAULT_SOURCE_PILOT_DIR,
    *,
    city_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    base_dir = Path(source_dir)
    requested = {str(city_id) for city_id in city_ids or []}
    pilots: list[dict[str, Any]] = []
    for path in sorted(base_dir.glob("*.json")):
        pilot = load_source_validation_pilot(path)
        if requested and pilot["city_id"] not in requested:
            continue
        pilot["_source_path"] = str(path)
        pilots.append(pilot)
    return pilots


def build_source_validation_report(
    *,
    source_dir: str | Path = DEFAULT_SOURCE_PILOT_DIR,
    history_path: str | Path = DEFAULT_KALSHI_HISTORY_PATH,
    city_ids: Iterable[str] | None = None,
    registry: WeatherRegistry | None = None,
) -> dict[str, Any]:
    registry = registry or WeatherRegistry.from_file()
    mapper = WeatherMarketCityMapper(registry)
    pilots = load_source_validation_pilots(source_dir, city_ids=city_ids)
    historical = _build_historical_lookup(history_path, mapper=mapper)

    city_reports: list[dict[str, Any]] = []
    summary = Counter()
    summary["cities"] = len(pilots)
    summary["archive_threshold_markets_available"] = sum(
        historical["coverage_by_city"].get(pilot["city_id"], 0) for pilot in pilots
    )

    for pilot in pilots:
        city_report = _build_city_report(pilot, historical)
        city_reports.append(city_report)
        summary["sources"] += city_report["summary"]["sources"]
        summary["validation_entries"] += city_report["summary"]["validation_entries"]
        summary["matched_validation_entries"] += city_report["summary"]["matched_validation_entries"]
        summary["correct"] += city_report["summary"]["correct"]
        summary["incorrect"] += city_report["summary"]["incorrect"]
        summary["unmatched_validation_entries"] += city_report["summary"]["unmatched_validation_entries"]

    matched = summary["matched_validation_entries"]
    return {
        "summary": {
            **dict(summary),
            "accuracy": round(summary["correct"] / matched, 4) if matched else None,
        },
        "cities": city_reports,
    }


def _build_city_report(pilot: dict[str, Any], historical: dict[str, Any]) -> dict[str, Any]:
    city_id = pilot["city_id"]
    sources_by_id: dict[str, dict[str, Any]] = {}
    source_summaries: dict[str, dict[str, Any]] = {}
    inventory_counts = {}
    source_group_breakdown: dict[str, list[dict[str, Any]]] = {}

    for group_name in SOURCE_GROUPS:
        group_sources = [dict(source) for source in pilot["source_groups"].get(group_name, [])]
        source_group_breakdown[group_name] = group_sources
        inventory_counts[group_name] = len(group_sources)
        for source in group_sources:
            sources_by_id[source["source_id"]] = source
            source_summaries[source["source_id"]] = {
                "source_id": source["source_id"],
                "name": source["name"],
                "group": group_name,
                "scope": source.get("scope"),
                "platform": source["platform"],
                "status": source["status"],
                "registry_source_id": source.get("registry_source_id"),
                "validation_entries": 0,
                "matched_validation_entries": 0,
                "correct": 0,
                "incorrect": 0,
                "accuracy": None,
            }

    validation_rows: list[dict[str, Any]] = []
    city_summary = Counter()
    city_summary["sources"] = sum(inventory_counts.values())
    city_summary["archive_threshold_markets"] = historical["coverage_by_city"].get(city_id, 0)
    city_summary["validation_entries"] = 0
    city_summary["matched_validation_entries"] = 0
    city_summary["correct"] = 0
    city_summary["incorrect"] = 0
    city_summary["unmatched_validation_entries"] = 0

    for entry in pilot.get("validation_entries", []):
        source = sources_by_id[entry["source_id"]]
        row = _score_validation_entry(city_id, source, entry, historical["markets_by_ticker"])
        validation_rows.append(row)
        source_summary = source_summaries[entry["source_id"]]
        city_summary["validation_entries"] += 1
        source_summary["validation_entries"] += 1
        if row["matched_market"]:
            city_summary["matched_validation_entries"] += 1
            source_summary["matched_validation_entries"] += 1
        if row["was_correct"] is True:
            city_summary["correct"] += 1
            source_summary["correct"] += 1
        elif row["was_correct"] is False:
            city_summary["incorrect"] += 1
            source_summary["incorrect"] += 1
        elif not row["matched_market"]:
            city_summary["unmatched_validation_entries"] += 1

    for source_summary in source_summaries.values():
        matched = source_summary["matched_validation_entries"]
        if matched:
            source_summary["accuracy"] = round(source_summary["correct"] / matched, 4)

    reference_markets = [
        _enrich_reference_market(city_id, item, historical["markets_by_ticker"])
        for item in pilot.get("archive_reference_markets", [])
    ]
    fallback_examples = historical["recent_markets_by_city"].get(city_id, [])[:3]

    matched = city_summary["matched_validation_entries"]
    return {
        "city_id": city_id,
        "city": pilot["city"],
        "source_file": pilot.get("_source_path"),
        "summary": {
            **dict(city_summary),
            "accuracy": round(city_summary["correct"] / matched, 4) if matched else None,
            "inventory_counts": inventory_counts,
        },
        "pilot_notes": list(pilot.get("pilot_notes", [])),
        "source_groups": source_group_breakdown,
        "source_summaries": list(source_summaries.values()),
        "archive_reference_markets": reference_markets,
        "archive_threshold_examples": fallback_examples,
        "validation_entries": validation_rows,
    }


def _score_validation_entry(
    city_id: str,
    source: dict[str, Any],
    entry: dict[str, Any],
    markets_by_ticker: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    historical = markets_by_ticker.get(entry["market_ticker"])
    predicted_value_f = _float_or_none(entry.get("predicted_value_f"))
    predicted_direction = entry.get("predicted_direction")
    if predicted_direction is None:
        predicted_direction = infer_direction_from_value(
            predicted_value_f,
            historical["threshold_value"] if historical else None,
        )
    predicted_outcome = _normalized_outcome(entry.get("predicted_outcome"))
    if predicted_outcome is None and historical:
        predicted_outcome = infer_predicted_outcome(historical["question_side"], predicted_direction)

    matched_market = bool(historical and historical["city_id"] == city_id)
    resolved_outcome = historical["resolved_outcome"] if matched_market else None
    was_correct = predicted_outcome == resolved_outcome if matched_market and predicted_outcome else None

    return {
        "city_id": city_id,
        "source_id": source["source_id"],
        "source_name": source["name"],
        "source_group": _source_group_for(source),
        "source_scope": source.get("scope"),
        "market_ticker": entry["market_ticker"],
        "matched_market": matched_market,
        "question": historical["question"] if matched_market else None,
        "market_type": historical["market_type"] if matched_market else None,
        "question_side": historical["question_side"] if matched_market else None,
        "threshold_value": historical["threshold_value"] if matched_market else None,
        "resolved_outcome": resolved_outcome,
        "predicted_direction": predicted_direction,
        "predicted_value_f": predicted_value_f,
        "predicted_outcome": predicted_outcome,
        "observed_at": entry.get("observed_at"),
        "evidence": entry.get("evidence"),
        "notes": list(entry.get("notes", [])),
        "was_correct": was_correct,
    }


def _enrich_reference_market(
    city_id: str,
    entry: dict[str, Any],
    markets_by_ticker: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    market = markets_by_ticker.get(entry["market_ticker"])
    if not market or market["city_id"] != city_id:
        return {
            "market_ticker": entry["market_ticker"],
            "matched_market": False,
            "notes": list(entry.get("notes", [])),
        }
    return {
        "market_ticker": entry["market_ticker"],
        "matched_market": True,
        "question": market["question"],
        "market_type": market["market_type"],
        "question_side": market["question_side"],
        "threshold_value": market["threshold_value"],
        "resolved_outcome": market["resolved_outcome"],
        "resolved_at": market["resolved_at"],
        "notes": list(entry.get("notes", [])),
    }


def _build_historical_lookup(
    history_path: str | Path,
    *,
    mapper: WeatherMarketCityMapper,
) -> dict[str, Any]:
    markets_by_ticker: dict[str, dict[str, Any]] = {}
    coverage_by_city: Counter[str] = Counter()
    recent_markets_by_city: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for record in load_historical_weather_records(history_path, one_per_series=False):
        metadata = {
            "market_subtitle": record.market_subtitle,
            "yes_subtitle": record.yes_subtitle,
            "no_subtitle": record.no_subtitle,
        }
        threshold_value = extract_threshold_value(record.question, metadata)
        question_side = infer_question_side(record.question, metadata)
        if threshold_value is None or question_side not in {"above", "below"}:
            continue

        mapped = mapper.resolve(record.question, record.series_ticker)
        if mapped is None:
            continue

        item = {
            "city_id": mapped.city_id,
            "market_ticker": record.market_ticker,
            "series_ticker": record.series_ticker,
            "question": record.question,
            "market_type": _market_type_from_record(record.question, record.series_ticker),
            "question_side": question_side,
            "threshold_value": threshold_value,
            "resolved_outcome": _normalized_outcome(record.outcome),
            "resolved_at": record.resolved_at,
        }
        markets_by_ticker[record.market_ticker] = item
        coverage_by_city[mapped.city_id] += 1
        recent_markets_by_city[mapped.city_id].append(item)

    for city_id, records in recent_markets_by_city.items():
        records.sort(key=lambda item: (str(item.get("resolved_at") or ""), item["market_ticker"]), reverse=True)
        recent_markets_by_city[city_id] = records[:6]

    return {
        "markets_by_ticker": markets_by_ticker,
        "coverage_by_city": dict(coverage_by_city),
        "recent_markets_by_city": dict(recent_markets_by_city),
    }


def _validate_source_validation_pilot(data: dict[str, Any], path: Path) -> None:
    if not isinstance(data, dict):
        raise SourceValidationError(f"{path} must contain a JSON object")
    for field in ("version", "city_id", "city", "source_groups"):
        if field not in data:
            raise SourceValidationError(f"{path} missing required field '{field}'")
    if not isinstance(data["version"], int):
        raise SourceValidationError(f"{path} field 'version' must be an integer")
    if not isinstance(data["city_id"], str) or not data["city_id"]:
        raise SourceValidationError(f"{path} field 'city_id' must be a non-empty string")
    if not isinstance(data["city"], str) or not data["city"]:
        raise SourceValidationError(f"{path} field 'city' must be a non-empty string")
    if not isinstance(data["source_groups"], dict):
        raise SourceValidationError(f"{path} field 'source_groups' must be an object")

    source_ids: set[str] = set()
    for group_name in SOURCE_GROUPS:
        group_items = data["source_groups"].get(group_name, [])
        if not isinstance(group_items, list):
            raise SourceValidationError(f"{path} group '{group_name}' must be a list")
        for index, item in enumerate(group_items):
            if not isinstance(item, dict):
                raise SourceValidationError(f"{path} {group_name}[{index}] must be an object")
            for field in SOURCE_FIELDS:
                if field not in item:
                    raise SourceValidationError(f"{path} {group_name}[{index}] missing '{field}'")
            if item["source_id"] in source_ids:
                raise SourceValidationError(f"{path} duplicate source_id '{item['source_id']}'")
            if item["status"] not in SOURCE_STATUSES:
                raise SourceValidationError(f"{path} invalid status '{item['status']}' for source '{item['source_id']}'")
            if not isinstance(item["notes"], list) or any(not isinstance(note, str) for note in item["notes"]):
                raise SourceValidationError(f"{path} source '{item['source_id']}' field 'notes' must be list[str]")
            if group_name == "candidate_sources":
                if item.get("scope") not in CANDIDATE_SCOPES:
                    raise SourceValidationError(
                        f"{path} candidate source '{item['source_id']}' must declare scope in {sorted(CANDIDATE_SCOPES)}"
                    )
            source_ids.add(item["source_id"])

    for field in ("pilot_notes", "archive_reference_markets", "validation_entries"):
        if field in data and not isinstance(data[field], list):
            raise SourceValidationError(f"{path} field '{field}' must be a list")

    for index, item in enumerate(data.get("archive_reference_markets", [])):
        if not isinstance(item, dict) or not isinstance(item.get("market_ticker"), str):
            raise SourceValidationError(f"{path} archive_reference_markets[{index}] must contain 'market_ticker'")

    for index, item in enumerate(data.get("validation_entries", [])):
        if not isinstance(item, dict):
            raise SourceValidationError(f"{path} validation_entries[{index}] must be an object")
        for field in VALIDATION_ENTRY_FIELDS:
            if not isinstance(item.get(field), str) or not item[field]:
                raise SourceValidationError(f"{path} validation_entries[{index}] missing '{field}'")
        if item["source_id"] not in source_ids:
            raise SourceValidationError(f"{path} validation entry references unknown source_id '{item['source_id']}'")
        if item.get("predicted_direction") not in (None, "above", "below"):
            raise SourceValidationError(
                f"{path} validation entry '{item['market_ticker']}' has invalid predicted_direction"
            )
        if item.get("predicted_outcome") not in (None, "YES", "NO", "yes", "no"):
            raise SourceValidationError(
                f"{path} validation entry '{item['market_ticker']}' has invalid predicted_outcome"
            )
        if "notes" in item and (not isinstance(item["notes"], list) or any(not isinstance(note, str) for note in item["notes"])):
            raise SourceValidationError(f"{path} validation entry '{item['market_ticker']}' field 'notes' must be list[str]")


def _normalized_outcome(value: object) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip().upper()
    return rendered if rendered in {"YES", "NO"} else None


def _float_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _source_group_for(source: dict[str, Any]) -> str:
    if source["status"] == "primary_reference":
        return "official_sources"
    if source["status"] == "settlement_reference":
        return "resolution_adjacent_sources"
    return "candidate_sources"


def _market_type_from_record(question: str, series_ticker: str) -> str:
    market_type = normalized_market_type(question)
    if market_type != "temperature":
        return market_type

    normalized_series = str(series_ticker or "").upper()
    if normalized_series.startswith(("KXHIGH", "KXHIGHT")):
        return "high_temp"
    if normalized_series.startswith(("KXLOW", "KXLOWT", "KXMINTEMP")):
        return "low_temp"
    return market_type
