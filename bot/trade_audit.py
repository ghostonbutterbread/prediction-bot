"""Trade accounting audit helpers shared by simulator, resolver, and reports."""

from __future__ import annotations

from collections import defaultdict
from math import isfinite
from typing import Optional

from bot.market_classification import is_weather_market


VALID_DIRECTIONS = {"BUY_YES", "BUY_NO"}
VALID_OUTCOMES = {"YES", "NO"}


def coerce_float(value, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if value is None:
            return default
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if isfinite(value) else default


def is_trade_effective_row(trade: dict) -> bool:
    market_id = str(trade.get("market_id", "") or "").strip()
    size = coerce_float(trade.get("position_size"), default=None)
    return bool(market_id) and size is not None and size > 0


def normalize_outcome(value) -> Optional[str]:
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
            "WIN": "YES",
            "LOSE": "NO",
            "WON": "YES",
            "LOST": "NO",
            "1": "YES",
            "0": "NO",
        }
        return aliases.get(value.strip().upper())
    return None


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
    if is_weather_market(market_id=market_id, question=question, category=category):
        parts = market_id.split("-")
        if len(parts) >= 3:
            return "-".join(parts[:2])

    return market_id


def enrich_trade_audit_fields(trade: dict, fee_rate: float = 0.07) -> dict:
    issues: list[str] = []

    trade["event_key"] = trade_event_key(trade)

    direction = str(trade.get("direction", "") or "").upper()
    if direction:
        trade["direction"] = direction

    size = coerce_float(trade.get("position_size"), default=None)
    if size is not None and size > 0:
        trade["position_size"] = round(size, 2)
    elif trade.get("resolved"):
        issues.append("invalid_position_size")

    reserved_capital = coerce_float(trade.get("reserved_capital"), default=None)
    if reserved_capital is None and size is not None and size > 0 and not trade.get("resolved"):
        reserved_capital = size
    if reserved_capital is not None and reserved_capital >= 0:
        trade["reserved_capital"] = round(reserved_capital, 2)

    entry_price = coerce_float(trade.get("market_price"), default=None)
    if entry_price is not None and 0 < entry_price < 1:
        trade["market_price"] = round(entry_price, 4)
        if size is not None and size > 0:
            trade["contracts"] = round(calculate_contracts(entry_price, size), 4)
    elif trade.get("resolved"):
        issues.append("invalid_market_price")

    if not trade.get("resolved"):
        trade["integrity_status"] = "ok" if not issues else "invalid"
        trade["integrity_errors"] = sorted(set(issues))
        return trade

    if direction not in VALID_DIRECTIONS:
        issues.append("invalid_direction")

    outcome = normalize_outcome(trade.get("outcome"))
    if outcome is None:
        issues.append("invalid_outcome")
    else:
        trade["outcome"] = outcome

    if not trade.get("resolved_at"):
        issues.append("missing_resolved_at")

    reported_pnl = coerce_float(trade.get("pnl"), default=None)
    if (
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
    return {
        "resolved_events": total,
        "wins": wins,
        "losses": losses,
        "flat": flats,
        "win_rate": round(wins / total * 100, 1) if total else 0.0,
        "total_pnl": round(sum(event_pnls), 4) if event_pnls else 0.0,
        "avg_pnl_per_event": round(sum(event_pnls) / total, 4) if total else 0.0,
    }
