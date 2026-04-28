"""Operator-facing notification formatting for trading lifecycle events."""

from __future__ import annotations

from typing import Any


VERBOSITY_NORMAL = "normal"
VERBOSITY_VERBOSE = "verbose"
VERBOSITY_DOUBLE = "double_verbose"


def normalize_verbosity(level: str | None) -> str:
    level = str(level or VERBOSITY_NORMAL).strip().lower()
    if level in {"vv", "double", "double_verbose", "double-verbose"}:
        return VERBOSITY_DOUBLE
    if level in {"v", "verbose"}:
        return VERBOSITY_VERBOSE
    return VERBOSITY_NORMAL


def build_notification(event_type: str, details: dict[str, Any] | None = None, *, verbosity: str = VERBOSITY_NORMAL) -> str | None:
    details = details or {}
    verbosity = normalize_verbosity(verbosity)

    if event_type == "trade_placed":
        return _format_trade_placed(details, verbosity)
    if event_type == "single_trade_completed":
        return _format_single_trade_completed(details, verbosity)
    if event_type == "positions_resolved":
        return _format_positions_resolved(details, verbosity)
    if event_type == "hourly_summary":
        return _format_hourly_summary(details, verbosity)
    if event_type == "live_runtime_state_changed":
        return _format_live_runtime_state_changed(details, verbosity)
    if verbosity != VERBOSITY_NORMAL and event_type in {"mode_changed", "trading_paused", "trading_resumed", "reconciliation_completed"}:
        return _format_verbose_lifecycle(event_type, details, verbosity)
    return None


def _format_trade_placed(details: dict[str, Any], verbosity: str) -> str:
    base = (
        f"Trade placed: {details.get('direction', '?')} {details.get('market_id', '')}\n"
        f"Amount: ${_money(details.get('size'))} at ${_money(details.get('price'), precision=4)}\n"
        f"Confidence: {_pct(details.get('confidence'))}\n"
        f"Balance: ${_money(details.get('balance_after'))} | In market: ${_money(details.get('reserved_capital'))}"
    )
    if verbosity == VERBOSITY_NORMAL:
        return base
    extra = [
        f"Question: {details.get('question', '')}",
        f"Edge: {_pct(details.get('edge'))}",
        f"Mode: {details.get('mode', 'unknown')}",
    ]
    if verbosity == VERBOSITY_DOUBLE:
        extra.extend(
            [
                f"Available cash: ${_money(details.get('available_cash'))}",
                f"Tradable cap: ${_money(details.get('tradable_cap'))}",
            ]
        )
    return base + "\n" + "\n".join(extra)


def _format_single_trade_completed(details: dict[str, Any], verbosity: str) -> str:
    base = (
        "Single-trade mode complete. No further new entries will be taken.\n"
        f"Open positions: {details.get('open_positions', 0)} | Open orders: {details.get('open_orders', 0)}"
    )
    if verbosity == VERBOSITY_NORMAL:
        return base
    extra = "Monitoring and resolution tracking will continue."
    if verbosity == VERBOSITY_DOUBLE:
        extra += f" Behavior: {details.get('behavior', '')}"
    return base + "\n" + extra


def _format_positions_resolved(details: dict[str, Any], verbosity: str) -> str:
    markets = details.get("markets", []) or []
    base = (
        f"Resolved {details.get('count', 0)} market(s).\n"
        f"Markets: {', '.join(markets[:3])}"
    )
    if verbosity == VERBOSITY_NORMAL:
        return base
    extra = [f"Exchange: {details.get('exchange', 'unknown')}"]
    if verbosity == VERBOSITY_DOUBLE and len(markets) > 3:
        extra.append(f"All markets: {', '.join(markets)}")
    return base + "\n" + "\n".join(extra)


def _format_hourly_summary(details: dict[str, Any], verbosity: str) -> str:
    base = (
        f"Hourly summary ({details.get('mode', 'unknown')})\n"
        f"Scans: {details.get('scans', 0)} | Signals: {details.get('signals_considered', 0)} | Trades: {details.get('trades_executed', 0)}\n"
        f"Blocked: {details.get('blocked_total', 0)} | Open positions: {details.get('open_positions', 0)} | Errors: {details.get('errors', 0)}"
    )
    runtime_state = (details.get("live_runtime_state") or {}).get("state")
    if runtime_state and runtime_state != "safe":
        base += f"\nLive runtime: {runtime_state}"
    if verbosity == VERBOSITY_NORMAL:
        return base
    blockers = details.get('top_blockers', {}) or {}
    extra = [f"Top blockers: {blockers if blockers else 'none'}"]
    if verbosity == VERBOSITY_DOUBLE:
        extra.append(f"Started at: {details.get('started_at', '')}")
    return base + "\n" + "\n".join(extra)


def _format_live_runtime_state_changed(details: dict[str, Any], verbosity: str) -> str | None:
    state = str(details.get("state") or "safe")
    exchange_state = str(details.get("exchange_state") or state)
    if state == "safe" and verbosity == VERBOSITY_NORMAL:
        return None
    issues = details.get("exchange_issues") or details.get("issues") or []
    base = (
        f"Live runtime state: {state}\n"
        f"Exchange: {details.get('exchange', 'unknown')} ({exchange_state})\n"
        f"Reason: {details.get('reason', '')}"
    )
    if issues:
        base += f"\nIssues: {', '.join(str(issue) for issue in issues[:5])}"
    if verbosity == VERBOSITY_DOUBLE:
        base += f"\nDetails: {details.get('details', {})}"
    return base


def _format_verbose_lifecycle(event_type: str, details: dict[str, Any], verbosity: str) -> str:
    title = event_type.replace("_", " ").title()
    lines = [title]
    for key in sorted(details):
        lines.append(f"{key}: {details[key]}")
    if verbosity == VERBOSITY_DOUBLE:
        lines.append("verbosity: double_verbose")
    return "\n".join(lines)


def _money(value: Any, precision: int = 2) -> str:
    try:
        return f"{float(value or 0):.{precision}f}"
    except (TypeError, ValueError):
        return f"{0:.{precision}f}"


def _pct(value: Any) -> str:
    try:
        return f"{float(value or 0) * 100:.1f}%"
    except (TypeError, ValueError):
        return "0.0%"
