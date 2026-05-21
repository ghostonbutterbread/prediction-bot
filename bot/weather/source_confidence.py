"""Pure helpers for first-pass weather source confidence rows."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from bot.weather.source_reliability import (
    SourceReliabilityTable,
    TIER_NEUTRAL,
    TIER_STRONG_TRUSTED,
    TIER_TRUSTED,
    TIER_WEAK,
)
from bot.weather.thresholds import infer_predicted_outcome, infer_question_side


SCHEMA_NAME = "weather_source_confidence_v1"
ENGINE_VERSION = "source_confidence_v1"
NORMALIZER_VERSION = "weather_source_observation_v1"
CONFIDENCE_TYPE = "rank_score_not_calibrated_probability"

GRADE_STRONG_YES = "STRONG_YES"
GRADE_WEAK_YES = "WEAK_YES"
GRADE_DISAGREE = "DISAGREE"
GRADE_WEAK_NO = "WEAK_NO"
GRADE_STRONG_NO = "STRONG_NO"
GRADE_NO_SOURCE_DATA = "NO_SOURCE_DATA"
GRADE_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"

ACTION_ALLOW = "ALLOW"
ACTION_BLOCK = "BLOCK"
DIRECTION_YES = "YES"
DIRECTION_NO = "NO"
DIRECTION_DISAGREE = "DISAGREE"
DIRECTION_UNKNOWN = "UNKNOWN"

UNKNOWN_VALUE = "unknown"
USABLE_TIERS = {TIER_STRONG_TRUSTED, TIER_TRUSTED, TIER_WEAK}
TIER_WEIGHTS = {
    TIER_STRONG_TRUSTED: 2.0,
    TIER_TRUSTED: 1.0,
    TIER_WEAK: 0.25,
}
BACKOFF_KEYS = [
    ("source_id+city_id+market_kind+contract_shape", False, False, False),
    ("source_id+city_id+market_kind+unknown", False, False, True),
    ("source_id+city_id+unknown+unknown", False, True, True),
    ("source_id+unknown+market_kind+contract_shape", True, False, False),
    ("source_id+unknown+unknown+unknown", True, True, True),
]


@dataclass(frozen=True, slots=True)
class _LookupMarket:
    city_id: str
    market_kind: str
    contract_shape: str


@dataclass(frozen=True, slots=True)
class _LookupObservation:
    source_id: str
    source_name: str
    market: _LookupMarket


def normalize_source_observations(row: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Extract normalized source observations from known row shapes."""

    if not isinstance(row, Mapping):
        return []

    market_kind = _market_kind(row)
    forecast_target = _forecast_target(row)
    market_date = _first_text(
        row.get("market_date"),
        _nested(row, "shared_candidate", "market_date"),
        _nested(row, "shared_candidate", "market", "market_date"),
        _nested(row, "shared_candidate", "market", "event_date"),
        _nested(row, "market", "market_date"),
        _nested(row, "market", "event_date"),
        _nested(row, "decision_artifact", "strategy_signal", "data", "market_date"),
        _nested(row, "data", "market_date"),
    )
    row_observed_at = _first_text(
        row.get("observed_at"),
        _nested(row, "shared_candidate", "observed_at"),
        _nested(row, "decision_artifact", "observed_at"),
        _nested(row, "decision_artifact", "strategy_signal", "observed_at"),
        _nested(row, "decision_artifact", "strategy_signal", "data", "observed_at"),
        _nested(row, "data", "observed_at"),
    )

    candidates = [
        (
            "shared_candidate.evidence.weather_risk.data.source_details",
            _nested(row, "shared_candidate", "evidence", "weather_risk", "data", "source_details"),
        ),
        ("evidence.weather_risk.data.source_details", _nested(row, "evidence", "weather_risk", "data", "source_details")),
        (
            "decision_artifact.strategy_signal.data.source_details",
            _nested(row, "decision_artifact", "strategy_signal", "data", "source_details"),
        ),
        (
            "decision_artifact.strategy_trace.raw_signals.live.data.sources",
            _nested(row, "decision_artifact", "strategy_trace", "raw_signals", "live", "data", "sources"),
        ),
        (
            "strategy_trace.raw_signals.live.data.source_details",
            _nested(row, "strategy_trace", "raw_signals", "live", "data", "source_details"),
        ),
        (
            "strategy_trace.raw_signals.live.data.sources",
            _nested(row, "strategy_trace", "raw_signals", "live", "data", "sources"),
        ),
        (
            "decision_artifact.strategy_signal.signal_details.live.data.source_details",
            _nested(
                row,
                "decision_artifact",
                "strategy_signal",
                "signal_details",
                "live",
                "data",
                "source_details",
            ),
        ),
        (
            "decision_artifact.strategy_trace.raw_signals.live.data.source_details",
            _nested(
                row,
                "decision_artifact",
                "strategy_trace",
                "raw_signals",
                "live",
                "data",
                "source_details",
            ),
        ),
        (
            "decision_artifact.strategy_signal.signal_details.live.data.sources",
            _nested(
                row,
                "decision_artifact",
                "strategy_signal",
                "signal_details",
                "live",
                "data",
                "sources",
            ),
        ),
        (
            "strategy_signal.signal_details.live.data.source_details",
            _nested(row, "strategy_signal", "signal_details", "live", "data", "source_details"),
        ),
        (
            "strategy_signal.signal_details.live.data.sources",
            _nested(row, "strategy_signal", "signal_details", "live", "data", "sources"),
        ),
        (
            "decision_artifact.strategy_trace.accepted_signals.live.data.source_details",
            _nested(
                row,
                "decision_artifact",
                "strategy_trace",
                "accepted_signals",
                "live",
                "data",
                "source_details",
            ),
        ),
        (
            "decision_artifact.source_context.data.weather_source_snapshot.sources",
            _nested(row, "decision_artifact", "source_context", "data", "weather_source_snapshot", "sources"),
        ),
        (
            "decision_artifact.source_context.data.weather_source_snapshot.source_signal.data.source_details",
            _nested(
                row,
                "decision_artifact",
                "source_context",
                "data",
                "weather_source_snapshot",
                "source_signal",
                "data",
                "source_details",
            ),
        ),
        (
            "decision_artifact.source_context.data.weather_source_snapshot.source_signal.data.sources",
            _nested(
                row,
                "decision_artifact",
                "source_context",
                "data",
                "weather_source_snapshot",
                "source_signal",
                "data",
                "sources",
            ),
        ),
        (
            "decision_artifact.trade_context.source_context.signal_details.live.data.source_details",
            _nested(
                row,
                "decision_artifact",
                "trade_context",
                "source_context",
                "signal_details",
                "live",
                "data",
                "source_details",
            ),
        ),
        ("weather.source_details", _nested(row, "weather", "source_details")),
        ("weather.weather_source_snapshot.sources", _nested(row, "weather", "weather_source_snapshot", "sources")),
        (
            "weather.weather_source_snapshot.source_signal.data.source_details",
            _nested(row, "weather", "weather_source_snapshot", "source_signal", "data", "source_details"),
        ),
        (
            "weather.weather_source_snapshot.source_signal.data.sources",
            _nested(row, "weather", "weather_source_snapshot", "source_signal", "data", "sources"),
        ),
        ("data.source_details", _nested(row, "data", "source_details")),
        ("data.sources", _nested(row, "data", "sources")),
        ("source_details", row.get("source_details")),
        ("sources", row.get("sources")),
    ]

    observations: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for provenance, payload in candidates:
        for source in _iter_source_detail_items(payload):
            normalized = _normalize_observation(
                source,
                provenance=provenance,
                market_kind=market_kind,
                forecast_target=forecast_target,
                market_date=market_date,
                row_observed_at=row_observed_at,
            )
            if not normalized:
                continue
            key = (
                normalized.get("source_id"),
                normalized.get("source_name"),
                normalized.get("source_family"),
                normalized.get("forecast_temp_f"),
                normalized.get("fetched_at"),
            )
            if key in seen:
                continue
            seen.add(key)
            observations.append(normalized)
    return observations


