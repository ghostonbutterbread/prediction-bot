"""Shadow-only weather source reliability evaluation.

This module consumes frozen source-scoreboard artifacts and evaluates already
recorded candidate/source observations. It is deliberately pure: no network
access, no trades, and no mutation of paper/live state.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any, Iterable, Mapping

from bot.weather.source_scoreboard import SourceForecastObservation, extract_source_forecast_observations
from bot.weather.thresholds import infer_direction_from_value, infer_predicted_outcome


DEFAULT_MIN_SAMPLE_COUNT = 100
DEFAULT_TRUSTED_SAMPLE_COUNT = 200
DEFAULT_MAX_WINDOW = 200
DEFAULT_TRUSTED_DIRECTION_ACCURACY = 0.90
DEFAULT_EXCLUDED_DIRECTION_ACCURACY = 0.45
DEFAULT_DEAD_ZONE_F = 0.5
SOURCE_OUTCOME_LEDGER_SCHEMA_VERSION = 1
SOURCE_EDGE_EVALUATION_SCHEMA_VERSION = 1

TIER_STRONG_TRUSTED = "strong_trusted"
TIER_TRUSTED = "trusted"
TIER_NEUTRAL = "neutral"
TIER_WEAK = "weak"
TIER_EXCLUDED = "excluded"

ACTION_SKIP = "SKIP"
BUY_ACTIONS = {"BUY_YES", "BUY_NO"}
TRUSTED_TIERS = {TIER_TRUSTED, TIER_STRONG_TRUSTED}
WEIGHTS = {
    TIER_STRONG_TRUSTED: 2.0,
    TIER_TRUSTED: 1.0,
    TIER_WEAK: 0.25,
    TIER_NEUTRAL: 0.0,
    TIER_EXCLUDED: 0.0,
}


@dataclass(frozen=True, slots=True)
class SourceReliabilityStats:
    source_id: str
    source_name: str
    city_id: str
    market_kind: str
    contract_shape: str
    sample_count: int
    direction_accuracy: float | None
    tier: str
    mae: float | None = None
    mean_bias: float | None = None
    within_3f_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "city_id": self.city_id,
            "market_kind": self.market_kind,
            "contract_shape": self.contract_shape,
            "sample_count": self.sample_count,
            "direction_accuracy": self.direction_accuracy,
            "tier": self.tier,
            "mae": self.mae,
            "mean_bias": self.mean_bias,
            "within_3f_rate": self.within_3f_rate,
        }


@dataclass(frozen=True, slots=True)
class SourceReliabilityEvaluation:
    recommended_action: str
    effect: str
    reason_code: str
    confidence_multiplier: float
    confidence_delta: float
    trusted_support_count: int
    trusted_dissent_count: int
    excluded_dissent_count: int
    neutral_count: int
    weak_support_count: int
    weak_dissent_count: int
    weighted_support: float
    weighted_dissent: float
    action: str
    observed_source_count: int
    no_reliability_count: int
    tier_counts: dict[str, int]
    source_votes: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommended_action": self.recommended_action,
            "effect": self.effect,
            "reason_code": self.reason_code,
            "confidence_multiplier": self.confidence_multiplier,
            "confidence_delta": self.confidence_delta,
            "trusted_support_count": self.trusted_support_count,
            "trusted_dissent_count": self.trusted_dissent_count,
            "excluded_dissent_count": self.excluded_dissent_count,
            "neutral_count": self.neutral_count,
            "weak_support_count": self.weak_support_count,
            "weak_dissent_count": self.weak_dissent_count,
            "weighted_support": round(self.weighted_support, 6),
            "weighted_dissent": round(self.weighted_dissent, 6),
            "action": self.action,
            "observed_source_count": self.observed_source_count,
            "no_reliability_count": self.no_reliability_count,
            "tier_counts": dict(self.tier_counts),
            "source_votes": list(self.source_votes),
        }


class SourceReliabilityTable:
    def __init__(self, rows: Iterable[Mapping[str, Any]] = ()) -> None:
        self._by_key: dict[tuple[str, str, str, str], SourceReliabilityStats] = {}
        for row in rows:
            stats = stats_from_scoreboard_row(row)
            if stats is None:
                continue
            self._add(stats)

    @classmethod
    def from_path(cls, path: str | Path) -> "SourceReliabilityTable":
        return cls(load_scoreboard_rows(path))

    def lookup(self, observation: SourceForecastObservation) -> SourceReliabilityStats | None:
        city_id = observation.market.city_id or "unknown"
        market_kind = observation.market.market_kind or "unknown"
        contract_shape = observation.market.contract_shape or "unknown"
        for source_key in _source_id_keys(observation.source_id):
            stats = self._by_key.get((source_key, city_id, market_kind, contract_shape))
            if stats is not None:
                return stats
        return None

    def _add(self, stats: SourceReliabilityStats) -> None:
        for source_key in _source_id_keys(stats.source_id):
            self._by_key[(source_key, stats.city_id, stats.market_kind, stats.contract_shape)] = stats


def load_scoreboard_rows(path_value: str | Path) -> list[dict[str, Any]]:
    path = Path(path_value)
    rows: list[dict[str, Any]] = []
    if path.suffix == ".jsonl":
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if isinstance(payload, dict):
                    rows.append(payload)
        return rows

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("slices"), list):
        return [row for row in payload["slices"] if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [payload]
    return rows


def load_source_outcome_ledger_rows(path_value: str | Path) -> list[dict[str, Any]]:
    """Load source-outcome ledger rows from JSONL or JSON list artifacts."""

    path = Path(path_value)
    if path.suffix == ".jsonl":
        rows: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if isinstance(payload, dict):
                    rows.append(payload)
        return rows

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("ledger_rows", "source_outcome_ledger_rows", "rows"):
            rows_value = payload.get(key)
            if isinstance(rows_value, list):
                return [row for row in rows_value if isinstance(row, dict)]
        return [payload]
    return []


def stats_from_scoreboard_row(row: Mapping[str, Any]) -> SourceReliabilityStats | None:
    source_name = _optional_text(row.get("source_name"))
    source_id = _optional_text(row.get("source_id")) or _slug(source_name)
    if not source_id and not source_name:
        return None
    sample_count = _int(row.get("sample_count"))
    accuracy = _number(row.get("threshold_direction_accuracy"), row.get("direction_accuracy"))
    tier = _optional_text(row.get("tier")) or classify_reliability_tier(accuracy, sample_count)
    return SourceReliabilityStats(
        source_id=source_id or "unknown",
        source_name=source_name or source_id or "unknown",
        city_id=_optional_text(row.get("city_id")) or "unknown",
        market_kind=_optional_text(row.get("market_kind")) or "unknown",
        contract_shape=_optional_text(row.get("contract_shape")) or "unknown",
        sample_count=sample_count,
        direction_accuracy=accuracy,
        tier=tier,
        mae=_number(row.get("mae")),
        mean_bias=_number(row.get("mean_bias")),
        within_3f_rate=_number(row.get("within_3f_rate")),
    )


def classify_reliability_tier(
    direction_accuracy: float | None,
    sample_count: int,
    *,
    min_sample_count: int = DEFAULT_MIN_SAMPLE_COUNT,
    trusted_accuracy: float = DEFAULT_TRUSTED_DIRECTION_ACCURACY,
    excluded_accuracy: float = DEFAULT_EXCLUDED_DIRECTION_ACCURACY,
) -> str:
    if direction_accuracy is None or sample_count < min_sample_count:
        return TIER_NEUTRAL
    if direction_accuracy < excluded_accuracy:
        return TIER_EXCLUDED
    if direction_accuracy >= 1.0:
        return TIER_STRONG_TRUSTED
    if direction_accuracy >= trusted_accuracy:
        return TIER_TRUSTED
    return TIER_WEAK


def build_reliability_candidate_row(
    signal: Mapping[str, Any] | None,
    shared_candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a row shape compatible with source-scoreboard extraction."""

    signal = signal if isinstance(signal, Mapping) else {}
    shared_candidate = shared_candidate if isinstance(shared_candidate, Mapping) else {}
    market = shared_candidate.get("market") if isinstance(shared_candidate.get("market"), Mapping) else {}
    evidence = shared_candidate.get("evidence") if isinstance(shared_candidate.get("evidence"), Mapping) else {}
    row = dict(signal)
    row.setdefault("shared_candidate_id", shared_candidate.get("candidate_id"))
    row.setdefault("market_id", shared_candidate.get("market_id") or market.get("id"))
    row.setdefault("question", market.get("question"))
    row.setdefault("market", dict(market))
    if "weather_source_snapshot" in evidence:
        row.setdefault("weather_source_snapshot", evidence.get("weather_source_snapshot"))
    artifact = row.get("decision_artifact") if isinstance(row.get("decision_artifact"), dict) else {}
    if "source_snapshots" in evidence and "source_snapshots" not in artifact:
        artifact = dict(artifact)
        artifact["source_snapshots"] = evidence.get("source_snapshots")
    if "weather_source_snapshot" in evidence:
        artifact = dict(artifact)
        source_context = artifact.get("source_context") if isinstance(artifact.get("source_context"), dict) else {}
        data = source_context.get("data") if isinstance(source_context.get("data"), dict) else {}
        data = dict(data)
        data.setdefault("weather_source_snapshot", evidence.get("weather_source_snapshot"))
        source_context = dict(source_context)
        source_context["data"] = data
        artifact["source_context"] = source_context
    if artifact:
        row["decision_artifact"] = artifact
    return row


