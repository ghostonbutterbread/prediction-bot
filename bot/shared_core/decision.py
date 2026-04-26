"""Thin shared decision path used by paper today and live later."""

from __future__ import annotations

from math import isfinite
from typing import Any, Protocol

from bot.trade_audit import trade_event_key

from .interfaces import TradeContext, TradeDecision


class KellySizerLike(Protocol):
    def calculate(self, win_probability: float, entry_price: float, bankroll: float) -> float:
        ...


class RiskPolicyLike(Protocol):
    def check_trade(self, signal: dict[str, Any], position_size: float, *, available_cash: float | None = None):
        ...


DEFAULT_RETRADE_POLICY = {
    "max_event_exposure_pct": 0.10,
    "max_event_positions": 3,
    "retrade_edge_premium": 0.01,
    "retrade_confidence_premium": 0.0,
    "retrade_size_decay": 0.65,
    "strict_event_overlap": True,
    "min_retrade_net_edge": 0.005,
    "min_retrade_expected_profit_usd": 0.0,
    "require_price_improvement_for_same_market_family": False,
    "price_improvement_ticks": 0.03,
    "fee_rate": 0.07,
}

HIDDEN_GEM_ENTRY_PRICE_CAP = 0.05
HIDDEN_GEM_MIN_EDGE = 0.05
HIDDEN_GEM_MIN_PROBABILITY_MULTIPLE = 3.0
NON_HIDDEN_GEM_MIN_WIN_PROBABILITY = 0.50


