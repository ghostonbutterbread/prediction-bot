"""Offline weather training dataset aggregation helpers.

This module normalizes Prediction Lab and archive replay JSONL rows into a
provenance-explicit dataset. It intentionally performs no network access and
does not mutate runtime state.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bot.weather.date_matcher import derive_market_date, validate_weather_date_match
from bot.weather.thresholds import extract_threshold_value, infer_question_side


SCHEMA_VERSION = 1

DATASET_FIRST_PARTY = "first_party"
DATASET_ARCHIVE_REPLAY = "archive_replay"

TIER_FIRST_PARTY_RECORDED_AS_OF = "first_party_recorded_as_of"
TIER_FIRST_PARTY_MISSING_SOURCE = "first_party_missing_source"
TIER_ARCHIVE_HISTORICAL_POST_FACTO = "archive_historical_post_facto"
TIER_ARCHIVE_MISSING_WEATHER = "archive_missing_weather"
TIER_UNKNOWN_OR_UNSUPPORTED = "unknown_or_unsupported"

SOURCE_RECORDED_AS_OF = "recorded_as_of"
SOURCE_HISTORICAL_POST_FACTO = "historical_post_facto"
SOURCE_MISSING = "missing"
SOURCE_LIVE_CURRENT_FORBIDDEN = "live_current_forbidden"
SOURCE_UNKNOWN = "unknown"

PROVENANCE_RECORDED_COLLECTION = "recorded_collection"
PROVENANCE_HISTORICAL_POST_FACTO_BACKFILL = "historical_post_facto_backfill"
PROVENANCE_MISSING = "missing"
PROVENANCE_UNKNOWN = "unknown"

ANTI_HINDSIGHT_RECORDED = "recorded_at_decision_time"
ANTI_HINDSIGHT_POST_FACTO = "post_facto_weather_not_recorded_as_of"
ANTI_HINDSIGHT_MISSING = "missing"

SUPPORTED_MARKET_FAMILIES = {"daily_temperature"}
RESOLVED_OUTCOMES = {"YES", "NO"}

PROVENANCE_PRIORITY = {
    TIER_FIRST_PARTY_RECORDED_AS_OF: 0,
    TIER_FIRST_PARTY_MISSING_SOURCE: 1,
    TIER_ARCHIVE_HISTORICAL_POST_FACTO: 2,
    TIER_ARCHIVE_MISSING_WEATHER: 3,
    TIER_UNKNOWN_OR_UNSUPPORTED: 4,
}


def default_dataset_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"weather-training-dataset-{now.strftime('%Y%m%d-%H%M%S')}"


def load_resolution_index(paths: Iterable[str | Path]) -> dict[str, dict[str, Any]]:
    """Load optional resolution JSONL files keyed by market_id."""

    resolutions: dict[str, dict[str, Any]] = {}
    for path_value in paths:
        path = Path(path_value)
        if not path.exists():
            continue
        for line_number, row in _iter_jsonl(path):
            market_id = _string_or_none(row.get("market_id"))
            if not market_id:
                continue
            resolution = row.get("resolution") if isinstance(row.get("resolution"), dict) else {}
            normalized = {
                "outcome": _normalize_outcome(resolution.get("outcome") or row.get("outcome") or row.get("result")),
                "resolved_at": _string_or_none(resolution.get("resolved_at") or row.get("resolved_at")),
                "input_path": str(path),
                "input_line": line_number,
            }
            resolutions[market_id] = normalized
    return resolutions


def normalize_input_rows(
    input_paths: Iterable[str | Path],
    *,
    resolution_index: dict[str, dict[str, Any]] | None = None,
    dataset_id: str | None = None,
    source_label: str = "auto",
) -> list[dict[str, Any]]:
    """Normalize one or more Prediction Lab JSONL inputs."""

    active_dataset_id = dataset_id or default_dataset_id()
    resolutions = resolution_index or {}
    rows: list[dict[str, Any]] = []
    for path_value in input_paths:
        path = Path(path_value)
        dataset_source = _dataset_source_for_path(path, source_label=source_label)
        for line_number, raw_row in _iter_jsonl(path):
            rows.append(
                normalize_row(
                    raw_row,
                    input_path=str(path),
                    input_line=line_number,
                    dataset_id=active_dataset_id,
                    dataset_source=dataset_source,
                    resolution=resolutions.get(str(raw_row.get("market_id") or "")),
                )
            )
    rows.sort(key=lambda row: (str(row["market"].get("event_date") or ""), row["row_id"]))
    return rows


def normalize_row(
    row: dict[str, Any],
    *,
    input_path: str,
    input_line: int,
    dataset_id: str,
    dataset_source: str,
    resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifact = row.get("decision_artifact") if isinstance(row.get("decision_artifact"), dict) else {}
    market_route = _first_dict(row.get("market_route"), artifact.get("market_route"))
    route_evidence = market_route.get("evidence") if isinstance(market_route.get("evidence"), dict) else {}
    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    source_context_data = source_context.get("data") if isinstance(source_context.get("data"), dict) else {}
    market_metadata = source_context_data.get("market_metadata") if isinstance(source_context_data.get("market_metadata"), dict) else {}
    weather_snapshot = _extract_weather_snapshot(artifact)
    weather_forecast = weather_snapshot.get("forecast") if isinstance(weather_snapshot.get("forecast"), dict) else {}
    date_validation = _date_validation(row, weather_snapshot)

    question = _string_or_none(row.get("question") or artifact.get("question") or route_evidence.get("question") or weather_snapshot.get("question")) or ""
    market_id = _string_or_none(row.get("market_id") or artifact.get("market_id") or route_evidence.get("market_id") or weather_snapshot.get("market_id")) or ""
    event_ticker = _string_or_none(
        row.get("event_ticker")
        or route_evidence.get("event_ticker")
        or market_metadata.get("event_ticker")
        or _event_ticker_from_market_id(market_id)
    )
    series_ticker = _string_or_none(
        route_evidence.get("series_ticker")
        or row.get("series_ticker")
        or row.get("series")
        or market_metadata.get("series")
        or _series_ticker_from_market_id(market_id)
    )

    event_date = _string_or_none(
        weather_snapshot.get("market_date")
        or (date_validation or {}).get("market_date")
        or weather_snapshot.get("weather_date")
        or _derived_market_date(row, market_id=market_id, question=question, event_ticker=event_ticker)
    )
    question_side = _string_or_none(weather_forecast.get("question_side") or _signal_data(weather_snapshot).get("question_side"))
    if not question_side:
        question_side = infer_question_side(question, route_evidence)

    threshold = _float_or_none(weather_forecast.get("threshold"))
    if threshold is None:
        threshold = _float_or_none(_signal_data(weather_snapshot).get("threshold"))
    if threshold is None:
        threshold = extract_threshold_value(question, route_evidence)
    bucket_low, bucket_high = _bucket_bounds(row, question=question, market_id=market_id)

    source_mode = _source_mode(artifact, source_context, weather_snapshot)
    source_provenance = _source_provenance(source_context, weather_snapshot, source_mode)
    provenance_tier = _provenance_tier(
        dataset_source=dataset_source,
        market_family=_market_family(market_route, route_evidence, market_metadata),
        source_mode=source_mode,
        weather_snapshot=weather_snapshot,
    )
    anti_hindsight = _anti_hindsight_marker(source_mode)
    resolution_payload = _resolution_payload(row, market_metadata, resolution)

    weather = _weather_payload(weather_snapshot, weather_forecast, date_validation)
    market = {
        "market_id": market_id,
        "event_ticker": event_ticker,
        "series_ticker": series_ticker,
        "question": question,
        "market_family": _market_family(market_route, route_evidence, market_metadata),
        "city_id": _city_id(weather_snapshot, row),
        "city": _city_name(weather_snapshot, row),
        "event_date": event_date,
        "market_type": _market_type(market_route, route_evidence),
        "question_side": question_side or "unknown",
        "threshold": threshold,
        "bucket_low": bucket_low,
        "bucket_high": bucket_high,
    }
    market_state = _market_state_payload(row, artifact)
    decision = _decision_payload(row, artifact)

    warnings = _quality_warnings(
        provenance_tier=provenance_tier,
        source_mode=source_mode,
        weather=weather,
        resolution=resolution_payload,
    )
    quality = {
        "usable_for_training": _usable_for_training(provenance_tier, weather, resolution_payload),
        "usable_for_production_replay": _usable_for_production_replay(
            dataset_source=dataset_source,
            provenance_tier=provenance_tier,
            source_mode=source_mode,
            weather=weather,
            market_state=market_state,
        ),
        "warnings": warnings,
    }

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id,
        "row_id": "",
        "input_path": input_path,
        "input_line": input_line,
        "dataset_source": dataset_source,
        "provenance_tier": provenance_tier,
        "source_mode": source_mode,
        "source_provenance": source_provenance,
        "anti_hindsight": anti_hindsight,
        "market": market,
        "market_state": market_state,
        "decision": decision,
        "weather": weather,
        "resolution": resolution_payload,
        "quality": quality,
    }
    normalized["row_id"] = _row_id(normalized)
    return normalized


def dedupe_rows(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Deduplicate rows deterministically within provenance tier."""

    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    input_count = 0
    for row in rows:
        input_count += 1
        groups.setdefault(_dedupe_key(row), []).append(row)

    selected: list[dict[str, Any]] = []
    dropped_by_provenance: Counter[str] = Counter()
    for key in sorted(groups):
        candidates = sorted(groups[key], key=_dedupe_sort_key)
        selected.append(candidates[0])
        for dropped in candidates[1:]:
            dropped_by_provenance[str(dropped.get("provenance_tier") or "unknown")] += 1

    selected.sort(key=lambda row: (str(row["market"].get("event_date") or ""), row["row_id"]))
    stats = {
        "input_rows": input_count,
        "output_rows": len(selected),
        "dropped_rows": input_count - len(selected),
        "dropped_by_provenance_tier": dict(sorted(dropped_by_provenance.items())),
    }
    return selected, stats