def build_source_outcome_ledger_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    actual_lookup: dict[Any, Any] | None = None,
    source_row_path: str | None = None,
) -> list[dict[str, Any]]:
    """Convert raw scored weather rows into chronological source-outcome rows."""

    ledger_rows: list[dict[str, Any]] = []
    for line_number, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            continue
        ledger_rows.extend(
            build_source_outcome_ledger_rows_for_row(
                row,
                actual_lookup=actual_lookup,
                source_row_path=source_row_path,
                source_line_number=line_number,
            )
        )
    return ledger_rows


def build_source_outcome_ledger_rows_for_row(
    row: Mapping[str, Any],
    *,
    actual_lookup: dict[Any, Any] | None = None,
    source_row_path: str | None = None,
    source_line_number: int | None = None,
) -> list[dict[str, Any]]:
    """Build source-outcome ledger rows for a single raw candidate/scoreboard row."""

    raw_row = dict(row)
    observations = extract_source_forecast_observations(raw_row, actual_lookup=actual_lookup)
    return [
        build_source_outcome_ledger_row(
            observation,
            raw_row,
            source_row_path=source_row_path,
            source_line_number=source_line_number,
        )
        for observation in observations
    ]


def build_source_outcome_ledger_row(
    observation: SourceForecastObservation,
    row: Mapping[str, Any] | None = None,
    *,
    source_row_path: str | None = None,
    source_line_number: int | None = None,
) -> dict[str, Any]:
    """Convert one extracted source forecast observation into a ledger row dict."""

    row = row if isinstance(row, Mapping) else {}
    market = observation.market
    forecast = observation.forecast_temp_f
    actual = observation.actual_temp_f
    threshold = market.threshold
    predicted_direction = infer_direction_from_value(forecast, threshold)
    actual_direction = infer_direction_from_value(actual, threshold)
    predicted_outcome = infer_predicted_outcome(market.question_side, predicted_direction)
    actual_outcome = infer_predicted_outcome(market.question_side, actual_direction)
    direction_correct = predicted_outcome == actual_outcome if predicted_outcome and actual_outcome else None
    absolute_error = abs(forecast - actual) if forecast is not None and actual is not None else None
    bias = forecast - actual if forecast is not None and actual is not None else None
    observed_at = _first_timestamp_text(
        row.get("observed_at"),
        row.get("timestamp"),
        row.get("created_at"),
        row.get("source_timestamp"),
        _mapping_at(row, "decision_artifact").get("observed_at"),
        _mapping_at(row, "decision_artifact").get("timestamp"),
        _mapping_at(row, "decision_artifact").get("created_at"),
    )
    resolved_at = _first_timestamp_text(
        _mapping_at(row, "resolution").get("resolved_at"),
        row.get("resolved_at"),
        row.get("settled_at"),
    )
    known_after = _first_timestamp_text(row.get("known_after"), resolved_at)
    exclusion_reasons = list(observation.missing_reasons)
    if forecast is None:
        exclusion_reasons.append("missing_forecast_temp")
    if actual is None:
        exclusion_reasons.append("missing_actual_temp")
    if threshold is None:
        exclusion_reasons.append("missing_threshold")
    if predicted_outcome is None:
        exclusion_reasons.append("missing_predicted_outcome")
    if actual_outcome is None:
        exclusion_reasons.append("missing_actual_outcome")
    if not market.city_id:
        exclusion_reasons.append("missing_city_id")
    if not market.market_date:
        exclusion_reasons.append("missing_market_date")
    if market.market_kind == "unknown":
        exclusion_reasons.append("missing_market_kind")
    if market.contract_shape == "unknown":
        exclusion_reasons.append("missing_contract_shape")
    if known_after is None:
        exclusion_reasons.append("missing_known_after")

    market_id = _optional_text(row.get("market_id")) or market.market_id
    shared_candidate_id = _optional_text(row.get("shared_candidate_id") or row.get("candidate_id"))
    ledger_row = {
        "schema_version": SOURCE_OUTCOME_LEDGER_SCHEMA_VERSION,
        "observation_id": None,
        "source_row_path": _optional_text(row.get("source_row_path")) or source_row_path,
        "source_line_number": _int(row.get("source_line_number")) or source_line_number,
        "market_id": market_id,
        "shared_candidate_id": shared_candidate_id,
        "source_id": observation.source_id,
        "source_name": observation.source_name,
        "city_id": market.city_id or "unknown",
        "market_kind": market.market_kind or "unknown",
        "contract_shape": market.contract_shape or "unknown",
        "observed_at": observed_at,
        "market_date": market.market_date,
        "resolved_at": resolved_at,
        "known_after": known_after,
        "forecast_temp_f": forecast,
        "threshold": threshold,
        "question_side": market.question_side,
        "action": _optional_text(row.get("action") or row.get("direction")),
        "entry_price": _price_value(row, "entry_price"),
        "price": _price_value(row, "price"),
        "market_price": _price_value(row, "market_price"),
        "estimated_fill_price": _price_value(row, "estimated_fill_price"),
        "yes_price": _price_value(row, "yes_price"),
        "no_price": _price_value(row, "no_price"),
        "best_yes_ask": _price_value(row, "best_yes_ask"),
        "best_yes_bid": _price_value(row, "best_yes_bid"),
        "best_no_ask": _price_value(row, "best_no_ask"),
        "best_no_bid": _price_value(row, "best_no_bid"),
        "execution_snapshot_source": _optional_text(row.get("execution_snapshot_source") or _mapping_at(row, "provenance", "future_pnl_inputs").get("execution_snapshot_source")),
        "actual_temp_f": actual,
        "predicted_outcome": predicted_outcome,
        "actual_outcome": actual_outcome,
        "direction_correct": direction_correct,
        "absolute_error_f": _round_metric(absolute_error),
        "bias_f": _round_metric(bias),
        "source_mode": _optional_text(row.get("source_mode")),
        "actual_source": _optional_text(row.get("actual_source") or _mapping_at(row, "resolution").get("actual_source")),
        "date_validation_ok": not any(str(reason).startswith("date_validation_failed") for reason in exclusion_reasons),
        "eligible_for_reliability": not exclusion_reasons,
        "exclusion_reason": ";".join(dict.fromkeys(exclusion_reasons)) if exclusion_reasons else None,
    }
    ledger_row["observation_id"] = _ledger_observation_id(ledger_row)
    return ledger_row




