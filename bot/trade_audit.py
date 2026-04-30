"""Trade accounting audit helpers shared by simulator, resolver, and reports."""

from __future__ import annotations

from collections import defaultdict
from math import isfinite
from typing import Optional

from bot.market_classification import is_weather_market


VALID_DIRECTIONS = {"BUY_YES", "BUY_NO"}
VALID_OUTCOMES = {"YES", "NO"}
EXECUTION_AUDIT_SCHEMA_NAME = "execution_audit_row"
EXECUTION_AUDIT_SCHEMA_VERSION = 1
VALID_EXECUTION_SNAPSHOT_SOURCES = {"book", "fallback", "missing", "unknown"}
VALID_EXECUTION_STATUSES = {"candidate", "rejected", "approved", "placed", "partial", "filled", "canceled", "stale", "failed", "resolved"}
VALID_EXECUTION_REVALIDATION_OUTCOMES = {"approved", "rejected", "fallback", "skipped", "missing"}


def signal_snapshot_source(signal: dict, direction: object = None) -> str:
    side = str(direction or signal.get("direction") or "").upper()
    if side == "BUY_NO":
        if signal.get("best_no_ask") is not None:
            return "book"
        if signal.get("no_price") is not None or signal.get("market_price") is not None:
            return "fallback"
        return "missing"

    if signal.get("best_yes_ask") is not None:
        return "book"
    if signal.get("yes_price") is not None or signal.get("market_price") is not None:
        return "fallback"
    return "missing"


def build_signal_snapshot(signal: dict, *, direction: object = None) -> dict:
    return {
        "market_id": signal.get("market_id"),
        "direction": direction or signal.get("direction"),
        "market_price": signal.get("market_price"),
        "yes_price": signal.get("yes_price"),
        "no_price": signal.get("no_price"),
        "best_yes_ask": signal.get("best_yes_ask"),
        "best_no_ask": signal.get("best_no_ask"),
        "source": signal_snapshot_source(signal, direction),
    }