def split_train_validation(
    rows: Iterable[dict[str, Any]],
    *,
    validation_fraction: float = 0.2,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Create a deterministic time-based split from usable training rows."""

    usable = [row for row in rows if bool(row.get("quality", {}).get("usable_for_training"))]
    usable.sort(key=lambda row: (str(row.get("market", {}).get("event_date") or ""), row["row_id"]))
    if not usable or validation_fraction <= 0:
        validation_count = 0
    else:
        validation_count = min(len(usable), max(1, math.ceil(len(usable) * validation_fraction)))
    train_count = len(usable) - validation_count
    train_rows = usable[:train_count]
    validation_rows = usable[train_count:]
    stats = {
        "method": "time_based_event_date",
        "validation_fraction": validation_fraction,
        "eligible_rows": len(usable),
        "train_rows": len(train_rows),
        "validation_rows": len(validation_rows),
        "validation_start_date": validation_rows[0]["market"].get("event_date") if validation_rows else None,
    }
    return train_rows, validation_rows, stats


def summarize_rows(
    rows: Iterable[dict[str, Any]],
    *,
    raw_input_rows: int | None = None,
    dedupe_stats: dict[str, Any] | None = None,
    split_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row_list = list(rows)
    event_dates = [str(row["market"].get("event_date")) for row in row_list if row["market"].get("event_date")]
    summary = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "raw_input_rows": raw_input_rows if raw_input_rows is not None else len(row_list),
        "normalized_rows": len(row_list),
        "dedupe": dedupe_stats or {},
        "split": split_stats or {},
        "counts": {
            "by_dataset_source": _count(row.get("dataset_source") for row in row_list),
            "by_provenance_tier": _count(row.get("provenance_tier") for row in row_list),
            "by_source_mode": _count(row.get("source_mode") for row in row_list),
            "by_market_family": _count(row.get("market", {}).get("market_family") for row in row_list),
            "by_outcome": _count(row.get("resolution", {}).get("outcome") for row in row_list),
            "by_city": _count(row.get("market", {}).get("city_id") or row.get("market", {}).get("city") for row in row_list),
        },
        "date_range": {
            "min_event_date": min(event_dates) if event_dates else None,
            "max_event_date": max(event_dates) if event_dates else None,
        },
        "quality": {
            "usable_for_training": sum(1 for row in row_list if row.get("quality", {}).get("usable_for_training")),
            "usable_for_production_replay": sum(1 for row in row_list if row.get("quality", {}).get("usable_for_production_replay")),
        },
        "missingness": {
            "missing_event_date": sum(1 for row in row_list if not row.get("market", {}).get("event_date")),
            "missing_city": sum(1 for row in row_list if not (row.get("market", {}).get("city_id") or row.get("market", {}).get("city"))),
            "missing_threshold": sum(1 for row in row_list if row.get("market", {}).get("threshold") is None),
            "missing_weather": sum(1 for row in row_list if row.get("weather", {}).get("actual_temp_used") is None),
            "missing_outcome": sum(1 for row in row_list if row.get("resolution", {}).get("outcome") == "unknown"),
        },
    }
    return summary


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open(encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield line_number, payload


def _dataset_source_for_path(path: Path, *, source_label: str) -> str:
    if source_label in {DATASET_FIRST_PARTY, DATASET_ARCHIVE_REPLAY}:
        return source_label
    rendered = str(path)
    if "/archive_replay/" in rendered or rendered.startswith("data/archive_replay/"):
        return DATASET_ARCHIVE_REPLAY
    return DATASET_FIRST_PARTY


def _extract_weather_snapshot(artifact: dict[str, Any]) -> dict[str, Any]:
    source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
    data = source_context.get("data") if isinstance(source_context.get("data"), dict) else {}
    snapshot = data.get("weather_source_snapshot")
    if isinstance(snapshot, dict) and snapshot:
        return snapshot
    snapshots = artifact.get("source_snapshots")
    if isinstance(snapshots, list):
        for source_snapshot in snapshots:
            if not isinstance(source_snapshot, dict):
                continue
            if str(source_snapshot.get("source") or source_snapshot.get("source_name") or "").lower() != "weather":
                continue
            resolved = _resolve_snapshot_ref(artifact, source_snapshot)
            if isinstance(resolved, dict) and resolved:
                return resolved
            if any(key in source_snapshot for key in ("forecast", "date_validation", "weather_date", "station_id")):
                return source_snapshot
    return {}


def _resolve_snapshot_ref(artifact: dict[str, Any], snapshot: dict[str, Any]) -> Any:
    ref = _string_or_none(snapshot.get("snapshot_ref"))
    if not ref:
        return None
    value: Any = artifact
    for part in ref.split("."):
        if part == "decision_artifact":
            continue
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _source_mode(source_artifact: dict[str, Any], source_context: dict[str, Any], weather_snapshot: dict[str, Any]) -> str:
    candidates: list[str] = []
    snapshots = source_artifact.get("source_snapshots")
    if isinstance(snapshots, list):
        for snapshot in snapshots:
            if isinstance(snapshot, dict):
                candidates.append(str(snapshot.get("mode") or snapshot.get("source_mode") or ""))
                candidates.append(str(snapshot.get("source") or ""))
    candidates.extend(
        [
            str(source_context.get("source_mode") or ""),
            str(source_context.get("mode") or ""),
            str(source_context.get("source") or ""),
            str(weather_snapshot.get("mode") or ""),
            str(weather_snapshot.get("source_mode") or ""),
            str((weather_snapshot.get("provenance") or {}).get("source_mode") or "") if isinstance(weather_snapshot.get("provenance"), dict) else "",
        ]
    )
    normalized = {candidate.strip().lower() for candidate in candidates if candidate and candidate.strip()}
    if normalized & {"live_current_forbidden", "live_current", "current", "live"}:
        return SOURCE_LIVE_CURRENT_FORBIDDEN
    if normalized & {"historical_post_facto", "historical", "historical_replay", "post_facto"}:
        return SOURCE_HISTORICAL_POST_FACTO if weather_snapshot else SOURCE_MISSING
    if SOURCE_RECORDED_AS_OF in normalized or "provided" in normalized:
        return SOURCE_RECORDED_AS_OF if weather_snapshot else SOURCE_MISSING
    return SOURCE_MISSING if not weather_snapshot else SOURCE_UNKNOWN


def _source_provenance(source_context: dict[str, Any], weather_snapshot: dict[str, Any], source_mode: str) -> str:
    provenance = source_context.get("provenance") if isinstance(source_context.get("provenance"), dict) else {}
    weather_provenance = weather_snapshot.get("provenance") if isinstance(weather_snapshot.get("provenance"), dict) else {}
    value = _string_or_none(
        source_context.get("source_provenance")
        or weather_snapshot.get("source_provenance")
        or provenance.get("source_provenance")
        or weather_provenance.get("source_provenance")
    )
    if value:
        return value
    if source_mode == SOURCE_RECORDED_AS_OF:
        return PROVENANCE_RECORDED_COLLECTION
    if source_mode == SOURCE_HISTORICAL_POST_FACTO:
        return PROVENANCE_HISTORICAL_POST_FACTO_BACKFILL
    if source_mode == SOURCE_MISSING:
        return PROVENANCE_MISSING
    return PROVENANCE_UNKNOWN


def _provenance_tier(
    *,
    dataset_source: str,
    market_family: str,
    source_mode: str,
    weather_snapshot: dict[str, Any],
) -> str:
    if market_family not in SUPPORTED_MARKET_FAMILIES:
        return TIER_UNKNOWN_OR_UNSUPPORTED
    if dataset_source == DATASET_ARCHIVE_REPLAY:
        if not weather_snapshot:
            return TIER_ARCHIVE_MISSING_WEATHER
        if source_mode == SOURCE_HISTORICAL_POST_FACTO:
            return TIER_ARCHIVE_HISTORICAL_POST_FACTO
        return TIER_UNKNOWN_OR_UNSUPPORTED
    if dataset_source == DATASET_FIRST_PARTY:
        if source_mode == SOURCE_RECORDED_AS_OF and weather_snapshot:
            return TIER_FIRST_PARTY_RECORDED_AS_OF
        return TIER_FIRST_PARTY_MISSING_SOURCE
    return TIER_UNKNOWN_OR_UNSUPPORTED


def _anti_hindsight_marker(source_mode: str) -> str:
    if source_mode == SOURCE_HISTORICAL_POST_FACTO:
        return ANTI_HINDSIGHT_POST_FACTO
    if source_mode == SOURCE_RECORDED_AS_OF:
        return ANTI_HINDSIGHT_RECORDED
    return ANTI_HINDSIGHT_MISSING


def _date_validation(row: dict[str, Any], weather_snapshot: dict[str, Any]) -> dict[str, Any] | None:
    candidate = weather_snapshot.get("date_validation")
    if isinstance(candidate, dict):
        return dict(candidate)
    if weather_snapshot:
        return validate_weather_date_match(row, weather_snapshot).as_dict()
    return None


def _derived_market_date(row: dict[str, Any], *, market_id: str, question: str, event_ticker: str | None) -> str | None:
    market_date = derive_market_date(
        {
            "market_id": market_id,
            "market_ticker": market_id,
            "event_ticker": event_ticker,
            "question": question,
            "event_date": row.get("event_date"),
            "market_date": row.get("market_date"),
        }
    )
    return market_date.isoformat


def _weather_payload(
    weather_snapshot: dict[str, Any],
    weather_forecast: dict[str, Any],
    date_validation: dict[str, Any] | None,
) -> dict[str, Any]:
    signal_data = _signal_data(weather_snapshot)
    sources = weather_snapshot.get("sources")
    first_source = sources[0] if isinstance(sources, list) and sources and isinstance(sources[0], dict) else {}
    return {
        "weather_date": _string_or_none(weather_snapshot.get("weather_date") or weather_snapshot.get("target_forecast_date") or (date_validation or {}).get("weather_date")),
        "high": _float_or_none(weather_forecast.get("high") or signal_data.get("forecast_high") or signal_data.get("historical_high")),
        "low": _float_or_none(weather_forecast.get("low") or signal_data.get("forecast_low") or signal_data.get("historical_low")),
        "current": _float_or_none(weather_forecast.get("current") or signal_data.get("current")),
        "actual_temp_used": _float_or_none(weather_forecast.get("actual_temp_used") or signal_data.get("actual_temp_used")),
        "source": _string_or_none(weather_snapshot.get("settlement_source") or first_source.get("source_name") or signal_data.get("source_quality")) or "unknown",
        "station_id": _string_or_none(weather_snapshot.get("station_id") or first_source.get("station_id")),
        "station_cli": _string_or_none(weather_snapshot.get("station_cli") or first_source.get("station_cli")),
        "station_mapping": _string_or_none(weather_snapshot.get("station_mapping")),
        "date_validation_ok": bool((date_validation or {}).get("ok")),
        "date_validation_reason": _string_or_none((date_validation or {}).get("reason")) or "missing",
    }


def _market_state_payload(row: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    order_book_snapshot = artifact.get("order_book_snapshot") if isinstance(artifact.get("order_book_snapshot"), dict) else {}
    order_book_data = order_book_snapshot.get("data") if isinstance(order_book_snapshot.get("data"), dict) else {}
    order_book = artifact.get("order_book") if isinstance(artifact.get("order_book"), dict) else {}
    spread = _float_or_none(order_book_data.get("spread") or order_book.get("spread"))
    yes_price = _float_or_none(row.get("yes_price") or row.get("yes_market_price"))
    no_price = _float_or_none(row.get("no_price") or row.get("no_market_price"))
    weather_risk = row.get("weather_risk") if isinstance(row.get("weather_risk"), dict) else {}
    weather_risk_evidence = weather_risk.get("evidence") if isinstance(weather_risk.get("evidence"), dict) else {}
    return {
        "yes_price": yes_price,
        "no_price": no_price,
        "spread": spread,
        "volume": _float_or_none(row.get("volume") or weather_risk_evidence.get("market_volume")),
        "liquidity": _float_or_none(row.get("liquidity")),
        "order_book_mode": _order_book_mode(artifact),
        "order_book": {
            "best_yes_ask": _float_or_none(order_book_data.get("best_yes_ask") or order_book.get("best_yes_ask")),
            "best_yes_bid": _float_or_none(order_book_data.get("best_yes_bid") or order_book.get("best_yes_bid")),
            "best_no_ask": _float_or_none(order_book_data.get("best_no_ask") or order_book.get("best_no_ask")),
            "best_no_bid": _float_or_none(order_book_data.get("best_no_bid") or order_book.get("best_no_bid")),
            "mid_yes": _float_or_none(order_book_data.get("mid_yes") or order_book.get("mid_yes")),
        },
    }


def _decision_payload(row: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    ensemble = ((artifact.get("strategy_trace") or {}).get("ensemble_signal") or {}) if isinstance(artifact.get("strategy_trace"), dict) else {}
    shared_pipeline = row.get("shared_pipeline") if isinstance(row.get("shared_pipeline"), dict) else {}
    return {
        "final_action": _string_or_none(artifact.get("final_action") or shared_pipeline.get("final_action") or row.get("direction")) or "unknown",
        "final_reason_code": _string_or_none(artifact.get("final_reason_code") or shared_pipeline.get("final_reason_code")),
        "model_probability": _float_or_none(ensemble.get("model_probability") or ensemble.get("predicted_prob")),
        "model_edge": _float_or_none(ensemble.get("edge") or row.get("edge")),
        "strategy_version": _string_or_none(row.get("strategy_version") or artifact.get("logic_version")),
    }


def _resolution_payload(row: dict[str, Any], market_metadata: dict[str, Any], resolution: dict[str, Any] | None) -> dict[str, Any]:
    resolution_dict = row.get("resolution") if isinstance(row.get("resolution"), dict) else {}
    outcome = _normalize_outcome(
        (resolution or {}).get("outcome")
        or resolution_dict.get("outcome")
        or row.get("outcome")
        or market_metadata.get("outcome")
        or market_metadata.get("result")
    )
    return {
        "outcome": outcome or "unknown",
        "resolved_at": _string_or_none((resolution or {}).get("resolved_at") or resolution_dict.get("resolved_at") or row.get("resolved_at")),
    }


def _quality_warnings(
    *,
    provenance_tier: str,
    source_mode: str,
    weather: dict[str, Any],
    resolution: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    if source_mode == SOURCE_HISTORICAL_POST_FACTO:
        warnings.append("historical_post_facto_not_production_replay_grade")
    if provenance_tier == TIER_UNKNOWN_OR_UNSUPPORTED:
        warnings.append("unknown_or_unsupported_market")
    if weather.get("actual_temp_used") is None:
        warnings.append("missing_weather")
    if not weather.get("date_validation_ok"):
        warnings.append(f"date_validation:{weather.get('date_validation_reason') or 'missing'}")
    if resolution.get("outcome") not in RESOLVED_OUTCOMES:
        warnings.append("missing_outcome")
    return warnings


def _usable_for_training(provenance_tier: str, weather: dict[str, Any], resolution: dict[str, Any]) -> bool:
    return (
        provenance_tier in {TIER_FIRST_PARTY_RECORDED_AS_OF, TIER_ARCHIVE_HISTORICAL_POST_FACTO}
        and weather.get("actual_temp_used") is not None
        and bool(weather.get("date_validation_ok"))
        and resolution.get("outcome") in RESOLVED_OUTCOMES
    )


def _usable_for_production_replay(
    *,
    dataset_source: str,
    provenance_tier: str,
    source_mode: str,
    weather: dict[str, Any],
    market_state: dict[str, Any],
) -> bool:
    return (
        dataset_source == DATASET_FIRST_PARTY
        and provenance_tier == TIER_FIRST_PARTY_RECORDED_AS_OF
        and source_mode == SOURCE_RECORDED_AS_OF
        and bool(weather.get("date_validation_ok"))
        and market_state.get("order_book_mode") == "recorded_book"
    )


def _order_book_mode(artifact: dict[str, Any]) -> str:
    for key in ("order_book_snapshot", "pre_logic_order_book_snapshot"):
        snapshot = artifact.get(key) if isinstance(artifact.get(key), dict) else {}
        source = str(snapshot.get("source") or "").lower()
        data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else {}
        if source in {"book", "recorded_book"} and _has_book_prices(data):
            return "recorded_book"
        if source in {"fallback", "signal_price_fallback"} and _has_book_prices(data):
            return "signal_price_fallback"
        if source == "synthetic" and _has_book_prices(data):
            return "synthetic"
    order_book = artifact.get("order_book") if isinstance(artifact.get("order_book"), dict) else {}
    if _has_book_prices(order_book):
        return "recorded_book"
    return "missing"


def _has_book_prices(value: dict[str, Any]) -> bool:
    return any(_float_or_none(value.get(key)) is not None for key in ("best_yes_ask", "best_yes_bid", "best_no_ask", "best_no_bid", "mid_yes"))


def _market_family(market_route: dict[str, Any], route_evidence: dict[str, Any], market_metadata: dict[str, Any]) -> str:
    return _string_or_none(market_route.get("family") or route_evidence.get("market_family") or market_metadata.get("market_family")) or "unknown"


def _market_type(market_route: dict[str, Any], route_evidence: dict[str, Any]) -> str:
    return _string_or_none(market_route.get("subcategory") or route_evidence.get("shape") or market_route.get("family")) or "unknown"


def _city_id(weather_snapshot: dict[str, Any], row: dict[str, Any]) -> str | None:
    station_resolution = weather_snapshot.get("station_resolution") if isinstance(weather_snapshot.get("station_resolution"), dict) else {}
    risk = row.get("weather_risk") if isinstance(row.get("weather_risk"), dict) else {}
    risk_evidence = risk.get("evidence") if isinstance(risk.get("evidence"), dict) else {}
    risk_station = risk_evidence.get("weather_station_resolution") if isinstance(risk_evidence.get("weather_station_resolution"), dict) else {}
    value = _string_or_none(station_resolution.get("city_id") or risk_station.get("city_id"))
    if value:
        return value
    city = _city_name(weather_snapshot, row)
    return _slug(city) if city else None


def _city_name(weather_snapshot: dict[str, Any], row: dict[str, Any]) -> str | None:
    station_resolution = weather_snapshot.get("station_resolution") if isinstance(weather_snapshot.get("station_resolution"), dict) else {}
    signal_city = _signal_data(weather_snapshot).get("city")
    risk = row.get("weather_risk") if isinstance(row.get("weather_risk"), dict) else {}
    risk_evidence = risk.get("evidence") if isinstance(risk.get("evidence"), dict) else {}
    risk_station = risk_evidence.get("weather_station_resolution") if isinstance(risk_evidence.get("weather_station_resolution"), dict) else {}
    return _string_or_none(station_resolution.get("city") or risk_station.get("city") or signal_city)


def _signal_data(weather_snapshot: dict[str, Any]) -> dict[str, Any]:
    source_signal = weather_snapshot.get("source_signal") if isinstance(weather_snapshot.get("source_signal"), dict) else {}
    data = source_signal.get("data") if isinstance(source_signal.get("data"), dict) else {}
    return data


def _bucket_bounds(row: dict[str, Any], *, question: str, market_id: str) -> tuple[float | None, float | None]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    low = _float_or_none(metadata.get("bucket_low") or row.get("bucket_low"))
    high = _float_or_none(metadata.get("bucket_high") or row.get("bucket_high"))
    if low is not None or high is not None:
        return low, high
    match = re.search(r"-B(-?\d+(?:\.\d+)?)", market_id.upper())
    if match:
        return float(match.group(1)), None
    match = re.search(r"\bbetween\s+(-?\d+(?:\.\d+)?)\s+(?:and|to|-)\s+(-?\d+(?:\.\d+)?)", question, flags=re.IGNORECASE)
    if match:
        return float(match.group(1)), float(match.group(2))
    return None, None


def _event_ticker_from_market_id(market_id: str) -> str | None:
    if "-" not in market_id:
        return None
    return market_id.rsplit("-", 1)[0]


def _series_ticker_from_market_id(market_id: str) -> str | None:
    if "-" not in market_id:
        return None
    return market_id.split("-", 1)[0]


def _first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _normalize_outcome(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if normalized in RESOLVED_OUTCOMES:
        return normalized
    if normalized in {"TRUE", "1", "YES_WIN"}:
        return "YES"
    if normalized in {"FALSE", "0", "NO_WIN"}:
        return "NO"
    return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _slug(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or None


def _row_id(row: dict[str, Any]) -> str:
    identity = {
        "schema_version": row["schema_version"],
        "dataset_source": row["dataset_source"],
        "provenance_tier": row["provenance_tier"],
        "source_mode": row["source_mode"],
        "market": row["market"],
        "input_path": row["input_path"],
        "input_line": row["input_line"],
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def _dedupe_key(row: dict[str, Any]) -> tuple[Any, ...]:
    market = row.get("market", {})
    return (
        market.get("market_id"),
        market.get("event_date"),
        market.get("threshold"),
        market.get("bucket_low"),
        market.get("bucket_high"),
        market.get("question_side"),
        row.get("provenance_tier"),
    )


def _dedupe_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    quality = row.get("quality", {})
    weather = row.get("weather", {})
    resolution = row.get("resolution", {})
    return (
        -int(bool(quality.get("usable_for_production_replay"))),
        -int(bool(quality.get("usable_for_training"))),
        -int(resolution.get("outcome") in RESOLVED_OUTCOMES),
        -int(weather.get("actual_temp_used") is not None),
        -int(bool(weather.get("date_validation_ok"))),
        PROVENANCE_PRIORITY.get(str(row.get("provenance_tier")), 99),
        str(row.get("input_path") or ""),
        int(row.get("input_line") or 0),
        str(row.get("row_id") or ""),
    )


def _count(values: Iterable[Any]) -> dict[str, int]:
    counts = Counter(str(value) if value not in (None, "") else "missing" for value in values)
    return dict(sorted(counts.items()))
