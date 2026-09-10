"""Outcome-free intent projection for composed shadow-lane wallet replays.

This module is deliberately a decision-time boundary.  It turns one already
composed lane row into the ``TradeContext`` consumed by shared-core Kelly/risk
logic; it neither executes, settles, nor writes wallet state.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from bot.shared_core import AccountState, TradeContext, build_trade_decision

_BUY_ACTIONS = frozenset({"BUY_YES", "BUY_NO"})
_FORBIDDEN_OUTCOME_FIELDS = frozenset({"outcome", "official_outcome", "settlement_ts", "resolved_at"})


def build_composed_trade_context(
    intent: Mapping[str, Any],
    account_state: AccountState,
) -> tuple[TradeContext, dict[str, Any]]:
    """Project a composed decision into shared-core inputs without outcome state.

    ``model_probability`` is always the probability of YES.  The returned
    receipt additionally records the probability of the selected side so reports
    can explain Kelly sizing for BUY_NO correctly.
    """
    row = dict(intent)
    _reject_outcome_fields(row)
    action = str(row.get("action") or "SKIP").upper()
    if action not in _BUY_ACTIONS:
        raise ValueError("composed wallet intent must be BUY_YES or BUY_NO")
    market_id = _required_text(row, "market_id")
    question = _required_text(row, "question")
    observed_at = _required_text(row, "observed_at")
    price = _probability(row.get("entry_price"), "entry_price")
    yes_probability = _probability(row.get("model_probability"), "model_probability")
    confidence = _probability(row.get("confidence"), "confidence")
    selected_probability = yes_probability if action == "BUY_YES" else 1.0 - yes_probability
    source_context = dict(row.get("source_context") or {})
    route = source_context.get("market_route")
    if not isinstance(route, Mapping) or not route.get("allowed") or not route.get("handler_id"):
        raise ValueError("composed wallet intent requires an allowed market_route with handler_id")
    source_context["market_id"] = market_id
    source_context["direction"] = action
    source_context["model_probability"] = yes_probability
    source_context["market_route"] = dict(route)
    context = TradeContext(
        exchange=str(row.get("exchange") or "kalshi"),
        market_id=market_id,
        question=question,
        direction=action,
        market_price=price,
        yes_price=price if action == "BUY_YES" else None,
        no_price=price if action == "BUY_NO" else None,
        model_probability=yes_probability,
        edge=selected_probability - price,
        confidence=confidence,
        account_state=account_state,
        source_context=source_context,
        metadata={
            "composed_lane_id": str(row.get("lane_id") or ""),
            "composed_decision_id": str(row.get("decision_id") or ""),
            "observed_at": observed_at,
            "market_route": dict(route),
        },
    )
    return context, {
        "schema_name": "composed_lane_wallet_intent",
        "schema_version": 1,
        "decision_id": str(row.get("decision_id") or ""),
        "lane_id": str(row.get("lane_id") or ""),
        "market_id": market_id,
        "observed_at": observed_at,
        "action": action,
        "entry_price": price,
        "yes_probability": yes_probability,
        "selected_side_probability": selected_probability,
        "probability_provider": "composition.model_probability",
        "non_mutating": True,
    }


def evaluate_composed_intents(
    *,
    intents: list[Mapping[str, Any]],
    resolutions: list[Mapping[str, Any]],
    starting_balance_usd: float,
    kelly_sizer: Any,
    risk_policy: Any,
    min_edge: float,
    min_confidence: float,
    max_entry_price: float,
) -> dict[str, Any]:
    """Run sealed composed intents through shared-core sizing in memory only."""
    if starting_balance_usd <= 0:
        raise ValueError("starting_balance_usd must be positive")
    settlement_by_id = _strict_resolution_index(resolutions)
    ordered = sorted((dict(row) for row in intents), key=lambda row: (_timestamp(row["observed_at"]), str(row.get("decision_id") or "")))
    cash, positions, decisions, settlements = float(starting_balance_usd), [], [], []
    for intent in ordered:
        now = _timestamp(intent["observed_at"])
        cash, due = _settle_due(positions, settlement_by_id, cash, now)
        settlements.extend(due)
        state = AccountState(
            starting_balance=float(starting_balance_usd), current_balance=cash + sum(p["stake_usd"] for p in positions),
            available_cash=cash, reserved_capital=sum(p["stake_usd"] for p in positions),
            total_exposure=sum(p["stake_usd"] for p in positions), open_positions=len(positions),
        )
        context, intent_receipt = build_composed_trade_context(intent, state)
        decision = build_trade_decision(context, kelly_sizer=kelly_sizer, risk_policy=risk_policy, min_edge=min_edge, min_confidence=min_confidence, max_entry_price=max_entry_price)
        row = {"decision_id": intent_receipt["decision_id"], "market_id": context.market_id, "observed_at": intent_receipt["observed_at"], "action": decision.action if decision.approved else "SKIP", "available_cash_before_usd": round(cash, 6), "requested_size_usd": decision.requested_position_size, "approved_stake_usd": decision.position_size or 0.0, "reason_code": decision.reason_code, "shared_core_reasoning": decision.reasoning, "status": "skipped"}
        if decision.approved and decision.position_size and decision.position_size > 0:
            stake = float(decision.position_size)
            if stake > cash:
                row["reason_code"] = "insufficient_available_cash"
            else:
                cash -= stake
                positions.append({"decision_id": row["decision_id"], "shared_candidate_id": _required_text(intent, "shared_candidate_id"), "run_id": _required_text(intent, "run_id"), "market_id": context.market_id, "action": decision.action, "entry_price": float(decision.entry_price or 0.0), "stake_usd": stake, "observed_at": now})
                row.update(status="opened", available_cash_after_usd=round(cash, 6))
        decisions.append(row)
    cash, due = _settle_due(positions, settlement_by_id, cash, None)
    settlements.extend(due)
    reserved = sum(p["stake_usd"] for p in positions)
    return {"schema_name": "composed_lane_synthetic_wallet", "schema_version": 1, "non_mutating": True, "paper_only": True, "paper_parity_claim": False, "promotion_eligible": False, "decision_rows": decisions, "settlement_rows": settlements, "open_positions": positions, "summary": {"starting_balance_usd": starting_balance_usd, "final_balance_usd": round(cash + reserved, 6), "reserved_capital_usd": round(reserved, 6), "opened_positions": sum(row["status"] == "opened" for row in decisions)}}


def _strict_resolution_index(resolutions: list[Mapping[str, Any]]) -> dict[tuple[str, str, str, str], Mapping[str, Any]]:
    """Accept only one authoritative receipt per complete sealed identity."""
    candidates: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for raw in resolutions:
        row = dict(raw)
        if row.get("authoritative") is not True or not _is_sha256(row.get("resolution_row_sha256")):
            continue
        try:
            key = _resolution_key(row)
            _timestamp(row.get("settlement_ts"))
        except ValueError:
            continue
        candidates.setdefault(key, []).append(row)
    accepted: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    for key, rows in candidates.items():
        fingerprints = {(row.get("outcome"), row.get("settlement_ts"), row.get("resolution_row_sha256")) for row in rows}
        if len(rows) == 1 or len(fingerprints) == 1:
            accepted[key] = rows[0]
    return accepted


def _resolution_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        _required_text(row, "decision_id"), _required_text(row, "shared_candidate_id"),
        _required_text(row, "run_id"), _required_text(row, "market_id"),
    )


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value.lower())


def _settle_due(positions: list[dict[str, Any]], resolutions: Mapping[tuple[str, str, str, str], Mapping[str, Any]], cash: float, before: datetime | None) -> tuple[float, list[dict[str, Any]]]:
    settled, remaining = [], []
    for position in positions:
        receipt = resolutions.get((position["decision_id"], position["shared_candidate_id"], position["run_id"], position["market_id"]))
        if not receipt or _timestamp(receipt.get("settlement_ts")) <= position["observed_at"] or (before is not None and _timestamp(receipt.get("settlement_ts")) > before):
            remaining.append(position)
            continue
        outcome = str(receipt.get("outcome") or "").upper()
        if outcome not in {"YES", "NO", "VOID"}:
            remaining.append(position)
            continue
        if outcome == "VOID":
            cash += position["stake_usd"]
            settled.append({"decision_id": position["decision_id"], "market_id": position["market_id"], "outcome": outcome, "settlement_ts": receipt["settlement_ts"], "void_resolution": True, "resolution_row_sha256": receipt["resolution_row_sha256"]})
            continue
        won = outcome == position["action"].removeprefix("BUY_")
        payout = position["stake_usd"] / position["entry_price"] if won else 0.0
        cash += payout
        settled.append({"decision_id": position["decision_id"], "market_id": position["market_id"], "outcome": outcome, "settlement_ts": receipt["settlement_ts"], "stake_usd": round(position["stake_usd"], 6), "pnl_usd": round(payout - position["stake_usd"], 6), "resolution_row_sha256": receipt["resolution_row_sha256"]})
    positions[:] = remaining
    return cash, settled


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("composed wallet timestamps must be ISO strings")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("composed wallet timestamps require timezone")
    return parsed


def _reject_outcome_fields(row: Mapping[str, Any]) -> None:
    """Reject outcome-like keys at every depth before decision evaluation."""
    def visit(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_text = str(key).lower()
                child_path = f"{path}.{key}" if path else str(key)
                if key_text in _FORBIDDEN_OUTCOME_FIELDS or any(token in key_text for token in ("outcome", "settlement", "resolved")):
                    raise ValueError(f"composed wallet intent contains outcome-like field: {child_path}")
                visit(child, child_path)
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(row, "")


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = str(row.get(field) or "").strip()
    if not value:
        raise ValueError(f"composed wallet intent requires {field}")
    return value


def _probability(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"composed wallet intent requires numeric {field}") from exc
    if not 0 < number < 1:
        raise ValueError(f"composed wallet intent requires {field} in (0, 1)")
    return number
