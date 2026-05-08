from __future__ import annotations

from collections import Counter
from pathlib import Path
from math import isfinite
from typing import Any, Iterable

from bot.strategy_policy import coerce_strategy_policy, strategy_policy_status


def build_shadow_delta(
    decision_artifact: dict[str, Any],
    market_id: str,
    run_id: str,
    fallback_strategy_policy: Any = None,
) -> dict[str, Any] | None:
    """Build compact beta/shadow comparison metadata from a decision artifact."""
    if not isinstance(decision_artifact, dict):
        return None

    policy_status = _artifact_strategy_policy_status(
        decision_artifact,
        fallback_strategy_policy=fallback_strategy_policy,
    )
    if not (
        policy_status.get("version") == "beta"
        and policy_status.get("mode") == "shadow"
        and policy_status.get("shadow") is True
    ):
        return None

    shared_decision = (
        decision_artifact.get("shared_core_decision")
        if isinstance(decision_artifact.get("shared_core_decision"), dict)
        else {}
    )
    reasoning = shared_decision.get("reasoning") if isinstance(shared_decision.get("reasoning"), dict) else {}
    stable = _shadow_delta_side(
        action=decision_artifact.get("final_action"),
        reason_code=decision_artifact.get("final_reason_code"),
        requested_position_size=shared_decision.get("requested_position_size"),
        selected_lane=_stable_selected_lane(reasoning),
    )
    shadow = dict(stable)
    evidence_sources: list[str] = []
    status = "complete"
    comparison_complete = True
    action_comparison_available = True

    beta_lane_gate = _beta_lane_gate(reasoning)
    if beta_lane_gate and (
        beta_lane_gate.get("allowed") is False
        or beta_lane_gate.get("differs_from_final") is True
    ):
        evidence_sources.append("beta_lane_gate")
        shadow["selected_lane"] = beta_lane_gate.get("lane_id") or shadow.get("selected_lane")
        if beta_lane_gate.get("allowed") is False:
            shadow.update(
                _shadow_delta_side(
                    action="SKIP",
                    reason_code=beta_lane_gate.get("reason_code") or "strategy_lane_disabled",
                    requested_position_size=None,
                    selected_lane=shadow.get("selected_lane"),
                )
            )
        elif stable.get("action") == "SKIP" and beta_lane_gate.get("allowed") is True:
            status = "partial_beta_evidence"
            comparison_complete = False
            action_comparison_available = False
            shadow = _partial_shadow_delta_side(
                reason_code=beta_lane_gate.get("reason_code"),
                requested_position_size=None,
                selected_lane=shadow.get("selected_lane"),
            )

    weather_beta_gate = _weather_beta_gate(reasoning, "beta_gate")
    if weather_beta_gate:
        evidence_sources.append("weather_risk.beta_gate")
        if weather_beta_gate.get("would_reject"):
            status = "complete"
            comparison_complete = True
            action_comparison_available = True
            shadow.update(
                _shadow_delta_side(
                    action="SKIP",
                    reason_code=weather_beta_gate.get("reason_code") or "weather_risk_rejected",
                    requested_position_size=None,
                    selected_lane=shadow.get("selected_lane"),
                )
            )

    if action_comparison_available and shadow.get("action") != "SKIP":
        lane_sizing = reasoning.get("lane_sizing") if isinstance(reasoning.get("lane_sizing"), dict) else {}
        if _active_shadow_gate(lane_sizing) and lane_sizing.get("beta_adjusted_size") is not None:
            evidence_sources.append("lane_sizing")
            shadow["requested_position_size"] = _coerce_finite_number(lane_sizing.get("beta_adjusted_size"))

        weather_sizing_gate = _weather_beta_gate(reasoning, "beta_sizing_gate")
        if weather_sizing_gate and weather_sizing_gate.get("beta_adjusted_size") is not None:
            evidence_sources.append("weather_risk.beta_sizing_gate")
            shadow["requested_position_size"] = _coerce_finite_number(weather_sizing_gate.get("beta_adjusted_size"))

    if not evidence_sources:
        return None

    if action_comparison_available:
        action_changed: bool | None = stable.get("action") != shadow.get("action")
        side_changed: bool | None = stable.get("direction") != shadow.get("direction")
        buy_decision_changed: bool | None = (stable.get("action") != "SKIP") != (shadow.get("action") != "SKIP")
        reason_changed: bool | None = stable.get("reason_code") != shadow.get("reason_code")
        size_changed: bool | None = stable.get("requested_position_size") != shadow.get("requested_position_size")
    else:
        action_changed = None
        side_changed = None
        buy_decision_changed = None
        reason_changed = None
        size_changed = None
    lane_changed = stable.get("selected_lane") != shadow.get("selected_lane")
    known_changes = [
        value
        for value in (action_changed, side_changed, buy_decision_changed, reason_changed, size_changed, lane_changed)
        if value is not None
    ]
    if action_comparison_available:
        changed = any(known_changes) if known_changes else False
    else:
        changed = True if any(known_changes) else None

    return {
        "schema_version": 1,
        "mode": "beta_shadow_delta",
        "status": status,
        "comparison_complete": comparison_complete,
        "action_comparison_available": action_comparison_available,
        "policy": {
            "version": policy_status.get("version"),
            "mode": policy_status.get("mode"),
            "enabled_features": [
                name
                for name, enabled in sorted((policy_status.get("enabled_features") or {}).items())
                if enabled is True
            ],
        },
        "stable": stable,
        "shadow": shadow,
        "changed": changed,
        "action_changed": action_changed,
        "side_changed": side_changed,
        "buy_decision_changed": buy_decision_changed,
        "reason_changed": reason_changed,
        "size_changed": size_changed,
        "lane_changed": lane_changed,
        "dedupe_key": f"{market_id}|{run_id}|beta-shadow",
        "evidence_sources": evidence_sources,
    }