def build_source_edge_evaluation_rows(
    ledger_rows: Iterable[Mapping[str, Any]],
    *,
    outcome_lookup: Mapping[Any, Any] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate source-implied sides against finalized market outcomes and prices.

    This is the scoreboard settlement/edge layer. It consumes source-outcome
    ledger rows produced from known-at-time scoreboard/candidate rows, then joins
    official market outcomes supplied by the caller. It performs no network
    access and does not mutate paper/live accounting.
    """

    lookup = outcome_lookup or {}
    results: list[dict[str, Any]] = []
    for row in ledger_rows:
        if not isinstance(row, Mapping):
            continue
        results.append(build_source_edge_evaluation_row(row, outcome_lookup=lookup))
    return results


def build_source_edge_evaluation_row(
    ledger_row: Mapping[str, Any],
    *,
    outcome_lookup: Mapping[Any, Any] | None = None,
) -> dict[str, Any]:
    """Build one realized-edge row for a source ledger observation."""

    lookup = outcome_lookup or {}
    market_id = _optional_text(ledger_row.get("market_id"))
    official = _market_outcome_for(ledger_row, lookup)
    official_outcome = _normalize_market_outcome(official.get("official_outcome"))
    predicted_outcome = _normalize_market_outcome(ledger_row.get("predicted_outcome"))
    source_side_price = _source_side_price_for(ledger_row, predicted_outcome)
    win = predicted_outcome == official_outcome if predicted_outcome and official_outcome in {"YES", "NO"} else None
    binary_edge = None
    flat_pnl = None
    if win is not None and source_side_price is not None:
        binary_edge = (1.0 if win else 0.0) - source_side_price
        flat_pnl = (1.0 - source_side_price) if win else -source_side_price

    blockers: list[str] = []
    if not market_id:
        blockers.append("missing_market_id")
    if predicted_outcome not in {"YES", "NO"}:
        blockers.append("missing_source_implied_side")
    if official_outcome not in {"YES", "NO"}:
        blockers.append("missing_official_outcome")
    if source_side_price is None:
        blockers.append("missing_source_side_price")

    result = {
        "schema_version": SOURCE_EDGE_EVALUATION_SCHEMA_VERSION,
        "row_type": "source_scoreboard_edge_evaluation",
        "observation_id": _optional_text(ledger_row.get("observation_id")),
        "market_id": market_id,
        "shared_candidate_id": _optional_text(ledger_row.get("shared_candidate_id")),
        "source_id": _optional_text(ledger_row.get("source_id")) or "unknown",
        "source_name": _optional_text(ledger_row.get("source_name")) or _optional_text(ledger_row.get("source_id")) or "unknown",
        "city_id": _optional_text(ledger_row.get("city_id")) or "unknown",
        "market_kind": _optional_text(ledger_row.get("market_kind")) or "unknown",
        "contract_shape": _optional_text(ledger_row.get("contract_shape")) or "unknown",
        "observed_at": _optional_text(ledger_row.get("observed_at")),
        "market_date": _optional_text(ledger_row.get("market_date")),
        "known_after": _optional_text(ledger_row.get("known_after")),
        "forecast_temp_f": _number(ledger_row.get("forecast_temp_f")),
        "threshold": _number(ledger_row.get("threshold")),
        "question_side": _optional_text(ledger_row.get("question_side")),
        "source_implied_side": predicted_outcome,
        "official_outcome": official_outcome,
        "outcome_source": _optional_text(official.get("outcome_source")),
        "outcome_known_at": _optional_text(official.get("outcome_known_at")),
        "label_independence": _optional_text(official.get("label_independence")),
        "source_side_price": _round_metric(source_side_price),
        "market_implied_probability": _round_metric(source_side_price),
        "win": win,
        "binary_edge_realized": _round_metric(binary_edge),
        "flat_1usd_pnl": _round_metric(flat_pnl),
        "eligible_for_edge_validation": not blockers,
        "exclusion_reason": ";".join(dict.fromkeys(blockers)) if blockers else None,
    }
    result["edge_evaluation_id"] = _edge_evaluation_id(result)
    return result


def summarize_source_edge_evaluation_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize realized edge by source/city/kind/shape slices."""

    materialized = [dict(row) for row in rows if isinstance(row, Mapping)]
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    reason_counts: Counter[str] = Counter()
    for row in materialized:
        reason = _optional_text(row.get("exclusion_reason"))
        if reason:
            for part in reason.split(";"):
                if part:
                    reason_counts[part] += 1
        key = (
            _optional_text(row.get("source_id")) or "unknown",
            _optional_text(row.get("city_id")) or "unknown",
            _optional_text(row.get("market_kind")) or "unknown",
            _optional_text(row.get("contract_shape")) or "unknown",
        )
        groups.setdefault(key, []).append(row)

    slices = [_summarize_edge_group(key, group_rows) for key, group_rows in groups.items()]
    slices.sort(key=lambda row: (row.get("eligible_count") or 0, row.get("avg_binary_edge_realized") or -999), reverse=True)
    eligible_count = sum(1 for row in materialized if row.get("eligible_for_edge_validation") is True)
    return {
        "schema_version": SOURCE_EDGE_EVALUATION_SCHEMA_VERSION,
        "row_type": "source_scoreboard_edge_summary",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "input_rows": len(materialized),
            "eligible_rows": eligible_count,
            "blocked_rows": len(materialized) - eligible_count,
            "source_slice_count": len(slices),
            "reason_counts": dict(sorted(reason_counts.items())),
        },
        "slices": slices,
    }

