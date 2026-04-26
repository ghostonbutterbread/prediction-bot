"""Helpers for normalizing paper/live trade audit rows into one comparable parity view."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bot.trade_audit import apply_execution_audit_contract, coerce_float, validate_execution_audit_row


def normalize_parity_trade_row(row: dict[str, Any], *, source: str) -> dict[str, Any]:
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
    normalized["contract_issues"] = validate_execution_audit_row(canonical_row)
    normalized["contract_issue_count"] = len(normalized["contract_issues"])
    normalized["contract_valid"] = not normalized["contract_issues"]
    normalized["decision_reason_delta"] = bool(
        normalized.get("original_decision_reason_code")
        and normalized.get("execution_decision_reason_code")
        and normalized.get("original_decision_reason_code") != normalized.get("execution_decision_reason_code")
    )
    original_snapshot = normalized.get("original_signal_snapshot") or {}
    execution_snapshot_payload = normalized.get("execution_snapshot") or {}
    original_market_price = coerce_float((original_snapshot or {}).get("market_price"), default=None)
    execution_market_price = coerce_float((execution_snapshot_payload or {}).get("market_price"), default=None)
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


def build_parity_view(data_dir: str | Path) -> dict[str, Any]:
    paper_rows = [normalize_parity_trade_row(row, source="paper") for row in load_latest_paper_session_rows(data_dir)]
    live_rows = [normalize_parity_trade_row(row, source="live") for row in load_live_rows(data_dir)]
    return {
        "paper_rows": paper_rows,
        "live_rows": live_rows,
        "paper_summary": summarize_normalized_rows(paper_rows),
        "live_summary": summarize_normalized_rows(live_rows),
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
    contract_issue_counts: dict[str, int] = {}
    invalid_contract_examples: list[dict[str, Any]] = []
    parity_candidates = 0
    parity_enabled_rows = 0
    execution_revalidated_rows = 0
    execution_rejected_rows = 0
    fallback_rows = 0
    missing_snapshot_rows = 0
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
        if row.get("execution_snapshot_source") in (None, "missing"):
            missing_snapshot_rows += 1
        original_reason = row.get("original_decision_reason_code")
        execution_reason_value = row.get("execution_decision_reason_code")
        if row.get("decision_reason_delta") or (
            original_reason and execution_reason_value and original_reason != execution_reason_value
        ):
            decision_delta_rows += 1
        if row.get("execution_price_delta"):
            execution_price_delta_rows += 1
        else:
            original_snapshot = row.get("original_signal_snapshot") or {}
            execution_snapshot = row.get("execution_snapshot") or {}
            original_market_price = coerce_float((original_snapshot or {}).get("market_price"), default=None)
            execution_market_price = coerce_float((execution_snapshot or {}).get("market_price"), default=None)
            if original_market_price is not None and execution_market_price is not None and original_market_price != execution_market_price:
                execution_price_delta_rows += 1
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
    top_contract_issues = sorted(contract_issue_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    return {
        "total_rows": len(rows),
        "status_counts": status_counts,
        "lifecycle_state_counts": lifecycle_state_counts,
        "failure_stage_counts": failure_stage_counts,
        "execution_revalidation_outcome_counts": execution_outcome_counts,
        "top_reason_codes": top_reasons,
        "top_execution_reason_codes": top_execution_reasons,
        "resolved_outcome_counts": resolved_outcome_counts,
        "snapshot_source_counts": snapshot_source_counts,
        "parity_candidates": parity_candidates,
        "parity_enabled_rows": parity_enabled_rows,
        "execution_revalidated_rows": execution_revalidated_rows,
        "execution_rejected_rows": execution_rejected_rows,
        "fallback_rows": fallback_rows,
        "missing_snapshot_rows": missing_snapshot_rows,
        "invalid_contract_rows": invalid_contract_rows,
        "decision_delta_rows": decision_delta_rows,
        "execution_price_delta_rows": execution_price_delta_rows,
        "top_contract_issues": top_contract_issues,
        "invalid_contract_examples": invalid_contract_examples,
    }
