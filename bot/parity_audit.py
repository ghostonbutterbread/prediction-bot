"""Helpers for normalizing paper/live trade audit rows into one comparable parity view."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bot.config import get_operating_mode_label, get_parity_comparison_mode, get_runtime_mode
from bot.trade_audit import (
    EXECUTION_AUDIT_SCHEMA_NAME,
    EXECUTION_AUDIT_SCHEMA_VERSION,
    VALID_EXECUTION_SNAPSHOT_SOURCES,
    apply_execution_audit_contract,
    canonical_execution_snapshot_source,
    coerce_float,
    validate_execution_audit_row,
)


REQUIRED_EXECUTION_AUDIT_FIELDS = (
    "schema_name",
    "schema_version",
    "timestamp",
    "trade_id",
    "market_id",
    "event_key",
    "exchange",
    "direction",
    "status",
    "lifecycle_state",
    "requested_size",
    "approved_size",
    "placed_size",
    "filled_size",
    "remaining_size",
    "reserved_capital",
    "market_price",
    "entry_price",
    "decision_reason_code",
    "parity_mode_enabled",
    "execution_revalidated",
    "execution_snapshot_source",
)

SNAPSHOT_SOURCE_ORDER = ("book", "fallback", "missing", "unknown")
DRIFT_CATEGORY_ORDER = ("logic_drift", "risk_drift", "execution_drift", "lifecycle_drift")
RISK_FAILURE_STAGES = {"risk_block", "risk_check"}
LIFECYCLE_CONTRACT_ISSUES = {
    "rejected_with_fill",
    "failed_with_active_size",
    "placed_without_size",
    "placed_with_fill",
    "placed_remaining_mismatch",
    "filled_with_remaining",
    "partial_without_split_fill",
    "partial_size_sum_mismatch",
    "resolved_flag_status_mismatch",
    "resolved_without_flag",
}
COMPARISON_FIELDS = (
    "status",
    "lifecycle_state",
    "failure_stage",
    "decision_reason_code",
    "execution_revalidation_outcome",
    "execution_snapshot_source",
    "contract_valid",
)


def _has_required_value(row: dict[str, Any], field: str) -> bool:
    if field not in row:
        return False
    value = row.get(field)
    if value is None:
        return field in {"market_price", "entry_price"}
    if isinstance(value, str):
        return bool(value.strip())
    return True


def find_schema_gaps(row: dict[str, Any]) -> list[str]:
    """Report raw-row schema gaps before canonical defaults hide them."""
    gaps = [f"missing_{field}" for field in REQUIRED_EXECUTION_AUDIT_FIELDS if not _has_required_value(row, field)]
    if row.get("schema_name") not in (None, EXECUTION_AUDIT_SCHEMA_NAME):
        gaps.append("invalid_schema_name")
    if row.get("schema_version") not in (None, EXECUTION_AUDIT_SCHEMA_VERSION):
        gaps.append("invalid_schema_version")
    snapshot_source = row.get("execution_snapshot_source")
    if snapshot_source is not None and canonical_execution_snapshot_source(snapshot_source) != str(snapshot_source).strip().lower():
        gaps.append("invalid_execution_snapshot_source")
    return sorted(set(gaps))


def find_lifecycle_contradictions(row: dict[str, Any]) -> list[str]:
    status = str(row.get("status") or "").strip().lower()
    lifecycle_state = str(row.get("lifecycle_state") or "").strip().lower()
    if not status or not lifecycle_state:
        return []

    filled_size = coerce_float(row.get("filled_size"), default=0.0) or 0.0
    contradictions: list[str] = []
    expected: str | None = None
    if status == "candidate":
        expected = "candidate"
    elif status == "approved":
        expected = "approved"
    elif status == "placed":
        expected = "placed_open"
    elif status == "partial":
        expected = "partial_open"
    elif status == "filled":
        expected = "filled_open"
    elif status == "canceled":
        expected = "canceled_partial" if filled_size > 0 else "canceled_unfilled"
    elif status == "stale":
        expected = "stale_open_order"
    elif status == "resolved":
        expected = "resolved_position"

    if expected and lifecycle_state != expected:
        contradictions.append(f"{status}_lifecycle_mismatch")
    if status == "rejected" and not lifecycle_state.endswith("_rejected"):
        contradictions.append("rejected_lifecycle_mismatch")
    if status == "failed" and not (lifecycle_state.endswith("_failed") or lifecycle_state == "placement_failed"):
        contradictions.append("failed_lifecycle_mismatch")
    return sorted(set(contradictions))


def _snapshot_price(snapshot: Any) -> float | None:
    if not isinstance(snapshot, dict):
        return None
    return coerce_float(snapshot.get("market_price"), default=None)


def normalize_parity_trade_row(row: dict[str, Any], *, source: str) -> dict[str, Any]:
    schema_gaps = find_schema_gaps(row)
    decision_trace = dict(row.get("decision_trace") or {})
    parity_mode = dict(decision_trace.get("parity_mode") or {})
    canonical_row = apply_execution_audit_contract(dict(row))
    execution_snapshot = canonical_row.get("execution_snapshot") or parity_mode.get("execution_snapshot")
    original_signal_snapshot = canonical_row.get("original_signal_snapshot") or parity_mode.get("original_signal_snapshot")

    normalized = {
        "source": source,
        "schema_name": canonical_row.get("schema_name"),
        "schema_version": canonical_row.get("schema_version"),
        "timestamp": canonical_row.get("timestamp"),
        "trade_id": canonical_row.get("trade_id"),
        "market_id": canonical_row.get("market_id"),
        "question": canonical_row.get("question"),
        "direction": canonical_row.get("direction"),
        "status": canonical_row.get("status"),
        "lifecycle_state": canonical_row.get("lifecycle_state"),
        "failure_stage": canonical_row.get("failure_stage"),
        "decision_reason": canonical_row.get("decision_reason"),
        "decision_reason_code": canonical_row.get("decision_reason_code"),
        "requested_size": canonical_row.get("requested_size"),
        "approved_size": canonical_row.get("approved_size"),
        "placed_size": canonical_row.get("placed_size"),
        "filled_size": canonical_row.get("filled_size"),
        "remaining_size": canonical_row.get("remaining_size"),
        "entry_price": coerce_float(canonical_row.get("entry_price"), default=None),
        "fill_price": coerce_float(canonical_row.get("fill_price"), default=None),
        "market_price": coerce_float(canonical_row.get("market_price"), default=None),
        "estimated_fill_price": coerce_float(
            canonical_row.get("estimated_fill_price"),
            default=(execution_snapshot or {}).get("estimated_fill_price") if execution_snapshot else None,
        ),
        "slippage_estimate": coerce_float(canonical_row.get("slippage_estimate"), default=None),
        "exchange": canonical_row.get("exchange"),
        "model_probability": coerce_float(canonical_row.get("model_probability"), default=None),
        "edge": coerce_float(canonical_row.get("edge"), default=None),
        "confidence": coerce_float(canonical_row.get("confidence"), default=None),
        "resolved": bool(canonical_row.get("resolved")),
        "outcome": canonical_row.get("outcome"),
        "pnl": coerce_float(canonical_row.get("pnl"), default=None),
        "reserved_capital": coerce_float(canonical_row.get("reserved_capital"), default=0.0),
        "available_cash_before": coerce_float(canonical_row.get("available_cash_before"), default=None),
        "available_cash_after_entry": coerce_float(canonical_row.get("available_cash_after_entry"), default=None),
        "event_key": canonical_row.get("event_key"),
        "parity_mode_enabled": bool(row.get("parity_mode_enabled", parity_mode.get("enabled", False))),
        "execution_revalidated": bool(row.get("execution_revalidated", parity_mode.get("execution_revalidated", False))),
        "execution_revalidation_outcome": row.get("execution_revalidation_outcome")
        or canonical_row.get("execution_revalidation_outcome")
        or parity_mode.get("execution_revalidation_outcome"),
        "execution_snapshot_source": row.get("execution_snapshot_source")
        or canonical_row.get("execution_snapshot_source")
        or parity_mode.get("execution_snapshot_source"),
        "original_decision_reason_code": canonical_row.get("original_decision_reason_code")
        or parity_mode.get("original_decision_reason_code"),
        "execution_decision_reason_code": canonical_row.get("execution_decision_reason_code")
        or parity_mode.get("execution_decision_reason_code"),
        "original_signal_snapshot": original_signal_snapshot,
        "execution_snapshot": execution_snapshot,
    }
    normalized["schema_gaps"] = schema_gaps
    normalized["schema_gap_count"] = len(schema_gaps)
    normalized["contract_issues"] = validate_execution_audit_row(canonical_row)
    normalized["contract_issue_count"] = len(normalized["contract_issues"])
    normalized["contract_valid"] = not normalized["contract_issues"]
    normalized["lifecycle_contradictions"] = find_lifecycle_contradictions(canonical_row)
    normalized["lifecycle_contradiction_count"] = len(normalized["lifecycle_contradictions"])
    normalized["decision_reason_delta"] = bool(
        normalized.get("original_decision_reason_code")
        and normalized.get("execution_decision_reason_code")
        and normalized.get("original_decision_reason_code") != normalized.get("execution_decision_reason_code")
    )
    original_market_price = _snapshot_price(normalized.get("original_signal_snapshot"))
    execution_market_price = _snapshot_price(normalized.get("execution_snapshot"))
    normalized["original_market_price"] = original_market_price
    normalized["execution_market_price"] = execution_market_price
    normalized["execution_market_price_delta"] = (
        round(execution_market_price - original_market_price, 6)
        if original_market_price is not None and execution_market_price is not None
        else None
    )
    normalized["execution_price_delta"] = bool(
        original_market_price is not None
        and execution_market_price is not None
        and original_market_price != execution_market_price
    )
    normalized["is_parity_candidate"] = bool(
        normalized["parity_mode_enabled"]
        or normalized["execution_revalidated"]
        or normalized["execution_snapshot"]
        or normalized["original_signal_snapshot"]
    )
    return normalized


def load_latest_paper_session_rows(data_dir: str | Path) -> list[dict[str, Any]]:
    data_path = Path(data_dir)
    session_files = sorted(data_path.glob("paper/sim_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not session_files:
        session_files = sorted(data_path.glob("sim_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not session_files:
        return []
    payload = json.loads(session_files[0].read_text())
    return list(payload.get("trades") or [])


def _load_live_trade_rows(data_path: Path) -> list[dict[str, Any]]:
    trade_files = [data_path / "live" / "trades.jsonl", data_path / "trades.jsonl"]
    for trade_file in trade_files:
        if not trade_file.exists():
            continue
        rows: list[dict[str, Any]] = []
        for line in trade_file.read_text().splitlines():
            if not line.strip():
                continue
            rows.append(json.loads(line))
        return rows
    return []


def _load_live_risk_block_rows(data_path: Path) -> list[dict[str, Any]]:
    risk_files = [data_path / "live" / "risk_blocks.jsonl", data_path / "risk_blocks.jsonl"]
    for risk_blocks in risk_files:
        if not risk_blocks.exists():
            continue
        rows: list[dict[str, Any]] = []
        for line in risk_blocks.read_text().splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("schema_name") == "execution_audit_row":
                rows.append(obj)
                continue
            rows.append(
                {
                    "timestamp": obj.get("timestamp"),
                    "trade_id": obj.get("trade_id") or f"risk-block:{obj.get('timestamp')}:{obj.get('market_id')}",
                    "market_id": obj.get("market_id"),
                    "question": obj.get("question"),
                    "direction": obj.get("direction"),
                    "exchange": obj.get("exchange"),
                    "status": "rejected",
                    "lifecycle_state": "risk_check_rejected",
                    "failure_stage": "risk_block",
                    "decision_reason": obj.get("decision_reason"),
                    "decision_reason_code": obj.get("decision_reason_code") or obj.get("blocked_reason"),
                    "requested_size": 0.0,
                    "approved_size": 0.0,
                    "placed_size": 0.0,
                    "filled_size": 0.0,
                    "remaining_size": 0.0,
                }
            )
        return rows
    return []


def load_live_rows(data_dir: str | Path) -> list[dict[str, Any]]:
    data_path = Path(data_dir)
    rows: list[dict[str, Any]] = []
    rows.extend(_load_live_trade_rows(data_path))
    rows.extend(_load_live_risk_block_rows(data_path))
    return rows


def build_comparison_context(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    parity_mode = config.get("parity_mode", {}) or {}
    parity_enabled = bool(parity_mode.get("enabled", False))
    comparison_mode = get_parity_comparison_mode(config)
    runtime_mode = get_runtime_mode(config)
    live_mode_label = get_operating_mode_label(config)
    live_risk_preset_mode = "paper" if runtime_mode == "live" and comparison_mode == "identical_risk" else ("live" if runtime_mode == "live" else "paper")
    paper_config = {**config, "trading": {**(config.get("trading", {}) or {}), "mode": "paper"}}
    paper_mode_label = get_operating_mode_label(paper_config)
    return {
        "parity_mode_enabled": parity_enabled,
        "parity_comparison_mode": comparison_mode,
        "runtime_mode": runtime_mode,
        "paper_mode_label": paper_mode_label,
        "live_mode_label": live_mode_label,
        "paper_risk_preset_mode": "paper",
        "live_risk_preset_mode": live_risk_preset_mode,
        "apples_to_apples": live_risk_preset_mode == "paper",
        "differences_expected": live_risk_preset_mode != "paper",
    }


def build_parity_view(data_dir: str | Path, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    data_path = Path(data_dir)
    paper_rows = [normalize_parity_trade_row(row, source="paper") for row in load_latest_paper_session_rows(data_dir)]
    live_rows = [normalize_parity_trade_row(row, source="live") for row in load_live_rows(data_dir)]
    comparison_context = build_comparison_context(config)
    return {
        "paper_rows": paper_rows,
        "live_rows": live_rows,
        "paper_summary": summarize_normalized_rows(paper_rows),
        "live_summary": summarize_normalized_rows(live_rows),
        "comparison_context": comparison_context,
        "comparison": build_paper_live_comparison(paper_rows, live_rows),
        "comparison_artifact_path": str(default_comparison_artifact_path(data_path)),
    }


def summarize_normalized_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    lifecycle_state_counts: dict[str, int] = {}
    failure_stage_counts: dict[str, int] = {}
    execution_outcome_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    execution_reason_counts: dict[str, int] = {}
    resolved_outcome_counts: dict[str, int] = {}
    snapshot_source_counts: dict[str, int] = {}
    schema_gap_counts: dict[str, int] = {}
    lifecycle_contradiction_counts: dict[str, int] = {}
    contract_issue_counts: dict[str, int] = {}
    schema_gap_examples: list[dict[str, Any]] = []
    lifecycle_contradiction_examples: list[dict[str, Any]] = []
    invalid_contract_examples: list[dict[str, Any]] = []
    decision_delta_examples: list[dict[str, Any]] = []
    price_delta_examples: list[dict[str, Any]] = []
    decision_delta_pair_counts: dict[str, int] = {}
    parity_candidates = 0
    parity_enabled_rows = 0
    execution_revalidated_rows = 0
    execution_rejected_rows = 0
    fallback_rows = 0
    missing_snapshot_rows = 0
    unknown_snapshot_rows = 0
    schema_gap_rows = 0
    lifecycle_contradiction_rows = 0
    invalid_contract_rows = 0
    decision_delta_rows = 0
    execution_price_delta_rows = 0
    for row in rows:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        lifecycle_state = str(row.get("lifecycle_state") or "unknown")
        lifecycle_state_counts[lifecycle_state] = lifecycle_state_counts.get(lifecycle_state, 0) + 1
        failure_stage = str(row.get("failure_stage") or "")
        if failure_stage:
            failure_stage_counts[failure_stage] = failure_stage_counts.get(failure_stage, 0) + 1
        execution_outcome = str(row.get("execution_revalidation_outcome") or "")
        if execution_outcome:
            execution_outcome_counts[execution_outcome] = execution_outcome_counts.get(execution_outcome, 0) + 1
        reason = str(row.get("decision_reason_code") or "")
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        execution_reason = str(row.get("execution_decision_reason_code") or "")
        if execution_reason:
            execution_reason_counts[execution_reason] = execution_reason_counts.get(execution_reason, 0) + 1
        resolved_outcome = str(row.get("outcome") or "").upper()
        if resolved_outcome in {"YES", "NO"}:
            resolved_outcome_counts[resolved_outcome] = resolved_outcome_counts.get(resolved_outcome, 0) + 1
        snapshot_source = str(row.get("execution_snapshot_source") or "unknown")
        snapshot_source_counts[snapshot_source] = snapshot_source_counts.get(snapshot_source, 0) + 1
        if snapshot_source == "unknown":
            unknown_snapshot_rows += 1
        if row.get("is_parity_candidate"):
            parity_candidates += 1
        if row.get("parity_mode_enabled"):
            parity_enabled_rows += 1
        if row.get("execution_revalidated"):
            execution_revalidated_rows += 1
        if row.get("execution_revalidation_outcome") == "rejected":
            execution_rejected_rows += 1
        if row.get("execution_snapshot_source") == "fallback":
            fallback_rows += 1
        if row.get("execution_snapshot_source") == "missing":
            missing_snapshot_rows += 1
        original_reason = row.get("original_decision_reason_code")
        execution_reason_value = row.get("execution_decision_reason_code")
        if row.get("decision_reason_delta") or (
            original_reason and execution_reason_value and original_reason != execution_reason_value
        ):
            decision_delta_rows += 1
            pair_key = f"{original_reason or 'missing'} -> {execution_reason_value or 'missing'}"
            decision_delta_pair_counts[pair_key] = decision_delta_pair_counts.get(pair_key, 0) + 1
            if len(decision_delta_examples) < 10:
                decision_delta_examples.append(
                    {
                        "trade_id": row.get("trade_id"),
                        "market_id": row.get("market_id"),
                        "original_decision_reason_code": original_reason,
                        "execution_decision_reason_code": execution_reason_value,
                        "execution_revalidation_outcome": row.get("execution_revalidation_outcome"),
                    }
                )
        if row.get("execution_price_delta"):
            execution_price_delta_rows += 1
            if len(price_delta_examples) < 10:
                price_delta_examples.append(
                    {
                        "trade_id": row.get("trade_id"),
                        "market_id": row.get("market_id"),
                        "original_market_price": row.get("original_market_price"),
                        "execution_market_price": row.get("execution_market_price"),
                        "execution_market_price_delta": row.get("execution_market_price_delta"),
                    }
                )
        else:
            original_market_price = _snapshot_price(row.get("original_signal_snapshot"))
            execution_market_price = _snapshot_price(row.get("execution_snapshot"))
            if original_market_price is not None and execution_market_price is not None and original_market_price != execution_market_price:
                execution_price_delta_rows += 1
        for gap in row.get("schema_gaps", []) or []:
            schema_gap_counts[gap] = schema_gap_counts.get(gap, 0) + 1
        if row.get("schema_gaps"):
            schema_gap_rows += 1
            if len(schema_gap_examples) < 10:
                schema_gap_examples.append(
                    {
                        "source": row.get("source"),
                        "trade_id": row.get("trade_id"),
                        "market_id": row.get("market_id"),
                        "gaps": list(row.get("schema_gaps") or []),
                    }
                )
        for contradiction in row.get("lifecycle_contradictions", []) or []:
            lifecycle_contradiction_counts[contradiction] = lifecycle_contradiction_counts.get(contradiction, 0) + 1
        if row.get("lifecycle_contradictions"):
            lifecycle_contradiction_rows += 1
            if len(lifecycle_contradiction_examples) < 10:
                lifecycle_contradiction_examples.append(
                    {
                        "source": row.get("source"),
                        "trade_id": row.get("trade_id"),
                        "market_id": row.get("market_id"),
                        "status": row.get("status"),
                        "lifecycle_state": row.get("lifecycle_state"),
                        "contradictions": list(row.get("lifecycle_contradictions") or []),
                    }
                )
        for issue in row.get("contract_issues", []) or []:
            contract_issue_counts[issue] = contract_issue_counts.get(issue, 0) + 1
        if row.get("contract_issues"):
            invalid_contract_rows += 1
            if len(invalid_contract_examples) < 10:
                invalid_contract_examples.append(
                    {
                        "trade_id": row.get("trade_id"),
                        "market_id": row.get("market_id"),
                        "status": row.get("status"),
                        "issues": list(row.get("contract_issues") or []),
                    }
                )
    top_reasons = sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    top_execution_reasons = sorted(execution_reason_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    top_schema_gaps = sorted(schema_gap_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    top_lifecycle_contradictions = sorted(
        lifecycle_contradiction_counts.items(),
        key=lambda item: (-item[1], item[0]),
    )[:10]
    top_contract_issues = sorted(contract_issue_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    top_decision_delta_pairs = sorted(decision_delta_pair_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    return {
        "total_rows": len(rows),
        "status_counts": status_counts,
        "lifecycle_state_counts": lifecycle_state_counts,
        "failure_stage_counts": failure_stage_counts,
        "execution_revalidation_outcome_counts": execution_outcome_counts,
        "top_reason_codes": top_reasons,
        "top_execution_reason_codes": top_execution_reasons,
        "resolved_outcome_counts": resolved_outcome_counts,
        "snapshot_source_counts": {
            source: snapshot_source_counts.get(source, 0)
            for source in SNAPSHOT_SOURCE_ORDER
            if source in VALID_EXECUTION_SNAPSHOT_SOURCES
        },
        "parity_candidates": parity_candidates,
        "parity_enabled_rows": parity_enabled_rows,
        "execution_revalidated_rows": execution_revalidated_rows,
        "execution_rejected_rows": execution_rejected_rows,
        "fallback_rows": fallback_rows,
        "missing_snapshot_rows": missing_snapshot_rows,
        "unknown_snapshot_rows": unknown_snapshot_rows,
        "schema_gap_rows": schema_gap_rows,
        "top_schema_gaps": top_schema_gaps,
        "schema_gap_examples": schema_gap_examples,
        "lifecycle_contradiction_rows": lifecycle_contradiction_rows,
        "top_lifecycle_contradictions": top_lifecycle_contradictions,
        "lifecycle_contradiction_examples": lifecycle_contradiction_examples,
        "invalid_contract_rows": invalid_contract_rows,
        "decision_delta_rows": decision_delta_rows,
        "top_decision_delta_pairs": top_decision_delta_pairs,
        "decision_delta_examples": decision_delta_examples,
        "execution_price_delta_rows": execution_price_delta_rows,
        "price_delta_examples": price_delta_examples,
        "top_contract_issues": top_contract_issues,
        "invalid_contract_examples": invalid_contract_examples,
    }


def _comparison_key(row: dict[str, Any]) -> str:
    direction = str(row.get("direction") or "unknown")
    if row.get("event_key"):
        return f"event:{row.get('event_key')}:{direction}"
    if row.get("market_id"):
        return f"market:{row.get('market_id')}:{direction}"
    return f"trade:{row.get('trade_id') or 'unknown'}"


def _group_by_comparison_key(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(_comparison_key(row), []).append(row)
    for group_rows in grouped.values():
        group_rows.sort(key=lambda item: str(item.get("timestamp") or ""))
    return grouped


def _has_execution_snapshot_delta(row: dict[str, Any]) -> bool:
    if row.get("execution_price_delta"):
        return True
    original_market_price = row.get("original_market_price")
    execution_market_price = row.get("execution_market_price")
    return original_market_price is not None and execution_market_price is not None and original_market_price != execution_market_price


def _has_lifecycle_contract_issue(row: dict[str, Any]) -> bool:
    for issue in row.get("contract_issues", []) or []:
        if issue in LIFECYCLE_CONTRACT_ISSUES or str(issue).endswith("_lifecycle_mismatch"):
            return True
    return False


def _has_risk_failure_stage(row: dict[str, Any]) -> bool:
    return str(row.get("failure_stage") or "").strip().lower() in RISK_FAILURE_STAGES


def _classify_drift_categories(
    paper_row: dict[str, Any],
    live_row: dict[str, Any],
    field_deltas: dict[str, dict[str, Any]],
) -> list[str]:
    categories: list[str] = []
    delta_fields = set(field_deltas)

    if delta_fields & {"status", "lifecycle_state", "failure_stage"}:
        categories.append("lifecycle_drift")
    elif paper_row.get("lifecycle_contradictions") or live_row.get("lifecycle_contradictions"):
        categories.append("lifecycle_drift")
    elif _has_lifecycle_contract_issue(paper_row) or _has_lifecycle_contract_issue(live_row):
        categories.append("lifecycle_drift")

    if "execution_snapshot_source" in delta_fields or _has_execution_snapshot_delta(paper_row) or _has_execution_snapshot_delta(live_row):
        categories.append("execution_drift")

    if delta_fields & {"decision_reason_code", "execution_revalidation_outcome"}:
        if _has_risk_failure_stage(paper_row) or _has_risk_failure_stage(live_row):
            categories.append("risk_drift")
        else:
            categories.append("logic_drift")

    if not categories:
        categories.append("logic_drift")
    return categories


def build_paper_live_comparison(paper_rows: list[dict[str, Any]], live_rows: list[dict[str, Any]]) -> dict[str, Any]:
    paper_by_key = _group_by_comparison_key(paper_rows)
    live_by_key = _group_by_comparison_key(live_rows)
    all_keys = sorted(set(paper_by_key) | set(live_by_key))
    mismatch_field_counts: dict[str, int] = {}
    drift_category_counts: dict[str, int] = {}
    mismatch_examples: list[dict[str, Any]] = []
    matched_keys = 0
    matched_pairs = 0
    mismatched_pair_count = 0
    paper_only_row_count = 0
    live_only_row_count = 0

    for key in all_keys:
        paper_group = paper_by_key.get(key, [])
        live_group = live_by_key.get(key, [])
        if not paper_group:
            live_only_row_count += len(live_group)
            continue
        if not live_group:
            paper_only_row_count += len(paper_group)
            continue
        matched_keys += 1
        for index, (paper_row, live_row) in enumerate(zip(paper_group, live_group)):
            matched_pairs += 1
            field_deltas = {
                field: {"paper": paper_row.get(field), "live": live_row.get(field)}
                for field in COMPARISON_FIELDS
                if paper_row.get(field) != live_row.get(field)
            }
            if not field_deltas:
                continue
            mismatched_pair_count += 1
            for field in field_deltas:
                mismatch_field_counts[field] = mismatch_field_counts.get(field, 0) + 1
            drift_categories = _classify_drift_categories(paper_row, live_row, field_deltas)
            for category in drift_categories:
                drift_category_counts[category] = drift_category_counts.get(category, 0) + 1
            if len(mismatch_examples) < 20:
                mismatch_examples.append(
                    {
                        "comparison_key": key,
                        "pair_index": index,
                        "paper_trade_id": paper_row.get("trade_id"),
                        "live_trade_id": live_row.get("trade_id"),
                        "drift_categories": drift_categories,
                        "field_deltas": field_deltas,
                    }
                )

        if len(paper_group) > len(live_group):
            paper_only_row_count += len(paper_group) - len(live_group)
        elif len(live_group) > len(paper_group):
            live_only_row_count += len(live_group) - len(paper_group)

    return {
        "paper_rows": len(paper_rows),
        "live_rows": len(live_rows),
        "matched_keys": matched_keys,
        "matched_pairs": matched_pairs,
        "paper_only_keys": [key for key in all_keys if key in paper_by_key and key not in live_by_key][:20],
        "live_only_keys": [key for key in all_keys if key in live_by_key and key not in paper_by_key][:20],
        "paper_only_row_count": paper_only_row_count,
        "live_only_row_count": live_only_row_count,
        "mismatched_pair_count": mismatched_pair_count,
        "drift_category_counts": [
            (category, drift_category_counts.get(category, 0))
            for category in DRIFT_CATEGORY_ORDER
            if drift_category_counts.get(category, 0)
        ],
        "mismatch_field_counts": sorted(mismatch_field_counts.items(), key=lambda item: (-item[1], item[0])),
        "mismatch_examples": mismatch_examples,
    }


def default_comparison_artifact_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "parity_comparison.json"


def write_parity_comparison_artifact(
    data_dir: str | Path,
    output_path: str | Path | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> Path:
    path = Path(output_path) if output_path is not None else default_comparison_artifact_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_parity_view(data_dir, config=config), indent=2, sort_keys=True))
    return path