def build_rolling_source_reliability_table(
    ledger_rows: Iterable[Mapping[str, Any]],
    as_of: Any,
    *,
    max_window: int = DEFAULT_MAX_WINDOW,
    min_samples: int = DEFAULT_MIN_SAMPLE_COUNT,
    trusted_samples: int = DEFAULT_TRUSTED_SAMPLE_COUNT,
    trusted_accuracy: float = DEFAULT_TRUSTED_DIRECTION_ACCURACY,
    excluded_accuracy: float = DEFAULT_EXCLUDED_DIRECTION_ACCURACY,
) -> SourceReliabilityTable:
    """Build an as-of source reliability table from eligible resolved ledger rows."""

    return SourceReliabilityTable(
        build_rolling_source_reliability_rows(
            ledger_rows,
            as_of,
            max_window=max_window,
            min_samples=min_samples,
            trusted_samples=trusted_samples,
            trusted_accuracy=trusted_accuracy,
            excluded_accuracy=excluded_accuracy,
        )
    )


def build_rolling_source_reliability_rows(
    ledger_rows: Iterable[Mapping[str, Any]],
    as_of: Any,
    *,
    max_window: int = DEFAULT_MAX_WINDOW,
    min_samples: int = DEFAULT_MIN_SAMPLE_COUNT,
    trusted_samples: int = DEFAULT_TRUSTED_SAMPLE_COUNT,
    trusted_accuracy: float = DEFAULT_TRUSTED_DIRECTION_ACCURACY,
    excluded_accuracy: float = DEFAULT_EXCLUDED_DIRECTION_ACCURACY,
) -> list[dict[str, Any]]:
    """Return scoreboard-compatible reliability rows using only data known before ``as_of``."""

    as_of_dt = _parse_dt(as_of)
    if as_of_dt is None:
        return []

    grouped: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = {}
    for row in ledger_rows:
        if not isinstance(row, Mapping) or row.get("eligible_for_reliability") is not True:
            continue
        known_after = _parse_dt(row.get("known_after"))
        if known_after is None or known_after >= as_of_dt:
            continue
        source_name = _optional_text(row.get("source_name"))
        source_id = _optional_text(row.get("source_id")) or _slug(source_name) or "unknown"
        key = (
            source_id,
            _optional_text(row.get("city_id")) or "unknown",
            _optional_text(row.get("market_kind")) or "unknown",
            _optional_text(row.get("contract_shape")) or "unknown",
        )
        grouped.setdefault(key, []).append(row)

    stats_rows: list[dict[str, Any]] = []
    for key, group_rows in grouped.items():
        source_id, city_id, market_kind, contract_shape = key
        source_name = _canonical_source_name(group_rows, source_id)
        latest_rows = sorted(
            group_rows,
            key=lambda row: (
                _parse_dt(row.get("known_after")) or datetime.min.replace(tzinfo=timezone.utc),
                _optional_text(row.get("observation_id")) or "",
            ),
            reverse=True,
        )[: max(0, max_window)]
        sample_count = len(latest_rows)
        correct_count = sum(1 for row in latest_rows if row.get("direction_correct") is True)
        absolute_errors = [_number(row.get("absolute_error_f")) for row in latest_rows]
        biases = [_number(row.get("bias_f")) for row in latest_rows]
        absolute_errors = [value for value in absolute_errors if value is not None]
        biases = [value for value in biases if value is not None]
        direction_accuracy = correct_count / sample_count if sample_count else None
        within_3_count = sum(
            1
            for row in latest_rows
            if (error := _number(row.get("absolute_error_f"))) is not None and error <= 3.0
        )
        stats_rows.append(
            {
                "source_id": source_id,
                "source_name": source_name,
                "city_id": city_id,
                "market_kind": market_kind,
                "contract_shape": contract_shape,
                "sample_count": sample_count,
                "threshold_sample_count": sample_count,
                "threshold_correct_count": correct_count,
                "threshold_direction_accuracy": _round_metric(direction_accuracy),
                "direction_accuracy": _round_metric(direction_accuracy),
                "tier": classify_rolling_reliability_tier(
                    direction_accuracy,
                    sample_count,
                    min_samples=min_samples,
                    trusted_samples=trusted_samples,
                    trusted_accuracy=trusted_accuracy,
                    excluded_accuracy=excluded_accuracy,
                ),
                "mae": _round_metric(sum(absolute_errors) / len(absolute_errors)) if absolute_errors else None,
                "mean_bias": _round_metric(sum(biases) / len(biases)) if biases else None,
                "within_3f_rate": _round_metric(within_3_count / sample_count) if sample_count else None,
                "as_of": _dt_iso(as_of_dt),
                "window_size": sample_count,
                "max_window": max_window,
            }
        )
    return sorted(stats_rows, key=lambda row: (row["source_id"], row["city_id"], row["market_kind"], row["contract_shape"]))