def summarize_shadow_delta_rows(
    rows: Iterable[dict[str, Any]],
    *,
    prediction_lab_rows: bool = False,
) -> dict[str, Any]:
    """Summarize top-level row shadow deltas without creating synthetic rows."""
    total_rows = 0
    keyed: dict[str, dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        shadow_delta = row.get("shadow_delta")
        if not isinstance(shadow_delta, dict) or not shadow_delta:
            continue
        total_rows += 1
        key = _shadow_delta_summary_key(row, shadow_delta, prediction_lab_rows=prediction_lab_rows)
        if key is None:
            unkeyed.append(row)
            continue
        existing = keyed.get(key)
        if existing is None or _prefer_shadow_delta_summary_row(row, existing):
            keyed[key] = row

    opportunity_rows = list(keyed.values()) + unkeyed
    action_available_rows = [
        row
        for row in opportunity_rows
        if _shadow_delta(row).get("action_comparison_available") is True
    ]
    unavailable_action_rows = len(opportunity_rows) - len(action_available_rows)
    changed_counts = _shadow_delta_changed_counts(opportunity_rows)
    summary = {
        "schema_version": 1,
        "basis": "row_level_shadow_delta_deduped",
        "total_shadow_delta_rows": total_rows,
        "shadow_delta_rows": total_rows,
        "total_shadow_delta_opportunities": len(opportunity_rows),
        "shadow_delta_opportunities": len(opportunity_rows),
        "keyed_opportunities": len(keyed),
        "unkeyed_opportunities": len(unkeyed),
        "deduped_duplicate_rows": total_rows - len(opportunity_rows),
        "status_counts": _counts(_coerce_label(_shadow_delta(row).get("status"), "unknown") for row in opportunity_rows),
        "changed_counts": changed_counts,
        "changed_rows": changed_counts.get("changed", 0),
        "unchanged_rows": changed_counts.get("unchanged", 0),
        "unknown_changed_rows": changed_counts.get("unknown", 0),
        "action_comparison_available_rows": len(action_available_rows),
        "unavailable_action_comparisons": unavailable_action_rows,
        "action_comparison_unavailable_rows": unavailable_action_rows,
        "action_changed": _bool_count(opportunity_rows, "action_changed", available_only=True),
        "action_unchanged": _bool_count(opportunity_rows, "action_changed", false_only=True, available_only=True),
        "side_changed": _bool_count(opportunity_rows, "side_changed", available_only=True),
        "side_unchanged": _bool_count(opportunity_rows, "side_changed", false_only=True, available_only=True),
        "buy_decision_changed": _bool_count(opportunity_rows, "buy_decision_changed", available_only=True),
        "buy_decision_unchanged": _bool_count(opportunity_rows, "buy_decision_changed", false_only=True, available_only=True),
        "reason_changed": _bool_count(opportunity_rows, "reason_changed"),
        "reason_unchanged": _bool_count(opportunity_rows, "reason_changed", false_only=True),
        "size_changed": _bool_count(opportunity_rows, "size_changed"),
        "size_unchanged": _bool_count(opportunity_rows, "size_changed", false_only=True),
        "lane_changed": _bool_count(opportunity_rows, "lane_changed"),
        "lane_unchanged": _bool_count(opportunity_rows, "lane_changed", false_only=True),
        "stable_action_counts": _counts(
            _coerce_label((_shadow_delta(row).get("stable") or {}).get("action"), "unknown")
            for row in opportunity_rows
            if isinstance(_shadow_delta(row).get("stable"), dict)
        ),
        "shadow_action_counts": _counts(
            _coerce_label((_shadow_delta(row).get("shadow") or {}).get("action"), "unknown")
            for row in opportunity_rows
            if isinstance(_shadow_delta(row).get("shadow"), dict)
        ),
        "evidence_source_counts": _counts(
            source
            for row in opportunity_rows
            for source in _shadow_delta_evidence_sources(_shadow_delta(row))
        ),
    }
    return summary


def format_shadow_delta_summary(summary: dict[str, Any] | None) -> str | None:
    if not isinstance(summary, dict):
        return None
    opportunities = int(summary.get("total_shadow_delta_opportunities") or summary.get("shadow_delta_opportunities") or 0)
    if opportunities <= 0:
        return None
    raw_rows = int(summary.get("total_shadow_delta_rows") or summary.get("shadow_delta_rows") or opportunities)
    return (
        "Shadow delta: "
        f"{opportunities} opportunities"
        f" ({raw_rows} rows, deduped {int(summary.get('deduped_duplicate_rows') or 0)}) | "
        f"changed {int(summary.get('changed_rows') or 0)} | "
        f"action {int(summary.get('action_changed') or 0)} | "
        f"side {int(summary.get('side_changed') or 0)} | "
        f"buy {int(summary.get('buy_decision_changed') or 0)} | "
        f"reason {int(summary.get('reason_changed') or 0)} | "
        f"size {int(summary.get('size_changed') or 0)} | "
        f"lane {int(summary.get('lane_changed') or 0)} | "
        f"action unavailable {int(summary.get('unavailable_action_comparisons') or 0)}"
    )


def _shadow_delta(row: dict[str, Any]) -> dict[str, Any]:
    shadow_delta = row.get("shadow_delta")
    return shadow_delta if isinstance(shadow_delta, dict) else {}


def _shadow_delta_summary_key(
    row: dict[str, Any],
    shadow_delta: dict[str, Any],
    *,
    prediction_lab_rows: bool,
) -> str | None:
    dedupe_key = shadow_delta.get("dedupe_key")
    if dedupe_key not in (None, ""):
        return str(dedupe_key)
    if prediction_lab_rows or _looks_like_prediction_lab_row(row):
        market_id = row.get("market_id")
        run_id = row.get("run_id")
        if market_id not in (None, "") and run_id not in (None, ""):
            return f"{market_id}|{run_id}|beta-shadow"
    return None


def _looks_like_prediction_lab_row(row: dict[str, Any]) -> bool:
    source_path = row.get("_source_path") or row.get("source_path")
    if source_path not in (None, ""):
        name = Path(str(source_path)).name
        if name in {"predictions.jsonl", "market_snapshots.jsonl"}:
            return True
    if row.get("prediction_id") not in (None, ""):
        return True
    return False


def _prefer_shadow_delta_summary_row(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    candidate_recorded = candidate.get("recorded_prediction") is True
    existing_recorded = existing.get("recorded_prediction") is True
    if candidate_recorded != existing_recorded:
        return candidate_recorded
    candidate_artifact = _has_populated_decision_artifact(candidate)
    existing_artifact = _has_populated_decision_artifact(existing)
    if candidate_artifact != existing_artifact:
        return candidate_artifact
    return False


def _has_populated_decision_artifact(row: dict[str, Any]) -> bool:
    artifact = row.get("decision_artifact")
    return isinstance(artifact, dict) and bool(artifact)


def _shadow_delta_changed_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        value = _shadow_delta(row).get("changed")
        if value is True:
            counts["changed"] += 1
        elif value is False:
            counts["unchanged"] += 1
        else:
            counts["unknown"] += 1
    return dict(sorted(counts.items()))


def _bool_count(
    rows: list[dict[str, Any]],
    key: str,
    *,
    false_only: bool = False,
    available_only: bool = False,
) -> int:
    count = 0
    for row in rows:
        shadow_delta = _shadow_delta(row)
        if available_only and shadow_delta.get("action_comparison_available") is not True:
            continue
        value = shadow_delta.get(key)
        if false_only:
            count += value is False
        else:
            count += value is True
    return count


def _shadow_delta_evidence_sources(shadow_delta: dict[str, Any]) -> list[str]:
    sources = shadow_delta.get("evidence_sources")
    if not isinstance(sources, list):
        return []
    return [str(source) for source in sources if source not in (None, "")]


def _coerce_label(value: Any, default: str) -> str:
    if value in (None, ""):
        return default
    return str(value)


def _counts(values: Iterable[Any]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for value in values:
        counter[str(value)] += 1
    return dict(sorted(counter.items()))


def _artifact_strategy_policy_status(
    decision_artifact: dict[str, Any],
    *,
    fallback_strategy_policy: Any = None,
) -> dict[str, Any]:
    shared_decision = (
        decision_artifact.get("shared_core_decision")
        if isinstance(decision_artifact.get("shared_core_decision"), dict)
        else {}
    )
    reasoning = shared_decision.get("reasoning") if isinstance(shared_decision.get("reasoning"), dict) else {}
    for value in (
        reasoning.get("strategy_policy_status"),
        _nested_get(reasoning, ("strategy_lane", "evidence", "strategy_policy")),
        _nested_get(reasoning, ("lane_sizing", "policy")),
        _nested_get(reasoning, ("weather_risk", "beta_gate", "policy")),
        _nested_get(reasoning, ("weather_risk", "beta_sizing_gate", "policy")),
    ):
        if isinstance(value, dict) and value:
            return _normalize_policy_status(value)
    return strategy_policy_status(coerce_strategy_policy(fallback_strategy_policy))


def _normalize_policy_status(value: dict[str, Any]) -> dict[str, Any]:
    status = strategy_policy_status(coerce_strategy_policy(value))
    enabled_features = value.get("enabled_features")
    if isinstance(enabled_features, dict):
        status["enabled_features"] = {str(key): enabled is True for key, enabled in enabled_features.items()}
    return status


def _nested_get(root: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = root
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _stable_selected_lane(reasoning: dict[str, Any]) -> str | None:
    strategy_lane = reasoning.get("strategy_lane") if isinstance(reasoning.get("strategy_lane"), dict) else {}
    lane_id = strategy_lane.get("lane_id")
    return str(lane_id) if lane_id not in (None, "") else None


def _beta_lane_gate(reasoning: dict[str, Any]) -> dict[str, Any] | None:
    gate = _nested_get(reasoning, ("strategy_lane", "evidence", "beta_lane_gate"))
    if not isinstance(gate, dict):
        return None
    if _active_shadow_gate(gate):
        return gate
    if gate.get("beta_behavior_enabled") is True and gate.get("beta_behavior_enforced") is not True:
        return gate
    return None


def _weather_beta_gate(reasoning: dict[str, Any], name: str) -> dict[str, Any] | None:
    gate = _nested_get(reasoning, ("weather_risk", name))
    if isinstance(gate, dict) and _active_shadow_gate(gate):
        return gate
    return None


def _active_shadow_gate(gate: dict[str, Any]) -> bool:
    return bool(gate.get("active") is True and gate.get("shadow") is True and gate.get("enforced") is not True)


def _shadow_delta_side(
    *,
    action: Any,
    reason_code: Any,
    requested_position_size: Any,
    selected_lane: Any,
) -> dict[str, Any]:
    normalized_action = str(action or "SKIP").upper()
    if normalized_action not in {"BUY_YES", "BUY_NO"}:
        normalized_action = "SKIP"
    requested_size = _coerce_finite_number(requested_position_size)
    if normalized_action == "SKIP":
        requested_size = None
    return {
        "action": normalized_action,
        "reason_code": str(reason_code) if reason_code not in (None, "") else None,
        "direction": normalized_action if normalized_action in {"BUY_YES", "BUY_NO"} else "SKIP",
        "decision_type": _decision_type_for_action(normalized_action),
        "requested_position_size": requested_size,
        "selected_lane": str(selected_lane) if selected_lane not in (None, "") else None,
    }


def _partial_shadow_delta_side(
    *,
    reason_code: Any,
    requested_position_size: Any,
    selected_lane: Any,
) -> dict[str, Any]:
    return {
        "action": None,
        "reason_code": str(reason_code) if reason_code not in (None, "") else None,
        "direction": None,
        "decision_type": "unknown",
        "requested_position_size": _coerce_finite_number(requested_position_size),
        "selected_lane": str(selected_lane) if selected_lane not in (None, "") else None,
    }


def _decision_type_for_action(action: str) -> str:
    if action == "BUY_YES":
        return "buy_yes"
    if action == "BUY_NO":
        return "buy_no"
    return "skip"


def _coerce_finite_number(value: Any) -> float | None:
    try:
        if value is None:
            return None
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(numeric):
        return None
    return round(numeric, 4)


__all__ = ["build_shadow_delta", "format_shadow_delta_summary", "summarize_shadow_delta_rows"]