def build_source_confidence_row(
    row: Mapping[str, Any] | None,
    reliability_table: Any = None,
) -> dict[str, Any]:
    """Build one first-pass source confidence row for a candidate."""

    row = row if isinstance(row, Mapping) else {}
    observations = normalize_source_observations(row)
    threshold = _first_number(
        row.get("threshold"),
        _nested(row, "shared_candidate", "threshold"),
        _nested(row, "shared_candidate", "market", "threshold"),
        _nested(row, "decision_artifact", "strategy_signal", "data", "threshold"),
        _weather_data_value(row, "threshold"),
        _nested(row, "data", "threshold"),
    )
    inferred_low, inferred_high = _threshold_range_from_question(_market_question(row))
    threshold_low = _first_number(
        row.get("threshold_low"),
        _nested(row, "shared_candidate", "threshold_low"),
        _nested(row, "shared_candidate", "market", "threshold_low"),
        _nested(row, "decision_artifact", "strategy_signal", "data", "threshold_low"),
        _weather_data_value(row, "threshold_low"),
        _nested(row, "data", "threshold_low"),
        inferred_low,
    )
    threshold_high = _first_number(
        row.get("threshold_high"),
        _nested(row, "shared_candidate", "threshold_high"),
        _nested(row, "shared_candidate", "market", "threshold_high"),
        _nested(row, "decision_artifact", "strategy_signal", "data", "threshold_high"),
        _weather_data_value(row, "threshold_high"),
        _nested(row, "data", "threshold_high"),
        inferred_high,
    )
    question_side = _question_side(row)
    predicted_outcome = _predicted_outcome(row)
    source_grade = GRADE_NO_SOURCE_DATA if not observations else GRADE_INSUFFICIENT_HISTORY
    reason_code = "no_source_data" if not observations else "no_usable_reliability_after_backoff"

    result = {
        "schema": SCHEMA_NAME,
        "shared_candidate_id": _shared_candidate_id(row),
        "market_id": _market_id(row),
        "observed_at": _observed_at(row),
        "engine_version": ENGINE_VERSION,
        "predicted_outcome": predicted_outcome,
        "candidate_side_mapping": _candidate_side_mapping(row),
        "source_direction": DIRECTION_UNKNOWN,
        "source_grade": source_grade,
        "source_confidence_score": 0.0,
        "confidence_type": CONFIDENCE_TYPE,
        "recommended_action": ACTION_BLOCK,
        "reason_code": reason_code,
        "weighted_support": 0.0,
        "weighted_dissent": 0.0,
        "agreement_state": reason_code,
        "city_id": _city_id(row),
        "market_date": _market_date(row),
        "market_kind": _market_kind(row),
        "contract_shape": _contract_shape(row),
        "question_side": question_side,
        "normalized_predicate": _first_text(
            row.get("normalized_predicate"),
            _nested(row, "shared_candidate", "normalized_predicate"),
            _nested(row, "decision_artifact", "strategy_signal", "data", "normalized_predicate"),
            _nested(row, "data", "normalized_predicate"),
        ),
        "threshold": threshold,
        "threshold_low": threshold_low,
        "threshold_high": threshold_high,
        "forecast_target": _forecast_target(row),
        "forecast_horizon_hours": _first_number(
            row.get("forecast_horizon_hours"),
            _nested(row, "shared_candidate", "forecast_horizon_hours"),
            _nested(row, "decision_artifact", "strategy_signal", "data", "forecast_horizon_hours"),
            _nested(row, "data", "forecast_horizon_hours"),
        ),
        "sources_used": [],
        "sources_excluded": [],
        "source_observations": observations,
        "data_quality": _build_data_quality(observations),
    }
    if observations:
        scoring = _score_source_observations(
            observations,
            threshold=threshold,
            threshold_low=threshold_low,
            threshold_high=threshold_high,
            question_side=question_side,
            candidate_outcome=predicted_outcome,
            city_id=result["city_id"],
            market_kind=result["market_kind"],
            contract_shape=result["contract_shape"],
            reliability_table=reliability_table,
        )
        result.update(
            {
                "source_direction": scoring["source_direction"],
                "source_grade": scoring["source_grade"],
                "source_confidence_score": scoring["source_confidence_score"],
                "recommended_action": scoring["recommended_action"],
                "reason_code": scoring["reason_code"],
                "weighted_support": scoring["weighted_support"],
                "weighted_dissent": scoring["weighted_dissent"],
                "agreement_state": scoring["agreement_state"],
                "sources_used": scoring["sources_used"],
                "sources_excluded": scoring["sources_excluded"],
                "predicted_outcome": scoring["candidate_outcome"],
            }
        )
        result["data_quality"] = _build_data_quality(
            observations,
            sources_used=result["sources_used"],
            sources_excluded=result["sources_excluded"],
        )
    result["engine_inputs_hash"] = _engine_inputs_hash(result)
    return result