def build_trade_decision(
    context: TradeContext,
    *,
    kelly_sizer: KellySizerLike,
    risk_policy: RiskPolicyLike,
    min_edge: float,
    min_confidence: float,
    max_entry_price: float,
) -> TradeDecision:
    """Return a shared-core trade decision without doing any execution."""

    normalized = normalize_trade_context(context)
    event_snapshot = build_event_snapshot(context)
    retrade_policy = _retrade_policy_for_context(context)
    reasoning = {
        "account_state": {
            "available_cash": round(context.account_state.available_cash, 2),
            "reserved_capital": round(context.account_state.reserved_capital, 2),
            "open_positions": context.account_state.open_positions,
            "total_exposure": round(context.account_state.total_exposure, 2),
        },
        "thresholds": {
            "min_edge": min_edge,
            "min_confidence": min_confidence,
            "max_entry_price": max_entry_price,
        },
        "event": event_snapshot,
        "retrade": {
            "enabled": bool(event_snapshot["retrade"]),
            "event_key": event_snapshot["event_key"],
            "event_position_count_before": event_snapshot["event_position_count_before"],
            "event_exposure_before": event_snapshot["event_exposure_before"],
            "event_headroom": event_snapshot["event_headroom"],
            "filled_event_exposure_before": event_snapshot["filled_event_exposure_before"],
            "pending_event_exposure_before": event_snapshot["pending_event_exposure_before"],
            "filled_event_position_count_before": event_snapshot["filled_event_position_count_before"],
            "pending_event_position_count_before": event_snapshot["pending_event_position_count_before"],
            "overlap_penalty": event_snapshot["overlap_penalty"],
        },
    }

    if normalized is None:
        return TradeDecision(
            action="SKIP",
            approved=False,
            reason_code="invalid_signal",
            reason="Signal missing valid price/probability inputs",
            edge=_coerce_optional_float(context.edge),
            confidence=_coerce_optional_float(context.confidence) or 0.0,
            reasoning=reasoning,
        )

    direction = normalized["direction"]
    entry_price = normalized["entry_price"]
    win_probability = normalized["win_probability"]
    edge = _coerce_optional_float(context.edge) or 0.0
    confidence = _coerce_optional_float(context.confidence) or 0.0

    reasoning["normalized"] = {
        "direction": direction,
        "entry_price": entry_price,
        "win_probability": win_probability,
    }

    if edge < min_edge:
        return TradeDecision(
            action="SKIP",
            approved=False,
            reason_code="edge_below_threshold",
            reason=f"Edge {edge:.4f} below minimum {min_edge:.4f}",
            edge=edge,
            confidence=confidence,
            entry_price=entry_price,
            win_probability=win_probability,
            reasoning=reasoning,
        )

    if confidence < min_confidence:
        return TradeDecision(
            action="SKIP",
            approved=False,
            reason_code="confidence_below_threshold",
            reason=f"Confidence {confidence:.4f} below minimum {min_confidence:.4f}",
            edge=edge,
            confidence=confidence,
            entry_price=entry_price,
            win_probability=win_probability,
            reasoning=reasoning,
        )

    if entry_price > max_entry_price:
        return TradeDecision(
            action="SKIP",
            approved=False,
            reason_code="entry_price_above_cap",
            reason=f"Entry price {entry_price:.4f} above cap {max_entry_price:.4f}",
            edge=edge,
            confidence=confidence,
            entry_price=entry_price,
            win_probability=win_probability,
            reasoning=reasoning,
        )

    hidden_gem_thresholds = {
        "entry_price_cap": HIDDEN_GEM_ENTRY_PRICE_CAP,
        "min_edge": HIDDEN_GEM_MIN_EDGE,
        "min_probability_multiple": HIDDEN_GEM_MIN_PROBABILITY_MULTIPLE,
    }
    reasoning["thresholds"]["hidden_gem"] = hidden_gem_thresholds
    if entry_price <= HIDDEN_GEM_ENTRY_PRICE_CAP:
        probability_multiple = (win_probability / entry_price) if entry_price > 0 else float("inf")
        reasoning["hidden_gem"] = {
            "triggered": True,
            "probability_multiple": round(probability_multiple, 4),
            "entry_price": entry_price,
            "win_probability": win_probability,
            "edge": edge,
        }
        if edge < HIDDEN_GEM_MIN_EDGE:
            return TradeDecision(
                action="SKIP",
                approved=False,
                reason_code="hidden_gem_edge_below_threshold",
                reason=(
                    f"Hidden gem edge {edge:.4f} below minimum {HIDDEN_GEM_MIN_EDGE:.4f} "
                    f"for entry price {entry_price:.4f}"
                ),
                edge=edge,
                confidence=confidence,
                entry_price=entry_price,
                win_probability=win_probability,
                reasoning=reasoning,
            )
        if probability_multiple + 1e-9 < HIDDEN_GEM_MIN_PROBABILITY_MULTIPLE:
            return TradeDecision(
                action="SKIP",
                approved=False,
                reason_code="hidden_gem_probability_multiple_below_threshold",
                reason=(
                    f"Hidden gem probability multiple {probability_multiple:.4f} below minimum "
                    f"{HIDDEN_GEM_MIN_PROBABILITY_MULTIPLE:.4f} for entry price {entry_price:.4f}"
                ),
                edge=edge,
                confidence=confidence,
                entry_price=entry_price,
                win_probability=win_probability,
                reasoning=reasoning,
            )
    else:
        reasoning["hidden_gem"] = {"triggered": False}
        reasoning["thresholds"]["non_hidden_gem_min_win_probability"] = NON_HIDDEN_GEM_MIN_WIN_PROBABILITY
        if win_probability <= NON_HIDDEN_GEM_MIN_WIN_PROBABILITY:
            return TradeDecision(
                action="SKIP",
                approved=False,
                reason_code="win_probability_below_non_hidden_gem_floor",
                reason=(
                    f"Win probability {win_probability:.4f} must be above "
                    f"{NON_HIDDEN_GEM_MIN_WIN_PROBABILITY:.4f} unless hidden-gem criteria are met"
                ),
                edge=edge,
                confidence=confidence,
                entry_price=entry_price,
                win_probability=win_probability,
                reasoning=reasoning,
            )

    duplicate_reason = _event_blocker_reason(event_snapshot, retrade_policy)
    if duplicate_reason is not None:
        return TradeDecision(
            action="SKIP",
            approved=False,
            reason_code=duplicate_reason[0],
            reason=duplicate_reason[1],
            edge=edge,
            confidence=confidence,
            entry_price=entry_price,
            win_probability=win_probability,
            reasoning=reasoning,
        )

    if event_snapshot["retrade"]:
        required_retrade_edge = min_edge + retrade_policy["retrade_edge_premium"]
        required_retrade_confidence = min_confidence + retrade_policy["retrade_confidence_premium"]
        reasoning["retrade"].update(
            {
                "retrade_edge_threshold": required_retrade_edge,
                "retrade_confidence_threshold": required_retrade_confidence,
            }
        )
        if edge < required_retrade_edge:
            return TradeDecision(
                action="SKIP",
                approved=False,
                reason_code="retrade_edge_below_threshold",
                reason=f"Retrade edge {edge:.4f} below minimum {required_retrade_edge:.4f}",
                edge=edge,
                confidence=confidence,
                entry_price=entry_price,
                win_probability=win_probability,
                reasoning=reasoning,
            )
        if confidence < required_retrade_confidence:
            return TradeDecision(
                action="SKIP",
                approved=False,
                reason_code="retrade_confidence_below_threshold",
                reason=(
                    f"Retrade confidence {confidence:.4f} below minimum {required_retrade_confidence:.4f}"
                ),
                edge=edge,
                confidence=confidence,
                entry_price=entry_price,
                win_probability=win_probability,
                reasoning=reasoning,
            )

    account_metadata = dict(context.account_state.metadata or {})
    effective_tradable_cash = account_metadata.get("effective_tradable_cash", context.account_state.available_cash)
    sizing_cash = max(0.0, float(effective_tradable_cash))
    requested_size = float(kelly_sizer.calculate(win_probability, entry_price, sizing_cash) or 0.0)
    reasoning["kelly"] = {
        "bankroll": round(sizing_cash, 2),
        "requested_size": requested_size,
        "available_cash": round(context.account_state.available_cash, 2),
    }

    if not isfinite(requested_size) or requested_size <= 0:
        return TradeDecision(
            action="SKIP",
            approved=False,
            reason_code="kelly_zero_size",
            reason=(
                f"Kelly rejected size {requested_size:.2f} "
                f"(wp={win_probability:.3f}, ep={entry_price:.3f}, cash={sizing_cash:.2f})"
            ),
            edge=edge,
            confidence=confidence,
            entry_price=entry_price,
            win_probability=win_probability,
            requested_position_size=requested_size,
            reasoning=reasoning,
        )

    requested_size = apply_event_sizing(requested_size, event_snapshot, retrade_policy, reasoning)
    if requested_size <= 0:
        return TradeDecision(
            action="SKIP",
            approved=False,
            reason_code="event_headroom_below_minimum",
            reason="Event headroom left no minimum-size retrade",
            edge=edge,
            confidence=confidence,
            entry_price=entry_price,
            win_probability=win_probability,
            requested_position_size=requested_size,
            reasoning=reasoning,
        )

    if event_snapshot["retrade"]:
        cost_summary = estimate_retrade_costs(
            edge=edge,
            win_probability=win_probability,
            entry_price=entry_price,
            position_size=requested_size,
            event_snapshot=event_snapshot,
            retrade_policy=retrade_policy,
            context=context,
        )
        reasoning["retrade"].update(cost_summary)
        fee_aware_net_edge = float(cost_summary["fee_aware_net_edge"])
        if fee_aware_net_edge < retrade_policy["min_retrade_net_edge"]:
            return TradeDecision(
                action="SKIP",
                approved=False,
                reason_code="retrade_net_edge_below_threshold",
                reason=(
                    f"Retrade net edge {fee_aware_net_edge:.4f} below minimum "
                    f"{retrade_policy['min_retrade_net_edge']:.4f}"
                ),
                edge=edge,
                confidence=confidence,
                entry_price=entry_price,
                win_probability=win_probability,
                requested_position_size=requested_size,
                reasoning=reasoning,
            )
        min_expected_profit = float(retrade_policy.get("min_retrade_expected_profit_usd", 0.0) or 0.0)
        if float(cost_summary["expected_profit_usd"]) < min_expected_profit:
            return TradeDecision(
                action="SKIP",
                approved=False,
                reason_code="retrade_expected_profit_below_threshold",
                reason=(
                    f"Retrade expected profit ${float(cost_summary['expected_profit_usd']):.2f} below minimum "
                    f"${min_expected_profit:.2f}"
                ),
                edge=edge,
                confidence=confidence,
                entry_price=entry_price,
                win_probability=win_probability,
                requested_position_size=requested_size,
                reasoning=reasoning,
            )

    source_signal = dict(context.source_context or {})
    risk_decision = risk_policy.check_trade(
        source_signal,
        requested_size,
        available_cash=context.account_state.available_cash,
    )
    reasoning["risk"] = {
        "approved": bool(getattr(risk_decision, "approved", False)),
        "reason": getattr(risk_decision, "reason", ""),
        "risk_score": float(getattr(risk_decision, "risk_score", 0.0) or 0.0),
        "warnings": list(getattr(risk_decision, "warnings", []) or []),
        "adjusted_size": _coerce_optional_float(getattr(risk_decision, "adjusted_size", None)),
    }

    if not getattr(risk_decision, "approved", False):
        reason = getattr(risk_decision, "reason", "Risk rejected")
        return TradeDecision(
            action="SKIP",
            approved=False,
            reason_code=reason_to_key(reason, prefix="risk"),
            reason=reason,
            edge=edge,
            confidence=confidence,
            entry_price=entry_price,
            win_probability=win_probability,
            requested_position_size=requested_size,
            risk_score=float(getattr(risk_decision, "risk_score", 0.0) or 0.0),
            warnings=list(getattr(risk_decision, "warnings", []) or []),
            reasoning=reasoning,
        )

    return TradeDecision(
        action=direction,
        approved=True,
        reason_code="approved",
        reason=getattr(risk_decision, "reason", "Approved"),
        edge=edge,
        confidence=confidence,
        entry_price=entry_price,
        win_probability=win_probability,
        requested_position_size=requested_size,
        position_size=_coerce_optional_float(getattr(risk_decision, "adjusted_size", requested_size)),
        risk_score=float(getattr(risk_decision, "risk_score", 0.0) or 0.0),
        warnings=list(getattr(risk_decision, "warnings", []) or []),
        reasoning=reasoning,
    )


