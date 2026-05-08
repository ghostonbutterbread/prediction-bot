"""Shared counterfactual shadow-intent audit helpers.

Shadow intents are audit rows, not execution audit rows. Paper and live
handlers may persist them to a separate ledger, but they must never be routed
through order placement, trade history, risk accounting, or P&L accounting.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any, Iterable

from bot.file_ops import append_jsonl


SHADOW_INTENT_SCHEMA_NAME = "shadow_intent_audit_row"
SHADOW_INTENT_SCHEMA_VERSION = 1
logger = logging.getLogger(__name__)


def build_hypothetical_shadow_intent_row(
    source_row: dict[str, Any],
    *,
    runtime_mode: str,
    recorded_at: str | None = None,
) -> dict[str, Any] | None:
    """Convert a row-level beta/shadow delta into a separate audit-ledger row."""
    if not isinstance(source_row, dict):
        return None
    shadow_delta = source_row.get("shadow_delta")
    if not isinstance(shadow_delta, dict) or not _is_beta_shadow_delta(shadow_delta):
        return None

    policy = shadow_delta.get("policy") if isinstance(shadow_delta.get("policy"), dict) else {}
    stable = shadow_delta.get("stable") if isinstance(shadow_delta.get("stable"), dict) else {}
    shadow = shadow_delta.get("shadow") if isinstance(shadow_delta.get("shadow"), dict) else {}
    action = _normalize_shadow_action(shadow.get("action"))
    direction = action if action in {"BUY_YES", "BUY_NO"} else None
    requested_size = _coerce_finite_number(shadow.get("requested_position_size"))
    intent_kind = "trade" if action in {"BUY_YES", "BUY_NO"} else ("skip" if action == "SKIP" else "unknown")
    dedupe_key = _coerce_optional_string(shadow_delta.get("dedupe_key"))
    market_id = _coerce_optional_string(source_row.get("market_id")) or _coerce_optional_string(shadow_delta.get("market_id"))
    run_id = _coerce_optional_string(source_row.get("run_id")) or _coerce_optional_string(shadow_delta.get("run_id"))
    ledger_id = _shadow_intent_id(dedupe_key=dedupe_key, market_id=market_id, run_id=run_id, runtime_mode=runtime_mode)

    return {
        "schema_name": SHADOW_INTENT_SCHEMA_NAME,
        "schema_version": SHADOW_INTENT_SCHEMA_VERSION,
        "ledger_type": "counterfactual_shadow_intent",
        "intent_id": ledger_id,
        "recorded_at": recorded_at or _source_timestamp(source_row),
        "runtime_mode": _normalize_runtime_mode(runtime_mode),
        "mode": "beta_shadow",
        "policy": {
            "version": "beta",
            "mode": "shadow",
            "enabled_features": _coerce_string_list(policy.get("enabled_features")),
        },
        "hypothetical": True,
        "counterfactual": True,
        "real_trade": False,
        "counts_as_trade": False,
        "counts_as_exposure": False,
        "counts_as_pnl": False,
        "execution_allowed": False,
        "final_action_mutated": False,
        "final_action_effect": "none",
        "mutates_balances": False,
        "mutates_exposure": False,
        "mutates_risk_state": False,
        "mutates_pnl": False,
        "mutates_trade_history": False,
        "mutates_open_orders": False,
        "mutates_open_positions": False,
        "market_id": market_id,
        "run_id": run_id,
        "prediction_id": _coerce_optional_string(source_row.get("prediction_id")),
        "source_dedupe_key": dedupe_key,
        "source_shadow_delta": {
            "schema_version": shadow_delta.get("schema_version"),
            "mode": shadow_delta.get("mode"),
            "status": shadow_delta.get("status"),
            "dedupe_key": dedupe_key,
            "evidence_sources": _coerce_string_list(shadow_delta.get("evidence_sources")),
        },
        "stable_final": {
            "action": _normalize_shadow_action(stable.get("action")),
            "direction": _coerce_optional_string(stable.get("direction")),
            "decision_type": _coerce_optional_string(stable.get("decision_type")),
            "reason_code": _coerce_optional_string(stable.get("reason_code")),
            "requested_position_size": _coerce_finite_number(stable.get("requested_position_size")),
            "selected_lane": _coerce_optional_string(stable.get("selected_lane")),
        },
        "shadow_intent": {
            "intent_kind": intent_kind,
            "action": action,
            "direction": direction,
            "decision_type": _coerce_optional_string(shadow.get("decision_type")) or intent_kind,
            "reason_code": _coerce_optional_string(shadow.get("reason_code")),
            "hypothetical_requested_position_size": requested_size if intent_kind == "trade" else None,
            "selected_lane": _coerce_optional_string(shadow.get("selected_lane")),
            "comparison_complete": shadow_delta.get("comparison_complete") is True,
            "action_comparison_available": shadow_delta.get("action_comparison_available") is True,
        },
        "deltas": {
            "changed": shadow_delta.get("changed"),
            "action_changed": shadow_delta.get("action_changed"),
            "side_changed": shadow_delta.get("side_changed"),
            "buy_decision_changed": shadow_delta.get("buy_decision_changed"),
            "reason_changed": shadow_delta.get("reason_changed"),
            "size_changed": shadow_delta.get("size_changed"),
            "lane_changed": shadow_delta.get("lane_changed"),
        },
        "execution": {
            "status": "not_executed",
            "order_id": None,
            "requested_size": 0.0,
            "approved_size": 0.0,
            "placed_size": 0.0,
            "filled_size": 0.0,
            "remaining_size": 0.0,
            "reserved_capital_delta": 0.0,
            "pnl": None,
        },
        "handler_contract": {
            "allowed_actions": ["append_counterfactual_ledger", "audit"],
            "forbidden_actions": [
                "place_order",
                "append_trade_history",
                "reserve_capital",
                "record_risk_trade",
                "update_pnl",
                "change_final_action",
            ],
        },
    }


def build_hypothetical_shadow_intent_rows(
    rows: Iterable[dict[str, Any]],
    *,
    runtime_mode: str,
) -> list[dict[str, Any]]:
    """Build counterfactual rows from all beta/shadow deltas in an iterable."""
    return [
        row
        for row in (build_hypothetical_shadow_intent_row(source_row, runtime_mode=runtime_mode) for source_row in rows or [])
        if row is not None
    ]


def append_hypothetical_shadow_intent_row(
    path: str | Path,
    source_row: dict[str, Any],
    *,
    runtime_mode: str,
    recorded_at: str | None = None,
) -> dict[str, Any] | None:
    """Append one counterfactual shadow-intent row to its isolated ledger.

    Shadow-intent persistence is audit-only and must never block the real
    paper/live execution path. Build failures still fail closed by returning
    ``None``; append failures are logged and swallowed.
    """
    row = build_hypothetical_shadow_intent_row(
        source_row,
        runtime_mode=runtime_mode,
        recorded_at=recorded_at,
    )
    if row is None:
        return None
    try:
        append_jsonl(Path(path), row)
    except Exception as exc:
        logger.warning("shadow_intent_append_failed path=%s error=%s", path, exc)
        return None
    return row


def is_hypothetical_shadow_intent_row(row: dict[str, Any]) -> bool:
    return bool(
        isinstance(row, dict)
        and row.get("schema_name") == SHADOW_INTENT_SCHEMA_NAME
        and row.get("schema_version") == SHADOW_INTENT_SCHEMA_VERSION
        and row.get("hypothetical") is True
        and row.get("execution_allowed") is False
    )


def _is_beta_shadow_delta(shadow_delta: dict[str, Any]) -> bool:
    if shadow_delta.get("mode") != "beta_shadow_delta":
        return False
    policy = shadow_delta.get("policy") if isinstance(shadow_delta.get("policy"), dict) else {}
    if not policy:
        return False
    return str(policy.get("version") or "").strip().lower() == "beta" and str(
        policy.get("mode") or ""
    ).strip().lower() == "shadow"


def _normalize_runtime_mode(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "live":
        return "live"
    if normalized == "prediction_lab":
        return "prediction_lab"
    return "paper"


def _normalize_shadow_action(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {"BUY_YES", "BUY_NO", "SKIP"}:
        return normalized
    return "UNKNOWN"


def _source_timestamp(source_row: dict[str, Any]) -> str:
    for key in ("recorded_at", "timestamp", "observed_at", "created_at"):
        value = _coerce_optional_string(source_row.get(key))
        if value:
            return value
    return datetime.now(timezone.utc).isoformat()


def _shadow_intent_id(
    *,
    dedupe_key: str | None,
    market_id: str | None,
    run_id: str | None,
    runtime_mode: str,
) -> str:
    base = dedupe_key or "|".join(
        part or "unknown"
        for part in (
            market_id,
            run_id,
            "beta-shadow",
        )
    )
    return f"shadow-intent:{_normalize_runtime_mode(runtime_mode)}:{base}"


def _coerce_optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip()
    return normalized or None


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, dict):
        return sorted(str(key) for key, enabled in value.items() if enabled is True)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


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


__all__ = [
    "SHADOW_INTENT_SCHEMA_NAME",
    "SHADOW_INTENT_SCHEMA_VERSION",
    "append_hypothetical_shadow_intent_row",
    "build_hypothetical_shadow_intent_row",
    "build_hypothetical_shadow_intent_rows",
    "is_hypothetical_shadow_intent_row",
]