def summarize_source_confidence_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize first-pass source confidence rows for CLI/reporting."""

    normalized_rows = [dict(row) for row in rows if isinstance(row, Mapping)]
    grade_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    agreement_state_counts: Counter[str] = Counter()
    source_direction_counts: Counter[str] = Counter()
    confidence_type_counts: Counter[str] = Counter()
    per_source_counts: Counter[str] = Counter()
    per_source_used_counts: Counter[str] = Counter()
    source_exclusion_reason_counts: Counter[str] = Counter()
    source_used_vote_counts: Counter[str] = Counter()
    source_used_tier_counts: Counter[str] = Counter()
    source_excluded_tier_counts: Counter[str] = Counter()
    source_observation_rows: list[dict[str, Any]] = []
    confidence_scores: list[float] = []
    sources_used_count = 0
    sources_excluded_count = 0

    for row in normalized_rows:
        grade = _text(row.get("source_grade")) or "UNKNOWN"
        grade_counts[grade] += 1
        reason_counts[_text(row.get("reason_code")) or "unknown"] += 1
        action_counts[_text(row.get("recommended_action")) or "UNKNOWN"] += 1
        agreement_state_counts[_text(row.get("agreement_state")) or "unknown"] += 1
        source_direction_counts[_text(row.get("source_direction")) or DIRECTION_UNKNOWN] += 1
        confidence_type_counts[_text(row.get("confidence_type")) or "unknown"] += 1
        score = _number(row.get("source_confidence_score"))
        if score is not None:
            confidence_scores.append(score)
        for observation in _list_of_dicts(row.get("source_observations")):
            observation_row = dict(observation)
            observation_row.setdefault("shared_candidate_id", row.get("shared_candidate_id"))
            observation_row.setdefault("market_id", row.get("market_id"))
            observation_row.setdefault("source_grade", grade)
            source_observation_rows.append(observation_row)
            source_key = _text(observation_row.get("source_id")) or _text(observation_row.get("source_name")) or "unknown"
            per_source_counts[source_key] += 1
        for used in _list_of_dicts(row.get("sources_used")):
            sources_used_count += 1
            source_key = _text(used.get("source_id")) or _text(used.get("source_name")) or "unknown"
            per_source_used_counts[source_key] += 1
            source_used_vote_counts[_text(used.get("vote")) or "unknown"] += 1
            source_used_tier_counts[_text(used.get("tier")) or "unknown"] += 1
        for excluded in _list_of_dicts(row.get("sources_excluded")):
            sources_excluded_count += 1
            source_exclusion_reason_counts[_text(excluded.get("reason_code")) or "unknown"] += 1
            source_excluded_tier_counts[_text(excluded.get("tier")) or "unknown"] += 1

    score_range = {
        "min": min(confidence_scores) if confidence_scores else None,
        "max": max(confidence_scores) if confidence_scores else None,
    }
    return {
        "row_count": len(normalized_rows),
        "grade_counts": dict(sorted(grade_counts.items())),
        "reason_counts": dict(sorted(reason_counts.items())),
        "recommended_action_counts": dict(sorted(action_counts.items())),
        "agreement_state_counts": dict(sorted(agreement_state_counts.items())),
        "source_direction_counts": dict(sorted(source_direction_counts.items())),
        "confidence_type_counts": dict(sorted(confidence_type_counts.items())),
        "confidence_score_range": score_range,
        "rows": normalized_rows,
        "source_observation_rows": source_observation_rows,
        "sources_used_count": sources_used_count,
        "sources_excluded_count": sources_excluded_count,
        "no_source_data": int(grade_counts.get(GRADE_NO_SOURCE_DATA, 0)),
        "insufficient_history": int(grade_counts.get(GRADE_INSUFFICIENT_HISTORY, 0)),
        "per_source_counts": dict(sorted(per_source_counts.items())),
        "per_source_used_counts": dict(sorted(per_source_used_counts.items())),
        "source_exclusion_reason_counts": dict(sorted(source_exclusion_reason_counts.items())),
        "source_used_vote_counts": dict(sorted(source_used_vote_counts.items())),
        "source_used_tier_counts": dict(sorted(source_used_tier_counts.items())),
        "source_excluded_tier_counts": dict(sorted(source_excluded_tier_counts.items())),
    }


def _build_data_quality(
    observations: list[dict[str, Any]],
    *,
    sources_used: list[dict[str, Any]] | None = None,
    sources_excluded: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    sources_used = sources_used or []
    sources_excluded = sources_excluded or []
    usable_forecast_count = sum(1 for observation in observations if observation.get("forecast_temp_f") is not None)
    known_flags = [observation.get("known_at_time_assertion") for observation in observations]
    known_at_time = bool(known_flags) and all(flag is True for flag in known_flags)
    sample_counts = [_first_number(source.get("sample_count")) for source in sources_used]
    return {
        "source_observation_count": len(observations),
        "usable_forecast_count": usable_forecast_count,
        "min_sample_count_met": any((sample_count or 0.0) >= 100.0 for sample_count in sample_counts),
        "known_at_time": known_at_time,
        "label_independence_status": "not_applicable_live_candidate",
        "reliability_history_available": bool(sources_used)
        or any(source.get("reason_code") == "non_usable_reliability_tier" for source in sources_excluded),
    }


def _score_source_observations(
    observations: list[dict[str, Any]],
    *,
    threshold: float | None,
    threshold_low: float | None = None,
    threshold_high: float | None = None,
    question_side: str | None = None,
    candidate_outcome: str | None,
    city_id: str | None,
    market_kind: str | None,
    contract_shape: str | None,
    reliability_table: Any,
) -> dict[str, Any]:
    lookup = _coerce_reliability_lookup(reliability_table)
    normalized_candidate_outcome = _normalize_candidate_outcome(candidate_outcome, question_side=question_side)
    usable_rows: list[dict[str, Any]] = []
    excluded_rows: list[dict[str, Any]] = []

    for observation in observations:
        used_row, excluded_row = _score_single_observation(
            observation,
            threshold=threshold,
            threshold_low=threshold_low,
            threshold_high=threshold_high,
            question_side=question_side,
            candidate_outcome=normalized_candidate_outcome,
            city_id=city_id,
            market_kind=market_kind,
            contract_shape=contract_shape,
            lookup=lookup,
        )
        if used_row is not None:
            usable_rows.append(used_row)
        elif excluded_row is not None:
            excluded_rows.append(excluded_row)

    if not usable_rows:
        return {
            "candidate_outcome": normalized_candidate_outcome,
            "source_direction": DIRECTION_UNKNOWN,
            "source_grade": GRADE_INSUFFICIENT_HISTORY,
            "source_confidence_score": 0.0,
            "recommended_action": ACTION_BLOCK,
            "reason_code": "no_usable_reliability_after_backoff",
            "weighted_support": 0.0,
            "weighted_dissent": 0.0,
            "agreement_state": "no_usable_reliability_after_backoff",
            "sources_used": [],
            "sources_excluded": excluded_rows,
        }

    usable_rows, family_exclusions = _collapse_source_family_votes(usable_rows)
    excluded_rows.extend(family_exclusions)
    if not usable_rows:
        return {
            "candidate_outcome": normalized_candidate_outcome,
            "source_direction": DIRECTION_UNKNOWN,
            "source_grade": GRADE_INSUFFICIENT_HISTORY,
            "source_confidence_score": 0.0,
            "recommended_action": ACTION_BLOCK,
            "reason_code": "no_usable_reliability_after_family_dedupe",
            "weighted_support": 0.0,
            "weighted_dissent": 0.0,
            "agreement_state": "no_usable_reliability_after_family_dedupe",
            "sources_used": [],
            "sources_excluded": excluded_rows,
        }

    weighted_support = round(sum(row["weight"] for row in usable_rows if row["vote"] == "support"), 6)
    weighted_dissent = round(sum(row["weight"] for row in usable_rows if row["vote"] == "dissent"), 6)
    support_rows = [row for row in usable_rows if row["vote"] == "support"]
    dissent_rows = [row for row in usable_rows if row["vote"] == "dissent"]
    trusted_support = any(row["tier"] in {TIER_STRONG_TRUSTED, TIER_TRUSTED} for row in support_rows)
    trusted_dissent = any(row["tier"] in {TIER_STRONG_TRUSTED, TIER_TRUSTED} for row in dissent_rows)
    trusted_any = any(row["tier"] in {TIER_STRONG_TRUSTED, TIER_TRUSTED} for row in usable_rows)
    source_outcomes = {_first_text(row.get("predicted_outcome")) for row in usable_rows}
    source_outcomes.discard(None)

    if len(source_outcomes) != 1 or (support_rows and dissent_rows):
        source_direction = DIRECTION_DISAGREE
        source_grade = GRADE_DISAGREE
        recommended_action = ACTION_BLOCK
        reason_code = "mixed_source_votes"
        agreement_state = "mixed_support_and_dissent"
    else:
        source_direction = next(iter(source_outcomes))
        source_grade = _grade_for_source_outcome(source_direction, trusted=trusted_any)
        if support_rows:
            recommended_action = ACTION_ALLOW
            reason_code = "trusted_support" if trusted_support else "weak_support"
            agreement_state = "unanimous_support"
        else:
            recommended_action = ACTION_BLOCK
            reason_code = "trusted_dissent" if trusted_dissent else "weak_dissent"
            agreement_state = "unanimous_dissent"

    return {
        "candidate_outcome": normalized_candidate_outcome,
        "source_direction": source_direction,
        "source_grade": source_grade,
        "source_confidence_score": round(weighted_support - weighted_dissent, 6),
        "recommended_action": recommended_action,
        "reason_code": reason_code,
        "weighted_support": weighted_support,
        "weighted_dissent": weighted_dissent,
        "agreement_state": agreement_state,
        "sources_used": usable_rows,
        "sources_excluded": excluded_rows,
    }


def _grade_for_source_outcome(source_outcome: str | None, *, trusted: bool) -> str:
    if source_outcome == DIRECTION_YES:
        return GRADE_STRONG_YES if trusted else GRADE_WEAK_YES
    if source_outcome == DIRECTION_NO:
        return GRADE_STRONG_NO if trusted else GRADE_WEAK_NO
    return GRADE_DISAGREE


def _score_single_observation(
    observation: Mapping[str, Any],
    *,
    threshold: float | None,
    threshold_low: float | None,
    threshold_high: float | None,
    question_side: str | None,
    candidate_outcome: str | None,
    city_id: str | None,
    market_kind: str | None,
    contract_shape: str | None,
    lookup: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    source_id = _first_text(observation.get("source_id")) or UNKNOWN_VALUE
    source_name = _first_text(observation.get("source_name"), source_id) or UNKNOWN_VALUE
    forecast_temp = _first_number(observation.get("forecast_temp_f"))
    attempted_paths, matched_path, stats = _lookup_reliability_stats(
        lookup,
        source_id=source_id,
        source_name=source_name,
        city_id=city_id,
        market_kind=market_kind,
        contract_shape=contract_shape,
    )
    base_row = {
        "source_id": source_id,
        "source_name": source_name,
        "source_family": _first_text(observation.get("source_family")),
        "forecast_temp_f": forecast_temp,
        "threshold": threshold,
        "threshold_low": threshold_low,
        "threshold_high": threshold_high,
        "question_side": question_side,
        "candidate_outcome": candidate_outcome,
        "backoff_path": " -> ".join(attempted_paths),
    }
    if forecast_temp is None:
        return None, {**base_row, "reason_code": "missing_forecast_temp"}
    normalized_question_side = (question_side or "").strip().lower()
    if normalized_question_side in {"range", "binary_bucket"}:
        if threshold_low is None or threshold_high is None:
            return None, {**base_row, "reason_code": "missing_bucket_threshold_range"}
    elif threshold is None:
        return None, {**base_row, "reason_code": "missing_threshold"}
    if question_side is None:
        return None, {**base_row, "reason_code": "missing_question_side"}
    if candidate_outcome not in {"YES", "NO"}:
        return None, {**base_row, "reason_code": "missing_candidate_outcome"}
    if stats is None:
        return None, {**base_row, "reason_code": "no_reliability_after_backoff"}

    tier = _optional_text(getattr(stats, "tier", None)) or TIER_NEUTRAL
    if tier not in USABLE_TIERS:
        return None, {
            **base_row,
            "tier": tier,
            "sample_count": _int(getattr(stats, "sample_count", None)),
            "threshold_direction_accuracy": _first_number(
                getattr(stats, "direction_accuracy", None),
                getattr(stats, "threshold_direction_accuracy", None),
            ),
            "mae_f": _number(getattr(stats, "mae", None)),
            "bias_f": _number(getattr(stats, "mean_bias", None)),
            "specificity": matched_path,
            "reason_code": "non_usable_reliability_tier",
        }

    predicted_direction = _source_direction_from_forecast(forecast_temp, threshold)
    predicted_outcome = _source_outcome_from_forecast(
        forecast_temp,
        threshold=threshold,
        threshold_low=threshold_low,
        threshold_high=threshold_high,
        question_side=question_side,
    )
    if predicted_outcome not in {"YES", "NO"}:
        return None, {**base_row, "reason_code": "source_neutral_at_threshold"}
    vote = "support" if predicted_outcome == candidate_outcome else "dissent"
    return (
        {
            **base_row,
            "vote": vote,
            "predicted_outcome": predicted_outcome,
            "tier": tier,
            "base_weight": TIER_WEIGHTS[tier],
            "weight": TIER_WEIGHTS[tier],
            "specificity": matched_path,
            "sample_count": _int(getattr(stats, "sample_count", None)),
            "threshold_direction_accuracy": _first_number(
                getattr(stats, "direction_accuracy", None),
                getattr(stats, "threshold_direction_accuracy", None),
            ),
            "mae_f": _number(getattr(stats, "mae", None)),
            "bias_f": _number(getattr(stats, "mean_bias", None)),
            "dedupe_group_weight": 1.0,
        },
        None,
    )


def _collapse_source_family_votes(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        group_key = _source_family_group_key(row)
        grouped.setdefault(group_key, []).append(row)

    collapsed: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for group_key, group_rows in grouped.items():
        votes = {str(row.get("vote") or "") for row in group_rows}
        if "support" in votes and "dissent" in votes:
            excluded.append(
                {
                    "source_family": group_key,
                    "source_ids": sorted({_first_text(row.get("source_id")) or UNKNOWN_VALUE for row in group_rows}),
                    "votes": sorted(votes),
                    "reason_code": "source_family_self_conflict",
                    "dedupe_group_weight": 0.0,
                    "member_count": len(group_rows),
                }
            )
            continue

        representative = max(
            group_rows,
            key=lambda row: (
                _first_number(row.get("base_weight")) or 0.0,
                _first_number(row.get("sample_count")) or 0.0,
                _first_number(row.get("threshold_direction_accuracy")) or 0.0,
            ),
        )
        representative = dict(representative)
        representative["dedupe_group_weight"] = 1.0
        representative["duplicate_source_count"] = len(group_rows)
        representative["weight"] = _first_number(representative.get("base_weight")) or 0.0
        collapsed.append(representative)
    return collapsed, excluded


def _source_family_group_key(row: Mapping[str, Any]) -> str:
    return _first_text(row.get("source_family"), row.get("source_id")) or UNKNOWN_VALUE


def _coerce_reliability_lookup(reliability_table: Any) -> Any:
    if reliability_table is None:
        return None
    if isinstance(reliability_table, SourceReliabilityTable):
        return reliability_table
    if isinstance(reliability_table, list):
        return SourceReliabilityTable(reliability_table)
    if hasattr(reliability_table, "lookup"):
        return reliability_table
    return None


def _lookup_reliability_stats(
    lookup: Any,
    *,
    source_id: str,
    source_name: str,
    city_id: str | None,
    market_kind: str | None,
    contract_shape: str | None,
) -> tuple[list[str], str | None, Any]:
    attempted_paths: list[str] = []
    city_value = _first_text(city_id) or UNKNOWN_VALUE
    market_kind_value = _first_text(market_kind) or UNKNOWN_VALUE
    contract_shape_value = _first_text(contract_shape) or UNKNOWN_VALUE

    for _label, city_unknown, kind_unknown, shape_unknown in BACKOFF_KEYS:
        lookup_city = UNKNOWN_VALUE if city_unknown else city_value
        lookup_kind = UNKNOWN_VALUE if kind_unknown else market_kind_value
        lookup_shape = UNKNOWN_VALUE if shape_unknown else contract_shape_value
        actual_label = _backoff_label(lookup_city, lookup_kind, lookup_shape)
        if attempted_paths and attempted_paths[-1] == actual_label:
            continue
        attempted_paths.append(actual_label)
        if lookup is None:
            continue
        observation = _LookupObservation(
            source_id=source_id,
            source_name=source_name,
            market=_LookupMarket(
                city_id=lookup_city,
                market_kind=lookup_kind,
                contract_shape=lookup_shape,
            ),
        )
        stats = lookup.lookup(observation)
        if stats is not None:
            return attempted_paths, actual_label, stats
    return attempted_paths, None, None


def _backoff_label(city_id: str, market_kind: str, contract_shape: str) -> str:
    city_part = "unknown" if city_id == UNKNOWN_VALUE else "city_id"
    kind_part = "unknown" if market_kind == UNKNOWN_VALUE else "market_kind"
    shape_part = "unknown" if contract_shape == UNKNOWN_VALUE else "contract_shape"
    return f"source_id+{city_part}+{kind_part}+{shape_part}"


def _normalize_candidate_outcome(candidate_outcome: str | None, *, question_side: str | None) -> str | None:
    normalized = (_optional_text(candidate_outcome) or "").upper() or None
    if normalized in {"YES", "NO"}:
        return normalized
    if normalized in {"ABOVE", "BELOW"}:
        return infer_predicted_outcome((question_side or "").lower(), normalized.lower())
    if normalized in {"BUY_YES", "BUY_NO"}:
        return normalized.split("_", 1)[1]
    return None


def _source_outcome_from_forecast(
    forecast_temp: float | None,
    *,
    threshold: float | None,
    threshold_low: float | None,
    threshold_high: float | None,
    question_side: str | None,
) -> str | None:
    normalized_question_side = (question_side or "").strip().lower()
    if normalized_question_side in {"range", "binary_bucket"}:
        if forecast_temp is None or threshold_low is None or threshold_high is None:
            return None
        low, high = sorted((threshold_low, threshold_high))
        return "YES" if low <= forecast_temp <= high else "NO"
    predicted_direction = _source_direction_from_forecast(forecast_temp, threshold)
    return infer_predicted_outcome(normalized_question_side, predicted_direction)


def _source_direction_from_forecast(forecast_temp: float | None, threshold: float | None) -> str | None:
    if forecast_temp is None or threshold is None:
        return None
    if forecast_temp > threshold:
        return "above"
    if forecast_temp < threshold:
        return "below"
    return None


def _normalize_observation(
    source: Mapping[str, Any],
    *,
    provenance: str,
    market_kind: str,
    forecast_target: str | None,
    market_date: str | None,
    row_observed_at: str | None,
) -> dict[str, Any] | None:
    source_name = _first_text(source.get("source_name"), source.get("name"), source.get("source"))
    source_id = _first_text(source.get("source_id"), source.get("id"), source.get("provider_id")) or _slug(source_name)
    if not source_id and not source_name:
        return None

    forecast_temp = _forecast_temp_for_source(source, market_kind=market_kind, forecast_target=forecast_target)
    temp_unit = _first_text(source.get("temp_unit"), source.get("temperature_unit"), source.get("unit"))
    if temp_unit is None and forecast_temp is not None:
        temp_unit = "F"
    provenance_value = _first_text(source.get("provenance")) or provenance
    known_at_time = _bool_or_none(source.get("known_at_time_assertion"))
    if known_at_time is None and provenance_value:
        known_at_time = "known_at_time" in provenance_value.lower()

    return {
        "source_id": source_id or "unknown",
        "source_name": source_name or source_id or "unknown",
        "source_family": _first_text(
            source.get("source_family"),
            source.get("family"),
            source.get("provider_family"),
            source.get("adapter_family"),
        ),
        "source_location_basis": _first_text(
            source.get("source_location_basis"),
            source.get("location_basis"),
            source.get("location_type"),
            source.get("gridpoint"),
        ),
        "forecast_temp_f": forecast_temp,
        "temp_unit": temp_unit,
        "forecast_valid_at": _first_text(
            source.get("forecast_valid_at"),
            source.get("forecast_date"),
            source.get("weather_date"),
            source.get("valid_at"),
            source.get("forecast_start"),
            source.get("forecast_period_start"),
            market_date,
        ),
        "observed_at": _first_text(source.get("observed_at"), row_observed_at),
        "fetched_at": _first_text(
            source.get("fetched_at"),
            source.get("source_fetched_at"),
            source.get("feed_timestamp"),
            source.get("timestamp"),
        ),
        "known_at_time_assertion": known_at_time,
        "adapter_version": _first_text(
            source.get("adapter_version"),
            source.get("provider_version"),
            source.get("adapter"),
        ),
        "normalizer_version": _first_text(source.get("normalizer_version")) or NORMALIZER_VERSION,
        "provenance": provenance_value,
    }


def _iter_source_detail_items(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        items: list[Mapping[str, Any]] = []
        for item in value:
            if isinstance(item, Mapping):
                items.append(item)
            elif isinstance(item, str) and item.strip():
                items.append({"source_name": item.strip()})
        return items
    if isinstance(value, Mapping):
        if isinstance(value.get("sources"), list):
            return _iter_source_detail_items(value["sources"])
        return [value]
    if isinstance(value, str) and value.strip():
        return [{"source_name": value.strip()}]
    return []


def _forecast_temp_for_source(
    source: Mapping[str, Any],
    *,
    market_kind: str,
    forecast_target: str | None,
) -> float | None:
    target_text = " ".join(part for part in (market_kind, _text(forecast_target)) if part).lower()
    if "high" in target_text or "max" in target_text:
        high_value = _first_number(
            source.get("forecast_high"),
            source.get("high"),
            source.get("temp_high"),
            source.get("maximum_temp"),
            source.get("max_temp"),
            source.get("high_temp_f"),
        )
        if high_value is not None:
            return high_value
    if "low" in target_text or "min" in target_text:
        low_value = _first_number(
            source.get("forecast_low"),
            source.get("low"),
            source.get("temp_low"),
            source.get("minimum_temp"),
            source.get("min_temp"),
            source.get("low_temp_f"),
        )
        if low_value is not None:
            return low_value
    if "current" in target_text:
        current_value = _first_number(
            source.get("current_forecast"),
            source.get("current_temp"),
            source.get("current"),
            source.get("current_temp_f"),
        )
        if current_value is not None:
            return current_value
    return _first_number(
        source.get("forecast_high"),
        source.get("forecast_low"),
        source.get("current_forecast"),
        source.get("predicted_temp"),
        source.get("forecast_temp"),
        source.get("temperature"),
        source.get("temp"),
        source.get("current_temp"),
        source.get("current"),
        source.get("high"),
        source.get("low"),
    )


def _shared_candidate_id(row: Mapping[str, Any]) -> str | None:
    return _first_text(
        row.get("shared_candidate_id"),
        _nested(row, "shared_candidate", "candidate_id"),
        row.get("candidate_id"),
        _nested(row, "decision_artifact", "strategy_signal", "shared_candidate_id"),
        _nested(row, "decision_artifact", "strategy_signal", "data", "shared_candidate_id"),
    )


def _market_id(row: Mapping[str, Any]) -> str | None:
    return _first_text(
        row.get("market_id"),
        _nested(row, "shared_candidate", "market_id"),
        _nested(row, "shared_candidate", "market", "id"),
        _nested(row, "market", "id"),
        _nested(row, "decision_artifact", "market_id"),
        _nested(row, "decision_artifact", "strategy_signal", "market_id"),
    )


def _observed_at(row: Mapping[str, Any]) -> str | None:
    return _first_text(
        row.get("observed_at"),
        _nested(row, "shared_candidate", "observed_at"),
        _nested(row, "decision_artifact", "observed_at"),
        _nested(row, "decision_artifact", "strategy_signal", "observed_at"),
        _nested(row, "decision_artifact", "strategy_signal", "data", "observed_at"),
        _nested(row, "data", "observed_at"),
    )


def _city_id(row: Mapping[str, Any]) -> str | None:
    city = _first_text(
        row.get("city_id"),
        _nested(row, "shared_candidate", "city_id"),
        _nested(row, "shared_candidate", "market", "city_id"),
        _nested(row, "market", "city_id"),
        _nested(row, "data", "city_id"),
    )
    if city:
        return city
    city_name = _first_text(
        row.get("city"),
        _nested(row, "shared_candidate", "city"),
        _nested(row, "shared_candidate", "market", "city"),
        _nested(row, "market", "city"),
    )
    return _slug(city_name)


def _market_date(row: Mapping[str, Any]) -> str | None:
    return _first_text(
        row.get("market_date"),
        _nested(row, "shared_candidate", "market_date"),
        _nested(row, "shared_candidate", "market", "market_date"),
        _nested(row, "shared_candidate", "market", "event_date"),
        _nested(row, "market", "market_date"),
        _nested(row, "market", "event_date"),
        _nested(row, "decision_artifact", "strategy_signal", "data", "market_date"),
        _nested(row, "data", "market_date"),
    )


def _market_kind(row: Mapping[str, Any]) -> str:
    explicit = _first_text(
        row.get("market_kind"),
        _nested(row, "shared_candidate", "market_kind"),
        _nested(row, "shared_candidate", "market", "market_kind"),
        _nested(row, "market", "market_kind"),
        _nested(row, "decision_artifact", "strategy_signal", "data", "market_kind"),
        _weather_data_value(row, "market_kind"),
        _nested(row, "data", "market_kind"),
    )
    if explicit:
        return explicit
    question_text = _market_question(row).lower()
    if "high temp" in question_text or "high temperature" in question_text or "maximum" in question_text:
        return "high"
    if "low temp" in question_text or "low temperature" in question_text or "minimum" in question_text:
        return "low"
    forecast_target = (_forecast_target(row) or "").lower()
    if "high" in forecast_target or "max" in forecast_target:
        return "high"
    if "low" in forecast_target or "min" in forecast_target:
        return "low"
    if "current" in forecast_target:
        return "current"
    return "unknown"



def _market_question(row: Mapping[str, Any]) -> str:
    return _first_text(
        row.get("question"),
        _nested(row, "shared_candidate", "question"),
        _nested(row, "shared_candidate", "market", "question"),
        _nested(row, "market", "question"),
        _nested(row, "decision_artifact", "market", "question"),
        _nested(row, "data", "question"),
    ) or ""

def _forecast_target(row: Mapping[str, Any]) -> str | None:
    return _first_text(
        row.get("forecast_target"),
        _nested(row, "shared_candidate", "forecast_target"),
        _nested(row, "shared_candidate", "market", "forecast_target"),
        _nested(row, "decision_artifact", "strategy_signal", "data", "forecast_target"),
        _weather_data_value(row, "forecast_target"),
        _nested(row, "data", "forecast_target"),
    )


def _contract_shape(row: Mapping[str, Any]) -> str | None:
    return _first_text(
        row.get("contract_shape"),
        _nested(row, "shared_candidate", "contract_shape"),
        _nested(row, "shared_candidate", "market", "contract_shape"),
        _nested(row, "market", "contract_shape"),
        _nested(row, "decision_artifact", "strategy_signal", "data", "contract_shape"),
        _nested(row, "shared_candidate", "market", "route", "evidence", "shape"),
        _weather_data_value(row, "contract_shape"),
        _nested(row, "data", "contract_shape"),
    )


def _question_side(row: Mapping[str, Any]) -> str | None:
    explicit = _first_text(
        row.get("question_side"),
        _nested(row, "shared_candidate", "question_side"),
        _nested(row, "shared_candidate", "market", "question_side"),
        _nested(row, "market", "question_side"),
        _nested(row, "decision_artifact", "strategy_signal", "data", "question_side"),
        _weather_data_value(row, "question_side"),
        _nested(row, "data", "question_side"),
    )
    if explicit:
        return explicit
    question = _market_question(row)
    if question:
        return infer_question_side(question)
    return None


def _predicted_outcome(row: Mapping[str, Any]) -> str | None:
    return _first_text(
        row.get("predicted_outcome"),
        _nested(row, "shared_candidate", "predicted_outcome"),
        _nested(row, "decision_artifact", "strategy_signal", "predicted_outcome"),
        _nested(row, "decision_artifact", "strategy_signal", "data", "predicted_outcome"),
        _weather_data_value(row, "predicted_outcome"),
        _nested(row, "data", "predicted_outcome"),
    )


def _candidate_side_mapping(row: Mapping[str, Any]) -> str:
    predicted_outcome = _predicted_outcome(row) or "unknown"
    candidate_side = _first_text(
        row.get("candidate_side"),
        row.get("question_side"),
        _nested(row, "shared_candidate", "candidate_side"),
        _nested(row, "shared_candidate", "question_side"),
        _nested(row, "decision_artifact", "strategy_signal", "candidate_side"),
        _nested(row, "decision_artifact", "strategy_signal", "data", "candidate_side"),
        _nested(row, "decision_artifact", "strategy_signal", "data", "question_side"),
    ) or "UNKNOWN"
    return f"{predicted_outcome} -> {candidate_side}"


def _engine_inputs_hash(row: Mapping[str, Any]) -> str:
    payload = {
        "shared_candidate_id": row.get("shared_candidate_id"),
        "market_id": row.get("market_id"),
        "observed_at": row.get("observed_at"),
        "predicted_outcome": row.get("predicted_outcome"),
        "market_kind": row.get("market_kind"),
        "contract_shape": row.get("contract_shape"),
        "question_side": row.get("question_side"),
        "threshold": row.get("threshold"),
        "threshold_low": row.get("threshold_low"),
        "threshold_high": row.get("threshold_high"),
        "forecast_target": row.get("forecast_target"),
        "forecast_horizon_hours": row.get("forecast_horizon_hours"),
        "source_observations": row.get("source_observations"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"



def _threshold_range_from_question(question: str) -> tuple[float | None, float | None]:
    if not question:
        return None, None
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:°|degrees?)?\s*(?:-|to|through)\s*(-?\d+(?:\.\d+)?)", question, flags=re.IGNORECASE)
    if not match:
        return None, None
    left = float(match.group(1))
    right = float(match.group(2))
    return (min(left, right), max(left, right))


def _weather_data_value(row: Mapping[str, Any], key: str) -> Any:
    return _first_non_null(
        _nested(row, "decision_artifact", "strategy_trace", "raw_signals", "live", "data", key),
        _nested(row, "decision_artifact", "strategy_trace", "accepted_signals", "live", "data", key),
        _nested(row, "decision_artifact", "strategy_trace", "ensemble_signal", "signal_details", "live", "data", key),
        _nested(row, "decision_artifact", "source_context", "data", "weather_source_snapshot", key),
        _nested(row, "decision_artifact", "source_context", "data", "weather_source_snapshot", "source_signal", "data", key),
        _nested(row, "weather_risk", "evidence", key),
    )


def _first_non_null(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None

def _nested(value: Any, *path: str) -> Any:
    current = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _first_text(*values: Any) -> str | None:
    for value in values:
        rendered = _text(value)
        if rendered is not None:
            return rendered
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _optional_text(value: Any) -> str | None:
    return _text(value)


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int(value: Any) -> int:
    number = _number(value)
    return int(number) if number is not None else 0


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def _slug(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or None


__all__ = [
    "ACTION_ALLOW",
    "ACTION_BLOCK",
    "CONFIDENCE_TYPE",
    "DIRECTION_DISAGREE",
    "DIRECTION_NO",
    "DIRECTION_UNKNOWN",
    "DIRECTION_YES",
    "ENGINE_VERSION",
    "GRADE_DISAGREE",
    "GRADE_INSUFFICIENT_HISTORY",
    "GRADE_NO_SOURCE_DATA",
    "GRADE_STRONG_NO",
    "GRADE_STRONG_YES",
    "GRADE_WEAK_NO",
    "GRADE_WEAK_YES",
    "NORMALIZER_VERSION",
    "SCHEMA_NAME",
    "build_source_confidence_row",
    "normalize_source_observations",
    "summarize_source_confidence_rows",
]