def coerce_float(value, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if value is None:
            return default
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if isfinite(value) else default


def coerce_bool(value, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n", ""}:
        return False
    return default


def canonical_execution_snapshot_source(value) -> str:
    normalized = str(value or "unknown").strip().lower()
    return normalized if normalized in VALID_EXECUTION_SNAPSHOT_SOURCES else "unknown"


def canonical_execution_status(
    status: object,
    *,
    filled_size: Optional[float] = None,
    placed_size: Optional[float] = None,
    remaining_size: Optional[float] = None,
) -> str:
    filled = coerce_float(filled_size, default=0.0) or 0.0
    placed = coerce_float(placed_size, default=0.0) or 0.0
    remaining = coerce_float(remaining_size, default=0.0) or 0.0

    normalized = str(status or "").strip().lower()
    if normalized:
        aliases = {
            "open": "placed",
            "accepted": "placed",
            "submitted": "placed",
            "resting": "placed",
            "pending": "placed",
            "pending_confirmation": "placed",
            "partial_fill": "partial",
            "partially_filled": "partial",
            "partially-filled": "partial",
            "cancelled": "canceled",
            "voided": "canceled",
            "void": "canceled",
            "expired": "stale",
            "timed_out": "stale",
            "timed-out": "stale",
        }
        canonical = aliases.get(normalized, normalized)
        if canonical == "placed" and filled > 0 and remaining > 0:
            return "partial"
        if canonical == "placed" and filled > 0 and remaining <= 0:
            return "filled"
        if canonical == "partial" and filled > 0 and remaining <= 0:
            return "filled"
        if canonical == "partial" and filled <= 0 and remaining > 0:
            return "placed"
        return canonical

    if filled > 0 and remaining > 0:
        return "partial"
    if filled > 0:
        return "filled"
    if placed > 0:
        return "placed"
    return "candidate"


def canonical_lifecycle_state(
    status: object,
    *,
    failure_stage: object = None,
    filled_size: Optional[float] = None,
    remaining_size: Optional[float] = None,
) -> str:
    normalized_status = canonical_execution_status(
        status,
        filled_size=filled_size,
        remaining_size=remaining_size,
    )
    if normalized_status == "rejected":
        stage = str(failure_stage or "unknown").strip().lower() or "unknown"
        if stage == "risk_block":
            stage = "risk_check"
        return f"{stage}_rejected"
    if normalized_status == "partial":
        return "partial_open"
    if normalized_status == "filled":
        return "filled_open"
    if normalized_status == "placed":
        return "placed_open"
    if normalized_status == "canceled":
        return "canceled_partial" if (coerce_float(filled_size, default=0.0) or 0.0) > 0 else "canceled_unfilled"
    if normalized_status == "stale":
        return "stale_open_order"
    if normalized_status == "failed":
        stage = str(failure_stage or "placement").strip().lower() or "placement"
        return f"{stage}_failed"
    if normalized_status == "resolved":
        return "resolved_position"
    return normalized_status or "candidate"


def infer_reserved_capital(
    status: object,
    *,
    filled_size: Optional[float] = None,
    remaining_size: Optional[float] = None,
    current_value: Optional[float] = None,
) -> float:
    normalized_status = canonical_execution_status(
        status,
        filled_size=filled_size,
        remaining_size=remaining_size,
    )
    filled = max(0.0, float(coerce_float(filled_size, default=0.0) or 0.0))
    remaining = max(0.0, float(coerce_float(remaining_size, default=0.0) or 0.0))
    current = coerce_float(current_value, default=None)
    if normalized_status in {"rejected", "failed", "resolved", "candidate", "approved"}:
        return 0.0
    if normalized_status == "canceled":
        return filled
    if normalized_status == "filled":
        return filled
    if normalized_status == "partial":
        return filled + remaining
    if normalized_status in {"placed", "stale"}:
        return remaining
    if current is not None and current >= 0:
        return current
    return 0.0


def build_scan_candidate_summary(signal: dict, *, timestamp: Optional[str] = None, rank: Optional[int] = None) -> dict:
    timestamp = timestamp or ""
    snapshot = build_signal_snapshot(signal)
    row = {
        "timestamp": timestamp,
        "trade_id": f"candidate:{signal.get('market_id') or 'unknown'}:{rank or 0}",
        "market_id": signal.get("market_id", ""),
        "question": signal.get("question", ""),
        "direction": signal.get("direction", "BUY_YES"),
        "exchange": signal.get("exchange", "unknown"),
        "status": "candidate",
        "lifecycle_state": "candidate",
        "decision_reason_code": signal.get("decision_reason_code") or "candidate",
        "requested_size": 0.0,
        "approved_size": 0.0,
        "placed_size": 0.0,
        "filled_size": 0.0,
        "remaining_size": 0.0,
        "market_price": signal.get("market_price"),
        "entry_price": signal.get("market_price"),
        "fill_price": None,
        "model_probability": signal.get("model_probability"),
        "edge": signal.get("edge"),
        "confidence": signal.get("confidence"),
        "signals": signal.get("signals", {}),
        "market_group": signal.get("market_group"),
        "category": signal.get("category", ""),
        "event_key": trade_event_key(signal),
        "candidate_rank": rank,
        "original_signal_snapshot": snapshot,
        "execution_snapshot": snapshot,
        "execution_snapshot_source": snapshot.get("source"),
    }
    return apply_execution_audit_contract(row)


def build_risk_block_audit_row(
    signal: dict,
    *,
    decision=None,
    blocked_reason: Optional[str] = None,
    timestamp: Optional[str] = None,
    available_cash: Optional[float] = None,
) -> dict:
    timestamp = timestamp or ""
    decision_trace = dict(getattr(decision, "reasoning", {}) or {}) if decision is not None else {}
    parity_mode = dict((decision_trace or {}).get("parity_mode", {}) or {})
    direction = getattr(decision, "action", signal.get("direction", "BUY_YES"))
    snapshot = build_signal_snapshot(signal, direction=direction)
    row = {
        "timestamp": timestamp,
        "trade_id": f"risk-block:{signal.get('market_id') or 'unknown'}:{blocked_reason or 'unknown'}:{timestamp}",
        "market_id": signal.get("market_id", ""),
        "question": signal.get("question", ""),
        "direction": direction,
        "exchange": signal.get("exchange", "unknown"),
        "status": "rejected",
        "failure_stage": "risk_block",
        "decision_reason": getattr(decision, "reason", None) or blocked_reason,
        "decision_reason_code": getattr(decision, "reason_code", None) or blocked_reason or "unknown",
        "blocked_reason": blocked_reason or getattr(decision, "reason_code", None) or "unknown",
        "requested_size": float(getattr(decision, "requested_position_size", 0.0) or 0.0),
        "approved_size": float(getattr(decision, "position_size", 0.0) or 0.0),
        "placed_size": 0.0,
        "filled_size": 0.0,
        "remaining_size": 0.0,
        "reserved_capital": 0.0,
        "market_price": signal.get("market_price"),
        "entry_price": signal.get("market_price"),
        "fill_price": None,
        "model_probability": signal.get("model_probability"),
        "edge": signal.get("edge"),
        "confidence": signal.get("confidence"),
        "signals": signal.get("signals", {}),
        "decision_trace": decision_trace,
        "parity_mode_enabled": bool(parity_mode.get("enabled", False)),
        "execution_revalidated": bool(parity_mode.get("execution_revalidated", False)),
        "execution_revalidation_outcome": parity_mode.get("execution_revalidation_outcome"),
        "original_signal_snapshot": parity_mode.get("original_signal_snapshot") or snapshot,
        "execution_snapshot": parity_mode.get("execution_snapshot") or snapshot,
        "original_decision_reason_code": parity_mode.get("original_decision_reason_code"),
        "execution_decision_reason_code": parity_mode.get("execution_decision_reason_code"),
        "execution_snapshot_source": parity_mode.get("execution_snapshot_source") or snapshot.get("source"),
        "available_cash_before": available_cash,
        "available_cash_after_entry": available_cash,
        "event_key": trade_event_key(signal),
    }
    return apply_execution_audit_contract(row)


def apply_execution_audit_contract(trade: dict) -> dict:
    decision_trace = dict(trade.get("decision_trace") or {})
    parity_mode = dict(decision_trace.get("parity_mode") or {})

    trade["schema_name"] = EXECUTION_AUDIT_SCHEMA_NAME
    trade["schema_version"] = EXECUTION_AUDIT_SCHEMA_VERSION
    trade_id = str(trade.get("trade_id") or trade.get("id") or trade.get("order_id") or "").strip()
    if not trade_id:
        market_id_hint = str(trade.get("market_id") or "unknown").strip() or "unknown"
        timestamp_hint = str(trade.get("timestamp") or trade.get("resolved_at") or "unknown").strip() or "unknown"
        trade_id = f"legacy:{market_id_hint}:{timestamp_hint}"
    trade["trade_id"] = trade_id

    requested_size = coerce_float(trade.get("requested_size"), default=None)
    approved_size = coerce_float(trade.get("approved_size"), default=None)
    placed_size = coerce_float(trade.get("placed_size"), default=None)
    filled_size = coerce_float(trade.get("filled_size"), default=None)
    remaining_size = coerce_float(trade.get("remaining_size"), default=None)
    explicit_filled_size = filled_size
    explicit_remaining_size = remaining_size
    position_size = coerce_float(trade.get("position_size"), default=None)
    size_value = coerce_float(trade.get("size"), default=None)
    raw_status = trade.get("status")
    resolved_flag = coerce_bool(trade.get("resolved"), default=False)
    if resolved_flag:
        raw_status = "resolved"
    status_hint = canonical_execution_status(raw_status)

    if requested_size is None:
        requested_size = position_size if position_size is not None else coerce_float(trade.get("size"), default=0.0)
    if approved_size is None:
        approved_size = requested_size
    if placed_size is None:
        if status_hint in {"placed", "partial", "canceled", "stale"}:
            placed_size = approved_size if approved_size is not None else requested_size
        elif status_hint in {"filled", "resolved"}:
            placed_size = position_size if position_size is not None else (size_value if size_value is not None else approved_size)
        elif raw_status is None and position_size is not None:
            placed_size = position_size
        else:
            placed_size = 0.0
    if filled_size is None:
        if status_hint == "partial" and remaining_size is not None:
            filled_size = max(0.0, float(placed_size or 0.0) - max(0.0, float(remaining_size or 0.0)))
        elif status_hint in {"filled", "resolved"} or (raw_status is None and position_size is not None):
            filled_size = position_size if position_size is not None else (size_value if size_value is not None else placed_size)
        else:
            filled_size = 0.0

    status = canonical_execution_status(
        raw_status,
        filled_size=filled_size,
        placed_size=placed_size,
        remaining_size=remaining_size,
    )
    if status in {"rejected", "failed"}:
        placed_size = 0.0
        filled_size = 0.0
        remaining_size = 0.0

    requested_size = max(0.0, float(requested_size or 0.0))
    approved_size = max(0.0, min(float(approved_size or 0.0), requested_size))
    placed_size = max(0.0, min(float(placed_size or 0.0), approved_size))
    filled_size = max(0.0, min(float(filled_size or 0.0), placed_size))

    if remaining_size is None or remaining_size < 0:
        remaining_size = max(placed_size - filled_size, 0.0)
    if status == "placed":
        filled_size = 0.0
        remaining_size = placed_size
    elif status == "filled":
        if explicit_filled_size is None:
            filled_size = placed_size
        if explicit_remaining_size is None or explicit_remaining_size < 0:
            remaining_size = 0.0
    elif status in {"rejected", "failed"}:
        placed_size = 0.0
        filled_size = 0.0
        remaining_size = 0.0

    trade["status"] = status
    canonical_lifecycle = canonical_lifecycle_state(
        status,
        failure_stage=trade.get("failure_stage"),
        filled_size=filled_size,
        remaining_size=remaining_size,
    )
    existing_lifecycle = str(trade.get("lifecycle_state") or "")
    trade["lifecycle_state"] = (
        canonical_lifecycle if status == "resolved" else existing_lifecycle or canonical_lifecycle
    )
    trade["requested_size"] = round(requested_size, 4)
    trade["approved_size"] = round(approved_size, 4)
    trade["placed_size"] = round(placed_size, 4)
    trade["filled_size"] = round(filled_size, 4)
    trade["remaining_size"] = round(float(remaining_size), 4)

    reserved_capital = coerce_float(trade.get("reserved_capital"), default=None)
    reserved_capital = infer_reserved_capital(
        status,
        filled_size=filled_size,
        remaining_size=remaining_size,
        current_value=reserved_capital,
    )
    trade["reserved_capital"] = round(max(0.0, float(reserved_capital or 0.0)), 4)

    market_price = coerce_float(trade.get("market_price"), default=coerce_float(trade.get("price"), default=None))
    fill_price = coerce_float(trade.get("fill_price"), default=None)
    entry_price = coerce_float(
        trade.get("entry_price"),
        default=fill_price if fill_price is not None else market_price,
    )
    trade["market_price"] = round(market_price, 4) if market_price is not None else None
    trade["entry_price"] = round(entry_price, 4) if entry_price is not None else None
    trade["fill_price"] = round(fill_price, 4) if fill_price is not None else None

    for field in ("estimated_fill_price", "slippage_estimate", "available_cash_before", "available_cash_after_entry"):
        value = coerce_float(trade.get(field), default=None)
        trade[field] = round(value, 4) if value is not None else None

    trade["parity_mode_enabled"] = bool(trade.get("parity_mode_enabled", parity_mode.get("enabled", False)))
    trade["execution_revalidated"] = bool(
        trade.get("execution_revalidated", parity_mode.get("execution_revalidated", False))
    )
    trade["execution_revalidation_outcome"] = (
        trade.get("execution_revalidation_outcome")
        or parity_mode.get("execution_revalidation_outcome")
        or None
    )
    if trade.get("original_signal_snapshot") is None:
        trade["original_signal_snapshot"] = parity_mode.get("original_signal_snapshot")
    if trade.get("execution_snapshot") is None:
        trade["execution_snapshot"] = parity_mode.get("execution_snapshot")
    trade["execution_snapshot_source"] = canonical_execution_snapshot_source(
        trade.get("execution_snapshot_source")
        or ((trade.get("execution_snapshot") or {}).get("source") if isinstance(trade.get("execution_snapshot"), dict) else None)
        or parity_mode.get("execution_snapshot_source")
    )
    trade["decision_reason_code"] = trade.get("decision_reason_code") or trade.get("execution_decision_reason_code") or trade.get("original_decision_reason_code") or status or "unknown"
    trade["original_decision_reason_code"] = (
        trade.get("original_decision_reason_code")
        or parity_mode.get("original_decision_reason_code")
        or None
    )
    trade["execution_decision_reason_code"] = (
        trade.get("execution_decision_reason_code")
        or parity_mode.get("execution_decision_reason_code")
        or None
    )
    trade["resolved"] = coerce_bool(trade.get("resolved"), default=status == "resolved")
    if trade["resolved"] or status == "resolved":
        canonicalize_resolved_resolution_fields(trade)
    if status != "resolved" and not trade["resolved"]:
        trade["resolved_at"] = trade.get("resolved_at")
    return trade


def validate_execution_audit_row(trade: dict) -> list[str]:
    issues: list[str] = []

    if trade.get("schema_name") != EXECUTION_AUDIT_SCHEMA_NAME:
        issues.append("invalid_schema_name")
    if trade.get("schema_version") != EXECUTION_AUDIT_SCHEMA_VERSION:
        issues.append("invalid_schema_version")
    if not str(trade.get("trade_id") or "").strip():
        issues.append("missing_trade_id")
    if not str(trade.get("market_id") or "").strip():
        issues.append("missing_market_id")
    if str(trade.get("direction") or "").upper() not in VALID_DIRECTIONS:
        issues.append("invalid_direction")

    requested_size = coerce_float(trade.get("requested_size"), default=None)
    approved_size = coerce_float(trade.get("approved_size"), default=None)
    placed_size = coerce_float(trade.get("placed_size"), default=None)
    filled_size = coerce_float(trade.get("filled_size"), default=None)
    remaining_size = coerce_float(trade.get("remaining_size"), default=None)
    reserved_capital = coerce_float(trade.get("reserved_capital"), default=None)

    for name, value in (
        ("requested_size", requested_size),
        ("approved_size", approved_size),
        ("placed_size", placed_size),
        ("filled_size", filled_size),
        ("remaining_size", remaining_size),
        ("reserved_capital", reserved_capital),
    ):
        if value is None or value < 0:
            issues.append(f"invalid_{name}")

    if None not in (requested_size, approved_size, placed_size, filled_size):
        if not (filled_size <= placed_size <= approved_size <= requested_size):
            issues.append("size_monotonicity_violation")

    status = canonical_execution_status(
        trade.get("status"),
        filled_size=filled_size,
        placed_size=placed_size,
        remaining_size=remaining_size,
    )
    if status not in VALID_EXECUTION_STATUSES:
        issues.append("invalid_status")
    if status == "rejected" and (filled_size or 0.0) > 0:
        issues.append("rejected_with_fill")
    if status == "failed" and ((filled_size or 0.0) > 0 or (remaining_size or 0.0) > 0):
        issues.append("failed_with_active_size")
    if status == "placed" and (placed_size or 0.0) <= 0:
        issues.append("placed_without_size")
    if status == "placed" and (filled_size or 0.0) != 0:
        issues.append("placed_with_fill")
    if status == "placed" and (remaining_size or 0.0) != (placed_size or 0.0):
        issues.append("placed_remaining_mismatch")
    if status == "filled" and ((filled_size or 0.0) <= 0 or (remaining_size or 0.0) != 0):
        issues.append("filled_with_remaining")
    if status == "partial" and ((filled_size or 0.0) <= 0 or (remaining_size or 0.0) <= 0):
        issues.append("partial_without_split_fill")
    if status == "partial" and None not in (placed_size, filled_size, remaining_size):
        if abs((filled_size or 0.0) + (remaining_size or 0.0) - (placed_size or 0.0)) > 0.0001:
            issues.append("partial_size_sum_mismatch")
    if trade.get("resolved") and status != "resolved":
        issues.append("resolved_flag_status_mismatch")
    if status == "resolved":
        if not trade.get("resolved"):
            issues.append("resolved_without_flag")
        if not trade.get("resolved_at"):
            issues.append("resolved_without_timestamp")
        resolution_type = str(trade.get("resolution_type") or "")
        if resolution_type != "manual_mark_close" and normalize_market_outcome(trade.get("outcome")) is None:
            issues.append("resolved_without_outcome")
        if coerce_float(trade.get("pnl"), default=None) is None:
            issues.append("resolved_without_pnl")
        if coerce_float(trade.get("settlement_value"), default=None) is None:
            issues.append("resolved_without_settlement_value")
    lifecycle_state = str(trade.get("lifecycle_state") or "")
    if status == "placed" and lifecycle_state and lifecycle_state != "placed_open":
        issues.append("placed_lifecycle_mismatch")
    if status == "partial" and lifecycle_state and lifecycle_state != "partial_open":
        issues.append("partial_lifecycle_mismatch")
    if status == "filled" and lifecycle_state and lifecycle_state != "filled_open":
        issues.append("filled_lifecycle_mismatch")
    if status == "canceled":
        expected_lifecycle = "canceled_partial" if (filled_size or 0.0) > 0 else "canceled_unfilled"
        if lifecycle_state and lifecycle_state != expected_lifecycle:
            issues.append("canceled_lifecycle_mismatch")
    if status == "stale" and lifecycle_state and lifecycle_state != "stale_open_order":
        issues.append("stale_lifecycle_mismatch")
    if status == "resolved" and lifecycle_state and lifecycle_state != "resolved_position":
        issues.append("resolved_lifecycle_mismatch")
    if status == "rejected" and lifecycle_state and not lifecycle_state.endswith("_rejected"):
        issues.append("rejected_lifecycle_mismatch")
    if status == "failed" and lifecycle_state and not lifecycle_state.endswith("_failed"):
        issues.append("failed_lifecycle_mismatch")

    if not trade.get("decision_reason_code"):
        issues.append("missing_decision_reason_code")
    outcome = trade.get("execution_revalidation_outcome")
    if trade.get("execution_revalidated") and not outcome:
        issues.append("missing_execution_revalidation_outcome")
    if not trade.get("execution_revalidated") and outcome not in (None, "skipped"):
        issues.append("unexpected_execution_revalidation_outcome")
    if outcome is not None and outcome not in VALID_EXECUTION_REVALIDATION_OUTCOMES:
        issues.append("invalid_execution_revalidation_outcome")
    if not trade.get("execution_revalidated") and trade.get("execution_decision_reason_code"):
        issues.append("execution_reason_without_revalidation")
    if canonical_execution_snapshot_source(trade.get("execution_snapshot_source")) == "missing" and trade.get("execution_revalidated"):
        issues.append("revalidated_with_missing_snapshot_source")
    if trade.get("parity_mode_enabled") and trade.get("execution_revalidated") and not trade.get("execution_snapshot"):
        issues.append("missing_execution_snapshot")

    return sorted(set(issues))


def is_trade_effective_row(trade: dict) -> bool:
    market_id = str(trade.get("market_id", "") or "").strip()
    size = coerce_float(trade.get("position_size"), default=None)
    return bool(market_id) and size is not None and size > 0


def normalize_outcome(value) -> Optional[str]:
    outcome = normalize_market_outcome(value)
    if outcome is not None:
        return outcome
    result = normalize_resolution_result(value)
    if result == "won":
        return "YES"
    if result == "lost":
        return "NO"
    return None


def normalize_market_outcome(value) -> Optional[str]:
    if isinstance(value, bool):
        return "YES" if value else "NO"
    if isinstance(value, (int, float)) and value in (0, 1):
        return "YES" if int(value) == 1 else "NO"
    if isinstance(value, str):
        aliases = {
            "YES": "YES",
            "NO": "NO",
            "TRUE": "YES",
            "FALSE": "NO",
            "1": "YES",
            "0": "NO",
        }
        return aliases.get(value.strip().upper())
    return None


def normalize_resolution_result(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    aliases = {
        "WIN": "won",
        "WON": "won",
        "WINNER": "won",
        "LOSE": "lost",
        "LOSS": "lost",
        "LOST": "lost",
    }
    return aliases.get(value.strip().upper())


def resolution_result_for_outcome(direction: str, outcome: str) -> Optional[str]:
    if direction not in VALID_DIRECTIONS or outcome not in VALID_OUTCOMES:
        return None
    won = (direction == "BUY_YES" and outcome == "YES") or (direction == "BUY_NO" and outcome == "NO")
    return "won" if won else "lost"


def market_outcome_for_resolution_result(direction: str, result: Optional[str]) -> Optional[str]:
    if direction not in VALID_DIRECTIONS or result not in {"won", "lost"}:
        return None
    if direction == "BUY_YES":
        return "YES" if result == "won" else "NO"
    return "NO" if result == "won" else "YES"


def canonicalize_resolved_resolution_fields(trade: dict) -> dict:
    if not trade.get("resolved") and canonical_execution_status(trade.get("status")) != "resolved":
        return trade

    resolution_type = str(trade.get("resolution_type") or "")
    manual_mark_close = resolution_type == "manual_mark_close"
    direction = str(trade.get("direction", "") or "").upper()

    explicit_resolution_outcome = normalize_market_outcome(trade.get("resolution_outcome"))
    outcome = explicit_resolution_outcome or normalize_market_outcome(trade.get("outcome"))
    explicit_result = normalize_resolution_result(trade.get("resolution_result")) or normalize_resolution_result(
        trade.get("outcome")
    )

    if manual_mark_close:
        if explicit_resolution_outcome is not None:
            trade["resolution_outcome"] = explicit_resolution_outcome
        if outcome is not None:
            trade["outcome"] = outcome
        if explicit_result is not None:
            trade["resolution_result"] = explicit_result
        return trade

    if outcome is None:
        outcome = market_outcome_for_resolution_result(direction, explicit_result)
    if outcome is None:
        return trade

    trade["outcome"] = outcome
    trade["resolution_outcome"] = outcome

    result = resolution_result_for_outcome(direction, outcome)
    if result is not None:
        trade["resolution_result"] = result

    if coerce_float(trade.get("exit_price"), default=None) is None:
        trade["exit_price"] = 1.0 if outcome == "YES" else 0.0

    return trade


def calculate_contracts(entry_price: float, position_size: float) -> float:
    if position_size <= 0 or not (0 < entry_price < 1):
        return 0.0
    return position_size / entry_price


def calculate_realized_accounting(
    direction: str,
    entry_price: float,
    position_size: float,
    outcome: str,
    fee_rate: float = 0.07,
) -> dict:
    if position_size <= 0 or not (0 < entry_price < 1):
        return {
            "contracts": 0.0,
            "gross_pnl": 0.0,
            "fee_paid": 0.0,
            "net_pnl": 0.0,
        }

    contracts = calculate_contracts(entry_price, position_size)
    won = (
        (direction == "BUY_YES" and outcome == "YES")
        or (direction == "BUY_NO" and outcome == "NO")
    )
    if won:
        gross_pnl = contracts * (1 - entry_price)
        fee_paid = gross_pnl * fee_rate
        net_pnl = gross_pnl - fee_paid
    else:
        gross_pnl = -position_size
        fee_paid = 0.0
        net_pnl = gross_pnl

    return {
        "contracts": contracts,
        "gross_pnl": gross_pnl,
        "fee_paid": fee_paid,
        "net_pnl": net_pnl,
    }


def calculate_unrealized_pnl(
    direction: str,
    entry_price: float,
    current_price: float,
    position_size: float,
) -> float:
    if position_size <= 0 or not (0 < entry_price < 1) or current_price is None:
        return 0.0

    contracts = calculate_contracts(entry_price, position_size)
    if direction == "BUY_YES":
        return contracts * (current_price - entry_price)
    return contracts * (entry_price - current_price)


def trade_event_key(trade: dict) -> str:
    signals = trade.get("signals")
    explicit = (
        trade.get("event_key")
        or trade.get("event_ticker")
        or trade.get("event_id")
        or (signals.get("event_ticker") if isinstance(signals, dict) else None)
        or (signals.get("event_id") if isinstance(signals, dict) else None)
    )
    if explicit:
        return str(explicit)

    market_id = str(trade.get("market_id", "") or "").strip()
    if not market_id:
        return "unknown"

    category = str(trade.get("category", "") or "")
    question = str(trade.get("question", "") or "")
    parts = market_id.split("-")
    if is_weather_market(market_id=market_id, question=question, category=category):
        if len(parts) >= 3:
            return "-".join(parts[:2])

    if len(parts) >= 3:
        return "-".join(parts[:-1])
    return market_id


def enrich_trade_audit_fields(trade: dict, fee_rate: float = 0.07) -> dict:
    issues: list[str] = []

    apply_execution_audit_contract(trade)
    trade["event_key"] = trade_event_key(trade)

    direction = str(trade.get("direction", "") or "").upper()
    if direction:
        trade["direction"] = direction

    size = coerce_float(trade.get("position_size"), default=None)
    if size is None:
        size = coerce_float(
            trade.get("size"),
            default=coerce_float(trade.get("filled_size"), default=coerce_float(trade.get("placed_size"), default=None)),
        )
    if size is not None and size > 0:
        trade["position_size"] = round(size, 2)
    elif trade.get("resolved"):
        issues.append("invalid_position_size")

    reserved_capital = coerce_float(trade.get("reserved_capital"), default=None)
    if reserved_capital is None and size is not None and size > 0 and not trade.get("resolved"):
        reserved_capital = size
    if reserved_capital is not None and reserved_capital >= 0:
        trade["reserved_capital"] = round(reserved_capital, 2)

    market_price = coerce_float(trade.get("market_price"), default=None)
    entry_price = coerce_float(
        trade.get("entry_price"),
        default=coerce_float(trade.get("fill_price"), default=market_price),
    )
    if market_price is not None and 0 < market_price < 1:
        trade["market_price"] = round(market_price, 4)
    elif trade.get("resolved"):
        issues.append("invalid_market_price")

    if entry_price is not None and 0 < entry_price < 1:
        trade["entry_price"] = round(entry_price, 4)
        if size is not None and size > 0:
            trade["contracts"] = round(calculate_contracts(entry_price, size), 4)
    elif trade.get("resolved"):
        issues.append("invalid_entry_price")

    if not trade.get("resolved"):
        issues.extend(validate_execution_audit_row(trade))
        trade["integrity_status"] = "ok" if not issues else "invalid"
        trade["integrity_errors"] = sorted(set(issues))
        return trade

    if direction not in VALID_DIRECTIONS:
        issues.append("invalid_direction")

    resolution_type = str(trade.get("resolution_type") or "")
    manual_mark_close = resolution_type == "manual_mark_close"

    canonicalize_resolved_resolution_fields(trade)

    outcome = normalize_market_outcome(trade.get("outcome"))
    if outcome is None:
        if not manual_mark_close:
            issues.append("invalid_outcome")
    else:
        trade["outcome"] = outcome

    if not trade.get("resolved_at"):
        issues.append("missing_resolved_at")

    reported_pnl = coerce_float(trade.get("pnl"), default=None)
    if manual_mark_close:
        if reported_pnl is None:
            issues.append("missing_pnl")
        else:
            contracts = calculate_contracts(entry_price, size) if entry_price is not None and size is not None else 0.0
            trade["contracts"] = round(contracts, 4)
            trade["gross_pnl"] = round(reported_pnl, 4)
            trade["fee_paid"] = 0.0
            trade["expected_pnl"] = round(reported_pnl, 4)
            trade["pnl"] = round(reported_pnl, 4)
            trade["net_pnl"] = round(reported_pnl, 4)
            settlement_value = coerce_float(trade.get("settlement_value"), default=None)
            if settlement_value is None and size is not None and size > 0:
                settlement_value = size + reported_pnl
            if settlement_value is not None:
                trade["settlement_value"] = round(settlement_value, 4)
    elif (
        direction in VALID_DIRECTIONS
        and outcome in VALID_OUTCOMES
        and size is not None
        and size > 0
        and entry_price is not None
        and 0 < entry_price < 1
    ):
        accounting = calculate_realized_accounting(
            direction=direction,
            entry_price=entry_price,
            position_size=size,
            outcome=outcome,
            fee_rate=fee_rate,
        )
        trade["contracts"] = round(accounting["contracts"], 4)
        trade["gross_pnl"] = round(accounting["gross_pnl"], 4)
        trade["fee_paid"] = round(accounting["fee_paid"], 4)
        trade["expected_pnl"] = round(accounting["net_pnl"], 4)
        if reported_pnl is None:
            reported_pnl = accounting["net_pnl"]
            trade["pnl"] = round(reported_pnl, 4)
        if abs(reported_pnl - accounting["net_pnl"]) > 0.01:
            issues.append("pnl_mismatch")
        trade["net_pnl"] = round(reported_pnl, 4)
    elif reported_pnl is None:
        issues.append("missing_pnl")

    if reported_pnl is not None:
        trade["pnl"] = round(reported_pnl, 4)
        trade["net_pnl"] = round(reported_pnl, 4)
        settlement_value = coerce_float(trade.get("settlement_value"), default=None)
        if settlement_value is None and size is not None and size > 0:
            settlement_value = size + reported_pnl
        if settlement_value is not None:
            trade["settlement_value"] = round(settlement_value, 4)

    issues.extend(validate_execution_audit_row(trade))
    trade["integrity_status"] = "ok" if not issues else "invalid"
    trade["integrity_errors"] = sorted(set(issues))
    return trade


def group_trades_by_event(
    trades: list[dict],
    *,
    resolved_only: bool = False,
    trusted_only: bool = False,
) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        if resolved_only and not trade.get("resolved"):
            continue
        if trusted_only and trade.get("integrity_status") != "ok":
            continue
        grouped[trade.get("event_key") or trade_event_key(trade)].append(trade)
    return dict(grouped)


def summarize_event_performance(trades: list[dict]) -> dict:
    event_groups = group_trades_by_event(trades, resolved_only=True, trusted_only=True)
    event_pnls = [
        round(sum(coerce_float(t.get("net_pnl", t.get("pnl")), 0.0) for t in group), 4)
        for group in event_groups.values()
    ]
    wins = sum(1 for pnl in event_pnls if pnl > 0)
    losses = sum(1 for pnl in event_pnls if pnl < 0)
    flats = sum(1 for pnl in event_pnls if pnl == 0)
    total = len(event_pnls)
    avg_positions = (
        round(sum(len(group) for group in event_groups.values()) / total, 2)
        if total else 0.0
    )
    return {
        "resolved_events": total,
        "wins": wins,
        "losses": losses,
        "flat": flats,
        "win_rate": round(wins / total * 100, 1) if total else 0.0,
        "total_pnl": round(sum(event_pnls), 4) if event_pnls else 0.0,
        "avg_pnl_per_event": round(sum(event_pnls) / total, 4) if total else 0.0,
        "avg_positions_per_resolved_event": avg_positions,
        "retrade_count": sum(max(0, len(group) - 1) for group in event_groups.values()),
    }
