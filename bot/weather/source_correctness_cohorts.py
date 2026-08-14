"""Outcome-blind, event-aware cohorts for source-correctness research.

The selector deliberately works only with sealed candidate decisions and their
post-resolution correctness observations.  It never opens collector rows and
does not use correctness to choose a representative or fill a quota.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


DEFAULT_PER_SHAPE_TARGET = 30
_VALID_SIDES = frozenset({"YES", "NO"})
_VALID_CORRECTNESS = frozenset({"correct", "incorrect"})


def select_source_correctness_cohort(
    candidate_decisions: Iterable[Mapping[str, Any]],
    correctness_observations: Iterable[Mapping[str, Any]],
    *,
    per_shape_target: int = DEFAULT_PER_SHAPE_TARGET,
) -> dict[str, Any]:
    """Select the oldest eligible representative per ``(event, shape)``.

    A missing event ticker is explicitly retained as a market-id fallback with
    ``contract_only`` independence quality.  This permits audit without
    treating the fallback as independently identified event evidence.
    """
    if per_shape_target < 1:
        raise ValueError("per_shape_target must be at least 1")

    observations, observation_stats = _correctness_by_decision(correctness_observations)
    decisions = [dict(row) for row in candidate_decisions if isinstance(row, Mapping)]
    eligible: list[dict[str, Any]] = []
    eligibility_exclusions: Counter[str] = Counter()
    seen_decision_ids: Counter[str] = Counter(
        str(row.get("decision_id")) for row in decisions if _text(row.get("decision_id")) is not None
    )
    for decision in decisions:
        item, reason = _eligible_item(decision, observations, seen_decision_ids)
        if item is None:
            eligibility_exclusions[reason or "invalid_candidate"] += 1
            continue
        eligible.append(item)

    per_shape: list[dict[str, Any]] = []
    selected_all: list[dict[str, Any]] = []
    for shape in sorted({item["contract_shape"] for item in eligible}):
        shape_items = [item for item in eligible if item["contract_shape"] == shape]
        shape_items.sort(key=_chronological_key)
        representatives: list[dict[str, Any]] = []
        represented_units: set[str] = set()
        for item in shape_items:
            unit_id = item["unit_id"]
            if unit_id in represented_units:
                continue
            represented_units.add(unit_id)
            representatives.append(item)
        selected = representatives[:per_shape_target]
        selected_rows = [_representative_row(item) for item in selected]
        selected_all.extend(selected_rows)
        selected_counts = Counter(row["source_selection_correctness"] for row in selected_rows)
        available_event_tickers = {item["event_ticker"] for item in representatives if item["event_ticker"] is not None}
        selected_event_tickers = {item["event_ticker"] for item in selected if item["event_ticker"] is not None}
        per_shape.append({
            "contract_shape": shape,
            "target_count": per_shape_target,
            "eligible_candidate_observations": len(shape_items),
            "available_count": len(representatives),
            "available_unique_units": len(representatives),
            "available_unique_event_count": len(available_event_tickers),
            "available_contract_only_unit_count": sum(item["independence_quality"] == "contract_only" for item in representatives),
            "duplicate_or_reobservation_exclusions": len(shape_items) - len(representatives),
            "selected_count": len(selected_rows),
            "selected_unique_event_count": len(selected_event_tickers),
            "selected_contract_only_unit_count": sum(item["independence_quality"] == "contract_only" for item in selected),
            "quota_exclusions": max(0, len(representatives) - per_shape_target),
            "shortfall": max(0, per_shape_target - len(selected_rows)),
            "correct": selected_counts["correct"],
            "incorrect": selected_counts["incorrect"],
            "correctness_rate": _rate(selected_counts),
            "selected_representatives": selected_rows,
        })

    return {
        "schema_name": "source_correctness_shape_cohort_report",
        "schema_version": 1,
        "research_status": "offline_source_only_research",
        "selection_policy": {
            "per_shape_target": per_shape_target,
            "selection_order": "oldest_to_newest_decision_timestamp_then_decision_id",
            "representative_rule": "at_most_one_candidate_per_unit_id_and_contract_shape",
            "unit_id_rule": "event_ticker_when_present_else_market_id",
            "outcome_blind_selection": True,
            "eligibility": (
                "candidate lane decision with selected source, valid YES/NO source side, "
                "and one exact post-resolution correct/incorrect observation"
            ),
        },
        "input_counts": {
            "candidate_decisions_seen": len(decisions),
            "valid_correctness_observations": observation_stats["accepted"],
            "ambiguous_correctness_observations": observation_stats["ambiguous"],
            "eligible_candidate_observations": len(eligible),
            "eligibility_exclusions": dict(sorted(eligibility_exclusions.items())),
        },
        "per_shape": per_shape,
        "aggregate": _aggregate(selected_all),
    }


def _correctness_by_decision(rows: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Mapping[str, Any]], Counter[str]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    stats: Counter[str] = Counter()
    for row in rows:
        if not isinstance(row, Mapping):
            stats["invalid"] += 1
            continue
        decision_id = _text(row.get("decision_id"))
        correctness = row.get("source_selection_correctness")
        if decision_id is None or correctness not in _VALID_CORRECTNESS:
            stats["invalid"] += 1
            continue
        grouped[decision_id].append(row)
    accepted: dict[str, Mapping[str, Any]] = {}
    for decision_id, matches in grouped.items():
        if len(matches) != 1:
            stats["ambiguous"] += 1
            continue
        accepted[decision_id] = matches[0]
        stats["accepted"] += 1
    return accepted, stats


def _eligible_item(
    decision: Mapping[str, Any], observations: Mapping[str, Mapping[str, Any]], seen_decision_ids: Mapping[str, int],
) -> tuple[dict[str, Any] | None, str | None]:
    if decision.get("lane_id") != "source_router_candidate_v1":
        return None, "not_candidate_lane"
    decision_id = _text(decision.get("decision_id"))
    if decision_id is None or seen_decision_ids.get(decision_id, 0) != 1:
        return None, "ambiguous_or_missing_decision_id"
    observation = observations.get(decision_id)
    if observation is None:
        return None, "missing_exact_correctness_observation"
    if _text(decision.get("selected_source_id")) is None:
        return None, "missing_selected_source"
    if decision.get("side") not in _VALID_SIDES:
        return None, "invalid_source_side"
    timestamp = _parse_timestamp(decision.get("decision_timestamp"))
    if timestamp is None:
        return None, "invalid_decision_timestamp"
    market_id = _text(decision.get("market_id"))
    if market_id is None:
        return None, "missing_market_id"
    event_ticker = _text(decision.get("event_ticker"))
    unit_id = event_ticker or market_id
    independence_quality = "event_ticker" if event_ticker is not None else "contract_only"
    if decision.get("unit_id") != unit_id or decision.get("independence_quality") != independence_quality:
        return None, "invalid_event_unit_identity"
    return {
        "decision_id": decision_id,
        "market_id": market_id,
        "decision_timestamp": _timestamp_text(timestamp),
        "timestamp_sort": timestamp,
        "event_ticker": event_ticker,
        "unit_id": unit_id,
        "independence_quality": independence_quality,
        "city_id": _trait(decision.get("city_id")),
        "market_kind": _trait(decision.get("market_kind")),
        "contract_shape": _shape(decision),
        "subcategory": _trait(decision.get("subcategory")),
        "question_side": _trait(decision.get("question_side")),
        "selected_source_id": str(decision["selected_source_id"]).strip(),
        "source_implied_side": decision["side"],
        "source_selection_correctness": observation["source_selection_correctness"],
    }, None


def _aggregate(selected: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(row["source_selection_correctness"] for row in selected)
    event_shapes: dict[str, set[str]] = defaultdict(set)
    for row in selected:
        if row["event_ticker"] is not None:
            event_shapes[str(row["event_ticker"])].add(row["contract_shape"])
    shared = [
        {"event_ticker": ticker, "contract_shapes": sorted(shapes)}
        for ticker, shapes in sorted(event_shapes.items()) if len(shapes) > 1
    ]
    return {
        "selected_count": len(selected),
        "correct": counts["correct"],
        "incorrect": counts["incorrect"],
        "correctness_rate": _rate(counts),
        "selected_global_unique_unit_count": len({row["unit_id"] for row in selected}),
        "selected_global_unique_event_count": len(event_shapes),
        "selected_contract_only_unit_count": len({row["unit_id"] for row in selected if row["independence_quality"] == "contract_only"}),
        "cross_shape_shared_event_count": len(shared),
        "cross_shape_shared_events": shared,
        "cross_shape_shared_event_warning": bool(shared),
        "independence_note": (
            "Per-shape cohorts may share event_ticker values; counts are not a claim that shapes are mutually independent. "
            "Contract-only fallbacks lack an event-level independence identifier."
        ),
    }


def _representative_row(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in (
        "decision_id", "market_id", "decision_timestamp", "event_ticker", "unit_id", "independence_quality",
        "city_id", "market_kind", "contract_shape", "subcategory", "question_side", "selected_source_id",
        "source_implied_side", "source_selection_correctness",
    )}


def _shape(decision: Mapping[str, Any]) -> str:
    return _trait(decision.get("contract_shape")) or _trait(decision.get("subcategory")) or "unknown"


def _trait(value: Any) -> str | None:
    return _text(value)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _timestamp_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _chronological_key(item: Mapping[str, Any]) -> tuple[datetime, str]:
    return item["timestamp_sort"], item["decision_id"]


def _rate(counts: Mapping[str, int]) -> float | None:
    total = counts.get("correct", 0) + counts.get("incorrect", 0)
    return round(counts.get("correct", 0) / total, 6) if total else None
