"""Offline weather source scoreboard helpers.

This module reads already-recorded Prediction Lab/Paper rows and scores embedded
source forecasts against actual temperatures available in the row or in a
caller-provided lookup. It performs no network access and does not mutate
runtime state.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bot.weather.thresholds import (
    extract_threshold_value,
    infer_direction_from_value,
    infer_question_side,
)


SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1
WITHIN_BUCKETS_F = (1, 2, 3, 5)
DEFAULT_REPORT_LIMIT = 25


@dataclass(frozen=True)
class MarketContext:
    market_id: str | None = None
    city_id: str | None = None
    city_name: str | None = None
    market_kind: str = "unknown"
    contract_shape: str = "unknown"
    question_side: str = "unknown"
    threshold: float | None = None
    market_date: str | None = None
    question: str | None = None


@dataclass(frozen=True)
class SourceForecastObservation:
    source_id: str
    source_name: str
    forecast_temp_f: float | None
    actual_temp_f: float | None
    market: MarketContext
    missing_reasons: tuple[str, ...] = ()


@dataclass
class SliceAccumulator:
    source_id: str
    source_name: str
    city_id: str
    city_name: str | None
    market_kind: str
    contract_shape: str
    total_observations: int = 0
    sample_count: int = 0
    absolute_error_sum: float = 0.0
    bias_sum: float = 0.0
    threshold_sample_count: int = 0
    threshold_correct_count: int = 0
    within_counts: Counter[int] = field(default_factory=Counter)
    question_sides: Counter[str] = field(default_factory=Counter)
    missing: Counter[str] = field(default_factory=Counter)

    def add(self, observation: SourceForecastObservation) -> None:
        self.total_observations += 1
        side = observation.market.question_side or "unknown"
        self.question_sides[side] += 1

        for reason in observation.missing_reasons:
            self.missing[reason] += 1

        forecast = observation.forecast_temp_f
        actual = observation.actual_temp_f
        if forecast is None or actual is None:
            if forecast is None:
                self.missing["missing_forecast_temp"] += 1
            if actual is None:
                self.missing["missing_actual_temp"] += 1
            return

        error = forecast - actual
        absolute_error = abs(error)
        self.sample_count += 1
        self.absolute_error_sum += absolute_error
        self.bias_sum += error
        for bucket in WITHIN_BUCKETS_F:
            if absolute_error <= bucket:
                self.within_counts[bucket] += 1

        threshold = observation.market.threshold
        forecast_direction = infer_direction_from_value(forecast, threshold)
        actual_direction = infer_direction_from_value(actual, threshold)
        if forecast_direction and actual_direction:
            self.threshold_sample_count += 1
            if forecast_direction == actual_direction:
                self.threshold_correct_count += 1
        elif threshold is None:
            self.missing["missing_threshold"] += 1
        else:
            self.missing["threshold_tie"] += 1

    def as_dict(self) -> dict[str, Any]:
        sample_count = self.sample_count
        threshold_count = self.threshold_sample_count
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "city_id": self.city_id,
            "city_name": self.city_name,
            "market_kind": self.market_kind,
            "contract_shape": self.contract_shape,
            "question_sides": dict(sorted(self.question_sides.items())),
            "total_observations": self.total_observations,
            "sample_count": sample_count,
            "mae": _round_metric(self.absolute_error_sum / sample_count) if sample_count else None,
            "mean_bias": _round_metric(self.bias_sum / sample_count) if sample_count else None,
            "threshold_sample_count": threshold_count,
            "threshold_direction_accuracy": _round_metric(self.threshold_correct_count / threshold_count)
            if threshold_count
            else None,
            "threshold_correct_count": self.threshold_correct_count,
            "within_1f_rate": _rate(self.within_counts[1], sample_count),
            "within_2f_rate": _rate(self.within_counts[2], sample_count),
            "within_3f_rate": _rate(self.within_counts[3], sample_count),
            "within_5f_rate": _rate(self.within_counts[5], sample_count),
            "missing": dict(sorted(self.missing.items())),
        }


def build_source_scoreboard(
    rows: Iterable[dict[str, Any]],
    *,
    actual_lookup: dict[Any, Any] | None = None,
) -> dict[str, Any]:
    """Build a compact scoreboard from in-memory row dictionaries."""

    counters: Counter[str] = Counter()
    slices: dict[tuple[str, str, str, str, str], SliceAccumulator] = {}
    actual_lookup = actual_lookup or {}

    for row in rows:
        counters["input_rows"] += 1
        if not isinstance(row, dict):
            counters["unknown_rows"] += 1
            continue

        observations = extract_source_forecast_observations(row, actual_lookup=actual_lookup)
        if not observations:
            counters["rows_without_observations"] += 1
            continue

        counters["rows_with_observations"] += 1
        for observation in observations:
            counters["observations_extracted"] += 1
            if observation.forecast_temp_f is None:
                counters["observations_missing_forecast"] += 1
            if observation.actual_temp_f is None:
                counters["observations_missing_actual"] += 1
            if observation.forecast_temp_f is not None and observation.actual_temp_f is not None:
                counters["observations_scored"] += 1

            key = (
                observation.source_id,
                observation.source_name,
                observation.market.city_id or "unknown",
                observation.market.market_kind or "unknown",
                observation.market.contract_shape or "unknown",
            )
            if key not in slices:
                slices[key] = SliceAccumulator(
                    source_id=observation.source_id,
                    source_name=observation.source_name,
                    city_id=observation.market.city_id or "unknown",
                    city_name=observation.market.city_name,
                    market_kind=observation.market.market_kind or "unknown",
                    contract_shape=observation.market.contract_shape or "unknown",
                )
            slices[key].add(observation)

    slice_rows = sorted((accumulator.as_dict() for accumulator in slices.values()), key=_slice_sort_key)
    for key in (
        "input_rows",
        "unknown_rows",
        "rows_without_observations",
        "rows_with_observations",
        "observations_extracted",
        "observations_missing_forecast",
        "observations_missing_actual",
        "observations_scored",
    ):
        counters.setdefault(key, 0)

    summary = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **dict(sorted(counters.items())),
        "slice_count": len(slice_rows),
    }
    return {"schema_version": SCHEMA_VERSION, "summary": summary, "slices": slice_rows}


def build_scoreboard_report(
    scoreboard: dict[str, Any],
    *,
    run_metadata: dict[str, Any] | None = None,
    limit: int = DEFAULT_REPORT_LIMIT,
) -> dict[str, Any]:
    """Build repeatable offline report slices and leaderboards from a scoreboard."""

    slices = [row for row in scoreboard.get("slices", []) if isinstance(row, dict)]
    report_limit = max(0, limit)
    leaderboards = {
        "sources": _leaderboard(slices, ("source_id", "source_name"), "source"),
        "cities": _leaderboard(slices, ("city_id", "city_name"), "city"),
        "types": _leaderboard(slices, ("market_kind", "contract_shape"), "type"),
    }
    notes = build_missing_data_notes(scoreboard)
    generated_at = None
    if isinstance(run_metadata, dict):
        generated_at = _string_or_none(run_metadata.get("generated_at"))
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "scoreboard_schema_version": scoreboard.get("schema_version", SCHEMA_VERSION),
        "generated_at": generated_at,
        "report_limit": report_limit,
        "summary": dict(scoreboard.get("summary", {})),
        "run_metadata": dict(run_metadata or {}),
        "missing_data_notes": notes,
        "best_slices": best_slices(slices, limit=report_limit),
        "worst_slices": worst_slices(slices, limit=report_limit),
        "leaderboards": {
            name: rows[:report_limit] if report_limit else []
            for name, rows in leaderboards.items()
        },
        "leaderboard_totals": {
            name: len(rows)
            for name, rows in leaderboards.items()
        },
    }


def build_missing_data_notes(scoreboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Return conservative notes explaining data gaps that affect scoring."""

    summary = scoreboard.get("summary", {}) if isinstance(scoreboard.get("summary"), dict) else {}
    slices = [row for row in scoreboard.get("slices", []) if isinstance(row, dict)]
    notes: list[dict[str, Any]] = []

    def add(code: str, message: str, count: int | None = None) -> None:
        note: dict[str, Any] = {"code": code, "severity": "info", "message": message}
        if count is not None:
            note["count"] = count
        notes.append(note)

    rows_without_observations = int(summary.get("rows_without_observations") or 0)
    missing_forecast = int(summary.get("observations_missing_forecast") or 0)
    missing_actual = int(summary.get("observations_missing_actual") or 0)
    observations_scored = int(summary.get("observations_scored") or 0)
    observations_extracted = int(summary.get("observations_extracted") or 0)
    if rows_without_observations:
        add(
            "rows_without_source_observations",
            "Some input rows did not expose source forecast observations and are excluded from slice scoring.",
            rows_without_observations,
        )
    if missing_forecast:
        add(
            "missing_forecast_temperatures",
            "Some source observations named a source but did not include a usable forecast temperature.",
            missing_forecast,
        )
    if missing_actual:
        add(
            "missing_actual_temperatures",
            "Some source observations lacked a usable actual or resolved temperature, so error metrics omit them.",
            missing_actual,
        )
    if observations_extracted and observations_scored == 0:
        add(
            "no_scored_observations",
            "No observations had both forecast and actual temperatures; rankings are coverage-only placeholders.",
            observations_extracted,
        )

    missing_threshold = _sum_missing(slices, "missing_threshold")
    threshold_ties = _sum_missing(slices, "threshold_tie")
    if missing_threshold:
        add(
            "missing_thresholds",
            "Some scored observations could not be used for threshold direction accuracy because no threshold was available.",
            missing_threshold,
        )
    if threshold_ties:
        add(
            "threshold_ties",
            "Some scored observations landed exactly on the threshold and are omitted from direction accuracy.",
            threshold_ties,
        )

    date_validation_failures = sum(
        count
        for row in slices
        for reason, count in (row.get("missing") or {}).items()
        if str(reason).startswith("date_validation_failed:")
    )
    if date_validation_failures:
        add(
            "date_validation_failures",
            "Some observations failed market/weather date validation and are treated as missing actual temperatures.",
            int(date_validation_failures),
        )
    if not notes:
        add(
            "no_missing_data_flags",
            "No missing-data flags were emitted by the offline scorer for this run.",
            0,
        )
    return notes