def build_event_snapshot(context: TradeContext) -> dict[str, Any]:
    metadata = dict(context.metadata or {})
    snapshot = dict(metadata.get("event_snapshot") or {})
    signal = dict(context.source_context or {})
    event_key = str(snapshot.get("event_key") or metadata.get("event_key") or trade_event_key(signal))
    current_balance = max(0.0, _coerce_optional_float(context.account_state.current_balance, 0.0) or 0.0)
    max_event_exposure_pct = _coerce_optional_float(snapshot.get("max_event_exposure_pct"), 0.10) or 0.10
    has_explicit_snapshot = bool(snapshot)
    max_event_exposure = round(current_balance * max_event_exposure_pct, 2) if has_explicit_snapshot else float("inf")
    event_exposure_before = round(_coerce_optional_float(snapshot.get("event_exposure_before"), 0.0) or 0.0, 2)
    event_headroom = round(max(0.0, max_event_exposure - event_exposure_before), 2) if has_explicit_snapshot else float("inf")
    event_position_count_before = int(snapshot.get("event_position_count_before") or 0)
    return {
        "event_key": event_key,
        "candidate_market_id": str(context.market_id or signal.get("market_id") or ""),
        "candidate_direction": str(context.direction or signal.get("direction") or "BUY_YES").upper(),
        "candidate_family_key": str(snapshot.get("candidate_family_key") or metadata.get("market_family_key") or ""),
        "event_position_count_before": event_position_count_before,
        "event_exposure_before": event_exposure_before,
        "event_headroom": event_headroom,
        "max_event_exposure": max_event_exposure,
        "held_market_ids": list(snapshot.get("held_market_ids") or []),
        "same_event_directions": list(snapshot.get("same_event_directions") or []),
        "same_family_markets": list(snapshot.get("same_family_markets") or []),
        "same_family_positions": list(snapshot.get("same_family_positions") or []),
        "retrade": event_position_count_before > 0,
        "opposite_side_detected": bool(snapshot.get("opposite_side_detected", False)),
        "overlap_penalty": float(snapshot.get("overlap_penalty", 1.0) or 1.0),
        "event_entries_count": int(snapshot.get("event_entries_count") or event_position_count_before),
        "estimated_slippage": float(snapshot.get("estimated_slippage", 0.0) or 0.0),
        "estimated_fill_price": _coerce_optional_float(snapshot.get("estimated_fill_price")),
        "liquidity": _coerce_optional_float(snapshot.get("liquidity")),
        "best_yes_ask": _coerce_optional_float(snapshot.get("best_yes_ask")),
        "best_no_ask": _coerce_optional_float(snapshot.get("best_no_ask")),
        "best_yes_bid": _coerce_optional_float(snapshot.get("best_yes_bid")),
        "best_no_bid": _coerce_optional_float(snapshot.get("best_no_bid")),
        "event_entry_prices": list(snapshot.get("event_entry_prices") or []),
        "best_same_market_entry_price": _coerce_optional_float(snapshot.get("best_same_market_entry_price")),
        "best_same_family_entry_price": _coerce_optional_float(snapshot.get("best_same_family_entry_price")),
        "filled_event_exposure_before": round(_coerce_optional_float(snapshot.get("filled_event_exposure_before"), event_exposure_before) or 0.0, 2),
        "pending_event_exposure_before": round(_coerce_optional_float(snapshot.get("pending_event_exposure_before"), 0.0) or 0.0, 2),
        "filled_event_position_count_before": int(snapshot.get("filled_event_position_count_before") or event_position_count_before),
        "pending_event_position_count_before": int(snapshot.get("pending_event_position_count_before") or 0),
        "max_event_exposure_pct": max_event_exposure_pct,
    }