def _canonical_source_name(rows: Iterable[Mapping[str, Any]], fallback_source_id: str) -> str:
    counts: Counter[str] = Counter()
    for row in rows:
        source_name = _optional_text(row.get("source_name"))
        if source_name:
            counts[source_name] += 1
    if fallback_source_id in counts:
        return fallback_source_id
    if counts:
        return sorted(counts.items(), key=lambda item: (-item[1], item[0].lower(), item[0]))[0][0]
    return fallback_source_id


def classify_rolling_reliability_tier(
    direction_accuracy: float | None,
    sample_count: int,
    *,
    min_samples: int = DEFAULT_MIN_SAMPLE_COUNT,
    trusted_samples: int = DEFAULT_TRUSTED_SAMPLE_COUNT,
    trusted_accuracy: float = DEFAULT_TRUSTED_DIRECTION_ACCURACY,
    excluded_accuracy: float = DEFAULT_EXCLUDED_DIRECTION_ACCURACY,
) -> str:
    if direction_accuracy is None or sample_count < min_samples:
        return TIER_NEUTRAL
    if direction_accuracy < excluded_accuracy:
        return TIER_EXCLUDED
    if sample_count >= trusted_samples and direction_accuracy >= 1.0:
        return TIER_STRONG_TRUSTED
    if direction_accuracy >= trusted_accuracy:
        return TIER_TRUSTED
    return TIER_WEAK


