"""Thin shared decision path used by paper today and live later."""

from __future__ import annotations

from math import isfinite
from typing import Any, Protocol

from .interfaces import TradeContext, TradeDecision


class KellySizerLike(Protocol):
    def calculate(self, win_probability: float, entry_price: float, bankroll: float) -> float:
        ...


class RiskPolicyLike(Protocol):
    def check_trade(self, signal: dict[str, Any], position_size: float, *, available_cash: float | None = None):
        ...


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

    sizing_cash = max(0.0, context.account_state.available_cash)
    requested_size = float(kelly_sizer.calculate(win_probability, entry_price, sizing_cash) or 0.0)
    reasoning["kelly"] = {
        "bankroll": round(sizing_cash, 2),
        "requested_size": requested_size,
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
