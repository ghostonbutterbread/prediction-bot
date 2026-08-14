"""Legacy, unbound immediate-settlement diagnostic for source-router replay.

This module is deliberately not a wallet, risk, Kelly, execution, capacity, or
drawdown simulation. Its market-level outcome joins are not bound to an exact
decision/resolution identity, and every eligible row is settled immediately.
Keep a true wallet deferred until identity-bound resolutions and settlement-time
position handling exist.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

BUY_ACTIONS = {"BUY_YES", "BUY_NO"}
LEGACY_DIAGNOSTIC_MODE = "legacy_unbound_immediate_settlement_diagnostic"
LEGACY_DIAGNOSTIC_BLOCKERS = [
    "market_level_legacy_outcome_join",
    "immediate_settlement",
    "no_pending_position_or_capital_reservation",
    "no_executable_quote_guarantee",
]


def simulate_legacy_unbound_immediate_settlement_diagnostic(
    decisions: Iterable[Mapping[str, Any]], *, fixed_stake_usd: float = 10.0
) -> dict[str, Any]:
    """Return a labeled legacy diagnostic, never a wallet evaluation.

    The optional fixed stake only normalizes a row-level hypothetical payoff.
    It does not reserve capital, model pending positions, or establish
    executable P&L, capacity, risk, drawdown, viability, or promotion evidence.
    """
    if fixed_stake_usd <= 0:
        raise ValueError("fixed_stake_usd must be positive")
    ordered = sorted((dict(row) for row in decisions if isinstance(row, Mapping)), key=_decision_sort_key)
    trades = [_settle_immediately(row, stake=fixed_stake_usd) for row in ordered]
    settled = [trade for trade in trades if trade.get("settled")]
    return {
        "schema_version": 2,
        "mode": LEGACY_DIAGNOSTIC_MODE,
        "diagnostic_label": LEGACY_DIAGNOSTIC_MODE,
        "network_access": False,
        "non_mutating": True,
        "not_a_wallet": True,
        "not_executable_pnl_evidence": True,
        "not_capacity_or_drawdown_viability_evidence": True,
        "not_promotion_evidence": True,
        "blockers": list(LEGACY_DIAGNOSTIC_BLOCKERS),
        "true_wallet_status": "deferred_pending_exact_identity_bound_outcomes_and_settlement_time_position_handling",
        "legacy_fixed_stake_diagnostic": {
            "mode": LEGACY_DIAGNOSTIC_MODE,
            "stake_per_eligible_buy_usd": fixed_stake_usd,
            "settlement_model": "immediate_unbound_market_level_outcome_join",
            "trades": trades,
            "summary": {
                "decision_count": len(ordered),
                "buy_count": len(settled),
                "skipped_count": len(ordered) - len(settled),
                "total_hypothetical_pnl_usd": _round(sum(float(row.get("hypothetical_pnl_usd") or 0.0) for row in settled)),
            },
        },
    }


def simulate_replay_wallet_lanes(
    decisions: Iterable[Mapping[str, Any]], *, fixed_stake_usd: float = 10.0, **_ignored: Any
) -> dict[str, Any]:
    """Compatibility alias; its result remains explicitly diagnostic-only."""
    return simulate_legacy_unbound_immediate_settlement_diagnostic(decisions, fixed_stake_usd=fixed_stake_usd)


def _settle_immediately(row: Mapping[str, Any], *, stake: float) -> dict[str, Any]:
    comparison = row.get("comparison") if isinstance(row.get("comparison"), Mapping) else {}
    resolution = row.get("resolution_join") if isinstance(row.get("resolution_join"), Mapping) else {}
    trade = {
        "source_router_decision_id": row.get("source_router_decision_id"),
        "market_id": row.get("market_id"),
        "observed_at": row.get("observed_at"),
        "action": _action(row),
        "side": comparison.get("source_router_side"),
        "entry_price": _number(comparison.get("source_router_side_price")),
        "market_level_outcome": resolution.get("official_outcome"),
        "stake_usd": stake,
        "settlement_semantics": "immediate_unbound_market_level_outcome_join",
        "blockers": list(LEGACY_DIAGNOSTIC_BLOCKERS),
    }
    if trade["action"] not in BUY_ACTIONS:
        trade.update({"settled": False, "skip_reason": "router_skip"})
        return trade
    if trade["side"] not in {"YES", "NO"} or trade["market_level_outcome"] not in {"YES", "NO"} or not 0 < trade["entry_price"] <= 1:
        trade.update({"settled": False, "skip_reason": "missing_legacy_input"})
        return trade
    won = trade["side"] == trade["market_level_outcome"]
    pnl = stake * ((1.0 / trade["entry_price"]) - 1.0) if won else -stake
    trade.update({"settled": True, "won": won, "hypothetical_pnl_usd": _round(pnl), "skip_reason": None})
    return trade


def _action(row: Mapping[str, Any]) -> str:
    comparison = row.get("comparison") if isinstance(row.get("comparison"), Mapping) else {}
    return str(comparison.get("source_router_action") or "SKIP")


def _decision_sort_key(row: Mapping[str, Any]) -> tuple[datetime, str]:
    value = row.get("observed_at")
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")), str(row.get("source_router_decision_id") or "")
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc), str(row.get("source_router_decision_id") or "")


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _round(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


__all__ = [
    "LEGACY_DIAGNOSTIC_BLOCKERS",
    "LEGACY_DIAGNOSTIC_MODE",
    "simulate_legacy_unbound_immediate_settlement_diagnostic",
    "simulate_replay_wallet_lanes",
]