def best_slices(rows: list[dict[str, Any]], *, limit: int = DEFAULT_REPORT_LIMIT) -> list[dict[str, Any]]:
    return sorted(rows, key=_quality_sort_key, reverse=True)[: max(0, limit)]


def worst_slices(rows: list[dict[str, Any]], *, limit: int = DEFAULT_REPORT_LIMIT) -> list[dict[str, Any]]:
    scored = [row for row in rows if row.get("sample_count")]
    return sorted(scored, key=_risk_sort_key)[: max(0, limit)]


def render_slices_markdown(title: str, rows: list[dict[str, Any]]) -> str:
    lines = [
        f"# {title}",
        "",
        "| source | city | kind | shape | obs | scored | mae | bias | dir_acc | within_3f | missing |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    if not rows:
        lines.append("| none | none | none | none | 0 | 0 | n/a | n/a | n/a | n/a | 0 |")
    for row in rows:
        lines.append(
            "| "
            f"{_markdown_cell(row.get('source_name'))} | "
            f"{_markdown_cell(row.get('city_id'))} | "
            f"{_markdown_cell(row.get('market_kind'))} | "
            f"{_markdown_cell(row.get('contract_shape'))} | "
            f"{row.get('total_observations') or 0} | "
            f"{row.get('sample_count') or 0} | "
            f"{_format_metric(row.get('mae'))} | "
            f"{_format_metric(row.get('mean_bias'))} | "
            f"{_format_metric(row.get('threshold_direction_accuracy'))} | "
            f"{_format_metric(row.get('within_3f_rate'))} | "
            f"{sum((row.get('missing') or {}).values())} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_leaderboard_markdown(title: str, rows: list[dict[str, Any]]) -> str:
    lines = [
        f"# {title}",
        "",
        "| rank | label | slices | obs | scored | coverage | mae | bias | dir_acc | within_3f | missing |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    if not rows:
        lines.append("| 0 | none | 0 | 0 | 0 | n/a | n/a | n/a | n/a | n/a | 0 |")
    for row in rows:
        lines.append(
            "| "
            f"{row.get('rank') or 0} | "
            f"{_markdown_cell(row.get('label'))} | "
            f"{row.get('slice_count') or 0} | "
            f"{row.get('total_observations') or 0} | "
            f"{row.get('sample_count') or 0} | "
            f"{_format_metric(row.get('coverage_rate'))} | "
            f"{_format_metric(row.get('mae'))} | "
            f"{_format_metric(row.get('mean_bias'))} | "
            f"{_format_metric(row.get('threshold_direction_accuracy'))} | "
            f"{_format_metric(row.get('within_3f_rate'))} | "
            f"{sum((row.get('missing') or {}).values())} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_scoreboard_report_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    run_metadata = report.get("run_metadata", {}) if isinstance(report.get("run_metadata"), dict) else {}
    leaderboards = report.get("leaderboards", {}) if isinstance(report.get("leaderboards"), dict) else {}
    notes = report.get("missing_data_notes") if isinstance(report.get("missing_data_notes"), list) else []

    lines = [
        "# Weather Source Scoreboard Report",
        "",
        "## Run Metadata",
        "",
        f"- generated_at: {_markdown_cell(report.get('generated_at'))}",
        f"- mode: {_markdown_cell(run_metadata.get('mode'))}",
        f"- network_access: {_markdown_cell(run_metadata.get('network_access'))}",
        f"- inputs: {len(run_metadata.get('inputs') or [])}",
        f"- limit: {_markdown_cell(run_metadata.get('limit'))}",
        "",
        "## Summary",
        "",
        "| rows | observations | scored | slices | missing_actual | missing_forecast |",
        "|---:|---:|---:|---:|---:|---:|",
        "| "
        f"{summary.get('input_rows') or 0} | "
        f"{summary.get('observations_extracted') or 0} | "
        f"{summary.get('observations_scored') or 0} | "
        f"{summary.get('slice_count') or 0} | "
        f"{summary.get('observations_missing_actual') or 0} | "
        f"{summary.get('observations_missing_forecast') or 0} |",
        "",
        "## Missing Data Notes",
        "",
    ]
    for note in notes:
        if not isinstance(note, dict):
            continue
        count = note.get("count")
        suffix = f" ({count})" if count is not None else ""
        lines.append(f"- {note.get('code', 'note')}: {note.get('message', '')}{suffix}")
    if not notes:
        lines.append("- none")
    lines.extend(
        [
            "",
            render_leaderboard_markdown("Source Leaderboard", list(leaderboards.get("sources") or [])).rstrip(),
            "",
            render_leaderboard_markdown("City Leaderboard", list(leaderboards.get("cities") or [])).rstrip(),
            "",
            render_leaderboard_markdown("Type Leaderboard", list(leaderboards.get("types") or [])).rstrip(),
            "",
            render_slices_markdown("Best Slices", list(report.get("best_slices") or [])).rstrip(),
            "",
            render_slices_markdown("Worst Slices", list(report.get("worst_slices") or [])).rstrip(),
            "",
        ]
    )
    return "\n".join(lines)


def extract_source_forecast_observations(
    row: dict[str, Any],
    *,
    actual_lookup: dict[Any, Any] | None = None,
) -> list[SourceForecastObservation]:
    """Extract source forecast observations from a single raw row."""

    actual_lookup = actual_lookup or {}
    market = extract_market_context(row)
    sources = _extract_source_details(row)
    if not sources:
        return []

    actual_temp = _actual_temp_for(row, market, actual_lookup)
    date_validation_problem = _date_validation_problem(row, market)
    if date_validation_problem:
        actual_temp = None
    observations: list[SourceForecastObservation] = []
    seen: set[tuple[str, str, float | None]] = set()
    for source in sources:
        source_id, source_name = _source_identity(source)
        if not source_id and not source_name:
            continue
        source_name = source_name or source_id or "unknown"
        source_id = source_id or _slug(source_name) or "unknown"
        forecast_temp = _forecast_temp_for_source(source, market.market_kind)
        key = (source_id, source_name, forecast_temp)
        if key in seen:
            continue
        seen.add(key)
        missing = []
        if market.city_id is None:
            missing.append("missing_city")
        if market.market_kind == "unknown":
            missing.append("missing_market_kind")
        if date_validation_problem:
            missing.append(date_validation_problem)
        observations.append(
            SourceForecastObservation(
                source_id=source_id,
                source_name=source_name,
                forecast_temp_f=forecast_temp,
                actual_temp_f=actual_temp,
                market=market,
                missing_reasons=tuple(missing),
            )
        )
    return observations


def extract_market_context(row: dict[str, Any]) -> MarketContext:
    """Extract market/city/threshold/date context from known row shapes."""

    artifact = _dict_at(row, "decision_artifact")
    source_context_data = _dict_at(artifact, "source_context", "data")
    weather_snapshot = _extract_weather_snapshot(row)
    live_data = _live_signal_data(row)
    market = _dict_at(row, "market")
    market_metadata = _first_dict(source_context_data.get("market_metadata"), row.get("market_metadata"), row.get("metadata"))
    route_evidence = _dict_at(row, "market_route", "evidence")
    if not route_evidence:
        route_evidence = _dict_at(artifact, "market_route", "evidence")
    forecast = _dict_at(weather_snapshot, "forecast")
    station_resolution = _first_dict(
        weather_snapshot.get("station_resolution"),
        live_data.get("station_resolution"),
        _dict_at(row, "weather_risk", "evidence", "weather_station_resolution"),
    )

    question = _string_or_none(
        row.get("question")
        or artifact.get("question")
        or weather_snapshot.get("question")
        or market.get("question")
        or route_evidence.get("question")
        or market_metadata.get("question")
    )
    market_id = _string_or_none(
        row.get("market_id")
        or row.get("ticker")
        or artifact.get("market_id")
        or weather_snapshot.get("market_id")
        or market.get("market_id")
        or market.get("ticker")
    )
    city_id = _string_or_none(
        market.get("city_id")
        or row.get("city_id")
        or station_resolution.get("city_id")
        or live_data.get("city_id")
        or weather_snapshot.get("city_id")
    )
    city_name = _string_or_none(
        market.get("city")
        or row.get("city")
        or station_resolution.get("city")
        or live_data.get("city")
        or weather_snapshot.get("city")
    )
    if not city_id:
        city_id = _slug(city_name)

    question_side = _string_or_none(
        market.get("question_side")
        or row.get("question_side")
        or forecast.get("question_side")
        or live_data.get("question_side")
    )
    if not question_side:
        question_side = infer_question_side(question or "", {**route_evidence, **market_metadata})
    question_side = question_side or "unknown"

    threshold = _first_float(
        market.get("threshold"),
        row.get("threshold"),
        forecast.get("threshold"),
        live_data.get("threshold"),
        market_metadata.get("threshold"),
        route_evidence.get("threshold"),
    )
    if threshold is None:
        threshold = extract_threshold_value(question or "", {**route_evidence, **market_metadata})

    market_kind = _infer_market_kind(
        question=question,
        market_id=market_id,
        market_kind=_string_or_none(market.get("market_kind") or row.get("market_kind")),
        market_type=_string_or_none(market.get("market_type") or row.get("market_type") or route_evidence.get("market_type")),
    )
    contract_shape = _infer_contract_shape(
        question_side=question_side,
        market_id=market_id,
        question=question,
        row=row,
        market=market,
    )
    market_date = _string_or_none(
        market.get("event_date")
        or market.get("market_date")
        or row.get("event_date")
        or row.get("market_date")
        or forecast.get("market_date")
        or live_data.get("market_date")
        or live_data.get("target_forecast_date")
        or weather_snapshot.get("market_date")
        or weather_snapshot.get("target_forecast_date")
        or weather_snapshot.get("weather_date")
    )

    return MarketContext(
        market_id=market_id,
        city_id=city_id,
        city_name=city_name,
        market_kind=market_kind,
        contract_shape=contract_shape,
        question_side=question_side,
        threshold=threshold,
        market_date=market_date,
        question=question,
    )


def load_jsonl_rows(paths: Iterable[str | Path], *, limit: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load JSONL rows from explicit paths with conservative bad-line handling."""

    rows: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()
    for path_value in paths:
        path = Path(path_value)
        stats["input_files"] += 1
        with path.open(encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, start=1):
                if limit is not None and len(rows) >= limit:
                    stats["limit_reached"] = 1
                    return rows, dict(stats)
                stats["lines_read"] += 1
                line = line.strip()
                if not line:
                    stats["blank_lines"] += 1
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    stats["invalid_json_lines"] += 1
                    continue
                if not isinstance(payload, dict):
                    stats["non_object_lines"] += 1
                    continue
                rows.append(payload)
                stats["rows_loaded"] += 1
    return rows, dict(stats)


def _extract_source_details(row: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = [
        _dict_at(row, "decision_artifact", "strategy_trace", "raw_signals", "live", "data").get("source_details"),
        _dict_at(row, "strategy_trace", "raw_signals", "live", "data").get("source_details"),
        _dict_at(row, "decision_artifact", "strategy_trace", "raw_signals", "live", "data").get("sources"),
        _dict_at(row, "strategy_trace", "raw_signals", "live", "data").get("sources"),
        _dict_at(row, "decision_artifact", "strategy_signal", "signal_details", "live", "data").get("source_details"),
        _dict_at(row, "strategy_signal", "signal_details", "live", "data").get("source_details"),
        _dict_at(row, "decision_artifact", "strategy_signal", "signal_details", "live", "data").get("sources"),
        _dict_at(row, "strategy_signal", "signal_details", "live", "data").get("sources"),
        row.get("source_details"),
        _dict_at(row, "weather", "source_details"),
    ]
    weather_snapshot = _extract_weather_snapshot(row)
    candidates.append(weather_snapshot.get("sources"))
    candidates.append(_dict_at(weather_snapshot, "source_signal", "data").get("source_details"))
    candidates.append(_dict_at(weather_snapshot, "source_signal", "data").get("sources"))

    sources: list[dict[str, Any]] = []
    detailed_source_ids: set[str] = set()
    for candidate in candidates:
        if isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, dict):
                    copied = dict(item)
                    sources.append(copied)
                    source_id, source_name = _source_identity(copied)
                    if source_id or source_name:
                        detailed_source_ids.add(source_id or _slug(source_name) or str(source_name))
                elif isinstance(item, str) and item.strip():
                    source_id = _slug(item.strip()) or item.strip()
                    if source_id not in detailed_source_ids:
                        sources.append({"source_name": item.strip()})
        elif isinstance(candidate, dict):
            copied = dict(candidate)
            sources.append(copied)
            source_id, source_name = _source_identity(copied)
            if source_id or source_name:
                detailed_source_ids.add(source_id or _slug(source_name) or str(source_name))
        elif isinstance(candidate, str) and candidate.strip():
            source_id = _slug(candidate.strip()) or candidate.strip()
            if source_id not in detailed_source_ids:
                sources.append({"source_name": candidate.strip()})
    return sources


def _extract_weather_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    direct = _first_dict(row.get("weather_source_snapshot"), _dict_at(row, "weather", "weather_source_snapshot"))
    if direct:
        return direct

    artifact = _dict_at(row, "decision_artifact")
    source_context_data = _dict_at(artifact, "source_context", "data")
    snapshot = _first_dict(source_context_data.get("weather_source_snapshot"))
    if snapshot:
        return snapshot

    snapshots = artifact.get("source_snapshots")
    if isinstance(snapshots, list):
        for source_snapshot in snapshots:
            if not isinstance(source_snapshot, dict):
                continue
            source_name = str(source_snapshot.get("source") or source_snapshot.get("source_name") or "").lower()
            if source_name != "weather":
                continue
            ref_payload = _resolve_snapshot_ref(artifact, _string_or_none(source_snapshot.get("snapshot_ref")))
            if isinstance(ref_payload, dict) and ref_payload:
                return ref_payload
            if any(key in source_snapshot for key in ("forecast", "sources", "source_signal")):
                return source_snapshot
    return {}


def _live_signal_data(row: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        _dict_at(row, "decision_artifact", "strategy_trace", "raw_signals", "live", "data"),
        _dict_at(row, "strategy_trace", "raw_signals", "live", "data"),
        _dict_at(row, "decision_artifact", "strategy_signal", "signal_details", "live", "data"),
        _dict_at(row, "strategy_signal", "signal_details", "live", "data"),
        _dict_at(_extract_weather_snapshot(row), "source_signal", "data"),
    ]
    for candidate in candidates:
        if candidate:
            return candidate
    return {}


def _date_validation_problem(row: dict[str, Any], market: MarketContext) -> str | None:
    weather_snapshot = _extract_weather_snapshot(row)
    live_data = _live_signal_data(row)
    date_validation = _first_dict(
        weather_snapshot.get("date_validation"),
        live_data.get("date_validation"),
        _dict_at(row, "weather", "date_validation"),
        row.get("date_validation"),
    )
    if date_validation and date_validation.get("ok") is False:
        reason = _string_or_none(date_validation.get("reason")) or "explicit_false"
        return f"date_validation_failed:{reason}"

    weather_date = _string_or_none(
        date_validation.get("weather_date")
        or weather_snapshot.get("weather_date")
        or weather_snapshot.get("target_forecast_date")
        or live_data.get("weather_date")
        or live_data.get("target_forecast_date")
    )
    market_date = _string_or_none(date_validation.get("market_date") or market.market_date)
    if market_date and weather_date and market_date != weather_date:
        return "date_validation_failed:market_weather_date_mismatch"
    return None


def _actual_temp_for(row: dict[str, Any], market: MarketContext, actual_lookup: dict[Any, Any]) -> float | None:
    lookup_value = _actual_temp_from_lookup(market, actual_lookup)
    if lookup_value is not None:
        return lookup_value

    weather_snapshot = _extract_weather_snapshot(row)
    live_data = _live_signal_data(row)
    forecast = _dict_at(weather_snapshot, "forecast")
    weather = _dict_at(row, "weather")
    resolution = _dict_at(row, "resolution")
    candidates = [
        row.get("actual_temp_used"),
        row.get("actual_temp"),
        row.get("resolved_temp"),
        row.get("observed_temp"),
        row.get("settlement_temp"),
        row.get("result_temp"),
        weather.get("actual_temp_used"),
        weather.get("actual_temp"),
        weather.get("resolved_temp"),
        forecast.get("actual_temp_used"),
        forecast.get("actual_temp"),
        forecast.get("resolved_temp"),
        live_data.get("actual_temp_used"),
        live_data.get("actual_temp"),
        live_data.get("resolved_temp"),
        resolution.get("actual_temp"),
        resolution.get("resolved_temp"),
        resolution.get("settlement_temp"),
    ]
    if market.market_kind == "high":
        candidates.extend(
            [
                row.get("actual_high"),
                row.get("resolved_high"),
                weather.get("actual_high"),
                resolution.get("actual_high"),
                resolution.get("high"),
            ]
        )
    elif market.market_kind == "low":
        candidates.extend(
            [
                row.get("actual_low"),
                row.get("resolved_low"),
                weather.get("actual_low"),
                resolution.get("actual_low"),
                resolution.get("low"),
            ]
        )
    return _first_float(*candidates)


def _actual_temp_from_lookup(market: MarketContext, actual_lookup: dict[Any, Any]) -> float | None:
    keys = [
        market.market_id,
        (market.market_id, market.market_kind),
        (market.city_id, market.market_date, market.market_kind),
        (market.city_id, market.market_kind, market.market_date),
    ]
    for key in keys:
        if key in actual_lookup:
            return _actual_lookup_value(actual_lookup[key], market.market_kind)
    return None


def _actual_lookup_value(value: Any, market_kind: str) -> float | None:
    direct = _float_or_none(value)
    if direct is not None:
        return direct
    if not isinstance(value, dict):
        return None
    candidates = [value.get("actual_temp"), value.get("resolved_temp"), value.get("settlement_temp")]
    if market_kind == "high":
        candidates.extend([value.get("actual_high"), value.get("high"), value.get("max")])
    elif market_kind == "low":
        candidates.extend([value.get("actual_low"), value.get("low"), value.get("min")])
    return _first_float(*candidates)


def _forecast_temp_for_source(source: dict[str, Any], market_kind: str) -> float | None:
    if market_kind == "high":
        value = _first_float(
            source.get("forecast_high"),
            source.get("high"),
            source.get("temp_high"),
            source.get("maximum_temp"),
            source.get("max_temp"),
        )
        if value is not None:
            return value
    if market_kind == "low":
        value = _first_float(
            source.get("forecast_low"),
            source.get("low"),
            source.get("temp_low"),
            source.get("minimum_temp"),
            source.get("min_temp"),
        )
        if value is not None:
            return value
    return _first_float(
        source.get("predicted_temp"),
        source.get("forecast_temp"),
        source.get("temperature"),
        source.get("temp"),
        source.get("current_forecast"),
        source.get("current_temp"),
        source.get("current"),
    )


def _source_identity(source: dict[str, Any]) -> tuple[str | None, str | None]:
    source_name = _string_or_none(source.get("source_name") or source.get("name") or source.get("source"))
    source_id = _string_or_none(source.get("source_id") or source.get("id") or source.get("provider_id"))
    if not source_id and source_name:
        source_id = _slug(source_name)
    return source_id, source_name


def _infer_market_kind(
    *,
    question: str | None,
    market_id: str | None,
    market_kind: str | None,
    market_type: str | None,
) -> str:
    candidates = " ".join(str(value or "").lower() for value in (market_kind, market_type, question, market_id))
    if re.search(r"\b(high|max|maximum)\b", candidates) or "kxhigh" in candidates:
        return "high"
    if re.search(r"\b(low|min|minimum)\b", candidates) or "kxlow" in candidates:
        return "low"
    return "unknown"


def _infer_contract_shape(
    *,
    question_side: str,
    market_id: str | None,
    question: str | None,
    row: dict[str, Any],
    market: dict[str, Any],
) -> str:
    normalized_side = str(question_side or "").lower()
    text = " ".join(str(value or "").lower() for value in (market_id, question, row.get("market_type"), market.get("market_type")))
    if normalized_side == "range" or "between" in text:
        return "range"
    if normalized_side == "binary_bucket" or "bucket" in text or re.search(r"-b-?\d", text):
        return "bucket"
    if normalized_side in {"above", "below"} or ">" in text or "<" in text:
        return "tail"
    return "unknown"


def _resolve_snapshot_ref(artifact: dict[str, Any], snapshot_ref: str | None) -> Any:
    if not snapshot_ref:
        return None
    value: Any = artifact
    for part in snapshot_ref.split("."):
        if part == "decision_artifact":
            continue
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _dict_at(value: Any, *path: str) -> dict[str, Any]:
    current = value
    for part in path:
        if not isinstance(current, dict):
            return {}
        current = current.get(part)
    return current if isinstance(current, dict) else {}


def _first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _first_float(*values: Any) -> float | None:
    for value in values:
        parsed = _float_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _slug(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or None


def _rate(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return _round_metric(numerator / denominator)


def _round_metric(value: float) -> float:
    return round(value, 6)


def _slice_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["source_id"],
        row["source_name"],
        row["city_id"],
        row["market_kind"],
        row["contract_shape"],
    )


def _leaderboard(
    slices: list[dict[str, Any]],
    group_fields: tuple[str, ...],
    group_kind: str,
) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in slices:
        key = tuple(row.get(field) or "unknown" for field in group_fields)
        group = groups.setdefault(
            key,
            {
                "kind": group_kind,
                "key": {field: key[index] for index, field in enumerate(group_fields)},
                "label": _leaderboard_label(group_fields, key),
                "slice_count": 0,
                "total_observations": 0,
                "sample_count": 0,
                "absolute_error_sum": 0.0,
                "bias_sum": 0.0,
                "threshold_sample_count": 0,
                "threshold_correct_count": 0,
                "within_sums": {bucket: 0.0 for bucket in WITHIN_BUCKETS_F},
                "missing": Counter(),
            },
        )
        sample_count = int(row.get("sample_count") or 0)
        total_observations = int(row.get("total_observations") or 0)
        group["slice_count"] += 1
        group["total_observations"] += total_observations
        group["sample_count"] += sample_count
        if row.get("mae") is not None:
            group["absolute_error_sum"] += float(row["mae"]) * sample_count
        if row.get("mean_bias") is not None:
            group["bias_sum"] += float(row["mean_bias"]) * sample_count
        group["threshold_sample_count"] += int(row.get("threshold_sample_count") or 0)
        group["threshold_correct_count"] += int(row.get("threshold_correct_count") or 0)
        for bucket in WITHIN_BUCKETS_F:
            rate = row.get(f"within_{bucket}f_rate")
            if rate is not None:
                group["within_sums"][bucket] += float(rate) * sample_count
        missing = row.get("missing") or {}
        if isinstance(missing, dict):
            group["missing"].update({str(reason): int(count or 0) for reason, count in missing.items()})

    rows: list[dict[str, Any]] = []
    for group in groups.values():
        sample_count = int(group["sample_count"])
        total_observations = int(group["total_observations"])
        threshold_count = int(group["threshold_sample_count"])
        output = {
            "kind": group["kind"],
            "key": group["key"],
            **group["key"],
            "label": group["label"],
            "slice_count": group["slice_count"],
            "total_observations": total_observations,
            "sample_count": sample_count,
            "coverage_rate": _rate(sample_count, total_observations),
            "mae": _round_metric(group["absolute_error_sum"] / sample_count) if sample_count else None,
            "mean_bias": _round_metric(group["bias_sum"] / sample_count) if sample_count else None,
            "threshold_sample_count": threshold_count,
            "threshold_correct_count": group["threshold_correct_count"],
            "threshold_direction_accuracy": _rate(group["threshold_correct_count"], threshold_count),
            "within_1f_rate": _weighted_rate(group["within_sums"][1], sample_count),
            "within_2f_rate": _weighted_rate(group["within_sums"][2], sample_count),
            "within_3f_rate": _weighted_rate(group["within_sums"][3], sample_count),
            "within_5f_rate": _weighted_rate(group["within_sums"][5], sample_count),
            "missing": dict(sorted(group["missing"].items())),
        }
        rows.append(output)

    ranked = sorted(rows, key=_quality_sort_key, reverse=True)
    for rank, row in enumerate(ranked, start=1):
        row["rank"] = rank
    return ranked


def _leaderboard_label(group_fields: tuple[str, ...], key: tuple[Any, ...]) -> str:
    values = [str(value if value not in (None, "") else "unknown") for value in key]
    if group_fields == ("source_id", "source_name"):
        return values[1] if values[1] != "unknown" else values[0]
    if group_fields == ("city_id", "city_name"):
        return values[1] if values[1] != "unknown" else values[0]
    if group_fields == ("market_kind", "contract_shape"):
        return " / ".join(values)
    return " / ".join(values)


def _weighted_rate(numerator: float, denominator: int) -> float | None:
    if not denominator:
        return None
    return _round_metric(numerator / denominator)


def _quality_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("threshold_direction_accuracy") is not None,
        row.get("threshold_direction_accuracy") or -1,
        row.get("within_3f_rate") or -1,
        -(row.get("mae") if row.get("mae") is not None else 999999),
        row.get("sample_count") or 0,
        row.get("total_observations") or 0,
        str(row.get("label") or row.get("source_name") or row.get("city_id") or ""),
    )


def _risk_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("threshold_direction_accuracy") is None,
        row.get("threshold_direction_accuracy") if row.get("threshold_direction_accuracy") is not None else 2,
        -(row.get("mae") or 0),
        row.get("within_3f_rate") if row.get("within_3f_rate") is not None else 2,
        -(row.get("sample_count") or 0),
    )


def _sum_missing(rows: list[dict[str, Any]], reason: str) -> int:
    return sum(int((row.get("missing") or {}).get(reason) or 0) for row in rows)


def _markdown_cell(value: Any) -> str:
    return str(value if value not in (None, "") else "unknown").replace("|", "/")


def _format_metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)