def evaluate_source_reliability_candidate(
    row: Mapping[str, Any],
    table: SourceReliabilityTable,
    *,
    action: str | None = None,
    dead_zone_f: float = DEFAULT_DEAD_ZONE_F,
) -> SourceReliabilityEvaluation:
    candidate_row = dict(row)
    action_label = _normalize_action(
        action
        or candidate_row.get("replayed_action")
        or candidate_row.get("action")
        or candidate_row.get("direction")
        or candidate_row.get("decision_type")
    )
    if action_label not in BUY_ACTIONS:
        return _empty_evaluation(
            action=action_label,
            recommended_action=ACTION_SKIP,
            effect="unchanged",
            reason_code="baseline_not_buy",
        )

    observations = extract_source_forecast_observations(candidate_row)
    votes: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    tier_counts: Counter[str] = Counter()
    weighted_support = 0.0
    weighted_dissent = 0.0

    for observation in observations:
        stats = table.lookup(observation)
        tier = stats.tier if stats is not None else TIER_NEUTRAL
        tier_counts[tier] += 1
        source_vote = _source_vote(observation, action_label, dead_zone_f=dead_zone_f)
        vote_label = source_vote["vote"]
        weight = WEIGHTS[tier]
        if stats is None:
            counts["no_reliability"] += 1
        if vote_label == "support":
            if tier in TRUSTED_TIERS:
                counts["trusted_support"] += 1
                weighted_support += weight
            elif tier == TIER_WEAK:
                counts["weak_support"] += 1
                weighted_support += weight
            elif tier == TIER_NEUTRAL:
                counts["neutral"] += 1
            else:
                counts["excluded_support"] += 1
        elif vote_label == "dissent":
            if tier in TRUSTED_TIERS:
                counts["trusted_dissent"] += 1
                weighted_dissent += weight
            elif tier == TIER_WEAK:
                counts["weak_dissent"] += 1
                weighted_dissent += weight
            elif tier == TIER_EXCLUDED:
                counts["excluded_dissent"] += 1
            else:
                counts["neutral"] += 1
        else:
            counts["neutral"] += 1

        vote_row = {
            **source_vote,
            "source_id": observation.source_id,
            "source_name": observation.source_name,
            "city_id": observation.market.city_id,
            "market_kind": observation.market.market_kind,
            "contract_shape": observation.market.contract_shape,
            "tier": tier,
            "weight": weight,
        }
        if stats is not None:
            vote_row["reliability"] = stats.to_dict()
        votes.append(vote_row)

    reason_code, recommended_action, effect, multiplier, delta = _recommendation(
        action_label,
        counts=counts,
        weighted_support=weighted_support,
        weighted_dissent=weighted_dissent,
        observation_count=len(observations),
    )
    return SourceReliabilityEvaluation(
        recommended_action=recommended_action,
        effect=effect,
        reason_code=reason_code,
        confidence_multiplier=multiplier,
        confidence_delta=delta,
        trusted_support_count=counts["trusted_support"],
        trusted_dissent_count=counts["trusted_dissent"],
        excluded_dissent_count=counts["excluded_dissent"],
        neutral_count=counts["neutral"],
        weak_support_count=counts["weak_support"],
        weak_dissent_count=counts["weak_dissent"],
        weighted_support=weighted_support,
        weighted_dissent=weighted_dissent,
        action=action_label,
        observed_source_count=len(observations),
        no_reliability_count=counts["no_reliability"],
        tier_counts=dict(sorted(tier_counts.items())),
        source_votes=votes,
    )


def apply_source_reliability_confidence(
    confidence: Any,
    evaluation: SourceReliabilityEvaluation,
) -> float | None:
    """Apply the evaluator's confidence effect using the central policy.

    Callers should use this helper instead of reimplementing source-reliability
    confidence math so replay and paper lanes stay adapter-only.
    """

    confidence_value = _number(confidence)
    if confidence_value is None:
        return None
    return max(0.0, min(1.0, confidence_value * evaluation.confidence_multiplier))


def build_source_reliability_shadow_row(
    row: Mapping[str, Any],
    evaluation: SourceReliabilityEvaluation,
    *,
    stable_action: str | None = None,
    replayed_action: str | None = None,
) -> dict[str, Any]:
    market_id = row.get("market_id") or row.get("snapshot_key")
    artifact = row.get("decision_artifact") if isinstance(row.get("decision_artifact"), Mapping) else {}
    return {
        "row_type": "source_reliability_shadow",
        "schema_version": 1,
        "market_id": str(market_id or artifact.get("market_id") or ""),
        "shared_candidate_id": row.get("shared_candidate_id"),
        "stable_action": _normalize_action(stable_action or row.get("action") or row.get("direction")),
        "replayed_action": _normalize_action(replayed_action or row.get("replayed_action") or row.get("action") or row.get("direction")),
        "reliability_recommended_action": evaluation.recommended_action,
        "reliability_effect": evaluation.effect,
        "reason_code": evaluation.reason_code,
        "trusted_support_count": evaluation.trusted_support_count,
        "excluded_dissent_count": evaluation.excluded_dissent_count,
        "weighted_support": round(evaluation.weighted_support, 6),
        "weighted_dissent": round(evaluation.weighted_dissent, 6),
        "tier_counts": dict(evaluation.tier_counts),
    }


def summarize_source_reliability_shadow_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    materialized = [dict(row) for row in rows if isinstance(row, Mapping)]
    return {
        "evaluated_rows": len(materialized),
        "trusted_support_rows": sum(1 for row in materialized if _int(row.get("trusted_support_count")) > 0),
        "skip_recommended_rows": sum(
            1 for row in materialized if _normalize_action(row.get("reliability_recommended_action")) == ACTION_SKIP
        ),
        "unchanged_rows": sum(
            1
            for row in materialized
            if _normalize_action(row.get("replayed_action")) == _normalize_action(row.get("reliability_recommended_action"))
        ),
        "excluded_dissent_rows": sum(1 for row in materialized if _int(row.get("excluded_dissent_count")) > 0),
        "no_reliability_rows": sum(
            1 for row in materialized if _normalize_action(row.get("reason_code")) == "NO_TRUSTED_SUPPORT"
        ),
        "tier_counts": _merge_nested_counts(materialized, "tier_counts"),
        "action_counts": dict(sorted(Counter(_normalize_action(row.get("reliability_recommended_action")) for row in materialized).items())),
        "reason_counts": dict(sorted(Counter(str(row.get("reason_code") or "unknown") for row in materialized).items())),
    }


