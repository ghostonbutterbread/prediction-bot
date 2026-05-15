from __future__ import annotations

from collections import Counter
from math import isfinite
from typing import Any

from bot.strategy_lanes import CONFIDENCE_SLOW_PROFIT_LANE


def extract_strategy_lane(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return strategy-lane metadata from known paper/live/lab artifact shapes."""
    if not isinstance(row, dict):
        return None

    direct = row.get("strategy_lane")
    if isinstance(direct, dict):
        return direct

    for reasoning in (
        row.get("reasoning"),
        row.get("decision_trace"),
        _shared_core_decision(row).get("reasoning"),
    ):
        if isinstance(reasoning, dict) and isinstance(reasoning.get("strategy_lane"), dict):
            return reasoning["strategy_lane"]

    return None


def extract_lane_sizing(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return lane-sizing metadata from known paper/live/lab artifact shapes."""
    if not isinstance(row, dict):
        return None

    direct = row.get("lane_sizing")
    if isinstance(direct, dict):
        return direct

    for reasoning in (
        row.get("reasoning"),
        row.get("decision_trace"),
        _shared_core_decision(row).get("reasoning"),
    ):
        if isinstance(reasoning, dict) and isinstance(reasoning.get("lane_sizing"), dict):
            return reasoning["lane_sizing"]

    return None


def summarize_strategy_lanes(rows: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "schema_version": 1,
        "basis": "strategy_lane",
        "rows_scanned": 0,
        "lane_rows": 0,
        "no_lane_rows": 0,
        "selected_lane_counts": {},
        "would_select_lane_counts": {},
        "would_select_allowed_counts": {},
        "selected_slow_profit_rows": 0,
        "would_select_slow_profit_rows": 0,
        "slow_profit_differs_from_final_rows": 0,
        "beta_lane_gate_rows": 0,
        "beta_lane_gate_missing_rows": 0,
        "lane_selection_delta_rows": 0,
        "approved_lane_rows": 0,
        "rejected_lane_rows": 0,
        "lane_sizing_rows": 0,
        "lane_sizing_configured_rows": 0,
        "lane_sizing_would_adjust_rows": 0,
        "lane_sizing_applied_rows": 0,
        "lane_sizing_preserved_rows": 0,
        "lane_sizing_shadow_rows": 0,
        "lane_sizing_differs_from_final_rows": 0,
        "lane_sizing_selected_lane_counts": {},
        "lane_sizing_size_counts": {},
        "lane_sizing_size_totals": {},
        "lane_sizing_size_avgs": {},
    }
    selected_counts: Counter[str] = Counter()
    would_select_counts: Counter[str] = Counter()
    would_select_allowed_counts: Counter[str] = Counter()
    sizing_selected_counts: Counter[str] = Counter()
    sizing_size_counts: Counter[str] = Counter()
    sizing_size_totals: Counter[str] = Counter()

    for row in rows or []:
        summary["rows_scanned"] += 1
        if not isinstance(row, dict):
            summary["no_lane_rows"] += 1
            continue

        lane = extract_strategy_lane(row)
        lane_sizing = extract_lane_sizing(row)
        if not isinstance(lane, dict):
            summary["no_lane_rows"] += 1
            if isinstance(lane_sizing, dict):
                _record_lane_sizing(summary, sizing_selected_counts, sizing_size_counts, sizing_size_totals, lane_sizing)
            continue

        summary["lane_rows"] += 1
        selected_lane = _clean_label(lane.get("lane_id"))
        selected_counts[selected_lane] += 1
        if isinstance(lane_sizing, dict):
            _record_lane_sizing(
                summary,
                sizing_selected_counts,
                sizing_size_counts,
                sizing_size_totals,
                lane_sizing,
                fallback_lane_id=selected_lane,
            )
        if selected_lane == CONFIDENCE_SLOW_PROFIT_LANE:
            summary["selected_slow_profit_rows"] += 1

        final_reason_code = _final_reason_code(row)
        if _is_final_rejected(row, final_reason_code=final_reason_code):
            summary["rejected_lane_rows"] += 1
        elif _is_final_approved(row, final_reason_code=final_reason_code):
            summary["approved_lane_rows"] += 1

        evidence = lane.get("evidence") if isinstance(lane.get("evidence"), dict) else {}
        beta_gate = evidence.get("beta_lane_gate") if isinstance(evidence.get("beta_lane_gate"), dict) else {}
        if beta_gate:
            summary["beta_lane_gate_rows"] += 1
        else:
            summary["beta_lane_gate_missing_rows"] += 1
        would_select_lane = _clean_label(beta_gate.get("lane_id") or selected_lane)
        would_select_counts[would_select_lane] += 1
        would_select_allowed_counts["allowed" if beta_gate.get("allowed", True) else "rejected"] += 1
        if would_select_lane == CONFIDENCE_SLOW_PROFIT_LANE:
            summary["would_select_slow_profit_rows"] += 1
            if beta_gate.get("differs_from_final"):
                summary["slow_profit_differs_from_final_rows"] += 1
        if beta_gate.get("differs_from_final"):
            summary["lane_selection_delta_rows"] += 1

    summary["selected_lane_counts"] = dict(sorted(selected_counts.items()))
    summary["would_select_lane_counts"] = dict(sorted(would_select_counts.items()))
    summary["would_select_allowed_counts"] = dict(sorted(would_select_allowed_counts.items()))
    summary["lane_sizing_selected_lane_counts"] = dict(sorted(sizing_selected_counts.items()))
    summary["lane_sizing_size_counts"] = dict(sorted(sizing_size_counts.items()))
    summary["lane_sizing_size_totals"] = {
        key: round(value, 4) for key, value in sorted(sizing_size_totals.items())
    }
    summary["lane_sizing_size_avgs"] = {
        key: round(sizing_size_totals[key] / count, 4)
        for key, count in sorted(sizing_size_counts.items())
        if count
    }
    return summary


def build_strategy_lane_rollout_readiness(
    *,
    policy_status: dict[str, Any] | None,
    strategy_lane_summary: dict[str, Any] | None,
    hidden_gem_evidence_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build an observability-only checklist for beta strategy-lane enforce readiness."""
    policy = _normalize_policy_status(policy_status)
    lane_summary = strategy_lane_summary if isinstance(strategy_lane_summary, dict) else {}
    hidden_summary = hidden_gem_evidence_summary if isinstance(hidden_gem_evidence_summary, dict) else {}

    rows_scanned = _coerce_int(lane_summary.get("rows_scanned"))
    lane_rows = _coerce_int(lane_summary.get("lane_rows"))
    hidden_rows = _coerce_int(hidden_summary.get("rows_scanned")) or rows_scanned
    card_rows = _coerce_int(hidden_summary.get("card_rows"))
    insufficient_card_rows = _coerce_int(hidden_summary.get("insufficient_data_rows"))
    clean_card_rows = max(0, card_rows - insufficient_card_rows)
    beta_lane_gate_rows = _coerce_int(lane_summary.get("beta_lane_gate_rows"))
    lane_delta_rows = _coerce_int(lane_summary.get("lane_selection_delta_rows"))
    sizing_rows = _coerce_int(lane_summary.get("lane_sizing_rows"))
    sizing_diff_rows = _coerce_int(lane_summary.get("lane_sizing_differs_from_final_rows"))
    sizing_would_adjust_rows = _coerce_int(lane_summary.get("lane_sizing_would_adjust_rows"))
    sizing_shadow_rows = _coerce_int(lane_summary.get("lane_sizing_shadow_rows"))

    enabled_features = set(policy["enabled_features"])
    checks: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []

    def add_check(name: str, ok: bool, detail: str, *, severity: str = "blocker") -> None:
        check = {"name": name, "ok": bool(ok), "severity": severity, "detail": detail}
        checks.append(check)
        if ok:
            return
        if severity == "warning":
            warnings.append(f"{name}: {detail}")
        else:
            blockers.append(f"{name}: {detail}")

    add_check(
        "pre_enforce_shadow_policy",
        (
            policy["version"] == "beta"
            and policy["mode"] == "shadow"
            and policy["active"] is True
            and policy["shadow"] is True
            and policy["enforce"] is False
        ),
        (
            f"policy is {policy['version']}/{policy['mode']} "
            f"active={policy['active']} shadow={policy['shadow']} enforce={policy['enforce']}; "
            "collect clean normalized shadow evidence before enforce"
        ),
    )
    add_check(
        "beta_features_enabled",
        bool(enabled_features),
        "no active beta features are enabled",
    )
    add_check(
        "weather_hidden_gem_evidence_card_feature",
        "weather_hidden_gem_evidence_card" in enabled_features,
        "weather_hidden_gem_evidence_card is not active",
    )
    add_check(
        "hidden_gem_lane_gates_feature",
        "hidden_gem_lane_gates" in enabled_features,
        "hidden_gem_lane_gates is not active",
    )
    add_check(
        "confidence_slow_profit_feature",
        "confidence_slow_profit" in enabled_features,
        "confidence_slow_profit is not active",
    )
    add_check(
        "lane_sizing_caps_feature",
        "lane_sizing_caps" in enabled_features,
        "lane_sizing_caps is not active",
    )
    add_check(
        "strategy_lane_rows_present",
        rows_scanned > 0 and lane_rows > 0,
        f"strategy lane rows {lane_rows}/{rows_scanned}",
    )
    add_check(
        "hidden_gem_evidence_cards_present",
        hidden_rows > 0 and card_rows > 0,
        f"hidden-gem evidence cards {card_rows}/{hidden_rows}",
    )
    add_check(
        "hidden_gem_evidence_cards_clean",
        card_rows > 0 and insufficient_card_rows == 0,
        f"insufficient hidden-gem evidence cards {insufficient_card_rows}/{card_rows}",
        severity="warning",
    )
    add_check(
        "lane_delta_coverage_present",
        lane_rows > 0 and beta_lane_gate_rows > 0,
        f"beta lane-gate coverage {beta_lane_gate_rows}/{lane_rows}",
    )
    add_check(
        "lane_delta_coverage_complete",
        lane_rows > 0 and beta_lane_gate_rows == lane_rows,
        f"beta lane-gate coverage {beta_lane_gate_rows}/{lane_rows}",
        severity="warning",
    )
    add_check(
        "lane_deltas_observed",
        lane_delta_rows > 0,
        "no lane-selection deltas observed in this sample",
        severity="warning",
    )
    add_check(
        "lane_sizing_delta_coverage_present",
        sizing_rows > 0,
        f"lane sizing rows {sizing_rows}/{lane_rows or rows_scanned}",
    )
    add_check(
        "lane_sizing_delta_coverage_complete",
        lane_rows > 0 and sizing_rows >= lane_rows,
        f"lane sizing rows {sizing_rows}/{lane_rows}",
        severity="warning",
    )
    add_check(
        "lane_sizing_deltas_observed",
        sizing_diff_rows > 0 or sizing_would_adjust_rows > 0,
        "no lane-sizing deltas observed in this sample",
        severity="warning",
    )

    ready = not blockers and not warnings
    status = "ready" if ready else "blocked" if blockers else "needs_review"
    return {
        "schema_version": 1,
        "basis": "strategy_lane_rollout_readiness",
        "target": "beta_shadow_evidence_before_enforce",
        "status": status,
        "ready_for_enforce": ready,
        "policy": policy,
        "coverage": {
            "rows_scanned": rows_scanned,
            "hidden_gem_evidence_cards": {
                "rows_scanned": hidden_rows,
                "card_rows": card_rows,
                "clean_card_rows": clean_card_rows,
                "insufficient_data_rows": insufficient_card_rows,
                "no_card_rows": _coerce_int(hidden_summary.get("no_card_rows")),
                "coverage_pct": _coverage_pct(card_rows, hidden_rows),
            },
            "lane_delta": {
                "lane_rows": lane_rows,
                "beta_lane_gate_rows": beta_lane_gate_rows,
                "beta_lane_gate_missing_rows": _coerce_int(lane_summary.get("beta_lane_gate_missing_rows")),
                "delta_rows": lane_delta_rows,
                "coverage_pct": _coverage_pct(beta_lane_gate_rows, lane_rows),
            },
            "lane_sizing_delta": {
                "lane_rows": lane_rows,
                "lane_sizing_rows": sizing_rows,
                "configured_rows": _coerce_int(lane_summary.get("lane_sizing_configured_rows")),
                "would_adjust_rows": sizing_would_adjust_rows,
                "differs_from_final_rows": sizing_diff_rows,
                "shadow_rows": sizing_shadow_rows,
                "coverage_pct": _coverage_pct(sizing_rows, lane_rows),
            },
        },
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
    }


def format_strategy_lane_summary(summary: dict[str, Any] | None, *, max_lanes: int = 3) -> str | None:
    if not isinstance(summary, dict) or int(summary.get("rows_scanned") or 0) <= 0:
        return None

    parts = [
        f"Strategy lanes: rows {int(summary.get('lane_rows') or 0)}/{int(summary.get('rows_scanned') or 0)}",
        (
            f"final approved {int(summary.get('approved_lane_rows') or 0)} "
            f"rejected {int(summary.get('rejected_lane_rows') or 0)}"
        ),
        f"deltas {int(summary.get('lane_selection_delta_rows') or 0)}",
        (
            f"slow-profit selected {int(summary.get('selected_slow_profit_rows') or 0)} "
            f"would {int(summary.get('would_select_slow_profit_rows') or 0)} "
            f"diff {int(summary.get('slow_profit_differs_from_final_rows') or 0)}"
        ),
    ]
    selected = _format_counter(summary.get("selected_lane_counts"), max_lanes=max_lanes)
    if selected:
        parts.append(f"selected {selected}")
    would_select = _format_counter(summary.get("would_select_lane_counts"), max_lanes=max_lanes)
    if would_select:
        parts.append(f"would-select {would_select}")
    lane_sizing = _format_lane_sizing_summary(summary, max_lanes=max_lanes)
    if lane_sizing:
        parts.append(lane_sizing)
    return " | ".join(parts)


def format_strategy_lane_rollout_readiness(readiness: dict[str, Any] | None) -> str | None:
    if not isinstance(readiness, dict):
        return None

    policy = readiness.get("policy") if isinstance(readiness.get("policy"), dict) else {}
    coverage = readiness.get("coverage") if isinstance(readiness.get("coverage"), dict) else {}
    hidden = coverage.get("hidden_gem_evidence_cards") if isinstance(coverage.get("hidden_gem_evidence_cards"), dict) else {}
    lane_delta = coverage.get("lane_delta") if isinstance(coverage.get("lane_delta"), dict) else {}
    sizing_delta = coverage.get("lane_sizing_delta") if isinstance(coverage.get("lane_sizing_delta"), dict) else {}
    enabled_features = policy.get("enabled_features") if isinstance(policy.get("enabled_features"), list) else []
    features = ",".join(enabled_features) if enabled_features else "none"
    parts = [
        f"Strategy lane readiness: {readiness.get('status', 'unknown')}",
        f"policy {policy.get('version', 'stable')}/{policy.get('mode', 'off')} features={features}",
        (
            f"cards {_coerce_int(hidden.get('card_rows'))}/{_coerce_int(hidden.get('rows_scanned'))} "
            f"clean {_coerce_int(hidden.get('clean_card_rows'))}"
        ),
        (
            f"lane-delta {_coerce_int(lane_delta.get('beta_lane_gate_rows'))}/"
            f"{_coerce_int(lane_delta.get('lane_rows'))} diff {_coerce_int(lane_delta.get('delta_rows'))}"
        ),
        (
            f"sizing-delta {_coerce_int(sizing_delta.get('lane_sizing_rows'))}/"
            f"{_coerce_int(sizing_delta.get('lane_rows'))} diff "
            f"{_coerce_int(sizing_delta.get('differs_from_final_rows'))}"
        ),
        (
            f"blockers {len(readiness.get('blockers') or [])} "
            f"warnings {len(readiness.get('warnings') or [])}"
        ),
    ]
    return " | ".join(parts)


def _format_counter(value: Any, *, max_lanes: int) -> str | None:
    if not isinstance(value, dict) or not value:
        return None
    rows = sorted(value.items(), key=lambda item: (-int(item[1] or 0), str(item[0])))[:max(0, int(max_lanes))]
    return " ".join(f"{lane} {int(count or 0)}" for lane, count in rows)


def _format_lane_sizing_summary(summary: dict[str, Any], *, max_lanes: int) -> str | None:
    sizing_rows = int(summary.get("lane_sizing_rows") or 0)
    if sizing_rows <= 0:
        return None

    parts = [
        (
            f"sizing configured {int(summary.get('lane_sizing_configured_rows') or 0)}/{sizing_rows} "
            f"would-adjust {int(summary.get('lane_sizing_would_adjust_rows') or 0)} "
            f"applied {int(summary.get('lane_sizing_applied_rows') or 0)} "
            f"preserved {int(summary.get('lane_sizing_preserved_rows') or 0)} "
            f"shadow {int(summary.get('lane_sizing_shadow_rows') or 0)}"
        )
    ]
    sizes = _format_size_totals(summary)
    if sizes:
        parts.append(sizes)
    selected = _format_counter(summary.get("lane_sizing_selected_lane_counts"), max_lanes=max_lanes)
    if selected:
        parts.append(f"sizing-selected {selected}")
    return " | ".join(parts)


def _format_size_totals(summary: dict[str, Any]) -> str | None:
    totals = summary.get("lane_sizing_size_totals")
    avgs = summary.get("lane_sizing_size_avgs")
    if not isinstance(totals, dict) or not isinstance(avgs, dict):
        return None

    labels = (
        ("requested", "req"),
        ("beta_adjusted", "beta"),
        ("applied", "applied"),
    )
    parts = []
    for key, label in labels:
        total = _coerce_finite_float(totals.get(key))
        avg = _coerce_finite_float(avgs.get(key))
        if total is None or avg is None:
            continue
        parts.append(f"{label} {total:.2f} avg {avg:.2f}")
    return "sizes " + " ".join(parts) if parts else None


def _record_lane_sizing(
    summary: dict[str, Any],
    selected_counts: Counter[str],
    size_counts: Counter[str],
    size_totals: Counter[str],
    lane_sizing: dict[str, Any],
    *,
    fallback_lane_id: str | None = None,
) -> None:
    summary["lane_sizing_rows"] += 1
    summary["lane_sizing_configured_rows"] += int(bool(lane_sizing.get("configured")))
    summary["lane_sizing_would_adjust_rows"] += int(_lane_sizing_would_adjust(lane_sizing))
    summary["lane_sizing_applied_rows"] += int(bool(lane_sizing.get("applied")))
    summary["lane_sizing_preserved_rows"] += int(bool(lane_sizing.get("preserved_stable_size")))
    summary["lane_sizing_shadow_rows"] += int(bool(lane_sizing.get("shadow")))
    summary["lane_sizing_differs_from_final_rows"] += int(bool(lane_sizing.get("differs_from_final")))

    selected_counts[_clean_label(lane_sizing.get("lane_id") or fallback_lane_id)] += 1
    for key, value in (
        ("requested", _first_numeric_lane_sizing_value(lane_sizing, "final_requested_size_before_lane_caps", "requested_size")),
        ("beta_adjusted", _first_numeric_lane_sizing_value(lane_sizing, "beta_adjusted_size", "metadata_adjusted_size")),
        ("applied", _first_numeric_lane_sizing_value(lane_sizing, "applied_size")),
    ):
        if value is None:
            continue
        size_counts[key] += 1
        size_totals[key] += value


def _lane_sizing_would_adjust(lane_sizing: dict[str, Any]) -> bool:
    if lane_sizing.get("would_adjust_size"):
        return True
    requested = _first_numeric_lane_sizing_value(lane_sizing, "final_requested_size_before_lane_caps", "requested_size")
    adjusted = _first_numeric_lane_sizing_value(lane_sizing, "beta_adjusted_size", "metadata_adjusted_size")
    return bool(requested is not None and adjusted is not None and adjusted < requested)


def _first_numeric_lane_sizing_value(lane_sizing: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _coerce_finite_float(lane_sizing.get(key))
        if value is not None:
            return value
    return None


def _coerce_finite_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number):
        return None
    return number


def _coerce_int(value: Any) -> int:
    try:
        number = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not isfinite(number):
        return 0
    return int(number)


def _coverage_pct(part: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(part / total * 100, 1)


def _normalize_policy_status(policy_status: dict[str, Any] | None) -> dict[str, Any]:
    raw = policy_status if isinstance(policy_status, dict) else {}
    features = raw.get("enabled_features") if isinstance(raw.get("enabled_features"), dict) else {}
    return {
        "version": _clean_label(raw.get("version") or "stable"),
        "mode": _clean_label(raw.get("mode") or "off"),
        "active": raw.get("active") is True,
        "shadow": raw.get("shadow") is True,
        "enforce": raw.get("enforce") is True,
        "enabled_features": sorted(name for name, enabled in features.items() if enabled is True),
    }


def _shared_core_decision(row: dict[str, Any]) -> dict[str, Any]:
    decision = row.get("shared_core_decision")
    if isinstance(decision, dict):
        return decision
    artifact = row.get("decision_artifact")
    if isinstance(artifact, dict) and isinstance(artifact.get("shared_core_decision"), dict):
        return artifact["shared_core_decision"]
    return {}


def _decision_artifact(row: dict[str, Any]) -> dict[str, Any]:
    artifact = row.get("decision_artifact")
    return artifact if isinstance(artifact, dict) else {}


def _final_reason_code(row: dict[str, Any]) -> str | None:
    artifact = _decision_artifact(row)
    shared_pipeline = row.get("shared_pipeline") if isinstance(row.get("shared_pipeline"), dict) else {}
    decision = _shared_core_decision(row)
    for value in (
        artifact.get("final_reason_code"),
        shared_pipeline.get("final_reason_code"),
        decision.get("reason_code"),
        row.get("final_reason_code"),
        row.get("decision_reason_code"),
        row.get("skip_reason_code"),
    ):
        cleaned = _clean_optional(value)
        if cleaned:
            return cleaned
    return None


def _final_action(row: dict[str, Any]) -> str | None:
    artifact = _decision_artifact(row)
    shared_pipeline = row.get("shared_pipeline") if isinstance(row.get("shared_pipeline"), dict) else {}
    for value in (
        artifact.get("final_action"),
        shared_pipeline.get("final_action"),
        row.get("final_action"),
        row.get("direction"),
    ):
        cleaned = _clean_optional(value)
        if cleaned:
            return cleaned.upper()
    return None


def _is_final_approved(row: dict[str, Any], *, final_reason_code: str | None) -> bool:
    decision = _shared_core_decision(row)
    if decision.get("approved") is True:
        return True
    action = _final_action(row)
    if action in {"BUY_YES", "BUY_NO"}:
        return True
    decision_type = _clean_optional(row.get("decision_type"))
    if decision_type in {"buy_yes", "buy_no"}:
        return True
    return final_reason_code == "approved"


def _is_final_rejected(row: dict[str, Any], *, final_reason_code: str | None) -> bool:
    decision = _shared_core_decision(row)
    if decision.get("approved") is False:
        return True
    if _final_action(row) == "SKIP":
        return True
    if _clean_optional(row.get("decision_type")) == "skip":
        return True
    if _clean_optional(row.get("status")) in {"rejected", "failed", "skip"}:
        return True
    return False


def _clean_label(value: Any) -> str:
    cleaned = _clean_optional(value)
    return cleaned if cleaned else "unknown"


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None
