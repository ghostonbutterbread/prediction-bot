"""Shared settlement outcome normalization helpers."""

from __future__ import annotations

from typing import Any, Mapping, Optional


DEFINITIVE_SETTLEMENT_STATUSES = frozenset({"settled", "resolved", "finalized"})
VOID_MARKET_STATUSES = frozenset({"cancelled", "canceled", "voided", "void"})

_OUTCOME_ALIASES = {
    "YES": "YES",
    "NO": "NO",
    "TRUE": "YES",
    "FALSE": "NO",
    "WIN": "YES",
    "LOSE": "NO",
    "WON": "YES",
    "LOST": "NO",
    "1": "YES",
    "1.0": "YES",
    "0": "NO",
    "0.0": "NO",
    "VOID": "VOID",
    "VOIDED": "VOID",
    "CANCELLED": "VOID",
    "CANCELED": "VOID",
}


def normalize_outcome_alias(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return "YES" if value else "NO"
    if isinstance(value, (int, float)) and value in (0, 1):
        return "YES" if int(value) == 1 else "NO"
    if isinstance(value, str):
        return _OUTCOME_ALIASES.get(value.strip().upper())
    return None


def detect_market_outcome(market_like: Any) -> Optional[str]:
    for key in ("result", "outcome", "settlement_value"):
        normalized = normalize_outcome_alias(_get_market_value(market_like, key))
        if normalized is not None:
            return normalized

    status = market_resolution_status(market_like)
    if status in VOID_MARKET_STATUSES:
        return "VOID"

    if status in DEFINITIVE_SETTLEMENT_STATUSES:
        normalized = normalize_outcome_alias(_get_market_value(market_like, "close_price"))
        if normalized in {"YES", "NO"}:
            return normalized

    return None


def has_definitive_market_outcome(market_like: Any) -> bool:
    return detect_market_outcome(market_like) is not None


def market_resolution_status(market_like: Any) -> str:
    status = _get_market_value(market_like, "status")
    return str(status or "").strip().lower()


def _get_market_value(market_like: Any, key: str) -> Any:
    if isinstance(market_like, Mapping):
        if key in market_like:
            return market_like.get(key)
        metadata = market_like.get("metadata")
        if isinstance(metadata, Mapping):
            return metadata.get(key)
        return None

    if hasattr(market_like, key):
        return getattr(market_like, key)
    metadata = getattr(market_like, "metadata", None)
    if isinstance(metadata, Mapping):
        return metadata.get(key)
    return None