def _source_vote(
    observation: SourceForecastObservation,
    action: str,
    *,
    dead_zone_f: float,
) -> dict[str, Any]:
    forecast = observation.forecast_temp_f
    threshold = observation.market.threshold
    if forecast is None or threshold is None:
        return {"vote": "neutral", "predicted_outcome": None, "reason": "missing_forecast_or_threshold"}
    if abs(forecast - threshold) <= dead_zone_f:
        return {"vote": "neutral", "predicted_outcome": None, "reason": "dead_zone"}
    predicted_direction = "above" if forecast > threshold else "below"
    predicted_outcome = infer_predicted_outcome(observation.market.question_side, predicted_direction)
    action_side = _side_from_action(action)
    if predicted_outcome is None or action_side is None:
        return {
            "vote": "neutral",
            "predicted_outcome": predicted_outcome,
            "reason": "unknown_question_or_action_side",
        }
    return {
        "vote": "support" if predicted_outcome == action_side else "dissent",
        "predicted_outcome": predicted_outcome,
        "predicted_direction": predicted_direction,
        "forecast_temp_f": forecast,
        "threshold": threshold,
        "reason": "threshold_direction",
    }


def _recommendation(
    action: str,
    *,
    counts: Counter[str],
    weighted_support: float,
    weighted_dissent: float,
    observation_count: int,
) -> tuple[str, str, str, float, float]:
    if observation_count == 0:
        return "no_source_observations", ACTION_SKIP, "skip", 1.0, 0.0
    if counts["trusted_dissent"] > 0 and counts["trusted_support"] == 0:
        return "trusted_dissent", ACTION_SKIP, "skip", 0.75, -0.25
    if counts["trusted_dissent"] > 0 and counts["trusted_support"] > 0:
        return "trusted_conflict", ACTION_SKIP, "skip", 0.85, -0.15
    if counts["trusted_support"] > 0 and weighted_support >= weighted_dissent:
        multiplier = 1.10 if counts["trusted_support"] > 1 or weighted_support >= 2.0 else 1.05
        return "trusted_support", action, "boost", multiplier, multiplier - 1.0
    return "no_trusted_support", ACTION_SKIP, "skip", 0.90, -0.10


def _empty_evaluation(
    *,
    action: str,
    recommended_action: str,
    effect: str,
    reason_code: str,
) -> SourceReliabilityEvaluation:
    return SourceReliabilityEvaluation(
        recommended_action=recommended_action,
        effect=effect,
        reason_code=reason_code,
        confidence_multiplier=1.0,
        confidence_delta=0.0,
        trusted_support_count=0,
        trusted_dissent_count=0,
        excluded_dissent_count=0,
        neutral_count=0,
        weak_support_count=0,
        weak_dissent_count=0,
        weighted_support=0.0,
        weighted_dissent=0.0,
        action=action,
        observed_source_count=0,
        no_reliability_count=0,
        tier_counts={},
        source_votes=[],
    )




def _price_value(row: Mapping[str, Any], key: str) -> float | None:
    return _number(row.get(key), _mapping_at(row, "provenance", "future_pnl_inputs").get(key))


def _market_outcome_for(ledger_row: Mapping[str, Any], lookup: Mapping[Any, Any]) -> dict[str, Any]:
    market_id = _optional_text(ledger_row.get("market_id"))
    candidates = []
    if market_id is not None:
        candidates.extend([lookup.get(market_id), lookup.get(market_id.upper()), lookup.get(market_id.lower())])
    candidates.extend([ledger_row.get("market_outcome"), ledger_row.get("official_outcome")])
    for candidate in candidates:
        normalized = _normalize_outcome_payload(candidate)
        if normalized:
            return normalized
    return {}


def _normalize_outcome_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        outcome = _normalize_market_outcome(
            value.get("official_outcome")
            or value.get("outcome")
            or value.get("result")
            or value.get("settlement_value")
            or value.get("actual_outcome")
        )
        if outcome is None:
            return {}
        return {
            "official_outcome": outcome,
            "outcome_source": _optional_text(value.get("outcome_source") or value.get("source")) or "lookup",
            "outcome_known_at": _optional_text(value.get("outcome_known_at") or value.get("known_at") or value.get("resolved_at") or value.get("settled_at")),
            "label_independence": _optional_text(value.get("label_independence")) or "independent_kalshi_result",
        }
    outcome = _normalize_market_outcome(value)
    if outcome is None:
        return {}
    return {
        "official_outcome": outcome,
        "outcome_source": "lookup",
        "outcome_known_at": None,
        "label_independence": "independent_kalshi_result",
    }


def _source_side_price_for(row: Mapping[str, Any], side: str | None) -> float | None:
    side = _normalize_market_outcome(side)
    if side == "YES":
        side_specific = _number(row.get("source_side_price"), row.get("yes_price"), row.get("yes_ask"), row.get("best_yes_ask"))
        return side_specific if side_specific is not None else _same_side_fill_price(row, side)
    if side == "NO":
        side_specific = _number(row.get("source_side_price"), row.get("no_price"), row.get("no_ask"), row.get("best_no_ask"))
        return side_specific if side_specific is not None else _same_side_fill_price(row, side)
    return None


def _same_side_fill_price(row: Mapping[str, Any], side: str) -> float | None:
    # Side-agnostic fill/entry prices are only valid when the row's action side
    # matches the source-implied side. Otherwise a stable BUY_YES fill could be
    # incorrectly used to price a source-implied BUY_NO counterfactual.
    if _row_action_side(row) != side:
        return None
    return _number(row.get("estimated_fill_price"), row.get("entry_price"), row.get("price"), row.get("market_price"))


def _row_action_side(row: Mapping[str, Any]) -> str | None:
    for key in ("source_side", "direction", "action", "stable_action", "replayed_action"):
        text = _optional_text(row.get(key))
        if not text:
            continue
        normalized = text.strip().upper()
        if normalized in {"YES", "BUY_YES"}:
            return "YES"
        if normalized in {"NO", "BUY_NO"}:
            return "NO"
    return None


