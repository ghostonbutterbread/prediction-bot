"""Read-only live canary preflight checks.

This module intentionally performs static config validation only. It does not
instantiate exchanges, read credential files, or inspect credential env vars.
"""

from __future__ import annotations

from typing import Any


SAFE_MAX_TRADABLE_BALANCE_USD = 100.0
SAFE_MIN_POSITION_SIZE_USD = 1.0
SAFE_MAX_POSITION_SIZE_USD = 5.0
SAFE_MAX_DAILY_LOSS_LIMIT_PCT = 0.05
SAFE_MAX_DRAWDOWN_PCT = 0.10
SAFE_MAX_OPEN_POSITIONS = 3


def _nested(config: dict[str, Any], *keys: str, default: Any = None) -> Any:
    node: Any = config
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _is_explicit_false(value: Any) -> bool:
    return value is False


def _is_explicit_true(value: Any) -> bool:
    return value is True


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _add_check(report: dict[str, Any], name: str, ok: bool, message: str, *, value: Any = None) -> None:
    check = {"name": name, "ok": bool(ok), "message": message}
    if value is not None:
        check["value"] = value
    report["checks"].append(check)
    if not ok:
        report["issues"].append(message)


def _risk_value(config: dict[str, Any], key: str) -> Any:
    risk = config.get("risk") or {}
    if isinstance(risk, dict) and key in risk:
        return risk.get(key)
    return config.get(key)


def _validate_fraction(
    report: dict[str, Any],
    *,
    name: str,
    value: Any,
    max_value: float,
    label: str,
) -> None:
    numeric = _number(value)
    if numeric is None:
        _add_check(report, name, False, f"{label} must be an explicit numeric fraction", value=value)
        return
    if numeric >= 1:
        _add_check(
            report,
            name,
            False,
            f"{label} must be a fraction, not a whole-percent value like {numeric:g}",
            value=numeric,
        )
        return
    _add_check(
        report,
        name,
        0 < numeric <= max_value,
        f"{label} must be > 0 and <= {max_value:.2f}",
        value=numeric,
    )