def apply_event_sizing(
    requested_size: float,
    event_snapshot: dict[str, Any],
    retrade_policy: dict[str, float | bool],
    reasoning: dict[str, Any],
) -> float:
    adjusted_size = float(requested_size)
    existing_positions = event_snapshot["event_position_count_before"]
    size_decay_applied = 1.0
    if event_snapshot["retrade"]:
        size_decay_applied = float(retrade_policy["retrade_size_decay"]) ** existing_positions
        adjusted_size *= size_decay_applied
    overlap_penalty = float(event_snapshot.get("overlap_penalty", 1.0) or 1.0)
    adjusted_size *= overlap_penalty
    adjusted_size = min(adjusted_size, event_snapshot["event_headroom"])
    adjusted_size = round(adjusted_size, 2)
    reasoning["retrade"]["size_decay_applied"] = round(size_decay_applied, 6)
    reasoning["retrade"]["size_after_event_clipping"] = adjusted_size
    return adjusted_size


def estimate_retrade_costs(
    *,
    edge: float,
    win_probability: float,
    entry_price: float,
    position_size: float,
    event_snapshot: dict[str, Any],
    retrade_policy: dict[str, float | bool],
    context: TradeContext,
) -> dict[str, float]:
    fee_rate = float(retrade_policy.get("fee_rate", 0.07) or 0.07)
    liquidity = _coerce_optional_float(event_snapshot.get("liquidity"))
    if liquidity is None:
        liquidity = _coerce_optional_float((context.source_context or {}).get("liquidity"))
    slippage = float(event_snapshot.get("estimated_slippage", 0.0) or 0.0)
    if liquidity and liquidity > 0 and position_size > 0:
        slippage = max(slippage, min((position_size / max(liquidity, 1.0)) * 0.10, 0.03))
    estimated_fill_price = _coerce_optional_float(event_snapshot.get("estimated_fill_price"))
    if estimated_fill_price is None:
        estimated_fill_price = min(0.99, max(0.01, entry_price + slippage))
    effective_entry_price = max(entry_price, estimated_fill_price)
    fee_edge_drag = max(0.0, (1.0 - effective_entry_price) * fee_rate)
    net_edge = edge - fee_edge_drag - (slippage * 2)
    gross_profit_per_dollar = max(0.0, (win_probability / max(effective_entry_price, 0.0001)) - 1.0)
    expected_profit_usd = position_size * net_edge
    return {
        "liquidity": round(liquidity, 4) if liquidity is not None else 0.0,
        "estimated_slippage": round(slippage, 6),
        "estimated_fill_price": round(effective_entry_price, 6),
        "fee_edge_drag": round(fee_edge_drag, 6),
        "gross_profit_per_dollar": round(gross_profit_per_dollar, 6),
        "fee_aware_net_edge": round(net_edge, 6),
        "expected_profit_usd": round(expected_profit_usd, 6),
    }


