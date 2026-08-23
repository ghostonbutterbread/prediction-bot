"""Bounded compatibility accounting for strict source evidence in replay inputs."""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping


_REQUIRED_SOURCE_FIELDS = (
    "source_id",
    "source_as_of",
    "source_location_city",
    "source_target_date",
    "source_timezone",
    "forecast_measurement_kind",
    "contract_shape",
    "question_side",
    "raw_row_sha256",
)


def summarize_strict_source_replay_compatibility(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Report whether sealed replay inputs preserve every strict source field."""
    missing: Counter[str] = Counter()
    report: Counter[str] = Counter()
    for record in records:
        report["records_seen"] += 1
        sources = _sources(record)
        if not sources:
            report["records_without_weather_sources"] += 1
            continue
        raw_hash = _mapping(record.get("snapshot_provenance")).get("raw_row_sha256")
        for source in sources:
            report["source_rows_seen"] += 1
            values = {
                "source_id": source.get("source_id"),
                "source_as_of": source.get("source_as_of"),
                "source_location_city": source.get("source_location_city"),
                "source_target_date": source.get("source_target_date"),
                "source_timezone": _mapping(source.get("target_mapping")).get("source_timezone"),
                "forecast_measurement_kind": source.get("forecast_measurement_kind"),
                "contract_shape": source.get("contract_shape"),
                "question_side": source.get("question_side"),
                "raw_row_sha256": raw_hash,
            }
            absent = [field for field in _REQUIRED_SOURCE_FIELDS if not _present(values[field])]
            if absent:
                report["strict_contract_incomplete"] += 1
                missing.update(absent)
            else:
                report["strict_contract_complete"] += 1
    return {
        **{key: int(value) for key, value in report.items()},
        "missing_field_counts": dict(sorted(missing.items())),
        "required_source_fields": list(_REQUIRED_SOURCE_FIELDS),
    }


def _sources(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    source_inputs = _mapping(record.get("source_inputs"))
    context = _mapping(source_inputs.get("source_context"))
    data = _mapping(context.get("data"))
    snapshot = _mapping(data.get("weather_source_snapshot"))
    sources = snapshot.get("sources")
    return [source for source in sources if isinstance(source, Mapping)] if isinstance(sources, list) else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _present(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