def validate_live_canary_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate an unarmed live-canary config and return a static report.

    The validator fails closed: missing, ambiguous, zero, or unsafe values become
    blocking issues unless explicitly called out as a warning.
    """

    if not isinstance(config, dict):
        config = {}

    report: dict[str, Any] = {
        "ready": False,
        "status": "blocked",
        "checks": [],
        "issues": [],
        "warnings": [],
    }

    trading = config.get("trading") or {}
    if not isinstance(trading, dict):
        trading = {}

    mode = str(trading.get("mode") or "").strip().lower()
    _add_check(report, "runtime_live_mode", mode == "live", "trading.mode must be live", value=mode or None)

    trading_enabled = trading.get("enabled")
    _add_check(
        report,
        "trading_enabled_unarmed",
        _is_explicit_false(trading_enabled),
        "trading.enabled must be explicitly false for read-only canary preflight",
        value=trading_enabled,
    )

    nested_trading_enabled = trading.get("trading_enabled")
    if nested_trading_enabled is not None:
        _add_check(
            report,
            "trading_trading_enabled_unarmed",
            _is_explicit_false(nested_trading_enabled),
            "trading.trading_enabled must be false when present",
            value=nested_trading_enabled,
        )

    top_level_trading_enabled = config.get("trading_enabled")
    if top_level_trading_enabled is not None:
        _add_check(
            report,
            "top_level_trading_enabled_unarmed",
            _is_explicit_false(top_level_trading_enabled),
            "top-level trading_enabled must be false when present",
            value=top_level_trading_enabled,
        )

    cap_key = None
    cap_value = None
    if "max_tradable_balance_usd" in config:
        cap_key = "max_tradable_balance_usd"
        cap_value = config.get("max_tradable_balance_usd")
    elif "max_tradable_balance" in config:
        cap_key = "max_tradable_balance"
        cap_value = config.get("max_tradable_balance")
    cap = _number(cap_value)
    _add_check(
        report,
        "max_tradable_balance_cap",
        cap_key is not None and cap is not None and 0 < cap <= SAFE_MAX_TRADABLE_BALANCE_USD,
        f"explicit max_tradable_balance_usd or max_tradable_balance must be > 0 and <= {SAFE_MAX_TRADABLE_BALANCE_USD:.0f}",
        value={cap_key: cap_value} if cap_key else None,
    )

    max_position_size = _number(config.get("max_position_size_usd"))
    _add_check(
        report,
        "max_position_size_usd",
        max_position_size is not None and SAFE_MIN_POSITION_SIZE_USD <= max_position_size <= SAFE_MAX_POSITION_SIZE_USD,
        f"max_position_size_usd must be between {SAFE_MIN_POSITION_SIZE_USD:.0f} and {SAFE_MAX_POSITION_SIZE_USD:.0f}",
        value=max_position_size if max_position_size is not None else config.get("max_position_size_usd"),
    )

    _validate_fraction(
        report,
        name="daily_loss_limit_pct",
        value=_risk_value(config, "daily_loss_limit_pct"),
        max_value=SAFE_MAX_DAILY_LOSS_LIMIT_PCT,
        label="daily_loss_limit_pct",
    )
    _validate_fraction(
        report,
        name="max_drawdown_pct",
        value=_risk_value(config, "max_drawdown_pct"),
        max_value=SAFE_MAX_DRAWDOWN_PCT,
        label="max_drawdown_pct",
    )

    max_open_positions_raw = _risk_value(config, "max_open_positions")
    max_open_positions = _number(max_open_positions_raw)
    _add_check(
        report,
        "max_open_positions",
        max_open_positions is not None
        and max_open_positions.is_integer()
        and 1 <= int(max_open_positions) <= SAFE_MAX_OPEN_POSITIONS,
        f"max_open_positions must be an integer between 1 and {SAFE_MAX_OPEN_POSITIONS}",
        value=max_open_positions_raw,
    )

    single_trade_mode = trading.get("single_trade_mode")
    _add_check(
        report,
        "single_trade_mode",
        _is_explicit_true(single_trade_mode),
        "trading.single_trade_mode must be explicitly true",
        value=single_trade_mode,
    )

    block_on_degraded = _nested(trading, "live_reconciliation", "block_on_degraded")
    _add_check(
        report,
        "block_on_degraded",
        _is_explicit_true(block_on_degraded),
        "trading.live_reconciliation.block_on_degraded must be explicitly true",
        value=block_on_degraded,
    )

    live_identity = trading.get("live_identity")
    if not live_identity:
        report["warnings"].append("trading.live_identity is not configured; identity gate cannot compare expected runtime identity")
    elif not isinstance(live_identity, dict):
        _add_check(
            report,
            "live_identity_shape",
            False,
            "trading.live_identity must be a mapping when configured",
            value=type(live_identity).__name__,
        )
    else:
        report["warnings"].append(
            "trading.live_identity configured; preflight checked presence only and did not read credentials"
        )

    report["ready"] = not report["issues"]
    report["status"] = "ready" if report["ready"] else "blocked"
    return report


def format_live_canary_report(report: dict[str, Any]) -> str:
    """Render a human-readable preflight report."""

    status = str(report.get("status") or ("ready" if report.get("ready") else "blocked")).upper()
    lines = [f"Live canary preflight: {status}"]

    checks = report.get("checks") or []
    if checks:
        lines.append("")
        lines.append("Checks:")
        for check in checks:
            prefix = "PASS" if check.get("ok") else "FAIL"
            name = check.get("name", "check")
            message = check.get("message", "")
            lines.append(f"- {prefix} {name}: {message}")

    issues = report.get("issues") or []
    if issues:
        lines.append("")
        lines.append("Blocking issues:")
        for issue in issues:
            lines.append(f"- {issue}")

    warnings = report.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in warnings:
            lines.append(f"- {warning}")

    return "\n".join(lines)
