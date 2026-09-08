"""Configuration-driven, derived-only beta maintenance orchestration.

A frequent scheduler may call this module, but the configured intervals decide
when the independent resolver and the lower-frequency SourceWriter promotion
actually run.  The collector is deliberately not a child of this workflow.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from bot.auto_source_router_promotion import auto_populate_source_router_history
from bot.file_ops import atomic_write_json
from bot.resolution_feed import ResolutionFeedResult, run_resolution_feed_once

DEFAULT_RESOLUTION_INTERVAL_SECONDS = 1800
DEFAULT_PROMOTION_INTERVAL_SECONDS = 7200


@dataclass(frozen=True, slots=True)
class DerivedMaintenanceResult:
    resolution_status: str
    resolution_ran: bool
    promotion_status: str
    promotion_ran: bool
    state_path: Path


def run_derived_maintenance_once(
    config: Mapping[str, Any],
    *,
    now: datetime | None = None,
    resolve: Callable[..., ResolutionFeedResult] = run_resolution_feed_once,
    promote: Callable[..., Any] = auto_populate_source_router_history,
) -> DerivedMaintenanceResult:
    """Run only the due, configured derived stages after the collector.

    A promotion is attempted only immediately after a successful independent
    resolution refresh.  State advances only after each stage succeeds, so a
    failed promotion remains due on the next resolver refresh.
    """
    maintenance = _mapping(config.get("derived_maintenance"))
    promotion = _mapping(maintenance.get("source_router_promotion"))
    if not maintenance.get("enabled", False):
        return DerivedMaintenanceResult("disabled", False, "disabled", False, _state_path(maintenance, promotion, required=False))
    state_path = _state_path(maintenance, promotion)
    if not promotion.get("enabled", False):
        return DerivedMaintenanceResult("not_run", False, "disabled", False, state_path)

    now_dt = _as_utc(now) or datetime.now(timezone.utc)
    state = _load_state(state_path)
    resolution_interval = _positive_seconds(maintenance.get("resolution_interval_seconds"), DEFAULT_RESOLUTION_INTERVAL_SECONDS)
    if not _is_due(state.get("last_successful_resolution_at"), now_dt, resolution_interval):
        return DerivedMaintenanceResult("skipped_not_due", False, "skipped_resolution_not_refreshed", False, state_path)

    resolution = resolve(config, force=True)
    if not resolution.refreshed:
        return DerivedMaintenanceResult(resolution.status, True, "skipped_resolution_not_refreshed", False, state_path)

    state["last_successful_resolution_at"] = _iso(now_dt)
    _write_state(state_path, state)
    promotion_interval = _positive_seconds(promotion.get("interval_seconds"), DEFAULT_PROMOTION_INTERVAL_SECONDS)
    if not _is_due(state.get("last_successful_promotion_at"), now_dt, promotion_interval):
        _write_state(state_path, state)
        return DerivedMaintenanceResult(resolution.status, True, "skipped_not_due", False, state_path)

    resolution_output_path = resolution.output_path
    if resolution_output_path is None:
        return DerivedMaintenanceResult(resolution.status, True, "skipped_resolution_output_missing", False, state_path)

    collector_snapshots_path = _required_path(promotion, "collector_snapshots_path")
    configured_resolution_path = _required_path(promotion, "strict_resolutions_path")
    if not _same_path(configured_resolution_path, resolution_output_path):
        return DerivedMaintenanceResult(resolution.status, True, "skipped_resolution_output_mismatch", False, state_path)
    output_root = promotion.get("output_root")
    result = promote(
        collector_snapshots_path=collector_snapshots_path,
        strict_resolutions_path=resolution_output_path,
        output_root=output_root,
        storage_root=_mapping(config.get("runtime")).get("storage_root"),
    )
    status = _promotion_status(result)
    state["last_successful_promotion_at"] = _iso(now_dt)
    state["last_promotion_status"] = status
    _write_state(state_path, state)
    return DerivedMaintenanceResult(resolution.status, True, status, True, state_path)


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _state_path(maintenance: Mapping[str, Any], promotion: Mapping[str, Any], *, required: bool = True) -> Path:
    configured = maintenance.get("state_path") or promotion.get("state_path")
    if configured in (None, ""):
        if required:
            raise ValueError("derived_maintenance.state_path is required")
        return Path("data/derived_maintenance/state.json").resolve()
    return Path(str(configured)).expanduser().resolve()


def _required_path(config: Mapping[str, Any], key: str) -> str:
    value = config.get(key)
    if value in (None, ""):
        raise ValueError(f"derived_maintenance.source_router_promotion.{key} is required")
    return str(value)


def _same_path(configured: str, actual: Path) -> bool:
    return Path(configured).expanduser().resolve() == actual.expanduser().resolve()


def _positive_seconds(value: object, default: int) -> int:
    candidate = default if value in (None, "") else value
    try:
        seconds = int(str(candidate))
    except (TypeError, ValueError) as exc:
        raise ValueError("derived maintenance intervals must be positive integer seconds") from exc
    if seconds <= 0:
        raise ValueError("derived maintenance intervals must be positive integer seconds")
    return seconds


def _load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"derived maintenance state is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"derived maintenance state must be a JSON object: {path}")
    return value


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    payload = {
        "schema_name": "derived_maintenance_state",
        "schema_version": 1,
        **dict(state),
    }
    atomic_write_json(path, payload)


def _is_due(previous: object, now: datetime, interval_seconds: int) -> bool:
    if not isinstance(previous, str):
        return True
    try:
        prior = _as_utc(datetime.fromisoformat(previous))
    except ValueError:
        return True
    return prior is None or (now - prior).total_seconds() >= interval_seconds


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _promotion_status(result: object) -> str:
    if isinstance(result, Mapping):
        status = result.get("status")
    else:
        status = getattr(result, "status", None)
    return str(status or "completed")