def _event_blocker_reason(event_snapshot: dict[str, Any], retrade_policy: dict[str, float | bool]):
    candidate_market_id = event_snapshot["candidate_market_id"]
    if candidate_market_id and candidate_market_id in set(event_snapshot["held_market_ids"]):
        return "duplicate_market_id_open", f"Duplicate unresolved market {candidate_market_id}"
    if event_snapshot["event_position_count_before"] >= int(retrade_policy["max_event_positions"]):
        return (
            "event_position_limit_reached",
            f"Event already has {event_snapshot['event_position_count_before']} unresolved positions",
        )
    if event_snapshot["event_exposure_before"] >= event_snapshot["max_event_exposure"] and event_snapshot["max_event_exposure"] > 0:
        return "event_exposure_limit_reached", f"Event exposure cap reached for {event_snapshot['event_key']}"
    if event_snapshot["opposite_side_detected"]:
        return "opposite_side_same_event_blocked", f"Opposite-side same-event entry blocked for {event_snapshot['event_key']}"
    if bool(retrade_policy["strict_event_overlap"]) and candidate_market_id in set(event_snapshot["same_family_markets"]):
        return "same_event_family_duplicate", f"Same-event market family already open for {event_snapshot['event_key']}"
    if event_snapshot["retrade"] and bool(retrade_policy.get("require_price_improvement_for_same_market_family", False)):
        best_same_family_entry_price = _coerce_optional_float(event_snapshot.get("best_same_family_entry_price"))
        improvement_ticks = float(retrade_policy.get("price_improvement_ticks", 0.0) or 0.0)
        candidate_price = _candidate_entry_price_for_improvement(event_snapshot)
        if best_same_family_entry_price is not None and candidate_price is not None:
            price_improvement = round(best_same_family_entry_price - candidate_price, 6)
            if price_improvement < improvement_ticks:
                return (
                    "same_family_price_not_improved",
                    (
                        f"Same-family retrade needs {improvement_ticks:.4f} better price, got {price_improvement:.4f} "
                        f"for {event_snapshot['event_key']}"
                    ),
                )
    return None


