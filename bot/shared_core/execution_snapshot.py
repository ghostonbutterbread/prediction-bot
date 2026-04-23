from __future__ import annotations

from typing import Any


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value


def build_execution_snapshot(
    signal: dict[str, Any],
    *,
    direction: str,
    bid_ask: dict[str, Any] | None = None,
    fallback_to_signal_prices: bool = True,
) -> dict[str, Any]:
    """Normalize execution-time price inputs for both live and paper.

    Returns a canonical snapshot containing side-aware prices plus metadata about
    where the pricing came from.
    """

    direction = str(direction or signal.get("direction", "BUY_YES") or "BUY_YES").upper()
    source = "book" if bid_ask else "missing"

    raw_market_price = _coerce_float(signal.get("market_price"))
    signal_yes = _coerce_float(signal.get("yes_price"))
    signal_no = _coerce_float(signal.get("no_price"))
    if signal_no is None:
        signal_no = _coerce_float(signal.get("no_market_price"))

    best_yes_ask = _coerce_float((bid_ask or {}).get("best_yes_ask"))
    best_no_ask = _coerce_float((bid_ask or {}).get("best_no_ask"))
    best_yes_bid = _coerce_float((bid_ask or {}).get("best_yes_bid"))
    best_no_bid = _coerce_float((bid_ask or {}).get("best_no_bid"))

    if best_yes_ask is None and fallback_to_signal_prices:
        if signal_yes is not None:
            best_yes_ask = signal_yes
            source = "fallback"
        elif raw_market_price is not None and direction != "BUY_NO":
            best_yes_ask = raw_market_price
            source = "fallback"
    if best_no_ask is None and fallback_to_signal_prices:
        if signal_no is not None:
            best_no_ask = signal_no
            source = "fallback"
        elif raw_market_price is not None and direction == "BUY_NO":
            best_no_ask = raw_market_price
            source = "fallback"

    if best_yes_ask is None and best_no_ask is not None:
        best_yes_ask = round(1 - best_no_ask, 4)
    if best_no_ask is None and best_yes_ask is not None:
        best_no_ask = round(1 - best_yes_ask, 4)

    if best_yes_bid is None and best_yes_ask is not None:
        best_yes_bid = max(0.0, round(best_yes_ask - 0.01, 4))
    if best_no_bid is None and best_no_ask is not None:
        best_no_bid = max(0.0, round(best_no_ask - 0.01, 4))

    market_price = best_no_ask if direction == "BUY_NO" else best_yes_ask
    estimated_fill_price = market_price

    return {
        "source": source,
        "direction": direction,
        "market_price": market_price,
        "yes_price": best_yes_ask,
        "no_price": best_no_ask,
        "best_yes_ask": best_yes_ask,
        "best_no_ask": best_no_ask,
        "best_yes_bid": best_yes_bid,
        "best_no_bid": best_no_bid,
        "estimated_fill_price": estimated_fill_price,
    }
