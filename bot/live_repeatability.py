"""Read-only repeatability evidence reports for supervised live canaries.

This module only reads local artifact files. It intentionally avoids importing
runner, exchange, config-loader, dotenv, and credential/env override paths.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.trade_audit import canonical_execution_status, coerce_float, validate_execution_audit_row


DIRECT_RECONCILIATION_FIELDS = {
    "verdict",
    "balance",
    "available_cash",
    "reserved_capital",
    "filled_exposure",
    "pending_exposure",
    "open_positions",
    "open_orders",
}
UNSAFE_STATES = {"blocked", "degraded"}
UNSAFE_VERDICTS = {"blocked", "degraded", "failed", "error"}
MONEY_EPSILON_USD = 0.01
MIN_REMAINING_EXPOSURE_USD = 0.01


def mode_storage_dir(path: str | Path, mode: str = "live") -> Path:
    base = Path(path)
    normalized = "live" if str(mode or "").strip().lower() == "live" else "paper"
    if base.name == normalized:
        return base
    if base.name in {"paper", "live"}:
        return base.parent / normalized
    return base / normalized


def data_dir_from_static_config(config: dict[str, Any]) -> Path:
    trading = config.get("trading") if isinstance(config.get("trading"), dict) else {}
    runtime = config.get("runtime") if isinstance(config.get("runtime"), dict) else {}
    logging_cfg = config.get("logging") if isinstance(config.get("logging"), dict) else {}
    mode = str((trading or {}).get("mode") or "live").strip().lower()
    base = (
        (runtime or {}).get("base_dir")
        or config.get("data_dir")
        or (logging_cfg or {}).get("log_dir")
        or config.get("log_dir")
        or "data"
    )
    return mode_storage_dir(base, mode)


def build_live_repeatability_report(data_dir: str | Path, *, sessions: int = 5) -> dict[str, Any]:
    root = Path(data_dir)
    report: dict[str, Any] = {
        "ready": False,
        "status": "blocked",
        "data_dir": str(root),
        "requested_sessions": max(1, int(sessions or 5)),
        "sessions_reviewed": 0,
        "checks": [],
        "issues": [],
        "warnings": [],
        "summary": {
            "total_lifecycle_events": 0,
            "total_reconciliation_events": 0,
            "total_trade_rows": 0,
            "total_risk_block_rows": 0,
            "direct_reconciliation_fields_present": False,
            "safety_pauses": 0,
            "degraded_indicators": 0,
            "contradictions": 0,
        },
        "sessions": [],
    }

    lifecycle_path = root / "lifecycle.jsonl"
    reconciliation_path = root / "reconciliation.jsonl"
    hourly_path = root / "hourly_summary.jsonl"
    trades_path = root / "trades.jsonl"
    risk_blocks_path = root / "risk_blocks.jsonl"

    lifecycle = _read_jsonl(lifecycle_path, report, required=True)
    reconciliation = _read_jsonl(reconciliation_path, report, required=True)
    hourly = _read_jsonl(hourly_path, report, required=True)
    trades = _read_jsonl(trades_path, report, required=True, allow_empty=True)
    risk_blocks = _read_jsonl(risk_blocks_path, report, required=True, allow_empty=True)

    report["summary"]["total_lifecycle_events"] = len(lifecycle)
    report["summary"]["total_reconciliation_events"] = len(reconciliation)
    report["summary"]["total_trade_rows"] = len(trades)
    report["summary"]["total_risk_block_rows"] = len(risk_blocks)

    _validate_artifact_timestamps(
        report,
        {
            "lifecycle.jsonl": lifecycle,
            "reconciliation.jsonl": reconciliation,
            "hourly_summary.jsonl": hourly,
            "trades.jsonl": trades,
            "risk_blocks.jsonl": risk_blocks,
        },
    )

    if report["issues"]:
        _finish(report)
        return report

    session_windows = _session_windows(lifecycle)
    if len(session_windows) < 2:
        _add_check(report, "minimum_sessions", False, "at least 2 supervised sessions with startup artifacts are required")
        _finish(report)
        return report
    _add_check(report, "minimum_sessions", True, "at least 2 supervised sessions with startup artifacts are present")

    selected = session_windows[-report["requested_sessions"] :]
    for index, window in enumerate(selected, start=1):
        session_report = _review_session(
            index=index,
            window=window,
            reconciliation=reconciliation,
            hourly=hourly,
            trades=trades,
            risk_blocks=risk_blocks,
        )
        report["sessions"].append(session_report)

    report["sessions_reviewed"] = len(report["sessions"])
    _roll_up_session_results(report)
    _finish(report)
    return report


def format_live_repeatability_report(report: dict[str, Any]) -> str:
    status = str(report.get("status") or ("ready" if report.get("ready") else "blocked")).upper()
    lines = [f"Live repeatability evidence: {status}"]
    lines.append(f"data_dir={report.get('data_dir')}")
    lines.append(f"sessions_reviewed={report.get('sessions_reviewed', 0)}")

    summary = report.get("summary") or {}
    lines.append(
        "artifacts="
        f"lifecycle:{summary.get('total_lifecycle_events', 0)} "
        f"reconciliation:{summary.get('total_reconciliation_events', 0)} "
        f"trades:{summary.get('total_trade_rows', 0)} "
        f"risk_blocks:{summary.get('total_risk_block_rows', 0)}"
    )

    if report.get("sessions"):
        lines.append("")
        lines.append("Sessions:")
        for session in report["sessions"]:
            verdict = "PASS" if session.get("ready") else "FAIL"
            start = session.get("started_at") or "unknown"
            end = session.get("ended_at") or "unknown"
            lines.append(
                f"- {verdict} {session.get('session_id')}: {start} -> {end}; "
                f"reconciliation={session.get('reconciliation_events', 0)} "
                f"trades={session.get('trade_rows', 0)} "
                f"issues={len(session.get('issues') or [])}"
            )

    if report.get("issues"):
        lines.append("")
        lines.append("Blocking issues:")
        for issue in report["issues"]:
            lines.append(f"- {issue}")

    if report.get("warnings"):
        lines.append("")
        lines.append("Warnings:")
        for warning in report["warnings"]:
            lines.append(f"- {warning}")

    return "\n".join(lines)


def _read_jsonl(path: Path, report: dict[str, Any], *, required: bool, allow_empty: bool = False) -> list[dict[str, Any]]:
    if not path.exists():
        if required:
            _add_check(report, f"artifact:{path.name}", False, f"missing required artifact: {path}")
        else:
            report["warnings"].append(f"optional artifact not found: {path}")
        return []

    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                _add_check(report, f"artifact:{path.name}", False, f"invalid JSON in {path}:{line_no}: {exc.msg}")
                continue
            if not isinstance(value, dict):
                _add_check(report, f"artifact:{path.name}", False, f"non-object JSON row in {path}:{line_no}")
                continue
            rows.append(value)

    if required and not rows and not allow_empty:
        _add_check(report, f"artifact:{path.name}", False, f"required artifact is empty: {path}")
    elif required:
        _add_check(report, f"artifact:{path.name}", True, f"required artifact present: {path.name}")
    return rows


def _session_windows(lifecycle: list[dict[str, Any]]) -> list[dict[str, Any]]:
    starts = [idx for idx, row in enumerate(lifecycle) if row.get("event") == "startup"]
    windows: list[dict[str, Any]] = []
    for ordinal, start_index in enumerate(starts, start=1):
        end_index = starts[ordinal] if ordinal < len(starts) else len(lifecycle)
        events = lifecycle[start_index:end_index]
        start_ts = _parse_ts(events[0].get("timestamp"))
        next_start_ts = _parse_ts(lifecycle[end_index].get("timestamp")) if end_index < len(lifecycle) else None
        shutdowns = [row for row in events if row.get("event") == "shutdown"]
        end_ts = _parse_ts((shutdowns[-1] if shutdowns else events[-1]).get("timestamp")) if events else None
        details = events[0].get("details") if isinstance(events[0].get("details"), dict) else {}
        session_id = str(details.get("session_id") or f"session-{ordinal}")
        windows.append(
            {
                "session_id": session_id,
                "start": start_ts,
                "end": end_ts,
                "next_start": next_start_ts,
                "events": events,
                "startup": events[0],
                "has_shutdown": bool(shutdowns),
            }
        )
    return windows


def _review_session(
    *,
    index: int,
    window: dict[str, Any],
    reconciliation: list[dict[str, Any]],
    hourly: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    risk_blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    session_reconciliation = _rows_in_window(reconciliation, window)
    session_hourly = _rows_in_window(hourly, window)
    session_trades = _rows_in_window(trades, window)
    session_risk_blocks = _rows_in_window(risk_blocks, window)
    startup_details = window["startup"].get("details") if isinstance(window["startup"].get("details"), dict) else {}
    issues: list[str] = []
    warnings: list[str] = []

    if str(startup_details.get("mode") or "").lower() != "live":
        issues.append("session mode is not live")
    if not window.get("has_shutdown"):
        issues.append("missing shutdown lifecycle event")
    if not session_reconciliation:
        issues.append("missing reconciliation evidence for session")

    direct_fields_present = any(DIRECT_RECONCILIATION_FIELDS.issubset(row.keys()) for row in session_reconciliation)
    if not direct_fields_present:
        issues.append("missing direct exchange-truth reconciliation fields")

    for row in session_reconciliation:
        issues.extend(_reconciliation_issues(row))
    for row in session_hourly:
        issues.extend(_hourly_issues(row))

    lifecycle_events = window.get("events") or []
    for event in lifecycle_events:
        if event.get("event") == "reconciliation_completed":
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            for issue in _reconciliation_issues(details):
                issues.append(f"lifecycle reconciliation event: {issue}")
    safety_pauses = [row for row in lifecycle_events if row.get("event") == "live_safety_pause"]
    failed_reconciliation = [row for row in lifecycle_events if row.get("event") == "reconciliation_failed"]
    unsafe_runtime_changes = [
        row
        for row in lifecycle_events
        if row.get("event") == "live_runtime_state_changed"
        and _nested_lower(row, "details", "state") in UNSAFE_STATES
    ]
    if safety_pauses:
        issues.append("live safety pause occurred")
    if failed_reconciliation:
        issues.append("reconciliation failure lifecycle event occurred")
    if unsafe_runtime_changes:
        issues.append("unsafe live runtime state change occurred")

    contradiction_count = 0
    for source_name, rows in (("trades", session_trades), ("risk_blocks", session_risk_blocks)):
        for row in rows:
            row_issues = _audit_row_issues(row)
            if row_issues:
                contradiction_count += len(row_issues)
                row_id = row.get("trade_id") or row.get("order_id") or "unknown"
                issues.append(f"{source_name} row {row_id} audit contradiction: {', '.join(row_issues)}")

    return {
        "session_id": window.get("session_id") or f"session-{index}",
        "started_at": _format_ts(window.get("start")),
        "ended_at": _format_ts(window.get("end")),
        "ready": not issues,
        "mode": startup_details.get("mode"),
        "lifecycle_events": len(lifecycle_events),
        "reconciliation_events": len(session_reconciliation),
        "hourly_summaries": len(session_hourly),
        "trade_rows": len(session_trades),
        "risk_block_rows": len(session_risk_blocks),
        "direct_reconciliation_fields_present": direct_fields_present,
        "safety_pauses": len(safety_pauses),
        "contradictions": contradiction_count,
        "issues": sorted(set(issues)),
        "warnings": warnings,
    }


def _roll_up_session_results(report: dict[str, Any]) -> None:
    direct_fields = False
    for session in report.get("sessions") or []:
        direct_fields = direct_fields or bool(session.get("direct_reconciliation_fields_present"))
        report["summary"]["safety_pauses"] += int(session.get("safety_pauses", 0) or 0)
        report["summary"]["contradictions"] += int(session.get("contradictions", 0) or 0)
        for issue in session.get("issues") or []:
            if "degraded" in issue or "blocked" in issue or "reconciliation failure" in issue:
                report["summary"]["degraded_indicators"] += 1
            report["issues"].append(f"{session.get('session_id')}: {issue}")
    report["summary"]["direct_reconciliation_fields_present"] = direct_fields
    _add_check(
        report,
        "direct_reconciliation_fields",
        direct_fields,
        "direct exchange-truth reconciliation fields are present in reviewed sessions",
    )


def _reconciliation_issues(row: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    verdict = str(row.get("verdict") or row.get("reconciliation_verdict") or row.get("status") or "").lower()
    if verdict in UNSAFE_VERDICTS:
        issues.append(f"reconciliation verdict is {verdict}")
    runtime_state = str(row.get("runtime_state") or row.get("state") or "").lower()
    if runtime_state in UNSAFE_STATES:
        issues.append(f"reconciliation runtime state is {runtime_state}")
    rec_issues = row.get("issues") or row.get("reconciliation_issues") or []
    if rec_issues:
        issues.append(f"reconciliation issues present: {', '.join(str(item) for item in rec_issues)}")

    numeric_fields = ("balance", "available_cash", "reserved_capital", "filled_exposure", "pending_exposure")
    for field in numeric_fields:
        value = coerce_float(row.get(field), default=None)
        if value is not None and value < -MONEY_EPSILON_USD:
            issues.append(f"negative {field} in reconciliation")

    reserved = coerce_float(row.get("reserved_capital"), default=None)
    filled = coerce_float(row.get("filled_exposure"), default=None)
    pending = coerce_float(row.get("pending_exposure"), default=None)
    if None not in (reserved, filled, pending) and abs((filled or 0.0) + (pending or 0.0) - (reserved or 0.0)) > MONEY_EPSILON_USD:
        issues.append("reserved capital does not match filled plus pending exposure")
    return issues


def _hourly_issues(row: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    runtime = row.get("live_runtime_state") if isinstance(row.get("live_runtime_state"), dict) else {}
    state = str((runtime or {}).get("state") or "").lower()
    if state in UNSAFE_STATES:
        issues.append(f"hourly runtime state is {state}")
    if (runtime or {}).get("issues"):
        issues.append("hourly runtime issues are present")
    gate = row.get("reconciliation_gate") if isinstance(row.get("reconciliation_gate"), dict) else {}
    if gate:
        issues.append("hourly reconciliation gate is active")
    safety_pause = row.get("safety_pause") if isinstance(row.get("safety_pause"), dict) else {}
    if safety_pause and safety_pause.get("active"):
        issues.append("hourly safety pause is active")
    return issues


def _audit_row_issues(row: dict[str, Any]) -> list[str]:
    issues = validate_execution_audit_row(dict(row))
    status = canonical_execution_status(
        row.get("status"),
        filled_size=row.get("filled_size"),
        placed_size=row.get("placed_size"),
        remaining_size=row.get("remaining_size"),
    )
    if status == "canceled" and (coerce_float(row.get("remaining_size"), default=0.0) or 0.0) > MIN_REMAINING_EXPOSURE_USD:
        issues.append("canceled_order_has_remaining_exposure")
    if status in {"rejected", "failed"} and (coerce_float(row.get("filled_size"), default=0.0) or 0.0) > MIN_REMAINING_EXPOSURE_USD:
        issues.append(f"{status}_row_has_fill")
    return sorted(set(issues))


def _rows_in_window(rows: list[dict[str, Any]], window: dict[str, Any]) -> list[dict[str, Any]]:
    start = window.get("start")
    next_start = window.get("next_start")
    if start is None:
        return []
    selected: list[dict[str, Any]] = []
    for row in rows:
        ts = _row_ts(row)
        if ts is None:
            continue
        if ts < start:
            continue
        if next_start is not None and ts >= next_start:
            continue
        selected.append(row)
    return selected


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_ts(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _validate_artifact_timestamps(report: dict[str, Any], artifacts: dict[str, list[dict[str, Any]]]) -> None:
    for artifact_name, rows in artifacts.items():
        for row_number, row in enumerate(rows, start=1):
            if _row_ts(row) is not None:
                continue
            label = _row_label(row)
            fields = ", ".join(_row_ts_fields(row)) or "timestamp/resolved_at/created_at"
            _add_check(
                report,
                f"artifact_time:{artifact_name}",
                False,
                f"{artifact_name} row {row_number} ({label}) has no parseable time anchor in {fields}",
            )


def _row_ts(row: dict[str, Any]) -> datetime | None:
    for field in _row_ts_fields(row):
        parsed = _parse_ts(row.get(field))
        if parsed is not None:
            return parsed
    return None


def _row_ts_fields(row: dict[str, Any]) -> list[str]:
    return [field for field in ("timestamp", "resolved_at", "created_at") if row.get(field)]


def _row_label(row: dict[str, Any]) -> str:
    for field in ("session_id", "trade_id", "order_id", "market_id", "event", "source"):
        value = row.get(field)
        if value:
            return f"{field}={value}"
    return "unlabeled"


def _nested_lower(row: dict[str, Any], *keys: str) -> str:
    node: Any = row
    for key in keys:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    return str(node or "").lower()


def _add_check(report: dict[str, Any], name: str, ok: bool, message: str) -> None:
    report["checks"].append({"name": name, "ok": bool(ok), "message": message})
    if not ok:
        report["issues"].append(message)


def _finish(report: dict[str, Any]) -> None:
    report["issues"] = sorted(set(report.get("issues") or []))
    report["warnings"] = sorted(set(report.get("warnings") or []))
    report["ready"] = not report["issues"] and report.get("sessions_reviewed", 0) >= 2
    report["status"] = "ready" if report["ready"] else "blocked"
