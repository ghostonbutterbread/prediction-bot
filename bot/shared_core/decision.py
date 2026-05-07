"""Thin shared decision path used by paper today and live later."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any, Protocol

from bot.trade_audit import trade_event_key
from bot.strategy_lanes import select_strategy_lane
from bot.strategy_policy import coerce_strategy_policy, strategy_policy_status

from .interfaces import TradeContext, TradeDecision
from .weather_risk import (
    apply_weather_size_limits,
    assess_weather_market_risk,
    build_weather_source_confidence_evidence,
    classify_weather_market,
)


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
WEATHER_EVIDENCE_CARD_FEATURE = "weather_hidden_gem_evidence_card"
WEATHER_BUCKET_SCORING_FEATURE = "bucket_distribution_scoring"


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
    source_signal = dict(context.source_context or {})
    strategy_policy = _strategy_policy_for_context(context, source_signal)
    reasoning["strategy_policy_status"] = strategy_policy_status(strategy_policy)
    reasoning["market_route_enforcement"] = "stable_required"
    reasoning["market_route_required"] = True

    reasoning["normalized"] = {
        "direction": direction,
        "entry_price": entry_price,
        "win_probability": win_probability,
    }

    route_rejection = _market_route_rejection(context, source_signal)
    if route_rejection is not None:
        reason_code, reason, route = route_rejection
        reasoning["market_route"] = route
        return TradeDecision(
            action="SKIP",
            approved=False,
            reason_code=reason_code,
            reason=reason,
            edge=edge,
            confidence=confidence,
            entry_price=entry_price,
            win_probability=win_probability,
            reasoning=reasoning,
        )
    route_metadata = _route_metadata(context, source_signal)
    if route_metadata is not None:
        reasoning["market_route"] = route_metadata

    strategy_lane = select_strategy_lane(
        entry_price=entry_price,
        win_probability=win_probability,
        edge=edge,
        confidence=confidence,
        min_edge=float(min_edge),
        min_confidence=float(min_confidence),
        hidden_gem_entry_price_cap=HIDDEN_GEM_ENTRY_PRICE_CAP,
        config=_strategy_lane_config(context, source_signal),
        strategy_policy=strategy_policy,
    )
    reasoning["strategy_lane"] = strategy_lane.to_dict()
    if not strategy_lane.allowed:
        return TradeDecision(
            action="SKIP",
            approved=False,
            reason_code=strategy_lane.reason_code,
            reason=f"Strategy lane rejected: {strategy_lane.lane_id}",
            edge=edge,
            confidence=confidence,
            entry_price=entry_price,
            win_probability=win_probability,
            reasoning=reasoning,
        )

    effective_min_edge = strategy_lane.effective_min_edge
    effective_min_confidence = strategy_lane.effective_min_confidence
    reasoning["thresholds"]["effective_min_edge"] = effective_min_edge
    reasoning["thresholds"]["effective_min_confidence"] = effective_min_confidence

    if edge < effective_min_edge:
        return TradeDecision(
            action="SKIP",
            approved=False,
            reason_code="edge_below_threshold",
            reason=f"Edge {edge:.4f} below minimum {effective_min_edge:.4f}",
            edge=edge,
            confidence=confidence,
            entry_price=entry_price,
            win_probability=win_probability,
            reasoning=reasoning,
        )

    if confidence < effective_min_confidence:
        return TradeDecision(
            action="SKIP",
            approved=False,
            reason_code="confidence_below_threshold",
            reason=f"Confidence {confidence:.4f} below minimum {effective_min_confidence:.4f}",
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

    weather_policy = dict((context.metadata or {}).get("weather_risk_policy") or {})
    weather_signal = {
        "market_id": context.market_id,
        "question": context.question,
        **source_signal,
        "candidate_direction": direction,
    }
    is_weather_market = (
        classify_weather_market(str(weather_signal.get("question") or ""), str(weather_signal.get("market_id") or "")) != "unknown"
        or str(weather_signal.get("market_group") or weather_signal.get("group") or "").lower() == "weather"
        or bool(weather_policy)
    )
    if is_weather_market:
        weather_evidence = build_weather_source_confidence_evidence(weather_signal)
        weather_signal = {**weather_signal, **weather_evidence}
        weather_assessment = assess_weather_market_risk(
            weather_signal,
            entry_price=entry_price,
            win_probability=win_probability,
            policy=weather_policy,
        )
        reasoning["weather_risk"] = {
            **weather_assessment.to_dict(),
            "evidence": weather_evidence,
            "beta_gate": _weather_rejection_beta_gate(strategy_policy, weather_assessment),
        }
        if _should_emit_hidden_gem_evidence_card(reasoning):
            reasoning["hidden_gem_evidence_card"] = _build_hidden_gem_evidence_card(
                context=context,
                source_signal=weather_signal,
                route_metadata=route_metadata,
                strategy_policy=strategy_policy,
                strategy_lane=reasoning["strategy_lane"],
                hidden_gem=reasoning["hidden_gem"],
                weather_assessment=weather_assessment,
                weather_evidence=weather_evidence,
                weather_risk=reasoning["weather_risk"],
                entry_price=entry_price,
                win_probability=win_probability,
            )
        if weather_assessment.should_skip and reasoning["weather_risk"]["beta_gate"]["enforced"]:
            return TradeDecision(
                action="SKIP",
                approved=False,
                reason_code=weather_assessment.reason_code or "weather_risk_rejected",
                reason=weather_assessment.reason or "Weather risk gates rejected trade",
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
        required_retrade_edge = effective_min_edge + retrade_policy["retrade_edge_premium"]
        required_retrade_confidence = effective_min_confidence + retrade_policy["retrade_confidence_premium"]
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

    if is_weather_market:
        weather_adjusted_size = apply_weather_size_limits(
            requested_size,
            weather_assessment,
            current_balance=context.account_state.current_balance,
        )
        reasoning["weather_risk"]["beta_sizing_gate"] = _weather_sizing_beta_gate(
            strategy_policy,
            weather_assessment,
            requested_size=requested_size,
            adjusted_size=weather_adjusted_size,
        )
        if isinstance(reasoning.get("hidden_gem_evidence_card"), dict):
            _attach_hidden_gem_sizing_gate(
                reasoning["hidden_gem_evidence_card"],
                reasoning["weather_risk"]["beta_sizing_gate"],
            )
        if weather_adjusted_size != requested_size:
            reasoning["weather_risk"]["requested_size_before_weather_limits"] = round(requested_size, 4)
            reasoning["weather_risk"]["requested_size_after_weather_limits"] = weather_adjusted_size
            if reasoning["weather_risk"]["beta_sizing_gate"]["enforced"]:
                requested_size = weather_adjusted_size

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

    risk_decision = risk_policy.check_trade(
        source_signal,
        requested_size,
        available_cash=context.account_state.available_cash,
    )
    risk_metadata = dict(getattr(risk_decision, "metadata", {}) or {})
    reasoning["risk"] = {
        "approved": bool(getattr(risk_decision, "approved", False)),
        "reason": getattr(risk_decision, "reason", ""),
        "risk_score": float(getattr(risk_decision, "risk_score", 0.0) or 0.0),
        "warnings": list(getattr(risk_decision, "warnings", []) or []),
        "adjusted_size": _coerce_optional_float(getattr(risk_decision, "adjusted_size", None)),
        "metadata": risk_metadata,
    }

    if not getattr(risk_decision, "approved", False):
        reason = getattr(risk_decision, "reason", "Risk rejected")
        reason_code = str(risk_metadata.get("reason_code") or reason_to_key(reason, prefix="risk"))
        return TradeDecision(
            action="SKIP",
            approved=False,
            reason_code=reason_code,
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


def _route_metadata(context: TradeContext, source_signal: dict[str, Any]) -> dict[str, Any] | None:
    for value in (
        (context.metadata or {}).get("market_route"),
        source_signal.get("market_route"),
    ):
        if isinstance(value, dict):
            return dict(value)
    return None


def _market_route_rejection(context: TradeContext, source_signal: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None] | None:
    route = _route_metadata(context, source_signal)
    if not isinstance(route, dict):
        return "missing_market_route", "Market route is required before shared-core approval", None
    if not route.get("allowed"):
        reason_code = str(route.get("reason_code") or "market_route_not_allowed")
        return reason_code, f"Market route rejected: {reason_code}", route
    if not route.get("handler_id"):
        return "missing_market_route_handler", "Market route is missing a handler", route
    return None


def _strategy_lane_config(context: TradeContext, source_signal: dict[str, Any]) -> dict[str, Any] | None:
    for value in (
        (context.metadata or {}).get("strategy_lanes"),
        source_signal.get("strategy_lanes"),
        (context.metadata or {}).get("strategy_lane_config"),
        source_signal.get("strategy_lane_config"),
    ):
        if isinstance(value, dict):
            return dict(value)
    return None


def _strategy_policy_for_context(context: TradeContext, source_signal: dict[str, Any]):
    for value in (
        (context.metadata or {}).get("strategy_policy_normalized"),
        (context.metadata or {}).get("strategy_policy"),
        source_signal.get("strategy_policy_normalized"),
        source_signal.get("strategy_policy"),
    ):
        if isinstance(value, dict) and value:
            return coerce_strategy_policy(value)
    return coerce_strategy_policy(None)


def _weather_rejection_beta_gate(strategy_policy: Any, weather_assessment) -> dict[str, Any]:
    policy = coerce_strategy_policy(strategy_policy)
    reason_code = weather_assessment.reason_code
    feature = _weather_rejection_feature(reason_code)
    active = bool(feature and policy.feature_enabled(feature))
    enforced = bool(feature and policy.feature_enforced(feature))
    return {
        "policy": strategy_policy_status(policy),
        "feature": feature,
        "would_reject": bool(weather_assessment.should_skip),
        "reason_code": reason_code,
        "active": active,
        "shadow": bool(active and policy.is_shadow),
        "enforced": enforced,
        "preserved_stable_action": bool(weather_assessment.should_skip and not enforced),
        "differs_from_final": bool(weather_assessment.should_skip and active and not enforced),
    }


def _weather_sizing_beta_gate(
    strategy_policy: Any,
    weather_assessment,
    *,
    requested_size: float,
    adjusted_size: float,
) -> dict[str, Any]:
    policy = coerce_strategy_policy(strategy_policy)
    features = _weather_sizing_features(weather_assessment)
    would_adjust = adjusted_size != requested_size
    active = bool(would_adjust and features and all(policy.feature_enabled(feature) for feature in features))
    enforced = bool(would_adjust and features and all(policy.feature_enforced(feature) for feature in features))
    return {
        "policy": strategy_policy_status(policy),
        "features": features,
        "would_adjust_size": would_adjust,
        "requested_size": round(float(requested_size), 4),
        "beta_adjusted_size": round(float(adjusted_size), 4),
        "active": active,
        "shadow": bool(active and policy.is_shadow),
        "enforced": enforced,
        "preserved_stable_size": bool(would_adjust and not enforced),
        "differs_from_final": bool(would_adjust and active and not enforced),
    }


def _weather_rejection_feature(reason_code: Any) -> str | None:
    code = str(reason_code or "")
    if code == "weather_bucket_hidden_gem_missing_distribution_probability":
        return WEATHER_BUCKET_SCORING_FEATURE
    if code in {
        "weather_tail_hidden_gem_live_probability_mismatch",
        "weather_hidden_gem_without_strong_evidence",
        "weather_extreme_disagreement_without_perfect_evidence",
    }:
        return WEATHER_EVIDENCE_CARD_FEATURE
    return None


def _weather_sizing_features(weather_assessment) -> list[str]:
    flags = set(weather_assessment.flags or [])
    features: set[str] = set()
    if weather_assessment.shape == "bucket" and (
        "narrow_bucket" in flags
        or weather_assessment.max_position_pct is not None
        or weather_assessment.max_position_usd is not None
    ):
        features.add(WEATHER_BUCKET_SCORING_FEATURE)
    if weather_assessment.hidden_gem_tier in {"suspicious", "exceptional"} or any(
        str(flag).startswith("extreme_disagreement") for flag in flags
    ):
        features.add(WEATHER_EVIDENCE_CARD_FEATURE)
    return sorted(features)


def _should_emit_hidden_gem_evidence_card(reasoning: dict[str, Any]) -> bool:
    lane = reasoning.get("strategy_lane")
    hidden_gem = reasoning.get("hidden_gem")
    return (
        isinstance(lane, dict)
        and lane.get("lane_id") == "hidden_gem"
        and isinstance(hidden_gem, dict)
        and bool(hidden_gem.get("triggered"))
    )


def _build_hidden_gem_evidence_card(
    *,
    context: TradeContext,
    source_signal: dict[str, Any],
    route_metadata: dict[str, Any] | None,
    strategy_policy: Any,
    strategy_lane: dict[str, Any],
    hidden_gem: dict[str, Any],
    weather_assessment: Any,
    weather_evidence: dict[str, Any],
    weather_risk: dict[str, Any],
    entry_price: float,
    win_probability: float,
) -> dict[str, Any]:
    route = dict(route_metadata or {})
    beta_gate = dict(weather_risk.get("beta_gate") or {})
    shape = weather_assessment.shape
    card = {
        "artifact_version": 1,
        "lane": "hidden_gem",
        "market_id": context.market_id,
        "market_route": route or None,
        "market_group": route.get("group"),
        "market_family": route.get("family"),
        "market_subcategory": route.get("subcategory"),
        "route_handler_id": route.get("handler_id"),
        "weather_shape": shape,
        "entry_price": round(float(entry_price), 6),
        "probability_multiple": hidden_gem.get("probability_multiple"),
        "model_probability": _coerce_optional_float(context.model_probability),
        "candidate_probability": round(float(win_probability), 6),
        "source_mode": _hidden_gem_source_mode(source_signal),
        "station_mapping_quality": weather_evidence.get("weather_station_mapping"),
        "station_mapping": weather_evidence.get("weather_station_resolution"),
        "source_agreement_score": weather_evidence.get("source_agreement_score"),
        "source_freshness": _source_freshness(source_signal),
        "market_volume": weather_evidence.get("market_volume"),
        "liquidity": _first_float(source_signal, "liquidity", "market_liquidity"),
        "volume_known": bool(weather_assessment.volume_known),
        "weather_confidence_score": weather_evidence.get("weather_confidence_score"),
        "hidden_gem_tier": weather_assessment.hidden_gem_tier,
        "weather_flags": list(weather_assessment.flags or []),
        "strategy_lane": {
            "reason_code": strategy_lane.get("reason_code"),
            "behavior_enabled": bool(strategy_lane.get("behavior_enabled")),
            "new_behavior_enabled": bool(strategy_lane.get("new_behavior_enabled")),
        },
        "strategy_policy": strategy_policy_status(strategy_policy),
        "reason_codes": {
            "weather_reject": weather_assessment.reason_code,
            "beta_reject": beta_gate.get("reason_code") if beta_gate.get("active") else None,
            "resize": None,
        },
        "beta_gate": beta_gate,
        "beta_deltas": {
            "rejection_differs_from_final": bool(beta_gate.get("differs_from_final", False)),
            "sizing_differs_from_final": False,
        },
    }
    if shape == "bucket":
        card["bucket"] = _bucket_evidence(source_signal, weather_evidence)
    if shape in {"tail_low", "tail_high"}:
        card["tail"] = _tail_evidence(source_signal, candidate_probability=win_probability)
    return card


def _attach_hidden_gem_sizing_gate(card: dict[str, Any], sizing_gate: dict[str, Any]) -> None:
    card["beta_sizing_gate"] = dict(sizing_gate)
    card.setdefault("reason_codes", {})["resize"] = (
        "weather_size_limit" if sizing_gate.get("enforced") else None
    )
    card.setdefault("reason_codes", {})["potential_resize"] = (
        "weather_size_limit" if sizing_gate.get("active") and not sizing_gate.get("enforced") else None
    )
    card.setdefault("beta_deltas", {})["sizing_differs_from_final"] = bool(
        sizing_gate.get("differs_from_final", False)
    )


def _bucket_evidence(source_signal: dict[str, Any], weather_evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "distribution_probability": weather_evidence.get("distribution_probability"),
        "forecast": _first_present(source_signal, "forecast", "weather_forecast"),
        "forecast_mean": _first_float(
            source_signal,
            "forecast_mean",
            "predicted_temp",
            "current_forecast",
        ),
        "forecast_high": _first_float(source_signal, "forecast_high", "predicted_high"),
        "forecast_low": _first_float(source_signal, "forecast_low", "predicted_low"),
        "forecast_spread": _first_float(
            source_signal,
            "forecast_spread",
            "forecast_uncertainty",
            "forecast_stddev",
            "forecast_sigma",
        ),
        "bucket": _first_present(source_signal, "bucket", "bucket_range"),
        "bucket_center": _first_float(source_signal, "bucket_center", "range_center"),
        "bucket_width": _first_float(source_signal, "bucket_width", "range_width"),
        "distance_to_center": _first_float(source_signal, "distance_to_center", "distance_from_bucket_center"),
    }


def _tail_evidence(source_signal: dict[str, Any], *, candidate_probability: float) -> dict[str, Any]:
    return {
        "threshold": _first_float(source_signal, "threshold", "tail_threshold"),
        "threshold_probability": _first_float(
            source_signal,
            "threshold_probability",
            "tail_threshold_probability",
            "tail_probability",
            "distribution_probability",
        ),
        "candidate_tail_probability": round(float(candidate_probability), 6),
        "raw_live_weather_probability": _first_float(
            source_signal,
            "live_weather_probability",
            "weather_probability",
        ),
        "distance_to_threshold": _first_float(
            source_signal,
            "distance_to_threshold",
            "distance_from_threshold",
            "forecast_threshold_distance",
        ),
    }


def _source_freshness(source_signal: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_agreement_score": _first_float(source_signal, "source_agreement_score", "agreement"),
        "source_age_seconds": _first_float(source_signal, "source_age_seconds", "age_seconds"),
        "source_age_minutes": _first_float(source_signal, "source_age_minutes", "age_minutes", "staleness_minutes"),
        "source_timestamp": _first_present(source_signal, "source_timestamp", "fetched_at", "as_of", "observed_at"),
        "freshness": _first_present(source_signal, "source_freshness", "freshness"),
    }


def _hidden_gem_source_mode(source_signal: dict[str, Any]) -> str:
    explicit = _first_present(source_signal, "source_mode", "source_provenance", "mode")
    normalized = str(explicit or "").strip().lower()
    if "recorded" in normalized:
        return "recorded"
    if "historical" in normalized or "backfill" in normalized or "replay" in normalized:
        return "historical"
    if "synthetic" in normalized or "simulated" in normalized:
        return "synthetic"
    if "live" in normalized or "current" in normalized:
        return "live"

    source_snapshot = source_signal.get("source_snapshot")
    if isinstance(source_snapshot, Mapping):
        source = str(source_snapshot.get("source") or "").lower()
        mode = str(source_snapshot.get("mode") or "").lower()
        if source == "missing":
            return "missing"
        if "recorded" in source or "recorded" in mode:
            return "recorded"
        if "historical" in source or "historical" in mode:
            return "historical"
        if "synthetic" in source or "synthetic" in mode:
            return "synthetic"
        if "live" in source or "live" in mode:
            return "live"
    return "missing"


def _first_float(source_signal: dict[str, Any], *keys: str) -> float | None:
    for mapping in _iter_signal_mappings(source_signal):
        for key in keys:
            value = _coerce_optional_float(mapping.get(key))
            if value is not None:
                return value
    return None


def _first_present(source_signal: dict[str, Any], *keys: str) -> Any:
    for mapping in _iter_signal_mappings(source_signal):
        for key in keys:
            value = mapping.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _iter_signal_mappings(source_signal: Mapping[str, Any]):
    nested_keys = (
        "data",
        "weather",
        "weather_context",
        "weather_market_context",
        "metadata",
        "forecast",
        "bucket",
        "signal_details",
        "live",
    )
    seen: set[int] = set()

    def visit(mapping: Mapping[str, Any], depth: int):
        marker = id(mapping)
        if marker in seen:
            return
        seen.add(marker)
        yield mapping
        if depth <= 0:
            return
        for nested_key in nested_keys:
            nested = mapping.get(nested_key)
            if isinstance(nested, Mapping):
                yield from visit(nested, depth - 1)
                for value in nested.values():
                    if isinstance(value, Mapping):
                        yield from visit(value, depth - 1)

    yield from visit(source_signal, 4)


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