def _candidate_entry_price_for_improvement(event_snapshot: dict[str, Any]) -> float | None:
    direction = str(event_snapshot.get("candidate_direction") or "BUY_YES").upper()
    if direction == "BUY_NO":
        return _coerce_optional_float(event_snapshot.get("best_no_ask"))
    return _coerce_optional_float(event_snapshot.get("best_yes_ask"))


def _retrade_policy_for_context(context: TradeContext) -> dict[str, float | bool]:
    metadata = dict(context.metadata or {})
    policy = dict(DEFAULT_RETRADE_POLICY)
    policy.update(dict(metadata.get("retrade_policy") or {}))
    return policy


def normalize_trade_context(context: TradeContext) -> dict[str, float | str] | None:
    """Normalize a trade context into side-specific price and win probability."""

    yes_price = _coerce_optional_float(context.yes_price)
    if yes_price is None:
        yes_price = _coerce_optional_float(context.market_price)
    model_probability = _coerce_optional_float(context.model_probability, default=0.5)
    if yes_price is None or model_probability is None:
        return None
    if not (0 < yes_price < 1) or not (0 <= model_probability <= 1):
        return None

    direction = str(context.direction or "BUY_YES").upper()
    if direction == "BUY_NO":
        no_price = _coerce_optional_float(context.no_price)
        entry_price = no_price if no_price is not None and 0 < no_price < 1 else 1 - yes_price
        win_probability = 1 - model_probability
    else:
        direction = "BUY_YES"
        entry_price = yes_price
        win_probability = model_probability

    if not (0 < entry_price < 1) or not (0 <= win_probability <= 1):
        return None

    return {
        "direction": direction,
        "entry_price": entry_price,
        "win_probability": win_probability,
    }


def reason_to_key(reason: str, *, prefix: str = "") -> str:
    """Convert a free-form reason string into a stable blocker code."""

    cleaned = (reason or "rejected").lower()
    for old, new in (
        ("%", "pct"),
        ("$", "usd"),
        ("/", "_"),
        ("(", ""),
        (")", ""),
        ("-", "_"),
    ):
        cleaned = cleaned.replace(old, new)
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in cleaned)
    stem = "_".join(part for part in cleaned.split("_") if part)
    return f"{prefix}_{stem}" if prefix else stem


def _coerce_optional_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        coerced = float(value)
    except (TypeError, ValueError):
        return default
    return coerced if isfinite(coerced) else default