def _normalize_market_outcome(value: Any) -> str | None:
    if isinstance(value, bool):
        return "YES" if value else "NO"
    text = _optional_text(value)
    if not text:
        return None
    normalized = text.strip().upper()
    if normalized in {"YES", "Y", "TRUE", "1", "1.0"}:
        return "YES"
    if normalized in {"NO", "N", "FALSE", "0", "0.0"}:
        return "NO"
    return None


def _summarize_edge_group(key: tuple[str, str, str, str], rows: list[dict[str, Any]]) -> dict[str, Any]:
    source_id, city_id, market_kind, contract_shape = key
    eligible = [row for row in rows if row.get("eligible_for_edge_validation") is True]
    wins = sum(1 for row in eligible if row.get("win") is True)
    edge_values = [_number(row.get("binary_edge_realized")) for row in eligible]
    pnl_values = [_number(row.get("flat_1usd_pnl")) for row in eligible]
    prices = [_number(row.get("source_side_price")) for row in eligible]
    edge_values = [value for value in edge_values if value is not None]
    pnl_values = [value for value in pnl_values if value is not None]
    prices = [value for value in prices if value is not None]
    source_name = next((_optional_text(row.get("source_name")) for row in rows if _optional_text(row.get("source_name"))), source_id)
    return {
        "source_id": source_id,
        "source_name": source_name,
        "city_id": city_id,
        "market_kind": market_kind,
        "contract_shape": contract_shape,
        "total_rows": len(rows),
        "eligible_count": len(eligible),
        "blocked_count": len(rows) - len(eligible),
        "wins": wins,
        "losses": len(eligible) - wins,
        "win_rate": _round_metric(wins / len(eligible)) if eligible else None,
        "avg_source_side_price": _round_metric(sum(prices) / len(prices)) if prices else None,
        "avg_binary_edge_realized": _round_metric(sum(edge_values) / len(edge_values)) if edge_values else None,
        "flat_1usd_pnl": _round_metric(sum(pnl_values)) if pnl_values else None,
    }


def _edge_evaluation_id(row: Mapping[str, Any]) -> str:
    payload = {
        key: row.get(key)
        for key in (
            "observation_id",
            "market_id",
            "shared_candidate_id",
            "source_id",
            "source_implied_side",
            "official_outcome",
            "source_side_price",
        )
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha1(raw.encode("utf-8")).hexdigest()

def _source_id_keys(source_id: str | None) -> tuple[str, ...]:
    keys = []
    for value in (source_id, _slug(source_id)):
        text = _optional_text(value)
        if text and text not in keys:
            keys.append(text)
    return tuple(keys)


def _side_from_action(action: str) -> str | None:
    action = _normalize_action(action)
    if action == "BUY_YES":
        return "YES"
    if action == "BUY_NO":
        return "NO"
    return None


def _normalize_action(value: Any) -> str:
    text = str(value or ACTION_SKIP).strip().upper()
    if text in {"BUY_YES", "YES", "BUY"}:
        return "BUY_YES"
    if text in {"BUY_NO", "NO"}:
        return "BUY_NO"
    return ACTION_SKIP if text in {"", "SKIP", "NONE"} else text


def _number(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def _int(value: Any) -> int:
    number = _number(value)
    return int(number) if number is not None else 0


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _slug(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    slug = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return slug or None


def _merge_nested_counts(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        nested = row.get(key)
        if not isinstance(nested, Mapping):
            continue
        for nested_key, value in nested.items():
            counts[str(nested_key)] += _int(value)
    return dict(sorted(counts.items()))


def _mapping_at(value: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    current: Any = value
    for part in path:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(part)
    return current if isinstance(current, Mapping) else {}


def _first_timestamp_text(*values: Any) -> str | None:
    for value in values:
        parsed = _parse_dt(value)
        if parsed is not None:
            return _dt_iso(parsed)
    return None


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _optional_text(value)
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _dt_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _round_metric(value: float | None) -> float | None:
    return round(value, 6) if value is not None and math.isfinite(value) else None


def _ledger_observation_id(row: Mapping[str, Any]) -> str:
    payload = {
        key: row.get(key)
        for key in (
            "source_row_path",
            "source_line_number",
            "market_id",
            "shared_candidate_id",
            "source_id",
            "city_id",
            "market_kind",
            "contract_shape",
            "observed_at",
            "market_date",
            "resolved_at",
            "forecast_temp_f",
            "actual_temp_f",
            "threshold",
        )
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha1(raw.encode("utf-8")).hexdigest()


__all__ = [
    "SOURCE_OUTCOME_LEDGER_SCHEMA_VERSION",
    "SOURCE_EDGE_EVALUATION_SCHEMA_VERSION",
    "SourceReliabilityEvaluation",
    "SourceReliabilityStats",
    "SourceReliabilityTable",
    "build_reliability_candidate_row",
    "build_rolling_source_reliability_rows",
    "build_rolling_source_reliability_table",
    "build_source_outcome_ledger_row",
    "build_source_edge_evaluation_row",
    "build_source_edge_evaluation_rows",
    "build_source_outcome_ledger_rows",
    "build_source_outcome_ledger_rows_for_row",
    "build_source_reliability_shadow_row",
    "classify_reliability_tier",
    "classify_rolling_reliability_tier",
    "evaluate_source_reliability_candidate",
    "apply_source_reliability_confidence",
    "load_source_outcome_ledger_rows",
    "load_scoreboard_rows",
    "stats_from_scoreboard_row",
    "summarize_source_reliability_shadow_rows",
    "summarize_source_edge_evaluation_rows",
]
